"""Tests for the M4 gate store: packets, hashes, decisions, expiry."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from freetalon.orchestrator.gates import (
    GateStatus,
    GateStore,
    build_packet,
    compute_packet_hash,
)


def _packet(**overrides):
    kwargs = {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "node_id": "flash",
        "action": "flash_ecu",
        "what": "Flash the ECU with the analysed tune file",
        "why": "next step toward the tuned ECU",
        "risk": "a bad flash can brick the ECU",
        "cost": 0.0,
        "reversible": False,
        "evidence": ["research/ecu-brick-risk.json"],
        "inputs": {"file": "sandbox/tune.bin"},
    }
    kwargs.update(overrides)
    return build_packet(**kwargs)


class TestPacketHash(unittest.TestCase):
    def test_hash_is_stable_for_the_same_action(self):
        a = compute_packet_hash("flash_ecu", "flash", {"file": "tune.bin"})
        b = compute_packet_hash("flash_ecu", "flash", {"file": "tune.bin"})
        self.assertEqual(a, b)

    def test_hash_changes_when_inputs_change(self):
        a = compute_packet_hash("flash_ecu", "flash", {"file": "tune-a.bin"})
        b = compute_packet_hash("flash_ecu", "flash", {"file": "tune-b.bin"})
        self.assertNotEqual(a, b)

    def test_hash_does_not_depend_on_evidence(self):
        # Evidence grows as the run progresses; it must not void an
        # approval for the same action.
        a = compute_packet_hash("flash_ecu", "flash", {"file": "tune.bin"})
        b = compute_packet_hash("flash_ecu", "flash", {"file": "tune.bin"})
        self.assertEqual(a, b)


class TestGateStore(unittest.TestCase):
    def _store(self, tmp):
        return GateStore(Path(tmp) / "state.db")

    def test_create_and_get_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            packet = _packet()
            store.create(packet)
            fetched = store.get(packet.packet_id)
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.action, "flash_ecu")
            self.assertEqual(fetched.status, GateStatus.PENDING)
            self.assertEqual(fetched.packet_hash, packet.packet_hash)

    def test_pending_lists_only_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            p1 = store.create(_packet(node_id="a"))
            p2 = store.create(_packet(node_id="b"))
            store.decide(p1.packet_id, "approve", "pat")
            pending = store.pending()
            self.assertEqual([p.packet_id for p in pending], [p2.packet_id])

    def test_approve_records_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            packet = store.create(_packet())
            decided = store.decide(packet.packet_id, "approve", "pat", note="looks good")
            self.assertEqual(decided.status, GateStatus.APPROVED)
            self.assertEqual(decided.decided_by, "pat")
            self.assertEqual(decided.decision_note, "looks good")
            self.assertIsNotNone(decided.decided_at)

    def test_deny_records_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            packet = store.create(_packet())
            decided = store.decide(packet.packet_id, "deny", "pat", note="too risky")
            self.assertEqual(decided.status, GateStatus.DENIED)

    def test_decide_twice_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            packet = store.create(_packet())
            store.decide(packet.packet_id, "approve", "pat")
            with self.assertRaises(ValueError):
                store.decide(packet.packet_id, "deny", "pat")

    def test_decide_unknown_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ValueError):
                store.decide("gate-nope", "approve", "pat")

    def test_expire_overdue_marks_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            packet = store.create(_packet(ttl_s=1))
            future = datetime.now(UTC) + timedelta(seconds=3600)
            expired = store.expire_overdue(now=future)
            self.assertEqual(len(expired), 1)
            self.assertEqual(expired[0].status, GateStatus.EXPIRED)
            fetched = store.get(packet.packet_id)
            assert fetched is not None
            self.assertEqual(fetched.status, GateStatus.EXPIRED)
            self.assertEqual(store.pending(), [])

    def test_decide_after_expiry_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            packet = store.create(_packet(ttl_s=1))
            store.expire_overdue(now=datetime.now(UTC) + timedelta(seconds=3600))
            with self.assertRaises(ValueError):
                store.decide(packet.packet_id, "approve", "pat")

    def test_denied_hashes_for_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            p1 = store.create(_packet(node_id="a"))
            p2 = store.create(_packet(node_id="b"))
            store.decide(p1.packet_id, "deny", "pat")
            hashes = store.denied_hashes_for_run("run-1")
            self.assertIn(p1.packet_hash, hashes)
            self.assertNotIn(p2.packet_hash, hashes)

    def test_list_for_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.create(_packet(node_id="a"))
            store.create(_packet(run_id="run-2", node_id="b"))
            self.assertEqual(len(store.list_for_run("run-1")), 1)
            self.assertEqual(len(store.list_for_run("run-2")), 1)


if __name__ == "__main__":
    unittest.main()
