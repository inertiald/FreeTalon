"""Campaign build #25 (2026-11-26): the store-behavior probe family
plus generation-side method-surface steering.

The store-ceiling diagnosis (2026-11-25) classified the pairing
exam's two standing store failures as evidence failures, not
capability failures: expiry-wipe was probe-invisible for 18
straight arms (every in-run check read the API view; nothing ever
read the store FILE after an expiry poll, where the wiped bundle
in fact remained), and the corrupt-file timing (raise at
construction vs first use) had no machine arbiter, so arm #31
resolved it toward its own rewritten test and shipped the
regression. This build registers a store-behavior probe runner
that exercises the tree's real store module through the
contract's clock seam and asserts on the file bytes, binding
C06-expiry and C08-persistence to probe records — the build-#23
treatment — and adds the generation-side half of the
method-surface story: build #24's screening showed a run ship a
do_GET/do_POST-only server because its re-plan budget was spent
elsewhere, so the clauses block now steers the planner to the
closed method set up front whenever a contract pins 405.

No GPU is touched: the probe tests drive fixture store modules
in temp dirs, exactly as builds #6/#12/#16/#23/#24 did.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.contract_clauses import (
    clause_states,
    format_clauses_block,
    format_replan_clauses_block,
    probe_failures,
    probe_suspect_map,
    run_contract_probes,
)

STORE_CLAUSES = [
    {
        "id": "C06-expiry",
        "spec": "freetalon/pairing/CONTRACT.md",
        "heading": "Expiry",
        "text": "## Expiry\n",
        "declared_files": [],
        "method_surface": False,
    },
    {
        "id": "C07-store-api-store-py",
        "spec": "freetalon/pairing/CONTRACT.md",
        "heading": "Store API (store.py) and exceptions",
        "text": "## Store API (store.py) and exceptions\n",
        "declared_files": ["freetalon/pairing/store.py"],
        "method_surface": False,
    },
    {
        "id": "C08-persistence",
        "spec": "freetalon/pairing/CONTRACT.md",
        "heading": "Persistence",
        "text": "## Persistence\n",
        "declared_files": [],
        "method_surface": False,
    },
]

FIXTURE_STORE_GOOD = '''"""Fixture store: the contract's behavior, minimally."""

import json
import time
from dataclasses import dataclass
from pathlib import Path


class StoreCorruptError(Exception):
    pass


@dataclass(frozen=True)
class PollOutcome:
    status: str
    credentials: object = None


class PairingStore:
    def __init__(self, path, ttl_seconds=1800.0, now_fn=time.time):
        self._path = Path(path)
        self._ttl = float(ttl_seconds)
        self._now_fn = now_fn
        self._records = self._load()

    def _load(self):
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise StoreCorruptError(str(exc)) from exc
        return raw["records"]

    def _save(self):
        payload = {"version": 1, "records": self._records}
        self._path.write_text(json.dumps(payload), encoding="utf-8")

    def _refresh(self, record):
        if record["status"] in ("pending", "confirmed"):
            if float(self._now_fn()) >= float(record["expires_at"]):
                record["status"] = "expired"
                record["credentials"] = None
                return True
        return False

    def register(self, code, nonce, machine_id, hostname=None, mac=None):
        now = float(self._now_fn())
        self._records[code] = {
            "code": code,
            "status": "pending",
            "credentials": None,
            "expires_at": now + self._ttl,
        }
        self._save()
        return dict(self._records[code])

    def confirm(self, code, credentials):
        record = self._records[code]
        record["credentials"] = credentials
        record["status"] = "confirmed"
        self._save()
        return dict(record)

    def poll(self, code):
        record = self._records[code]
        changed = self._refresh(record)
        if record["status"] == "confirmed":
            bundle = record["credentials"]
            record["credentials"] = None
            record["status"] = "claimed"
            self._save()
            return PollOutcome(status="confirmed", credentials=bundle)
        if changed:
            self._save()
        return PollOutcome(status=record["status"], credentials=None)
'''

FIXTURE_STORE_BROKEN = '''"""Fixture store: the standing broken shape (diagnosis 2026-11-25).

Lazy load (construction never raises, even over a corrupt file)
and a poll that honors the in-memory expiry wipe but discards
the persist flag — the two defects the probe family exists to
see. The claim path mutates, so it saves: the positive control
stays green against this shape, as it did in every arm.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path


class StoreCorruptError(Exception):
    pass


@dataclass(frozen=True)
class PollOutcome:
    status: str
    credentials: object = None


class PairingStore:
    def __init__(self, path, ttl_seconds=1800.0, now_fn=time.time):
        self._path = Path(path)
        self._ttl = float(ttl_seconds)
        self._now_fn = now_fn
        self._records = None

    def _ensure_loaded(self):
        if self._records is None:
            if not self._path.exists():
                self._records = {}
            else:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._records = raw["records"]

    def _save(self):
        payload = {"version": 1, "records": self._records}
        self._path.write_text(json.dumps(payload), encoding="utf-8")

    def _refresh(self, record):
        if record["status"] in ("pending", "confirmed"):
            if float(self._now_fn()) >= float(record["expires_at"]):
                record["status"] = "expired"
                record["credentials"] = None
                return True
        return False

    def register(self, code, nonce, machine_id, hostname=None, mac=None):
        self._ensure_loaded()
        now = float(self._now_fn())
        self._records[code] = {
            "code": code,
            "status": "pending",
            "credentials": None,
            "expires_at": now + self._ttl,
        }
        self._save()
        return dict(self._records[code])

    def confirm(self, code, credentials):
        self._ensure_loaded()
        record = self._records[code]
        record["credentials"] = credentials
        record["status"] = "confirmed"
        self._save()
        return dict(record)

    def poll(self, code):
        self._ensure_loaded()
        record = self._records[code]
        self._refresh(record)  # changed flag discarded: never persisted
        if record["status"] == "confirmed":
            bundle = record["credentials"]
            record["credentials"] = None
            record["status"] = "claimed"
            self._save()
            return PollOutcome(status="confirmed", credentials=bundle)
        return PollOutcome(status=record["status"], credentials=None)
'''


def _fixture_tree(root: Path, store_text: str) -> Path:
    package = root / "freetalon" / "pairing"
    package.mkdir(parents=True, exist_ok=True)
    (package / "store.py").write_text(store_text, encoding="utf-8")
    return root


def _store_records(root: Path) -> list[dict]:
    return [
        record
        for record in run_contract_probes(STORE_CLAUSES, root)
        if record.get("probe") == "store_behavior"
    ]


class StoreBehaviorProbeTests(unittest.TestCase):
    def test_product_store_passes_all_three_flows(self):
        repo_root = Path(__file__).resolve().parent.parent
        records = _store_records(repo_root)
        self.assertEqual(
            [r["flow"] for r in records],
            [
                "store_expiry_wipe",
                "store_corrupt_raises",
                "store_claim_burn_persists",
            ],
        )
        self.assertTrue(all(r["ok"] for r in records), records)
        self.assertEqual(records[0]["clause_id"], "C06-expiry")
        self.assertEqual(records[1]["clause_id"], "C08-persistence")
        self.assertEqual(records[2]["clause_id"], "C08-persistence")
        for record in records:
            self.assertEqual(record["file"], "freetalon/pairing/store.py")

    def test_good_fixture_store_passes_all_three_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_STORE_GOOD)
            records = _store_records(root)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(r["ok"] for r in records), records)

    def test_broken_fixture_store_fails_exactly_the_two_red_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_STORE_BROKEN)
            records = _store_records(root)
        by_flow = {r["flow"]: r for r in records}
        self.assertFalse(by_flow["store_expiry_wipe"]["ok"])
        self.assertIn(
            "still present in the store file bytes",
            by_flow["store_expiry_wipe"]["detail"],
        )
        self.assertFalse(by_flow["store_corrupt_raises"]["ok"])
        self.assertIn(
            "did not raise", by_flow["store_corrupt_raises"]["detail"]
        )
        self.assertTrue(by_flow["store_claim_burn_persists"]["ok"])

    def test_clause_states_are_probe_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_STORE_BROKEN)
            broken = _store_records(root)
        states = clause_states(STORE_CLAUSES, [], broken)
        self.assertEqual(states["C06-expiry"]["state"], "failed")
        self.assertEqual(states["C08-persistence"]["state"], "failed")

        repo_root = Path(__file__).resolve().parent.parent
        green = _store_records(repo_root)
        states = clause_states(STORE_CLAUSES, [], green)
        self.assertEqual(states["C06-expiry"]["state"], "verified")
        self.assertEqual(states["C08-persistence"]["state"], "verified")

    def test_failures_name_the_store_as_suspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_STORE_BROKEN)
            records = _store_records(root)
        suspect = probe_suspect_map(probe_failures(records))
        self.assertIsNotNone(suspect)
        self.assertEqual(suspect["file"], "freetalon/pairing/store.py")
        self.assertEqual(
            {flow["flow"] for flow in suspect["flows"]},
            {"store_expiry_wipe", "store_corrupt_raises"},
        )

    def test_runner_silent_without_a_store_clause(self):
        clauses = [
            dict(clause, declared_files=[]) for clause in STORE_CLAUSES
        ]
        repo_root = Path(__file__).resolve().parent.parent
        records = [
            r
            for r in run_contract_probes(clauses, repo_root)
            if r.get("probe") == "store_behavior"
        ]
        self.assertEqual(records, [])

    def test_runner_silent_when_the_module_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = _store_records(Path(tmp))
        self.assertEqual(records, [])


METHOD_CLAUSE = {
    "id": "C09-http-api",
    "spec": "freetalon/pairing/CONTRACT.md",
    "heading": "HTTP API",
    "text": "## HTTP API\n",
    "declared_files": ["freetalon/pairing/server.py"],
    "method_surface": True,
}

PLAIN_CLAUSE = {
    "id": "C01-roles",
    "spec": "freetalon/pairing/CONTRACT.md",
    "heading": "Roles",
    "text": "## Roles\n",
    "declared_files": [],
    "method_surface": False,
}


class MethodSurfaceGuidanceTests(unittest.TestCase):
    def test_clauses_block_steers_when_a_clause_pins_405(self):
        block = format_clauses_block([PLAIN_CLAUSE, METHOD_CLAUSE])
        self.assertIn("METHOD SURFACE", block)
        self.assertIn("do_PUT", block)
        self.assertIn("501", block)
        self.assertIn("GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS", block)

    def test_clauses_block_silent_without_a_method_clause(self):
        block = format_clauses_block([PLAIN_CLAUSE])
        self.assertNotIn("METHOD SURFACE", block)

    def test_replan_block_steers_when_a_clause_pins_405(self):
        block = format_replan_clauses_block(
            [PLAIN_CLAUSE, METHOD_CLAUSE], ["C09-http-api"]
        )
        self.assertIn("METHOD SURFACE", block)
        self.assertIn("do_DELETE", block)

    def test_replan_block_silent_without_a_method_clause(self):
        block = format_replan_clauses_block([PLAIN_CLAUSE], [])
        self.assertNotIn("METHOD SURFACE", block)


if __name__ == "__main__":
    unittest.main()
