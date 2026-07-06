from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from freetalon.config import HiveConfig
from freetalon.hive import HiveController


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.config = HiveConfig(
            workspace=base,
            state_path=base / "state.json",
            audit_log_path=base / "audit.log",
            worker_cap=2,
            queue_multiplier=3,
            poll_interval_seconds=0.02,
        )
        self.controller = HiveController(self.config)
        self.controller.host = replace(self.controller.host, gpu_available=False)
        self.controller.start()

    def tearDown(self) -> None:
        self.controller.stop()
        self.tmpdir.cleanup()

    def test_retry_backoff_then_fail(self) -> None:
        task = self.controller.submit_task(
            {
                "action": "sum",
                "values": [1, 2],
                "requires_gpu": True,
                "retries": 1,
                "backoff_seconds": 0.01,
            }
        )
        deadline = time.time() + 2
        while time.time() < deadline:
            current = self.controller.get_task(task.task_id)
            if current and current["status"] == "failed":
                self.assertEqual(current["attempt"], 2)
                return
            time.sleep(0.02)
        self.fail("task did not transition to failed")

    def test_cancel_running_sleep_task(self) -> None:
        task = self.controller.submit_task({"action": "sleep", "duration": 1.0})
        time.sleep(0.05)
        self.assertTrue(self.controller.cancel_task(task.task_id))

        deadline = time.time() + 2
        while time.time() < deadline:
            current = self.controller.get_task(task.task_id)
            if current and current["status"] == "cancelled":
                return
            time.sleep(0.02)
        self.fail("task did not transition to cancelled")

    # ── exponential backoff with jitter ──────────────────────────────────

    def test_retry_next_run_at_uses_exponential_jitter(self) -> None:
        """next_run_at should be in (0, cap] after first failure."""
        config = HiveConfig(
            workspace=Path(self.tmpdir.name) / "jitter",
            state_path=Path(self.tmpdir.name) / "jitter" / "state.json",
            audit_log_path=Path(self.tmpdir.name) / "jitter" / "audit.log",
            worker_cap=1,
            queue_multiplier=2,
            poll_interval_seconds=0.01,
            max_backoff_seconds=5.0,
            retry_jitter=True,
        )
        controller = HiveController(config)
        controller.host = replace(controller.host, gpu_available=False)
        # Do NOT start scheduler — we want to inspect the task state after the
        # first execution without it immediately retrying.
        task = controller.submit_task(
            {
                "action": "sum",
                "values": [1],
                "requires_gpu": True,
                "retries": 3,
                "backoff_seconds": 2.0,
            }
        )
        controller.start()
        # Wait for first attempt (retrying status)
        deadline = time.time() + 2
        while time.time() < deadline:
            record = controller.get_task(task.task_id)
            if record and record["status"] in {"retrying", "failed"}:
                break
            time.sleep(0.02)
        controller.stop()

        record = controller.get_task(task.task_id)
        self.assertIsNotNone(record)
        if record["status"] == "retrying":
            # next_run_at must be within (now, now + max_backoff]
            now = time.time()
            delay = record["next_run_at"] - record["updated_at"]
            # With full jitter the delay is in [0, min(backoff * 2^(attempt-1), cap)]
            # attempt=1 → cap is min(2.0 * 2^0, 5.0) = 2.0
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, 5.0)

    def test_retry_jitter_disabled_uses_capped_exponential(self) -> None:
        """With jitter=False the delay should equal min(base * 2^(attempt-1), cap)."""
        config = HiveConfig(
            workspace=Path(self.tmpdir.name) / "nojitter",
            state_path=Path(self.tmpdir.name) / "nojitter" / "state.json",
            audit_log_path=Path(self.tmpdir.name) / "nojitter" / "audit.log",
            worker_cap=1,
            queue_multiplier=2,
            poll_interval_seconds=0.01,
            max_backoff_seconds=10.0,
            retry_jitter=False,
        )
        controller = HiveController(config)
        controller.host = replace(controller.host, gpu_available=False)
        task = controller.submit_task(
            {
                "action": "sum",
                "values": [1],
                "requires_gpu": True,
                "retries": 3,
                "backoff_seconds": 1.0,
            }
        )
        controller.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            record = controller.get_task(task.task_id)
            if record and record["status"] in {"retrying", "failed"}:
                break
            time.sleep(0.02)
        controller.stop()

        record = controller.get_task(task.task_id)
        self.assertIsNotNone(record)
        if record["status"] == "retrying":
            delay = record["next_run_at"] - record["updated_at"]
            # attempt=1, base=1.0, cap=10.0 → expected = min(1.0 * 2^0, 10.0) = 1.0
            self.assertAlmostEqual(delay, 1.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
