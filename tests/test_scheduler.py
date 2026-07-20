from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from freetalon.config import HiveConfig
from freetalon.hardware import HostCapabilities
from freetalon.hive import HiveController


def _cpu_only_host() -> HostCapabilities:
    return HostCapabilities(
        cpu_count=4,
        memory_mib=4096,
        gpu_available=False,
        acceleration_libs=(),
        rdma_available=False,
        nccl_available=False,
        gpu_count=0,
    )


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
        self.controller = HiveController(self.config, host=_cpu_only_host())
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


if __name__ == "__main__":
    unittest.main()
