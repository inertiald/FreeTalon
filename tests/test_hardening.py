"""Tests for the mechanisms mined from copilot/production-grade-hardening.

Triage execution 2026-11-19: the branch's three mechanisms were
genuinely absent from the current tree (hive retry was the linear
backoff_seconds * attempt; the API had no request IDs and read request
bodies unbounded), so they were implemented fresh in current style:

- exponential retry backoff with a cap and full jitter (hive),
- per-request IDs bound to the audit log + X-Request-ID header (api),
- a request body size cap with a 413 (api).
"""

from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from freetalon.api import HiveAPIServer
from freetalon.orchestrator.gates import GateStore
from freetalon.audit import (
    AuditLogger,
    clear_request_id,
    current_request_id,
    set_request_id,
)
from freetalon.config import HiveConfig
from freetalon.hive import HiveController, _retry_delay_seconds

_TOKEN = "hardening" + "-test-token"


class RetryDelayTests(unittest.TestCase):
    def test_exponential_schedule_without_jitter(self) -> None:
        config = HiveConfig(retry_jitter=False, max_backoff_seconds=60.0)
        self.assertEqual(_retry_delay_seconds(config, 0.2, 1), 0.2)
        self.assertEqual(_retry_delay_seconds(config, 0.2, 2), 0.4)
        self.assertEqual(_retry_delay_seconds(config, 0.2, 3), 0.8)
        self.assertEqual(_retry_delay_seconds(config, 0.2, 4), 1.6)

    def test_delay_is_capped(self) -> None:
        config = HiveConfig(retry_jitter=False, max_backoff_seconds=60.0)
        self.assertEqual(_retry_delay_seconds(config, 100.0, 1), 60.0)
        self.assertEqual(_retry_delay_seconds(config, 0.2, 20), 60.0)

    def test_jitter_draws_within_the_cap(self) -> None:
        config = HiveConfig(retry_jitter=True, max_backoff_seconds=60.0)
        samples = {_retry_delay_seconds(config, 0.2, 3) for _ in range(200)}
        self.assertTrue(all(0.0 <= s <= 0.8 for s in samples))
        # Full jitter actually spreads the draws (not a constant).
        self.assertGreater(len(samples), 100)

    def test_config_defaults_and_bounds(self) -> None:
        config = HiveConfig()
        self.assertEqual(config.max_backoff_seconds, 60.0)
        self.assertTrue(config.retry_jitter)
        self.assertEqual(config.max_request_body_bytes, 65536)
        with self.assertRaises(ValidationError):
            HiveConfig(max_request_body_bytes=100)
        with self.assertRaises(ValidationError):
            HiveConfig(max_backoff_seconds=0.5)


class RequestIdAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_request_id()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "audit.log"
        self.logger = AuditLogger(self.path)

    def tearDown(self) -> None:
        clear_request_id()
        self.tmpdir.cleanup()

    def _entries(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_no_request_id_by_default(self) -> None:
        self.logger.log("plain.event")
        (entry,) = self._entries()
        self.assertNotIn("request_id", entry)

    def test_request_id_is_stamped_and_cleared(self) -> None:
        set_request_id("abc123def456")
        self.assertEqual(current_request_id(), "abc123def456")
        self.logger.log("bound.event")
        clear_request_id()
        self.assertIsNone(current_request_id())
        self.logger.log("after.clear")
        bound, after = self._entries()
        self.assertEqual(bound["request_id"], "abc123def456")
        self.assertNotIn("request_id", after)

    def test_request_id_is_thread_local(self) -> None:
        set_request_id("main-thread-id")

        def worker() -> None:
            self.logger.log("worker.event")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        (entry,) = self._entries()
        self.assertNotIn("request_id", entry)


class APIServerTests(unittest.TestCase):
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
            max_request_body_bytes=256,
        )
        self.controller = HiveController(self.config)
        self.controller.start()
        self.server = HiveAPIServer(
            self.controller, _TOKEN, gate_store=GateStore(base / "freetalon_state.db")
        )
        self.server.start("127.0.0.1", 0)
        self.port = self.server._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.controller.stop()
        self.tmpdir.cleanup()

    def _request(
        self, method: str, path: str, body: bytes | None = None, token: str = _TOKEN
    ) -> tuple[int, dict, dict]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Authorization", "Bearer " + token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read())

    def test_responses_carry_a_request_id(self) -> None:
        status, headers, _ = self._request("GET", "/tasks")
        self.assertEqual(status, 200)
        rid = headers.get("X-Request-ID")
        self.assertIsNotNone(rid)
        self.assertRegex(rid, re.compile(r"^[0-9a-f]{12}$"))
        _, headers2, _ = self._request("GET", "/tasks")
        self.assertNotEqual(headers2.get("X-Request-ID"), rid)

    def test_audit_events_inherit_the_request_id(self) -> None:
        status, headers, _ = self._request("GET", "/tasks", token="wrong-token")
        self.assertEqual(status, 401)
        rid = headers.get("X-Request-ID")
        self.assertIsNotNone(rid)
        audit_path = self.config.audit_log_path
        entries = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        denied = [e for e in entries if e["event"] == "auth.denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["request_id"], rid)

    def test_body_over_the_cap_is_413_and_creates_nothing(self) -> None:
        payload = json.dumps({"action": "echo", "text": "x" * 300}).encode()
        self.assertGreater(len(payload), 256)
        status, _, body = self._request("POST", "/tasks", body=payload)
        self.assertEqual(status, 413)
        self.assertIn("exceeds 256 bytes", body["error"])
        status, _, listing = self._request("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(listing["count"], 0)

    def test_body_under_the_cap_still_works(self) -> None:
        payload = json.dumps({"action": "echo", "text": "hi"}).encode()
        status, _, body = self._request("POST", "/tasks", body=payload)
        self.assertEqual(status, 201)
        self.assertIn("task_id", body)

    def test_malformed_content_length_is_400(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3.0)
        conn.putrequest("POST", "/tasks")
        conn.putheader("Authorization", "Bearer " + _TOKEN)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "not-a-number")
        conn.endheaders()
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("Invalid Content-Length", body["error"])


if __name__ == "__main__":
    unittest.main()
