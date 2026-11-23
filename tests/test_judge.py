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

    def _brief_for(self, failed_node):
        judge = Judge(RunBudget())
        plan = _plan([failed_node])
        from freetalon.orchestrator.models import GoalContract

        return judge.build_replan_brief(
            GoalContract(objective="g"), plan, [_measurement()], [], 1
        )

    def test_plumbing_failure_does_not_reject_its_repeat(self):
        # Cycle 3: a failed verification step is plumbing — it has no
        # approach signature, so a delta plan may run the same check
        # again (the check was never the approach that failed).
        judge = Judge(RunBudget())
        brief = self._brief_for(_node(
            measurement=_measurement(),
            inputs={"command": "curl -s http://localhost:8765/health"},
        ))
        repeat = _node(inputs={"command": "curl -s http://localhost:8765/health"})
        self.assertIsNone(judge.validate_delta_plan(brief, [repeat]))

    def test_write_failure_rejects_repeat_write(self):
        # Cycle 3: same write mechanism on the same file — different
        # content and wording — is a repeat of the failed approach.
        judge = Judge(RunBudget())
        brief = self._brief_for(_node(
            measurement=_measurement(),
            assigned_claw="scaffold",
            inputs={"file_path": "/repo/api.py", "content": "# v1\n"},
        ))
        repeat = _node(
            hypothesis="entirely new wording",
            assigned_claw="scaffold",
            inputs={"file_path": "/repo/api.py", "content": "# v2\n"},
        )
        error = judge.validate_delta_plan(brief, [repeat])
        self.assertIsNotNone(error)
        self.assertIn("repeats failed approach", error)
        self.assertIn("/repo/api.py", error)
        changed = _node(
            assigned_claw="scaffold",
            inputs={"file_path": "/repo/api.py", "content": "# v2\n"},
            differs_by="rewrites the handler instead of appending",
        )
        self.assertIsNone(judge.validate_delta_plan(brief, [changed]))

    def test_brief_contains_goal_and_failures(self):
        brief = self._brief()
        self.assertEqual(brief["goal"]["objective"], "g")
        self.assertEqual(len(brief["failed_nodes"]), 1)
        self.assertIn("replans", brief["remaining_budget"])



# The node-3 failure shape from live re-run #2 (2026-11-18): a bash
# test-run node whose measurement is criterion-shaped ("exit_code 1
# != 0") while the assertion that names the actual defect lives only
# in the node's captured stderr.
_NODE3_STDERR = """F
======================================================================
FAIL: test_route_answers (__main__.RouteEndpointTests.test_route_answers)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/seeker/freetalon-workspace/test_health.py", line 48, in test_route_answers
    self.assertIsInstance(data["ollama_reachable"], bool)
AssertionError: 'Ellipsis' is not an instance of <class 'bool'>

----------------------------------------------------------------------
Ran 1 test in 0.563s

FAILED (failures=1)
"""

_NODE3_ASSERTION = "AssertionError: 'Ellipsis' is not an instance of <class 'bool'>"


def _executed_measurement():
    return {
        "node_id": "node-3",
        "verdict": "FAIL",
        "results": [
            {
                "criterion": {"type": "exit_code", "equals": 0},
                "passed": False,
                "measured": True,
                "detail": "exit_code 1 != 0",
            }
        ],
        "exit_code": 1,
        "duration_s": 0.773,
        "envelope_status": "completed",
    }


class TestBriefDiagnostics(unittest.TestCase):
    """Cycle 5: the re-plan brief carries the failure's substance."""

    def _brief(self, judge, node, plan_version=1):
        from freetalon.orchestrator.models import GoalContract

        plan = _plan([node])
        return judge.build_replan_brief(
            GoalContract(objective="g"),
            plan,
            [_executed_measurement()],
            [],
            plan_version,
        )

    def _node3(self, stderr=_NODE3_STDERR):
        return _node(
            "node-3",
            measurement=_executed_measurement(),
            error="failed criterion: exit_code 1 != 0",
            inputs={
                "command": "./.venv/bin/python /home/seeker/freetalon-workspace/test_health.py",
                "exit_code": 1,
                "stdout": "",
                "stderr": stderr,
            },
        )

    def test_brief_carries_failure_diagnostics(self):
        judge = Judge(RunBudget())
        brief = self._brief(judge, self._node3())
        entry = brief["failed_nodes"][0]
        # The assertion and its traceback frame reach the brief,
        # attributed to the node whose criterion line alone would
        # have said only "exit_code 1 != 0".
        self.assertIn(_NODE3_ASSERTION, entry["diagnostics"])
        self.assertIn('test_health.py", line 48', entry["diagnostics"])
        self.assertNotIn("repeated_diagnostic", entry)

        from freetalon.orchestrator.planner import _format_brief_for_prompt

        text = _format_brief_for_prompt(brief)
        self.assertIn("Captured output from failed node node-3", text)
        self.assertIn(_NODE3_ASSERTION, text)
        self.assertIn("failed criterion: exit_code 1 != 0", text)

    def test_brief_diagnostics_bounded_under_large_stderr(self):
        judge = Judge(RunBudget())
        big = "\n".join(f"noise line {i} " + "x" * 40 for i in range(5000))
        big += "\n" + _NODE3_STDERR
        brief = self._brief(judge, self._node3(stderr=big))
        diagnostics = brief["failed_nodes"][0]["diagnostics"]
        self.assertLessEqual(len(diagnostics), 1500)
        self.assertLessEqual(len(diagnostics.splitlines()), 12)
        # The tail survives the bound: the assertion is the substance.
        self.assertIn(_NODE3_ASSERTION, diagnostics)

    def test_brief_without_output_is_unchanged(self):
        judge = Judge(RunBudget())
        node = _node(
            "node-3",
            measurement=_executed_measurement(),
            error="failed criterion: exit_code 1 != 0",
            inputs={"command": "./.venv/bin/python tests/test_health.py"},
        )
        brief = self._brief(judge, node)
        entry = brief["failed_nodes"][0]
        self.assertEqual(entry["diagnostics"], "")
        self.assertNotIn("repeated_diagnostic", entry)

        from freetalon.orchestrator.planner import _format_brief_for_prompt

        text = _format_brief_for_prompt(brief)
        self.assertNotIn("Captured output", text)
        self.assertNotIn("Repeated failure", text)

    def test_format_brief_without_diagnostics_keys_is_byte_unchanged(self):
        # A brief dict in the pre-cycle-5 shape (no diagnostics keys)
        # renders exactly the prose it rendered before the change.
        from freetalon.orchestrator.planner import _format_brief_for_prompt

        brief = {
            "goal": {"objective": "g"},
            "plan_id": "p1",
            "plan_version": 1,
            "failed_nodes": [
                {
                    "id": "node-1",
                    "objective": "node node-1",
                    "assigned_claw": "bash",
                    "hypothesis": "h1",
                    "measurement": {
                        "node_id": "node-1",
                        "verdict": "FAIL",
                        "exit_code": 1,
                        "results": [
                            {
                                "criterion": {"type": "exit_code", "equals": 0},
                                "passed": False,
                                "measured": True,
                                "detail": "exit_code 1 != 0",
                            }
                        ],
                    },
                    "error": "failed criterion: exit_code 1 != 0",
                }
            ],
            "measurements": [],
            "memory": [],
            "remaining_budget": {},
            "instruction": "",
        }
        self.assertEqual(
            _format_brief_for_prompt(brief),
            "Re-plan brief:\n"
            "Goal: g\n"
            "Plan: p1 (version 1)\n"
            "Failed node node-1: node node-1 (claw: bash). "
            "Hypothesis: h1. "
            "Measurement: node node-1; verdict FAIL; exit 1; "
            "failed criterion: exit_code 1 != 0. "
            "Error: failed criterion: exit_code 1 != 0.",
        )

    def test_repeated_diagnostic_flagged_in_second_brief(self):
        judge = Judge(RunBudget())
        first = self._brief(judge, self._node3(), plan_version=1)
        self.assertNotIn("repeated_diagnostic", first["failed_nodes"][0])
        second = self._brief(judge, self._node3(), plan_version=2)
        entry = second["failed_nodes"][0]
        self.assertEqual(entry["repeated_diagnostic"], _NODE3_ASSERTION)
        self.assertEqual(entry["repeated_from_version"], 1)

        from freetalon.orchestrator.planner import _format_brief_for_prompt

        text = _format_brief_for_prompt(second)
        self.assertIn("Repeated failure: node node-3", text)
        self.assertIn("plan version 1", text)
        self.assertIn(_NODE3_ASSERTION, text)

    def test_diagnostics_without_error_line_not_tracked(self):
        judge = Judge(RunBudget())
        node = self._node3(stderr="curl: (7) Failed to connect to localhost\n")
        first = self._brief(judge, node, plan_version=1)
        self.assertIn("Failed to connect", first["failed_nodes"][0]["diagnostics"])
        second = self._brief(judge, self._node3(
            stderr="curl: (7) Failed to connect to localhost\n"), plan_version=2)
        self.assertNotIn("repeated_diagnostic", second["failed_nodes"][0])

    def test_stdout_used_when_no_stderr(self):
        judge = Judge(RunBudget())
        node = _node(
            "node-3",
            measurement=_executed_measurement(),
            error="failed criterion: exit_code 1 != 0",
            inputs={"stdout": "some output\n" + _NODE3_ASSERTION + "\n"},
        )
        brief = self._brief(judge, node)
        self.assertIn(_NODE3_ASSERTION, brief["failed_nodes"][0]["diagnostics"])


# The re-arm #8 failure shape (2026-11-23): a unittest run whose
# failures are identical across plan versions while the re-plans
# rewrite only the store. The judge now names the failing tests,
# stamps a stagnation record when the set repeats, tracks the run's
# write history, and gates a delta plan that re-attacks the same
# set through the same files.
_PAIRING_STDERR = """..F
======================================================================
FAIL: test_re_register_claimed_noop (tests.test_pairing.PairingStoreTests.test_re_register_claimed_noop)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_pairing.py", line 140, in test_re_register_claimed_noop
    with self.assertRaises(CodeConflict):
AssertionError: CodeConflict not raised

======================================================================
FAIL: test_register_idempotent (tests.test_pairing.PairingHttpTests.test_register_idempotent)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_pairing.py", line 96, in test_register_idempotent
    self.assertEqual(resp.status, 200)
AssertionError: 201 != 200

----------------------------------------------------------------------
Ran 33 tests in 7.268s

FAILED (failures=2)
"""

_PAIRING_FAILING = [
    "tests.test_pairing.PairingHttpTests.test_register_idempotent",
    "tests.test_pairing.PairingStoreTests.test_re_register_claimed_noop",
]

_OTHER_STDERR = """F
======================================================================
FAIL: test_wrong_method (tests.test_pairing.PairingHttpTests.test_wrong_method)
----------------------------------------------------------------------
AssertionError: 501 != 405

----------------------------------------------------------------------
Ran 33 tests in 7.301s

FAILED (failures=1)
"""


def _writer(node_id, target, status=PlanStatus.COMPLETED, **kw):
    fields = {
        "assigned_claw": "file_writer",
        "inputs": {"file_path": target, "content": "x = 1\n"},
    }
    fields.update(kw)
    return _node(node_id, status=status, **fields)


def _test_runner(stderr):
    return _node(
        "node-test",
        measurement=_executed_measurement(),
        error="failed criterion: exit_code 1 != 0",
        inputs={
            "command": "./.venv/bin/python -m unittest tests.test_pairing",
            "exit_code": 1,
            "stdout": "",
            "stderr": stderr,
        },
    )


class TestReplanAnchoring(unittest.TestCase):
    def _brief(self, judge, nodes, plan_version, plan=None):
        from freetalon.orchestrator.models import GoalContract

        plan = plan if plan is not None else _plan(nodes)
        return judge.build_replan_brief(
            GoalContract(objective="g"), plan, [_measurement()], [], plan_version
        )

    def _v1_nodes(self, stderr=_PAIRING_STDERR):
        return [
            _writer("w-store", "pkg/store.py"),
            _writer("w-server", "pkg/server.py"),
            _test_runner(stderr),
        ]

    def _v2_nodes(self, stderr=_PAIRING_STDERR):
        return [_writer("w-store", "pkg/store.py"), _test_runner(stderr)]

    def test_failing_tests_named_from_unittest_output(self):
        judge = Judge(RunBudget())
        brief = self._brief(judge, self._v1_nodes(), 1)
        self.assertEqual(brief["failing_tests"], _PAIRING_FAILING)
        self.assertNotIn("stagnation", brief)
        self.assertEqual(brief["never_revised_files"], [])

    def test_failing_tests_named_from_pytest_output(self):
        judge = Judge(RunBudget())
        stderr = (
            "FAILED tests/test_pairing.py::PairingStoreTests::test_x - "
            "AssertionError: boom\n"
            "FAILED tests/test_pairing.py::test_y\n"
        )
        brief = self._brief(judge, [_test_runner(stderr)], 1)
        self.assertEqual(
            brief["failing_tests"],
            [
                "tests/test_pairing.py::PairingStoreTests::test_x",
                "tests/test_pairing.py::test_y",
            ],
        )

    def test_failing_tests_bare_unittest_header(self):
        judge = Judge(RunBudget())
        brief = self._brief(judge, [_test_runner("FAIL: test_lonely\n")], 1)
        self.assertEqual(brief["failing_tests"], ["test_lonely"])

    def test_stagnation_stamped_when_set_repeats(self):
        judge = Judge(RunBudget())
        first = self._brief(judge, self._v1_nodes(), 1)
        self.assertNotIn("stagnation", first)
        second = self._brief(judge, self._v2_nodes(), 2)
        self.assertEqual(second["failing_tests"], _PAIRING_FAILING)
        stagnation = second["stagnation"]
        self.assertEqual(stagnation["failing_tests"], _PAIRING_FAILING)
        self.assertEqual(stagnation["unchanged_since_version"], 1)
        self.assertEqual(stagnation["last_change_files"], ["pkg/store.py"])
        # server.py was produced by v1 and not revised by v2.
        self.assertEqual(second["never_revised_files"], ["pkg/server.py"])

    def test_stagnation_absent_when_set_changes(self):
        judge = Judge(RunBudget())
        self._brief(judge, self._v1_nodes(), 1)
        second = self._brief(judge, self._v2_nodes(stderr=_OTHER_STDERR), 2)
        self.assertEqual(
            second["failing_tests"],
            ["tests.test_pairing.PairingHttpTests.test_wrong_method"],
        )
        self.assertNotIn("stagnation", second)

    def test_missing_declared_and_never_revised(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "store.py").write_text("x = 1\n", encoding="utf-8")
            judge = Judge(RunBudget())
            plan1 = _plan(self._v1_nodes())
            plan1.metadata["repo_root"] = tmp
            first = self._brief(judge, [], 1, plan=plan1)
            # server.py was declared by v1 but never produced.
            self.assertEqual(first["missing_declared_files"], ["pkg/server.py"])
            self.assertEqual(first["produced_files"], ["pkg/store.py"])
            self.assertEqual(first["never_revised_files"], [])
            plan2 = _plan(self._v2_nodes())
            plan2.metadata["repo_root"] = tmp
            second = self._brief(judge, [], 2, plan=plan2)
            self.assertEqual(second["never_revised_files"], ["pkg/server.py"])
            self.assertEqual(second["missing_declared_files"], ["pkg/server.py"])

    def _stagnant_brief(self):
        judge = Judge(RunBudget())
        self._brief(judge, self._v1_nodes(), 1)
        return judge, self._brief(judge, self._v2_nodes(), 2)

    def test_gate_fires_on_subset_targets_with_stagnant_set(self):
        judge, brief = self._stagnant_brief()
        repeat = _writer(
            "n1", "pkg/store.py", status=PlanStatus.DRAFT
        )
        error = judge.validate_delta_plan(brief, [repeat])
        self.assertIsNotNone(error)
        self.assertIn("repeats failed approach", error)
        self.assertIn("stagnation", error)
        # The unexamined suspect is named: server.py, never revised.
        self.assertIn("pkg/server.py", error)
        self.assertIn("fix the test", error)

    def test_gate_silent_when_targets_widen(self):
        judge, brief = self._stagnant_brief()
        widened = [
            _writer("n1", "pkg/store.py", status=PlanStatus.DRAFT),
            _writer("n2", "pkg/server.py", status=PlanStatus.DRAFT),
        ]
        self.assertIsNone(judge.validate_delta_plan(brief, widened))

    def test_gate_silent_on_first_replan(self):
        judge = Judge(RunBudget())
        first = self._brief(judge, self._v1_nodes(), 1)
        repeat = _writer("n1", "pkg/store.py", status=PlanStatus.DRAFT)
        self.assertIsNone(judge.validate_delta_plan(first, [repeat]))

    def test_gate_silent_with_system_repair_note(self):
        judge, brief = self._stagnant_brief()
        repaired = _writer(
            "n1",
            "pkg/store.py",
            status=PlanStatus.DRAFT,
            differs_by="splices the measured fix into the definition",
        )
        self.assertIsNone(judge.validate_delta_plan(brief, [repaired]))

    def test_gate_silent_without_write_targets(self):
        judge, brief = self._stagnant_brief()
        plumbing = _node(
            "n1",
            status=PlanStatus.DRAFT,
            inputs={"command": "./.venv/bin/python -m unittest tests.test_pairing"},
        )
        self.assertIsNone(judge.validate_delta_plan(brief, [plumbing]))


if __name__ == "__main__":
    unittest.main()
