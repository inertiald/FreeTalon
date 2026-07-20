from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from freetalon.config import HiveConfig
from freetalon.hardware import HostCapabilities


def _host(
    *,
    gpu_count: int = 0,
    rdma_available: bool = False,
    nccl_available: bool = False,
) -> HostCapabilities:
    return HostCapabilities(
        cpu_count=8,
        memory_mib=8192,
        gpu_available=gpu_count > 0,
        acceleration_libs=(),
        rdma_available=rdma_available,
        nccl_available=nccl_available,
        gpu_count=gpu_count,
    )


class ConfigDefaultsTests(unittest.TestCase):
    def test_distributed_defaults(self) -> None:
        cfg = HiveConfig()
        self.assertEqual(cfg.topology, "star")
        self.assertEqual(cfg.transport, "tcp")
        self.assertEqual(cfg.tensor_parallel_size, 1)
        self.assertEqual(cfg.pipeline_parallel_size, 1)
        self.assertEqual(cfg.data_parallel_size, 1)
        self.assertEqual(cfg.nccl_socket_ifname, "lo")
        self.assertFalse(cfg.nccl_debug)
        self.assertEqual(cfg.deepspeed_zero_stage, 0)
        self.assertEqual(cfg.vllm_max_model_len, 4096)
        self.assertEqual(cfg.vllm_dtype, "auto")

    def test_world_size_property(self) -> None:
        cfg = HiveConfig(
            tensor_parallel_size=2,
            pipeline_parallel_size=3,
            data_parallel_size=4,
        )
        self.assertEqual(cfg.world_size, 24)


class ConfigFieldValidationTests(unittest.TestCase):
    def test_blank_ifname_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            HiveConfig(nccl_socket_ifname="   ")

    def test_ifname_is_stripped(self) -> None:
        cfg = HiveConfig(nccl_socket_ifname="  eth0  ")
        self.assertEqual(cfg.nccl_socket_ifname, "eth0")

    def test_invalid_transport_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            HiveConfig(transport="rocev2")

    def test_deepspeed_zero_stage_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            HiveConfig(deepspeed_zero_stage=4)
        with self.assertRaises(ValidationError):
            HiveConfig(deepspeed_zero_stage=-1)

    def test_vllm_max_model_len_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            HiveConfig(vllm_max_model_len=0)

    def test_vllm_dtype_choices(self) -> None:
        with self.assertRaises(ValidationError):
            HiveConfig(vllm_dtype="float8")
        self.assertEqual(HiveConfig(vllm_dtype="bfloat16").vllm_dtype, "bfloat16")


class ValidateAgainstHostTests(unittest.TestCase):
    def _config(self, base: Path, **kwargs: object) -> HiveConfig:
        return HiveConfig(
            workspace=base,
            state_path=base / "state.json",
            audit_log_path=base / "audit.log",
            **kwargs,
        )

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_cpu_only_host_passes_defaults(self) -> None:
        cfg = self._config(self.base)
        # gpu_count == 0 means detection unavailable; must not block CPU flows.
        cfg.validate_against_host(_host(gpu_count=0))

    def test_world_size_within_gpu_count_passes(self) -> None:
        cfg = self._config(
            self.base, tensor_parallel_size=2, data_parallel_size=2
        )
        cfg.validate_against_host(_host(gpu_count=4, nccl_available=True))

    def test_world_size_exceeds_gpu_count_raises(self) -> None:
        cfg = self._config(
            self.base, tensor_parallel_size=4, data_parallel_size=2
        )
        with self.assertRaises(ValueError):
            cfg.validate_against_host(_host(gpu_count=4, nccl_available=True))

    def test_rdma_transport_without_hardware_raises(self) -> None:
        cfg = self._config(self.base, transport="rdma")
        with self.assertRaises(ValueError):
            cfg.validate_against_host(_host(gpu_count=0, rdma_available=False))

    def test_rdma_transport_with_hardware_passes(self) -> None:
        cfg = self._config(self.base, transport="rdma")
        cfg.validate_against_host(_host(gpu_count=0, rdma_available=True))

    def test_multi_gpu_without_nccl_raises(self) -> None:
        cfg = self._config(self.base, tensor_parallel_size=2)
        with self.assertRaises(ValueError):
            cfg.validate_against_host(_host(gpu_count=4, nccl_available=False))

    def test_deepspeed_zero_without_nccl_raises(self) -> None:
        cfg = self._config(self.base, deepspeed_zero_stage=2)
        with self.assertRaises(ValueError):
            cfg.validate_against_host(_host(gpu_count=2, nccl_available=False))

    def test_deepspeed_zero_with_nccl_passes(self) -> None:
        cfg = self._config(self.base, deepspeed_zero_stage=2)
        cfg.validate_against_host(_host(gpu_count=2, nccl_available=True))


if __name__ == "__main__":
    unittest.main()
