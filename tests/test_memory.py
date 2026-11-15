"""Tests for experiment memory (M2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from freetalon.orchestrator.memory import (
    ExperimentStore,
    approach_for,
    goal_key_for,
    lesson_for,
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
            self.assertEqual(failed, {("bash", "h-fail")})

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


if __name__ == "__main__":
    unittest.main()
