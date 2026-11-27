"""Fleet update channel tests — store, publisher, and HTTP routes.

Written against freetalon/pairing/CONTRACT.md ("Fleet update channel")
and DESIGN.md section 13: bundles are signed with the fleet key and
served per machine-id, reports land in the fleet state, the operator
view is bearer-gated, and with no fleet directory configured the
channel is off (plain 404s).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from freetalon.pairing.server import make_server
from freetalon.pairing.store import PairingStore
from freetalon.pairing.updates import (
    FleetStateCorrupt,
    FleetStore,
    InvalidBundle,
    build_bundle,
    canonical_manifest_bytes,
)

MACHINE = "380ba450743e4e1eab75b10c222f892d"
OTHER_MACHINE = "9f2c1e7ab4d84c0aa123456789abcdef"
TOKEN = "test-operator-token"
SSH_KEYGEN = shutil.which("ssh-keygen")


def _make_keypair(directory: Path) -> Path:
    key = directory / "fleet-key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "test-fleet", "-f", str(key)],
        capture_output=True, check=True, timeout=30,
    )
    return key


@unittest.skipUnless(SSH_KEYGEN, "ssh-keygen not available")
class FleetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.key = _make_keypair(self.root)
        self.store = FleetStore(self.root / "fleet")

    def _publish(self, machine=MACHINE, version="2026.11.27-1"):
        source = self.root / "payload.txt"
        source.write_text("hello fleet\n", encoding="utf-8")
        bundle = build_bundle(
            machine_id=machine,
            version=version,
            files=[("payload", "/etc/freetalon/payload.txt", source)],
            key_path=self.key,
        )
        bundles = self.root / "fleet" / "bundles"
        bundles.mkdir(parents=True, exist_ok=True)
        (bundles / ("%s.json" % machine)).write_text(
            json.dumps(bundle), encoding="utf-8"
        )
        return bundle

    def test_bundle_round_trip_and_canonical_bytes(self) -> None:
        bundle = self._publish()
        served = self.store.current_bundle(MACHINE)
        self.assertIsNotNone(served)
        self.assertEqual(served["manifest"]["version"], "2026.11.27-1")
        # The canonical string served is byte-identical to the signed bytes.
        self.assertEqual(
            served["manifest_canonical"].encode("utf-8"),
            canonical_manifest_bytes(served["manifest"]),
        )
        self.assertIn("BEGIN SSH SIGNATURE", served["signature"])

    def test_no_bundle_for_unknown_or_malformed_machine(self) -> None:
        self._publish()
        self.assertIsNone(self.store.current_bundle(OTHER_MACHINE))
        self.assertIsNone(self.store.current_bundle("not-a-machine-id"))
        self.assertIsNone(self.store.current_bundle("../pairing"))

    def test_bundle_for_other_machine_is_not_served(self) -> None:
        self._publish(machine=OTHER_MACHINE)
        self.assertIsNone(self.store.current_bundle(MACHINE))

    def test_build_bundle_rejects_bad_machine_and_version(self) -> None:
        source = self.root / "payload.txt"
        source.write_text("x", encoding="utf-8")
        with self.assertRaises(InvalidBundle):
            build_bundle(
                machine_id="nope", version="v1",
                files=[("p", "/etc/freetalon/p", source)], key_path=self.key,
            )
        with self.assertRaises(InvalidBundle):
            build_bundle(
                machine_id=MACHINE, version="bad version!",
                files=[("p", "/etc/freetalon/p", source)], key_path=self.key,
            )

    def test_report_round_trip_and_view(self) -> None:
        view = self.store.record_report(
            MACHINE,
            {
                "machine_id": MACHINE,
                "hostname": "freetalon-6438",
                "mac": "1c:69:7a:d9:8a:bf",
                "console_version": "3.0",
                "bundle_version": "2026.11.27-1",
                "last_result": "applied",
                "last_detail": "ok",
                "at": "2026-11-27T02:00:00Z",
            },
        )
        self.assertEqual(view["hostname"], "freetalon-6438")
        self.assertEqual(view["bundle_version"], "2026.11.27-1")
        fleet = self.store.fleet_view()
        self.assertEqual(len(fleet["nodes"]), 1)
        self.assertEqual(fleet["nodes"][0]["machine_id"], MACHINE)
        # A fresh store over the same directory sees the report (it persisted).
        again = FleetStore(self.root / "fleet")
        self.assertEqual(len(again.fleet_view()["nodes"]), 1)

    def test_report_rejects_bad_machine(self) -> None:
        with self.assertRaises(InvalidBundle):
            self.store.record_report("nope", {"machine_id": "nope"})

    def test_corrupt_state_raises_never_resets(self) -> None:
        fleet_dir = self.root / "fleet2"
        fleet_dir.mkdir(parents=True)
        (fleet_dir / "state.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(FleetStateCorrupt):
            FleetStore(fleet_dir)


class FleetHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pairing_store = PairingStore(self.root / "pairing.json", ttl_seconds=600)
        self.fleet_dir = self.root / "fleet"
        self.httpd = make_server(
            self.pairing_store, TOKEN, host="127.0.0.1", port=0,
            fleet_dir=str(self.fleet_dir),
        )
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def request(self, method, path, body=None, token=None):
        data = None
        hdrs = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if token is not None:
            hdrs["Authorization"] = "Bearer %s" % token
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {"raw": raw}

    @unittest.skipUnless(SSH_KEYGEN, "ssh-keygen not available")
    def test_bundle_served_after_publish(self) -> None:
        key = _make_keypair(self.root)
        source = self.root / "payload.txt"
        source.write_text("hello fleet\n", encoding="utf-8")
        bundle = build_bundle(
            machine_id=MACHINE, version="2026.11.27-1",
            files=[("payload", "/etc/freetalon/payload.txt", source)],
            key_path=key,
        )
        bundles = self.fleet_dir / "bundles"
        bundles.mkdir(parents=True, exist_ok=True)
        (bundles / ("%s.json" % MACHINE)).write_text(
            json.dumps(bundle), encoding="utf-8"
        )
        status, body = self.request("GET", "/fleet/updates/%s" % MACHINE)
        self.assertEqual(status, 200)
        self.assertEqual(body["manifest"]["version"], "2026.11.27-1")
        status, _ = self.request("GET", "/fleet/updates/%s" % OTHER_MACHINE)
        self.assertEqual(status, 404)

    def test_report_and_operator_view(self) -> None:
        status, _ = self.request("GET", "/fleet")
        self.assertEqual(status, 401)
        status, body = self.request(
            "POST", "/fleet/report",
            {"machine_id": MACHINE, "hostname": "freetalon-6438",
             "console_version": "3.0", "last_result": "applied"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.request("GET", "/fleet", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["nodes"][0]["hostname"], "freetalon-6438")
        status, _ = self.request(
            "POST", "/fleet/report", {"machine_id": "bad"}
        )
        self.assertEqual(status, 400)

    def test_wrong_method_is_405_and_pairing_unaffected(self) -> None:
        status, _ = self.request("POST", "/fleet/updates/%s" % MACHINE, {})
        self.assertEqual(status, 405)
        status, _ = self.request("GET", "/fleet/report")
        self.assertEqual(status, 405)
        status, _ = self.request("PUT", "/fleet", {})
        self.assertEqual(status, 405)
        # Pairing routes still behave (unknown code polls as pending).
        status, body = self.request("GET", "/pairing/KESTREL-4821")
        self.assertEqual(status, 404)
        self.assertEqual(body["status"], "pending")


class FleetDisabledTests(unittest.TestCase):
    def test_no_fleet_dir_means_plain_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PairingStore(Path(tmp) / "pairing.json", ttl_seconds=600)
            httpd = make_server(store, TOKEN, host="127.0.0.1", port=0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % httpd.server_address[1]
                req = urllib.request.Request(base + "/fleet", method="GET")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 404)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
