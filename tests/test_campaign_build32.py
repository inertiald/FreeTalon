"""Campaign build #32: the direct repair path.

Three arms broke three different links of the planner-mediated
repair chain — arm #38 at fill FORM (build #30's patch form),
arm #39 before fills (build #31's truncation + reroute fixes),
arm #40 before either: the initial plan bound no diagnosed
defect and the run escalated at an approval gate on a plan node
before any re-plan. The frontier consultation's unanimous
answer, and this build: diagnosed defects become repair
obligations executed directly from the probe record — the
planner bypassed, nothing downstream bypassed. The repaired
tree is a candidate version judged by the same probes and the
same selection; only verification resolves a defect.

Tested at the levels the build operates:

- the obligation executor (planner.execute_direct_repair_
  obligation, mocked call_llm): a patch-form payload patches
  the standing file on disk; a no-change patch is a FAILED
  attempt whose retry carries the obligation restated; a
  non-applying patch exhausts the bounded budget with the file
  untouched; a whole-file or anchored payload is a shape
  failure; a missing or escaping target declines with no model
  call at all;
- the lineage record (LineageRecorder.repair_obligations): the
  pass is recorded with its attempts, the defect row carries
  the direct_repair summary, resolution still comes only from
  the candidate's probe status, and lineage.md gains the
  obligations section under an unchanged table;
- the loop pass (LoopRunner._direct_repair_pass, real store
  probes): obligations open per diagnosed defect (deduped by
  defect id, file-less records declined), a changed file
  registers a candidate version with the parent's node-failure
  facts inherited, the candidate out-ranks its parent on probe
  greens, and a pass that changes nothing registers no
  candidate but still records its obligations;
- the gate foreclosure (arm #40 in miniature, a full run_plan):
  a plan whose second node pauses at an approval gate still
  gets its diagnosed store defect repaired — the gate is never
  touched, its handler never runs, the run still waits at
  AWAITING_APPROVAL — and the lineage credits the repair
  candidate with the verified fix.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import run_contract_probes
from freetalon.orchestrator.gates import GateStore
from freetalon.orchestrator.lineage import (
    LineageRecorder,
    defect_id_for_record,
    drain_pending_lineage,
)
from freetalon.orchestrator.loop import LoopRunner, _best_version_outcome
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    RunBudget,
    SuccessMeasure,
    TaskIntent,
)
from freetalon.orchestrator.planner import (
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
    "flow": "store_expiry_wipe",
    "detail": "the measured failure: VALUE must be 3",
    "file": "pkg/mod.py",
}
MOD_DEFECT = "C01-fixture::store_behavior::store_expiry_wipe"
MOD_FIX = json.dumps(
    {"edits": [{"search": "VALUE = 1\n", "replace": "VALUE = 3\n"}]}
)

# A pairing store correct in every probed flow except one: the
# expiry poll computes the wipe in memory and never persists it
# — the diagnostic battery's D1 shape, in miniature. The fix the
# mocked model returns persists the wipe in the same branch.
FIXTURE_STORE_DEFECTIVE = '''"""Fixture pairing store: the expiry wipe is not persisted."""

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
        if self._now() - record["created"] > self._ttl:
            wiped = dict(record, status="expired", credentials=None)
            return PollOutcome(wiped["status"], wiped["credentials"])
        if record["status"] == "confirmed":
            bundle = record["credentials"]
            record["status"] = "claimed"
            record["credentials"] = None
            self._save()
            return PollOutcome("confirmed", bundle)
        return PollOutcome(record["status"])
'''

STORE_FIX = json.dumps(
    {
        "edits": [
            {
                "search": (
                    '            wiped = dict(record, status="expired", '
                    "credentials=None)\n"
                    '            return PollOutcome(wiped["status"], '
                    'wiped["credentials"])\n'
                ),
                "replace": (
                    '            record["status"] = "expired"\n'
                    '            record["credentials"] = None\n'
                    "            self._save()\n"
                    '            return PollOutcome("expired", None)\n'
                ),
            }
        ]
    }
)

STORE_CLAUSE = {
    "id": "C06-pairing-store-expiry",
    "spec": "freetalon/pairing/CONTRACT.md",
    "heading": "Pairing store (store.py)",
    "text": "## Pairing store (store.py)\n",
    "declared_files": ["freetalon/pairing/store.py"],
    "method_surface": False,
}
STORE_PATH = "freetalon/pairing/store.py"
EXPIRY_DEFECT = "C06-pairing-store-expiry::store_behavior::store_expiry_wipe"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _store_tree(root: Path) -> Path:
    _write(root / "freetalon" / "__init__.py", "")
    _write(root / "freetalon" / "pairing" / "__init__.py", "")
    _write(root / STORE_PATH, FIXTURE_STORE_DEFECTIVE)
    return root


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
        # Fill / attempt records buffer in module-global sinks
        # when no recorder is installed; keep tests isolated.
        drain_pending_lineage(lambda record: None)
        drain_pending_planner_attempts(lambda record: None)
        super().tearDown()


# ── Part A: the obligation executor ──────────────────────────────


class TestObligationExecutor(_DrainSinks, unittest.TestCase):
    def _tree(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root / "pkg" / "mod.py", MOD_STANDING)
        return root

    def _run(self, root: Path, llm: _ScriptedLLM, **overrides):
        kwargs = {
            "defect_id": MOD_DEFECT,
            "record": dict(MOD_RECORD),
            "target": "pkg/mod.py",
            "repo_root": root,
            "goal_text": "fix the fixture",
        }
        kwargs.update(overrides)
        with mock.patch.object(planner_module, "call_llm", llm):
            return execute_direct_repair_obligation(**kwargs)

    def test_patch_applies_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            llm = _ScriptedLLM([MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            self.assertTrue(outcome["changed"])
            self.assertEqual(
                (root / "pkg" / "mod.py").read_text(encoding="utf-8"),
                "VALUE = 3\nOTHER = 2\n",
            )
            self.assertEqual(len(outcome["attempts"]), 1)
            self.assertEqual(outcome["attempts"][0]["outcome"], "accepted")
            self.assertNotEqual(
                outcome["before_sha256"], outcome["after_sha256"]
            )
            # The prompt is the battery's condition: the defect's
            # measured evidence and the standing file, no plan.
            self.assertIn("store_expiry_wipe", llm.prompts[0])
            self.assertIn("VALUE = 1", llm.prompts[0])
            self.assertIn("DIRECT REPAIR", llm.prompts[0])

    def test_no_change_patch_fails_then_retry_restates_obligation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            identity = json.dumps(
                {"edits": [{"search": "VALUE = 1\n", "replace": "VALUE = 1\n"}]}
            )
            llm = _ScriptedLLM([identity, MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            self.assertEqual(
                [a["outcome"] for a in outcome["attempts"]],
                ["rejected", "accepted"],
            )
            # The retry prompt restates the measured failure the
            # obligation exists to move.
            self.assertIn("MEASURED failing", llm.prompts[1])
            self.assertIn("the measured failure: VALUE must be 3", llm.prompts[1])

    def test_non_applying_patch_exhausts_with_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            absent = json.dumps(
                {"edits": [{"search": "NOT THERE\n", "replace": "X\n"}]}
            )
            llm = _ScriptedLLM([absent, absent, absent])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "exhausted")
            self.assertEqual(len(outcome["attempts"]), 3)
            self.assertFalse(outcome["changed"])
            self.assertEqual(
                (root / "pkg" / "mod.py").read_text(encoding="utf-8"),
                MOD_STANDING,
            )

    def test_whole_file_payload_is_a_shape_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            whole = json.dumps({"content": "VALUE = 3\nOTHER = 2\n"})
            llm = _ScriptedLLM([whole, MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            self.assertEqual(outcome["attempts"][0]["outcome"], "rejected")
            self.assertIn("REJECTED", llm.prompts[1])
            self.assertIn("NOT accepted", llm.prompts[1])

    def test_anchored_payload_is_a_shape_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            # The anchored whole-replacement form (anchor +
            # content): this path's applier is apply_scoped_edits,
            # so the form is a shape failure here, never applied.
            anchored = json.dumps(
                {"anchor": "VALUE = 1\n", "content": "VALUE = 3\n"}
            )
            llm = _ScriptedLLM([anchored, MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            self.assertEqual(outcome["attempts"][0]["outcome"], "rejected")
            self.assertTrue(
                any(
                    "anchored" in error
                    for error in outcome["attempts"][0]["errors"]
                )
            )

    def test_missing_target_declines_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = _ScriptedLLM([])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "declined")
            self.assertEqual(outcome["reason"], "target_unreadable")
            self.assertEqual(outcome["attempts"], [])
            self.assertEqual(llm.prompts, [])

    def test_target_outside_root_declines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            llm = _ScriptedLLM([])
            outcome = self._run(root, llm, target="../escape.py")
            self.assertEqual(outcome["status"], "declined")
            self.assertEqual(outcome["reason"], "target_outside_root")
            self.assertEqual(llm.prompts, [])


# ── Part B: the lineage record ───────────────────────────────────


class TestObligationLineage(_DrainSinks, unittest.TestCase):
    FAIL = {
        "clause_id": "C06-pairing-store-expiry",
        "probe": "store_behavior",
        "flow": "store_expiry_wipe",
        "ok": False,
        "detail": "the wipe was not persisted",
        "file": STORE_PATH,
    }
    OK = {**FAIL, "ok": True, "detail": "the wipe persisted"}

    def test_obligations_recorded_and_resolution_from_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_store = RunStore(Path(tmp) / "runs")
            recorder = LineageRecorder(run_store, "run-b32")
            outcome1 = {
                "version": 1,
                "probe_green": 3,
                "probe_total": 4,
                "failed_nodes": 1,
                "failed_criteria": 1,
            }
            recorder.note_baseline({STORE_PATH: b"defective store"})
            recorder.diagnosis(1, [self.FAIL], [self.FAIL])
            recorder.version_verified(1, outcome1, {STORE_PATH: b"defective store"})
            obligation = {
                "defect_id": EXPIRY_DEFECT,
                "clause_id": "C06-pairing-store-expiry",
                "probe": "store_behavior",
                "flow": "store_expiry_wipe",
                "target": STORE_PATH,
                "source": "direct",
                "status": "patched",
                "reason": None,
                "detail": "patch applied on attempt 1",
                "attempts": [
                    {
                        "attempt": 1,
                        "outcome": "accepted",
                        "errors": [],
                        "edits_count": 1,
                        "result_sha256": "x",
                    }
                ],
                "changed": True,
                "before_sha256": "a",
                "after_sha256": "x",
            }
            recorder.repair_obligations(
                2, parent_version=1, obligations=[obligation]
            )
            recorder.diagnosis(2, [self.OK], [])
            outcome2 = {
                "version": 2,
                "probe_green": 4,
                "probe_total": 4,
                "failed_nodes": 1,
                "failed_criteria": 1,
                "source": "direct_repair",
                "parent_version": 1,
            }
            recorder.version_verified(2, outcome2, {STORE_PATH: b"fixed store"})
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
                (run_store.run_dir("run-b32") / "lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(record["obligations"]), 1)
            event = record["obligations"][0]
            self.assertEqual(event["source"], "direct")
            self.assertEqual(event["version"], 2)
            self.assertEqual(event["parent_version"], 1)
            row = record["defects"][EXPIRY_DEFECT]
            self.assertEqual(row["direct_repair"]["status"], "patched")
            self.assertEqual(row["direct_repair"]["attempts"], 1)
            self.assertEqual(row["direct_repair"]["candidate_version"], 2)
            # Resolution is the probes' verdict at the candidate,
            # not the obligation's own claim.
            self.assertTrue(row["verified_fixed"])
            self.assertEqual(row["verified_fixed_version"], 2)
            table_md = (
                run_store.run_dir("run-b32") / "lineage.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Direct repair obligations (build #32):", table_md)
            self.assertIn(EXPIRY_DEFECT, table_md)
            # The main table's columns are unchanged.
            self.assertIn(
                "| Defect | Diagnosed | Planned | Scheduled | "
                "Generated diff | Verified fixed | Shipped fixed |",
                table_md,
            )


# ── Parts C + D: the loop ────────────────────────────────────────


def _loop_setup(tmp: str):
    root = Path(tmp)
    store = ExecutionPlanStateStore(root / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    calls: list[str] = []

    async def flash_handler(inputs):
        calls.append("flash")
        return {"status": "success", "metrics": {"flashed": 1}}

    registry.register("flash_ecu", flash_handler)
    run_store = RunStore(root / "runs")
    gates = GateStore(root / "state.db")
    runner = LoopRunner(store, registry, run_store=run_store, gate_store=gates)
    return store, registry, run_store, gates, runner, calls


def _no_replanner(brief):
    raise RuntimeError("re-planner disabled in this test")


class TestDirectRepairPass(_DrainSinks, unittest.TestCase):
    def _pass_kwargs(self, runner, plan, failures):
        return {
            "parent_version": 1,
            "failures": failures,
            "goal_text": "fix the pairing store",
            "declared_targets_all": set(),
            "version_outcomes": {
                1: {
                    "version": 1,
                    "probe_green": 3,
                    "probe_total": 4,
                    "failed_nodes": 2,
                    "failed_criteria": 1,
                }
            },
            "version_measurements_map": {1: []},
            "version_probe_results": {1: []},
            "version_snapshots": {},
            "repair_candidate_versions": set(),
        }

    def test_candidate_registered_ranked_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _store_tree(Path(tmp) / "tree")
            _, _, run_store, _, runner, _ = _loop_setup(tmp)
            plan = ExecutionPlan(
                plan_id="b32-pass",
                intent=TaskIntent(goal="fix the pairing store"),
                nodes=[],
                metadata={
                    "repo_root": str(tree),
                    "contract_clauses": [STORE_CLAUSE],
                },
            )
            failures = run_contract_probes([STORE_CLAUSE], tree)
            failing = [r for r in failures if not r["ok"]]
            self.assertEqual(
                [defect_id_for_record(r) for r in failing], [EXPIRY_DEFECT]
            )
            recorder = LineageRecorder(run_store, "run-pass")
            recorder.note_baseline({STORE_PATH: FIXTURE_STORE_DEFECTIVE.encode()})
            recorder.diagnosis(1, failures, failing)
            kwargs = self._pass_kwargs(runner, plan, failing)
            recorder.version_verified(
                1, kwargs["version_outcomes"][1], {STORE_PATH: FIXTURE_STORE_DEFECTIVE.encode()}
            )
            llm = _ScriptedLLM([STORE_FIX])
            with mock.patch.object(planner_module, "call_llm", llm):
                result = runner._direct_repair_pass(
                    "run-pass", plan, lineage_recorder=recorder, **kwargs
                )
            self.assertIsNotNone(result)
            self.assertEqual(result["version"], 2)
            self.assertEqual(result["failures"], [])
            candidate = kwargs["version_outcomes"][2]
            self.assertEqual(candidate["source"], "direct_repair")
            self.assertEqual(candidate["parent_version"], 1)
            self.assertEqual(candidate["probe_green"], 4)
            # Node-failure facts are inherited, never erased.
            self.assertEqual(candidate["failed_nodes"], 2)
            self.assertEqual(candidate["failed_criteria"], 1)
            self.assertIn(2, kwargs["repair_candidate_versions"])
            snapshot = kwargs["version_snapshots"][2]
            self.assertIn("self._save()", snapshot[STORE_PATH].decode())
            best = _best_version_outcome(
                [kwargs["version_outcomes"][v] for v in (1, 2)]
            )
            self.assertEqual(best["version"], 2)
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
                (run_store.run_dir("run-pass") / "lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            row = record["defects"][EXPIRY_DEFECT]
            self.assertTrue(row["verified_fixed"])
            self.assertEqual(row["verified_fixed_version"], 2)
            self.assertEqual(row["direct_repair"]["attempts"], 1)

    def test_no_change_pass_registers_no_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _store_tree(Path(tmp) / "tree")
            _, _, run_store, _, runner, _ = _loop_setup(tmp)
            plan = ExecutionPlan(
                plan_id="b32-pass-none",
                intent=TaskIntent(goal="fix the pairing store"),
                nodes=[],
                metadata={
                    "repo_root": str(tree),
                    "contract_clauses": [STORE_CLAUSE],
                },
            )
            results = run_contract_probes([STORE_CLAUSE], tree)
            failing = [r for r in results if not r["ok"]]
            recorder = LineageRecorder(run_store, "run-none")
            recorder.diagnosis(1, results, failing)
            kwargs = self._pass_kwargs(runner, plan, failing)
            recorder.version_verified(
                1, kwargs["version_outcomes"][1], {STORE_PATH: FIXTURE_STORE_DEFECTIVE.encode()}
            )
            identity = json.dumps(
                {
                    "edits": [
                        {
                            "search": '        if record is None:\n',
                            "replace": '        if record is None:\n',
                        }
                    ]
                }
            )
            llm = _ScriptedLLM([identity, identity, identity])
            with mock.patch.object(planner_module, "call_llm", llm):
                result = runner._direct_repair_pass(
                    "run-none", plan, lineage_recorder=recorder, **kwargs
                )
            self.assertIsNone(result)
            self.assertNotIn(2, kwargs["version_outcomes"])
            self.assertEqual(
                (tree / STORE_PATH).read_text(encoding="utf-8"),
                FIXTURE_STORE_DEFECTIVE,
            )
            recorder.finish(
                shipping=None,
                escalation=None,
                decision_history=[],
                attempts=[],
                goal_verdict="FAIL",
                goal_detail="",
                plan_versions=1,
            )
            record = json.loads(
                (run_store.run_dir("run-none") / "lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            # No candidate — but the obligations and their three
            # failed attempts are lineage facts.
            self.assertEqual(len(record["obligations"]), 1)
            event = record["obligations"][0]
            self.assertIsNone(event["version"])
            obligation = event["obligations"][0]
            self.assertEqual(obligation["status"], "exhausted")
            self.assertEqual(len(obligation["attempts"]), 3)
            row = record["defects"][EXPIRY_DEFECT]
            self.assertEqual(row["direct_repair"]["status"], "exhausted")
            self.assertFalse(row["verified_fixed"])


class TestGateForeclosure(_DrainSinks, unittest.TestCase):
    def test_repairs_run_while_the_run_waits_at_a_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _store_tree(Path(tmp) / "tree")
            store, _, run_store, gates, runner, calls = _loop_setup(tmp)
            plan = ExecutionPlan(
                plan_id="b32-gate",
                intent=TaskIntent(goal="Flash the ECU; fix the store"),
                nodes=[
                    PlanNode(
                        id="prep",
                        objective="prepare the tune file",
                        assigned_claw="bash",
                        inputs={"command": f"echo tune > {tmp}/tune.bin"},
                        criteria=[
                            {"type": "file_exists", "path": f"{tmp}/tune.bin"}
                        ],
                    ),
                    PlanNode(
                        id="flash",
                        objective="flash the ECU with the tune file",
                        assigned_claw="flash_ecu",
                        inputs={
                            "action": "flash_ecu",
                            "file": f"{tmp}/tune.bin",
                            "risk": "brick",
                        },
                        depends_on=["prep"],
                        reversible=False,
                        criteria=[
                            {
                                "type": "metric",
                                "name": "flashed",
                                "op": ">=",
                                "value": 1,
                            }
                        ],
                    ),
                ],
                metadata={
                    "repo_root": str(tree),
                    "contract_clauses": [STORE_CLAUSE],
                },
            )
            contract = GoalContract(
                objective="Flash the ECU; fix the store",
                success_measures=[
                    SuccessMeasure(
                        name="flashed", metric="flashed", op=">=", value=1
                    )
                ],
                budget=RunBudget(max_replans=0),
            )
            store.save(plan)
            llm = _ScriptedLLM([STORE_FIX])
            with mock.patch.object(planner_module, "call_llm", llm):
                result = runner.run_plan(
                    plan, contract, replan_fn=_no_replanner
                )
            # The gate's semantics are untouched: the run waits,
            # the packet is pending, the handler never ran.
            self.assertEqual(result.plan.status, PlanStatus.AWAITING_APPROVAL)
            self.assertIsNotNone(result.gate_packet_id)
            self.assertEqual(calls, [])
            packet = gates.get(result.gate_packet_id)
            self.assertEqual(packet.status, "pending")
            # And the diagnosed defect was still repaired: the
            # obligation executed against the live tree while the
            # plan waited, and the probes now pass on it.
            repaired = (tree / STORE_PATH).read_text(encoding="utf-8")
            self.assertIn('record["status"] = "expired"', repaired)
            results = run_contract_probes([STORE_CLAUSE], tree)
            self.assertTrue(all(r["ok"] for r in results))
            record = json.loads(
                (run_store.run_dir(result.run_id) / "lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(record["obligations"]), 1)
            row = record["defects"][EXPIRY_DEFECT]
            self.assertEqual(row["direct_repair"]["status"], "patched")
            self.assertTrue(row["verified_fixed"])
            self.assertEqual(row["verified_fixed_version"], 2)


if __name__ == "__main__":
    unittest.main()
