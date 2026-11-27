"""Campaign build #33: scope enumeration for repair artifacts.

Build #32's direct repair path executed one obligation per
diagnosed defect, each prompt carrying only its own probe
record. Arm #41 showed the cost: four repair passes patched the
pairing confirm flow one record at a time and never produced
the one-line envelope unwrap the records jointly force — the
confirm obligation never saw confirm_bad_credentials' measured
detail, and neither saw poll_claim's. The diagnostic battery
had repaired the same defects only when its spec named the
full scope (the two-location scope rider, in vivo again).

This build enumerates the scope mechanically: a defect's
repair family is every probe record sharing its (clause,
probe, file) key — failing members are co-defects of one
shared behavior, passing members are standing constraints —
and the loop executes ONE obligation per family whose prompt
renders the whole measurement set as simultaneous constraints
on the shared handling.

Tested at the levels the build operates:

- the derivation (contract_clauses.repair_family_key /
  repair_family_scope): the key is the record's own fields or
  nothing; the scope is exactly the family's records, in
  probe-run order, projected with defect ids;
- the prompt (planner._direct_repair_prompt +
  _direct_repair_family_scope): a singleton scope renders
  build #32's prompt byte-identically; a family scope names
  every failing member and every passing constraint;
- the executor (mocked call_llm): a family obligation's
  outcome reports the family it was shown; a no-change patch
  fails with the WHOLE family's failures restated;
- the loop pass (real store probes): a store whose poll-side
  mutations are computed on a copy — one shared root breaking
  two flows of one family, the D4/D5 shape in miniature — is
  repaired by ONE family obligation (one model call), both
  defects verified fixed at the candidate, the passing sibling
  never an obligation, both lineage rows carrying the family.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    repair_family_key,
    repair_family_scope,
    run_contract_probes,
)
from freetalon.orchestrator.gates import GateStore
from freetalon.orchestrator.lineage import (
    LineageRecorder,
    defect_id_for_record,
    drain_pending_lineage,
)
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.models import ExecutionPlan, TaskIntent
from freetalon.orchestrator.planner import (
    _direct_repair_family_scope,
    _direct_repair_prompt,
    drain_pending_planner_attempts,
    execute_direct_repair_obligation,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

# ── Fixtures ─────────────────────────────────────────────────────

MOD_STANDING = "VALUE = 1\nOTHER = 2\n"
MOD_RECORD = {
    "clause_id": "C01-fixture",
    "probe": "store_behavior",
    "flow": "fix_value",
    "detail": "the measured failure: VALUE must be 3",
    "file": "pkg/mod.py",
}
MOD_DEFECT = "C01-fixture::store_behavior::fix_value"
MOD_FIX = json.dumps(
    {"edits": [{"search": "VALUE = 1\n", "replace": "VALUE = 3\n"}]}
)

# The mod family's scope, in repair_family_scope's projection.
MOD_SCOPE = [
    {
        "defect_id": MOD_DEFECT,
        "clause_id": "C01-fixture",
        "probe": "store_behavior",
        "flow": "fix_value",
        "ok": False,
        "detail": "the measured failure: VALUE must be 3",
        "file": "pkg/mod.py",
    },
    {
        "defect_id": "C01-fixture::store_behavior::fix_other",
        "clause_id": "C01-fixture",
        "probe": "store_behavior",
        "flow": "fix_other",
        "ok": False,
        "detail": "the measured failure: OTHER must stay 2",
        "file": "pkg/mod.py",
    },
    {
        "defect_id": "C01-fixture::store_behavior::keep_check",
        "clause_id": "C01-fixture",
        "probe": "store_behavior",
        "flow": "keep_check",
        "ok": True,
        "detail": "the keep check held: the file parsed",
        "file": "pkg/mod.py",
    },
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _ScriptedLLM:
    """A call_llm stand-in: queued replies, captured prompts."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, system_prompt, prompt, settings=None):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("the scripted LLM ran out of replies")
        return self.replies.pop(0)


class _DrainSinks:
    def tearDown(self):
        drain_pending_lineage(lambda record: None)
        drain_pending_planner_attempts(lambda record: None)
        super().tearDown()


# ── Part A: the derivation ───────────────────────────────────────


class TestFamilyDerivation(unittest.TestCase):
    RECORDS = [
        {
            "clause_id": "C09-http",
            "probe": "http_smoke",
            "flow": "register",
            "ok": True,
            "detail": "register answered 201",
            "file": "pkg/server.py",
        },
        {
            "clause_id": "C09-http",
            "probe": "http_smoke",
            "flow": "confirm",
            "ok": False,
            "detail": "confirm answered 409",
            "file": "pkg/server.py",
        },
        {
            "clause_id": "C09-http",
            "probe": "http_smoke",
            "flow": "poll_claim",
            "ok": False,
            "detail": "poll_claim returned the envelope",
            "file": "pkg/server.py",
        },
        {
            "clause_id": "C09-http",
            "probe": "method_matrix",
            "flow": "method_matrix_get_on_confirm",
            "ok": False,
            "detail": "a different probe family",
            "file": "pkg/server.py",
        },
        {
            "clause_id": "C06-store",
            "probe": "store_behavior",
            "flow": "store_expiry_wipe",
            "ok": False,
            "detail": "a different clause and file",
            "file": "pkg/store.py",
        },
        {
            "clause_id": "C09-http",
            "probe": "http_smoke",
            "flow": "confirm",
            "ok": False,
            "detail": "the same flow against another file",
            "file": "pkg/other.py",
        },
        {
            "clause_id": "C09-http",
            "probe": "declared_files",
            "ok": True,
            "detail": "declared files present",
        },
    ]

    def test_key_is_the_records_own_fields(self):
        self.assertEqual(
            repair_family_key(self.RECORDS[1]),
            ("C09-http", "http_smoke", "pkg/server.py"),
        )
        # No file, no clause, no probe — no family.
        self.assertIsNone(repair_family_key(self.RECORDS[6]))
        self.assertIsNone(
            repair_family_key({"probe": "p", "file": "f"})
        )
        self.assertIsNone(
            repair_family_key({"clause_id": "c", "file": "f"})
        )
        self.assertIsNone(
            repair_family_key({"clause_id": "c", "probe": "p"})
        )
        self.assertIsNone(repair_family_key("not a record"))

    def test_scope_is_exactly_the_family_in_run_order(self):
        scope = repair_family_scope(
            self.RECORDS, ("C09-http", "http_smoke", "pkg/server.py")
        )
        self.assertEqual(
            [entry["flow"] for entry in scope],
            ["register", "confirm", "poll_claim"],
        )
        self.assertEqual(
            [entry["defect_id"] for entry in scope],
            [
                "C09-http::http_smoke::register",
                "C09-http::http_smoke::confirm",
                "C09-http::http_smoke::poll_claim",
            ],
        )
        self.assertEqual(
            [entry["ok"] for entry in scope], [True, False, False]
        )
        self.assertEqual(scope[1]["detail"], "confirm answered 409")

    def test_scope_of_a_flowless_record_uses_the_two_part_id(self):
        records = [
            {
                "clause_id": "C02-x",
                "probe": "method_surface",
                "ok": False,
                "detail": "the surface is short a handler",
                "file": "pkg/server.py",
            }
        ]
        scope = repair_family_scope(
            records, ("C02-x", "method_surface", "pkg/server.py")
        )
        self.assertEqual(len(scope), 1)
        self.assertEqual(scope[0]["defect_id"], "C02-x::method_surface")
        self.assertIsNone(scope[0]["flow"])


# ── Part B: the prompt ───────────────────────────────────────────


class TestFamilyPrompt(_DrainSinks, unittest.TestCase):
    def _prompt(self, scope):
        return _direct_repair_prompt(
            defect_id=MOD_DEFECT,
            record=dict(MOD_RECORD),
            target="pkg/mod.py",
            standing=MOD_STANDING,
            goal_text="fix the fixture",
            errors_block="",
            scope=scope,
        )

    def test_singleton_scope_is_build32_byte_identical(self):
        baseline = self._prompt(None)
        self.assertEqual(baseline, self._prompt([]))
        self.assertEqual(baseline, self._prompt(MOD_SCOPE[:1]))
        self.assertIn("one diagnosed defect in pkg/mod.py", baseline)
        self.assertNotIn("SCOPE", baseline)

    def test_family_prompt_names_every_member(self):
        prompt = self._prompt(MOD_SCOPE)
        self.assertIn("sharing one measured behavior", prompt)
        # Every failing member is a headline co-defect.
        self.assertIn("defect: C01-fixture::store_behavior::fix_value", prompt)
        self.assertIn("defect: C01-fixture::store_behavior::fix_other", prompt)
        self.assertIn("the measured failure: VALUE must be 3", prompt)
        self.assertIn("the measured failure: OTHER must stay 2", prompt)
        # The scope block enumerates the whole set, passing included.
        self.assertIn("SCOPE — the complete measurement set", prompt)
        self.assertIn("simultaneous constraints", prompt)
        self.assertIn("FAILING  flow fix_value", prompt)
        self.assertIn("FAILING  flow fix_other", prompt)
        self.assertIn(
            "PASSING  flow keep_check — the keep check held", prompt
        )
        self.assertIn("stays passing", prompt)
        # The standing file and the patch form are unchanged.
        self.assertIn("VALUE = 1", prompt)
        self.assertIn('"edits"', prompt)

    def test_family_scope_normalization(self):
        # Entries for another file are not this obligation's scope.
        foreign = MOD_SCOPE + [
            {**MOD_SCOPE[0], "defect_id": "X::y::z", "file": "pkg/x.py"}
        ]
        effective = _direct_repair_family_scope(
            foreign,
            defect_id=MOD_DEFECT,
            record=dict(MOD_RECORD),
            target="pkg/mod.py",
        )
        self.assertEqual(len(effective), 3)
        # A scope missing its headline defect gains it, failing.
        partial = [MOD_SCOPE[2]]
        effective = _direct_repair_family_scope(
            partial,
            defect_id=MOD_DEFECT,
            record=dict(MOD_RECORD),
            target="pkg/mod.py",
        )
        self.assertEqual(effective[0]["defect_id"], MOD_DEFECT)
        self.assertFalse(effective[0]["ok"])
        # A true singleton normalizes to None (build #32's form).
        self.assertIsNone(
            _direct_repair_family_scope(
                MOD_SCOPE[:1],
                defect_id=MOD_DEFECT,
                record=dict(MOD_RECORD),
                target="pkg/mod.py",
            )
        )


# ── Part C: the executor with a scope ────────────────────────────


class TestFamilyExecutor(_DrainSinks, unittest.TestCase):
    def _run(self, root: Path, llm: _ScriptedLLM, **overrides):
        kwargs = {
            "defect_id": MOD_DEFECT,
            "record": dict(MOD_RECORD),
            "target": "pkg/mod.py",
            "repo_root": root,
            "goal_text": "fix the fixture",
            "scope": [dict(entry) for entry in MOD_SCOPE],
        }
        kwargs.update(overrides)
        with mock.patch.object(planner_module, "call_llm", llm):
            return execute_direct_repair_obligation(**kwargs)

    def test_family_outcome_reports_the_scope_it_was_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            llm = _ScriptedLLM([MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            # The outcome's family list is canonical (sorted);
            # the prompt renders probe-run order instead.
            self.assertEqual(
                outcome["family"],
                [
                    "C01-fixture::store_behavior::fix_other",
                    "C01-fixture::store_behavior::fix_value",
                ],
            )
            self.assertEqual(len(outcome["scope"]), 3)
            self.assertIn("SCOPE", llm.prompts[0])
            self.assertEqual(
                (root / "pkg" / "mod.py").read_text(encoding="utf-8"),
                "VALUE = 3\nOTHER = 2\n",
            )

    def test_no_change_retry_restates_the_whole_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            identity = json.dumps(
                {"edits": [{"search": "VALUE = 1\n", "replace": "VALUE = 1\n"}]}
            )
            llm = _ScriptedLLM([identity, MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            self.assertIn("MEASURED failing", llm.prompts[1])
            self.assertIn("fix_value", llm.prompts[1])
            self.assertIn("fix_other", llm.prompts[1])
            self.assertIn("OTHER must stay 2", llm.prompts[1])

    def test_singleton_scope_keeps_the_build32_outcome_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            llm = _ScriptedLLM([MOD_FIX])
            outcome = self._run(root, llm, scope=MOD_SCOPE[:1])
            self.assertEqual(outcome["status"], "patched")
            self.assertNotIn("family", outcome)
            self.assertNotIn("scope", outcome)
            self.assertNotIn("SCOPE", llm.prompts[0])


# ── Part D: the loop pass over a real family ─────────────────────

# A pairing store whose poll-side mutations are computed on a
# copy and never persisted — ONE shared root (poll derives its
# answer instead of mutating + saving) breaking two flows of
# one family at once: the expiry wipe and the claim burn. The
# corrupt-file flow, the family's passing sibling, is intact.
FIXTURE_STORE_FAMILY_DEFECTIVE = '''"""Fixture pairing store: poll never persists its mutations."""

import json
from pathlib import Path


class StoreCorruptError(Exception):
    pass


class PollOutcome:
    def __init__(self, status, credentials=None):
        self.status = status
        self.credentials = credentials


class PairingStore:
    def __init__(self, path, ttl_seconds=900.0, now_fn=None):
        self._path = Path(path)
        self._ttl = float(ttl_seconds)
        self._now = now_fn or (lambda: 0.0)
        self._records = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise StoreCorruptError(str(exc)) from exc
            self._records = data.get("records") or {}

    def _save(self):
        self._path.write_text(
            json.dumps({"records": self._records}), encoding="utf-8"
        )

    def register(self, code, nonce, machine_id):
        self._records[code] = {
            "status": "pending",
            "nonce": nonce,
            "machine_id": machine_id,
            "created": self._now(),
            "credentials": None,
        }
        self._save()

    def confirm(self, code, credentials):
        record = self._records[code]
        record["status"] = "confirmed"
        record["credentials"] = credentials
        self._save()

    def poll(self, code):
        record = self._records.get(code)
        if record is None:
            return PollOutcome("unknown")
        view = dict(record)
        if self._now() - view["created"] > self._ttl:
            view["status"] = "expired"
            view["credentials"] = None
            return PollOutcome(view["status"], view["credentials"])
        if view["status"] == "confirmed":
            bundle = view["credentials"]
            view["status"] = "claimed"
            view["credentials"] = None
            return PollOutcome("confirmed", bundle)
        return PollOutcome(view["status"])
'''

# The shared-handling fix the scripted model returns: poll
# mutates the record and saves, in both branches, one edit.
FAMILY_FIX = json.dumps(
    {
        "edits": [
            {
                "search": (
                    "        view = dict(record)\n"
                    '        if self._now() - view["created"] > self._ttl:\n'
                    '            view["status"] = "expired"\n'
                    '            view["credentials"] = None\n'
                    '            return PollOutcome(view["status"], '
                    'view["credentials"])\n'
                    '        if view["status"] == "confirmed":\n'
                    '            bundle = view["credentials"]\n'
                    '            view["status"] = "claimed"\n'
                    '            view["credentials"] = None\n'
                    '            return PollOutcome("confirmed", bundle)\n'
                    '        return PollOutcome(view["status"])\n'
                ),
                "replace": (
                    "        if self._now() - record[\"created\"] > self._ttl:\n"
                    '            record["status"] = "expired"\n'
                    '            record["credentials"] = None\n'
                    "            self._save()\n"
                    '            return PollOutcome("expired", None)\n'
                    '        if record["status"] == "confirmed":\n'
                    '            bundle = record["credentials"]\n'
                    '            record["status"] = "claimed"\n'
                    '            record["credentials"] = None\n'
                    "            self._save()\n"
                    '            return PollOutcome("confirmed", bundle)\n'
                    '        return PollOutcome(record["status"])\n'
                ),
            }
        ]
    }
)

FAMILY_CLAUSE = {
    "id": "C06-pairing-store",
    "spec": "freetalon/pairing/CONTRACT.md",
    "heading": "Pairing store (store.py)",
    "text": "## Pairing store (store.py)\n",
    "declared_files": ["freetalon/pairing/store.py"],
    "method_surface": False,
}
STORE_PATH = "freetalon/pairing/store.py"
CLAIM_DEFECT = "C06-pairing-store::store_behavior::store_claim_burn_persists"
EXPIRY_DEFECT = "C06-pairing-store::store_behavior::store_expiry_wipe"
CORRUPT_DEFECT = "C06-pairing-store::store_behavior::store_corrupt_raises"


class TestFamilyRepairPass(_DrainSinks, unittest.TestCase):
    def test_one_family_one_call_both_defects_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = root / "tree"
            _write(tree / "freetalon" / "__init__.py", "")
            _write(tree / "freetalon" / "pairing" / "__init__.py", "")
            _write(tree / STORE_PATH, FIXTURE_STORE_FAMILY_DEFECTIVE)
            store = ExecutionPlanStateStore(root / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            run_store = RunStore(root / "runs")
            gates = GateStore(root / "state.db")
            runner = LoopRunner(
                store, registry, run_store=run_store, gate_store=gates
            )
            plan = ExecutionPlan(
                plan_id="b33-family",
                intent=TaskIntent(goal="fix the pairing store"),
                nodes=[],
                metadata={
                    "repo_root": str(tree),
                    "contract_clauses": [FAMILY_CLAUSE],
                },
            )
            results = run_contract_probes([FAMILY_CLAUSE], tree)
            failing = [r for r in results if not r["ok"]]
            self.assertEqual(
                sorted(defect_id_for_record(r) for r in failing),
                [CLAIM_DEFECT, EXPIRY_DEFECT],
            )
            recorder = LineageRecorder(run_store, "run-family")
            recorder.note_baseline(
                {STORE_PATH: FIXTURE_STORE_FAMILY_DEFECTIVE.encode()}
            )
            recorder.diagnosis(1, results, failing)
            version_outcomes = {
                1: {
                    "version": 1,
                    "probe_green": 2,
                    "probe_total": 4,
                    "failed_nodes": 2,
                    "failed_criteria": 1,
                }
            }
            recorder.version_verified(
                1,
                version_outcomes[1],
                {STORE_PATH: FIXTURE_STORE_FAMILY_DEFECTIVE.encode()},
            )
            llm = _ScriptedLLM([FAMILY_FIX])
            with mock.patch.object(planner_module, "call_llm", llm):
                result = runner._direct_repair_pass(
                    "run-family",
                    plan,
                    parent_version=1,
                    failures=failing,
                    goal_text="fix the pairing store",
                    declared_targets_all=set(),
                    version_outcomes=version_outcomes,
                    version_measurements_map={1: []},
                    version_probe_results={1: list(results)},
                    version_snapshots={},
                    repair_candidate_versions=set(),
                    lineage_recorder=recorder,
                )
            # ONE model call repaired the family — not one per defect.
            self.assertEqual(len(llm.prompts), 1)
            prompt = llm.prompts[0]
            self.assertIn("SCOPE — the complete measurement set", prompt)
            self.assertIn("store_expiry_wipe", prompt)
            self.assertIn("store_claim_burn_persists", prompt)
            # The passing sibling rode along as a constraint.
            self.assertIn("PASSING  flow store_corrupt_raises", prompt)
            self.assertIsNotNone(result)
            self.assertEqual(result["version"], 2)
            self.assertEqual(result["failures"], [])
            candidate = version_outcomes[2]
            self.assertEqual(candidate["probe_green"], 4)
            self.assertEqual(candidate["failed_nodes"], 2)
            recorder.finish(
                shipping=None,
                escalation=None,
                decision_history=[],
                attempts=[],
                goal_verdict="FAIL",
                goal_detail="",
                plan_versions=2,
            )
            record = json.loads(
                (run_store.run_dir("run-family") / "lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(record["obligations"]), 1)
            obligations = record["obligations"][0]["obligations"]
            # One lineage obligation per member defect, sharing
            # the family's single execution.
            self.assertEqual(
                [item["defect_id"] for item in obligations],
                [CLAIM_DEFECT, EXPIRY_DEFECT],
            )
            for item in obligations:
                self.assertEqual(item["status"], "patched")
                self.assertEqual(len(item["attempts"]), 1)
                self.assertEqual(
                    item["family"], [CLAIM_DEFECT, EXPIRY_DEFECT]
                )
                self.assertEqual(item["family_primary"], CLAIM_DEFECT)
                self.assertEqual(len(item["scope"]), 3)
            for defect in (CLAIM_DEFECT, EXPIRY_DEFECT):
                row = record["defects"][defect]
                self.assertTrue(row["verified_fixed"])
                self.assertEqual(row["verified_fixed_version"], 2)
                self.assertEqual(
                    row["direct_repair"]["family"],
                    [CLAIM_DEFECT, EXPIRY_DEFECT],
                )
                self.assertEqual(row["direct_repair"]["scope_measured"], 3)
            # The passing sibling was never diagnosed, never an
            # obligation.
            self.assertNotIn(CORRUPT_DEFECT, record["defects"])
            table_md = (
                run_store.run_dir("run-family") / "lineage.md"
            ).read_text(encoding="utf-8")
            self.assertIn("family of 2 failing, 3 measured", table_md)


if __name__ == "__main__":
    unittest.main()
