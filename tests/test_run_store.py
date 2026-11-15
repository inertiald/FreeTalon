"""Tests for the per-run directory store (loop Stage 3, M0)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.models import GoalContract
from freetalon.orchestrator.run_store import RunStore, new_run_id


class TestRunStore(unittest.TestCase):
    def test_new_run_id_format(self):
        run_id = new_run_id()
        self.assertTrue(run_id.startswith("run-"))
        self.assertEqual(len(run_id.split("-")), 3)

    def test_create_run_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id = new_run_id()
            run_dir = store.create_run(
                run_id, GoalContract(objective="test").model_dump(mode="json")
            )
            self.assertTrue((run_dir / "contract.json").exists())
            self.assertTrue((run_dir / "nodes").is_dir())
            self.assertTrue((run_dir / "gates").is_dir())
            self.assertTrue((run_dir / "logs").is_dir())
            contract = json.loads((run_dir / "contract.json").read_text())
            self.assertEqual(contract["objective"], "test")

    def test_plan_versions_numbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id = new_run_id()
            store.create_run(run_id, {"objective": "x"})
            p1 = store.write_plan_version(run_id, 1, {"plan_id": "p1"})
            p2 = store.write_plan_version(run_id, 2, {"plan_id": "p2"})
            self.assertEqual(p1.name, "plan-v001.json")
            self.assertEqual(p2.name, "plan-v002.json")
            self.assertEqual(json.loads(p2.read_text())["plan_id"], "p2")

    def test_node_output_and_measurements_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id = new_run_id()
            store.create_run(run_id, {"objective": "x"})
            paths = store.write_node_output(
                run_id, "node-1", stdout="hello\n", stderr="warn\n"
            )
            self.assertEqual(Path(paths["stdout_path"]).read_text(), "hello\n")
            self.assertEqual(Path(paths["stderr_path"]).read_text(), "warn\n")
            store.append_measurement(run_id, {"node_id": "node-1", "verdict": "PASS"})
            store.append_measurement(run_id, {"node_id": "node-2", "verdict": "FAIL"})
            records = store.read_measurements(run_id)
            self.assertEqual([r["verdict"] for r in records], ["PASS", "FAIL"])
            self.assertEqual(store.read_measurements("no-such-run"), [])

    def test_gate_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id = new_run_id()
            store.create_run(run_id, {"objective": "x"})
            packet = {"packet_id": "gate-1", "action": "flash", "status": "pending"}
            store.write_gate(run_id, packet)
            self.assertEqual(store.read_gate(run_id, "gate-1"), packet)
            self.assertIsNone(store.read_gate(run_id, "gate-nope"))

    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id = new_run_id()
            store.create_run(run_id, {"objective": "x"})
            path = store.write_summary(run_id, "# hello\n")
            self.assertEqual(path.read_text(), "# hello\n")


if __name__ == "__main__":
    unittest.main()
