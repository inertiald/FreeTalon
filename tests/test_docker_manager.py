"""Tests for DockerManager and docker_claw security validation."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch


class DockerManagerUnavailableTests(unittest.TestCase):
    """DockerManager raises clearly when docker SDK is not installed."""

    def test_raises_when_sdk_missing(self) -> None:
        import freetalon.docker_manager as dm_mod
        original = dm_mod._DOCKER_SDK
        try:
            dm_mod._DOCKER_SDK = False
            with self.assertRaises(RuntimeError) as cm:
                dm_mod.DockerManager()
            self.assertIn("docker SDK not installed", str(cm.exception))
        finally:
            dm_mod._DOCKER_SDK = original

    def test_raises_when_daemon_unreachable(self) -> None:
        import freetalon.docker_manager as dm_mod
        mock_docker = MagicMock()
        mock_docker.from_env.return_value.ping.side_effect = Exception("connection refused")
        original_sdk = dm_mod._DOCKER_SDK
        original_docker = dm_mod._docker
        try:
            dm_mod._DOCKER_SDK = True
            dm_mod._docker = mock_docker
            with self.assertRaises(RuntimeError) as cm:
                dm_mod.DockerManager()
            self.assertIn("Docker daemon unreachable", str(cm.exception))
        finally:
            dm_mod._DOCKER_SDK = original_sdk
            dm_mod._docker = original_docker


class DockerManagerLifecycleTests(unittest.TestCase):
    """DockerManager container lifecycle with fully mocked docker SDK."""

    def _make_manager(self) -> "tuple[object, MagicMock]":
        mock_docker = MagicMock()
        mock_errors = MagicMock()
        mock_errors.NotFound = type("NotFound", (Exception,), {})

        fake_container = MagicMock()
        fake_container.short_id = "abc123"
        fake_container.status = "exited"
        fake_container.wait.return_value = {"StatusCode": 0}
        fake_container.logs.return_value = b"hello output\n"

        mock_docker.from_env.return_value.ping.return_value = True
        mock_docker.from_env.return_value.containers.run.return_value = fake_container
        mock_docker.from_env.return_value.networks.get.side_effect = mock_errors.NotFound("no net")

        with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_errors}):
            import importlib
            import freetalon.docker_manager as dm_mod
            dm_mod._DOCKER_SDK = True
            dm_mod._docker = mock_docker
            dm_mod._NotFound = mock_errors.NotFound
            importlib.reload(dm_mod)
            manager = dm_mod.DockerManager.__new__(dm_mod.DockerManager)
            manager._client = mock_docker.from_env()
            manager._rm = None
            manager._lock = __import__("threading").Lock()
            manager._containers = {}
            # Patch _ensure_network to avoid real docker call
            manager._ensure_network = lambda: "freetalon-claw-net"  # type: ignore[method-assign]

        return manager, fake_container

    def test_spawn_returns_short_id(self) -> None:
        manager, fake_container = self._make_manager()
        cid = manager.spawn_claw("task001", "print('hi')")
        self.assertEqual(cid, "abc123")
        self.assertIn("task001", manager._containers)

    def test_collect_result_exit_code_and_output(self) -> None:
        manager, fake_container = self._make_manager()
        manager._containers["task001"] = fake_container
        result = manager.collect_result("task001")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello output", result["output"])

    def test_kill_claw_missing_task_is_noop(self) -> None:
        manager, _ = self._make_manager()
        manager.kill_claw("nonexistent")  # must not raise

    def test_remove_claw_cleans_up(self) -> None:
        manager, fake_container = self._make_manager()
        manager._containers["task002"] = fake_container
        manager.remove_claw("task002")
        self.assertNotIn("task002", manager._containers)
        fake_container.remove.assert_called_once_with(force=True)

    def test_claw_status_missing_returns_missing(self) -> None:
        manager, _ = self._make_manager()
        self.assertEqual(manager.claw_status("no-such"), "missing")

    def test_resources_summary_without_rm(self) -> None:
        manager, _ = self._make_manager()
        manager._rm = None
        summary = manager.resources_summary()
        self.assertIn("resource_manager", summary)


class DockerClawSecurityTests(unittest.TestCase):
    """sanitize_payload rejects invalid docker_claw payloads."""

    def setUp(self) -> None:
        from freetalon.security import sanitize_payload
        self.sanitize = sanitize_payload

    def _valid(self) -> dict:
        return {"action": "docker_claw", "code": "print('hello')"}

    def test_valid_docker_claw_accepted(self) -> None:
        result = self.sanitize(self._valid())
        self.assertEqual(result["action"], "docker_claw")
        self.assertEqual(result["code"], "print('hello')")
        self.assertEqual(result["profile"], "default")

    def test_empty_code_rejected(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.sanitize({"action": "docker_claw", "code": ""})
        self.assertIn("non-empty", str(cm.exception))

    def test_code_too_long_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.sanitize({"action": "docker_claw", "code": "x" * 4097})

    def test_invalid_profile_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.sanitize({"action": "docker_claw", "code": "x", "profile": "admin"})

    def test_valid_profiles_accepted(self) -> None:
        for profile in ("default", "video", "youtube_upload"):
            result = self.sanitize({"action": "docker_claw", "code": "x", "profile": profile})
            self.assertEqual(result["profile"], profile)

    def test_timeout_bounds(self) -> None:
        with self.assertRaises(ValueError):
            self.sanitize({"action": "docker_claw", "code": "x", "timeout": 0})
        with self.assertRaises(ValueError):
            self.sanitize({"action": "docker_claw", "code": "x", "timeout": 301})
        result = self.sanitize({"action": "docker_claw", "code": "x", "timeout": 60})
        self.assertEqual(result["timeout"], 60.0)

    def test_code_with_newlines_accepted(self) -> None:
        code = "import os\nprint(os.getcwd())\n"
        result = self.sanitize({"action": "docker_claw", "code": code})
        self.assertIn("import os", result["code"])
        self.assertIn("print(os.getcwd())", result["code"])

    def test_null_bytes_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.sanitize({"action": "docker_claw", "code": "x\x00y"})


class NewConfigFieldsTests(unittest.TestCase):
    """HiveConfig accepts ADR 0002 topology/parallelism fields."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from freetalon.config import HiveConfig
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.base_kwargs = dict(
            workspace=base,
            state_path=base / "state.json",
            audit_log_path=base / "audit.log",
        )
        self.HiveConfig = HiveConfig

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_defaults(self) -> None:
        cfg = self.HiveConfig(**self.base_kwargs)
        self.assertEqual(cfg.topology, "star")
        self.assertEqual(cfg.transport, "tcp")
        self.assertEqual(cfg.tensor_parallel_size, 1)
        self.assertEqual(cfg.pipeline_parallel_size, 1)
        self.assertEqual(cfg.data_parallel_size, 1)
        self.assertEqual(cfg.nccl_socket_ifname, "lo")
        self.assertFalse(cfg.nccl_debug)

    def test_ring_topology(self) -> None:
        cfg = self.HiveConfig(**self.base_kwargs, topology="ring")
        self.assertEqual(cfg.topology, "ring")

    def test_rdma_transport(self) -> None:
        cfg = self.HiveConfig(**self.base_kwargs, transport="rdma")
        self.assertEqual(cfg.transport, "rdma")

    def test_parallel_sizes(self) -> None:
        cfg = self.HiveConfig(
            **self.base_kwargs,
            tensor_parallel_size=4,
            pipeline_parallel_size=2,
            data_parallel_size=8,
        )
        self.assertEqual(cfg.tensor_parallel_size, 4)
        self.assertEqual(cfg.pipeline_parallel_size, 2)
        self.assertEqual(cfg.data_parallel_size, 8)

    def test_invalid_topology_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.HiveConfig(**self.base_kwargs, topology="mesh")

    def test_invalid_transport_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.HiveConfig(**self.base_kwargs, transport="infiniband")


class HardwareRdmaNcclTests(unittest.TestCase):
    """Hardware detection includes rdma_available and nccl_available."""

    def test_capabilities_has_rdma_field(self) -> None:
        from freetalon.hardware import detect_host_capabilities
        caps = detect_host_capabilities()
        self.assertIsInstance(caps.rdma_available, bool)

    def test_capabilities_has_nccl_field(self) -> None:
        from freetalon.hardware import detect_host_capabilities
        caps = detect_host_capabilities()
        self.assertIsInstance(caps.nccl_available, bool)

    def test_detect_rdma_false_when_tools_absent(self) -> None:
        from freetalon.hardware import _detect_rdma
        with patch("shutil.which", return_value=None), \
             patch("subprocess.run", return_value=MagicMock(stdout="")):
            result = _detect_rdma()
        self.assertFalse(result)

    def test_detect_rdma_true_when_ibstat_present(self) -> None:
        from freetalon.hardware import _detect_rdma
        original = __import__("shutil").which

        def mock_which(tool: str) -> str | None:
            return "/usr/sbin/ibstat" if tool == "ibstat" else None

        with patch("shutil.which", side_effect=mock_which):
            result = _detect_rdma()
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
