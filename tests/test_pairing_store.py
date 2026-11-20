"""Pairing store tests — written against freetalon/pairing/CONTRACT.md.

The contract pins: the status vocabulary (pending/confirmed/claimed/
expired), record fields, exception behavior, the clock seam (now_fn),
burn-on-claim, lazy expiry with bundle wipe, and 0600 atomic
persistence. If a test here and the implementation disagree, the
contract is the arbiter.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from freetalon.pairing.store import (
    STATUS_CLAIMED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    CodeAlreadyClaimed,
    CodeAlreadyConfirmed,
    CodeConflict,
    CodeExpired,
    CodeNotFound,
    InvalidBundle,
    InvalidRegistration,
    PairingStore,
    StoreCorruptError,
)

CODE = "KESTREL-4821"
NONCE = "ab" * 16
MACHINE = "9f2c1e7ab4d84c0aa123456789abcdef"
BUNDLE = {"tailscale_auth_key": "tskey-auth-TESTONLY", "ssh_key": "ssh-ed25519 TEST"}


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class PairingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "pairing-store.json"
        self.clock = FakeClock()
        self.store = PairingStore(self.path, ttl_seconds=600, now_fn=self.clock)

    def register(self, **overrides):
        args = {"code": CODE, "nonce": NONCE, "machine_id": MACHINE}
        args.update(overrides)
        return self.store.register(**args)

    # -- register --------------------------------------------------------

    def test_register_creates_pending_record(self) -> None:
        view = self.register(hostname="kestrel-01", mac="aa:bb:cc:dd:ee:ff")
        self.assertEqual(view["code"], CODE)
        self.assertEqual(view["status"], STATUS_PENDING)
        self.assertEqual(view["machine_id"], MACHINE)
        self.assertEqual(view["hostname"], "kestrel-01")
        self.assertEqual(view["created_at"], self.clock.t)
        self.assertEqual(view["expires_at"], self.clock.t + 600)
        self.assertIsNone(view["confirmed_at"])
        self.assertIsNone(view["claimed_at"])

    def test_public_view_never_leaks_nonce_or_credentials(self) -> None:
        view = self.register()
        self.assertNotIn("nonce", view)
        self.assertNotIn("credentials", view)
        self.store.confirm(CODE, BUNDLE)
        for listed in self.store.list_records():
            self.assertNotIn("nonce", listed)
            self.assertNotIn("credentials", listed)

    def test_register_is_idempotent_for_same_identity(self) -> None:
        first = self.register()
        second = self.register()
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_records()), 1)

    def test_register_conflict_on_different_nonce(self) -> None:
        self.register()
        with self.assertRaises(CodeConflict):
            self.register(nonce="cd" * 16)

    def test_register_conflict_on_different_machine(self) -> None:
        self.register()
        with self.assertRaises(CodeConflict):
            self.register(machine_id="0" * 32)

    def test_register_rejects_bad_code_shapes(self) -> None:
        for bad in ("kestrel-4821", "KESTREL-482", "KESTREL-48210", "KESTREL4821",
                    "KESTREL-ABCD", "", "KESTREL-4821 ", 4821, None):
            with self.assertRaises(InvalidRegistration, msg=repr(bad)):
                self.store.register(bad, NONCE, MACHINE)

    def test_register_rejects_bad_nonce_and_machine(self) -> None:
        for bad_nonce in ("zz" * 16, "ab" * 15, "ab" * 17, "", None, 42):
            with self.assertRaises(InvalidRegistration, msg=repr(bad_nonce)):
                self.store.register(CODE, bad_nonce, MACHINE)
        for bad_machine in ("", "   ", None, "x" * 129, "bad\nmachine"):
            with self.assertRaises(InvalidRegistration, msg=repr(bad_machine)):
                self.store.register(CODE, NONCE, bad_machine)

    def test_nonce_is_normalized_to_lowercase(self) -> None:
        self.register(nonce="AB" * 16)
        # Same nonce in its other case is the same identity: idempotent.
        view = self.register(nonce="ab" * 16)
        self.assertEqual(view["status"], STATUS_PENDING)

    # -- poll / confirm ----------------------------------------------------

    def test_poll_unknown_code_raises_not_found(self) -> None:
        with self.assertRaises(CodeNotFound):
            self.store.poll("MAPLE-0001")

    def test_poll_pending_returns_no_credentials(self) -> None:
        self.register()
        outcome = self.store.poll(CODE)
        self.assertEqual(outcome.status, STATUS_PENDING)
        self.assertIsNone(outcome.credentials)

    def test_confirm_then_poll_claims_exactly_once(self) -> None:
        self.register()
        view = self.store.confirm(CODE, BUNDLE)
        self.assertEqual(view["status"], STATUS_CONFIRMED)
        self.assertIsNotNone(view["confirmed_at"])

        outcome = self.store.poll(CODE)
        self.assertEqual(outcome.status, STATUS_CONFIRMED)
        self.assertEqual(outcome.credentials, BUNDLE)

        again = self.store.poll(CODE)
        self.assertEqual(again.status, STATUS_CLAIMED)
        self.assertIsNone(again.credentials)

        # The burn is durable: a fresh store over the same file agrees,
        # and the bundle bytes are gone from disk.
        fresh = PairingStore(self.path, ttl_seconds=600, now_fn=self.clock)
        self.assertEqual(fresh.poll(CODE).status, STATUS_CLAIMED)
        self.assertNotIn("tskey-auth-TESTONLY", self.path.read_text())

    def test_confirm_unknown_code(self) -> None:
        with self.assertRaises(CodeNotFound):
            self.store.confirm("MAPLE-0001", BUNDLE)

    def test_confirm_twice_is_already_confirmed(self) -> None:
        self.register()
        self.store.confirm(CODE, BUNDLE)
        with self.assertRaises(CodeAlreadyConfirmed):
            self.store.confirm(CODE, BUNDLE)

    def test_confirm_after_claim_is_already_claimed(self) -> None:
        self.register()
        self.store.confirm(CODE, BUNDLE)
        self.store.poll(CODE)
        with self.assertRaises(CodeAlreadyClaimed):
            self.store.confirm(CODE, BUNDLE)

    def test_confirm_rejects_bad_bundles(self) -> None:
        self.register()
        for bad in (None, {}, [], "secret", 42, {"k": "x" * 40000}):
            with self.assertRaises(InvalidBundle, msg=repr(bad)[:40]):
                self.store.confirm(CODE, bad)
        # None of the failed confirms confirmed anything.
        self.assertEqual(self.store.poll(CODE).status, STATUS_PENDING)

    # -- expiry --------------------------------------------------------------

    def test_pending_code_expires_and_stays_down(self) -> None:
        self.register()
        self.clock.advance(601)
        outcome = self.store.poll(CODE)
        self.assertEqual(outcome.status, STATUS_EXPIRED)
        self.assertIsNone(outcome.credentials)
        with self.assertRaises(CodeExpired):
            self.store.confirm(CODE, BUNDLE)

    def test_confirmed_code_expires_and_bundle_is_wiped(self) -> None:
        self.register()
        self.store.confirm(CODE, BUNDLE)
        self.clock.advance(601)
        outcome = self.store.poll(CODE)
        self.assertEqual(outcome.status, STATUS_EXPIRED)
        self.assertIsNone(outcome.credentials)
        self.assertNotIn("tskey-auth-TESTONLY", self.path.read_text())

    def test_claimed_code_never_expires(self) -> None:
        self.register()
        self.store.confirm(CODE, BUNDLE)
        self.store.poll(CODE)
        self.clock.advance(10_000_000)
        self.assertEqual(self.store.poll(CODE).status, STATUS_CLAIMED)

    def test_expired_code_reregisters_fresh_for_same_node(self) -> None:
        self.register()
        self.clock.advance(601)
        self.assertEqual(self.store.poll(CODE).status, STATUS_EXPIRED)
        view = self.register()
        self.assertEqual(view["status"], STATUS_PENDING)
        self.assertEqual(view["created_at"], self.clock.t)
        # And a different node still cannot take the code over.
        with self.assertRaises(CodeConflict):
            self.register(nonce="cd" * 16)

    # -- persistence -----------------------------------------------------------

    def test_records_survive_a_new_store_instance(self) -> None:
        self.register()
        fresh = PairingStore(self.path, ttl_seconds=600, now_fn=self.clock)
        self.assertEqual([r["code"] for r in fresh.list_records()], [CODE])

    def test_store_file_is_0600(self) -> None:
        self.register()
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_store_file_raises_and_is_not_reset(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(StoreCorruptError):
            PairingStore(self.path, ttl_seconds=600, now_fn=self.clock)
        self.assertEqual(self.path.read_text(), "{ not json")

    def test_store_file_is_valid_versioned_json(self) -> None:
        self.register()
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["version"], 1)
        self.assertIn(CODE, raw["records"])

    def test_nonpositive_ttl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PairingStore(self.path, ttl_seconds=0, now_fn=self.clock)


if __name__ == "__main__":
    unittest.main()
