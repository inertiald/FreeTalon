"""Tests for the judge (loop Stage 5, M1)."""

from __future__ import annotations

import unittest

from freetalon.orchestrator.judge import Decision, Judge, ProgressTracker
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    RunBudget,
    TaskIntent,
)


def _node(node_id="node-1", status=PlanStatus.FAILED, **kw):
    fields = {
        "id": node_id,
        "objective": f"node {node_id}",
        "assigned_claw": "bash",
        "hypothesis": "h1",
    }
    fields.update(kw)
    node = PlanNode(**fields)
    node.status = status
    return node


def _plan(nodes, status=PlanStatus.FAILED):
    plan = ExecutionPlan(
        plan_id="p1", intent=TaskIntent(goal="g"), nodes=nodes, status=status
    )
    return plan


def _measurement(verdict="FAIL", envelope_status="completed"):
    return {
        "node_id": "node-1",
        "verdict": verdict,
        "results": [],
        "envelope_status": envelope_status,
    }


class TestJudgeDecide(unittest.TestCase):
    def test_continue_when_completed(self):
        judge = Judge(RunBudget())
        plan = _plan([_node(status=PlanStatus.COMPLETED)], PlanStatus.COMPLETED)
        decision = judge.decide(plan, [])
        self.assertEqual(decision.decision, Decision.CONTINUE)

    def test_replan_on_informative_failure(self):
        judge = Judge(RunBudget())
        node = _node(measurement=_measurement())
        plan = _plan([node])
        decision = judge.decide(plan, [_measurement()])
        self.assertEqual(decision.decision, Decision.REPLAN)

    def test_escalate_when_replan_budget_spent(self):
        judge = Judge(RunBudget(max_replans=0))
        node = _node(measurement=_measurement())
        plan = _plan([node])
        decision = judge.decide(plan, [_measurement()])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertIn("re-plan budget spent", decision.reason)

    def test_retry_on_timeout_with_retries_left(self):
        judge = Judge(RunBudget())
        node = _node(
            measurement=_measurement(envelope_status="timeout"),
            max_retries=1,
        )
        plan = _plan([node])
        judge.record_pass([node])
        decision = judge.decide(plan, [_measurement(envelope_status="timeout")])
        self.assertEqual(decision.decision, Decision.RETRY)
        self.assertEqual(decision.retry_node_ids, ["node-1"])

    def test_escalate_when_retries_exhausted(self):
        judge = Judge(RunBudget())
        node = _node(
            measurement=_measurement(envelope_status="timeout"),
            max_retries=0,
        )
        plan = _plan([node])
        judge.record_pass([node])
        decision = judge.decide(plan, [_measurement(envelope_status="timeout")])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertIn("retries", decision.reason)

    def test_escalate_on_missing_tool(self):
        judge = Judge(RunBudget())
        node = _node(status=PlanStatus.NEEDS_TOOL, assigned_claw="ghost")
        plan = _plan([node])
        decision = judge.decide(plan, [])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertIn("ghost", decision.reason)

    def test_escalate_on_repeated_unmeasurable(self):
        judge = Judge(RunBudget())
        node = _node(measurement=_measurement(verdict="UNMEASURABLE"))
        plan = _plan([node])
        judge.record_pass([node])
        judge.record_pass([node])  # second consecutive unmeasurable pass
        decision = judge.decide(plan, [_measurement(verdict="UNMEASURABLE")])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertIn("unmeasurable twice", decision.reason)

    def test_wall_clock_budget_escalates(self):
        judge = Judge(RunBudget(max_wall_clock_s=0.001))
        import time

        time.sleep(0.01)
        node = _node(measurement=_measurement())
        plan = _plan([node])
        decision = judge.decide(plan, [_measurement()])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertIn("wall-clock", decision.reason)


class TestProgressTracker(unittest.TestCase):
    def test_stalls_after_two_replans_without_progress(self):
        tracker = ProgressTracker()
        self.assertFalse(tracker.observe([]))
        tracker.note_replan(False)
        tracker.note_replan(False)
        self.assertTrue(tracker.stalled())

    def test_progress_resets_stall(self):
        tracker = ProgressTracker()
        tracker.note_replan(False)
        tracker.observe([{
            "results": [{"passed": True, "criterion": {"type": "exit_code", "equals": 0}}]
        }])
        tracker.note_replan(True)
        self.assertFalse(tracker.stalled())


class TestValidateDeltaPlan(unittest.TestCase):
    def _brief(self):
        judge = Judge(RunBudget())
        node = _node(measurement=_measurement())
        plan = _plan([node])
        from freetalon.orchestrator.models import GoalContract

        return judge.build_replan_brief(
            GoalContract(objective="g"), plan, [_measurement()], [], 1
        )

    def test_repeat_without_differs_by_rejected(self):
        judge = Judge(RunBudget())
        brief = self._brief()
        repeat = _node(hypothesis="h1")
        error = judge.validate_delta_plan(brief, [repeat])
        self.assertIsNotNone(error)
        self.assertIn("repeats failed approach", error)

    def test_repeat_with_differs_by_accepted(self):
        judge = Judge(RunBudget())
        brief = self._brief()
        repeat = _node(hypothesis="h1", differs_by="uses a file instead")
        self.assertIsNone(judge.validate_delta_plan(brief, [repeat]))

    def test_different_approach_accepted(self):
        judge = Judge(RunBudget())
        brief = self._brief()
        different = _node(hypothesis="a new idea", assigned_claw="file_writer")
        self.assertIsNone(judge.validate_delta_plan(brief, [different]))

    def test_brief_contains_goal_and_failures(self):
        brief = self._brief()
        self.assertEqual(brief["goal"]["objective"], "g")
        self.assertEqual(len(brief["failed_nodes"]), 1)
        self.assertIn("replans", brief["remaining_budget"])


if __name__ == "__main__":
    unittest.main()
