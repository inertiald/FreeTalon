"""Campaign build #8: schema-explicit repair for fill shape failures.

Arm #17 died in content fill on the __init__.py edit node, and the
diagnosis (byte-identical reproduction during the build; the arm's
own fill payloads were never persisted) was that the model kept
answering "no change needed" in shape dialects — {}, a no-op edit,
{"edits": []} — while the repair prompt only ever repeated the
bare validator sentence. The build is tested at the levels it
operates:

- the shape classifier (_fill_payload_shape): every defect in the
  taxonomy named in plain words, valid envelopes untouched;
- the repair block (_fill_shape_repair_block): defect + exact
  schema per intent + a worked example;
- plan_task_intent end to end with a dispatching call_llm mock:
  a shape-broken first payload earns the schema-explicit re-ask
  and a corrected payload fills; the node-5 sequence ({} then
  {"edits": []} then a real edit) fills on the third attempt;
  blind-forever shape failures still fail the plan after exactly
  3 attempts with the precise error; content-validation failures
  keep the pre-build-#8 repair block byte-for-byte in shape; the
  first-attempt prompt carries no repair block at all.

Envelope recovery is deliberately NOT built (the observed payloads
carried no usable content under any alternate envelope), so there
are no recovery tests.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import PlanStatus, TaskIntent
from freetalon.orchestrator.planner import (
    _fill_payload_shape,
    _fill_shape_repair_block,
    plan_task_intent,
)

SERVER_SOURCE = (
    "class Server:\n"
    "    pass\n"
    "\n"
    "\n"
    "class Handler:\n"
    "    def __init__(self, server):\n"
    "        self.server = server\n"
    "\n"
    "    def handle(self):\n"
    "        return self.server\n"
)


def _write_node(node_id, target, intent=None, **extra_inputs):
    inputs = {"file_path": target}
    if intent is not None:
        inputs["content_intent"] = intent
    inputs.update(extra_inputs)
    return {
        "id": node_id,
        "objective": f"Write {target}",
        "depends_on": [],
        "assigned_claw": "scaffold",
        "inputs": inputs,
        "criteria": [{"type": "file_exists", "path": target}],
    }


def _bash_node(node_id, command="true", depends_on=None):
    return {
        "id": node_id,
        "objective": f"Run {command}",
        "depends_on": depends_on or [],
        "assigned_claw": "bash",
        "inputs": {"command": command},
        "criteria": [{"type": "exit_code", "equals": 0}],
    }


def _payload(*nodes) -> str:
    return json.dumps({"nodes": list(nodes), "metadata": {}})


class TestFillPayloadShape(unittest.TestCase):
    def test_empty_object_edit(self) -> None:
        fields, defects = _fill_payload_shape({}, "edit")
        self.assertEqual(fields, {})
        self.assertEqual(len(defects), 1)
        self.assertIn("empty JSON object", defects[0])
        self.assertIn('"edits"', defects[0])
        self.assertIn('"anchor"', defects[0])

    def test_empty_edits_list(self) -> None:
        _fields, defects = _fill_payload_shape({"edits": []}, "edit")
        self.assertIn('"edits" list, but it was empty', defects[0])

    def test_edits_wrong_type(self) -> None:
        _fields, defects = _fill_payload_shape({"edits": "x"}, "edit")
        self.assertIn('"edits" was a string', defects[0])

    def test_anchor_wrong_type(self) -> None:
        _fields, defects = _fill_payload_shape({"anchor": 5}, "edit")
        self.assertIn('"anchor" was a number', defects[0])

    def test_anchor_empty_string(self) -> None:
        _fields, defects = _fill_payload_shape({"anchor": ""}, "edit")
        self.assertIn('"anchor" was an empty string', defects[0])

    def test_anchor_without_content(self) -> None:
        _fields, defects = _fill_payload_shape(
            {"anchor": "class Server:"}, "edit"
        )
        self.assertIn('carried an "anchor" but no "content"', defects[0])

    def test_anchor_content_wrong_type(self) -> None:
        _fields, defects = _fill_payload_shape(
            {"anchor": "class Server:", "content": 5}, "edit"
        )
        self.assertIn('"content" was a number', defects[0])

    def test_content_key_only_for_edit(self) -> None:
        _fields, defects = _fill_payload_shape(
            {"content": "VALUE = 1\n"}, "edit"
        )
        self.assertIn("none of the required keys", defects[0])
        self.assertIn("keys present: content", defects[0])

    def test_valid_edits(self) -> None:
        edits = [{"search": "a", "replace": "b"}]
        fields, defects = _fill_payload_shape({"edits": edits}, "edit")
        self.assertEqual(defects, [])
        self.assertEqual(fields, {"edits": edits})

    def test_valid_anchor(self) -> None:
        fields, defects = _fill_payload_shape(
            {"anchor": "class Server:", "content": "X = 1\n"}, "edit"
        )
        self.assertEqual(defects, [])
        self.assertEqual(fields["anchor"], "class Server:")
        self.assertEqual(fields["position"], "after")
        self.assertEqual(fields["content"], "X = 1\n")

    def test_empty_object_create(self) -> None:
        _fields, defects = _fill_payload_shape({}, "create")
        self.assertIn("empty JSON object", defects[0])
        self.assertIn('no "content" at all', defects[0])

    def test_create_content_missing_with_other_keys(self) -> None:
        _fields, defects = _fill_payload_shape({"edits": []}, "create")
        self.assertIn('carried no "content" string', defects[0])
        self.assertIn("keys present: edits", defects[0])

    def test_create_content_empty(self) -> None:
        _fields, defects = _fill_payload_shape({"content": ""}, "whole_file")
        self.assertIn('"content" was an empty string', defects[0])

    def test_create_content_wrong_type(self) -> None:
        _fields, defects = _fill_payload_shape({"content": 5}, "create")
        self.assertIn('"content" was a number', defects[0])

    def test_create_valid(self) -> None:
        fields, defects = _fill_payload_shape(
            {"content": "VALUE = 1\n"}, "create"
        )
        self.assertEqual(defects, [])
        self.assertEqual(fields, {"content": "VALUE = 1\n"})


class TestShapeRepairBlock(unittest.TestCase):
    def test_edit_block_names_defect_schema_and_example(self) -> None:
        block = _fill_shape_repair_block(
            "edit", ['your previous payload was an empty JSON object']
        )
        self.assertIn("did not match the required payload shape", block)
        self.assertIn("empty JSON object", block)
        self.assertIn('{"edits": [{"search":', block)
        self.assertIn('{"anchor":', block)
        self.assertIn("Worked example", block)
        self.assertIn('NOT "no change" answers', block)

    def test_create_block_names_defect_schema_and_example(self) -> None:
        block = _fill_shape_repair_block(
            "create", ['your previous payload carried no "content"']
        )
        self.assertIn('{"content": "<the complete file content>"}', block)
        self.assertIn("Worked example", block)
        self.assertIn('carried no "content"', block)


EDIT_SKELETON = _payload(
    _write_node("w-server", "pkg/server.py", "edit"),
    _bash_node("run", "true", depends_on=["w-server"]),
)

CREATE_SKELETON = _payload(
    _write_node("w-store", "pkg/store.py", "create"),
    _bash_node("run", "true", depends_on=["w-store"]),
)

GOOD_EDIT = json.dumps(
    {
        "edits": [
            {
                "search": "    def handle(self):\n        return self.server\n",
                "replace": (
                    "    def handle(self):\n        return self.server\n"
                    "\n"
                    "    def handle_twice(self):\n"
                    "        return self.server\n"
                ),
            }
        ]
    }
)

UNGROUNDED_EDIT = json.dumps(
    {
        "edits": [
            {
                "search": "    def handle(self):\n        return self.server\n",
                "replace": (
                    "    def handle(self):\n        return self.server\n"
                    "\n"
                    "    def store_of(self):\n"
                    "        return self.store\n"
                ),
            }
        ]
    }
)

STORE_CONTENT = "class Store:\n    def get(self):\n        return 1\n"
GOOD_CREATE = json.dumps({"content": STORE_CONTENT})


class _FillCase(unittest.TestCase):
    skeleton = EDIT_SKELETON

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "server.py").write_text(SERVER_SOURCE, encoding="utf-8")
        self.intent = TaskIntent(goal="Extend the pairing server")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self.prompts: list[str] = []

    def _dispatcher(self, fill_payloads):
        state = {"fills": 0}

        def _call(system_prompt, user_prompt, settings=None):
            if "CONTENT FILL" in user_prompt:
                state["fills"] += 1
                self.prompts.append(user_prompt)
                return fill_payloads[
                    min(state["fills"], len(fill_payloads)) - 1
                ]
            return self.skeleton

        _call.state = state
        return _call

    def _plan(self, dispatcher):
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=dispatcher,
        ):
            return plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )


class TestEditShapeRepair(_FillCase):
    def test_empty_object_then_good_edit(self) -> None:
        dispatcher = self._dispatcher(["{}", GOOD_EDIT])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 2)
        repair_prompt = self.prompts[1]
        self.assertIn("did not match the required payload shape", repair_prompt)
        self.assertIn("empty JSON object", repair_prompt)
        self.assertIn("Worked example", repair_prompt)
        self.assertIn('{"edits": [{"search":', repair_prompt)
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])
        self.assertEqual(
            plan.metadata["plan_emission"]["content_attempts"],
            {"node-1": 2},
        )

    def test_node5_sequence_fills_on_third_attempt(self) -> None:
        # Arm #17's node-5 dialects, minus the no-op edit (a content
        # failure): {} then {"edits": []} then a real edit.
        dispatcher = self._dispatcher(["{}", '{"edits": []}', GOOD_EDIT])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 3)
        third_prompt = self.prompts[2]
        self.assertIn(
            '"edits" list, but it was empty', third_prompt
        )
        self.assertIn("Worked example", third_prompt)
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])
        self.assertEqual(
            plan.metadata["plan_emission"]["content_attempts"],
            {"node-1": 3},
        )

    def test_blind_forever_shape_failures_fail_the_plan(self) -> None:
        dispatcher = self._dispatcher(["{}"])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 3)
        # Build #26: the exhausted fill returns a FAILED plan;
        # the stamp's error is the fill error's own text.
        self.assertEqual(plan.status, PlanStatus.FAILED)
        message = plan.metadata["planning_failure"]["error"]
        self.assertIn("Content fill for planner node 'node-1'", message)
        self.assertIn("intent 'edit'", message)
        self.assertIn("empty JSON object", message)

    def test_unparseable_payload_then_good_edit(self) -> None:
        dispatcher = self._dispatcher(["this is not json", GOOD_EDIT])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 2)
        self.assertIn("was not a JSON object", self.prompts[1])
        self.assertIn("Worked example", self.prompts[1])
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])

    def test_anchor_without_content_then_good_edit(self) -> None:
        dispatcher = self._dispatcher(
            [json.dumps({"anchor": "class Server:"}), GOOD_EDIT]
        )
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 2)
        self.assertIn(
            'carried an "anchor" but no "content"', self.prompts[1]
        )
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])

    def test_content_failure_keeps_the_plain_repair_block(self) -> None:
        dispatcher = self._dispatcher([UNGROUNDED_EDIT, GOOD_EDIT])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 2)
        repair_prompt = self.prompts[1]
        self.assertIn(
            "Your previous payload for this node was REJECTED:",
            repair_prompt,
        )
        self.assertNotIn(
            "did not match the required payload shape", repair_prompt
        )
        self.assertNotIn("Worked example", repair_prompt)
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])

    def test_first_attempt_prompt_carries_no_repair_block(self) -> None:
        dispatcher = self._dispatcher([GOOD_EDIT])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 1)
        first_prompt = self.prompts[0]
        self.assertIn("CONTENT FILL", first_prompt)
        self.assertNotIn("REJECTED", first_prompt)
        self.assertNotIn("Worked example", first_prompt)
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])


class TestCreateShapeRepair(_FillCase):
    skeleton = CREATE_SKELETON

    def test_empty_object_then_good_create(self) -> None:
        dispatcher = self._dispatcher(["{}", GOOD_CREATE])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 2)
        repair_prompt = self.prompts[1]
        self.assertIn("did not match the required payload shape", repair_prompt)
        self.assertIn('no "content" at all', repair_prompt)
        self.assertIn(
            '{"content": "<the complete file content>"}', repair_prompt
        )
        self.assertIn("Worked example", repair_prompt)
        self.assertEqual(
            plan.nodes[0].inputs["content"], STORE_CONTENT
        )
        self.assertEqual(
            plan.metadata["plan_emission"]["content_attempts"],
            {"node-1": 2},
        )


if __name__ == "__main__":
    unittest.main()
