from __future__ import annotations

import unittest

from freetalon.hardware import HostCapabilities, adaptive_tuning


class HardwareTests(unittest.TestCase):
    def test_adaptive_tuning_respects_cpu_and_memory(self) -> None:
        caps = HostCapabilities(cpu_count=16, memory_mib=2048, gpu_available=False, acceleration_libs=())
        tuning = adaptive_tuning(caps, worker_cap=10, queue_multiplier=4)
        self.assertEqual(tuning.worker_count, 2)
        self.assertEqual(tuning.max_queue_size, 8)

    def test_adaptive_tuning_never_zero(self) -> None:
        caps = HostCapabilities(cpu_count=1, memory_mib=256, gpu_available=False, acceleration_libs=())
        tuning = adaptive_tuning(caps, worker_cap=8, queue_multiplier=6)
        self.assertEqual(tuning.worker_count, 1)
        self.assertEqual(tuning.max_queue_size, 6)


if __name__ == "__main__":
    unittest.main()
