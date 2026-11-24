"""Campaign build #5: split plan emission (skeleton + content fill).

Arm #14 measured the failure this answers: compliant plans died
truncated mid-JSON because one response carried every file's full
content plus the clause declarations. The split is tested at the
levels it operates:

- skeleton validation through parse_planner_response(skeleton=True):
  the basic content-intent rule, the gated intent-legality rule,
  clause coverage and suite placement running on skeletons;
- plan_task_intent end to end with a dispatching call_llm mock:
  skeleton accepted, each write node filled by its own call, the
  assembled plan shape-identical to the equivalent single-shot
  plan, per-node repair, per-node failure, edit grounding at the
  fill phase, and attempt anchoring still working at the skeleton
  phase.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import TaskIntent
from freetalon.orchestrator.planner import (
    parse_planner_response,
    plan_task_intent,
)

CLAUSES = [
    {"id": "C1-store", "text": "The store persists registrations."},
    {"id": "C2-verify", "text": "The pairing flow is verified."},
]

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


def _write_node(node_id, target, intent=None, clauses=None, **extra_inputs):
    inputs = {"file_path": target}
    if intent is not None:
        inputs["content_intent"] = intent
    if clauses:
        inputs["clauses"] = clauses
    inputs.update(extra_inputs)
    return {
        "id": node_id,
        "objective": f"Write {target}",
        "depends_on": [],
        "assigned_claw": "scaffold",
        "inputs": inputs,
        "criteria": [{"type": "file_exists", "path": target}],
    }


def _bash_node(node_id, command="true", clauses=None, depends_on=None):
    node = {
        "id": node_id,
        "objective": f"Run {command}",
        "depends_on": depends_on or [],
        "assigned_claw": "bash",
        "inputs": {"command": command},
        "criteria": [{"type": "exit_code", "equals": 0}],
    }
    if clauses:
        node["inputs"]["clauses"] = clauses
    return node


def _payload(*nodes) -> str:
    return json.dumps({"nodes": list(nodes), "metadata": {}})


class _TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "server.py").write_text(SERVER_SOURCE, encoding="utf-8")


class TestSkeletonValidation(_TreeCase):
    def _parse(self, text, **kwargs):
        return parse_planner_response(
            text, repo_root=self.root, skeleton=True, **kwargs
        )

    def test_valid_skeleton_accepted(self) -> None:
        nodes, _ = self._parse(
            _payload(
                _write_node(
                    "w1", "pkg/store.py", "create",
                    clauses={
                        "implements": ["C1-store", "C2-verify"]
                    },
                ),
                _bash_node(
                    "v1",
                    clauses={"verifies": ["C1-store", "C2-verify"]},
                    depends_on=["w1"],
                ),
            ),
            clauses=CLAUSES,
        )
        self.assertEqual(len(nodes), 2)
        self.assertEqual(
            nodes[0].inputs["content_intent"], "create"
        )

    def test_missing_intent_rejected(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(_payload(_write_node("w1", "pkg/store.py")))
        self.assertIn("content_intent", str(caught.exception))

    def test_clause_coverage_runs_on_skeleton(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                _payload(
                    _write_node(
                        "w1", "pkg/store.py", "create",
                        clauses={"implements": ["C1-store"]},
                    )
                ),
                clauses=CLAUSES,
            )
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("clause_coverage", codes)

    def test_create_intent_against_existing_file_corrected(self) -> None:
        # Campaign build #11: "create" against an existing file is
        # a deterministic contradiction — parse corrects it to
        # "edit" before validation and accepts, recording the
        # correction in the plan metadata. (Through build #10 this
        # rejected with skeleton_intent; exam arm #20 spent four
        # of its five skeleton attempts on exactly this defect.)
        nodes, metadata = self._parse(
            _payload(_write_node("w1", "pkg/server.py", "create"))
        )
        self.assertEqual(nodes[0].inputs["content_intent"], "edit")
        corrections = metadata["intent_corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["target"], "pkg/server.py")
        self.assertEqual(corrections[0]["from"], "create")
        self.assertEqual(corrections[0]["to"], "edit")

    def test_edit_intent_against_missing_file_corrected(self) -> None:
        # Campaign build #11: the symmetric correction — "edit"
        # against a file that does not exist becomes "create"
        # (through build #10 this rejected with skeleton_intent).
        nodes, metadata = self._parse(
            _payload(_write_node("w1", "pkg/store.py", "edit"))
        )
        self.assertEqual(nodes[0].inputs["content_intent"], "create")
        corrections = metadata["intent_corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["target"], "pkg/store.py")
        self.assertEqual(corrections[0]["from"], "edit")
        self.assertEqual(corrections[0]["to"], "create")

    def test_whole_file_intent_needs_justification_pair(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                _payload(_write_node("w1", "pkg/server.py", "whole_file"))
            )
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("skeleton_intent", codes)

    def test_whole_file_intent_with_pair_accepted(self) -> None:
        write = _write_node(
            "w1", "pkg/server.py", "whole_file",
            overwrite_justification="the module is being replaced wholesale",
        )
        check = _bash_node("c1", depends_on=["w1"])
        check["inputs"]["impact_check_for"] = "pkg/server.py"
        nodes, _ = self._parse(_payload(write, check))
        self.assertEqual(len(nodes), 2)

    def test_suite_placement_runs_on_skeleton(self) -> None:
        suite = _bash_node(
            "s1", "./.venv/bin/python -m unittest discover -s tests"
        )
        write = _write_node("w1", "pkg/store.py", "create")
        write["depends_on"] = ["s1"]
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(_payload(suite, write))
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("suite_placement", codes)

    def test_prefilled_node_needs_no_intent(self) -> None:
        node = _write_node("w1", "pkg/store.py")
        node["inputs"]["content"] = "VALUE = 1\n"
        nodes, _ = self._parse(_payload(node))
        self.assertEqual(nodes[0].inputs["content"], "VALUE = 1\n")


SKELETON = _payload(
    _write_node("w-store", "pkg/store.py", "create"),
    _write_node("w-test", "tests/test_store.py", "create"),
    _bash_node("run", "true", depends_on=["w-store", "w-test"]),
)
# No clause declarations here: plan_task_intent extracts the frozen
# clause list from a goal-named spec, and this fixture's goal names
# none — declared IDs would be invented IDs. The clause gate on
# skeletons is covered at parse level in TestSkeletonValidation.

STORE_CONTENT = "class Store:\n    def get(self):\n        return 1\n"
TEST_CONTENT = "def test_store():\n    assert True\n"


def _fill_dispatcher(failures=None):
    """A call_llm stand-in dispatching on the prompt's phase marker.

    ``failures`` maps a target path to a list of bad payloads to
    return (one per attempt) before the good one.
    """
    failures = failures or {}
    counts: dict[str, int] = {}

    def _call(system_prompt, user_prompt, settings=None):
        if "CONTENT FILL" in user_prompt:
            import re

            target = re.search(
                r"writes (\S+) \(content_intent", user_prompt
            ).group(1)
            good = json.dumps(
                {
                    "content": (
                        STORE_CONTENT
                        if target == "pkg/store.py"
                        else TEST_CONTENT
                    )
                }
            )
            counts[target] = counts.get(target, 0) + 1
            bad = failures.get(target) or []
            if counts[target] <= len(bad):
                return bad[counts[target] - 1]
            return good
        return SKELETON

    _call.counts = counts
    return _call


class TestSplitEmission(_TreeCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "tests").mkdir()
        self.intent = TaskIntent(goal="Build the pairing store")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    def _plan(self, dispatcher):
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=dispatcher,
        ):
            return plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )

    def test_fill_success_and_provenance(self) -> None:
        plan = self._plan(_fill_dispatcher())
        by_target = {
            node.inputs.get("file_path"): node for node in plan.nodes
        }
        self.assertEqual(
            by_target["pkg/store.py"].inputs["content"], STORE_CONTENT
        )
        self.assertEqual(
            by_target["tests/test_store.py"].inputs["content"], TEST_CONTENT
        )
        for node in plan.nodes:
            self.assertNotIn("content_intent", node.inputs)
        emission = plan.metadata["plan_emission"]
        self.assertEqual(emission["phases"], ["skeleton", "content_fill"])
        self.assertEqual(
            emission["content_attempts"], {"node-1": 1, "node-2": 1}
        )

    def test_assembly_shape_matches_single_shot(self) -> None:
        plan = self._plan(_fill_dispatcher())
        single_shot = _payload(
            {
                **_write_node("w-store", "pkg/store.py"),
                "inputs": {
                    "file_path": "pkg/store.py",
                    "content": STORE_CONTENT,
                },
            },
            {
                **_write_node("w-test", "tests/test_store.py"),
                "inputs": {
                    "file_path": "tests/test_store.py",
                    "content": TEST_CONTENT,
                },
            },
            _bash_node("run", "true", depends_on=["w-store", "w-test"]),
        )
        expected_nodes, _ = parse_planner_response(
            single_shot, repo_root=self.root
        )
        self.assertEqual(
            [node.model_dump(mode="json") for node in plan.nodes],
            [node.model_dump(mode="json") for node in expected_nodes],
        )

    def test_per_node_repair(self) -> None:
        bad = json.dumps({"content": "def broken(:\n"})
        dispatcher = _fill_dispatcher({"pkg/store.py": [bad]})
        plan = self._plan(dispatcher)
        self.assertEqual(
            plan.metadata["plan_emission"]["content_attempts"]["node-1"], 2
        )
        self.assertEqual(dispatcher.counts["pkg/store.py"], 2)

    def test_per_node_failure_fails_plan(self) -> None:
        bad = json.dumps({"content": "def broken(:\n"})
        dispatcher = _fill_dispatcher({"pkg/store.py": [bad, bad, bad, bad]})
        with self.assertRaises(LLMResponseError) as caught:
            self._plan(dispatcher)
        self.assertIn("Content fill for planner node", str(caught.exception))
        self.assertEqual(dispatcher.counts["pkg/store.py"], 3)

    def test_anchoring_still_functions_at_skeleton_phase(self) -> None:
        bad_skeleton = _payload(
            {
                "id": "a",
                "objective": "Teleport the work",
                "depends_on": [],
                "assigned_claw": "teleport",
                "inputs": {},
                "criteria": [{"type": "exit_code", "equals": 0}],
            }
        )
        state = {"skeleton_calls": 0}
        good = _fill_dispatcher()

        def _call(system_prompt, user_prompt, settings=None):
            if "CONTENT FILL" in user_prompt:
                return good(system_prompt, user_prompt, settings=settings)
            state["skeleton_calls"] += 1
            if state["skeleton_calls"] == 1:
                return bad_skeleton
            return SKELETON

        plan = self._plan(_call)
        self.assertIn("attempt_anchoring", plan.metadata)
        attempts = plan.metadata["attempt_anchoring"]["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertIn("unknown_claw", attempts[0]["errors"])


EDIT_SKELETON = _payload(
    _write_node("w-server", "pkg/server.py", "edit"),
    _bash_node("run", "true", depends_on=["w-server"]),
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


class TestEditFill(_TreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.intent = TaskIntent(goal="Extend the pairing server")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    def _dispatcher(self, edit_payloads):
        state = {"fills": 0}

        def _call(system_prompt, user_prompt, settings=None):
            if "CONTENT FILL" in user_prompt:
                state["fills"] += 1
                return edit_payloads[
                    min(state["fills"], len(edit_payloads)) - 1
                ]
            return EDIT_SKELETON

        _call.state = state
        return _call

    def test_edit_fill_materializes(self) -> None:
        dispatcher = self._dispatcher([GOOD_EDIT])
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=dispatcher,
        ):
            plan = plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )
        content = plan.nodes[0].inputs["content"]
        self.assertIn("handle_twice", content)
        self.assertEqual(
            plan.nodes[0].inputs["_materialized_from"], "scoped_edits"
        )
        self.assertEqual(
            plan.metadata["plan_emission"]["content_attempts"],
            {"node-1": 1},
        )

    def test_ungrounded_edit_is_repaired_then_accepted(self) -> None:
        dispatcher = self._dispatcher([UNGROUNDED_EDIT, GOOD_EDIT])
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=dispatcher,
        ):
            plan = plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )
        self.assertEqual(dispatcher.state["fills"], 2)
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])


if __name__ == "__main__":
    unittest.main()
