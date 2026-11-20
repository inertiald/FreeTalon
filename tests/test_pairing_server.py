"""Pairing HTTP tests — real HTTP against a live server instance.

Written against freetalon/pairing/CONTRACT.md: the section-6 poll
mapping (404 pending / 200 bundle exactly once / 410 gone), the
fail-closed operator bearer auth, and the no-oracle rule (an unknown
code polls exactly like a pending one).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from freetalon.pairing.server import make_server
from freetalon.pairing.store import PairingStore

CODE = "KESTREL-4821"
NONCE = "ab" * 16
MACHINE = "9f2c1e7ab4d84c0aa123456789abcdef"
TOKEN = "operator-token-TESTONLY"
BUNDLE = {"tailscale_auth_key": "tskey-auth-TESTONLY", "ssh_key": "ssh-ed25519 TEST"}


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class PairingServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()
        self.store = PairingStore(
            Path(self._tmp.name) / "store.json", ttl_seconds=600, now_fn=self.clock
        )
        self.httpd = make_server(self.store, TOKEN, host="127.0.0.1", port=0)
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    # -- tiny client -----------------------------------------------------

    def request(self, method, path, body=None, token=None, headers=None):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            self.base + path, data=data, headers=hdrs, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {"raw": raw}

    def register(self, code=CODE):
        return self.request(
            "POST",
            "/pairing/register",
            {"code": code, "nonce": NONCE, "machine_id": MACHINE,
             "hostname": "kestrel-01", "mac": "aa:bb:cc:dd:ee:ff"},
        )

    # -- the flow ----------------------------------------------------------

    def test_full_flow_over_http(self) -> None:
        status, view = self.register()
        self.assertEqual(status, 201)
        self.assertEqual(view["status"], "pending")

        status, again = self.register()
        self.assertEqual(status, 200)  # idempotent re-register

        status, body = self.request("GET", f"/pairing/{CODE}")
        self.assertEqual(status, 404)
        self.assertEqual(body["status"], "pending")
        self.assertNotIn("credentials", body)

        # Operator gate: no header, then wrong token, both refused and
        # the record is untouched.
        status, _ = self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE}
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE},
            token="wrong-token",
        )
        self.assertEqual(status, 401)
        status, body = self.request("GET", f"/pairing/{CODE}")
        self.assertEqual((status, body["status"]), (404, "pending"))

        status, view = self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE},
            token=TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(view["status"], "confirmed")
        self.assertNotIn("credentials", view)

        status, body = self.request("GET", f"/pairing/{CODE}")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "confirmed")
        self.assertEqual(body["credentials"], BUNDLE)

        status, body = self.request("GET", f"/pairing/{CODE}")
        self.assertEqual(status, 410)
        self.assertEqual(body["status"], "claimed")
        self.assertNotIn("credentials", body)

        status, body = self.request("GET", "/pairing", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual([r["status"] for r in body["records"]], ["claimed"])
        raw = json.dumps(body)
        self.assertNotIn("tskey-auth-TESTONLY", raw)
        self.assertNotIn(NONCE, raw)

    def test_unknown_code_polls_exactly_like_pending(self) -> None:
        self.register()
        pending = self.request("GET", f"/pairing/{CODE}")
        unknown = self.request("GET", "/pairing/MAPLE-0001")
        self.assertEqual(pending[0], 404)
        self.assertEqual(unknown[0], 404)
        self.assertEqual(unknown[1]["status"], pending[1]["status"])

    def test_expired_code_polls_410_without_credentials(self) -> None:
        self.register()
        self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE},
            token=TOKEN,
        )
        self.clock.advance(601)
        status, body = self.request("GET", f"/pairing/{CODE}")
        self.assertEqual(status, 410)
        self.assertEqual(body["status"], "expired")
        self.assertNotIn("credentials", body)

    # -- operator auth -------------------------------------------------------

    def test_operator_api_fails_closed_without_configured_token(self) -> None:
        httpd = make_server(self.store, None, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            req = urllib.request.Request(
                base + "/pairing",
                headers={"Authorization": "Bearer anything"},
                method="GET",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_list_requires_operator_token(self) -> None:
        status, _ = self.request("GET", "/pairing")
        self.assertEqual(status, 401)

    # -- error shapes ---------------------------------------------------------

    def test_register_validation_and_conflict(self) -> None:
        status, _ = self.request(
            "POST", "/pairing/register",
            {"code": "bad-code", "nonce": NONCE, "machine_id": MACHINE},
        )
        self.assertEqual(status, 400)
        self.register()
        status, _ = self.request(
            "POST", "/pairing/register",
            {"code": CODE, "nonce": "cd" * 16, "machine_id": MACHINE},
        )
        self.assertEqual(status, 409)

    def test_confirm_error_shapes(self) -> None:
        status, _ = self.request(
            "POST", "/pairing/MAPLE-0001/confirm", {"credentials": BUNDLE},
            token=TOKEN,
        )
        self.assertEqual(status, 404)
        self.register()
        status, _ = self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": {}},
            token=TOKEN,
        )
        self.assertEqual(status, 400)
        self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE},
            token=TOKEN,
        )
        status, _ = self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE},
            token=TOKEN,
        )
        self.assertEqual(status, 409)
        self.request("GET", f"/pairing/{CODE}")  # claim it
        status, body = self.request(
            "POST", f"/pairing/{CODE}/confirm", {"credentials": BUNDLE},
            token=TOKEN,
        )
        self.assertEqual(status, 410)
        self.assertEqual(body["status"], "claimed")

    def test_malformed_bodies_and_routes(self) -> None:
        req = urllib.request.Request(
            self.base + "/pairing/register",
            data=b"{ not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        status, _ = self.request("PUT", "/pairing", {"x": 1}, token=TOKEN)
        self.assertEqual(status, 405)

    def test_wrong_method_routing(self) -> None:
        # CONTRACT.md: an unknown route is 404 whatever the method; a
        # known path with the wrong method is 405 -- never the stdlib
        # default 501.
        status, _ = self.request("PUT", "/nope", {"x": 1}, token=TOKEN)
        self.assertEqual(status, 404)
        status, _ = self.request("DELETE", "/nope")
        self.assertEqual(status, 404)
        status, _ = self.request("PATCH", "/nope", {"x": 1}, token=TOKEN)
        self.assertEqual(status, 404)
        status, _ = self.request("PATCH", "/pairing", {"x": 1}, token=TOKEN)
        self.assertEqual(status, 405)
        status, _ = self.request("DELETE", f"/pairing/{CODE}")
        self.assertEqual(status, 405)
        status, _ = self.request("HEAD", "/pairing")
        self.assertEqual(status, 405)
        status, _ = self.request("POST", "/pairing", {"x": 1}, token=TOKEN)
        self.assertEqual(status, 405)
        status, _ = self.request(
            "POST", f"/pairing/{CODE}", {"x": 1}, token=TOKEN
        )
        self.assertEqual(status, 405)
        status, _ = self.request("GET", f"/pairing/{CODE}/confirm")
        self.assertEqual(status, 405)

    def test_non_ascii_bearer_is_401_not_a_dropped_connection(self) -> None:
        # hmac.compare_digest raises TypeError on non-ASCII str input;
        # unguarded, that killed the handler thread and the client saw
        # a dropped connection instead of the contract's one 401.
        weird = "Bearer t" + chr(0xF6) + "k" + chr(0xE9) + "n"
        status, body = self.request(
            "GET", "/pairing", token=None, headers={"Authorization": weird}
        )
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})
        # The server is unharmed: a correct request still works.
        status, _ = self.request("GET", "/pairing", token=TOKEN)
        self.assertEqual(status, 200)

    def test_oversized_body_is_413(self) -> None:
        status, _ = self.request(
            "POST", "/pairing/register",
            {"code": CODE, "nonce": NONCE, "machine_id": MACHINE,
             "hostname": "x" * 70000},
        )
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
