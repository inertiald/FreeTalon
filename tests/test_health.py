"""Tests for the GET /health endpoint."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from freetalon.api import HiveAPIServer
from freetalon.config import HiveConfig
from freetalon.hive import HiveController

_TOKEN = "test-api-token-abc"


class HealthEndpointTests(unittest.TestCase):
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
        self.controller.start()
        self.server = HiveAPIServer(self.controller, _TOKEN)
        self.server.start("127.0.0.1", 0)
        port = self.server._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.controller.stop()
        self.tmpdir.cleanup()

    def test_health_returns_ok(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
            data = json.loads(resp.read())
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "ready")

    def test_health_has_ollama_key(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
            data = json.loads(resp.read())
        self.assertIn("ollama_reachable", data)
        self.assertIsInstance(data["ollama_reachable"], bool)

    def test_health_has_gpu_key(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
            data = json.loads(resp.read())
        self.assertIn("gpu_temperature", data)


if __name__ == "__main__":
    unittest.main()
