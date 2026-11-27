"""Unit tests for the defect-lineage recorder (campaign build #29).

The recorder is observability only: these tests pin its identity
derivation (defect ids come from probe records, never from prose),
its sink/buffer behavior (the build #9/#10 pattern carried to fill
prompts), the per-defect stage assembly (the lineage table and its
first Yes->No transition), and its core discipline — a recorder
failure can never fail the run it observes.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.lineage import (
    LineageRecorder,
    defect_id_for_record,
    drain_pending_lineage,
    lineage_sink,
    record_fill_outcome,
    record_fill_prompt,
    snapshot_hashes,
    tree_hash_for,
)
from freetalon.orchestrator.run_store import RunStore

STORE = "/repo/freetalon/pairing/store.py"
SERVER = "/repo/freetalon/pairing/server.py"

D1 = "C06-expiry::store_behavior::store_expiry_wipe"
D2 = "C09-http-api-server-py::method_matrix::method_matrix_get_known"


def _fail(clause: str, probe: str, flow: str, file: str, detail: str):
    return {
        "clause_id": clause,
        "probe": probe,
        "ok": False,
        "detail": detail,
        "flow": flow,
        "file": file,
    }


def _ok(clause: str, probe: str, flow: str, file: str):
    return {
        "clause_id": clause,
        "probe": probe,
        "ok": True,
        "detail": "ok",
        "flow": flow,
        "file": file,
    }


D1_FAIL = _fail(
    "C06-expiry",
    "store_behavior",
    "store_expiry_wipe",
    "freetalon/pairing/store.py",
    "the sentinel credential is still present after the expiry poll",
)
D2_FAIL = _fail(
    "C09-http-api-server-py",
    "method_matrix",
    "method_matrix_get_known",
    "freetalon/pairing/server.py",
    "method_matrix_get_known: GET /pairing/register -> 404",
)
D1_OK = _ok(
    "C06-expiry",
    "store_behavior",
    "store_expiry_wipe",
    "freetalon/pairing/store.py",
)
D2_OK = _ok(
    "C09-http-api-server-py",
    "method_matrix",
    "method_matrix_get_known",
    "freetalon/pairing/server.py",
)


def _node(node_id: str, implements=(), verifies=()):
    return {
        "id": node_id,
        "objective": f"objective of {node_id}",
        "assigned_claw": "scaffold",
        "implements": list(implements),
        "verifies": list(verifies),
        "responds_to": None,
        "differs_by": None,
        "has_content": True,
        "content_sha256": "0" * 64,
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DefectIdentityTests(unittest.TestCase):
    def test_id_from_clause_probe_flow(self):
        self.assertEqual(defect_id_for_record(D1_FAIL), D1)
        self.assertEqual(defect_id_for_record(D2_FAIL), D2)

    def test_id_without_flow_is_clause_and_probe(self):
        record = {"clause_id": "C01-x", "probe": "declared_files", "ok": False}
        self.assertEqual(
            defect_id_for_record(record), "C01-x::declared_files"
        )

    def test_tree_hash_distinguishes_content_and_absence(self):
        base = {STORE: _sha("a"), SERVER: _sha("b")}
        self.assertEqual(tree_hash_for(base), tree_hash_for(dict(base)))
        changed = {STORE: _sha("a2"), SERVER: _sha("b")}
        self.assertNotEqual(tree_hash_for(base), tree_hash_for(changed))
        absent = {STORE: None, SERVER: _sha("b")}
        self.assertNotEqual(tree_hash_for(base), tree_hash_for(absent))
        self.assertIsNone(tree_hash_for({}))

    def test_snapshot_hashes(self):
        hashes = snapshot_hashes({STORE: b"a", SERVER: None})
        self.assertEqual(hashes[STORE], _sha("a"))
        self.assertIsNone(hashes[SERVER])
        self.assertEqual(snapshot_hashes(None), {})


class SinkTests(unittest.TestCase):
    def test_prompt_record_reaches_installed_sink(self):
        seen: list[dict] = []
        with lineage_sink(seen.append):
            record_fill_prompt(
                phase="replan_content_fill",
                attempt=1,
                node_id="node-1",
                target="x.py",
                intent="whole_file",
                prompt="THE PROMPT",
                system_prompt="THE SYSTEM",
                existing_source="old",
            )
        self.assertEqual(len(seen), 1)
        record = seen[0]
        self.assertEqual(record["kind"], "fill_prompt")
        self.assertEqual(record["prompt"], "THE PROMPT")
        self.assertEqual(record["existing_source_sha256"], _sha("old"))

    def test_initial_phase_buffers_and_drains(self):
        # No sink installed: an initial content_fill record waits in
        # the pending buffer; a replan-phase record does not.
        record_fill_prompt(
            phase="content_fill",
            attempt=1,
            node_id="node-1",
            target="x.py",
            intent="create",
            prompt="P1",
            system_prompt="S",
            existing_source=None,
        )
        record_fill_prompt(
            phase="replan_content_fill",
            attempt=1,
            node_id="node-2",
            target="y.py",
            intent="create",
            prompt="P2",
            system_prompt="S",
            existing_source=None,
        )
        seen: list[dict] = []
        delivered = drain_pending_lineage(seen.append)
        self.assertEqual(delivered, 1)
        self.assertEqual(seen[0]["prompt"], "P1")
        # The buffer is cleared by the drain.
        self.assertEqual(drain_pending_lineage(seen.append), 0)

    def test_fill_outcome_shapes(self):
        seen: list[dict] = []
        with lineage_sink(seen.append):
            record_fill_outcome(
                phase="content_fill",
                attempt=1,
                node_id="node-1",
                target="x.py",
                intent="whole_file",
                outcome="accepted",
                candidate_inputs={"content": "same"},
                existing_source="same",
            )
            record_fill_outcome(
                phase="content_fill",
                attempt=1,
                node_id="node-2",
                target="y.py",
                intent="edit",
                outcome="accepted",
                candidate_inputs={"edits": [{"search": "a", "replace": "b"}]},
                existing_source="a",
            )
        self.assertTrue(seen[0]["content_equals_existing"])
        self.assertEqual(seen[0]["content_sha256"], _sha("same"))
        self.assertIsNone(seen[1]["content_equals_existing"])
        self.assertEqual(seen[1]["edits_count"], 1)


class RecorderAssemblyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name) / "runs")
        self.run_id = "run-test-lineage"
        self.recorder = LineageRecorder(self.store, self.run_id)

    def tearDown(self):
        self._tmp.cleanup()

    def _read_lineage(self) -> dict:
        path = self.store.run_dir(self.run_id) / "lineage.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_full_run_assembly(self):
        r = self.recorder
        # Version 1: baseline, plan, execution, diagnoses D1 + D2.
        r.note_baseline({STORE: b"old store", SERVER: b"old server"})
        r.plan_written(1, [_node("node-1", implements=["C01-other"])])
        r.version_executed(
            1,
            [
                {
                    "id": "node-1",
                    "status_before": "draft",
                    "status_after": "completed",
                    "error": None,
                    "verdict": "PASS",
                    "exit_code": 0,
                    "duration_s": 1.0,
                    "path": STORE,
                    "bytes_written": 9,
                }
            ],
        )
        r.diagnosis(1, [D1_FAIL, D2_FAIL], [D1_FAIL, D2_FAIL])
        outcome1 = {
            "version": 1,
            "probe_green": 45,
            "probe_total": 47,
            "failed_nodes": 0,
            "failed_criteria": 0,
        }
        r.version_verified(
            1, outcome1, {STORE: b"old store", SERVER: b"old server"}
        )
        # The re-plan's fills: node-9 (D1's clause) is filled with
        # content byte-identical to what stands — a no-change fill;
        # node-10 (D2's clause) is filled with a real change.
        r.handle_sink_record(
            {
                "kind": "fill_outcome",
                "phase": "replan_content_fill",
                "attempt": 1,
                "node_id": "node-9",
                "target": "freetalon/pairing/store.py",
                "intent": "whole_file",
                "outcome": "accepted",
                "materialized_from": "no_change",
                "content_sha256": _sha("old store"),
                "content_equals_existing": True,
                "edits_count": None,
                "has_anchor": False,
                "errors": [],
            }
        )
        r.handle_sink_record(
            {
                "kind": "fill_outcome",
                "phase": "replan_content_fill",
                "attempt": 1,
                "node_id": "node-10",
                "target": "freetalon/pairing/server.py",
                "intent": "whole_file",
                "outcome": "accepted",
                "materialized_from": None,
                "content_sha256": _sha("new server"),
                "content_equals_existing": False,
                "edits_count": None,
                "has_anchor": False,
                "errors": [],
            }
        )
        r.plan_accepted(
            2,
            source="replan_from_brief",
            nodes=[
                _node("node-9", implements=["C06-expiry"]),
                _node("node-10", implements=["C09-http-api-server-py"]),
            ],
            coverage_writeback={"required": ["C06-expiry"]},
            plan_emission=None,
            repair_declines=[],
            seams_bypassed=None,
        )
        r.plan_written(
            2,
            [
                _node("node-9", implements=["C06-expiry"]),
                _node("node-10", implements=["C09-http-api-server-py"]),
            ],
        )
        r.version_executed(
            2,
            [
                {
                    "id": "node-9",
                    "status_before": "draft",
                    "status_after": "completed",
                    "error": None,
                    "verdict": "PASS",
                    "exit_code": 0,
                    "duration_s": 1.0,
                    "path": STORE,
                    "bytes_written": 9,
                },
                {
                    "id": "node-10",
                    "status_before": "draft",
                    "status_after": "completed",
                    "error": None,
                    "verdict": "PASS",
                    "exit_code": 0,
                    "duration_s": 1.0,
                    "path": SERVER,
                    "bytes_written": 10,
                },
            ],
        )
        # D2 fixed in v2; D1 still failing.
        r.diagnosis(2, [D1_FAIL, D2_OK], [D1_FAIL])
        outcome2 = {
            "version": 2,
            "probe_green": 46,
            "probe_total": 47,
            "failed_nodes": 0,
            "failed_criteria": 0,
        }
        r.version_verified(
            2, outcome2, {STORE: b"old store", SERVER: b"new server"}
        )
        shipping = {
            "shipped_version": 2,
            "final_version": 2,
            "restored": False,
            "versions": [outcome1, outcome2],
            "lost_checks": [],
        }
        r.shipping_settled(shipping, [outcome1, outcome2])
        r.finish(
            shipping=shipping,
            escalation="re-plan budget spent",
            decision_history=[
                {"version": 1, "decision": "replan", "reason": "probes"}
            ],
            attempts=[],
            goal_verdict="PARTIAL",
            goal_detail="",
            plan_versions=2,
        )

        record = self._read_lineage()
        self.assertEqual(record["shipped_version"], 2)
        self.assertIsNotNone(record["shipped_tree_hash"])
        candidates = {c["version"]: c for c in record["candidates"]}
        self.assertEqual(candidates[1]["introduced_defects"], [])
        self.assertEqual(candidates[1]["resolved_defects"], [])
        self.assertEqual(candidates[2]["resolved_defects"], [D2])
        self.assertEqual(
            candidates[2]["parent_tree_hash"], candidates[1]["tree_hash"]
        )
        self.assertNotEqual(
            candidates[2]["tree_hash"], candidates[1]["tree_hash"]
        )

        rows = {row["defect_id"]: row for row in record["table"]}
        d2 = rows[D2]
        self.assertTrue(d2["planned"])
        self.assertEqual(d2["planned_version"], 2)
        self.assertTrue(d2["scheduled"])
        self.assertTrue(d2["generated_diff"])
        self.assertTrue(d2["verified_fixed"])
        self.assertEqual(d2["verified_fixed_version"], 2)
        self.assertTrue(d2["shipped_fixed"])
        self.assertIsNone(d2["first_no_transition"])

        d1 = rows[D1]
        self.assertTrue(d1["planned"])
        self.assertTrue(d1["scheduled"])
        # The no-change fill and the unchanged snapshot agree: no
        # generation witness exists for D1 in v2.
        self.assertFalse(d1["generated_diff"])
        self.assertFalse(d1["verified_fixed"])
        self.assertFalse(d1["shipped_fixed"])
        self.assertEqual(d1["first_no_transition"], "generated_diff")

        table_md = (
            self.store.run_dir(self.run_id) / "lineage.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Diagnosed", table_md)
        self.assertIn(D1, table_md)
        self.assertIn("generated_diff", table_md)
        # The streamed events exist too.
        events = self.store.read_lineage(self.run_id)
        kinds = [event["kind"] for event in events]
        self.assertIn("diagnosis", kinds)
        self.assertIn("candidate", kinds)
        self.assertIn("shipping", kinds)

    def test_never_planned_defect_first_no_is_planned(self):
        r = self.recorder
        r.plan_written(1, [_node("node-1")])
        r.diagnosis(1, [D1_FAIL], [D1_FAIL])
        outcome1 = {
            "version": 1,
            "probe_green": 46,
            "probe_total": 47,
            "failed_nodes": 0,
            "failed_criteria": 0,
        }
        r.version_verified(1, outcome1, {STORE: b"old store"})
        r.finish(
            shipping=None,
            escalation="done",
            decision_history=[],
            attempts=[],
            goal_verdict="PARTIAL",
            goal_detail="",
            plan_versions=1,
        )
        record = self._read_lineage()
        # No settlement: the single executed version's tree stands.
        self.assertEqual(record["shipped_version"], 1)
        row = record["table"][0]
        self.assertEqual(row["defect_id"], D1)
        self.assertFalse(row["planned"])
        self.assertEqual(row["first_no_transition"], "planned")

    def test_prompt_files_are_saved_and_referenced_by_hash(self):
        r = self.recorder
        r.handle_sink_record(
            {
                "kind": "fill_prompt",
                "phase": "content_fill",
                "attempt": 1,
                "node_id": "node-1",
                "target": "x.py",
                "intent": "create",
                "prompt": "THE EXACT PROMPT",
                "system_prompt": "SYS",
                "existing_source_sha256": None,
                "existing_source_chars": None,
            }
        )
        events = self.store.read_lineage(self.run_id)
        prompt_events = [e for e in events if e["kind"] == "fill_prompt"]
        self.assertEqual(len(prompt_events), 1)
        event = prompt_events[0]
        self.assertNotIn("prompt", event)
        self.assertEqual(event["prompt_sha256"], _sha("THE EXACT PROMPT"))
        saved = (
            self.store.run_dir(self.run_id) / event["prompt_file"]
        ).read_text(encoding="utf-8")
        self.assertEqual(saved, "THE EXACT PROMPT")

    def test_recorder_never_raises_on_store_failure(self):
        class ExplodingStore(RunStore):
            def append_lineage(self, run_id, record):  # noqa: D102
                raise RuntimeError("disk gone")

        store = ExplodingStore(Path(self._tmp.name) / "runs2")
        recorder = LineageRecorder(store, "run-exploding")
        # None of these may raise.
        recorder.note_baseline({STORE: b"x"})
        recorder.plan_written(1, [_node("node-1")])
        recorder.diagnosis(1, [D1_FAIL], [D1_FAIL])
        recorder.version_verified(
            1,
            {
                "version": 1,
                "probe_green": 0,
                "probe_total": 1,
                "failed_nodes": 0,
                "failed_criteria": 0,
            },
            {STORE: b"x"},
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
        recorder.finalize()


class RunStoreLineageTests(unittest.TestCase):
    def test_lineage_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            store.append_lineage("run-x", {"kind": "one"})
            store.append_lineage("run-x", {"kind": "two"})
            self.assertEqual(
                [r["kind"] for r in store.read_lineage("run-x")],
                ["one", "two"],
            )
            rel = store.write_lineage_prompt("run-x", "0001.txt", "PROMPT")
            self.assertEqual(rel, "lineage_prompts/0001.txt")
            self.assertEqual(
                (store.run_dir("run-x") / rel).read_text(encoding="utf-8"),
                "PROMPT",
            )
            store.write_lineage("run-x", {"run_id": "run-x"})
            store.write_lineage_table("run-x", "# table")
            self.assertEqual(
                json.loads(
                    (store.run_dir("run-x") / "lineage.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {"run_id": "run-x"},
            )


if __name__ == "__main__":
    unittest.main()
