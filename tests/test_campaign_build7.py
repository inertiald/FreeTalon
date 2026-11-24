"""Campaign build #7 (2026-11-24): clause discipline for the
re-plan path.

Arm #16's terminal failure: build #6's machinery converted the
escalation and briefed the re-plan with the probe failures, but
replan_from_brief's output was judged by the initial planner's
FULL-coverage clause rule — the re-plan declared no clause
mappings at all, the rejection was 24 full-coverage errors, and
the run escalated with 2 of 3 re-plan units unspent. No v2 ever
landed, so the server repair was never attempted.

The build: the re-plan prompt carries the frozen clause list and
the brief's open clauses under a DELTA rule (every emitted node
declares inputs.clauses; each open clause is implemented and
verified within the delta; declared IDs must exist in the frozen
list), the delta rule validates inside replan_from_brief's own
repair cycle so coverage errors feed constrained re-asks, the
shared anchoring gains delta facts (open-clause coverage +
declared-node counts), and the accepted delta's declared coverage
is written back through the brief and stamped by the loop into
the new plan version's metadata.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import loop as loop_module  # noqa: F401  (house idiom)
from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    clause_coverage_errors,
    clause_delta_errors,
    clauses_for_specs,
    delta_clause_coverage,
)
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
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

CLAUSES = [
    {"id": "C01-roles", "heading": "Roles"},
    {"id": "C02-store-api", "heading": "Store API"},
    {"id": "C03-http-api", "heading": "HTTP API"},
]


def _node(node_id: str, clauses_marker) -> PlanNode:
    """A PlanNode whose inputs carry the given clauses value.

    clauses_marker is the module-level ABSENT sentinel for a node
    with no inputs.clauses key at all, else the raw value to carry
    (a dict, or a malformed value for the malformed test).
    """
    inputs: dict = {"command": "true"}
    if clauses_marker is not ABSENT:
        inputs["clauses"] = clauses_marker
    return PlanNode(
        id=node_id,
        objective=f"step {node_id}",
        assigned_claw="bash",
        inputs=inputs,
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the step holds",
    )


class _Absent:
    pass


ABSENT = _Absent()


# ── Part 1: the delta rule itself ────────────────────────────────


class TestClauseDeltaErrors(unittest.TestCase):
    def test_absent_mapping_is_an_undeclared_error(self):
        errors = clause_delta_errors([_node("node-1", ABSENT)], CLAUSES, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("node-1", errors[0])
        self.assertIn("declares no contract-clause mapping", errors[0])

    def test_empty_declaration_is_a_declaration(self):
        node = _node("node-1", {"implements": [], "verifies": []})
        self.assertEqual(clause_delta_errors([node], CLAUSES, []), [])

    def test_required_clause_must_be_implemented(self):
        node = _node("node-1", {"implements": [], "verifies": ["C03-http-api"]})
        errors = clause_delta_errors([node], CLAUSES, ["C03-http-api"])
        self.assertEqual(len(errors), 1)
        self.assertIn("C03-http-api", errors[0])
        self.assertIn("not implemented by any node in this re-plan", errors[0])

    def test_required_clause_must_be_verified(self):
        node = _node("node-1", {"implements": ["C03-http-api"], "verifies": []})
        errors = clause_delta_errors([node], CLAUSES, ["C03-http-api"])
        self.assertEqual(len(errors), 1)
        self.assertIn("C03-http-api", errors[0])
        self.assertIn("not verified by any node in this re-plan", errors[0])

    def test_required_clause_covered_is_clean(self):
        nodes = [
            _node("node-1", {"implements": ["C03-http-api"], "verifies": []}),
            _node("node-2", {"implements": [], "verifies": ["C03-http-api"]}),
        ]
        self.assertEqual(
            clause_delta_errors(nodes, CLAUSES, ["C03-http-api"]), []
        )

    def test_non_required_clause_needs_no_coverage(self):
        # The delta rule's defining difference from the full rule:
        # clauses the brief does not name as open are not the
        # delta's burden. The full rule flags the same node set.
        nodes = [
            _node(
                "node-1",
                {
                    "implements": ["C03-http-api"],
                    "verifies": ["C03-http-api"],
                },
            )
        ]
        self.assertEqual(
            clause_delta_errors(nodes, CLAUSES, ["C03-http-api"]), []
        )
        full_errors = clause_coverage_errors(nodes, CLAUSES)
        self.assertTrue(
            any("C01-roles" in error for error in full_errors),
            f"full rule should flag C01-roles: {full_errors}",
        )

    def test_invented_clause_id_is_an_error(self):
        node = _node("node-1", {"implements": ["C99-invented"], "verifies": []})
        errors = clause_delta_errors([node], CLAUSES, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("C99-invented", errors[0])
        self.assertIn("not in the frozen clause list", errors[0])

    def test_malformed_declaration_is_an_error(self):
        node = _node("node-1", ["C03-http-api"])
        errors = clause_delta_errors([node], CLAUSES, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("inputs.clauses must be an object", errors[0])

    def test_required_id_outside_the_frozen_list_is_not_required(self):
        # The judge computes the open set from the frozen list, so
        # this cannot arrive from the loop; the rule still refuses
        # to demand coverage of a clause that does not exist.
        node = _node("node-1", {"implements": [], "verifies": []})
        self.assertEqual(
            clause_delta_errors([node], CLAUSES, ["C99-invented"]), []
        )

    def test_delta_clause_coverage_record(self):
        nodes = [
            _node(
                "node-1",
                {
                    "implements": ["C03-http-api"],
                    "verifies": ["C01-roles", "C03-http-api", "C99-invented"],
                },
            ),
            _node("node-2", ABSENT),
        ]
        self.assertEqual(
            delta_clause_coverage(nodes, CLAUSES),
            {
                "implements": ["C03-http-api"],
                "verifies": ["C01-roles", "C03-http-api"],
                "addressed": ["C01-roles", "C03-http-api"],
            },
        )


# ── Part 2: anchoring parity (delta facts) ───────────────────────


def _raw_response(nodes: list[dict]) -> str:
    return json.dumps({"nodes": nodes})


def _raw_bash(node_id: str, clauses_marker) -> dict:
    inputs: dict = {"command": "true"}
    if clauses_marker is not ABSENT:
        inputs["clauses"] = clauses_marker
    return {
        "id": node_id,
        "objective": f"step {node_id}",
        "depends_on": [],
        "assigned_claw": "bash",
        "inputs": inputs,
        "outputs": [],
        "acceptance": ["The step's effect holds."],
        "criteria": [{"type": "exit_code", "equals": 0}],
        "error": None,
    }


class TestDeltaAnchoringFacts(unittest.TestCase):
    def test_attempt_facts_carry_delta_measures(self):
        response = _raw_response(
            [
                _raw_bash(
                    "step-1",
                    {
                        "implements": ["C03-http-api"],
                        "verifies": ["C03-http-api"],
                    },
                ),
                _raw_bash("step-2", ABSENT),
            ]
        )
        facts = planner_module._plan_attempt_facts(
            response, [], None, CLAUSES, ["C03-http-api"]
        )
        self.assertEqual(facts["required_total"], 1)
        self.assertEqual(facts["required_implemented"], 1)
        self.assertEqual(facts["required_verified"], 1)
        self.assertEqual(facts["nodes_total"], 2)
        self.assertEqual(facts["nodes_declared"], 1)

    def test_attempt_facts_without_a_required_set_have_no_delta_keys(self):
        response = _raw_response([_raw_bash("step-1", ABSENT)])
        facts = planner_module._plan_attempt_facts(
            response, [], None, CLAUSES
        )
        self.assertNotIn("required_total", facts)
        self.assertNotIn("nodes_declared", facts)

    def test_anchoring_names_open_clause_and_declaration_regressions(self):
        good = planner_module._plan_attempt_facts(
            _raw_response(
                [
                    _raw_bash(
                        "step-1",
                        {
                            "implements": ["C03-http-api"],
                            "verifies": ["C03-http-api"],
                        },
                    )
                ]
            ),
            [],
            None,
            CLAUSES,
            ["C03-http-api"],
        )
        blind = planner_module._plan_attempt_facts(
            _raw_response([_raw_bash("step-1", ABSENT)]),
            [],
            None,
            CLAUSES,
            ["C03-http-api"],
        )
        constraints = planner_module._attempt_anchoring_constraints(
            [good, blind]
        )
        self.assertTrue(
            any("Open-clause" in text for text in constraints),
            f"constraints: {constraints}",
        )
        self.assertTrue(
            any("inputs.clauses" in text for text in constraints),
            f"constraints: {constraints}",
        )
        block = planner_module._attempt_anchoring_block([good, blind])
        self.assertIn("Open-clause declarations", block)
        self.assertIn("DO NOT REGRESS", block)


# ── Part 3: replan_from_brief end to end (mocked LLM) ────────────

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


class ReplanClauseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / "freetalon" / "__init__.py", "")
        _write(
            self.root / "freetalon" / "pairing" / "CONTRACT.md",
            CONTRACT_TEXT,
        )
        specs = goal_named_specs(GOAL, self.root)
        self.clauses = clauses_for_specs(specs)
        self.assertEqual(len(self.clauses), 3)
        self.open_clause = self.clauses[-1]["id"]
        self.assertIn("http-api", self.open_clause)

    def tearDown(self) -> None:
        self._tmp.cleanup()

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

    def _delta(self, declared: bool, claw: str = "scaffold") -> list[dict]:
        server_path = str(
            self.root / "freetalon" / "pairing" / "server.py"
        )
        test_path = str(self.root / "tests" / "test_pairing_fixture.py")
        server_inputs: dict = {
            "file_path": server_path,
            "content": SERVER_TEXT,
        }
        test_inputs: dict = {"file_path": test_path, "content": TEST_TEXT}
        run_inputs: dict = {"command": f"python3 {test_path}"}
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
        return [
            _raw_node(
                "step-1",
                claw,
                server_inputs,
                [{"type": "file_exists", "path": server_path}],
                outputs=[server_path],
            ),
            _raw_node(
                "step-2",
                "scaffold",
                test_inputs,
                [{"type": "file_exists", "path": test_path}],
                depends_on=["step-1"],
                outputs=[test_path],
            ),
            _raw_node(
                "step-3",
                "bash",
                run_inputs,
                [{"type": "exit_code", "equals": 0}],
                depends_on=["step-2"],
            ),
        ]

    def _run_replan(self, responses: list[str], brief: dict | None = None):
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            index = min(len(calls) - 1, len(responses) - 1)
            return responses[index]

        brief = brief if brief is not None else self._brief()
        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            nodes = planner_module.replan_from_brief(
                brief, settings=SETTINGS, repo_root=self.root
            )
        return nodes, calls, brief


class TestReplanClauseDiscipline(ReplanClauseCase):
    def test_prompt_carries_frozen_list_open_clauses_and_delta_rule(self):
        nodes, calls, brief = self._run_replan(
            [json.dumps({"nodes": self._delta(declared=True)})]
        )
        self.assertEqual(len(nodes), 3)
        prompt = calls[0][1]
        self.assertIn("CONTRACT CLAUSES (re-plan)", prompt)
        for clause in self.clauses:
            self.assertIn(clause["id"], prompt)
        self.assertIn("OPEN clauses this re-plan must address", prompt)
        self.assertIn("EVERY node you emit must declare", prompt)
        coverage = brief["replan_clause_coverage"]
        self.assertEqual(coverage["required"], [self.open_clause])
        self.assertEqual(coverage["implements"], [self.open_clause])
        self.assertEqual(coverage["verifies"], [self.open_clause])
        self.assertEqual(coverage["addressed"], [self.open_clause])

    def test_clause_blind_output_is_reasked_inside_the_repair_cycle(self):
        responses = [
            json.dumps({"nodes": self._delta(declared=False)}),
            json.dumps({"nodes": self._delta(declared=True)}),
        ]
        nodes, calls, brief = self._run_replan(responses)
        self.assertEqual(len(nodes), 3)
        self.assertGreaterEqual(len(calls), 2)
        repair_prompt = calls[1][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("declares no contract-clause mapping", repair_prompt)
        self.assertEqual(
            brief["replan_clause_coverage"]["required"], [self.open_clause]
        )

    def test_clause_blind_forever_fails_with_clause_errors(self):
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return json.dumps({"nodes": self._delta(declared=False)})

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            with self.assertRaises(LLMResponseError) as caught:
                planner_module.replan_from_brief(
                    self._brief(), settings=SETTINGS, repo_root=self.root
                )
        codes = {
            record.code
            for record in getattr(caught.exception, "plan_errors", []) or []
        }
        self.assertIn("clause_coverage", codes)
        # The repair budget is spent, not bypassed: the initial
        # call plus at most max_repairs (2) re-asks (a stuck
        # escalation on the third identical rejection is the same
        # budget ending by the cycle's other door).
        self.assertGreaterEqual(len(calls), 2)
        self.assertLessEqual(len(calls), 3)

    def test_anchoring_block_reaches_the_third_replan_prompt(self):
        responses = [
            # Attempt 1: full declarations, one bogus claw — a
            # non-clause rejection whose facts still record the
            # declarations (the lenient fact extraction).
            json.dumps({"nodes": self._delta(declared=True, claw="bogus")}),
            # Attempt 2: clause-blind — the regression the
            # anchoring exists to name.
            json.dumps({"nodes": self._delta(declared=False)}),
            # Attempt 3: compliant.
            json.dumps({"nodes": self._delta(declared=True)}),
        ]
        nodes, calls, _brief = self._run_replan(responses)
        self.assertEqual(len(nodes), 3)
        self.assertGreaterEqual(len(calls), 3)
        third_prompt = calls[2][1]
        self.assertIn("PLAN-ATTEMPT ANCHORING", third_prompt)
        self.assertIn("Open-clause declarations", third_prompt)
        self.assertIn("DO NOT REGRESS", third_prompt)

    def test_replan_without_a_contract_behaves_as_before(self):
        brief = {
            "goal": {"objective": "Fix the widget."},
            "failed_nodes": [],
            "workspace": str(self.root / "ws"),
        }
        node = _raw_node(
            "step-1",
            "bash",
            {"command": "true"},
            [{"type": "exit_code", "equals": 0}],
        )
        nodes, calls, brief = self._run_replan(
            [json.dumps({"nodes": [node]})], brief=brief
        )
        self.assertEqual(len(nodes), 1)
        self.assertNotIn("CONTRACT CLAUSES (re-plan)", calls[0][1])
        self.assertNotIn("replan_clause_coverage", brief)


# ── Part 4: the loop stamps the delta's coverage ─────────────────


class TestLoopStampsDeltaCoverage(unittest.TestCase):
    def test_v2_metadata_keeps_frozen_list_and_records_the_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            runner = LoopRunner(
                store,
                registry,
                run_store=RunStore(Path(tmp) / "runs"),
                memory_store=ExperimentStore(Path(tmp) / "memory.db"),
            )
            clauses = [
                {
                    "id": "C03-http-api-server-py",
                    "spec": "freetalon/pairing/CONTRACT.md",
                    "heading": "HTTP API (server.py)",
                    "text": "## HTTP API (server.py)\n",
                    "declared_files": [],
                    "method_surface": False,
                }
            ]
            plan = ExecutionPlan(
                plan_id="plan-build7",
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
                metadata={
                    "contract_clauses": clauses,
                    "repo_root": tmp,
                },
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            record = {
                "required": ["C03-http-api-server-py"],
                "implements": ["C03-http-api-server-py"],
                "verifies": ["C03-http-api-server-py"],
                "addressed": ["C03-http-api-server-py"],
            }

            def replan_fn(brief):
                # replan_from_brief's write-back, simulated: the
                # accepted delta's declared coverage rides the
                # brief back to the loop.
                brief["replan_clause_coverage"] = dict(record)
                return [
                    PlanNode(
                        id="node-1",
                        objective="run the pairing check",
                        assigned_claw="bash",
                        inputs={"command": "true"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the check passes",
                    )
                ]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            saved = runner._store.load("plan-build7")
            self.assertIsNotNone(saved)
            self.assertEqual(
                saved.metadata.get("contract_clauses"), clauses
            )
            self.assertEqual(
                saved.metadata.get("replan_clause_coverage"),
                [{**record, "plan_version": 2}],
            )


if __name__ == "__main__":
    unittest.main()
