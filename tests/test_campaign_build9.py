"""Campaign build #9 (2026-11-24): two-phase re-plan emission +
planner attempt persistence.

Arm #18's terminal failure: planning and content fill were clean
(build #8), the escalation conversion fired with precise smoke
diagnostics in the brief, and then replan_from_brief's SINGLE-SHOT
emission produced a response that was not valid JSON — no v2 was
created and build #7's delta rule went unmeasured for the second
arm running. A repair delta for that failure class rewrites
server.py wholesale, which is not a small response: the
truncation/validity class build #5 eliminated from the initial
planner was alive in the re-plan path. Compounding it, per-attempt
re-plan outputs were persisted nowhere, so which attempt emitted
the invalid JSON was unresolvable from the artifacts (the same
blindness build #8's diagnosis hit for arm #17's fill payloads).

The build: replan_from_brief mirrors build #5 — a delta SKELETON
through the same repair cycle (build #7's delta rule, anchoring,
and re-ask machinery at that phase, budget unchanged), then the
per-node CONTENT FILL through the shared _fill_plan_contents (the
same validators and build #8 shape-repair blocks as the initial
planner), then ASSEMBLY through the ordinary full parse under the
delta rule and the contract pin. And every planner attempt —
initial skeleton, initial fill, re-plan skeleton, re-plan fill —
is persisted with its phase, attempt number, outcome, and raw
text: to stdout as a labeled block, and to the installed attempt
sink (LoopRunner.run_plan installs one bound to the run store, so
a run's attempts land in planner_attempts.jsonl).

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import clauses_for_specs
from freetalon.orchestrator.intake import LLMSettings
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.repo_index import goal_named_specs
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

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
    """A call_llm stand-in for the two-phase re-plan.

    Non-fill calls (the skeleton phase and its repairs) consume
    ``skeleton_responses`` in order (the last repeats); fill calls
    — recognized by the CONTENT FILL marker — consume the payload
    queue for the named target (the last repeats). Every call is
    recorded for prompt assertions.
    """

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

    def fill_calls(self, target: str) -> list[str]:
        """The fill prompts addressed to exactly ``target``.

        Matched on the prompt's own "writes <target>" header, not
        a substring: every fill prompt embeds the compact skeleton,
        which names every node's target.
        """
        matched = []
        for prompt in self.calls:
            if "CONTENT FILL" not in prompt:
                continue
            found = re.search(r"writes (\S+) \(content_intent", prompt)
            if found and found.group(1) == target:
                matched.append(prompt)
        return matched

    def skeleton_calls(self) -> list[str]:
        return [
            prompt for prompt in self.calls if "CONTENT FILL" not in prompt
        ]


class ReplanTwoPhaseCase(unittest.TestCase):
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
        self.assertIn("http-api", self.open_clause)
        self.server_path = str(
            self.root / "freetalon" / "pairing" / "server.py"
        )
        self.test_path = str(
            self.root / "tests" / "test_pairing_fixture.py"
        )

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

    def _skeleton(self, declared: bool = True, server_intent: str = "edit"):
        server_inputs: dict = {
            "file_path": self.server_path,
            "content_intent": server_intent,
        }
        if server_intent == "whole_file":
            server_inputs["overwrite_justification"] = (
                "the handler must be rewritten whole: its method "
                "surface is wrong in every branch"
            )
        test_inputs: dict = {
            "file_path": self.test_path,
            "content_intent": "create",
        }
        run_inputs: dict = {"command": f"python3 {self.test_path}"}
        if declared:
            server_inputs["clauses"] = {
                "implements": [self.open_clause],
                "verifies": [],
            }
            test_inputs["clauses"] = {
                "implements": [],
                "verifies": [self.open_clause],
            }
            run_inputs["clauses"] = {"implements": [], "verifies": []}
        nodes = [
            _raw_node(
                "step-1",
                "scaffold",
                server_inputs,
                [{"type": "file_exists", "path": self.server_path}],
                outputs=[self.server_path],
            ),
            _raw_node(
                "step-2",
                "scaffold",
                test_inputs,
                [{"type": "file_exists", "path": self.test_path}],
                depends_on=["step-1"],
                outputs=[self.test_path],
            ),
            _raw_node(
                "step-3",
                "bash",
                run_inputs,
                [{"type": "exit_code", "equals": 0}],
                depends_on=["step-2"],
            ),
        ]
        if server_intent == "whole_file":
            # The impact-check companion the whole-file override
            # demands: a bash node depending on the rewrite and
            # naming its target.
            check_inputs: dict = {
                "command": f"python3 {self.test_path}",
                "impact_check_for": self.server_path,
            }
            if declared:
                check_inputs["clauses"] = {
                    "implements": [],
                    "verifies": [],
                }
            nodes.append(
                _raw_node(
                    "step-4",
                    "bash",
                    check_inputs,
                    [{"type": "exit_code", "equals": 0}],
                    depends_on=["step-1", "step-2"],
                )
            )
        return json.dumps({"nodes": nodes, "metadata": {}})

    def _run_replan(self, llm, brief=None, **kwargs):
        brief = brief if brief is not None else self._brief()
        with mock.patch.object(planner_module, "call_llm", llm):
            nodes = planner_module.replan_from_brief(
                brief, settings=SETTINGS, repo_root=self.root, **kwargs
            )
        return nodes, brief

    def _good_fills(self, **overrides):
        fills = {
            "server.py": [SERVER_EDIT],
            "test_pairing_fixture.py": [json.dumps({"content": TEST_TEXT})],
        }
        fills.update(overrides)
        return fills


# ── Part 1: two-phase emission, end to end ──────────────────────


class TestTwoPhaseReplan(ReplanTwoPhaseCase):
    def test_skeleton_accepted_and_filled_end_to_end(self):
        llm = _ScriptedLLM([self._skeleton()], self._good_fills())
        nodes, brief = self._run_replan(llm)
        by_target = {
            (node.inputs or {}).get("file_path"): node for node in nodes
        }
        server = by_target[self.server_path]
        # The edit fill materialized against the existing file.
        self.assertIn("do_DELETE", server.inputs["content"])
        self.assertIn("PairingHandler", server.inputs["content"])
        test_node = by_target[self.test_path]
        self.assertEqual(test_node.inputs["content"], TEST_TEXT)
        # No skeleton-phase response carried any file content: the
        # plan was assembled from per-node payloads only.
        self.assertEqual(len(llm.skeleton_calls()), 1)
        self.assertEqual(len(llm.fill_calls(self.server_path)), 1)
        self.assertEqual(len(llm.fill_calls(self.test_path)), 1)
        # The loop's metadata channels are unchanged: the delta's
        # declared coverage and (new) the emission record ride the
        # brief back.
        coverage = brief["replan_clause_coverage"]
        self.assertEqual(coverage["required"], [self.open_clause])
        self.assertEqual(coverage["implements"], [self.open_clause])
        self.assertEqual(coverage["verifies"], [self.open_clause])
        emission = brief["plan_emission"]
        self.assertEqual(
            emission["phases"], ["replan_skeleton", "replan_content_fill"]
        )
        self.assertEqual(
            emission["content_attempts"], {"node-1": 1, "node-2": 1}
        )

    def test_delta_rule_failure_is_reasked_at_skeleton_phase(self):
        llm = _ScriptedLLM(
            [self._skeleton(declared=False), self._skeleton(declared=True)],
            self._good_fills(),
        )
        records: list[dict] = []
        nodes, brief = self._run_replan(llm, attempt_sink=records.append)
        self.assertEqual(len(nodes), 3)
        skeleton_prompts = llm.skeleton_calls()
        self.assertEqual(len(skeleton_prompts), 2)
        # The repair prompt names the clause rejection — the delta
        # rule judged the skeleton inside the repair cycle, not
        # after it (arm #16's wholesale terminal rejection).
        self.assertIn("REJECTED", skeleton_prompts[1])
        self.assertIn("clause", skeleton_prompts[1].lower())
        rejected = [
            record
            for record in records
            if record["phase"] == "replan_skeleton"
            and record["outcome"] == "rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn("clause_coverage", rejected[0]["error_codes"])

    def test_oversized_delta_completes_via_fill(self):
        # The arm #18 shape: the existing server is large and the
        # delta rewrites it whole. No single response may carry
        # the whole plan plus contents — the skeleton is structure
        # only, the large content rides its own fill call.
        # Campaign build #30: this whole_file node is a repair
        # node (standing file, overwrite_justification, the open
        # clause), so its fill rides PATCH form — SERVER_EDIT,
        # whose application produces exactly big_fill. The build
        # #9 claim is unchanged and the payload is now smaller
        # than the file it repairs, not merely separate from the
        # skeleton.
        big_existing = SERVER_TEXT + "\n" + "\n".join(
            f"# standing line {index}" for index in range(300)
        )
        _write(
            self.root / "freetalon" / "pairing" / "server.py", big_existing
        )
        big_fill = big_existing.replace(
            "    def do_PUT(self):",
            "    def do_DELETE(self):\n"
            "        self.send_response(405)\n"
            "        self.end_headers()\n"
            "\n"
            "    def do_PUT(self):",
        )
        llm = _ScriptedLLM(
            [self._skeleton(server_intent="whole_file")],
            self._good_fills(**{"server.py": [SERVER_EDIT]}),
        )
        nodes, brief = self._run_replan(llm)
        server = next(
            node
            for node in nodes
            if (node.inputs or {}).get("file_path") == self.server_path
        )
        self.assertEqual(server.inputs["content"], big_fill)
        for prompt in llm.skeleton_calls():
            self.assertLess(len(prompt), len(big_fill))
        skeleton_responses = [
            response
            for response in llm.skeleton_responses
        ]
        for response in skeleton_responses:
            self.assertNotIn("standing line", response)
        self.assertEqual(
            brief["plan_emission"]["content_attempts"]["node-1"], 1
        )

    def test_fill_shape_failure_uses_build8_repair_block(self):
        llm = _ScriptedLLM(
            [self._skeleton()],
            self._good_fills(
                **{
                    "test_pairing_fixture.py": [
                        "{}",
                        json.dumps({"content": TEST_TEXT}),
                    ]
                }
            ),
        )
        nodes, brief = self._run_replan(llm)
        test_fills = llm.fill_calls(self.test_path)
        self.assertEqual(len(test_fills), 2)
        # The retry carries build #8's schema-explicit block: the
        # defect named in plain words, not the bare validator
        # sentence.
        self.assertIn(
            "did not match the required payload shape", test_fills[1]
        )
        self.assertIn("empty JSON object", test_fills[1])
        self.assertEqual(
            brief["plan_emission"]["content_attempts"]["node-2"], 2
        )


# ── Part 2: attempt persistence ─────────────────────────────────


class TestAttemptPersistence(ReplanTwoPhaseCase):
    def test_failed_replan_attempts_are_persisted_with_outcome(self):
        # Arm #18's exact terminal shape: the first re-plan
        # response is not valid JSON. Under build #9 the attempt
        # is recorded — phase, number, outcome, raw text — and the
        # repair cycle recovers.
        llm = _ScriptedLLM(
            ["this is not json at all", self._skeleton()],
            self._good_fills(),
        )
        records: list[dict] = []
        nodes, _brief = self._run_replan(llm, attempt_sink=records.append)
        self.assertEqual(len(nodes), 3)
        first = records[0]
        self.assertEqual(first["phase"], "replan_skeleton")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["outcome"], "rejected")
        self.assertEqual(
            first["response_text"], "this is not json at all"
        )
        self.assertIn("not valid JSON", first["errors"])
        accepted = [
            record
            for record in records
            if record["phase"] == "replan_skeleton"
            and record["outcome"] == "accepted"
        ]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["attempt"], 2)
        fill_records = [
            record
            for record in records
            if record["phase"] == "replan_content_fill"
        ]
        self.assertEqual(len(fill_records), 2)
        self.assertTrue(
            all(
                record["outcome"] == "accepted"
                and record["node_id"]
                and record["target"]
                for record in fill_records
            )
        )

    def test_run_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            record = {
                "phase": "replan_skeleton",
                "attempt": 1,
                "outcome": "rejected",
                "error_codes": ["clause_coverage"],
                "errors": "boom",
                "node_id": None,
                "target": None,
                "intent": None,
                "response_text": "raw text",
            }
            store.append_planner_attempt("run-1", record)
            store.append_planner_attempt(
                "run-1", {**record, "attempt": 2, "outcome": "accepted"}
            )
            records = store.read_planner_attempts("run-1")
            self.assertEqual(
                [item["attempt"] for item in records], [1, 2]
            )
            self.assertEqual(records[0]["response_text"], "raw text")
            self.assertEqual(store.read_planner_attempts("run-none"), [])

    def test_loop_persists_replan_attempts_to_the_run_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionPlanStateStore(
                db_path=Path(tmp) / "state.db"
            )
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            run_store = RunStore(Path(tmp) / "runs")
            runner = LoopRunner(
                store,
                registry,
                run_store=run_store,
                memory_store=ExperimentStore(Path(tmp) / "memory.db"),
            )
            plan = ExecutionPlan(
                plan_id="plan-build9",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[
                    PlanNode(
                        id="node-1",
                        objective="run the pairing check",
                        assigned_claw="bash",
                        inputs={"command": "false"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the check passes",
                    )
                ],
                metadata={"repo_root": tmp},
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            delta = json.dumps(
                {
                    "nodes": [
                        _raw_node(
                            "step-1",
                            "bash",
                            {"command": "true"},
                            [{"type": "exit_code", "equals": 0}],
                        )
                    ],
                    "metadata": {},
                }
            )
            llm = _ScriptedLLM([delta])
            with mock.patch.object(planner_module, "call_llm", llm):
                result = runner.run_plan(plan, contract)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            attempts = run_store.read_planner_attempts(result.run_id)
            self.assertTrue(attempts)
            skeleton_records = [
                record
                for record in attempts
                if record["phase"] == "replan_skeleton"
            ]
            self.assertEqual(len(skeleton_records), 1)
            self.assertEqual(skeleton_records[0]["outcome"], "accepted")
            self.assertIn("true", skeleton_records[0]["response_text"])


class TestInitialPlanAttemptPersistence(unittest.TestCase):
    """The initial planner's fill attempts persist too (arm #17's
    blindness was a fill-phase blindness)."""

    def test_initial_fill_attempts_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests").mkdir()
            skeleton = json.dumps(
                {
                    "nodes": [
                        _raw_node(
                            "w-store",
                            "scaffold",
                            {
                                "file_path": "pkg/store.py",
                                "content_intent": "create",
                            },
                            [
                                {
                                    "type": "file_exists",
                                    "path": "pkg/store.py",
                                }
                            ],
                            outputs=["pkg/store.py"],
                        ),
                        _raw_node(
                            "w-test",
                            "scaffold",
                            {
                                "file_path": "tests/test_store.py",
                                "content_intent": "create",
                            },
                            [
                                {
                                    "type": "file_exists",
                                    "path": "tests/test_store.py",
                                }
                            ],
                            depends_on=["w-store"],
                            outputs=["tests/test_store.py"],
                        ),
                        _raw_node(
                            "run",
                            "bash",
                            {"command": "true"},
                            [{"type": "exit_code", "equals": 0}],
                            depends_on=["w-store", "w-test"],
                        ),
                    ],
                    "metadata": {},
                }
            )
            store_content = (
                "class Store:\n    def get(self):\n        return 1\n"
            )
            test_content = "def test_store():\n    assert True\n"
            llm = _ScriptedLLM(
                [skeleton],
                {
                    "pkg/store.py": [
                        "{}",
                        json.dumps({"content": store_content}),
                    ],
                    "tests/test_store.py": [
                        json.dumps({"content": test_content})
                    ],
                },
            )
            records: list[dict] = []
            intent = TaskIntent(goal="Create a store module in pkg")
            with mock.patch.object(planner_module, "call_llm", llm):
                plan = planner_module.plan_task_intent(
                    intent,
                    settings=SETTINGS,
                    repo_root=root,
                    attempt_sink=records.append,
                )
            self.assertEqual(len(plan.nodes), 3)
            skeleton_records = [
                record
                for record in records
                if record["phase"] == "plan_skeleton"
            ]
            self.assertEqual(len(skeleton_records), 1)
            self.assertEqual(skeleton_records[0]["outcome"], "accepted")
            store_fills = [
                record
                for record in records
                if record["phase"] == "content_fill"
                and record["target"] == "pkg/store.py"
            ]
            self.assertEqual(
                [record["outcome"] for record in store_fills],
                ["rejected", "accepted"],
            )
            self.assertEqual(
                [record["attempt"] for record in store_fills], [1, 2]
            )
            self.assertIn(
                "empty JSON object", store_fills[0]["errors"]
            )


if __name__ == "__main__":
    unittest.main()
