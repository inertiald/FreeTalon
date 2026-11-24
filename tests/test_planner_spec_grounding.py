"""Spec grounding (2026-11-23): a spec file the goal names is
loaded verbatim into the planner's prompt and into the
code-writing calls (re-plan, node-local repair), and the inclusion
is recorded in the plan metadata / re-plan brief.

The defect measured by exam re-arms #3/#4: the goal named
freetalon/pairing/CONTRACT.md as the specification, but the repo
context listed only Python modules and routes, so the spec's
contents reached no call — the loop implemented the goal's
restated summary instead (re-arm #3), then wrote
NotImplementedError stubs deferring to the unseen file and
invented a protocol on re-plan (re-arm #4). The cross-model
consultation's consensus fix is verbatim injection at both
boundaries, as a harness responsibility.

call_llm is mocked throughout: no GPU is touched. The loader and
context tests run against synthetic trees in temp dirs.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMSettings
from freetalon.orchestrator.models import TaskIntent
from freetalon.orchestrator.repo_index import (
    SpecContextError,
    build_repo_index,
    format_repo_context,
    format_spec_context,
    goal_named_specs,
)

CONTRACT_TEXT = """# Pairing contract (fixture)

PairingStore(path, ttl_seconds=1800, now_fn=time.time) is the store.
Statuses are exactly: pending, confirmed, claimed, expired.
Poll of an unknown code raises CodeNotFound — never a dict.
"""

DESIGN_TEXT = "# Design (fixture)\n\nBe excellent.\n"

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

BASH_PLAN = json.dumps(
    {
        "nodes": [
            {
                "id": "verify",
                "objective": "Verify inputs",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "ls inputs"},
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "execute",
                "objective": "Execute rollout",
                "depends_on": ["verify"],
                "assigned_claw": "bash",
                "inputs": {"command": "deploy.sh"},
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ],
        "metadata": {"planner": "llm"},
    }
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SpecTreeCase(unittest.TestCase):
    """A synthetic repo: a package, a contract, two same-named
    design docs (the ambiguous-basename fixture), and a huge spec."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / "freetalon" / "__init__.py", "")
        _write(
            self.root / "freetalon" / "pairing" / "CONTRACT.md",
            CONTRACT_TEXT,
        )
        _write(self.root / "docs" / "DESIGN.md", DESIGN_TEXT)
        _write(self.root / "docs" / "adr" / "DESIGN.md", DESIGN_TEXT)
        _write(self.root / "HUGE.md", "x" * 40000)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestGoalNamedSpecs(SpecTreeCase):
    def test_match_by_repo_relative_path(self) -> None:
        specs = goal_named_specs(
            "Implement the service in freetalon/pairing/CONTRACT.md exactly.",
            self.root,
        )
        self.assertEqual([s.path for s in specs], ["freetalon/pairing/CONTRACT.md"])
        self.assertEqual(specs[0].text, CONTRACT_TEXT)

    def test_match_by_unique_basename(self) -> None:
        specs = goal_named_specs(
            "Follow CONTRACT.md to the letter.", self.root
        )
        self.assertEqual([s.path for s in specs], ["freetalon/pairing/CONTRACT.md"])

    def test_match_by_absolute_path_under_root(self) -> None:
        goal = f"Implement per {self.root}/freetalon/pairing/CONTRACT.md now."
        specs = goal_named_specs(goal, self.root)
        self.assertEqual([s.path for s in specs], ["freetalon/pairing/CONTRACT.md"])

    def test_ambiguous_basename_matches_nothing(self) -> None:
        # docs/DESIGN.md and docs/adr/DESIGN.md share a basename;
        # guessing would ground the plan in the wrong text.
        self.assertEqual(goal_named_specs("See DESIGN.md.", self.root), [])

    def test_full_path_disambiguates_same_basename(self) -> None:
        specs = goal_named_specs(
            "See docs/adr/DESIGN.md for the rationale.", self.root
        )
        self.assertEqual([s.path for s in specs], ["docs/adr/DESIGN.md"])
        self.assertEqual(specs[0].text, DESIGN_TEXT)

    def test_goal_naming_no_existing_spec_returns_empty(self) -> None:
        self.assertEqual(
            goal_named_specs("Add a field to the health payload.", self.root),
            [],
        )
        self.assertEqual(
            goal_named_specs("Read MISSING.md first.", self.root), []
        )

    def test_code_file_named_in_goal_is_not_a_spec(self) -> None:
        self.assertEqual(
            goal_named_specs(
                "Write freetalon/pairing/store.py per the contract.",
                self.root,
            ),
            [],
        )

    def test_record_is_path_sha256_and_bytes(self) -> None:
        (spec,) = goal_named_specs("Per CONTRACT.md.", self.root)
        raw = CONTRACT_TEXT.encode("utf-8")
        self.assertEqual(
            spec.record(),
            {
                "path": "freetalon/pairing/CONTRACT.md",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            },
        )

    def test_oversized_spec_fails_loudly(self) -> None:
        with self.assertRaises(SpecContextError) as caught:
            goal_named_specs("Implement HUGE.md.", self.root)
        self.assertIn("HUGE.md", str(caught.exception))
        self.assertIn("never truncated", str(caught.exception))

    def test_format_spec_context_is_verbatim(self) -> None:
        specs = goal_named_specs("Per CONTRACT.md.", self.root)
        block = format_spec_context(specs)
        self.assertIn(CONTRACT_TEXT, block)
        self.assertIn("authoritative", block)
        self.assertIn("never replaces it", block)
        self.assertEqual(format_spec_context([]), "")

    def test_index_inventory_is_used_when_given(self) -> None:
        index = build_repo_index(self.root)
        self.assertEqual(
            index.docs["freetalon/pairing/CONTRACT.md"],
            len(CONTRACT_TEXT.encode("utf-8")),
        )
        specs = goal_named_specs("Per CONTRACT.md.", self.root, index)
        self.assertEqual(len(specs), 1)


STUB_STORE = '''"""Pairing store (stub scaffold)."""

from freetalon.pairing.errors import CodeNotFound


class PairingStore:
    """File-backed pairing store."""

    def __init__(self, path):
        raise NotImplementedError("Implement per CONTRACT.md")

    def register(self, code):
        raise NotImplementedError("Implement per CONTRACT.md")

    def poll(self, code):
        raise NotImplementedError("Implement per CONTRACT.md")


def make_server(store):
    raise NotImplementedError("Implement per CONTRACT.md")
'''

REAL_STORE = '''"""Pairing store (one real method)."""

import json


class PairingStore:
    """File-backed pairing store."""

    def __init__(self, path):
        self.path = path

    def register(self, code):
        raise NotImplementedError("Implement per CONTRACT.md")

    def poll(self, code):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)
'''

PROTOCOL_MODULE = '''"""Interface declarations only."""

from typing import Protocol


class Store(Protocol):
    """The store interface."""

    def register(self, code):
        raise NotImplementedError

    def poll(self, code):
        raise NotImplementedError
'''


class TestStubOnlyModuleRule(SpecTreeCase):
    """Item 6: a NEW module whose every definition only raises
    NotImplementedError is rejected at parse (code stub_only_module)
    instead of passing to execution for the critic to catch."""

    def _scaffold_plan(self, target: str, content: str) -> str:
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "step_1",
                        "objective": "Write the module",
                        "depends_on": [],
                        "assigned_claw": "scaffold",
                        "inputs": {"file_path": target, "content": content},
                        "outputs": [target],
                        "criteria": [{"type": "file_exists", "path": target}],
                    }
                ]
            }
        )

    def test_rearm4_shape_is_rejected(self) -> None:
        from freetalon.orchestrator.intake import LLMResponseError

        target = str(self.root / "freetalon" / "pairing" / "store.py")
        with self.assertRaises(LLMResponseError) as caught:
            planner_module.parse_planner_response(
                self._scaffold_plan(target, STUB_STORE),
                repo_root=self.root,
                goal_text="Implement the pairing store.",
            )
        self.assertIn("NotImplementedError", str(caught.exception))
        self.assertIn("store.py", str(caught.exception))

    def test_one_real_definition_disqualifies(self) -> None:
        target = str(self.root / "freetalon" / "pairing" / "store.py")
        nodes, _metadata = planner_module.parse_planner_response(
            self._scaffold_plan(target, REAL_STORE),
            repo_root=self.root,
            goal_text="Implement the pairing store.",
        )
        self.assertEqual(len(nodes), 1)

    def test_protocol_declarations_are_not_stubs(self) -> None:
        target = str(self.root / "freetalon" / "pairing" / "iface.py")
        nodes, _metadata = planner_module.parse_planner_response(
            self._scaffold_plan(target, PROTOCOL_MODULE),
            repo_root=self.root,
            goal_text="Declare the store interface.",
        )
        self.assertEqual(len(nodes), 1)

    def test_existing_file_rewrite_is_not_new(self) -> None:
        target = self.root / "freetalon" / "pairing" / "store.py"
        _write(target, '"""Old store."""\n')
        # Campaign build #2: a whole-file rewrite of an existing
        # file needs the modify-path override (justification + an
        # impact-check node) before any other rule sees the plan;
        # with it, the stub-only rule still exempts the rewrite
        # because the target is not new — the point of this test.
        plan = json.loads(self._scaffold_plan(str(target), STUB_STORE))
        plan["nodes"][0]["inputs"]["overwrite_justification"] = (
            "the module is being replaced wholesale per the contract"
        )
        plan["nodes"].append(
            {
                "id": "step_2",
                "objective": "Check the rewrite's impact",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "./.venv/bin/python -m unittest "
                        "tests.test_pairing"
                    ),
                    "impact_check_for": "freetalon/pairing/store.py",
                },
                "outputs": [],
                "criteria": [{"type": "exit_code", "expected": 0}],
            }
        )
        nodes, _metadata = planner_module.parse_planner_response(
            json.dumps(plan),
            repo_root=self.root,
            goal_text="Implement the pairing store.",
        )
        self.assertEqual(len(nodes), 2)


class TestRepoContextDocsListing(SpecTreeCase):
    def test_documents_are_listed_with_sizes(self) -> None:
        context = format_repo_context(build_repo_index(self.root))
        self.assertIn("Repo documents:", context)
        self.assertIn(
            f"freetalon/pairing/CONTRACT.md "
            f"({len(CONTRACT_TEXT.encode('utf-8'))} bytes)",
            context,
        )


class TestPlanTaskIntentGrounding(SpecTreeCase):
    GOAL = (
        "Implement the node pairing service. The complete and only "
        "specification is freetalon/pairing/CONTRACT.md — follow it "
        "exactly."
    )

    def _plan(self, goal: str, response: str = BASH_PLAN):
        intent = TaskIntent(
            goal=goal, constraints={"workspace": str(self.root)}
        )
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return response

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            plan = planner_module.plan_task_intent(
                intent, settings=SETTINGS, repo_root=self.root
            )
        return plan, calls

    def test_spec_text_reaches_the_planning_prompt_verbatim(self) -> None:
        _plan, calls = self._plan(self.GOAL)
        self.assertEqual(len(calls), 1)
        self.assertIn(CONTRACT_TEXT, calls[0][1])
        self.assertIn("GOAL-NAMED SPEC", calls[0][1])

    def test_inclusion_is_recorded_in_plan_metadata(self) -> None:
        plan, _calls = self._plan(self.GOAL)
        raw = CONTRACT_TEXT.encode("utf-8")
        self.assertEqual(
            plan.metadata["spec_context"],
            [
                {
                    "path": "freetalon/pairing/CONTRACT.md",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                }
            ],
        )

    def test_goal_without_a_spec_behaves_as_before(self) -> None:
        plan, calls = self._plan("Add a field to the health payload.")
        self.assertNotIn("GOAL-NAMED SPEC", calls[0][1])
        self.assertNotIn("spec_context", plan.metadata)

    def test_oversized_named_spec_fails_before_any_call(self) -> None:
        intent = TaskIntent(
            goal="Implement HUGE.md.", constraints={"workspace": str(self.root)}
        )
        with mock.patch.object(
            planner_module, "call_llm", side_effect=AssertionError("called")
        ):
            with self.assertRaises(SpecContextError):
                planner_module.plan_task_intent(
                    intent, settings=SETTINGS, repo_root=self.root
                )


class TestReplanGrounding(SpecTreeCase):
    def test_spec_reaches_the_replan_prompt_and_the_brief(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Write the store module",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": str(self.root / "freetalon" / "pairing" / "store.py"),
                "content": "print(1)\n",
            },
            "outputs": [str(self.root / "freetalon" / "pairing" / "store.py")],
            "criteria": [
                {
                    "type": "file_exists",
                    "path": str(self.root / "freetalon" / "pairing" / "store.py"),
                }
            ],
            "responds_to": "node-1",
            "differs_by": "write the module the contract pins",
        }
        brief = {
            "goal": {
                "objective": (
                    "Implement the pairing service per "
                    "freetalon/pairing/CONTRACT.md."
                )
            },
            "failed_nodes": [],
            "workspace": str(self.root / "ws"),
        }
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return json.dumps({"nodes": [node]})

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            nodes = planner_module.replan_from_brief(
                brief, settings=SETTINGS, repo_root=self.root
            )
        self.assertEqual(len(nodes), 1)
        self.assertIn(CONTRACT_TEXT, calls[0][1])
        self.assertEqual(
            brief["spec_context"][0]["path"], "freetalon/pairing/CONTRACT.md"
        )


class TestLocalRepairGrounding(unittest.TestCase):
    """The cycle-8 fixture shape, shrunk: a write node produced a
    module whose /health branch omits the field the spec pins; the
    repair call must carry the spec's verbatim text."""

    MODULE_SOURCE = (
        "class Handler:\n"
        "    def do_GET(self):\n"
        '        path = "/health"\n'
        '        if path == "/health":\n'
        '            return {"ok": True, "status": "ready"}\n'
        '        return {"ok": False}\n'
    )
    TEST_SOURCE = (
        "import fixture_module\n"
        "\n"
        "handler = fixture_module.Handler()\n"
        'handler.path = "/health"\n'
        "payload = handler.do_GET()\n"
        'assert "service_status" in payload, "\'service_status\' not found in payload"\n'
    )
    SPEC_TEXT = (
        "# Fixture spec\n\n"
        "The /health payload MUST carry service_status alongside ok "
        "and status.\n"
    )
    GOAL = (
        "Make the /health payload carry service_status, exactly as "
        "the spec in SPEC.md requires."
    )
    REPAIRED_DEF = (
        "def do_GET(self):\n"
        '    path = "/health"\n'
        '    if path == "/health":\n'
        '        return {"ok": True, "status": "ready", "service_status": "ok"}\n'
        '    return {"ok": False}\n'
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.module_path = str(self.root / "fixture_module.py")
        self.test_path = str(self.root / "test_health.py")
        _write(self.root / "fixture_module.py", self.MODULE_SOURCE)
        _write(self.root / "test_health.py", self.TEST_SOURCE)
        _write(self.root / "SPEC.md", self.SPEC_TEXT)
        self.stderr = (
            "Traceback (most recent call last):\n"
            f'  File "{self.test_path}", line 6, in <module>\n'
            '    assert "service_status" in payload, '
            "\"'service_status' not found in payload\"\n"
            "AssertionError: 'service_status' not found in payload\n"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _nodes(self) -> list:
        from freetalon.orchestrator.models import PlanNode, PlanStatus

        return [
            PlanNode(
                id="node-1",
                objective="Write the fixture module",
                hypothesis="the module serves /health",
                assigned_claw="scaffold",
                inputs={
                    "file_path": self.module_path,
                    "content": self.MODULE_SOURCE,
                },
                outputs=[self.module_path],
                acceptance=["The module exists."],
                criteria=[{"type": "file_exists", "path": self.module_path}],
                status=PlanStatus.COMPLETED,
            ),
            PlanNode(
                id="node-2",
                objective="Write the /health test script",
                hypothesis="a script can assert on the payload",
                assigned_claw="bash",
                inputs={
                    "command": (
                        f"cat > {self.test_path} <<'EOF'\n"
                        f"{self.TEST_SOURCE}EOF\n"
                    )
                },
                outputs=[self.test_path],
                acceptance=["The test script exists."],
                criteria=[{"type": "file_exists", "path": self.test_path}],
                depends_on=["node-1"],
                status=PlanStatus.COMPLETED,
            ),
            PlanNode(
                id="node-3",
                objective="Run the /health test script",
                hypothesis="the payload reports service_status",
                assigned_claw="bash",
                inputs={
                    "command": f"python3 {self.test_path}",
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": self.stderr,
                    "duration_s": 0.1,
                },
                outputs=[],
                acceptance=["The test passes."],
                criteria=[{"type": "exit_code", "expected": 0}],
                depends_on=["node-2"],
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
            ),
        ]

    def _brief(self, nodes: list) -> dict:
        from freetalon.orchestrator.judge import _failure_diagnostics

        failed = next(node for node in nodes if node.id == "node-3")
        return {
            "goal": {"objective": self.GOAL},
            "plan_id": "plan-spec-repair",
            "plan_version": 1,
            "failed_nodes": [
                {
                    "id": failed.id,
                    "objective": failed.objective,
                    "hypothesis": failed.hypothesis,
                    "assigned_claw": failed.assigned_claw,
                    "inputs": dict(failed.inputs),
                    "measurement": failed.measurement,
                    "error": failed.error,
                    "diagnostics": _failure_diagnostics(failed),
                }
            ],
            "measurements": [],
            "past_experiments": [],
            "remaining_budget": {"replans": 2, "wall_seconds": None, "attempts": None},
        }

    def test_spec_text_reaches_the_repair_prompt_verbatim(self) -> None:
        from freetalon.orchestrator.models import ExecutionPlan

        nodes = self._nodes()
        plan = ExecutionPlan(
            plan_id="plan-spec-repair",
            intent=TaskIntent(
                goal=self.GOAL, constraints={"workspace": str(self.root)}
            ),
            nodes=nodes,
            metadata={"repo_root": str(self.root)},
        )
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return self.REPAIRED_DEF

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_local_repair(plan, self._brief(nodes))
        self.assertEqual(len(calls), 1)
        self.assertIn(self.SPEC_TEXT, calls[0][1])
        self.assertIn("GOAL-NAMED SPEC", calls[0][1])
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
