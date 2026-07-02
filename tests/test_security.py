from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
