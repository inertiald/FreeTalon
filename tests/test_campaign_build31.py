"""Campaign build #31: make the repair path actually engage.

Build #30's patch-form repair fills were never exercised in
arm #39: the run escalated PARTIAL at two versions when the v3
re-plan died in the skeleton repair loop, and the v2 delta was
produced without a single replan_content_fill. The parent
verified the two blockers against the traces:

- The three rejected replan_skeleton outputs in arm #39's
  planner_attempts.jsonl are all TRUNCATED — they fail json.loads
  only at end-of-input (mid-object / mid-string cuts at 5,903 /
  805 / 276 chars), and the Ollama slot record for the three
  calls is conclusive: prompts of 23,005 / 24,364 / 24,503 tokens
  against a 24,576 window, generation stopping at 1,570 / 212 /
  73 tokens with truncated=1. The re-plan prompt nearly fills
  the window, and the ordinary repair prompt (the full original
  prompt PLUS the rejection apparatus) handed each retry
  strictly less room. Parse called all three "not valid JSON"
  with no structured code, so no steering could address the
  real cause, and the stuck detector escalated them as a
  repeated model defect.

- A node that arrives carrying its content (the pre-filled,
  legacy single-shot shape) was accepted by _fill_plan_contents
  untouched: build #30's discipline lived in _fill_node_payload,
  which pre-filled content never reaches — including the
  convicted case, a repair node whose carried content is the
  standing file byte-for-byte.

The build is tested at the levels it operates:

- the diagnosis (intake.json_truncation_diagnosis): arm #39's
  third response verbatim, mid-string and brackets-open cuts are
  truncation; prose, balanced-but-broken JSON, complete JSON,
  and trailing-junk JSON are not;
- parse: a truncated planner response raises the structured
  response_truncated rejection (schema stage; the message keeps
  the words "not valid JSON" and names the cause), while a
  non-truncated parse failure keeps the bare error with no
  records attached, exactly as before;
- the stuck signature: a truncation record keys on the text
  itself, so different truncated replies are different
  signatures and the identical reply keys identically;
- the repair loop with a mocked call_llm: a truncation earns
  the compact retry prompt (truncation block, tightened budget
  on the second), the attempt record carries the new code, a
  non-truncation rejection builds its prompt exactly as before,
  and three different truncations end on the repair budget with
  the truncation error — never on StuckRepairError;
- the pre-filled reroute (_fill_plan_contents): a re-plan
  repair node whose carried content is byte-identical to the
  standing file is rerouted through the fill (patch form
  engages; the node is reported rerouted, not pre-filled);
  changed pre-filled content, an initial-phase node, a
  justification-less node, and an edit-declared node all stand
  exactly as before, with no fill call made.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import (
    LLMResponseError,
    LLMSettings,
    json_truncation_diagnosis,
)
from freetalon.orchestrator.models import PlanNode
from freetalon.orchestrator.planner import (
    STAGE_SCHEMA,
    PlanError,
    StuckRepairError,
    _fill_plan_contents,
    _prefilled_reroute_standing,
    _rejection_signature,
    _truncation_steering_block,
    parse_planner_response,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

# Arm #39's third replan_skeleton response, verbatim from the
# run's planner_attempts.jsonl (276 chars, cut mid-string inside
# the first node's file_path).
ARM39_ATTEMPT3 = (
    '{"nodes": [{"id": "step_1", "objective": "Repair '
    "freetalon/pairing/store.py to fix expiry wipe persistence, "
    'corrupt-file detection, and confirmed_at/claimed_at timestamps", '
    '"depends_on": [], "assigned_claw": "scaffold", "inputs": '
    '{"file_path": "/home/seeker/projects/FreeTalon'
)

STORE_SOURCE = "class Store:\n    def load(self):\n        return {}\n"
REAL_EDIT = {
    "search": "        return {}\n",
    "replace": "        return {'ok': True}\n",
}
JUSTIFICATION = "load() swallows corrupt JSON instead of raising"


def _valid_payload() -> str:
    node = {
        "id": "step_1",
        "objective": "Check the tree state",
        "depends_on": [],
        "assigned_claw": "bash",
        "inputs": {"command": "true"},
        "outputs": [],
        "acceptance": ["The step's effect holds."],
        "criteria": [{"type": "exit_code", "equals": 0}],
        "error": None,
    }
    return json.dumps({"nodes": [node], "metadata": {}})


def _truncated(text: str, fraction: float = 0.5) -> str:
    """Cut a valid payload mid-string — the arm #39 shape."""
    cut = text[: int(len(text) * fraction)]
    assert json_truncation_diagnosis(cut) is not None
    return cut


class TruncationDiagnosisTests(unittest.TestCase):
    def test_arm39_third_response_is_truncated_mid_string(self) -> None:
        self.assertEqual(
            json_truncation_diagnosis(ARM39_ATTEMPT3),
            "the text ends inside a string",
        )

    def test_brackets_open_cut_is_truncated(self) -> None:
        cut = '{"nodes": [{"id": "a"}, {"id": "b"},'
        diagnosis = json_truncation_diagnosis(cut)
        self.assertIsNotNone(diagnosis)
        self.assertIn("bracket(s) still open", diagnosis)

    def test_non_truncated_shapes(self) -> None:
        for text in (
            "this is not json at all",
            "Sure, I can help with that.",
            '{"a": tru}',
            '{"nodes": [1, , 2]}',
            '{"nodes": []}',
            '{"nodes": []} trailing junk',
            '```json\n{"nodes": []}\n```',
        ):
            self.assertIsNone(json_truncation_diagnosis(text), text)


class ParseTruncationRejectionTests(unittest.TestCase):
    def test_truncated_response_raises_structured_rejection(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(ARM39_ATTEMPT3)
        exc = caught.exception
        records = getattr(exc, "plan_errors", None) or []
        self.assertEqual([record.code for record in records],
                         ["response_truncated"])
        self.assertEqual(records[0].stage, STAGE_SCHEMA)
        self.assertIn("not valid JSON", str(exc))
        self.assertIn("truncated", str(exc))
        self.assertIn("276 characters", str(exc))

    def test_non_truncated_failure_keeps_the_bare_error(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response("this is not json at all")
        exc = caught.exception
        self.assertIn("not valid JSON", str(exc))
        self.assertFalse(getattr(exc, "plan_errors", None))


class TruncationSignatureTests(unittest.TestCase):
    def _record(self) -> PlanError:
        return PlanError(
            code="response_truncated",
            message="truncated",
            stage=STAGE_SCHEMA,
        )

    def test_different_truncations_are_different_signatures(self) -> None:
        payload = _valid_payload()
        first = _rejection_signature(
            _truncated(payload, 0.5), [self._record()]
        )
        second = _rejection_signature(
            _truncated(payload, 0.65), [self._record()]
        )
        self.assertNotEqual(first, second)

    def test_identical_truncation_keys_identically(self) -> None:
        text = _truncated(_valid_payload())
        self.assertEqual(
            _rejection_signature(text, [self._record()]),
            _rejection_signature(text, [self._record()]),
        )

    def test_other_codeless_records_keep_the_none_component(self) -> None:
        record = PlanError(code="some_other_code", message="x")
        signature = _rejection_signature("whatever", [record])
        self.assertEqual(
            signature, frozenset({("some_other_code", None, None)})
        )


class TruncationSteeringBlockTests(unittest.TestCase):
    def test_first_truncation_budget(self) -> None:
        block = _truncation_steering_block("x" * 100, 1)
        self.assertIn("CUT OFF", block)
        self.assertIn("4,000 characters", block)
        self.assertNotIn("SECOND", block)

    def test_second_truncation_tightens(self) -> None:
        block = _truncation_steering_block("x" * 100, 2)
        self.assertIn("SECOND cut-off", block)
        self.assertIn("3,000 characters", block)


class RepairLoopTruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        planner_module.drain_pending_planner_attempts(lambda record: None)
        self.addCleanup(
            planner_module.drain_pending_planner_attempts,
            lambda record: None,
        )

    def _run(self, responses, **kwargs):
        calls: list[tuple[str, str]] = []
        records: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            index = min(len(calls) - 1, len(responses) - 1)
            return responses[index]

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            with planner_module.planner_attempt_sink(records.append):
                result = planner_module._call_llm_with_repair(
                    "system",
                    "user",
                    SETTINGS,
                    skeleton=True,
                    attempt_phase="replan_skeleton",
                    **kwargs,
                )
        return calls, result, records

    def test_truncation_retry_is_compact_and_recovers(self) -> None:
        payload = _valid_payload()
        calls, (nodes, _metadata), records = self._run(
            [_truncated(payload), payload]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(nodes), 1)
        retry_prompt = calls[1][1]
        self.assertIn("TRUNCATION — READ THIS FIRST", retry_prompt)
        self.assertIn("cut off before completion", retry_prompt)
        self.assertNotIn("text that is not valid JSON.", retry_prompt)
        rejected = [r for r in records if r["outcome"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_codes"], ["response_truncated"])

    def test_second_truncation_escalates_the_budget(self) -> None:
        payload = _valid_payload()
        calls, (_nodes, _metadata), _records = self._run(
            [_truncated(payload, 0.5), _truncated(payload, 0.55), payload]
        )
        self.assertEqual(len(calls), 3)
        self.assertIn("4,000 characters", calls[1][1])
        self.assertIn("SECOND cut-off", calls[2][1])
        self.assertIn("3,000 characters", calls[2][1])

    def test_non_truncation_retry_prompt_is_unchanged(self) -> None:
        payload = _valid_payload()
        calls, (_nodes, _metadata), records = self._run(
            ["this is not json at all", payload]
        )
        self.assertEqual(len(calls), 2)
        self.assertNotIn("TRUNCATION — READ THIS FIRST", calls[1][1])
        self.assertIn("text that is not valid JSON.", calls[1][1])
        rejected = [r for r in records if r["outcome"] == "rejected"]
        self.assertEqual(rejected[0]["error_codes"], [])

    def test_distinct_truncations_end_on_budget_not_stuck(self) -> None:
        payload = _valid_payload()
        responses = [
            _truncated(payload, 0.5),
            _truncated(payload, 0.55),
            _truncated(payload, 0.6),
        ]
        with self.assertRaises(LLMResponseError) as caught:
            self._run(responses, max_repairs=2)
        self.assertNotIsInstance(caught.exception, StuckRepairError)
        self.assertIn("truncated", str(caught.exception))


def _prefilled_node(
    target: Path,
    content: str,
    *,
    intent: str | None = "whole_file",
    justification: str | None = JUSTIFICATION,
) -> PlanNode:
    inputs: dict = {"file_path": str(target), "content": content}
    if intent is not None:
        inputs["content_intent"] = intent
    if justification is not None:
        inputs["overwrite_justification"] = justification
    inputs["clauses"] = {"implements": ["C08-persistence"], "verifies": []}
    return PlanNode(
        id="node-2",
        objective="Repair store.py to fix the corrupt-file defect.",
        assigned_claw="scaffold",
        inputs=inputs,
        criteria=[{"type": "file_exists", "path": str(target)}],
    )


class PrefilledRerouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.target = self.root / "store.py"
        self.target.write_text(STORE_SOURCE, encoding="utf-8")
        self.prompts: list[str] = []

    def _fill(self, nodes, responses, *, phase):
        def fake_call(system, prompt, settings=None):
            self.prompts.append(prompt)
            return responses[len(self.prompts) - 1]

        with mock.patch.object(planner_module, "call_llm", fake_call):
            return _fill_plan_contents(
                nodes,
                settings=SETTINGS,
                goal_text="goal",
                repo_root=self.root,
                clauses=None,
                spec_section="",
                inventory_section="",
                attempt_phase=phase,
            )

    def test_identical_repair_prefill_is_rerouted_to_patch_form(self) -> None:
        node = _prefilled_node(self.target, STORE_SOURCE)
        result = self._fill(
            [node],
            [json.dumps({"edits": [REAL_EDIT]})],
            phase="replan_content_fill",
        )
        self.assertIsNotNone(result)
        filled_dicts, attempts, prefilled, rerouted = result
        self.assertEqual(prefilled, [])
        self.assertEqual(rerouted, ["node-2"])
        self.assertEqual(attempts, {"node-2": 1})
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("REPAIR FILL", self.prompts[0])
        candidate_inputs = filled_dicts[0]["inputs"]
        self.assertEqual(candidate_inputs.get("edits"), [REAL_EDIT])
        self.assertNotIn("content", candidate_inputs)

    def test_legacy_shape_without_declared_intent_reroutes(self) -> None:
        node = _prefilled_node(self.target, STORE_SOURCE, intent=None)
        result = self._fill(
            [node],
            [json.dumps({"edits": [REAL_EDIT]})],
            phase="replan_content_fill",
        )
        self.assertIsNotNone(result)
        _dicts, _attempts, prefilled, rerouted = result
        self.assertEqual(prefilled, [])
        self.assertEqual(rerouted, ["node-2"])

    def test_changed_prefill_stands_without_a_fill_call(self) -> None:
        node = _prefilled_node(self.target, STORE_SOURCE + "# changed\n")
        result = self._fill([node], [], phase="replan_content_fill")
        self.assertIsNone(result)
        self.assertEqual(self.prompts, [])

    def test_initial_phase_identical_prefill_stands(self) -> None:
        node = _prefilled_node(self.target, STORE_SOURCE)
        result = self._fill([node], [], phase="content_fill")
        self.assertIsNone(result)
        self.assertEqual(self.prompts, [])

    def test_justification_less_prefill_stands(self) -> None:
        node = _prefilled_node(
            self.target, STORE_SOURCE, justification=None
        )
        result = self._fill([node], [], phase="replan_content_fill")
        self.assertIsNone(result)
        self.assertEqual(self.prompts, [])

    def test_edit_declared_prefill_stands(self) -> None:
        node = _prefilled_node(self.target, STORE_SOURCE, intent="edit")
        result = self._fill([node], [], phase="replan_content_fill")
        self.assertIsNone(result)
        self.assertEqual(self.prompts, [])

    def test_reroute_standing_helper(self) -> None:
        node = _prefilled_node(self.target, STORE_SOURCE)
        standing = _prefilled_reroute_standing(
            node, [], self.root, "replan_content_fill"
        )
        self.assertEqual(standing, STORE_SOURCE)
        changed = _prefilled_node(self.target, STORE_SOURCE + "x")
        self.assertIsNone(
            _prefilled_reroute_standing(
                changed, [], self.root, "replan_content_fill"
            )
        )


if __name__ == "__main__":
    unittest.main()
