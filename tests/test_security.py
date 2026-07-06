from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from freetalon.audit import AuditLogger, clear_request_id, set_request_id
from freetalon.security import authorize, redact_secret, sanitize_payload


class SecurityTests(unittest.TestCase):
    def test_sanitize_rejects_bad_action(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_payload({"action": "exec", "text": "rm -rf /"})

    def test_sanitize_accepts_echo(self) -> None:
        payload = sanitize_payload({"action": "echo", "text": "hello_123"})
        self.assertEqual(payload["action"], "echo")
        self.assertEqual(payload["text"], "hello_123")

    def test_authorize_constant_time_compare(self) -> None:
        self.assertTrue(authorize("abc", "abc"))
        self.assertFalse(authorize("abc", "def"))
        self.assertFalse(authorize(None, "abc"))

    def test_redaction_obfuscates_secret(self) -> None:
        redacted = redact_secret("super-secret-token")
        self.assertTrue(redacted.startswith("redacted:"))
        self.assertNotIn("super-secret-token", redacted)


class AuditLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmpdir.name) / "audit.log"
        self.logger = AuditLogger(path=self.log_path)

    def tearDown(self) -> None:
        clear_request_id()
        self.tmpdir.cleanup()

    def _read_entries(self) -> list[dict]:
        return [json.loads(l) for l in self.log_path.read_text().splitlines() if l]

    def test_log_writes_event_and_timestamp(self) -> None:
        self.logger.log("test.event", key="value")
        entries = self._read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "test.event")
        self.assertEqual(entries[0]["key"], "value")
        self.assertIn("ts", entries[0])

    def test_log_includes_request_id_when_set(self) -> None:
        set_request_id("abc123def456")
        self.logger.log("test.event")
        entries = self._read_entries()
        self.assertEqual(entries[0]["request_id"], "abc123def456")

    def test_log_omits_request_id_when_not_set(self) -> None:
        clear_request_id()
        self.logger.log("test.event")
        entries = self._read_entries()
        self.assertNotIn("request_id", entries[0])

    def test_request_id_is_thread_local(self) -> None:
        import threading

        results = {}

        def _thread_a() -> None:
            set_request_id("aaa111aaa111")
            import time
            time.sleep(0.05)
            self.logger.log("thread.a")

        def _thread_b() -> None:
            clear_request_id()
            import time
            time.sleep(0.02)
            self.logger.log("thread.b")

        ta = threading.Thread(target=_thread_a)
        tb = threading.Thread(target=_thread_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        entries = self._read_entries()
        a_entries = [e for e in entries if e["event"] == "thread.a"]
        b_entries = [e for e in entries if e["event"] == "thread.b"]
        self.assertEqual(a_entries[0]["request_id"], "aaa111aaa111")
        self.assertNotIn("request_id", b_entries[0])


if __name__ == "__main__":
    unittest.main()
