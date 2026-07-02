#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freetalon.config import HiveConfig
from freetalon.hive import HiveController


def run_benchmark(task_count: int, sleep_seconds: float, worker_cap: int) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        config = HiveConfig(
            workspace=base,
            state_path=base / "state.json",
            audit_log_path=base / "audit.log",
            worker_cap=worker_cap,
            queue_multiplier=task_count,
            poll_interval_seconds=0.01,
        )
        controller = HiveController(config)
        controller.start()
        try:
            task_ids = []
            for _ in range(task_count):
                task = controller.submit_task({"action": "sleep", "duration": sleep_seconds})
                task_ids.append(task.task_id)

            start = time.perf_counter()
            while True:
                snapshots = [controller.get_task(task_id) for task_id in task_ids]
                if any(s is None for s in snapshots):
                    raise RuntimeError("Task disappeared during benchmark run")
                statuses = [s["status"] for s in snapshots if s is not None]
                if all(s == "succeeded" for s in statuses):
                    break
                time.sleep(0.01)
            end = time.perf_counter()
            return end - start
        finally:
            controller.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=0.05)
    args = parser.parse_args()

    serial = run_benchmark(args.tasks, args.sleep, worker_cap=1)
    adaptive = run_benchmark(args.tasks, args.sleep, worker_cap=8)
    print(f"serial_seconds={serial:.4f}")
    print(f"adaptive_seconds={adaptive:.4f}")
    print(f"speedup={serial / adaptive:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
