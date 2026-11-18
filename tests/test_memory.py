"""Tests for experiment memory (M2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from freetalon.orchestrator.memory import (
    ExperimentStore,
    approach_for,
    approach_signature,
    describe_signature,
    goal_key_for,
    lesson_for,
    signature_from_approach,
)


def _node(node_id="n1", claw="bash", hypothesis="h1", objective="do it"):
    return SimpleNamespace(
        id=node_id, assigned_claw=claw, hypothesis=hypothesis,
        objective=objective, inputs={"command": "exit 1"},
    )


def _measurement(verdict="FAIL", exit_code=1):
    return {
        "node_id": "n1",
        "verdict": verdict,
        "results": [{"criterion": {"type": "exit_code", "equals": 0},
                     "passed": False, "measured": True,
                     "detail": "exit_code 1 != 0"}],
        "metrics": {"score": 0.5},
        "exit_code": exit_code,
        "duration_s": 0.4,
    }


class TestGoalKey(unittest.TestCase):
    def test_stable_and_case_insensitive(self):
        self.assertEqual(goal_key_for("Do X"), goal_key_for("  do   x "))
        self.assertNotEqual(goal_key_for("Do X"), goal_key_for("Do Y"))

    def test_approach_excludes_volatile_inputs(self):
        node = _node()
        node.inputs = {"command": "exit 1", "workdir": "/tmp/x", "timeout_s": 5}
        approach = approach_for(node)
        self.assertEqual(approach["capability"], "bash")
        self.assertNotIn("workdir", approach["key_params"])
        self.assertNotIn("timeout_s", approach["key_params"])
        self.assertIn("command", approach["key_params"])


class TestExperimentStore(unittest.TestCase):
    def test_record_and_retrieve(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            record = store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_node(), measurement=_measurement(), decision="REPLAN",
            )
            self.assertEqual(record["decision"], "REPLAN")
            self.assertIn("exit_code 1", record["lesson"])
            self.assertEqual(store.count(), 1)
            records = store.experiments_for_goal("g1", "general")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["measurement"]["verdict"], "FAIL")
            self.assertEqual(records[0]["approach"]["capability"], "bash")

    def test_failed_approaches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_node(hypothesis="h-fail"), measurement=_measurement(),
                decision="REPLAN",
            )
            store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_node(node_id="n2", hypothesis="h-pass"),
                measurement=_measurement(verdict="PASS", exit_code=0),
                decision="CONTINUE",
            )
            failed = store.failed_approaches("g1", "general")
            # Cycle-3 pin change: the failed-approach identity is the
            # substance signature (mechanism + targets), not the old
            # (capability, hypothesis-text) pair. The fixture node's
            # substance is its command; its hypothesis text ("h-fail")
            # no longer identifies it.
            self.assertEqual(failed, {("bash", "command", "exit 1")})

    def test_goal_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            store.record(
                run_id="r1", goal_key="g1", domain="finance",
                node=_node(), measurement=_measurement(), decision="REPLAN",
            )
            self.assertEqual(store.experiments_for_goal("g2", "finance"), [])
            self.assertEqual(store.experiments_for_goal("g1", "ecu"), [])
            self.assertEqual(store.failed_approaches("g1", "ecu"), set())

    def test_append_only_nothing_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            for i in range(3):
                store.record(
                    run_id=f"r{i}", goal_key="g1", domain="general",
                    node=_node(node_id=f"n{i}"), measurement=_measurement(),
                    decision="REPLAN",
                )
            self.assertEqual(store.count(), 3)
            self.assertEqual(len(store.experiments_for_goal("g1", "general")), 3)

    def test_lesson_is_factual(self):
        node = _node()
        lesson = lesson_for(node, _measurement(), "REPLAN")
        self.assertIn("FAIL", lesson)
        self.assertIn("exit_code 1 != 0", lesson)
        self.assertIn("REPLAN", lesson)


def _rich_node(claw, inputs, hypothesis="h", objective="o", node_id="n1"):
    return SimpleNamespace(
        id=node_id, assigned_claw=claw, hypothesis=hypothesis,
        objective=objective, inputs=inputs,
    )


class TestApproachSignature(unittest.TestCase):
    """Cycle 3: identity is substance (mechanism + targets), not text."""

    def test_scaffold_signature_is_path_not_content_or_wording(self):
        a = _rich_node(
            "scaffold",
            {"file_path": "/repo/api.py", "content": "x = 1\n"},
            hypothesis="first wording", objective="do the thing",
        )
        b = _rich_node(
            "scaffold",
            {"file_path": "/repo/api.py", "content": "totally different\n"},
            hypothesis="second wording", objective="other words",
        )
        c = _rich_node(
            "scaffold", {"file_path": "/repo/other.py", "content": "x = 1\n"}
        )
        self.assertEqual(approach_signature(a), ("scaffold", "write", ("/repo/api.py",)))
        self.assertEqual(approach_signature(a), approach_signature(b))
        self.assertNotEqual(approach_signature(a), approach_signature(c))

    def test_file_writer_accepts_path_key(self):
        node = _rich_node("file_writer", {"path": "/repo/a.py", "content": "z\n"})
        self.assertEqual(
            approach_signature(node), ("file_writer", "write", ("/repo/a.py",))
        )

    def test_bash_plumbing_has_no_signature(self):
        plumbing = [
            "curl -s http://localhost:8765/health",
            "curl -sf http://localhost:8765/health | grep OK",
            "cd /repo && ./.venv/bin/python -m unittest discover -s tests",
            "python3 /home/seeker/freetalon-workspace/test_health.py",
            "/home/seeker/freetalon-workspace/test.sh",
            "cd /repo && ./venv/bin/python -m freetalon.cli start",
            "cd /repo && ./.venv/bin/python -m freetalon.cli stop; "
            "./.venv/bin/python -m freetalon.cli start && sleep 1",
        ]
        for command in plumbing:
            with self.subTest(command=command):
                node = _rich_node("bash", {"command": command})
                self.assertIsNone(approach_signature(node))

    def test_bash_writes_key_on_targets(self):
        heredoc = _rich_node(
            "bash",
            {"command": "cat > /ws/test_health.py <<'EOF'\npass\nEOF"},
        )
        self.assertEqual(
            approach_signature(heredoc), ("bash", "write", ("/ws/test_health.py",))
        )
        redirect = _rich_node("bash", {"command": "echo 'x = 1' > /ws/out.py"})
        self.assertEqual(
            approach_signature(redirect), ("bash", "write", ("/ws/out.py",))
        )

    def test_bash_substantive_command_keys_on_command_text(self):
        node = _rich_node("bash", {"command": "exit 1"}, hypothesis="wording")
        self.assertEqual(approach_signature(node), ("bash", "command", "exit 1"))
        # An observation pipeline is not in the plumbing set: it keys
        # on its exact text, so only its literal repeat can match.
        check = _rich_node(
            "bash", {"command": "head -n 1 /x/api.py | grep -qx 'def health'"}
        )
        self.assertEqual(
            approach_signature(check),
            ("bash", "command", "head -n 1 /x/api.py | grep -qx 'def health'"),
        )

    def test_generic_claw_keys_on_params_not_outputs(self):
        # The executor merges outputs into inputs before recording;
        # exit_code/stdout must not split one approach into two.
        tried = _rich_node(
            "network",
            {"host": "10.0.0.1", "command": "show vlan", "exit_code": 1,
             "stdout": "err"},
        )
        candidate = _rich_node(
            "network", {"host": "10.0.0.1", "command": "show vlan"}
        )
        self.assertEqual(approach_signature(tried), approach_signature(candidate))
        self.assertEqual(approach_signature(tried)[1], "params")

    def test_describe_signature(self):
        self.assertIn(
            "/repo/api.py",
            describe_signature(("scaffold", "write", ("/repo/api.py",))),
        )
        self.assertIn(
            "exit 1", describe_signature(("bash", "command", "exit 1"))
        )


class TestFailedApproachStages(unittest.TestCase):
    """Cycle 3: only executed-stage, substantive failures are approaches."""

    def _planning_measurement(self):
        # A failure recorded before anything ran: no criterion was
        # ever measured.
        return {
            "node_id": "n1",
            "verdict": "FAIL",
            "results": [{"criterion": {"type": "exit_code", "equals": 0},
                         "passed": False, "measured": False,
                         "detail": "plan rejected at planning"}],
            "metrics": {},
            "exit_code": None,
            "duration_s": None,
        }

    def test_record_stamps_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            executed = store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_node(), measurement=_measurement(), decision="REPLAN",
            )
            self.assertEqual(executed["measurement"]["stage"], "executed")
            planned = store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_node(node_id="n2"),
                measurement=self._planning_measurement(), decision="ESCALATE",
            )
            self.assertEqual(planned["measurement"]["stage"], "planning")

    def test_planning_stage_failure_is_not_a_failed_approach(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_node(), measurement=self._planning_measurement(),
                decision="ESCALATE",
            )
            self.assertEqual(store.failed_approaches("g1", "general"), set())

    def test_gate_denial_record_is_executed_stage(self):
        # The gate path records a verdict with NO criterion results:
        # the approach was tried and refused at the cliff edge, so it
        # counts — a denied irreversible action is never re-asked
        # (the M4 contract, pinned end-to-end in test_loop_m4).
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_rich_node(
                    "flash_ecu",
                    {"action": "flash_ecu", "file": "/t/tune.bin",
                     "risk": "brick"},
                ),
                measurement={"node_id": "n1", "verdict": "FAIL",
                             "detail": "gate denied by pat",
                             "plan_version": 1},
                decision="ESCALATE",
            )
            failed = store.failed_approaches("g1", "general")
            self.assertEqual(len(failed), 1)
            self.assertEqual(next(iter(failed))[0], "flash_ecu")

    def test_plumbing_failure_is_not_a_failed_approach(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_rich_node(
                    "bash", {"command": "curl -s http://localhost:8765/health"},
                    hypothesis="Verify /health live",
                    objective="Verify /health live",
                ),
                measurement=_measurement(exit_code=7), decision="REPLAN",
            )
            self.assertEqual(store.failed_approaches("g1", "general"), set())

    def test_executed_write_failure_is_a_failed_approach(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(Path(tmp) / "exp.db")
            store.record(
                run_id="r1", goal_key="g1", domain="general",
                node=_rich_node(
                    "scaffold",
                    {"file_path": "/repo/api.py", "content": "x = 1\n"},
                ),
                measurement=_measurement(), decision="REPLAN",
            )
            self.assertEqual(
                store.failed_approaches("g1", "general"),
                {("scaffold", "write", ("/repo/api.py",))},
            )

    def test_signature_from_stored_approach_matches_live_node(self):
        # The recorded approach dict and the live node must produce
        # the identical signature (one conversion, both sides).
        node = _rich_node(
            "bash", {"command": "cat > /ws/t.py <<'EOF'\npass\nEOF"}
        )
        self.assertEqual(
            signature_from_approach(approach_for(node)), approach_signature(node)
        )


if __name__ == "__main__":
    unittest.main()
