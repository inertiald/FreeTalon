"""Static fallthrough tests for the pairing HTTP layer.

Written against freetalon/pairing/CONTRACT.md, "Static fallthrough":
with a static root configured, GET/HEAD outside the /pairing namespace
serve files from the tree (traversal-proof, no listings), pairing
routes are matched first and unaffected, and with no root configured
every non-route path stays the API's JSON 404.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from freetalon.pairing.server import PairingServerConfig, make_server
from freetalon.pairing.store import PairingStore

TOKEN = "test-operator-token"
CODE = "KESTREL-4821"
NONCE = "ab" * 16
MACHINE = "9f2c1e7ab4d84c0aa123456789abcdef"
PAYLOAD = b"#!/bin/sh\necho firstboot\n"
INDEX = b"<h1>index</h1>\n"
SECRET = b"outside-the-root\n"


class StaticFallthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "www"
        (self.root / "firstboot").mkdir(parents=True)
        (self.root / "firstboot" / "firstboot.sh").write_bytes(PAYLOAD)
        (self.root / "with-index").mkdir()
        (self.root / "with-index" / "index.html").write_bytes(INDEX)
        (self.root / "blob.frobozz").write_bytes(b"\x00\x01\x02")
        (base / "secret.txt").write_bytes(SECRET)
        self.store = PairingStore(base / "store.json", ttl_seconds=600)
        self.httpd = make_server(
            self.store,
            TOKEN,
            host="127.0.0.1",
            port=0,
            static_root=str(self.root),
        )
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    # -- tiny raw client (exact request paths, no normalization) --------

    def fetch(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    # -- static serving ---------------------------------------------------

    def test_get_serves_file_bytes(self) -> None:
        status, headers, data = self.fetch("GET", "/firstboot/firstboot.sh")
        self.assertEqual(status, 200)
        self.assertEqual(data, PAYLOAD)
        self.assertEqual(int(headers["Content-Length"]), len(PAYLOAD))
        self.assertIn("Content-Type", headers)

    def test_head_serves_headers_without_body(self) -> None:
        status, headers, data = self.fetch("HEAD", "/firstboot/firstboot.sh")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(PAYLOAD))
        self.assertEqual(data, b"")

    def test_get_directory_with_index_serves_it(self) -> None:
        for path in ("/with-index", "/with-index/"):
            status, _, data = self.fetch("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(data, INDEX, path)

    def test_get_directory_without_index_is_404(self) -> None:
        status, _, data = self.fetch("GET", "/firstboot/")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data), {"error": "not found"})

    def test_get_missing_file_is_json_404(self) -> None:
        status, _, data = self.fetch("GET", "/nope.txt")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data), {"error": "not found"})

    def test_unknown_extension_is_octet_stream(self) -> None:
        status, headers, data = self.fetch("GET", "/blob.frobozz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(data, b"\x00\x01\x02")

    def test_traversal_never_escapes_root(self) -> None:
        for path in (
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/..%2fsecret.txt",
            "/firstboot/../../secret.txt",
            "/firstboot/%2e%2e/%2e%2e/secret.txt",
        ):
            status, _, data = self.fetch("GET", path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(SECRET.strip(), data, path)

    # -- pairing routes are matched first ---------------------------------

    def test_pairing_routes_win_over_static(self) -> None:
        body = json.dumps(
            {"code": CODE, "nonce": NONCE, "machine_id": MACHINE}
        ).encode("utf-8")
        status, _, _ = self.fetch(
            "POST",
            "/pairing/register",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        # Operator list still refuses without the token.
        status, _, data = self.fetch("GET", "/pairing")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(data), {"error": "unauthorized"})
        # A pending poll is the contract's 404, not a static miss.
        status, _, data = self.fetch("GET", f"/pairing/{CODE}")
        self.assertEqual(status, 404)
        self.assertEqual(
            json.loads(data), {"code": CODE, "status": "pending"}
        )

    def test_post_never_falls_through_to_static(self) -> None:
        status, _, data = self.fetch("POST", "/firstboot/firstboot.sh")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data), {"error": "not found"})

    # -- off by default -----------------------------------------------------

    def test_static_off_by_default(self) -> None:
        httpd = make_server(self.store, TOKEN, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5
            )
            try:
                conn.request("GET", "/firstboot/firstboot.sh")
                resp = conn.getresponse()
                data = resp.read()
                self.assertEqual(resp.status, 404)
                self.assertEqual(json.loads(data), {"error": "not found"})
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_config_from_env_static_root(self) -> None:
        config = PairingServerConfig.from_env(
            {"FREETALON_PAIRING_STATIC_ROOT": "/srv/payloads"}
        )
        self.assertEqual(config.static_root, "/srv/payloads")
        self.assertIsNone(PairingServerConfig.from_env({}).static_root)


if __name__ == "__main__":
    unittest.main()
