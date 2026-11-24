"""Campaign build #3 (2026-11-23), parts 1 + 2.

Part 1 — seam-bypass observability: when a caller injects a
replan_fn, the loop's repair seams are skipped. Through exam arm
#11 every arm ran that way and the skip was recorded nowhere, so
the seams' firing rate was unmeasurable. The loop now audit-logs
loop.replan.seams_bypassed and stamps the record into the new plan
version's metadata, next to build #2's repair_declines.

Part 2 — edit grounding: an anchored/scoped edit (plan form or
repair seam) whose new code introduces references the target file
cannot resolve — arm #11's self.store for self.server.store —
is rejected (plan form: PlanError code "ungrounded_reference")
or declined (seams: reason "ungrounded_reference"). The check is
conservative: only introduced references, only clear violations.

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
from freetalon.orchestrator.grounding import edit_grounding_violations
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
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

GOAL = "Build a small fixture package with an HTTP handler module."

HANDLER_TEXT = '''"""Fixture handler."""

from http.server import BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        view = self.server.store.poll("KESTREL-0001")
        self.send_response(200)
        self.end_headers()


def helper_a():
    return "a"


def helper_b():
    return "b"


def helper_c():
    return "c"
'''

HANDLER_TEST_TEXT = '''import unittest

from pkg import handler


class HandlerTests(unittest.TestCase):
    def test_handler_module_loads(self):
        self.assertTrue(hasattr(handler, "Handler"))
'''

FAILING_ID = "tests.test_handler.HandlerTests.test_handler_module_loads"


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


# ── Part 2: the grounding check itself ──────────────────────────────


class TestGroundingUnit(unittest.TestCase):
    def test_self_store_is_the_canonical_catch(self):
        broken = HANDLER_TEXT.replace(
            "self.server.store.poll", "self.store.poll"
        )
        violations = edit_grounding_violations(HANDLER_TEXT, broken)
        self.assertEqual(len(violations), 1)
        self.assertIn("self.store", violations[0])

    def test_grounded_edit_is_clean(self):
        edited = HANDLER_TEXT.replace(
            'self.send_response(200)', 'self.send_response(200)\n        self.log_request(200)'
        )
        self.assertEqual(edit_grounding_violations(HANDLER_TEXT, edited), [])

    def test_preexisting_reference_is_not_introduced(self):
        # The old text already used self.store (broken before the
        # edit): the edit that does not touch it is not blamed.
        self.assertEqual(
            edit_grounding_violations(
                HANDLER_TEXT.replace("self.server.store", "self.store"),
                HANDLER_TEXT.replace("self.server.store", "self.store").replace(
                    "200", "201"
                ),
            ),
            [],
        )

    def test_attribute_assigned_in_another_method_is_grounded(self):
        old = (
            "class Worker:\n"
            "    def __init__(self):\n"
            "        self.cache = {}\n"
            "    def get(self):\n"
            "        return 1\n"
        )
        new = old.replace("return 1", "return self.cache")
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_inherited_attribute_is_grounded(self):
        old = (
            "class Base:\n"
            "    def __init__(self):\n"
            "        self.state = 0\n"
            "class Child(Base):\n"
            "    def get(self):\n"
            "        return 1\n"
        )
        new = old.replace("return 1", "return self.state")
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_getattr_class_is_exempt(self):
        old = (
            "class Dyn:\n"
            "    def __getattr__(self, name):\n"
            "        return None\n"
            "    def get(self):\n"
            "        return 1\n"
        )
        new = old.replace("return 1", "return self.anything")
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_unknown_base_class_is_exempt(self):
        old = (
            "class Sub(ExternalBase):\n"
            "    def get(self):\n"
            "        return 1\n"
        )
        new = old.replace("return 1", "return self.whatever")
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_undefined_bare_call_is_caught(self):
        old = "def f():\n    return 1\n"
        new = "def f():\n    return serialize_payload(1)\n"
        violations = edit_grounding_violations(old, new)
        self.assertEqual(len(violations), 1)
        self.assertIn("serialize_payload", violations[0])

    def test_defined_and_local_calls_are_grounded(self):
        old = (
            "def helper(x):\n    return x\n\n"
            "def f():\n    cb = helper\n    return 1\n"
        )
        new = old.replace("return 1", "return helper(cb(2))")
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_star_import_exempts_bare_calls(self):
        old = "from helpers import *\n\ndef f():\n    return 1\n"
        new = old.replace("return 1", "return anything(1)")
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_private_helper_call_is_a_forward_reference(self):
        # The cycle-7 precedent: a delta whose real expressions call
        # underscore-prefixed helpers defined elsewhere in the work
        # is accepted at parse and at the seams (the repo pins this
        # in TestPlannerPlaceholderValueFix / TestPlannerLocalRepair).
        old = "def do_GET(self):\n    return _send(200, {})\n"
        new = (
            "def do_GET(self):\n"
            "    payload = {}\n"
            "    payload['gpu_temperature'] = _gpu_temperature()\n"
            "    return _send(200, payload)\n"
        )
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_unparseable_text_yields_no_violations(self):
        self.assertEqual(edit_grounding_violations(HANDLER_TEXT, "def broken(:"), [])
        self.assertEqual(edit_grounding_violations("def broken(:", HANDLER_TEXT), [])


# ── Part 2: the seams decline ───────────────────────────────────────


class HandlerCase(unittest.TestCase):
    """A handler module + a test that imports it, in build #2's
    seam-fixture shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.handler_path = self.root / "pkg" / "handler.py"
        self.test_path = self.root / "tests" / "test_handler.py"
        _write(self.root / "pkg" / "__init__.py", "")
        _write(self.handler_path, HANDLER_TEXT)
        _write(self.test_path, HANDLER_TEST_TEXT)

    def _write_node(self, node_id: str) -> PlanNode:
        return PlanNode(
            id=node_id,
            objective="Write handler.py",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={"file_path": str(self.handler_path), "content": HANDLER_TEXT},
            outputs=[str(self.handler_path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(self.handler_path)}],
            depends_on=[],
            status=PlanStatus.COMPLETED,
        )

    def _run_node(self, node_id: str, diagnostics: str) -> PlanNode:
        return PlanNode(
            id=node_id,
            objective="Run the tests",
            hypothesis="the tests pass",
            assigned_claw="bash",
            inputs={"command": f"./.venv/bin/python {self.test_path}"},
            outputs=[],
            acceptance=["The tests pass."],
            criteria=[{"type": "exit_code", "expected": 0}],
            depends_on=["node-1"],
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

    def _plan(self, nodes: list[PlanNode]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-build3",
            intent=TaskIntent(goal=GOAL, constraints={"workspace": str(self.root)}),
            nodes=nodes,
            metadata={"repo_root": str(self.root)},
        )

    def _brief(self, diagnostics: str) -> dict:
        return {
            "goal": {"objective": GOAL},
            "plan_id": "plan-build3",
            "plan_version": 1,
            "failing_tests": [FAILING_ID],
            "produced_files": ["pkg/handler.py", "tests/test_handler.py"],
            "failed_nodes": [{"id": "node-2", "diagnostics": diagnostics}],
            "measurements": [],
            "past_experiments": [],
            "remaining_budget": {"replans": 2},
        }


class TestScopedSeamGrounding(HandlerCase):
    def test_ungrounded_edit_declines_with_reason(self):
        nodes = [self._write_node("node-1"), self._run_node("node-2", "AssertionError")]
        plan = self._plan(nodes)
        declines: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return _block(
                '        view = self.server.store.poll("KESTREL-0001")',
                '        view = self.store.poll("KESTREL-0001")',
            )

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan, self._brief("AssertionError"), SETTINGS, declines=declines
            )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["ungrounded_reference"])
        self.assertIn("self.store", declines[0]["detail"])

    def test_grounded_edit_still_repairs(self):
        nodes = [self._write_node("node-1"), self._run_node("node-2", "AssertionError")]
        plan = self._plan(nodes)
        declines: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return _block(
                '        view = self.server.store.poll("KESTREL-0001")',
                '        view = self.server.store.poll("KESTREL-0001")\n'
                "        self.log_request(200)",
            )

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan, self._brief("AssertionError"), SETTINGS, declines=declines
            )
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])


class TestLocalSeamGrounding(HandlerCase):
    def test_ungrounded_repair_declines_with_reason(self):
        diagnostics = (
            "Traceback (most recent call last):\n"
            f'  File "{self.handler_path}", line 8, in do_GET\n'
            "AssertionError: poll returned the wrong view"
        )
        nodes = [
            self._write_node("node-1"),
            self._run_node("node-2", diagnostics),
        ]
        plan = self._plan(nodes)
        declines: list[dict] = []
        response = (
            "```python\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            '        view = self.store.poll("KESTREL-0001")\n'
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "```\n"
        )

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return response

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_local_repair(
                plan, self._brief(diagnostics), SETTINGS, declines=declines
            )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["ungrounded_reference"])
        self.assertIn("self.store", declines[0]["detail"])


# ── Part 2: the plan form rejects ───────────────────────────────────


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


class TestPlanFormGrounding(HandlerCase):
    def _parse(self, nodes):
        return parse_planner_response(
            json.dumps({"nodes": nodes}),
            goal_text=GOAL,
            workspace=str(self.root),
            repo_root=self.root,
        )

    def test_scoped_edit_with_ungrounded_reference_is_a_plan_error(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.handler_path),
                "edits": [
                    {
                        "search": '        view = self.server.store.poll("KESTREL-0001")',
                        "replace": '        view = self.store.poll("KESTREL-0001")',
                    }
                ],
            },
            [{"type": "file_exists", "path": str(self.handler_path)}],
            outputs=[str(self.handler_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([node])
        codes = [r.code for r in caught.exception.plan_errors]
        self.assertEqual(codes, ["ungrounded_reference"])
        self.assertIn("self.store", str(caught.exception))

    def test_anchored_edit_with_ungrounded_reference_is_a_plan_error(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.handler_path),
                "anchor": "Handler.do_GET",
                "position": "replace",
                "content": (
                    "    def do_GET(self):\n"
                    '        view = self.store.poll("KESTREL-0001")\n'
                    "        self.send_response(200)\n"
                    "        self.end_headers()\n"
                ),
            },
            [{"type": "file_exists", "path": str(self.handler_path)}],
            outputs=[str(self.handler_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([node])
        codes = [r.code for r in caught.exception.plan_errors]
        self.assertEqual(codes, ["ungrounded_reference"])

    def test_grounded_scoped_edit_materializes(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.handler_path),
                "edits": [
                    {
                        "search": "        self.send_response(200)",
                        "replace": "        self.send_response(200)\n        self.log_request(200)",
                    }
                ],
            },
            [{"type": "file_exists", "path": str(self.handler_path)}],
            outputs=[str(self.handler_path)],
        )
        nodes, _metadata = self._parse([node])
        self.assertIn("self.log_request(200)", nodes[0].inputs["content"])
        self.assertEqual(nodes[0].inputs["_materialized_from"], "scoped_edits")


# ── Part 1: the loop records the bypass ─────────────────────────────


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _loop_runner(tmp: str, audit: _AuditCapture) -> LoopRunner:
    store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    return LoopRunner(
        store,
        registry,
        run_store=RunStore(Path(tmp) / "runs"),
        memory_store=ExperimentStore(Path(tmp) / "memory.db"),
        audit_logger=audit,
    )


def _failing_then_passing_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-bypass",
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


def _passing_nodes() -> list[PlanNode]:
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


class TestSeamBypassAccounting(unittest.TestCase):
    def test_injected_replan_fn_records_the_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            store = runner._store
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=2)
            )
            plan = _failing_then_passing_plan()
            store.save(plan)
            seam_calls: list[str] = []

            def fake_local(plan_, brief, settings=None, declines=None):
                seam_calls.append("local")
                return None

            def fake_scoped(plan_, brief, settings=None, declines=None):
                seam_calls.append("scoped")
                return None

            with (
                mock.patch.object(loop_module, "try_local_repair", fake_local),
                mock.patch.object(
                    loop_module, "try_scoped_edit_repair", fake_scoped
                ),
            ):
                result = runner.run_plan(
                    plan, contract, replan_fn=lambda brief: _passing_nodes()
                )
            self.assertEqual(result.goal_verdict, "PASS")
            # The seams were never consulted under the injection...
            self.assertEqual(seam_calls, [])
            # ...and the bypass is a recorded fact on both channels.
            events = [event for event, _fields in audit.events]
            self.assertIn("loop.replan.seams_bypassed", events)
            saved = store.load("plan-bypass")
            self.assertIsNotNone(saved)
            self.assertEqual(
                saved.metadata.get("seams_bypassed"),
                {"reason": "replan_fn_injected", "version": 1},
            )
            self.assertNotIn("repair_declines", saved.metadata)

    def test_default_path_records_no_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            store = runner._store
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=2)
            )
            plan = _failing_then_passing_plan()
            store.save(plan)

            def fake_local(plan_, brief, settings=None, declines=None):
                return None

            def fake_scoped(plan_, brief, settings=None, declines=None):
                return None

            with (
                mock.patch.object(loop_module, "try_local_repair", fake_local),
                mock.patch.object(
                    loop_module, "try_scoped_edit_repair", fake_scoped
                ),
                mock.patch.object(
                    loop_module,
                    "replan_from_brief",
                    lambda brief, **kwargs: _passing_nodes(),
                ),
            ):
                result = runner.run_plan(plan, contract)
            self.assertEqual(result.goal_verdict, "PASS")
            events = [event for event, _fields in audit.events]
            self.assertNotIn("loop.replan.seams_bypassed", events)
            saved = store.load("plan-bypass")
            self.assertIsNotNone(saved)
            self.assertNotIn("seams_bypassed", saved.metadata)


if __name__ == "__main__":
    unittest.main()
