"""Integration tests for M2 experiment memory.

The M2 done test: a second run of the same goal does not repeat a
measured-failed approach.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws


def _make_runner(tmpdir: str):
    store = ExecutionPlanStateStore(db_path=Path(tmpdir) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    run_store = RunStore(Path(tmpdir) / "runs")
    memory = ExperimentStore(Path(tmpdir) / "memory.db")
    runner = LoopRunner(store, registry, run_store=run_store, memory_store=memory)
    return runner, store, registry, memory


def _plan(plan_id: str, nodes: list[PlanNode]) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id, intent=TaskIntent(goal="test goal"), nodes=nodes
    )


def _failing_node():
    return PlanNode(
        id="node-1",
        objective="exit zero",
        assigned_claw="bash",
        inputs={"command": "exit 1"},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the command exits zero",
    )


def _no_replanner(brief):
    raise RuntimeError("no re-planner in this test")


class TestLoopMemory(unittest.TestCase):
    def test_second_run_does_not_repeat_failed_approach(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            # Run 1: fails, and the failure is recorded in memory.
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            result1 = runner.run_plan(plan1, contract, replan_fn=_no_replanner)
            self.assertEqual(result1.goal_verdict, "FAIL")
            self.assertEqual(memory.count(), 1)
            # The run's own memory.jsonl has the record too.
            mem_records = runner._run_store.read_memory(result1.run_id)
            self.assertEqual(len(mem_records), 1)
            self.assertEqual(mem_records[0]["decision"], "ESCALATE")

            # Run 2: the same goal, the same approach — refused before running.
            plan2 = _plan("plan-mem-2", [_failing_node()])
            store.save(plan2)
            result2 = runner.run_plan(plan2, contract, replan_fn=_no_replanner)
            self.assertEqual(result2.goal_verdict, "FAIL")
            self.assertIsNotNone(result2.escalation)
            self.assertIn("repeats a measured-failed approach", result2.escalation)
            self.assertEqual(result2.measurements, [])  # nothing executed
            self.assertEqual(result2.plan.nodes[0].status, PlanStatus.DRAFT)
            self.assertEqual(memory.count(), 1)  # no new experiment recorded

    def test_second_run_allowed_when_it_states_what_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            runner.run_plan(plan1, contract, replan_fn=_no_replanner)
            self.assertEqual(memory.count(), 1)

            # Same approach, but differs_by states the change — allowed.
            changed = PlanNode(
                id="node-1",
                objective="write a marker instead",
                assigned_claw="bash",
                inputs={"command": "echo ok > ok.txt"},
                criteria=[{"type": "file_exists", "path": "ok.txt"}],
                hypothesis="the command exits zero",
                differs_by="writes a file instead of relying on exit code",
            )
            plan2 = _plan("plan-mem-2", [changed])
            store.save(plan2)
            result2 = runner.run_plan(plan2, contract, replan_fn=_no_replanner)
            self.assertEqual(result2.goal_verdict, "PASS")
            self.assertEqual(memory.count(), 2)

    def test_replan_rejected_when_it_repeats_a_recorded_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=3)
            )
            # Run 1 records the bash/exit-zero failure.
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            runner.run_plan(plan1, contract, replan_fn=_no_replanner)

            # Run 2: a different first approach fails; its re-plan tries
            # the recorded-failed approach again, without differs_by.
            other = PlanNode(
                id="node-1",
                objective="write a missing file",
                assigned_claw="bash",
                inputs={"command": "true"},
                criteria=[{"type": "file_exists", "path": "nope.txt"}],
                hypothesis="the file exists",
            )
            plan2 = _plan("plan-mem-2", [other])
            store.save(plan2)

            def repeat_recorded_failure(brief):
                return [_failing_node()]

            result2 = runner.run_plan(
                plan2, contract, replan_fn=repeat_recorded_failure
            )
            self.assertEqual(result2.goal_verdict, "FAIL")
            self.assertIsNotNone(result2.escalation)
            self.assertIn("repeats failed approach", result2.escalation)

    def test_different_goal_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            runner.run_plan(plan1, contract, replan_fn=_no_replanner)
            self.assertEqual(memory.count(), 1)

            other_contract = GoalContract(
                objective="a completely different goal",
                budget=RunBudget(max_replans=0),
            )
            plan2 = _plan("plan-mem-3", [_failing_node()])
            store.save(plan2)
            result2 = runner.run_plan(plan2, other_contract, replan_fn=_no_replanner)
            # Runs (and fails on its own merit) — memory for the other goal
            # does not block it.
            self.assertEqual(len(result2.measurements), 1)
            self.assertEqual(memory.count(), 2)


if __name__ == "__main__":
    unittest.main()
