"""The repeat gate's substance check (2026-11-23 judge-gate fix).

Defect: run run-20261124T003551Z-523bc2's substantive /health plan
was vetoed before execution because its node-1 signature —
('scaffold', 'write', (api.py,)) — matched experiment memory. The
signature is mechanism + targets only; the recorded failure that
earned it was a bundled criterion timeout on a different payload,
and the same signature had measured PASSes after it. The gate now
adjudicates a cross-run signature match on substance
(judge.assess_approach_repeat): the veto stands for a payload
identical to a measured-failed one, for a payload the deterministic
confession rule convicts, and for signature forms that ARE their
payload (bash command text, recorded params); a different payload
proceeds with the historical warning recorded.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.judge import Judge, assess_approach_repeat
from freetalon.orchestrator.memory import (
    ExperimentStore,
    approach_for,
    goal_key_for,
    signature_from_approach,
)
from freetalon.orchestrator.models import PlanNode, RunBudget


def _measured(verdict="FAIL", measured=True):
    return {
        "node_id": "n",
        "verdict": verdict,
        "results": [
            {
                "criterion": {"type": "exit_code", "equals": 0},
                "passed": verdict == "PASS",
                "measured": measured,
                "detail": "ok" if verdict == "PASS" else "exit_code 1 != 0",
            }
        ],
        "metrics": {},
        "exit_code": 0 if verdict == "PASS" else 1,
        "duration_s": 0.1,
    }


def _scaffold(target="/repo/api.py", content="# v1\n", **kwargs):
    return PlanNode(
        id=kwargs.pop("id", "node-1"),
        objective=kwargs.pop("objective", "write the module"),
        assigned_claw="scaffold",
        inputs={"file_path": target, "content": content},
        criteria=[{"type": "file_exists", "path": target}],
        **kwargs,
    )


def _record_for(node, verdict="FAIL", run_id="run-hist", lesson="a lesson"):
    approach = approach_for(node)
    return {
        "signature": signature_from_approach(approach),
        "approach": approach,
        "verdict": verdict,
        "run_id": run_id,
        "node_id": node.id,
        "ts": "2026-11-18T00:00:00+00:00",
        "lesson": lesson,
    }


class TestAssessApproachRepeat(unittest.TestCase):
    def test_no_records_no_repeat(self):
        self.assertIsNone(assess_approach_repeat(_scaffold(), []))

    def test_pass_only_history_no_repeat(self):
        records = [_record_for(_scaffold(), verdict="PASS")]
        self.assertIsNone(assess_approach_repeat(_scaffold(), records))

    def test_differs_by_exempts(self):
        records = [_record_for(_scaffold())]
        node = _scaffold(differs_by="system repair note")
        self.assertIsNone(assess_approach_repeat(node, records))

    def test_bash_command_match_vetoes(self):
        # The command-form signature IS the payload: a match is the
        # identical substance that was measured FAIL.
        failed = PlanNode(
            id="node-1", objective="exit zero", assigned_claw="bash",
            inputs={"command": "exit 1"},
        )
        records = [_record_for(failed)]
        assessment = assess_approach_repeat(failed, records)
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.veto)

    def test_identical_write_payload_vetoes(self):
        records = [_record_for(_scaffold(content="# v1\n"))]
        assessment = assess_approach_repeat(
            _scaffold(content="# v1\n", objective="reworded"), records
        )
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.veto)
        self.assertIn("identical", assessment.reason)

    def test_whitespace_variant_write_payload_vetoes(self):
        records = [_record_for(_scaffold(content="x = 1\ny = 2\n"))]
        assessment = assess_approach_repeat(
            _scaffold(content="x = 1\n\n  y = 2\n"), records
        )
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.veto)

    def test_different_write_payload_proceeds_with_warning(self):
        records = [_record_for(_scaffold(content="# v1\n"))]
        assessment = assess_approach_repeat(
            _scaffold(content="# v2, a real implementation\n"), records
        )
        self.assertIsNotNone(assessment)
        self.assertFalse(assessment.veto)
        self.assertIn("warning", assessment.reason)
        self.assertIn("differs from every recorded failed payload",
                      assessment.reason)

    def test_confessing_write_payload_vetoes(self):
        # A different payload that is hollow by the deterministic
        # confession rule: the veto stands on the plan's own
        # evidence, not on history.
        records = [_record_for(_scaffold(content="# v1\n"))]
        hollow = (
            "def health():\n"
            "    return {\n"
            '        "gpu_temperature": 0,  # placeholder value\n'
            "    }\n"
        )
        assessment = assess_approach_repeat(
            _scaffold(content=hollow), records
        )
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.veto)
        self.assertIn("confession", assessment.reason)

    def test_bash_write_different_command_proceeds(self):
        failed = PlanNode(
            id="node-1", objective="write via shell", assigned_claw="bash",
            inputs={"command": "cat > /repo/api.py <<'EOF'\nv1\nEOF"},
        )
        records = [_record_for(failed)]
        candidate = PlanNode(
            id="node-1", objective="write via shell", assigned_claw="bash",
            inputs={"command": "cat > /repo/api.py <<'EOF'\nv2 real\nEOF"},
        )
        assessment = assess_approach_repeat(candidate, records)
        self.assertIsNotNone(assessment)
        self.assertFalse(assessment.veto)

    def test_bash_write_identical_command_vetoes(self):
        failed = PlanNode(
            id="node-1", objective="write via shell", assigned_claw="bash",
            inputs={"command": "cat > /repo/api.py <<'EOF'\nv1\nEOF"},
        )
        records = [_record_for(failed)]
        assessment = assess_approach_repeat(failed, records)
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.veto)

    def test_no_payload_fail_only_history_vetoes(self):
        # The candidate carries no payload to distinguish itself
        # with, and memory holds no later PASS: the gate does not
        # guess a difference it cannot establish.
        records = [_record_for(_scaffold(content="# v1\n"))]
        bare = PlanNode(
            id="node-1", objective="write the module",
            assigned_claw="scaffold", inputs={"file_path": "/repo/api.py"},
        )
        assessment = assess_approach_repeat(bare, records)
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.veto)

    def test_no_payload_superseded_failure_warns(self):
        # FAIL then PASS for the same signature: memory's newest
        # executed word on the approach is PASS, so even with no
        # payload to compare, the failure no longer vetoes.
        records = [
            _record_for(_scaffold(content="# v1\n"), run_id="run-fail"),
            _record_for(_scaffold(content="# v2\n"), verdict="PASS",
                        run_id="run-pass"),
        ]
        bare = PlanNode(
            id="node-1", objective="write the module",
            assigned_claw="scaffold", inputs={"file_path": "/repo/api.py"},
        )
        assessment = assess_approach_repeat(bare, records)
        self.assertIsNotNone(assessment)
        self.assertFalse(assessment.veto)
        self.assertIn("superseded", assessment.reason)


class TestExecutedApproachRecords(unittest.TestCase):
    def _store(self, tmp):
        return ExperimentStore(Path(tmp) / "exp.db")

    def _seed(self, store, node, verdict="FAIL", run_id="run-hist",
              measured=True):
        return store.record(
            run_id=run_id,
            goal_key=goal_key_for("test goal"),
            domain="general",
            node=node,
            measurement=_measured(verdict, measured=measured),
            decision="REPLAN" if verdict == "FAIL" else "CONTINUE",
        )

    def test_records_carry_fail_and_pass_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._seed(store, _scaffold(content="# v1\n"), run_id="run-1")
            self._seed(store, _scaffold(content="# v2\n"), verdict="PASS",
                       run_id="run-2")
            records = store.executed_approach_records(
                goal_key_for("test goal"), "general"
            )
            self.assertEqual([r["run_id"] for r in records],
                             ["run-1", "run-2"])
            self.assertEqual(records[0]["verdict"], "FAIL")
            self.assertEqual(records[1]["verdict"], "PASS")
            # failed_approaches is the FAIL-only view of the same set.
            self.assertEqual(
                store.failed_approaches(goal_key_for("test goal"), "general"),
                {records[0]["signature"]},
            )

    def test_planning_stage_and_plumbing_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            # Planning-stage failure: never executed, never a record.
            self._seed(store, _scaffold(), measured=False)
            # Plumbing: a curl check has no signature at all.
            self._seed(store, PlanNode(
                id="node-9", objective="check", assigned_claw="bash",
                inputs={"command": "curl -s http://localhost:8765/health"},
            ))
            self.assertEqual(
                store.executed_approach_records(
                    goal_key_for("test goal"), "general"
                ),
                [],
            )


class TestValidateDeltaPlanCrossRun(unittest.TestCase):
    """The cross-run half of delta validation uses the substance
    check; the brief's own (within-run) failures stay strict — that
    half is pinned by test_judge and unchanged."""

    def _judge(self):
        return Judge(RunBudget())

    def test_cross_run_identical_payload_rejected(self):
        judge = self._judge()
        records = [_record_for(_scaffold(content="# v1\n"))]
        error = judge.validate_delta_plan(
            {"failed_nodes": []},
            [_scaffold(content="# v1\n", objective="reworded")],
            known_failure_records=records,
        )
        self.assertIsNotNone(error)
        self.assertIn("repeats failed approach", error)

    def test_cross_run_new_payload_accepted_with_warning(self):
        judge = self._judge()
        records = [_record_for(_scaffold(content="# v1\n"))]
        error = judge.validate_delta_plan(
            {"failed_nodes": []},
            [_scaffold(content="# v2, a real implementation\n")],
            known_failure_records=records,
        )
        self.assertIsNone(error)
        self.assertEqual(len(judge.repeat_warnings), 1)
        self.assertIn("warning", judge.repeat_warnings[0])

    def test_cross_run_confessing_payload_rejected(self):
        judge = self._judge()
        records = [_record_for(_scaffold(content="# v1\n"))]
        hollow = '"gpu_temperature": 0,  # placeholder value\n'
        error = judge.validate_delta_plan(
            {"failed_nodes": []},
            [_scaffold(content=hollow)],
            known_failure_records=records,
        )
        self.assertIsNotNone(error)

    def test_signature_set_only_stays_strict(self):
        # Back-compat: a caller passing only the signature set gets
        # the pre-fix signature-strict cross-run behavior.
        judge = self._judge()
        signature = signature_from_approach(approach_for(_scaffold()))
        error = judge.validate_delta_plan(
            {"failed_nodes": []},
            [_scaffold(content="# v2, a real implementation\n")],
            known_failures={signature},
        )
        self.assertIsNotNone(error)
        self.assertEqual(judge.repeat_warnings, [])


if __name__ == "__main__":
    unittest.main()
