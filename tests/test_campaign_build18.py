"""Campaign build #18 (2026-11-24): repair persistence.

Arm #27's loop received build #16's exact probe facts and acted
on them: a v2 seam edit added path-first do_PUT routing and the
wrong-method probe flows went green — then v4's rewrite dropped
the handler, the flows went red again, and the run shipped v4's
tree (501 where the contract pins 404/405) because the loop had
no notion of shipping anything but its last version, and no
brief ever said "you had this green and lost it".

Two changes, both deterministic, no new model calls:

- Best-version shipping (loop): every executed version's outcome
  facts (contract probe flows green, failed nodes, failed
  measured criteria — all from the run's own records) and a
  snapshot of its declared write targets are kept; at a terminal
  finish with >= 2 versions the versions are ranked (probes,
  then failed nodes, then failed criteria, then recency) and the
  best version's tree is restored when the final version is
  measurably worse. The summary and the run record say which
  version shipped and what the final version lost. Single-
  version runs and gate pauses are untouched, byte for byte.

- Regression facts (judge + brief): the judge accumulates a
  per-version check history (probe records and measured
  criterion results, keyed mechanically) across its briefs; a
  brief for a version where a check is red that an earlier
  version had green carries regression_facts, rendered into the
  planner prompt through the anchoring section. A brief with no
  regressions is exactly the brief of before.

No GPU is touched and no LLM is called: loop tests use injected
replan functions and the pairing fixture tree.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import run_contract_probes
from freetalon.orchestrator.judge import (
    Judge,
    check_states,
    regressed_checks,
)
from freetalon.orchestrator.loop import LoopRunner, _best_version_outcome
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

# ── Fixture pairing packages ─────────────────────────────────────
#
# The build #6 fixture family: a minimal freetalon/pairing tree
# whose server implements the contract's flows. The bad variant
# drops the deliberate method surface (do_PUT / do_DELETE), so
# wrong-method flows fall through to the stdlib 501 — arm #27's
# shipped defect, in miniature.

FIXTURE_STORE = '''"""Fixture store: the bootstrap constructs it; nothing more."""


class PairingStore:
    def __init__(self, path):
        self.path = str(path)
'''

FIXTURE_SERVER_GOOD = '''"""Fixture pairing server: the contract's flows, minimally."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    records = {}

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            return {}

    def do_POST(self):
        if self.path == "/pairing/register":
            body = self._body()
            code = body.get("code", "")
            existing = self.records.get(code)
            if existing is not None and existing.get("nonce") == body.get(
                "nonce"
            ):
                self._json(200, {"code": code, "status": existing["status"]})
                return
            self.records[code] = {
                "status": "pending",
                "credentials": None,
                "nonce": body.get("nonce"),
            }
            self._json(201, {"code": code, "status": "pending"})
            return
        if self.path.startswith("/pairing/") and self.path.endswith("/confirm"):
            code = self.path.split("/")[2]
            record = self.records.get(code)
            if record is None:
                self._json(404, {"error": "not found"})
                return
            bundle = self._body().get("credentials")
            if not bundle:
                self._json(400, {"error": "invalid bundle"})
                return
            record["credentials"] = bundle
            record["status"] = "confirmed"
            self._json(200, {"code": code, "status": "confirmed"})
            return
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/pairing/") and self.path != "/pairing":
            code = self.path.split("/")[2]
            record = self.records.get(code)
            if record is None or record["status"] == "pending":
                self._json(404, {"code": code, "status": "pending"})
                return
            if record["status"] == "confirmed":
                bundle = record["credentials"]
                record["status"] = "claimed"
                record["credentials"] = None
                self._json(
                    200,
                    {
                        "code": code,
                        "status": "confirmed",
                        "credentials": bundle,
                    },
                )
                return
            self._json(410, {"code": code, "status": record["status"]})
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})


def make_server(store, token, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), _Handler)
'''

# No deliberate method surface: PUT/DELETE fall through to the
# stdlib 501 default the contract forbids.
FIXTURE_SERVER_NO_SURFACE = FIXTURE_SERVER_GOOD.replace(
    '''    def do_PUT(self):
        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

''',
    "",
)

SMOKE_CLAUSES = [
    {
        "id": "C09-http-api-server-py",
        "spec": "freetalon/pairing/CONTRACT.md",
        "heading": "HTTP API (server.py)",
        "text": "## HTTP API (server.py)\n",
        "declared_files": ["freetalon/pairing/server.py"],
        "method_surface": True,
    },
    {
        "id": "C10-server-construction-server-py",
        "spec": "freetalon/pairing/CONTRACT.md",
        "heading": "Server construction (server.py)",
        "text": "## Server construction (server.py)\n",
        "declared_files": [
            "freetalon/pairing/server.py",
            "freetalon/pairing/__main__.py",
        ],
        "method_surface": False,
    },
]

SERVER_TARGET = "freetalon/pairing/server.py"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture_tree(root: Path) -> Path:
    """The pairing package minus server.py — the plan writes that."""
    package = root / "freetalon" / "pairing"
    _write(root / "freetalon" / "__init__.py", "")
    _write(package / "__init__.py", "")
    _write(package / "__main__.py", "")
    _write(package / "store.py", FIXTURE_STORE)
    return root


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _loop_runner(tmp: str, audit: _AuditCapture) -> LoopRunner:
    store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    return LoopRunner(
        store,
        registry,
        run_store=RunStore(Path(tmp) / "runs"),
        memory_store=ExperimentStore(Path(tmp) / "memory.db"),
        audit_logger=audit,
    )


def _write_node(content: str) -> PlanNode:
    return PlanNode(
        id="node-w",
        objective="Write the pairing server module",
        assigned_claw="scaffold",
        inputs={"file_path": SERVER_TARGET, "content": content},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the written server satisfies the contract",
    )


def _bash_node(command: str) -> PlanNode:
    return PlanNode(
        id="node-t",
        objective="Run the marker check",
        assigned_claw="bash",
        inputs={"command": command},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the marker check passes",
    )


def _pairing_plan(nodes: list[PlanNode], tree: Path) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-build18",
        intent=TaskIntent(goal="pairing goal"),
        nodes=nodes,
        metadata={
            "contract_clauses": SMOKE_CLAUSES,
            "repo_root": str(tree),
        },
    )


# ── Part A machinery: check states + ranking (unit level) ────────


class TestCheckStateMachinery(unittest.TestCase):
    def test_check_states_from_records(self):
        measurements = [
            {
                "node_id": "node-1",
                "results": [
                    {
                        "criterion": {"type": "exit_code", "equals": 0},
                        "passed": True,
                        "measured": True,
                        "detail": "exit_code == 0",
                    },
                    {
                        "criterion": {"type": "critic_review"},
                        "passed": False,
                        "measured": False,
                        "detail": "not measured",
                    },
                ],
            }
        ]
        probes = [
            {
                "clause_id": "C09-x",
                "probe": "http_smoke",
                "flow": "register",
                "ok": False,
                "detail": "register -> 400",
            },
            {
                "clause_id": "C09-x",
                "probe": "declared_files",
                "ok": True,
                "detail": "present",
            },
        ]
        states = check_states(measurements, probes)
        # The unmeasured criterion is no evidence and is absent.
        self.assertEqual(len(states), 3)
        node_key = 'node:node-1:{"equals": 0, "type": "exit_code"}'
        self.assertTrue(states[node_key]["ok"])
        flow_key = "probe:C09-x:http_smoke:register"
        self.assertFalse(states[flow_key]["ok"])
        self.assertIn("flow register", states[flow_key]["label"])
        self.assertTrue(states["probe:C09-x:declared_files"]["ok"])

    def test_regressed_checks_names_the_most_recent_green(self):
        history = {
            "probe:C09:http_smoke:wrong_method": {1: True, 2: True, 3: False},
            "probe:C09:http_smoke:register": {1: False, 3: False},
            "probe:C09:declared_files": {1: True, 3: True},
        }
        labels = {key: f"label {key}" for key in history}
        facts = regressed_checks(history, labels, {}, 3)
        # Only the first check regressed: the second was never
        # green, the third is still green.
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["green_in_version"], 2)
        self.assertEqual(facts[0]["current_version"], 3)

    def test_best_version_ranking_order(self):
        outcomes = [
            {"version": 1, "probe_green": 14, "probe_total": 14,
             "failed_nodes": 1, "failed_criteria": 1},
            {"version": 2, "probe_green": 11, "probe_total": 14,
             "failed_nodes": 1, "failed_criteria": 1},
            {"version": 3, "probe_green": 11, "probe_total": 14,
             "failed_nodes": 0, "failed_criteria": 0},
        ]
        # Probe greens dominate: v1 wins over the cleaner v3.
        self.assertEqual(_best_version_outcome(outcomes)["version"], 1)
        tied = [
            {"version": 1, "probe_green": 0, "probe_total": 0,
             "failed_nodes": 1, "failed_criteria": 2},
            {"version": 2, "probe_green": 0, "probe_total": 0,
             "failed_nodes": 1, "failed_criteria": 2},
        ]
        # An exact tie ships the later version.
        self.assertEqual(_best_version_outcome(tied)["version"], 2)
        self.assertIsNone(_best_version_outcome([]))


class TestJudgeRegressionFacts(unittest.TestCase):
    def _brief(self, judge, probe_results, version):
        plan = ExecutionPlan(
            plan_id="plan-build18-judge",
            intent=TaskIntent(goal="pairing goal"),
            nodes=[],
            metadata={"contract_probe_results": probe_results},
        )
        contract = GoalContract(
            objective="pairing goal", budget=RunBudget(max_replans=3)
        )
        return judge.build_replan_brief(contract, plan, [], [], version)

    def test_second_brief_carries_the_regression(self):
        judge = Judge(RunBudget(max_replans=3))
        green = [
            {"clause_id": "C09-x", "probe": "http_smoke",
             "flow": "wrong_method", "ok": True, "detail": "-> 404"},
            {"clause_id": "C09-x", "probe": "http_smoke",
             "flow": "register", "ok": True, "detail": "-> 201"},
        ]
        first = self._brief(judge, green, 1)
        self.assertNotIn("regression_facts", first)
        red = [
            {"clause_id": "C09-x", "probe": "http_smoke",
             "flow": "wrong_method", "ok": False, "detail": "-> 501"},
            {"clause_id": "C09-x", "probe": "http_smoke",
             "flow": "register", "ok": True, "detail": "-> 201"},
        ]
        second = self._brief(judge, red, 2)
        facts = second.get("regression_facts") or []
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["green_in_version"], 1)
        self.assertEqual(facts[0]["current_version"], 2)
        self.assertIn("wrong_method", facts[0]["label"])
        self.assertIn("501", facts[0]["detail"])
        prompt = planner_module._format_brief_for_prompt(second)
        self.assertIn("Regressed checks (system)", prompt)
        self.assertIn("green in plan version 1", prompt)
        # And a brief without the field renders without the block.
        plain = planner_module._format_brief_for_prompt(
            {"goal": {"objective": "g"}, "plan_id": "p", "plan_version": 1}
        )
        self.assertNotIn("Regressed checks", plain)


# ── Part B: best-version shipping through the loop ───────────────


class TestBestVersionShipping(unittest.TestCase):
    def test_regressed_final_version_ships_the_earlier_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = _fixture_tree(Path(tmp) / "tree")
            plan = _pairing_plan(
                [_write_node(FIXTURE_SERVER_GOOD), _bash_node("exit 1")],
                tree,
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=1)
            )

            def replan_fn(brief):
                # v2 rewrites the server without the method
                # surface — the arm #27 regression, in miniature.
                return [
                    _write_node(FIXTURE_SERVER_NO_SURFACE),
                    _bash_node("exit 2"),
                ]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.plan_versions, 2)
            # Verdict machinery untouched: an informative measured
            # failure with the budget spent is PARTIAL, as today.
            self.assertEqual(result.goal_verdict, "PARTIAL")
            # The shipped tree is v1's, not the final version's.
            shipped = (tree / SERVER_TARGET).read_text(encoding="utf-8")
            self.assertEqual(shipped, FIXTURE_SERVER_GOOD)
            self.assertEqual(result.shipped_version, 1)
            assert result.shipping is not None
            self.assertTrue(result.shipping["restored"])
            self.assertEqual(result.shipping["final_version"], 2)
            lost_labels = [
                fact["label"] for fact in result.shipping["lost_checks"]
            ]
            self.assertTrue(
                any("wrong_method" in label for label in lost_labels),
                f"lost checks: {lost_labels}",
            )
            self.assertTrue(
                any("method_surface" in label for label in lost_labels),
                f"lost checks: {lost_labels}",
            )
            # The record: summary section + persisted trees/outcomes.
            run_dir = runner._run_store.run_dir(result.run_id)
            summary = (run_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("## Shipped version", summary)
            self.assertIn("Shipped: v1 (final version: v2)", summary)
            self.assertIn("restored from its snapshot", summary)
            self.assertTrue((run_dir / "trees" / "v001.json").exists())
            self.assertTrue((run_dir / "trees" / "v002.json").exists())
            outcomes = (run_dir / "version_outcomes.json").read_text(
                encoding="utf-8"
            )
            self.assertIn('"shipped_version": 1', outcomes)
            events = [event for event, _fields in audit.events]
            self.assertIn("loop.shipping.decision", events)

    def test_tie_on_all_rank_facts_ships_the_later_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = Path(tmp) / "tree"
            tree.mkdir()
            plan = ExecutionPlan(
                plan_id="plan-build18-tie",
                intent=TaskIntent(goal="tie goal"),
                nodes=[
                    PlanNode(
                        id="node-w",
                        objective="Write the output file",
                        assigned_claw="scaffold",
                        inputs={"file_path": "out.txt", "content": "A\n"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the file is written",
                    ),
                    _bash_node("exit 1"),
                ],
                metadata={"repo_root": str(tree)},
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="tie goal", budget=RunBudget(max_replans=1)
            )

            def replan_fn(brief):
                return [
                    PlanNode(
                        id="node-w",
                        objective="Write the output file",
                        assigned_claw="scaffold",
                        inputs={"file_path": "out.txt", "content": "B\n"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the file is written",
                    ),
                    _bash_node("exit 2"),
                ]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.plan_versions, 2)
            # Identical rank facts: recency wins, nothing restored.
            self.assertEqual(result.shipped_version, 2)
            assert result.shipping is not None
            self.assertFalse(result.shipping["restored"])
            self.assertEqual(result.shipping["lost_checks"], [])
            self.assertEqual(
                (tree / "out.txt").read_text(encoding="utf-8"), "B\n"
            )
            summary = (
                runner._run_store.run_dir(result.run_id) / "summary.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Shipped: v2 (final version: v2)", summary)


# ── Part C: regression facts reach the brief through the loop ────


class TestRegressionFactsThroughTheLoop(unittest.TestCase):
    def test_brief_for_v3_names_what_v2_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = _fixture_tree(Path(tmp) / "tree")
            plan = _pairing_plan(
                [_write_node(FIXTURE_SERVER_GOOD), _bash_node("exit 1")],
                tree,
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            briefs: list[dict] = []

            def replan_fn(brief):
                briefs.append(brief)
                if brief["plan_version"] == 1:
                    return [
                        _write_node(FIXTURE_SERVER_NO_SURFACE),
                        _bash_node("exit 2"),
                    ]
                return [
                    _write_node(FIXTURE_SERVER_GOOD),
                    _bash_node("true"),
                ]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(len(briefs), 2)
            # The first brief has no earlier version to regress from.
            self.assertNotIn("regression_facts", briefs[0])
            facts = briefs[1].get("regression_facts") or []
            self.assertTrue(facts, f"second brief: {sorted(briefs[1])}")
            wrong_method = [
                fact for fact in facts if "wrong_method" in fact["label"]
            ]
            self.assertTrue(wrong_method, f"facts: {facts}")
            self.assertEqual(wrong_method[0]["green_in_version"], 1)
            self.assertEqual(wrong_method[0]["current_version"], 2)
            prompt = planner_module._format_brief_for_prompt(briefs[1])
            self.assertIn("Regressed checks (system)", prompt)
            self.assertIn("wrong_method", prompt)
            # v3 restored the surface and passed: it ships itself.
            self.assertEqual(result.shipped_version, 3)
            shipped = (tree / SERVER_TARGET).read_text(encoding="utf-8")
            self.assertEqual(shipped, FIXTURE_SERVER_GOOD)


# ── Guardrail: single-version runs are exactly as before ─────────


class TestSingleVersionUnchanged(unittest.TestCase):
    def test_single_version_run_has_no_shipping_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = _fixture_tree(Path(tmp) / "tree")
            plan = _pairing_plan(
                [_write_node(FIXTURE_SERVER_GOOD), _bash_node("true")],
                tree,
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            result = runner.run_plan(plan, contract)
            self.assertEqual(result.plan_versions, 1)
            self.assertIsNone(result.shipped_version)
            self.assertIsNone(result.shipping)
            run_dir = runner._run_store.run_dir(result.run_id)
            summary = (run_dir / "summary.md").read_text(encoding="utf-8")
            self.assertNotIn("Shipped version", summary)
            self.assertFalse((run_dir / "trees").exists())
            self.assertFalse((run_dir / "version_outcomes.json").exists())
            # The tree is exactly what the one version produced.
            shipped = (tree / SERVER_TARGET).read_text(encoding="utf-8")
            self.assertEqual(shipped, FIXTURE_SERVER_GOOD)
            # Sanity: the probes really ran against this tree.
            results = run_contract_probes(SMOKE_CLAUSES, tree)
            self.assertTrue(all(record["ok"] for record in results))


if __name__ == "__main__":
    unittest.main()
