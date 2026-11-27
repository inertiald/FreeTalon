"""Campaign build #30: repair fills run in patch form.

Arm #38's lineage trace (build #29) showed the pairing exam's
repair fills returning the standing files byte-for-byte: the
re-plan declared whole_file nodes for the diagnosed defects and
the whole-file fill form anchored the model on the file as it
stood. The diagnostic battery had shown the same model fixing
all five defects when handed a defect directly as a targeted
patch. Build #30 converts the fill of a REPAIR node — a re-plan
node declaring whole_file over a standing file, carrying the
overwrite_justification that names its defect — to scoped edits,
and makes a no-change repair attempt a FAILED attempt.

The build is tested at the levels it operates:

- the predicate (_is_repair_patch_fill): all four witnesses
  (re-plan phase, whole_file intent, standing source, non-empty
  overwrite_justification) are required; each missing witness
  leaves the fill unconverted;
- the helpers: the no-change error restates the obligation
  (objective, justification, clause obligations); the repair
  shape block spells the scoped-edit envelope and never teaches
  build #17's identity-edit no-change spelling; the materialized
  content reader applies a candidate's edits and declines
  (None) when they do not apply;
- _fill_node_payload with a mocked call_llm: a repair fill is
  prompted in patch form; a whole-file payload is a shape
  failure whose retry carries the repair shape block; identity
  edits meet the materializer's no_op_edit rejection instead of
  completing as no-change; a validated-but-byte-identical result
  fails with the obligation restated; exhaustion raises and the
  lineage records every rejected attempt; an initial-plan fill
  and a justification-less re-plan fill keep the whole-file
  form exactly as before.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.models import PlanNode
from freetalon.orchestrator.planner import (
    _fill_node_payload,
    _is_repair_patch_fill,
    _materialized_fill_content,
    _materialize_plan_edits,
    _repair_no_change_error,
    _repair_patch_shape_repair_block,
)

STORE_SOURCE = "class Store:\n    def load(self):\n        return {}\n"
REAL_EDIT = {"search": "        return {}\n", "replace": "        return {'ok': True}\n"}
IDENTITY_EDIT = {"search": "        return {}\n", "replace": "        return {}\n"}


def _node(target: Path, *, justification: str | None = "load() swallows corrupt JSON instead of raising") -> PlanNode:
    inputs: dict = {
        "file_path": str(target),
        "content_intent": "whole_file",
        "clauses": {"implements": ["C08-persistence"], "verifies": []},
    }
    if justification is not None:
        inputs["overwrite_justification"] = justification
    return PlanNode(
        id="node-2",
        objective="Repair store.py to fix the corrupt-file defect.",
        assigned_claw="scaffold",
        inputs=inputs,
        criteria=[{"type": "file_exists", "path": str(target)}],
    )


def _run_fill(node, responses, *, phase, existing, root):
    prompts: list[str] = []

    def fake_call(system, prompt, settings=None):
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    with patch(
        "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
    ):
        candidate, used = _fill_node_payload(
            node,
            "whole_file",
            settings=None,
            skeleton_json="[]",
            obligations="",
            spec_section="",
            inventory_section="",
            existing_source=existing,
            prior_dicts=[],
            repo_root=root,
            goal_text="goal",
            attempt_phase=phase,
        )
    return candidate, used, prompts


class RepairPatchPredicateTests(unittest.TestCase):
    def test_all_four_witnesses_required(self) -> None:
        node = _node(Path("/tmp/store.py"))
        self.assertTrue(
            _is_repair_patch_fill(node, "whole_file", STORE_SOURCE, "replan_content_fill")
        )
        # Initial-plan phase: construction, never converted.
        self.assertFalse(
            _is_repair_patch_fill(node, "whole_file", STORE_SOURCE, "content_fill")
        )
        # Edit-intent fills already have their form.
        self.assertFalse(
            _is_repair_patch_fill(node, "edit", STORE_SOURCE, "replan_content_fill")
        )
        # No standing file: nothing to patch.
        self.assertFalse(
            _is_repair_patch_fill(node, "whole_file", None, "replan_content_fill")
        )
        # No justification: the plan did not declare a defect repair.
        bare = _node(Path("/tmp/store.py"), justification=None)
        self.assertFalse(
            _is_repair_patch_fill(bare, "whole_file", STORE_SOURCE, "replan_content_fill")
        )
        blank = _node(Path("/tmp/store.py"), justification="   ")
        self.assertFalse(
            _is_repair_patch_fill(blank, "whole_file", STORE_SOURCE, "replan_content_fill")
        )


class RepairPatchHelperTests(unittest.TestCase):
    def test_no_change_error_restates_the_obligation(self) -> None:
        node = _node(Path("/tmp/store.py"))
        error = _repair_no_change_error(node, "  implements C08-persistence: text")
        self.assertIn("changed NOTHING", error)
        self.assertIn("Repair store.py to fix the corrupt-file defect.", error)
        self.assertIn("load() swallows corrupt JSON instead of raising", error)
        self.assertIn("implements C08-persistence", error)
        self.assertIn("concrete change", error)

    def test_shape_block_teaches_patch_form_only(self) -> None:
        block = _repair_patch_shape_repair_block(["some defect"])
        self.assertIn('{"edits": [{"search": "import os\\n"', block)
        self.assertIn("FAILS", block)
        self.assertIn("NOT accepted", block)
        # Never build #17's lesson: identity edits as a legal
        # no-change completion.
        self.assertNotIn("completes the node as no-change", block)

    def test_materialized_fill_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mod.py"
            target.write_text(STORE_SOURCE, encoding="utf-8")
            candidate = {
                "id": "node-2",
                "objective": "Repair store.py.",
                "assigned_claw": "scaffold",
                "inputs": {"file_path": str(target), "edits": [REAL_EDIT]},
            }
            self.assertEqual(
                _materialized_fill_content(candidate, [], tmp),
                "class Store:\n    def load(self):\n        return {'ok': True}\n",
            )
            unappliable = {
                "id": "node-2",
                "objective": "Repair store.py.",
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": str(target),
                    "edits": [{"search": "absent\n", "replace": "x\n"}],
                },
            }
            self.assertIsNone(_materialized_fill_content(unappliable, [], tmp))


class RepairPatchFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.target = Path(self.root) / "store.py"
        self.target.write_text(STORE_SOURCE, encoding="utf-8")
        self.node = _node(self.target)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_whole_file_payload_rejected_then_patch_accepted(self) -> None:
        responses = [
            json.dumps({"content": STORE_SOURCE}),
            json.dumps({"edits": [REAL_EDIT]}),
        ]
        candidate, used, prompts = _run_fill(
            self.node,
            responses,
            phase="replan_content_fill",
            existing=STORE_SOURCE,
            root=self.root,
        )
        self.assertEqual(used, 2)
        # The first prompt is the patch form, never whole-file.
        self.assertIn("REPAIR FILL", prompts[0])
        self.assertIn("Do NOT return the whole file", prompts[0])
        self.assertNotIn('Return {"content": "<the complete file content>"}', prompts[0])
        # The retry carries the repair shape block.
        self.assertIn("REPAIR node", prompts[1])
        self.assertIn("did not match the required payload shape", prompts[1])
        # The accepted candidate carries edits that materialize
        # to the repaired content.
        self.assertEqual(candidate["inputs"].get("edits"), [REAL_EDIT])
        dicts = [json.loads(json.dumps(candidate))]
        _materialize_plan_edits(dicts, self.root)
        self.assertIn("'ok': True", dicts[0]["inputs"]["content"])

    def test_identity_edits_fail_instead_of_completing_no_change(self) -> None:
        responses = [
            json.dumps({"edits": [IDENTITY_EDIT]}),
            json.dumps({"edits": [REAL_EDIT]}),
        ]
        candidate, used, prompts = _run_fill(
            self.node,
            responses,
            phase="replan_content_fill",
            existing=STORE_SOURCE,
            root=self.root,
        )
        self.assertEqual(used, 2)
        self.assertIn("no_op_edit", prompts[1])
        self.assertEqual(candidate["inputs"].get("edits"), [REAL_EDIT])
        self.assertNotEqual(
            candidate["inputs"].get("_materialized_from"), "no_change"
        )

    def test_net_no_change_result_fails_the_attempt(self) -> None:
        # The comparison itself is unit-tested above; here the
        # materialized-content reader is stubbed to the standing
        # text to prove the wiring: a validated candidate whose
        # result is byte-identical fails, and the retry restates
        # the obligation.
        responses = [json.dumps({"edits": [REAL_EDIT]})] * 3
        prompts: list[str] = []

        def fake_call(system, prompt, settings=None):
            prompts.append(prompt)
            return responses[len(prompts) - 1]

        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ), patch(
            "freetalon.orchestrator.planner._materialized_fill_content",
            return_value=STORE_SOURCE,
        ):
            with self.assertRaises(LLMResponseError):
                _fill_node_payload(
                    self.node,
                    "whole_file",
                    settings=None,
                    skeleton_json="[]",
                    obligations="",
                    spec_section="",
                    inventory_section="",
                    existing_source=STORE_SOURCE,
                    prior_dicts=[],
                    repo_root=self.root,
                    goal_text="goal",
                    attempt_phase="replan_content_fill",
                )
        self.assertEqual(len(prompts), 3)
        self.assertIn("changed NOTHING", prompts[1])
        self.assertIn("concrete change", prompts[1])

    def test_exhaustion_records_every_rejected_attempt_in_lineage(self) -> None:
        responses = [json.dumps({"edits": [IDENTITY_EDIT]})] * 3
        outcomes: list[str] = []

        def fake_outcome(**kwargs):
            outcomes.append(kwargs["outcome"])

        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=lambda system, prompt, settings=None: responses.pop(0),
        ), patch(
            "freetalon.orchestrator.planner.record_fill_outcome",
            side_effect=fake_outcome,
        ):
            with self.assertRaises(LLMResponseError):
                _fill_node_payload(
                    self.node,
                    "whole_file",
                    settings=None,
                    skeleton_json="[]",
                    obligations="",
                    spec_section="",
                    inventory_section="",
                    existing_source=STORE_SOURCE,
                    prior_dicts=[],
                    repo_root=self.root,
                    goal_text="goal",
                    attempt_phase="replan_content_fill",
                )
        self.assertEqual(outcomes, ["rejected", "rejected", "rejected", "failed"])

    def test_initial_plan_fill_keeps_whole_file_form(self) -> None:
        responses = [json.dumps({"content": STORE_SOURCE})]
        candidate, used, prompts = _run_fill(
            self.node,
            responses,
            phase="content_fill",
            existing=STORE_SOURCE,
            root=self.root,
        )
        self.assertEqual(used, 1)
        self.assertIn('Return {"content": "<the complete file content>"}', prompts[0])
        self.assertEqual(candidate["inputs"].get("content"), STORE_SOURCE)

    def test_replan_fill_without_justification_keeps_whole_file_form(self) -> None:
        bare = _node(self.target, justification=None)
        responses = [json.dumps({"content": STORE_SOURCE})]
        candidate, used, prompts = _run_fill(
            bare,
            responses,
            phase="replan_content_fill",
            existing=STORE_SOURCE,
            root=self.root,
        )
        self.assertEqual(used, 1)
        self.assertIn('Return {"content": "<the complete file content>"}', prompts[0])
        self.assertEqual(candidate["inputs"].get("content"), STORE_SOURCE)


if __name__ == "__main__":
    unittest.main()
