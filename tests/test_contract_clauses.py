"""Campaign build #3 (2026-11-23), part 3: the contract-probe layer.

The goal-named contract compiles deterministically into a frozen
clause list (one clause per ``## `` section); the planner binds
clauses to plan nodes (inputs.clauses implements/verifies) and
parse validates the binding; the loop runs deterministic probes
(declared files exist; the HTTP method surface) outside the
model's own tests; re-plan briefs carry the uncovered clauses;
and the goal verdict refuses a PASS while a clause is neither
verified nor explicitly waived — PARTIAL / INCONCLUSIVE name the
other honest endings.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import loop as loop_module
from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    clause_coverage_errors,
    clause_states,
    clauses_for_specs,
    coverage_map,
    extract_clauses,
    run_contract_probes,
    uncovered_clauses,
)
from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.judge import Judge
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.planner import parse_planner_response
from freetalon.orchestrator.repo_index import SpecDocument
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "freetalon" / "pairing" / "CONTRACT.md"

GOAL = "Implement the fixture service described by SPEC.md."


def _spec(text: str, path: str = "pkg/SPEC.md") -> SpecDocument:
    import hashlib

    raw = text.encode("utf-8")
    return SpecDocument(
        path=path, sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw), text=text,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── Extraction ──────────────────────────────────────────────────────


class TestExtraction(unittest.TestCase):
    def test_real_contract_shape(self):
        spec = _spec(
            CONTRACT_PATH.read_text(encoding="utf-8"),
            path="freetalon/pairing/CONTRACT.md",
        )
        clauses = extract_clauses(spec)
        self.assertEqual(len(clauses), 12)
        by_id = {c["id"]: c for c in clauses}
        # The entry-point clause: python -m freetalon.pairing
        # declares __main__.py without naming the file.
        server_construction = by_id["C10-server-construction-server-py"]
        self.assertIn(
            "freetalon/pairing/__main__.py",
            server_construction["declared_files"],
        )
        self.assertIn(
            "freetalon/pairing/server.py", server_construction["declared_files"]
        )
        # The HTTP API clause carries the method-surface probe.
        http_api = by_id["C09-http-api-server-py"]
        self.assertTrue(http_api["method_surface"])
        self.assertIn("freetalon/pairing/server.py", http_api["declared_files"])
        # The store clause declares store.py from its heading.
        store_api = by_id["C07-store-api-store-py-and-exceptions"]
        self.assertEqual(store_api["declared_files"], ["freetalon/pairing/store.py"])

    def test_deterministic(self):
        spec = _spec(CONTRACT_PATH.read_text(encoding="utf-8"),
                     path="freetalon/pairing/CONTRACT.md")
        self.assertEqual(extract_clauses(spec), extract_clauses(spec))
        self.assertEqual(clauses_for_specs([spec]), clauses_for_specs([spec]))

    def test_preamble_yields_no_clause_and_ids_are_stable(self):
        spec = _spec(
            "# Title\n\nFraming text, not obligations.\n\n"
            "## First Section\n\nBody one.\n\n"
            "## Second (with parens)\n\nBody two.\n"
        )
        clauses = extract_clauses(spec)
        self.assertEqual(
            [c["id"] for c in clauses],
            ["C01-first-section", "C02-second-with-parens"],
        )
        self.assertEqual(clauses[0]["spec"], "pkg/SPEC.md")


# ── Coverage mapping validation ─────────────────────────────────────

CLAUSES = [
    {"id": "C01-alpha", "spec": "pkg/SPEC.md", "heading": "Alpha",
     "text": "## Alpha\n", "declared_files": [], "method_surface": False},
    {"id": "C02-beta", "spec": "pkg/SPEC.md", "heading": "Beta",
     "text": "## Beta\n", "declared_files": [], "method_surface": False},
]


def _node(node_id: str, clauses_inputs: dict | None = None) -> PlanNode:
    inputs: dict = {}
    if clauses_inputs is not None:
        inputs["clauses"] = clauses_inputs
    return PlanNode(
        id=node_id, objective=f"Node {node_id}", assigned_claw="bash",
        inputs=inputs, criteria=[{"type": "exit_code", "equals": 0}],
    )


class TestCoverageValidation(unittest.TestCase):
    def test_complete_mapping_has_no_errors(self):
        nodes = [
            _node("node-1", {"implements": ["C01-alpha", "C02-beta"]}),
            _node("node-2", {"verifies": ["C01-alpha", "C02-beta"]}),
        ]
        self.assertEqual(clause_coverage_errors(nodes, CLAUSES), [])
        self.assertEqual(
            coverage_map(nodes)["C01-alpha"],
            {"implement": ["node-1"], "verify": ["node-2"]},
        )

    def test_missing_implementation_and_verification(self):
        errors = clause_coverage_errors([_node("node-1")], CLAUSES)
        self.assertEqual(len(errors), 4)  # 2 clauses x (implement + verify)
        self.assertTrue(any("no implementation node" in e for e in errors))
        self.assertTrue(any("no verification node" in e for e in errors))

    def test_invented_id_is_an_error(self):
        nodes = [
            _node("node-1", {"implements": ["C01-alpha", "C02-beta", "C99-nope"],
                             "verifies": ["C01-alpha", "C02-beta"]}),
        ]
        errors = clause_coverage_errors(nodes, CLAUSES)
        self.assertEqual(len(errors), 1)
        self.assertIn("C99-nope", errors[0])

    def test_malformed_declaration_is_an_error(self):
        nodes = [_node("node-1", {"implements": "C01-alpha"})]
        errors = clause_coverage_errors(nodes, CLAUSES)
        self.assertTrue(any("must be an array" in e for e in errors))

    def test_parse_rejects_uncovered_plan_and_accepts_covered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_path = root / "pkg" / "mod.py"
            raw_nodes = [
                {
                    "id": "step_1",
                    "objective": "Write the module",
                    "depends_on": [],
                    "assigned_claw": "scaffold",
                    "inputs": {
                        "file_path": str(module_path),
                        "content": '"""Fixture module."""\n\n\ndef compute():\n    return 1\n',
                        "clauses": {"implements": ["C01-alpha", "C02-beta"]},
                    },
                    "outputs": [str(module_path)],
                    "acceptance": ["The module exists."],
                    "criteria": [{"type": "file_exists", "path": str(module_path)}],
                    "error": None,
                },
                {
                    "id": "step_2",
                    "objective": "Check the module",
                    "depends_on": ["step_1"],
                    "assigned_claw": "bash",
                    "inputs": {
                        "command": "./.venv/bin/python -c \"import pkg.mod\"",
                        "clauses": {"verifies": ["C01-alpha", "C02-beta"]},
                    },
                    "outputs": [],
                    "acceptance": ["The module imports."],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                    "error": None,
                },
            ]
            nodes, _metadata = parse_planner_response(
                json.dumps({"nodes": raw_nodes}),
                goal_text=GOAL, workspace=str(root), repo_root=root,
                clauses=CLAUSES,
            )
            self.assertEqual(len(nodes), 2)
            # Drop the verification declarations: the same plan is
            # now a clause_coverage rejection.
            raw_nodes[1]["inputs"].pop("clauses")
            with self.assertRaises(LLMResponseError) as caught:
                parse_planner_response(
                    json.dumps({"nodes": raw_nodes}),
                    goal_text=GOAL, workspace=str(root), repo_root=root,
                    clauses=CLAUSES,
                )
            codes = [r.code for r in caught.exception.plan_errors]
            self.assertEqual(codes, ["clause_coverage", "clause_coverage"])

    def test_parse_without_clauses_keeps_old_behavior(self):
        # No clauses argument: the rule does not run at all (the
        # corpus-replay contract).
        records, _cheap = planner_module._collect_plan_error_records(
            [_node("node-1")], None, GOAL, workspace=None,
        )
        self.assertNotIn(
            "clause_coverage", [r.code for r in records]
        )


# ── Probes ──────────────────────────────────────────────────────────

HANDLER_GET_POST_ONLY = '''"""Server fixture."""

from http.server import BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        pass

    def do_POST(self):
        pass
'''

HANDLER_WITH_PUT = HANDLER_GET_POST_ONLY.replace(
    "    def do_POST(self):\n        pass\n",
    "    def do_POST(self):\n        pass\n\n    def do_PUT(self):\n        pass\n",
)

HANDLER_WITH_GETATTR = HANDLER_GET_POST_ONLY.replace(
    "    def do_POST(self):\n        pass\n",
    "    def do_POST(self):\n        pass\n\n    def __getattr__(self, name):\n        raise AttributeError(name)\n",
)


class TestProbes(unittest.TestCase):
    def _clause(self, **overrides) -> dict:
        clause = {
            "id": "C09-http-api", "spec": "pkg/SPEC.md", "heading": "HTTP API",
            "text": "## HTTP API\n", "declared_files": ["pkg/__main__.py"],
            "method_surface": True,
        }
        clause.update(overrides)
        return clause

    def test_declared_files_missing_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = run_contract_probes([self._clause()], tmp)
            by_probe = {r["probe"]: r for r in results}
            self.assertFalse(by_probe["declared_files"]["ok"])
            self.assertIn("pkg/__main__.py", by_probe["declared_files"]["detail"])

    def test_declared_files_present_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "pkg" / "__main__.py", "")
            _write(Path(tmp) / "pkg" / "server.py", HANDLER_WITH_PUT)
            results = run_contract_probes([self._clause()], tmp)
            self.assertTrue(all(r["ok"] for r in results))

    def test_method_surface_get_post_only_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "pkg" / "server.py", HANDLER_GET_POST_ONLY)
            results = run_contract_probes(
                [self._clause(declared_files=[])], tmp
            )
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0]["ok"])
            self.assertIn("501", results[0]["detail"])

    def test_method_surface_with_extra_handler_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "pkg" / "server.py", HANDLER_WITH_PUT)
            results = run_contract_probes(
                [self._clause(declared_files=[])], tmp
            )
            self.assertTrue(results[0]["ok"])

    def test_method_surface_with_getattr_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "pkg" / "server.py", HANDLER_WITH_GETATTR)
            results = run_contract_probes(
                [self._clause(declared_files=[])], tmp
            )
            self.assertTrue(results[0]["ok"])

    def test_method_surface_no_handler_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "pkg" / "other.py", "VALUE = 1\n")
            results = run_contract_probes(
                [self._clause(declared_files=[])], tmp
            )
            self.assertFalse(results[0]["ok"])


# ── Clause states + brief ───────────────────────────────────────────


def _executed_node(node_id, status, clauses_inputs=None, verdict=None):
    node = _node(node_id, clauses_inputs)
    node.status = status
    if verdict is not None:
        node.measurement = {"verdict": verdict}
    return node


class TestClauseStates(unittest.TestCase):
    def test_verified_failed_unverified_waived(self):
        nodes = [
            _executed_node("node-1", PlanStatus.COMPLETED,
                           {"implements": ["C01-alpha", "C02-beta"],
                            "verifies": ["C01-alpha"]}, verdict="PASS"),
            _executed_node("node-2", PlanStatus.FAILED,
                           {"verifies": ["C02-beta"]}, verdict="FAIL"),
        ]
        states = clause_states(CLAUSES, nodes, [])
        self.assertEqual(states["C01-alpha"]["state"], "verified")
        self.assertEqual(states["C02-beta"]["state"], "failed")
        waived = clause_states(CLAUSES, nodes, [], {"C02-beta": "operator call"})
        self.assertEqual(waived["C02-beta"]["state"], "waived")
        fresh = clause_states(CLAUSES, [_node("node-1")], [])
        self.assertEqual(fresh["C01-alpha"]["state"], "unverified")
        self.assertIn(
            "no implementation node", fresh["C01-alpha"]["reasons"]
        )

    def test_probe_failure_marks_failed(self):
        nodes = [
            _executed_node("node-1", PlanStatus.COMPLETED,
                           {"implements": ["C01-alpha"], "verifies": ["C01-alpha"]},
                           verdict="PASS"),
        ]
        probes = [{"clause_id": "C01-alpha", "probe": "declared_files",
                   "ok": False, "detail": "declared files missing: pkg/__main__.py"}]
        states = clause_states(CLAUSES[:1], nodes, probes)
        self.assertEqual(states["C01-alpha"]["state"], "failed")
        self.assertIn("probe declared_files failing", states["C01-alpha"]["reasons"][0])


class TestBriefUncovered(unittest.TestCase):
    def test_brief_carries_uncovered_clauses_until_verified(self):
        judge = Judge(RunBudget(max_replans=3))
        contract = GoalContract(objective=GOAL)
        nodes = [
            _executed_node("node-1", PlanStatus.COMPLETED,
                           {"implements": ["C01-alpha"], "verifies": ["C01-alpha"]},
                           verdict="PASS"),
            _executed_node("node-2", PlanStatus.FAILED,
                           {"implements": ["C02-beta"]}, verdict="FAIL"),
        ]
        plan = ExecutionPlan(
            plan_id="plan-clauses",
            intent=TaskIntent(goal=GOAL),
            nodes=nodes,
            metadata={"contract_clauses": CLAUSES},
        )
        brief = judge.build_replan_brief(
            contract, plan, [], [], plan_version=1
        )
        uncovered = brief["uncovered_clauses"]
        self.assertEqual([entry["id"] for entry in uncovered], ["C02-beta"])
        self.assertIn("no verification node", uncovered[0]["reasons"])
        rendered = planner_module._format_brief_for_prompt(brief)
        self.assertIn("Uncovered contract clauses", rendered)
        self.assertIn("C02-beta", rendered)


# ── Verdict gating ──────────────────────────────────────────────────


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _runner(tmp: str) -> LoopRunner:
    store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    return LoopRunner(
        store,
        registry,
        run_store=RunStore(Path(tmp) / "runs"),
        memory_store=ExperimentStore(Path(tmp) / "memory.db"),
        audit_logger=_AuditCapture(),
    )


def _verdict_plan(nodes, metadata) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-verdict",
        intent=TaskIntent(goal=GOAL),
        nodes=nodes,
        metadata=metadata,
    )


class TestVerdictGating(unittest.TestCase):
    def _evaluate(self, runner, plan, escalation=None):
        contract = GoalContract(objective=GOAL)
        return runner._evaluate_goal(contract, plan, [], 1.0, escalation)

    def test_pass_with_unverified_clause_is_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            plan = _verdict_plan(
                [_executed_node("node-1", PlanStatus.COMPLETED,
                                {"implements": ["C01-alpha"]}, verdict="PASS")],
                {"contract_clauses": CLAUSES[:1]},
            )
            verdict, detail = self._evaluate(runner, plan)
            self.assertEqual(verdict, "PARTIAL")
            self.assertIn("C01-alpha", detail)

    def test_pass_with_all_clauses_verified_stays_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            plan = _verdict_plan(
                [_executed_node("node-1", PlanStatus.COMPLETED,
                                {"implements": ["C01-alpha"],
                                 "verifies": ["C01-alpha"]}, verdict="PASS")],
                {"contract_clauses": CLAUSES[:1]},
            )
            verdict, _detail = self._evaluate(runner, plan)
            self.assertEqual(verdict, "PASS")

    def test_pass_with_failed_clause_and_none_verified_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            plan = _verdict_plan(
                [_executed_node("node-1", PlanStatus.COMPLETED,
                                {"implements": ["C01-alpha"],
                                 "verifies": ["C01-alpha"]}, verdict="PASS")],
                {
                    "contract_clauses": CLAUSES[:1],
                    "contract_probe_results": [
                        {"clause_id": "C01-alpha", "probe": "declared_files",
                         "ok": False, "detail": "missing"}
                    ],
                },
            )
            verdict, _detail = self._evaluate(runner, plan)
            self.assertEqual(verdict, "FAIL")

    def test_escalation_before_verification_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            plan = _verdict_plan(
                [_node("node-1", {"implements": ["C01-alpha"],
                                  "verifies": ["C01-alpha"]})],
                {"contract_clauses": CLAUSES[:1]},
            )
            verdict, detail = self._evaluate(
                runner, plan, escalation="re-plan budget exhausted"
            )
            self.assertEqual(verdict, "INCONCLUSIVE")
            self.assertIn("C01-alpha", detail)

    def test_waived_clause_allows_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            plan = _verdict_plan(
                [_node("node-1")],
                {
                    "contract_clauses": CLAUSES[:1],
                    "contract_clause_waivers": {"C01-alpha": "operator decision"},
                },
            )
            verdict, _detail = self._evaluate(runner, plan)
            self.assertEqual(verdict, "PASS")

    def test_plan_without_clauses_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            plan = _verdict_plan(
                [_executed_node("node-1", PlanStatus.COMPLETED, verdict="PASS")],
                {},
            )
            verdict, detail = self._evaluate(runner, plan)
            self.assertEqual(verdict, "PASS")
            self.assertNotIn("contract clauses", detail)


# ── The probe loop, end to end ──────────────────────────────────────


class TestProbeLoopIntegration(unittest.TestCase):
    def test_probe_failure_replans_then_passes_when_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = _AuditCapture()
            store = ExecutionPlanStateStore(db_path=root / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            runner = LoopRunner(
                store,
                registry,
                run_store=RunStore(root / "runs"),
                memory_store=ExperimentStore(root / "memory.db"),
                audit_logger=audit,
            )
            clause = {
                "id": "C10-entry", "spec": "pkg/SPEC.md",
                "heading": "Entry point", "text": "## Entry point\n",
                "declared_files": ["pkg/__main__.py"], "method_surface": False,
            }
            plan = ExecutionPlan(
                plan_id="plan-probes",
                intent=TaskIntent(goal=GOAL),
                nodes=[
                    PlanNode(
                        id="node-1",
                        objective="noop",
                        assigned_claw="bash",
                        inputs={"command": "true"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="nothing to do yet",
                    )
                ],
                metadata={
                    "repo_root": str(root),
                    "contract_clauses": [clause],
                },
            )
            store.save(plan)
            contract = GoalContract(
                objective=GOAL, budget=RunBudget(max_replans=2)
            )

            def fake_replan(brief):
                return [
                    PlanNode(
                        id="node-1",
                        objective="create the entry point",
                        assigned_claw="bash",
                        inputs={
                            "command": (
                                f"mkdir -p {root}/pkg && "
                                f"touch {root}/pkg/__main__.py"
                            )
                        },
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the declared file must exist",
                    )
                ]

            with (
                mock.patch.object(
                    loop_module, "try_local_repair",
                    lambda plan_, brief, settings=None, declines=None: None,
                ),
                mock.patch.object(
                    loop_module, "try_scoped_edit_repair",
                    lambda plan_, brief, settings=None, declines=None: None,
                ),
                mock.patch.object(loop_module, "replan_from_brief", fake_replan),
            ):
                result = runner.run_plan(plan, contract)
            self.assertEqual(result.goal_verdict, "PASS")
            reasons = [
                entry["reason"] for entry in result.decision_history
                if entry["decision"] == "replan"
            ]
            self.assertTrue(
                any("contract probes failing" in r for r in reasons), reasons
            )
            events = [event for event, _fields in audit.events]
            self.assertIn("loop.contract_probes", events)
            saved = store.load("plan-probes")
            self.assertIsNotNone(saved)
            results = saved.metadata.get("contract_probe_results")
            self.assertTrue(results and all(r["ok"] for r in results))


if __name__ == "__main__":
    unittest.main()
