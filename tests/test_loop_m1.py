"""Integration tests for the M1 judge loop: re-plan, retry, escalation.

The M1 done test: a seeded failing task re-plans once from its
measurement, does something demonstrably different, and escalates cleanly
when its budget ends.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws


def _make_runner(tmpdir: str):
    store = ExecutionPlanStateStore(db_path=Path(tmpdir) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    run_store = RunStore(Path(tmpdir) / "runs")
    return LoopRunner(store, registry, run_store=run_store), store, registry


def _plan(plan_id: str, nodes: list[PlanNode]) -> ExecutionPlan:
    return ExecutionPlan(plan_id=plan_id, intent=TaskIntent(goal="test"), nodes=nodes)


class TestLoopReplan(unittest.TestCase):
    def test_failing_plan_replans_once_and_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            v1 = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the command exits zero",
            )
            plan = _plan("plan-replan", [v1])
            store.save(plan)

            def replan_fn(brief):
                # A demonstrably different approach: write a file instead.
                self.assertEqual(brief["plan_version"], 1)
                self.assertEqual(len(brief["failed_nodes"]), 1)
                return [PlanNode(
                    id="fix",
                    objective="write a marker file",
                    assigned_claw="bash",
                    inputs={"command": "echo fixed > fixed.txt"},
                    criteria=[{"type": "file_exists", "path": "fixed.txt"}],
                    hypothesis="a marker file proves the step ran",
                    responds_to="node-1",
                    differs_by="writes an artifact instead of relying on exit code",
                )]

            contract = GoalContract(
                objective="re-plan demo", budget=RunBudget(max_replans=1)
            )
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(result.plan_versions, 2)
            self.assertEqual(len(result.attempts), 2)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            # Both plan versions are kept in the run directory.
            run_dir = runner._run_store.run_dir(result.run_id)
            self.assertTrue((run_dir / "plan-v001.json").exists())
            self.assertTrue((run_dir / "plan-v002.json").exists())
            verdicts = [m["verdict"] for m in result.measurements]
            self.assertIn("FAIL", verdicts)
            self.assertIn("PASS", verdicts)

    def test_budget_exhausted_escalates_with_full_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            calls = {"n": 0}

            def always_fails(brief):
                calls["n"] += 1
                return [PlanNode(
                    id="try",
                    objective=f"attempt {calls['n']}",
                    assigned_claw="bash",
                    inputs={"command": "exit 1"},
                    criteria=[{"type": "exit_code", "equals": 0}],
                    hypothesis=f"attempt {calls['n']} exits zero",
                    responds_to="node-1",
                    differs_by=f"attempt number {calls['n']}",
                )]

            v1 = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="attempt 0 exits zero",
            )
            plan = _plan("plan-budget", [v1])
            store.save(plan)
            contract = GoalContract(
                objective="budget demo", budget=RunBudget(max_replans=1)
            )
            result = runner.run_plan(plan, contract, replan_fn=always_fails)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertEqual(result.plan_versions, 2)
            self.assertEqual(len(result.attempts), 2)
            self.assertIsNotNone(result.escalation)
            self.assertIn("re-plan budget spent", result.escalation)
            self.assertEqual(calls["n"], 1)  # exactly one re-plan happened
            summary = (runner._run_store.run_dir(result.run_id) / "summary.md").read_text()
            self.assertIn("Escalation", summary)

    def test_duplicate_replan_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)

            def repeat_fn(brief):
                # Same capability and hypothesis, no differs_by: a repeat.
                return [PlanNode(
                    id="same",
                    objective="exit zero again",
                    assigned_claw="bash",
                    inputs={"command": "exit 1"},
                    criteria=[{"type": "exit_code", "equals": 0}],
                    hypothesis="the command exits zero",
                    responds_to="node-1",
                )]

            v1 = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the command exits zero",
            )
            plan = _plan("plan-dup", [v1])
            store.save(plan)
            contract = GoalContract(
                objective="dup demo", budget=RunBudget(max_replans=3)
            )
            result = runner.run_plan(plan, contract, replan_fn=repeat_fn)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertEqual(result.plan_versions, 1)
            self.assertIsNotNone(result.escalation)
            self.assertIn("repeats failed approach", result.escalation)

    def test_timeout_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, registry = _make_runner(tmp)
            calls = {"n": 0}

            async def flaky(inputs):
                calls["n"] += 1
                if calls["n"] == 1:
                    await asyncio.sleep(0.7)  # exceeds the node timeout
                return {"ok": True}

            registry.register("flaky", flaky)
            node = PlanNode(
                id="node-1",
                objective="flaky step",
                assigned_claw="flaky",
                timeout_s=0.5,
                max_retries=1,
                hypothesis="flaky completes in time",
            )
            plan = _plan("plan-retry", [node])
            store.save(plan)
            result = runner.run_plan(plan, GoalContract(objective="retry demo"))
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["retry", "continue"],
            )

    def test_repeated_unmeasurable_escalates(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)

            def replan_fn(brief):
                return [PlanNode(
                    id="check",
                    objective="check for the missing file",
                    assigned_claw="bash",
                    inputs={"command": "true"},
                    criteria=[{"type": "file_exists", "path": "never-created.txt"}],
                    hypothesis="the file appears",
                    responds_to="node-1",
                    differs_by="checks again after a no-op",
                )]

            v1 = PlanNode(
                id="node-1",
                objective="check for the missing file",
                assigned_claw="bash",
                inputs={"command": "true"},
                criteria=[{"type": "file_exists", "path": "never-created.txt"}],
                hypothesis="the file exists",
            )
            plan = _plan("plan-unmeas", [v1])
            store.save(plan)
            contract = GoalContract(
                objective="unmeasurable demo", budget=RunBudget(max_replans=3)
            )
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertIsNotNone(result.escalation)
            self.assertIn("unmeasurable twice", result.escalation)


if __name__ == "__main__":
    unittest.main()
