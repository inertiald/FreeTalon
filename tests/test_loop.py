"""Tests for the loop runner and executor measurement (M0).

These are the M0 done tests: a script that exits non-zero reports FAILED
with its output attached; a script that passes its acceptance reports
COMPLETED with measurements; both verdicts land in the run record.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    SuccessMeasure,
    TaskIntent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws


def _no_replanner(brief):
    """Re-plan function for M0 tests: re-planning is out of scope here."""
    raise RuntimeError("re-planner disabled in this test")


def _make_runner(tmpdir: str):
    store = ExecutionPlanStateStore(db_path=Path(tmpdir) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    run_store = RunStore(Path(tmpdir) / "runs")
    return LoopRunner(store, registry, run_store=run_store), store, registry


def _contract(**overrides):
    kwargs = {"objective": "test goal"}
    kwargs.update(overrides)
    return GoalContract(**kwargs)


def _plan(plan_id: str, nodes: list[PlanNode]) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id, intent=TaskIntent(goal="test"), nodes=nodes
    )


class TestLoopRunnerM0(unittest.TestCase):
    def test_failing_script_reports_failed_with_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            node = PlanNode(
                id="node-1",
                objective="run a failing script",
                assigned_claw="bash",
                inputs={"command": "echo boom >&2; exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
            )
            plan = _plan("plan-fail", [node])
            store.save(plan)
            result = runner.run_plan(plan, _contract(), replan_fn=_no_replanner)
            finished = result.plan.nodes[0]
            self.assertEqual(finished.status, PlanStatus.FAILED)
            self.assertEqual(finished.measurement["verdict"], "FAIL")
            self.assertEqual(finished.exit_code, 1)
            self.assertIn("exit_code 1", finished.error or "")
            self.assertEqual(result.goal_verdict, "FAIL")
            # The verdict is in the run record on disk.
            measurements = runner._run_store.read_measurements(result.run_id)
            self.assertEqual(len(measurements), 1)
            self.assertEqual(measurements[0]["verdict"], "FAIL")
            # stdout/stderr are recorded in the run directory.
            node_dir = runner._run_store.node_dir(result.run_id, "node-1")
            self.assertIn("boom", (node_dir / "stderr.txt").read_text())
            self.assertTrue((runner._run_store.run_dir(result.run_id) / "summary.md").exists())

    def test_passing_script_with_file_criterion_reports_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            node = PlanNode(
                id="node-1",
                objective="write an artifact",
                assigned_claw="bash",
                inputs={"command": "echo hello > out.txt"},
                criteria=[{"type": "file_exists", "path": "out.txt"}],
            )
            plan = _plan("plan-pass", [node])
            store.save(plan)
            result = runner.run_plan(plan, _contract())
            finished = result.plan.nodes[0]
            self.assertEqual(finished.status, PlanStatus.COMPLETED)
            self.assertEqual(finished.measurement["verdict"], "PASS")
            self.assertEqual(result.goal_verdict, "PASS")
            node_dir = runner._run_store.node_dir(result.run_id, "node-1")
            self.assertEqual((node_dir / "out.txt").read_text().strip(), "hello")

    def test_downstream_node_does_not_run_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            nodes = [
                PlanNode(
                    id="node-1",
                    objective="fail",
                    assigned_claw="bash",
                    inputs={"command": "exit 1"},
                ),
                PlanNode(
                    id="node-2",
                    objective="downstream",
                    depends_on=["node-1"],
                    assigned_claw="bash",
                    inputs={"command": "echo should-not-run > x.txt"},
                ),
            ]
            plan = _plan("plan-dep", nodes)
            store.save(plan)
            result = runner.run_plan(plan, _contract(), replan_fn=_no_replanner)
            self.assertEqual(result.plan.status, PlanStatus.FAILED)
            self.assertEqual(result.plan.nodes[1].status, PlanStatus.DRAFT)

    def test_goal_success_measures_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, registry = _make_runner(tmp)

            async def scorer(inputs):
                return {"metrics": {"score": 0.9}}

            registry.register("scorer", scorer)
            node = PlanNode(
                id="node-1",
                objective="score it",
                assigned_claw="scorer",
                criteria=[{"type": "metric", "name": "score", "op": ">=", "value": 0.8}],
            )
            plan = _plan("plan-metric", [node])
            store.save(plan)
            contract = _contract(
                success_measures=[
                    SuccessMeasure(name="good score", metric="score", op=">=", value=0.8)
                ]
            )
            result = runner.run_plan(plan, contract)
            self.assertEqual(result.goal_verdict, "PASS")

            # Same run, stricter measure: goal fails honestly.
            plan2 = _plan("plan-metric-2", [PlanNode(
                id="node-1",
                objective="score it",
                assigned_claw="scorer",
                criteria=[{"type": "metric", "name": "score", "op": ">=", "value": 0.8}],
            )])
            store.save(plan2)
            contract2 = _contract(
                success_measures=[
                    SuccessMeasure(name="great score", metric="score", op=">=", value=0.95)
                ]
            )
            result2 = runner.run_plan(plan2, contract2)
            self.assertEqual(result2.goal_verdict, "FAIL")
            self.assertIn("great score", result2.goal_detail)

    def test_node_timeout_fails_the_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            node = PlanNode(
                id="node-1",
                objective="sleep too long",
                assigned_claw="bash",
                inputs={"command": "sleep 10"},
                timeout_s=0.5,
            )
            plan = _plan("plan-timeout", [node])
            store.save(plan)
            result = runner.run_plan(plan, _contract())
            finished = result.plan.nodes[0]
            self.assertEqual(finished.status, PlanStatus.FAILED)
            self.assertIn("timed out", finished.error or "")
            self.assertEqual(finished.measurement["verdict"], "FAIL")

    def test_plan_over_node_budget_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            nodes = [
                PlanNode(id=f"node-{i}", objective="x", assigned_claw="bash",
                         inputs={"command": "true"})
                for i in range(3)
            ]
            plan = _plan("plan-big", nodes)
            store.save(plan)
            contract = _contract()
            contract.budget.max_nodes = 2
            with self.assertRaises(ValueError):
                runner.run_plan(plan, contract)

    def test_run_goal_builds_plan_from_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            result = runner.run_goal(
                _contract(),
                TaskIntent(goal="write a file"),
                nodes=[{
                    "id": "write",
                    "objective": "write it",
                    "assigned_claw": "file_writer",
                    "inputs": {"file_path": "hello.txt", "content": "hi"},
                    "criteria": [{"type": "file_exists", "path": "hello.txt"}],
                }],
            )
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(result.plan.nodes[0].status, PlanStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
