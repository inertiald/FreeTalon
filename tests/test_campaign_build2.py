"""Campaign build #2 (2026-11-23): three coupled loop changes.

1. Decline observability: both repair seams (cycle-8 local repair,
   scoped-edit repair) record a structured reason for every
   decline through an optional ``declines`` out-param — arm #10's
   scoped seam declined its only repair opportunity silently, and
   a decline was observable only as the absence of a success.
   The loop audit-logs each record and stamps the list into the
   new plan version's metadata.

2. Step 1.5, the modify-path rule: a plan write node targeting an
   EXISTING file must express the change as edits (the new scoped
   "edits" plan form, materialized at parse like the anchored
   form, or an anchored edit) — an authored whole-file rewrite of
   an existing file is a plan error unless it carries the gated
   override (overwrite_justification + an impact-check node).
   Plus pair support in the scoped-edit seam: a suspect span of
   exactly two files is one candidate, per-file all-or-nothing.

3. Tiered criteria: a full-suite run belongs only on a plan's
   final gate node (or under an explicit suite_justification);
   node criteria default to targeted tests.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import loop as loop_module
from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.planner import parse_planner_response
from freetalon.orchestrator.repo_index import build_repo_index
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.scoped_edits import (
    ScopedEdit,
    ScopedEditError,
    apply_scoped_edits,
)
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

STORE_TEXT = '''"""Fixture store."""

CONSTANT = 42


def helper():
    return CONSTANT


def register(name):
    # DEFECT: returns the name unchanged instead of a code.
    return name


def unregister(name):
    return None
'''

FIXED_STORE_TEXT = '''"""Fixture store."""

CONSTANT = 42


def helper():
    return CONSTANT


def register(name):
    return "code-" + name


def unregister(name):
    return None
'''

SERVER_TEXT = '''"""Fixture server."""


def make_server():
    return None
'''

FIXED_SERVER_TEXT = '''"""Fixture server."""


def make_server():
    return "server"
'''

EXTRA_TEXT = '''"""Fixture extra."""


def extra():
    return None
'''

TEST_TEXT = '''import unittest

from pkg import store


class StoreTests(unittest.TestCase):
    def test_register_returns_code(self):
        self.assertEqual(store.register("alpha"), "code-alpha")
'''

PAIR_TEST_TEXT = '''import unittest

from pkg import server, store


class PairTests(unittest.TestCase):
    def test_register_and_server(self):
        self.assertEqual(store.register("alpha"), "code-alpha")
        self.assertIsNotNone(server.make_server())
'''

TRIPLE_TEST_TEXT = '''import unittest

from pkg import extra, server, store


class TripleTests(unittest.TestCase):
    def test_all(self):
        self.assertEqual(store.register("alpha"), "code-alpha")
'''

GOAL = (
    "Build a small fixture package whose store register function "
    "returns a code string for a name."
)

FAILING_ID = "tests.test_store.StoreTests.test_register_returns_code"
PAIR_FAILING_ID = "tests.test_pairing.PairTests.test_register_and_server"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _block(search: str, replace: str) -> str:
    return (
        "<<<<<<< SEARCH\n"
        f"{search}\n"
        "=======\n"
        f"{replace}\n"
        ">>>>>>> REPLACE\n"
    )


DEFECTIVE_REGISTER = '''def register(name):
    # DEFECT: returns the name unchanged instead of a code.
    return name'''

FIXED_REGISTER = '''def register(name):
    return "code-" + name'''


class SeamCase(unittest.TestCase):
    """The step-1 seam fixture (store + its test), extended with a
    server module, an extra module, and pair/triple test files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store_path = self.root / "pkg" / "store.py"
        self.server_path = self.root / "pkg" / "server.py"
        self.extra_path = self.root / "pkg" / "extra.py"
        self.test_path = self.root / "tests" / "test_store.py"
        self.pair_test_path = self.root / "tests" / "test_pairing.py"
        self.triple_test_path = self.root / "tests" / "test_triple.py"
        _write(self.root / "pkg" / "__init__.py", "")
        _write(self.store_path, STORE_TEXT)
        _write(self.server_path, SERVER_TEXT)
        _write(self.extra_path, EXTRA_TEXT)
        _write(self.test_path, TEST_TEXT)
        _write(self.pair_test_path, PAIR_TEST_TEXT)
        _write(self.triple_test_path, TRIPLE_TEST_TEXT)

    def _write_node(self, node_id: str, path: Path, content: str) -> PlanNode:
        return PlanNode(
            id=node_id,
            objective=f"Write {path.name}",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={"file_path": str(path), "content": content},
            outputs=[str(path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(path)}],
            depends_on=[],
            status=PlanStatus.COMPLETED,
        )

    def _run_node(
        self, node_id: str, test_path: Path, depends: list[str]
    ) -> PlanNode:
        return PlanNode(
            id=node_id,
            objective="Run the tests",
            hypothesis="the tests pass",
            assigned_claw="bash",
            inputs={"command": f"./.venv/bin/python {test_path}"},
            outputs=[],
            acceptance=["The tests pass."],
            criteria=[{"type": "exit_code", "expected": 0}],
            depends_on=depends,
            status=PlanStatus.FAILED,
            error="exit_code 1 != 0",
            exit_code=1,
            measurement={
                "verdict": "FAIL",
                "results": [
                    {
                        "criterion": {"type": "exit_code", "expected": 0},
                        "detail": "exit_code 1 != 0",
                        "measured": True,
                        "passed": False,
                    }
                ],
                "exit_code": 1,
            },
        )

    def _nodes(self) -> list[PlanNode]:
        return [
            self._write_node("node-1", self.store_path, STORE_TEXT),
            self._write_node("node-2", self.test_path, TEST_TEXT),
            self._run_node("node-3", self.test_path, ["node-1", "node-2"]),
        ]

    def _plan(self, nodes: list[PlanNode]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-build2",
            intent=TaskIntent(
                goal=GOAL, constraints={"workspace": str(self.root)}
            ),
            nodes=nodes,
            metadata={"repo_root": str(self.root)},
        )

    def _brief(self, **overrides) -> dict:
        brief = {
            "goal": {"objective": GOAL},
            "plan_id": "plan-build2",
            "plan_version": 1,
            "failing_tests": [FAILING_ID],
            "produced_files": [
                "pkg/store.py",
                "pkg/server.py",
                "pkg/extra.py",
                "tests/test_store.py",
                "tests/test_pairing.py",
                "tests/test_triple.py",
            ],
            "failed_nodes": [
                {
                    "id": "node-3",
                    "diagnostics": "AssertionError: 'alpha' != 'code-alpha'",
                }
            ],
            "measurements": [],
            "past_experiments": [],
            "remaining_budget": {"replans": 2},
        }
        brief.update(overrides)
        return brief

    def _run_scoped(self, response: str, nodes=None, brief=None):
        plan = self._plan(self._nodes() if nodes is None else nodes)
        declines: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return response

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan,
                self._brief() if brief is None else brief,
                SETTINGS,
                declines=declines,
            )
        return result, declines

    def _run_local(self, response: str, nodes=None, brief=None):
        plan = self._plan(self._nodes() if nodes is None else nodes)
        declines: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return response

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_local_repair(
                plan,
                self._brief() if brief is None else brief,
                SETTINGS,
                declines=declines,
            )
        return result, declines


class TestScopedEditErrorKinds(unittest.TestCase):
    def test_kinds(self):
        cases = [
            ("abc", [], "empty_set"),
            ("abc", [ScopedEdit("", "x")], "empty_search"),
            ("abc", [ScopedEdit("a", "a")], "no_op_edit"),
            ("abc", [ScopedEdit("zzz", "x")], "not_found"),
            # A search text occurring more than once is ambiguous.
            ("aaa", [ScopedEdit("a", "x")], "ambiguous"),
            (
                "abc",
                [ScopedEdit("ab", "x"), ScopedEdit("bc", "y")],
                "overlap",
            ),
            # Two edits over the identical span overlap.
            (
                "abc",
                [ScopedEdit("a", "x"), ScopedEdit("a", "y")],
                "overlap",
            ),
        ]
        for text, edits, kind in cases:
            with self.assertRaises(ScopedEditError) as caught:
                apply_scoped_edits(text, edits)
            self.assertEqual(caught.exception.kind, kind)


class TestScopedSeamDeclines(SeamCase):
    def test_no_failing_tests_records_no_diagnostics(self):
        result, declines = self._run_scoped(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER),
            brief=self._brief(failing_tests=[]),
        )
        self.assertIsNone(result)
        self.assertEqual(len(declines), 1)
        self.assertEqual(declines[0]["seam"], "scoped_edit_repair")
        self.assertEqual(declines[0]["reason"], "no_diagnostics")

    def test_no_edit_blocks(self):
        result, declines = self._run_scoped("the whole file, rewritten")
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["no_edit_blocks"])

    def test_malformed_edit_blocks(self):
        result, declines = self._run_scoped(
            "<<<<<<< SEARCH\nfoo\n>>>>>>> REPLACE\n"
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["malformed_edit_blocks"]
        )

    def test_blocks_failed_to_apply_carries_kind(self):
        result, declines = self._run_scoped(
            _block("def register(name):\n    return 42", FIXED_REGISTER)
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["blocks_failed_to_apply"]
        )
        self.assertIn("not_found", declines[0]["detail"])

    def test_syntax_error(self):
        result, declines = self._run_scoped(
            _block(DEFECTIVE_REGISTER, "def register(name):\n    return (")
        )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["syntax_error"])

    def test_disk_drift(self):
        _write(self.store_path, STORE_TEXT + "\n# drifted\n")
        result, declines = self._run_scoped(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER)
        )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["disk_drift"])

    def test_multiple_writers(self):
        nodes = self._nodes()
        nodes.append(
            self._write_node("node-4", self.store_path, STORE_TEXT)
        )
        result, declines = self._run_scoped(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER), nodes=nodes
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["multiple_writers"]
        )

    def test_three_module_span_declines_suspect_not_unique(self):
        nodes = [
            self._write_node("node-1", self.store_path, STORE_TEXT),
            self._write_node("node-2", self.server_path, SERVER_TEXT),
            self._write_node("node-3", self.extra_path, EXTRA_TEXT),
            self._write_node("node-4", self.triple_test_path, TRIPLE_TEST_TEXT),
            self._run_node(
                "node-5", self.triple_test_path, ["node-1", "node-2", "node-3"]
            ),
        ]
        brief = self._brief(
            failing_tests=["tests.test_triple.TripleTests.test_all"],
            failed_nodes=[
                {"id": "node-5", "diagnostics": "AssertionError"}
            ],
        )
        result, declines = self._run_scoped(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER),
            nodes=nodes,
            brief=brief,
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["suspect_not_unique"]
        )
        self.assertIn("3 produced modules", declines[0]["detail"])

    def test_success_records_no_decline(self):
        result, declines = self._run_scoped(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER)
        )
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])


class TestScopedTargetDeclinesDirect(SeamCase):
    """Reason paths inside _scoped_edit_targets that the seam's own
    entry conditions make unreachable (the diagnostics block is
    empty first) — exercised at the unit that owns them."""

    def _targets(self, brief, suspect_map):
        declines: list[dict] = []
        plan = self._plan(self._nodes())
        result = planner_module._scoped_edit_targets(
            plan, brief, self.root, suspect_map, declines
        )
        return result, declines

    def test_no_failing_tests(self):
        result, declines = self._targets(self._brief(failing_tests=[]), {})
        self.assertIsNone(result)
        self.assertEqual(declines[0]["reason"], "no_failing_tests")

    def test_suspect_cap_exceeded(self):
        brief = self._brief(
            failing_tests=[f"tests.test_store.T.test_{i}" for i in range(11)]
        )
        result, declines = self._targets(brief, {})
        self.assertIsNone(result)
        self.assertEqual(declines[0]["reason"], "suspect_cap_exceeded")

    def test_no_suspect_module(self):
        suspect = {FAILING_ID: {"file": "tests/test_store.py", "modules": []}}
        result, declines = self._targets(self._brief(), suspect)
        self.assertIsNone(result)
        self.assertEqual(declines[0]["reason"], "no_suspect_module")

    def test_test_file_target(self):
        suspect = {
            FAILING_ID: {
                "file": "tests/test_store.py",
                "modules": ["tests/test_store.py"],
            }
        }
        result, declines = self._targets(self._brief(), suspect)
        self.assertIsNone(result)
        self.assertEqual(declines[0]["reason"], "test_file_target")

    def test_file_too_large(self):
        big = '"""Big."""\n\nVALUE = "' + "x" * 24_500 + '"\n'
        big_path = self.root / "pkg" / "big.py"
        _write(big_path, big)
        nodes = [self._write_node("node-1", big_path, big)]
        plan = self._plan(nodes)
        declines: list[dict] = []
        suspect = {FAILING_ID: {"file": "tests/test_store.py", "modules": ["pkg/big.py"]}}
        result = planner_module._scoped_edit_targets(
            plan, self._brief(), self.root, suspect, declines
        )
        self.assertIsNone(result)
        self.assertEqual(declines[0]["reason"], "file_too_large")


class TestPairRepair(SeamCase):
    def _pair_nodes(self) -> list[PlanNode]:
        return [
            self._write_node("node-1", self.store_path, STORE_TEXT),
            self._write_node("node-2", self.server_path, SERVER_TEXT),
            self._write_node("node-3", self.pair_test_path, PAIR_TEST_TEXT),
            self._run_node(
                "node-4", self.pair_test_path, ["node-1", "node-2", "node-3"]
            ),
        ]

    def _pair_brief(self) -> dict:
        return self._brief(
            failing_tests=[PAIR_FAILING_ID],
            failed_nodes=[{"id": "node-4", "diagnostics": "AssertionError"}],
        )

    def _pair_response(self, store_replace=FIXED_REGISTER, server_block=None):
        if server_block is None:
            server_block = _block(
                "def make_server():\n    return None",
                'def make_server():\n    return "server"',
            )
        return (
            "FILE: pkg/store.py\n"
            + _block(DEFECTIVE_REGISTER, store_replace)
            + "\nFILE: pkg/server.py\n"
            + server_block
        )

    def test_pair_success_edits_both_files(self):
        result, declines = self._run_scoped(
            self._pair_response(),
            nodes=self._pair_nodes(),
            brief=self._pair_brief(),
        )
        self.assertEqual(declines, [])
        self.assertIsNotNone(result)
        by_id = {node.id: node for node in result}
        self.assertEqual(
            by_id["node-1"].inputs["content"], FIXED_STORE_TEXT
        )
        self.assertEqual(
            by_id["node-2"].inputs["content"], FIXED_SERVER_TEXT
        )
        self.assertIn("(pair)", by_id["node-1"].differs_by)
        self.assertIn("(pair)", by_id["node-2"].differs_by)
        # The untouched test node is preserved verbatim.
        self.assertEqual(
            by_id["node-3"].inputs["content"], PAIR_TEST_TEXT
        )

    def test_pair_is_atomic_when_one_file_fails(self):
        bad_server = _block(
            "def make_server():\n    return 42",
            'def make_server():\n    return "server"',
        )
        result, declines = self._run_scoped(
            self._pair_response(server_block=bad_server),
            nodes=self._pair_nodes(),
            brief=self._pair_brief(),
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["blocks_failed_to_apply"]
        )
        self.assertIn("pkg/server.py", declines[0]["detail"])

    def test_pair_missing_section(self):
        response = "FILE: pkg/store.py\n" + _block(
            DEFECTIVE_REGISTER, FIXED_REGISTER
        )
        result, declines = self._run_scoped(
            response, nodes=self._pair_nodes(), brief=self._pair_brief()
        )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["no_edit_blocks"])

    def test_pair_blocks_without_headers_are_malformed(self):
        response = _block(DEFECTIVE_REGISTER, FIXED_REGISTER) + _block(
            "def make_server():\n    return None",
            'def make_server():\n    return "server"',
        )
        result, declines = self._run_scoped(
            response, nodes=self._pair_nodes(), brief=self._pair_brief()
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["malformed_edit_blocks"]
        )


class TestLocalRepairDeclines(SeamCase):
    def test_no_failed_nodes(self):
        result, declines = self._run_local(
            "irrelevant", brief=self._brief(failed_nodes=[])
        )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["no_failed_nodes"])

    def test_failed_node_is_write(self):
        brief = self._brief(
            failed_nodes=[
                {
                    "id": "node-1",
                    "diagnostics": "write failed",
                    "measurement": {
                        "verdict": "FAIL",
                        "results": [
                            {"measured": True, "passed": False}
                        ],
                    },
                }
            ]
        )
        result, declines = self._run_local("irrelevant", brief=brief)
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["failed_node_is_write"]
        )

    def test_window_not_identified_when_frame_names_no_definition(self):
        # The frame lands in the suspect module but outside every
        # definition (line 1 is the module docstring), and no route
        # anchor applies: the suspect is unique, the window is not.
        diagnostics = (
            "Traceback (most recent call last):\n"
            f'  File "{self.store_path}", line 1, in <module>\n'
            "AssertionError: boom"
        )
        brief = self._brief(
            failed_nodes=[{"id": "node-3", "diagnostics": diagnostics}]
        )
        result, declines = self._run_local("irrelevant", brief=brief)
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["window_not_identified"]
        )

    def test_suspect_not_unique_when_nothing_links(self):
        # The standard fixture: the failure's diagnostics carry no
        # traceback frame and nothing else links the failure to a
        # produced module, so there is no unique suspect.
        result, declines = self._run_local("irrelevant")
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["suspect_not_unique"]
        )

    def test_suspect_not_unique_from_two_frames(self):
        diagnostics = (
            "Traceback (most recent call last):\n"
            f'  File "{self.store_path}", line 11, in register\n'
            f'  File "{self.server_path}", line 4, in make_server\n'
            "AssertionError: 'alpha' != 'code-alpha'"
        )
        nodes = self._nodes()
        nodes.append(
            self._write_node("node-4", self.server_path, SERVER_TEXT)
        )
        brief = self._brief(
            failed_nodes=[{"id": "node-3", "diagnostics": diagnostics}]
        )
        result, declines = self._run_local(
            "irrelevant", nodes=nodes, brief=brief
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["suspect_not_unique"]
        )

    def test_disk_drift(self):
        drifted = STORE_TEXT.replace(
            "    return name\n\n\ndef unregister",
            "    return name + 'x'\n\n\ndef unregister",
        )
        _write(self.store_path, drifted)
        diagnostics = (
            "Traceback (most recent call last):\n"
            f'  File "{self.store_path}", line 11, in register\n'
            "AssertionError: 'alpha' != 'code-alpha'"
        )
        brief = self._brief(
            failed_nodes=[{"id": "node-3", "diagnostics": diagnostics}]
        )
        result, declines = self._run_local("irrelevant", brief=brief)
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["disk_drift"])

    def test_no_definition_extracted(self):
        diagnostics = (
            "Traceback (most recent call last):\n"
            f'  File "{self.store_path}", line 11, in register\n'
            "AssertionError: 'alpha' != 'code-alpha'"
        )
        brief = self._brief(
            failed_nodes=[{"id": "node-3", "diagnostics": diagnostics}]
        )
        result, declines = self._run_local(
            "I cannot repair this definition.", brief=brief
        )
        self.assertIsNone(result)
        self.assertEqual(
            [d["reason"] for d in declines], ["no_definition_extracted"]
        )


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class TestLoopDeclineStamping(unittest.TestCase):
    def test_declines_are_logged_and_stamped_into_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            audit = _AuditCapture()
            runner = LoopRunner(
                store,
                registry,
                run_store=RunStore(Path(tmp) / "runs"),
                memory_store=ExperimentStore(Path(tmp) / "memory.db"),
                audit_logger=audit,
            )
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=2)
            )
            plan = ExecutionPlan(
                plan_id="plan-declines",
                intent=TaskIntent(goal="test goal"),
                nodes=[
                    PlanNode(
                        id="node-1",
                        objective="exit zero",
                        assigned_claw="bash",
                        inputs={"command": "exit 1"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the command exits zero",
                    )
                ],
            )
            store.save(plan)

            def fake_local(plan_, brief, settings=None, declines=None):
                if declines is not None:
                    declines.append(
                        {
                            "seam": "local_repair",
                            "reason": "suspect_not_unique",
                            "detail": "test double",
                        }
                    )
                return None

            def fake_scoped(plan_, brief, settings=None, declines=None):
                if declines is not None:
                    declines.append(
                        {
                            "seam": "scoped_edit_repair",
                            "reason": "no_edit_blocks",
                            "detail": "test double",
                        }
                    )
                return None

            def fake_replan(brief):
                return [
                    PlanNode(
                        id="node-1",
                        objective="exit zero",
                        assigned_claw="bash",
                        inputs={"command": "true"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the command exits zero",
                    )
                ]

            with (
                mock.patch.object(loop_module, "try_local_repair", fake_local),
                mock.patch.object(
                    loop_module, "try_scoped_edit_repair", fake_scoped
                ),
                mock.patch.object(
                    loop_module, "replan_from_brief", fake_replan
                ),
            ):
                result = runner.run_plan(plan, contract)
            self.assertEqual(result.goal_verdict, "PASS")
            saved = store.load("plan-declines")
            self.assertIsNotNone(saved)
            declines = saved.metadata.get("repair_declines")
            self.assertEqual(
                [d["reason"] for d in declines],
                ["suspect_not_unique", "no_edit_blocks"],
            )
            events = [event for event, _fields in audit.events]
            self.assertIn("loop.replan.local_repair_declined", events)
            self.assertIn("loop.replan.scoped_edit_repair_declined", events)
            reasons = {
                fields.get("reason")
                for event, fields in audit.events
                if event.endswith("_declined")
            }
            self.assertEqual(reasons, {"suspect_not_unique", "no_edit_blocks"})


ANCHOR_MODULE = '''"""Anchor fixture."""


def alpha():
    return 1


def beta():
    return 2


def gamma():
    return 3


def delta():
    return 4
'''

NEWMOD_TEXT = '''"""New module."""


def compute():
    return 1
'''


def _raw_node(node_id, claw, inputs, criteria, depends_on=None, outputs=None):
    return {
        "id": node_id,
        "objective": f"Step {node_id}",
        "depends_on": depends_on or [],
        "assigned_claw": claw,
        "inputs": inputs,
        "outputs": outputs if outputs is not None else [],
        "acceptance": ["The step's effect holds."],
        "criteria": criteria,
        "error": None,
    }


class TestScopedEditPlanForm(SeamCase):
    def _parse(self, nodes):
        parsed, _metadata = parse_planner_response(
            json.dumps({"nodes": nodes}),
            goal_text=GOAL,
            workspace=self.root,
            repo_root=self.root,
        )
        return parsed

    def test_edits_form_materializes_and_preserves_untouched(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "edits": [
                    {"search": DEFECTIVE_REGISTER, "replace": FIXED_REGISTER}
                ],
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            outputs=[str(self.store_path)],
        )
        nodes = self._parse([node])
        materialized = nodes[0]
        self.assertEqual(materialized.inputs["content"], FIXED_STORE_TEXT)
        self.assertEqual(
            materialized.inputs["_materialized_from"], "scoped_edits"
        )
        self.assertNotIn("edits", materialized.inputs)
        # Untouched regions are byte-identical to the original.
        for region in ("def helper():\n    return CONSTANT", "def unregister"):
            self.assertIn(region, materialized.inputs["content"])

    def test_create_new_file_keeps_whole_file_content(self):
        new_path = self.root / "pkg" / "newmod.py"
        node = _raw_node(
            "step_1",
            "scaffold",
            {"file_path": str(new_path), "content": NEWMOD_TEXT},
            [{"type": "file_exists", "path": str(new_path)}],
            outputs=[str(new_path)],
        )
        nodes = self._parse([node])
        self.assertEqual(nodes[0].inputs["content"], NEWMOD_TEXT)
        self.assertNotIn("_materialized_from", nodes[0].inputs)

    def test_edits_against_missing_file_rejected(self):
        missing = self.root / "pkg" / "nope.py"
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(missing),
                "edits": [{"search": "a", "replace": "b"}],
            },
            [{"type": "file_exists", "path": str(missing)}],
            outputs=[str(missing)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([node])
        codes = [r.code for r in caught.exception.plan_errors]
        self.assertEqual(codes, ["scoped_edit"])
        self.assertIn("does not exist", str(caught.exception))

    def test_edits_and_content_together_rejected(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "content": FIXED_STORE_TEXT,
                "edits": [
                    {"search": DEFECTIVE_REGISTER, "replace": FIXED_REGISTER}
                ],
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            outputs=[str(self.store_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([node])
        self.assertIn("carries both", str(caught.exception))

    def test_edits_search_not_found_rejected_with_kind(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "edits": [{"search": "def missing():", "replace": "x"}],
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            outputs=[str(self.store_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([node])
        self.assertIn("not_found", str(caught.exception))

    def test_edit_then_edit_in_one_plan_chains(self):
        # The materialize sources chain: a second edits node on the
        # same file applies to the first node's result, not to the
        # stale file on disk (the anchored form's established
        # chaining contract).
        first = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "edits": [
                    {"search": DEFECTIVE_REGISTER, "replace": FIXED_REGISTER}
                ],
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            outputs=[str(self.store_path)],
        )
        second = _raw_node(
            "step_2",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "edits": [
                    {
                        "search": "def unregister(name):\n    return None",
                        "replace": "def unregister(name):\n    return True",
                    }
                ],
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            depends_on=["step_1"],
            outputs=[str(self.store_path)],
        )
        nodes = self._parse([first, second])
        expected = FIXED_STORE_TEXT.replace(
            "def unregister(name):\n    return None",
            "def unregister(name):\n    return True",
        )
        self.assertEqual(nodes[1].inputs["content"], expected)
        self.assertEqual(
            nodes[1].inputs["_materialized_from"], "scoped_edits"
        )

    def test_create_then_edit_is_rejected_like_the_anchored_form(self):
        # Parity with the anchored form: an edit node changes a file
        # that exists when the plan is authored (on disk, or as an
        # earlier EDIT node's result). A file an earlier CONTENT
        # node creates is not an edit target — the model folds the
        # change into the create node's content instead.
        new_path = self.root / "pkg" / "newmod.py"
        create = _raw_node(
            "step_1",
            "scaffold",
            {"file_path": str(new_path), "content": NEWMOD_TEXT},
            [{"type": "file_exists", "path": str(new_path)}],
            outputs=[str(new_path)],
        )
        edit = _raw_node(
            "step_2",
            "scaffold",
            {
                "file_path": str(new_path),
                "edits": [{"search": "return 1", "replace": "return 2"}],
            },
            [{"type": "file_exists", "path": str(new_path)}],
            depends_on=["step_1"],
            outputs=[str(new_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([create, edit])
        self.assertIn("does not exist", str(caught.exception))

    def test_anchored_edit_still_materializes(self):
        anchor_path = self.root / "pkg" / "anchored.py"
        _write(anchor_path, ANCHOR_MODULE)
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(anchor_path),
                "anchor": "beta",
                "position": "replace",
                "content": "def beta():\n    return 22\n",
            },
            [{"type": "file_exists", "path": str(anchor_path)}],
            outputs=[str(anchor_path)],
        )
        nodes = self._parse([node])
        expected = ANCHOR_MODULE.replace(
            "def beta():\n    return 2", "def beta():\n    return 22"
        )
        self.assertEqual(nodes[0].inputs["content"], expected)
        self.assertEqual(
            nodes[0].inputs["_materialized_from"], "anchored_edit"
        )


class TestModifyPathRule(SeamCase):
    def _whole_file_node(self, node_id="node-1", **input_extras):
        inputs = {
            "file_path": str(self.store_path),
            "content": FIXED_STORE_TEXT,
        }
        inputs.update(input_extras)
        return PlanNode(
            id=node_id,
            objective="Rewrite the store module",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs=inputs,
            outputs=[str(self.store_path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(self.store_path)}],
            depends_on=[],
        )

    def _collect(self, nodes, **kwargs):
        records, _cheap = planner_module._collect_plan_error_records(
            nodes,
            self.root,
            GOAL,
            workspace=self.root,
            index=build_repo_index(self.root),
            **kwargs,
        )
        return records

    def test_whole_file_on_existing_is_an_error(self):
        records = self._collect([self._whole_file_node()])
        self.assertEqual([r.code for r in records], ["whole_file_modify"])

    def test_derivative_and_enrichment_contexts_are_exempt(self):
        for context in ("derivative", "enrichment"):
            records = self._collect(
                [self._whole_file_node()], plan_context=context
            )
            self.assertNotIn(
                "whole_file_modify", [r.code for r in records], context
            )

    def test_rule_yields_to_other_defects(self):
        broken = PlanNode(
            id="node-9",
            objective="Broken node",
            assigned_claw="no_such_claw",
            inputs={},
            outputs=[],
            acceptance=["Anything."],
            criteria=[{"type": "file_exists", "path": str(self.store_path)}],
            depends_on=[],
        )
        records = self._collect([self._whole_file_node(), broken])
        codes = [r.code for r in records]
        self.assertIn("unknown_claw", codes)
        self.assertNotIn("whole_file_modify", codes)

    def test_override_with_impact_check_accepted(self):
        write = self._whole_file_node(
            overwrite_justification=(
                "the edit machinery failed twice on this module and "
                "the change spans most definitions"
            )
        )
        impact = PlanNode(
            id="node-2",
            objective="Check the rewrite's blast radius",
            assigned_claw="bash",
            inputs={
                "command": f"./.venv/bin/python {self.test_path}",
                "impact_check_for": "pkg/store.py",
            },
            outputs=[],
            acceptance=["The store tests pass."],
            criteria=[{"type": "exit_code", "expected": 0}],
            depends_on=["node-1"],
        )
        records = self._collect([write, impact])
        self.assertNotIn("whole_file_modify", [r.code for r in records])

    def test_justification_without_impact_check_still_errors(self):
        write = self._whole_file_node(
            overwrite_justification="the change spans most definitions"
        )
        records = self._collect([write])
        self.assertEqual([r.code for r in records], ["whole_file_modify"])

    def test_impact_check_without_justification_still_errors(self):
        write = self._whole_file_node()
        impact = PlanNode(
            id="node-2",
            objective="Check the rewrite's blast radius",
            assigned_claw="bash",
            inputs={
                "command": f"./.venv/bin/python {self.test_path}",
                "impact_check_for": "pkg/store.py",
            },
            outputs=[],
            acceptance=["The store tests pass."],
            criteria=[{"type": "exit_code", "expected": 0}],
            depends_on=["node-1"],
        )
        records = self._collect([write, impact])
        self.assertIn("whole_file_modify", [r.code for r in records])

    def test_edit_derived_node_is_exempt(self):
        node = self._whole_file_node(_materialized_from="scoped_edits")
        records = self._collect([node])
        self.assertNotIn("whole_file_modify", [r.code for r in records])

    def test_new_file_write_is_unchanged(self):
        new_path = self.root / "pkg" / "newmod.py"
        node = PlanNode(
            id="node-1",
            objective="Create the new module",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={"file_path": str(new_path), "content": NEWMOD_TEXT},
            outputs=[str(new_path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(new_path)}],
            depends_on=[],
        )
        records = self._collect([node])
        self.assertNotIn("whole_file_modify", [r.code for r in records])

    def test_rewrite_of_plan_created_file_is_an_error(self):
        new_path = self.root / "pkg" / "newmod.py"
        create = PlanNode(
            id="node-1",
            objective="Create the new module",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={"file_path": str(new_path), "content": NEWMOD_TEXT},
            outputs=[str(new_path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(new_path)}],
            depends_on=[],
        )
        rewrite = PlanNode(
            id="node-2",
            objective="Rewrite the new module",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={
                "file_path": str(new_path),
                "content": NEWMOD_TEXT.replace("return 1", "return 2"),
            },
            outputs=[str(new_path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(new_path)}],
            depends_on=["node-1"],
        )
        records = self._collect([create, rewrite])
        codes = [r.code for r in records]
        self.assertIn("whole_file_modify", codes)
        flagged = [r for r in records if r.code == "whole_file_modify"]
        self.assertEqual(flagged[0].node_id, "node-2")

    def test_parse_level_whole_file_rejected_and_spoof_stripped(self):
        # A model-supplied _materialized_from marker must not exempt
        # an authored whole-file rewrite: parse strips the key, and
        # the modify rule then fires.
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "content": FIXED_STORE_TEXT,
                "_materialized_from": "scoped_edits",
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            outputs=[str(self.store_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                json.dumps({"nodes": [node]}),
                goal_text=GOAL,
                workspace=self.root,
                repo_root=self.root,
            )
        codes = [r.code for r in caught.exception.plan_errors]
        self.assertIn("whole_file_modify", codes)

    def test_parse_level_override_accepted(self):
        write = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.store_path),
                "content": FIXED_STORE_TEXT,
                "overwrite_justification": (
                    "the change spans most definitions of the module"
                ),
            },
            [{"type": "file_exists", "path": str(self.store_path)}],
            outputs=[str(self.store_path)],
        )
        impact = _raw_node(
            "step_2",
            "bash",
            {
                "command": f"./.venv/bin/python {self.test_path}",
                "impact_check_for": "pkg/store.py",
            },
            [{"type": "exit_code", "expected": 0}],
            depends_on=["step_1"],
        )
        nodes, _metadata = parse_planner_response(
            json.dumps({"nodes": [write, impact]}),
            goal_text=GOAL,
            workspace=self.root,
            repo_root=self.root,
        )
        self.assertEqual(len(nodes), 2)


SUITE_COMMAND = "./.venv/bin/python -m unittest discover -s tests"
SCOPED_COMMAND = "./.venv/bin/python -m unittest tests.test_store -v"


class TestTieredCriteria(SeamCase):
    def _write_node_t(self, node_id, depends_on=None):
        path = self.root / "pkg" / f"{node_id}.py"
        content = NEWMOD_TEXT.replace("compute", f"compute_{node_id}")
        return PlanNode(
            id=node_id,
            objective=f"Write {node_id}",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={"file_path": str(path), "content": content},
            outputs=[str(path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(path)}],
            depends_on=depends_on or [],
        )

    def _bash_node_t(self, node_id, command, depends_on=None, **extras):
        inputs = {"command": command}
        inputs.update(extras)
        return PlanNode(
            id=node_id,
            objective=f"Run {node_id}",
            hypothesis="the check passes",
            assigned_claw="bash",
            inputs=inputs,
            outputs=[],
            acceptance=["The check passes."],
            criteria=[{"type": "exit_code", "expected": 0}],
            depends_on=depends_on or [],
        )

    def _placement(self, nodes):
        return planner_module._suite_placement_errors(nodes, self.root)

    def test_mid_plan_suite_command_is_an_error(self):
        nodes = [
            self._write_node_t("w1"),
            self._bash_node_t("check", SUITE_COMMAND, depends_on=["w1"]),
            self._write_node_t("w2", depends_on=["check"]),
        ]
        errors = self._placement(nodes)
        self.assertEqual(len(errors), 1)
        self.assertIn("'check'", errors[0])

    def test_final_gate_suite_is_accepted(self):
        nodes = [
            self._write_node_t("w1"),
            self._write_node_t("w2"),
            self._bash_node_t(
                "gate", SUITE_COMMAND, depends_on=["w1", "w2"]
            ),
        ]
        self.assertEqual(self._placement(nodes), [])

    def test_suite_followed_only_by_readonly_steps_is_a_gate(self):
        # The barrier's purpose is that the suite is the last word
        # over the plan's WRITES: a trailing read-only verification
        # (a live probe) does not disqualify the gate.
        nodes = [
            self._write_node_t("w1"),
            self._bash_node_t("gate", SUITE_COMMAND, depends_on=["w1"]),
            self._bash_node_t(
                "probe", "curl -s http://localhost:8765/health",
                depends_on=["gate"],
            ),
        ]
        self.assertEqual(self._placement(nodes), [])

    def test_suite_justification_exempts(self):
        nodes = [
            self._write_node_t("w1"),
            self._bash_node_t(
                "check",
                SUITE_COMMAND,
                depends_on=["w1"],
                suite_justification="the defect is cross-module timing",
            ),
            self._write_node_t("w2", depends_on=["check"]),
        ]
        self.assertEqual(self._placement(nodes), [])

    def test_scoped_command_mid_plan_is_fine(self):
        nodes = [
            self._write_node_t("w1"),
            self._bash_node_t("check", SCOPED_COMMAND, depends_on=["w1"]),
            self._write_node_t("w2", depends_on=["check"]),
        ]
        self.assertEqual(self._placement(nodes), [])

    def test_tests_pass_directory_criterion_is_outside_this_rule(self):
        # Deliberate exclusion, documented in _suite_placement_errors:
        # a tests_pass criterion over the whole tests directory is
        # the canonical cycle-9 verification shape, measured through
        # measure's own budget — the tiered-criteria rule hard-stops
        # full-suite COMMAND forms only (the measured harm).
        node = self._bash_node_t("check", "true", depends_on=["w1"])
        node.criteria = [{"type": "tests_pass", "suite": "tests"}]
        nodes = [
            self._write_node_t("w1"),
            node,
            self._write_node_t("w2", depends_on=["check"]),
        ]
        self.assertEqual(self._placement(nodes), [])

    def test_tests_pass_file_criterion_is_fine(self):
        node = self._bash_node_t("check", "true", depends_on=["w1"])
        node.criteria = [
            {"type": "tests_pass", "suite": "tests/test_store.py"}
        ]
        nodes = [
            self._write_node_t("w1"),
            node,
            self._write_node_t("w2", depends_on=["check"]),
        ]
        self.assertEqual(self._placement(nodes), [])

    def test_collector_records_suite_placement_on_clean_plan(self):
        nodes = [
            self._write_node_t("w1"),
            self._bash_node_t("check", SUITE_COMMAND, depends_on=["w1"]),
            self._write_node_t("w2", depends_on=["check"]),
        ]
        records, _cheap = planner_module._collect_plan_error_records(
            nodes,
            self.root,
            GOAL,
            workspace=self.root,
            index=build_repo_index(self.root),
        )
        self.assertIn("suite_placement", [r.code for r in records])

    def test_collector_gate_accepts_clean_plan(self):
        nodes = [
            self._write_node_t("w1"),
            self._bash_node_t("gate", SUITE_COMMAND, depends_on=["w1"]),
        ]
        records, _cheap = planner_module._collect_plan_error_records(
            nodes,
            self.root,
            GOAL,
            workspace=self.root,
            index=build_repo_index(self.root),
        )
        self.assertNotIn("suite_placement", [r.code for r in records])


class TestPromptGuidance(unittest.TestCase):
    def test_planner_prompt_teaches_create_vs_modify(self):
        from freetalon.orchestrator.prompts import PLANNER_SYSTEM_PROMPT

        self.assertIn('"edits"', PLANNER_SYSTEM_PROMPT)
        self.assertIn("overwrite_justification", PLANNER_SYSTEM_PROMPT)
        self.assertIn("impact_check_for", PLANNER_SYSTEM_PROMPT)

    def test_planner_prompt_teaches_tiered_verification(self):
        from freetalon.orchestrator.prompts import PLANNER_SYSTEM_PROMPT

        self.assertIn("final gate node", PLANNER_SYSTEM_PROMPT)
        self.assertIn("suite_justification", PLANNER_SYSTEM_PROMPT)

    def test_replan_prompt_carries_both_rules(self):
        from freetalon.orchestrator.prompts import REPLAN_SYSTEM_PROMPT

        self.assertIn("overwrite_justification", REPLAN_SYSTEM_PROMPT)
        self.assertIn("gate node", REPLAN_SYSTEM_PROMPT)
        self.assertIn("suite_justification", REPLAN_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
