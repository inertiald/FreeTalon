"""Tests for the new API endpoints: GET /tasks, GET /tasks/{id}, GET /resources."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from freetalon.api import HiveAPIServer
from freetalon.config import HiveConfig
from freetalon.hive import HiveController

_TOKEN = "test-api-token-abc"


def _get(url: str, token: str = _TOKEN, timeout: float = 3.0) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post(url: str, token: str = _TOKEN, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


class APIEndpointTests(unittest.TestCase):
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

    # ── /tasks (list) ────────────────────────────────────────────────────

    def test_list_tasks_empty(self) -> None:
        resp = _get(f"{self.base}/tasks")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["tasks"], [])
        self.assertEqual(resp["count"], 0)

    def test_list_tasks_returns_submitted(self) -> None:
        _post(f"{self.base}/tasks", payload={"action": "echo", "text": "hello"})
        resp = _get(f"{self.base}/tasks")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["count"], 1)
        self.assertEqual(resp["tasks"][0]["payload"]["action"], "echo")

    def test_list_tasks_status_filter(self) -> None:
        _post(f"{self.base}/tasks", payload={"action": "echo", "text": "hi"})
        deadline = time.time() + 2
        while time.time() < deadline:
            resp = _get(f"{self.base}/tasks?status=succeeded")
            if resp["count"] == 1:
                break
            time.sleep(0.05)
        self.assertEqual(resp["count"], 1)
        self.assertEqual(resp["tasks"][0]["status"], "succeeded")

    def test_list_tasks_status_filter_empty(self) -> None:
        _post(f"{self.base}/tasks", payload={"action": "echo", "text": "hi"})
        resp = _get(f"{self.base}/tasks?status=failed")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["count"], 0)

    def test_list_tasks_requires_auth(self) -> None:
        req = urllib.request.Request(f"{self.base}/tasks")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 401)

    # ── /tasks/{id} (single) ─────────────────────────────────────────────

    def test_get_task_by_id(self) -> None:
        submit_resp = _post(f"{self.base}/tasks", payload={"action": "echo", "text": "find-me"})
        task_id = submit_resp["task_id"]
        resp = _get(f"{self.base}/tasks/{task_id}")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["task"]["task_id"], task_id)

    def test_get_task_not_found(self) -> None:
        req = urllib.request.Request(f"{self.base}/tasks/nonexistent")
        req.add_header("Authorization", "Bearer " + _TOKEN)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 404)

    def test_get_task_requires_auth(self) -> None:
        submit_resp = _post(f"{self.base}/tasks", payload={"action": "echo", "text": "x"})
        task_id = submit_resp["task_id"]
        req = urllib.request.Request(f"{self.base}/tasks/{task_id}")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 401)

    # ── /resources ───────────────────────────────────────────────────────

    def test_get_resources(self) -> None:
        resp = _get(f"{self.base}/resources")
        self.assertTrue(resp["ok"])
        self.assertIn("resources", resp)

    def test_get_resources_requires_auth(self) -> None:
        req = urllib.request.Request(f"{self.base}/resources")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 401)

    # ── existing endpoints still work ────────────────────────────────────

    def test_health_still_works(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
            data = json.loads(resp.read())
        self.assertTrue(data["ok"])

    def test_metrics_still_works(self) -> None:
        resp = _get(f"{self.base}/metrics")
        self.assertTrue(resp["ok"])
        self.assertIn("metrics", resp)


if __name__ == "__main__":
    unittest.main()
