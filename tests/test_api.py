"""Tests for the new API endpoints: GET /tasks, GET /tasks/{id}, GET /resources."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
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


def _get_with_response(url: str, token: str = _TOKEN, timeout: float = 3.0):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    return urllib.request.urlopen(req, timeout=timeout)


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

    # ── request ID header ────────────────────────────────────────────────

    def test_x_request_id_present_on_health(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
            self.assertIn("X-Request-ID", resp.headers)
            rid = resp.headers["X-Request-ID"]
        self.assertEqual(len(rid), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in rid))

    def test_x_request_id_present_on_authenticated_endpoint(self) -> None:
        with _get_with_response(f"{self.base}/tasks") as resp:
            self.assertIn("X-Request-ID", resp.headers)

    def test_x_request_id_unique_per_request(self) -> None:
        ids = set()
        for _ in range(5):
            with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
                ids.add(resp.headers["X-Request-ID"])
        self.assertEqual(len(ids), 5)

    # ── /health/ready ────────────────────────────────────────────────────

    def test_health_ready_returns_200_when_started(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health/ready", timeout=3) as resp:
            data = json.loads(resp.read())
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "ready")

    # ── structured error codes ───────────────────────────────────────────

    def test_error_response_has_error_code(self) -> None:
        req = urllib.request.Request(f"{self.base}/tasks")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        body = json.loads(cm.exception.read())
        self.assertFalse(body["ok"])
        self.assertIn("error_code", body)
        self.assertEqual(body["error_code"], "UNAUTHORIZED")

    def test_invalid_payload_returns_error_code(self) -> None:
        data = json.dumps({"action": "echo", "text": "!!!"}).encode()
        req = urllib.request.Request(f"{self.base}/tasks", data=data, method="POST")
        req.add_header("Authorization", "Bearer " + _TOKEN)
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        body = json.loads(cm.exception.read())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "INVALID_PAYLOAD")

    def test_not_found_returns_error_code(self) -> None:
        req = urllib.request.Request(f"{self.base}/does-not-exist")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        body = json.loads(cm.exception.read())
        self.assertEqual(body["error_code"], "NOT_FOUND")

    # ── body size limit ──────────────────────────────────────────────────

    def test_oversized_body_rejected(self) -> None:
        big = json.dumps({"action": "echo", "text": "x" * 70000}).encode()
        req = urllib.request.Request(f"{self.base}/tasks", data=big, method="POST")
        req.add_header("Authorization", "Bearer " + _TOKEN)
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 413)
        body = json.loads(cm.exception.read())
        self.assertEqual(body["error_code"], "PAYLOAD_TOO_LARGE")

    # ── task ID validation ───────────────────────────────────────────────

    def test_invalid_task_id_format_returns_404(self) -> None:
        req = urllib.request.Request(f"{self.base}/tasks/../secrets")
        req.add_header("Authorization", "Bearer " + _TOKEN)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 404)

    def test_too_long_task_id_returns_404(self) -> None:
        req = urllib.request.Request(f"{self.base}/tasks/{'a' * 64}")
        req.add_header("Authorization", "Bearer " + _TOKEN)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 404)

    # ── audit log request_id correlation ────────────────────────────────

    def test_audit_log_has_request_id(self) -> None:
        _post(f"{self.base}/tasks", payload={"action": "echo", "text": "audit-check"})
        time.sleep(0.1)
        audit_text = (Path(self.tmpdir.name) / "audit.log").read_text(encoding="utf-8")
        lines = [json.loads(l) for l in audit_text.strip().splitlines() if l]
        submission_events = [e for e in lines if e.get("event") == "task.submitted"]
        self.assertTrue(len(submission_events) > 0)
        self.assertIn("request_id", submission_events[0])


if __name__ == "__main__":
    unittest.main()
