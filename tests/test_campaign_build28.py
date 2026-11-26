"""Campaign build #28 (2026-11-26): re-plan clause-id fidelity
and declaration-shape stability across re-ask attempts.

Arm #36 (run-20261126T200249Z-edbe52): build #27's probe-gated
guidance worked — re-plan attempt 2 declared C06/C08 exactly
correctly (implements on the store-repair node, verifies on a
check node, frozen ids, object mapping on every node) and drew
no C06/C08 error. The re-plan still never landed:

- Attempt 2's five errors were clause-id fidelity failures on
  OTHER clauses: truncated ids (C02-status-vocabulary for the
  frozen C02-status-vocabulary-exact-strings-no-synonyms,
  C07-store-api for C07-store-api-store-py-and-exceptions) and
  re-slugged ids (C10-make-server, C11-env-only-config,
  C12-node-self-registration — the clause number kept, the slug
  invented; the frozen C10/C11/C12 are different clauses).
- Attempts 1 and 3 regressed on declaration SHAPE (absent
  mappings, a flat list instead of the object mapping), as if
  each re-ask carried only the last error delta: the repair
  prompt re-states the pinned module contract and the NODE
  SHAPE next to the rejection, but never the clause declaration
  contract itself.

The fix, at the campaign's proven seams:

(a) Deterministic emission-side repair (builds #11/#26): at
    parse, an emitted clause id that is not frozen but is a
    strict prefix of exactly one frozen id is normalized to
    that frozen id, in place, before validation; each repair
    is recorded under the system-owned metadata key
    clause_id_normalizations and rides rejections on the
    attach channel. Ambiguity (a prefix of two or more frozen
    ids) is never guessed, and a re-slugged id is not a prefix
    — it stays an error, so no coverage is ever fabricated.
(b) Fact-carrying invented-id errors (build #27): when an
    unknown id's clause number matches exactly one frozen
    id's number, the error names that frozen id verbatim and
    states the copy-verbatim rule. No number twin: the error
    text is byte-identical to before this build.
(c) Re-ask restatement: a repair prompt in a clauses-carrying
    planning call re-states the full declaration contract —
    format_replan_clauses_block in delta mode,
    format_clauses_block in full mode, including build #27's
    probe-gated lines — adjacent to the rejection. Clauses-free
    calls render exactly as before.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    clause_coverage_errors,
    clause_delta_errors,
    normalize_clause_id_declarations,
)
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import PlanNode
from freetalon.orchestrator.planner import parse_planner_response

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

# Arm #36's frozen clause list, verbatim from the run record's
# rejection texts (the pairing CONTRACT.md at that head).
CLAUSES_12 = [
    {"id": "C01-roles", "heading": "Roles"},
    {
        "id": "C02-status-vocabulary-exact-strings-no-synonyms",
        "heading": "Status vocabulary",
    },
    {"id": "C03-record-fields", "heading": "Record fields"},
    {"id": "C04-validation", "heading": "Validation"},
    {"id": "C05-clock-seam", "heading": "Clock seam"},
    {"id": "C06-expiry", "heading": "Expiry"},
    {
        "id": "C07-store-api-store-py-and-exceptions",
        "heading": "Store API (store.py) and exceptions",
    },
    {"id": "C08-persistence", "heading": "Persistence"},
    {"id": "C09-http-api-server-py", "heading": "HTTP API (server.py)"},
    {
        "id": "C10-server-construction-server-py",
        "heading": "Server construction (server.py)",
    },
    {"id": "C11-configuration-env-only", "heading": "Configuration"},
    {
        "id": "C12-deliberate-deviation-from-design-md-6-s-letter",
        "heading": "Deliberate deviation from DESIGN.md section 6",
    },
]

REQUIRED = ["C06-expiry", "C08-persistence", "C09-http-api-server-py"]

STORE_PROBE_FAILURES = [
    {
        "clause_id": "C06-expiry",
        "probe": "store_behavior",
        "ok": False,
        "detail": "the on-disk file still carries the credential bundle after expiry",
        "flow": "store_expiry_wipe",
        "file": "freetalon/pairing/store.py",
    },
    {
        "clause_id": "C08-persistence",
        "probe": "store_behavior",
        "ok": False,
        "detail": "a corrupt store file was silently reset instead of raising",
        "flow": "store_corrupt_raises",
        "file": "freetalon/pairing/store.py",
    },
]


def _raw_node(node_id, clauses, depends_on=None):
    return {
        "id": node_id,
        "objective": f"Step {node_id}",
        "depends_on": depends_on or [],
        "assigned_claw": "bash",
        "inputs": {"command": "true", "clauses": clauses},
        "outputs": [],
        "acceptance": ["The step's effect holds."],
        "criteria": [{"type": "exit_code", "equals": 0}],
        "error": None,
    }


def _attempt2_nodes(step5_implements):
    """Arm #36 re-plan attempt 2's declarations, verbatim: the
    shape is exactly right; step_4 truncates C02/C07; step_5's
    server-side ids are the caller's to supply."""
    empty = {"implements": [], "verifies": []}
    return [
        _raw_node("step_1", dict(empty)),
        _raw_node("step_2", dict(empty), ["step_1"]),
        _raw_node("step_3", dict(empty), ["step_1"]),
        _raw_node(
            "step_4",
            {
                "implements": [
                    "C02-status-vocabulary",
                    "C03-record-fields",
                    "C04-validation",
                    "C05-clock-seam",
                    "C06-expiry",
                    "C07-store-api",
                    "C08-persistence",
                ],
                "verifies": [],
            },
            ["step_2"],
        ),
        _raw_node(
            "step_5",
            {"implements": step5_implements, "verifies": []},
            ["step_3"],
        ),
        _raw_node(
            "step_6",
            {
                "implements": [],
                "verifies": ["C06-expiry", "C08-persistence"],
            },
            ["step_4"],
        ),
        _raw_node(
            "step_7",
            {"implements": [], "verifies": ["C09-http-api-server-py"]},
            ["step_5"],
        ),
        _raw_node("step_8", dict(empty), ["step_6", "step_7"]),
    ]


RESLUGGED_STEP5 = [
    "C09-http-api-server-py",
    "C10-make-server",
    "C11-env-only-config",
    "C12-node-self-registration",
]

EXACT_STEP5 = [
    "C09-http-api-server-py",
    "C10-server-construction-server-py",
    "C11-configuration-env-only",
    "C12-deliberate-deviation-from-design-md-6-s-letter",
]


def _payload(nodes) -> str:
    return json.dumps({"nodes": nodes, "metadata": {}})


def _parse_delta(nodes, **kwargs):
    return parse_planner_response(
        _payload(nodes),
        clauses=CLAUSES_12,
        clause_mode="delta",
        clause_required=REQUIRED,
        clause_probe_failures=STORE_PROBE_FAILURES,
        skeleton=True,
        **kwargs,
    )


def _plan_node(node_id, clauses) -> PlanNode:
    return PlanNode(
        id=node_id,
        objective=f"step {node_id}",
        assigned_claw="bash",
        inputs={"command": "true", "clauses": clauses},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the step holds",
    )


# ── Part 1: truncation normalization at parse (P1, P2) ──────────


class TestTruncationNormalization(unittest.TestCase):
    def test_attempt2_shape_rejected_only_on_reslugged_ids(self):
        # P1: pre-fix this shape drew 5 unknown-id errors; the two
        # truncations now normalize deterministically, leaving
        # exactly the three re-slugged ids as errors.
        with self.assertRaises(LLMResponseError) as caught:
            _parse_delta(_attempt2_nodes(RESLUGGED_STEP5))
        exc = caught.exception
        unknown = [
            record.message
            for record in exc.plan_errors
            if "not in the frozen clause list" in record.message
        ]
        self.assertEqual(len(unknown), 3, exc.plan_errors)
        joined = "\n".join(unknown)
        self.assertIn("C10-make-server", joined)
        self.assertIn("C11-env-only-config", joined)
        self.assertIn("C12-node-self-registration", joined)
        self.assertNotIn(
            "declares contract clause 'C02-status-vocabulary'", joined
        )
        self.assertNotIn(
            "declares contract clause 'C07-store-api'", joined
        )
        records = getattr(exc, "clause_id_normalizations", None)
        self.assertIsNotNone(records)
        by_from = {r["from"]: r for r in records}
        self.assertEqual(
            set(by_from), {"C02-status-vocabulary", "C07-store-api"}
        )
        self.assertEqual(
            by_from["C02-status-vocabulary"]["to"],
            "C02-status-vocabulary-exact-strings-no-synonyms",
        )
        self.assertEqual(
            by_from["C07-store-api"]["to"],
            "C07-store-api-store-py-and-exceptions",
        )
        self.assertTrue(
            all(r["role"] == "implements" for r in records), records
        )

    def test_attempt2_with_exact_ids_accepted_and_records_stamped(self):
        # P1 (acceptance half): with step_5's ids exact, the whole
        # attempt-2 shape parses; the nodes carry the frozen ids
        # and the metadata stamps exactly the two repairs.
        nodes, metadata = _parse_delta(_attempt2_nodes(EXACT_STEP5))
        step4 = next(n for n in nodes if n.objective == "Step step_4")
        implements = step4.inputs["clauses"]["implements"]
        self.assertIn(
            "C02-status-vocabulary-exact-strings-no-synonyms", implements
        )
        self.assertIn(
            "C07-store-api-store-py-and-exceptions", implements
        )
        self.assertNotIn("C02-status-vocabulary", implements)
        self.assertNotIn("C07-store-api", implements)
        records = metadata.get("clause_id_normalizations")
        self.assertIsNotNone(records)
        self.assertEqual(
            {r["from"] for r in records},
            {"C02-status-vocabulary", "C07-store-api"},
        )

    def test_exact_ids_produce_no_records(self):
        # P2: a declaration already in frozen form is untouched —
        # no metadata key, no record.
        nodes, metadata = _parse_delta(
            [
                _raw_node(
                    "step_1",
                    {
                        "implements": ["C06-expiry", "C08-persistence",
                                       "C09-http-api-server-py"],
                        "verifies": [],
                    },
                ),
                _raw_node(
                    "step_2",
                    {
                        "implements": [],
                        "verifies": ["C06-expiry", "C08-persistence",
                                     "C09-http-api-server-py"],
                    },
                    ["step_1"],
                ),
            ]
        )
        self.assertNotIn("clause_id_normalizations", metadata)
        self.assertEqual(len(nodes), 2)

    def test_reslugged_id_is_never_normalized(self):
        # P2: C12-node-self-registration shares only its number
        # with the frozen C12 — normalizing it would fabricate
        # coverage of a clause the model never declared.
        node = _plan_node(
            "node-1",
            {"implements": ["C12-node-self-registration"], "verifies": []},
        )
        records = normalize_clause_id_declarations([node], CLAUSES_12)
        self.assertEqual(records, [])
        self.assertEqual(
            node.inputs["clauses"]["implements"],
            ["C12-node-self-registration"],
        )

    def test_ambiguous_prefix_is_never_normalized(self):
        # P2: "C1" prefixes C10/C11/C12 — three candidates, so the
        # repair must not guess; the id stays an error.
        node = _plan_node(
            "node-1", {"implements": ["C1"], "verifies": []}
        )
        records = normalize_clause_id_declarations([node], CLAUSES_12)
        self.assertEqual(records, [])
        self.assertEqual(node.inputs["clauses"]["implements"], ["C1"])
        synthetic = [
            {"id": "C01-alpha", "heading": "A"},
            {"id": "C01-alpha-beta", "heading": "AB"},
        ]
        node2 = _plan_node(
            "node-2", {"implements": ["C01-al"], "verifies": []}
        )
        self.assertEqual(
            normalize_clause_id_declarations([node2], synthetic), []
        )
        # ...while a prefix of exactly one of them does repair.
        node3 = _plan_node(
            "node-3", {"implements": ["C01-alpha-b"], "verifies": []}
        )
        records3 = normalize_clause_id_declarations([node3], synthetic)
        self.assertEqual(len(records3), 1)
        self.assertEqual(records3[0]["to"], "C01-alpha-beta")
        self.assertEqual(
            node3.inputs["clauses"]["implements"], ["C01-alpha-beta"]
        )

    def test_verifies_role_normalizes_and_dedupes(self):
        # A truncated id in "verifies" repairs in that role; a
        # list carrying both the truncation and the full id
        # collapses to the one frozen id.
        node = _plan_node(
            "node-1",
            {
                "implements": [],
                "verifies": ["C06-expiry", "C08-persistence"],
            },
        )
        node2 = _plan_node(
            "node-2",
            {
                "implements": ["C07-store-api",
                               "C07-store-api-store-py-and-exceptions"],
                "verifies": [],
            },
        )
        records = normalize_clause_id_declarations([node, node2], CLAUSES_12)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["from"], "C07-store-api")
        self.assertEqual(
            node2.inputs["clauses"]["implements"],
            ["C07-store-api-store-py-and-exceptions"],
        )


# ── Part 2: fact-carrying invented-id errors (P3) ───────────────


class TestUnknownIdErrorText(unittest.TestCase):
    def test_number_twin_is_named(self):
        # P3: each re-slugged id's error names the frozen clause
        # that carries its number, verbatim.
        node = _plan_node(
            "node-1",
            {
                "implements": [
                    "C10-make-server",
                    "C11-env-only-config",
                    "C12-node-self-registration",
                ],
                "verifies": [],
            },
        )
        errors = clause_delta_errors([node], CLAUSES_12, [])
        self.assertEqual(len(errors), 3, errors)
        joined = "\n".join(errors)
        self.assertIn(
            "the frozen clause numbered C10 is "
            "'C10-server-construction-server-py'",
            joined,
        )
        self.assertIn(
            "the frozen clause numbered C11 is "
            "'C11-configuration-env-only'",
            joined,
        )
        self.assertIn(
            "the frozen clause numbered C12 is "
            "'C12-deliberate-deviation-from-design-md-6-s-letter'",
            joined,
        )
        self.assertIn("copy clause ids verbatim", joined)

    def test_no_twin_error_is_byte_identical_in_both_rules(self):
        # P3/P4: C99 has no frozen twin — both rules keep the
        # exact pre-build message.
        node = _plan_node(
            "node-1", {"implements": ["C99-invented"], "verifies": []}
        )
        expected = (
            "node 'node-1' declares contract clause 'C99-invented', "
            "which is not in the frozen clause list ("
            + ", ".join(sorted(c["id"] for c in CLAUSES_12))
            + ") — clause ids are system-extracted from the contract "
            "and cannot be invented"
        )
        self.assertEqual(
            clause_delta_errors([node], CLAUSES_12, []), [expected]
        )
        full = clause_coverage_errors([node], CLAUSES_12)
        self.assertIn(expected, full)


# ── Part 3: the re-ask re-states the declaration contract (P4) ──


class TestReaskRestatesClausesBlock(unittest.TestCase):
    def setUp(self) -> None:
        planner_module.drain_pending_planner_attempts(lambda record: None)
        self.addCleanup(
            planner_module.drain_pending_planner_attempts,
            lambda record: None,
        )

    def _run(self, responses, **kwargs):
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            index = min(len(calls) - 1, len(responses) - 1)
            return responses[index]

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module._call_llm_with_repair(
                "system", "user", SETTINGS, **kwargs
            )
        return calls, result

    def test_delta_repair_prompt_restates_full_block(self):
        # Attempt 1 shape (arm #36 attempts 1/3): no mappings at
        # all — rejected. Attempt 2: a conforming delta.
        shapeless = [
            {
                "id": "step_1",
                "objective": "Step step_1",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "true"},
                "outputs": [],
                "acceptance": ["The step's effect holds."],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            }
        ]
        conforming = [
            _raw_node(
                "step_1",
                {
                    "implements": ["C06-expiry", "C08-persistence",
                                   "C09-http-api-server-py"],
                    "verifies": [],
                },
            ),
            _raw_node(
                "step_2",
                {
                    "implements": [],
                    "verifies": ["C06-expiry", "C08-persistence",
                                 "C09-http-api-server-py"],
                },
                ["step_1"],
            ),
        ]
        calls, (_nodes, _metadata) = self._run(
            [_payload(shapeless), _payload(conforming)],
            clauses=CLAUSES_12,
            clause_mode="delta",
            clause_required=REQUIRED,
            clause_probe_failures=STORE_PROBE_FAILURES,
            skeleton=True,
            attempt_phase="replan_skeleton",
        )
        self.assertGreaterEqual(len(calls), 2)
        repair_prompt = calls[1][1]
        # The full declaration contract rides next to the
        # rejection: frozen list verbatim, the per-node rule, and
        # build #27's probe-gated lines.
        self.assertIn("CONTRACT CLAUSES (re-plan)", repair_prompt)
        self.assertIn(
            "C12-deliberate-deviation-from-design-md-6-s-letter",
            repair_prompt,
        )
        self.assertIn("PROBE-GATED open clauses", repair_prompt)
        self.assertIn("store_behavior", repair_prompt)
        self.assertIn("freetalon/pairing/store.py", repair_prompt)
        # ...and it appears AFTER the rejection it answers (the
        # restatement sits with the repair, not only in the
        # original prompt at the top).
        self.assertGreater(
            repair_prompt.rindex("CONTRACT CLAUSES (re-plan)"),
            repair_prompt.index("REJECTED"),
        )

    def test_clauses_free_repair_prompt_has_no_clauses_block(self):
        # P4: a clauses-free planning call renders its repair
        # prompt exactly as before — no CONTRACT CLAUSES block.
        bad = [
            {
                "id": "step_1",
                "objective": "Step step_1",
                "depends_on": ["step_99"],
                "assigned_claw": "bash",
                "inputs": {"command": "true"},
                "outputs": [],
                "acceptance": ["The step's effect holds."],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            }
        ]
        good = [
            {
                "id": "step_1",
                "objective": "Step step_1",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "true"},
                "outputs": [],
                "acceptance": ["The step's effect holds."],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            }
        ]
        calls, _result = self._run([_payload(bad), _payload(good)])
        self.assertGreaterEqual(len(calls), 2)
        self.assertNotIn("CONTRACT CLAUSES", calls[1][1])


if __name__ == "__main__":
    unittest.main()
