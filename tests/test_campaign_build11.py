"""Campaign build #11: deterministic skeleton-intent correction +
existence anchoring in skeleton repair briefs.

Arm #20 measured the failure this answers: the initial skeleton
exhausted all five attempts, four on skeleton_intent — the model
kept declaring content_intent "create" for store.py / server.py,
which the standing exam tree already held, and never settled on a
legal intent even though the rejection named the defect exactly.
Whether a target exists on disk is a deterministic fact the
system already computes (build #4's inventory); arm #19 declared
"whole_file" for the same files and passed on attempt 2 — pure
model variance on a fact, paid for in repair rounds.

The build is tested at the levels it operates:

- parse_planner_response(skeleton=True): the correction itself
  (create-on-existing -> edit, edit/whole_file-on-missing ->
  create), its deliberate exclusions (an unjustified whole_file
  on an existing file still rejects; a plan-internal "create"
  contradiction is not corrected), the pre-filled exemption, and
  correction-before-validation (a coexisting defect is judged
  against the corrected plan);
- the SKELETON TARGET FACTS block as a unit;
- plan_task_intent end to end with a dispatching call_llm mock:
  the corrected skeleton is accepted on the first attempt (no
  repair round spent on the intent), the fill is requested under
  the corrected intent, the corrections land in the plan metadata
  AND in the build #9 attempt record, and a rejected skeleton's
  repair brief carries the target-facts block;
- replan_from_brief: the delta skeleton phase shares the exact
  parse helper, so the correction applies there too and writes
  back through the brief.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import clauses_for_specs
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import TaskIntent
from freetalon.orchestrator.planner import (
    _skeleton_target_facts_block,
    parse_planner_response,
    plan_task_intent,
)
from freetalon.orchestrator.repo_index import goal_named_specs

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
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


class TestIntentCorrectionParse(_TreeCase):
    def _parse(self, text, **kwargs):
        return parse_planner_response(
            text, repo_root=self.root, skeleton=True, **kwargs
        )

    def test_whole_file_on_missing_corrected_to_create(self) -> None:
        nodes, metadata = self._parse(
            _payload(_write_node("w1", "pkg/store.py", "whole_file"))
        )
        self.assertEqual(nodes[0].inputs["content_intent"], "create")
        corrections = metadata["intent_corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["from"], "whole_file")
        self.assertEqual(corrections[0]["to"], "create")
        self.assertEqual(corrections[0]["target"], "pkg/store.py")

    def test_whole_file_on_existing_without_justification_not_corrected(
        self,
    ) -> None:
        # The justification is the model's claim to make — the
        # system cannot fabricate it, so this stays a rejection
        # and no correction is recorded for the node.
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                _payload(_write_node("w1", "pkg/server.py", "whole_file"))
            )
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("skeleton_intent", codes)
        self.assertFalse(
            getattr(caught.exception, "intent_corrections", None)
        )

    def test_plan_internal_create_is_not_corrected(self) -> None:
        # Node B re-declares "create" for a file node A of the
        # SAME plan creates: a plan-internal contradiction, not a
        # tree fact — it stays a skeleton_intent rejection and no
        # correction is recorded.
        first = _write_node("w1", "pkg/new.py", "create")
        second = _write_node("w2", "pkg/new.py", "create")
        second["depends_on"] = ["w1"]
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(_payload(first, second))
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("skeleton_intent", codes)
        self.assertFalse(
            getattr(caught.exception, "intent_corrections", None)
        )

    def test_edit_on_plan_created_target_is_legal(self) -> None:
        # The validator's plan-internal semantics, unchanged: a
        # later "edit" against a target an earlier node creates
        # is legal (the file exists by fill time) — no error, no
        # correction.
        first = _write_node("w1", "pkg/new.py", "create")
        second = _write_node("w2", "pkg/new.py", "edit")
        second["depends_on"] = ["w1"]
        nodes, metadata = self._parse(_payload(first, second))
        self.assertEqual(len(nodes), 2)
        self.assertNotIn("intent_corrections", metadata)

    def test_prefilled_node_is_exempt_from_correction(self) -> None:
        node = _write_node("w1", "pkg/store.py", "edit")
        node["inputs"]["content"] = "VALUE = 1\n"
        nodes, metadata = self._parse(_payload(node))
        self.assertEqual(nodes[0].inputs["content_intent"], "edit")
        self.assertNotIn("intent_corrections", metadata)

    def test_correction_applies_before_remaining_validation(self) -> None:
        # The skeleton carries BOTH a correctable intent defect
        # ("create" for the existing server.py) and an unrelated
        # clause-coverage defect. Validation judges the CORRECTED
        # plan: the rejection carries clause_coverage but no
        # skeleton_intent, and the correction rides the rejection
        # for the attempt record / repair brief.
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                _payload(
                    _write_node(
                        "w1",
                        "pkg/server.py",
                        "create",
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
        self.assertNotIn("skeleton_intent", codes)
        corrections = caught.exception.intent_corrections
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["target"], "pkg/server.py")
        self.assertEqual(corrections[0]["to"], "edit")


class TestTargetFactsBlock(_TreeCase):
    def test_block_names_existence_legal_intents_and_corrections(
        self,
    ) -> None:
        response = _payload(
            _write_node("w1", "pkg/server.py", "create"),
            _write_node("w2", "pkg/store.py", "edit"),
        )
        corrections = [
            {
                "node": "node-1",
                "target": "pkg/server.py",
                "from": "create",
                "to": "edit",
                "reason": "the target already exists on disk",
            },
            {
                "node": "node-2",
                "target": "pkg/store.py",
                "from": "edit",
                "to": "create",
                "reason": "the target does not exist on disk",
            },
        ]
        block = _skeleton_target_facts_block(
            response, self.root, corrections
        )
        self.assertIn("SKELETON TARGET FACTS", block)
        self.assertIn("pkg/server.py", block)
        self.assertIn("EXISTS on disk", block)
        self.assertIn("definitions", block)
        self.assertIn('corrected that declaration to "edit"', block)
        self.assertIn("pkg/store.py", block)
        self.assertIn("does not exist yet", block)
        self.assertIn('Legal content intents: "create"', block)
        self.assertIn('corrected that declaration to "create"', block)

    def test_block_is_empty_without_write_targets(self) -> None:
        self.assertEqual(
            _skeleton_target_facts_block(
                _payload(_bash_node("b1")), self.root
            ),
            "",
        )
        self.assertEqual(
            _skeleton_target_facts_block("not json", self.root), ""
        )


# The skeleton arm #20 needed and never produced: "create"
# declared for the existing server module. Under build #11 the
# system corrects it to "edit" and the first attempt stands.
CORRECTED_SKELETON = _payload(
    _write_node("w-server", "pkg/server.py", "create"),
    _bash_node("run", "true", depends_on=["w-server"]),
)


class _Dispatcher:
    """call_llm stand-in: a queue of skeleton responses, then
    per-target fill payloads; every prompt is recorded."""

    def __init__(self, skeletons, fills):
        self.skeletons = list(skeletons)
        self.fills = fills
        self.skeleton_prompts: list[str] = []
        self.fill_prompts: list[tuple[str, str, str]] = []

    def __call__(self, system_prompt, user_prompt, settings=None):
        if "CONTENT FILL" in user_prompt:
            found = re.search(
                r"writes (\S+) \(content_intent: (\w+)\)", user_prompt
            )
            target, intent = found.group(1), found.group(2)
            self.fill_prompts.append((target, intent, user_prompt))
            return self.fills[target]
        self.skeleton_prompts.append(user_prompt)
        if len(self.skeletons) > 1:
            return self.skeletons.pop(0)
        return self.skeletons[0]


class TestIntentCorrectionEndToEnd(_TreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.intent = TaskIntent(goal="Extend the pairing server")
        self.settings = SETTINGS

    def _plan(self, dispatcher, **kwargs):
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=dispatcher,
        ):
            return plan_task_intent(
                self.intent,
                settings=self.settings,
                repo_root=self.root,
                **kwargs,
            )

    def test_correction_consumes_no_repair_round(self) -> None:
        dispatcher = _Dispatcher(
            [CORRECTED_SKELETON], {"pkg/server.py": GOOD_EDIT}
        )
        plan = self._plan(dispatcher)
        # The skeleton was accepted on its FIRST attempt: the
        # intent defect never reached the repair loop.
        self.assertEqual(len(dispatcher.skeleton_prompts), 1)
        # The fill was requested under the CORRECTED intent.
        self.assertEqual(
            [(target, intent) for target, intent, _ in dispatcher.fill_prompts],
            [("pkg/server.py", "edit")],
        )
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])
        # And the correction is in the plan's own record.
        corrections = plan.metadata["intent_corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["target"], "pkg/server.py")
        self.assertEqual(corrections[0]["from"], "create")
        self.assertEqual(corrections[0]["to"], "edit")

    def test_attempt_record_carries_the_corrections(self) -> None:
        records: list[dict] = []
        dispatcher = _Dispatcher(
            [CORRECTED_SKELETON], {"pkg/server.py": GOOD_EDIT}
        )
        self._plan(dispatcher, attempt_sink=records.append)
        skeleton_records = [
            record
            for record in records
            if record["phase"] == "plan_skeleton"
        ]
        self.assertEqual(len(skeleton_records), 1)
        self.assertEqual(skeleton_records[0]["outcome"], "accepted")
        corrections = skeleton_records[0]["intent_corrections"]
        self.assertEqual(corrections[0]["to"], "edit")
        self.assertEqual(corrections[0]["target"], "pkg/server.py")

    def test_repair_brief_carries_target_facts(self) -> None:
        bad_skeleton = _payload(
            {
                "id": "a",
                "objective": "Teleport the work",
                "depends_on": [],
                "assigned_claw": "teleport",
                "inputs": {},
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            _write_node("w-server", "pkg/server.py", "create"),
        )
        dispatcher = _Dispatcher(
            [bad_skeleton, CORRECTED_SKELETON],
            {"pkg/server.py": GOOD_EDIT},
        )
        plan = self._plan(dispatcher)
        self.assertEqual(len(dispatcher.skeleton_prompts), 2)
        repair_prompt = dispatcher.skeleton_prompts[1]
        self.assertIn("SKELETON TARGET FACTS", repair_prompt)
        self.assertIn("pkg/server.py", repair_prompt)
        self.assertIn("EXISTS on disk", repair_prompt)
        self.assertIn('corrected that declaration to "edit"', repair_prompt)
        self.assertIn("handle_twice", plan.nodes[0].inputs["content"])


# ── The re-plan path: the shared parse helper ───────────────────

CONTRACT_TEXT = """# Pairing contract (fixture)

The pairing service pairs fleet nodes with the control plane.

## Roles

The operator registers a machine and confirms it with a token.

## Store API (store.py)

PairingStore persists pairing records on disk.

## HTTP API (server.py)

The server answers 405 for the wrong method, never the stdlib 501.
"""

SERVER_TEXT = '''"""Pairing HTTP API (fixture)."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class PairingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.send_response(201)
        self.end_headers()

    def do_PUT(self):
        self.send_response(405)
        self.end_headers()


def make_server(host="127.0.0.1", port=0):
    return HTTPServer((host, port), PairingHandler)
'''

TEST_TEXT = '''import unittest


class PairingFixtureTests(unittest.TestCase):
    def test_method_vocabulary(self):
        self.assertIn("pending", ("pending", "confirmed", "claimed"))


if __name__ == "__main__":
    unittest.main()
'''

GOAL = "Implement the pairing service per freetalon/pairing/CONTRACT.md."

SERVER_EDIT = json.dumps(
    {
        "edits": [
            {
                "search": "    def do_PUT(self):",
                "replace": (
                    "    def do_DELETE(self):\n"
                    "        self.send_response(405)\n"
                    "        self.end_headers()\n"
                    "\n"
                    "    def do_PUT(self):"
                ),
            }
        ]
    }
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _raw_node(node_id, claw, inputs, criteria, depends_on=None, outputs=None):
    return {
        "id": node_id,
        "objective": f"Step {node_id}",
        "depends_on": depends_on or [],
        "assigned_claw": claw,
        "inputs": inputs,
        "outputs": outputs if outputs is not None else [],
        "acceptance": ["The step's effect holds."],
        "criteria": criteria,
        "error": None,
    }


class _ScriptedLLM:
    """Build #9's scripted stand-in: skeleton responses in order,
    fill payloads per target; every prompt recorded."""

    def __init__(self, skeleton_responses, fill_payloads=None):
        self.skeleton_responses = list(skeleton_responses)
        self.fill_payloads = {
            key: list(value) for key, value in (fill_payloads or {}).items()
        }
        self.calls: list[str] = []

    def __call__(self, system_prompt, user_prompt, settings=None):
        self.calls.append(user_prompt)
        if "CONTENT FILL" in user_prompt:
            target = re.search(
                r"writes (\S+) \(content_intent", user_prompt
            ).group(1)
            for key, queue in self.fill_payloads.items():
                if key in target:
                    if len(queue) > 1:
                        return queue.pop(0)
                    return queue[0]
            raise AssertionError(f"no fill payload scripted for {target}")
        if len(self.skeleton_responses) > 1:
            return self.skeleton_responses.pop(0)
        return self.skeleton_responses[0]


class TestReplanIntentCorrection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write(self.root / "freetalon" / "__init__.py", "")
        _write(
            self.root / "freetalon" / "pairing" / "CONTRACT.md",
            CONTRACT_TEXT,
        )
        _write(
            self.root / "freetalon" / "pairing" / "server.py", SERVER_TEXT
        )
        (self.root / "tests").mkdir(exist_ok=True)
        specs = goal_named_specs(GOAL, self.root)
        self.clauses = clauses_for_specs(specs)
        self.assertEqual(len(self.clauses), 3)
        self.open_clause = self.clauses[-1]["id"]
        self.server_path = str(
            self.root / "freetalon" / "pairing" / "server.py"
        )
        self.test_path = str(self.root / "tests" / "test_pairing_fixture.py")

    def _brief(self) -> dict:
        return {
            "goal": {"objective": GOAL},
            "failed_nodes": [],
            "measurements": [],
            "memory": [],
            "remaining_budget": {"replans": 2},
            "workspace": str(self.root / "ws"),
            "uncovered_clauses": [
                {
                    "id": self.open_clause,
                    "heading": self.clauses[-1]["heading"],
                    "reasons": ["probe method_surface failing"],
                }
            ],
            "contract_probe_failures": [
                {
                    "clause_id": self.open_clause,
                    "probe": "method_surface",
                    "ok": False,
                    "detail": "handler defines only do_GET/do_POST",
                }
            ],
        }

    def _skeleton(self) -> str:
        # The delta declares "create" for the server module that
        # already exists on disk — arm #20's defect, in the delta
        # phase. Every node declares its clause mapping (the
        # build #7 delta rule); the open clause is implemented by
        # the server node and verified by the test node.
        nodes = [
            _raw_node(
                "step-1",
                "scaffold",
                {
                    "file_path": self.server_path,
                    "content_intent": "create",
                    "clauses": {
                        "implements": [self.open_clause],
                        "verifies": [],
                    },
                },
                [{"type": "file_exists", "path": self.server_path}],
                outputs=[self.server_path],
            ),
            _raw_node(
                "step-2",
                "scaffold",
                {
                    "file_path": self.test_path,
                    "content_intent": "create",
                    "clauses": {
                        "implements": [],
                        "verifies": [self.open_clause],
                    },
                },
                [{"type": "file_exists", "path": self.test_path}],
                depends_on=["step-1"],
                outputs=[self.test_path],
            ),
            _raw_node(
                "step-3",
                "bash",
                {
                    "command": f"python3 {self.test_path}",
                    "clauses": {"implements": [], "verifies": []},
                },
                [{"type": "exit_code", "equals": 0}],
                depends_on=["step-2"],
            ),
        ]
        return json.dumps({"nodes": nodes, "metadata": {}})

    def test_delta_skeleton_intent_corrected_and_written_back(self) -> None:
        llm = _ScriptedLLM(
            [self._skeleton()],
            {
                "server.py": [SERVER_EDIT],
                "test_pairing_fixture.py": [json.dumps({"content": TEST_TEXT})],
            },
        )
        brief = self._brief()
        with mock.patch.object(planner_module, "call_llm", llm):
            nodes = planner_module.replan_from_brief(
                brief, settings=SETTINGS, repo_root=self.root
            )
        self.assertEqual(len(nodes), 3)
        # The correction rode the brief write-back channel.
        corrections = brief["intent_corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["target"], self.server_path)
        self.assertEqual(corrections[0]["from"], "create")
        self.assertEqual(corrections[0]["to"], "edit")
        # And the server fill was requested under the corrected
        # intent (its payload was an edits form, and it applied).
        server_fills = [
            prompt
            for prompt in llm.calls
            if "CONTENT FILL" in prompt and "server.py" in prompt
        ]
        self.assertTrue(server_fills)
        self.assertIn("(content_intent: edit)", server_fills[0])
        by_target = {node.inputs.get("file_path"): node for node in nodes}
        self.assertIn(
            "do_DELETE", by_target[self.server_path].inputs["content"]
        )


if __name__ == "__main__":
    unittest.main()
