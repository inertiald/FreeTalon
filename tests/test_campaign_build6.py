"""Campaign build #6 (2026-11-24): two changes from exam arm #15.

Arm #15 was the first arm to execute on the production path: the
clause probes fired and caught both structural defects (C09's
501 method surface, C10's missing __main__.py) — and the run
still ended PARTIAL at 26/35 with all 3 re-plans unspent. Two
walls produced that ending:

1. The shipped server hung on POST flows (valid register POSTs
   answered 400; confirm/poll paths hung), so the plan's own
   test node timed out twice and the judge escalated on
   retries-exhausted. Part 1 — escalation-to-re-plan conversion:
   a timeout/retries-exhausted escalation with re-plan budget
   remaining converts into a re-plan whose brief carries the
   clause-probe failures verbatim and the per-node timeout
   diagnostics. Non-convertible classes (missing tool, wall
   clock, unmeasurable, stalled, plan state, budget spent) still
   escalate.

2. The static probes cannot see the hang/400 class at all.
   Part 2 — a live HTTP smoke probe in the clause layer: the
   pairing contract's runner boots the shipped server via its
   own make_server surface in a subprocess (ephemeral port,
   throwaway store, hard 20s overall deadline, unconditional
   process-group kill) and exercises the contract's pinned
   flows — register, pending poll, confirm, claiming poll,
   wrong method — recording per-flow outcomes in the standard
   probe record shape, so failures force re-plans through the
   same channel as static probe failures.

No GPU is touched and no LLM is called: loop tests use a
registered hang claw and injected replan functions.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    extract_clauses,
    probe_failures,
    run_contract_probes,
)
from freetalon.orchestrator.judge import Decision, Judge
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
from freetalon.orchestrator.repo_index import SpecDocument
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

# ── Fixture pairing packages ─────────────────────────────────────
#
# A minimal freetalon/pairing tree whose server implements the
# contract's flows. The bad variants are one-edit derivations, so
# each fixture fails exactly the flow it names.

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
            self.records[code] = {"status": "pending", "credentials": None}
            self._json(201, {"code": code, "status": "pending"})
            return
        if self.path.startswith("/pairing/") and self.path.endswith("/confirm"):
            code = self.path.split("/")[2]
            record = self.records.get(code)
            if record is None:
                self._json(404, {"error": "not found"})
                return
            record["credentials"] = self._body().get("credentials")
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
        self._json(405, {"error": "method not allowed"})

    def do_DELETE(self):
        self._json(405, {"error": "method not allowed"})


def make_server(store, token, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), _Handler)
'''

# Register answers every valid registration with 400 (arm #15's
# register defect), everything else as the good fixture.
FIXTURE_SERVER_400 = FIXTURE_SERVER_GOOD.replace(
    'self._json(201, {"code": code, "status": "pending"})',
    'self._json(400, {"error": "bad request"})',
)

# No deliberate method surface: PUT falls through to the stdlib
# 501 default the contract forbids.
FIXTURE_SERVER_501 = FIXTURE_SERVER_GOOD.replace(
    '''    def do_PUT(self):
        self._json(405, {"error": "method not allowed"})

    def do_DELETE(self):
        self._json(405, {"error": "method not allowed"})

''',
    "",
)

# Every POST hangs (arm #15's confirm/poll defect). make_server
# leaves a pidfile so the test can prove the probe's kill left no
# orphan behind.
FIXTURE_SERVER_HANG = (
    FIXTURE_SERVER_GOOD.replace(
        '"""Fixture pairing server: the contract\'s flows, minimally."""\n\nimport json\n',
        '"""Fixture pairing server: every POST hangs."""\n\nimport json\nimport time\n',
    )
    .replace(
        "    def do_POST(self):\n",
        "    def do_POST(self):\n        time.sleep(30)\n",
    )
    .replace(
        '''def make_server(store, token, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), _Handler)
''',
        '''def make_server(store, token, host="127.0.0.1", port=0):
    from pathlib import Path

    Path(__file__).with_name("server.pid").write_text(str(os.getpid()))
    return ThreadingHTTPServer((host, port), _Handler)
''',
    )
    .replace("import json\nimport time\n", "import json\nimport os\nimport time\n")
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture_tree(root: Path, server_text: str) -> Path:
    package = root / "freetalon" / "pairing"
    _write(root / "freetalon" / "__init__.py", "")
    _write(package / "__init__.py", "")
    _write(package / "__main__.py", "")
    _write(package / "store.py", FIXTURE_STORE)
    _write(package / "server.py", server_text)
    return root


def _smoke_records(results, clause_id="C09-http-api-server-py"):
    return [
        r
        for r in results
        if r["probe"] == "http_smoke" and r["clause_id"] == clause_id
    ]


# ── Part 2: the live smoke probe ─────────────────────────────────


class TestPairingSmokeProbe(unittest.TestCase):
    def test_good_server_passes_every_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_GOOD)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            self.assertEqual(probe_failures(results), [])
            flows = _smoke_records(results)
            self.assertEqual(len(flows), 5)
            boot = [
                r
                for r in results
                if r["probe"] == "http_smoke_boot"
                and r["clause_id"] == "C10-server-construction-server-py"
            ]
            self.assertEqual(len(boot), 1)
            self.assertTrue(boot[0]["ok"])

    def test_real_repo_server_passes(self):
        # The production pairing implementation is the known-good
        # fixture: the probe must pass the tree it will judge in
        # the exam, or it is measuring itself wrong.
        import hashlib

        root = Path(__file__).resolve().parent.parent
        text = (root / "freetalon" / "pairing" / "CONTRACT.md").read_text(
            encoding="utf-8"
        )
        spec = SpecDocument(
            path="freetalon/pairing/CONTRACT.md",
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            byte_count=len(text.encode()),
            text=text,
        )
        clauses = extract_clauses(spec)
        results = run_contract_probes(clauses, root)
        self.assertEqual(probe_failures(results), [])
        self.assertTrue(any(r["probe"] == "http_smoke" for r in results))
        self.assertTrue(any(r["probe"] == "http_smoke_boot" for r in results))

    def test_register_400_fails_the_register_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_400)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            register = [
                r
                for r in _smoke_records(results)
                if r["detail"].startswith("register:")
            ]
            self.assertEqual(len(register), 1)
            self.assertFalse(register[0]["ok"])
            self.assertIn("400", register[0]["detail"])
            self.assertIn(register[0], probe_failures(results))

    def test_wrong_method_501_fails_only_that_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_501)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            flows = _smoke_records(results)
            wrong = [r for r in flows if r["detail"].startswith("wrong_method:")]
            self.assertEqual(len(wrong), 1)
            self.assertFalse(wrong[0]["ok"])
            self.assertIn("501", wrong[0]["detail"])
            register = [r for r in flows if r["detail"].startswith("register:")]
            self.assertTrue(register[0]["ok"])

    def test_hang_fails_within_the_deadline_and_leaves_no_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_HANG)
            started = time.monotonic()
            results = run_contract_probes(SMOKE_CLAUSES, root)
            elapsed = time.monotonic() - started
            # The probe's hard bound: it reported failures, it did
            # not stall the caller.
            self.assertLess(elapsed, 20.0)
            flows = _smoke_records(results)
            register = [r for r in flows if r["detail"].startswith("register:")]
            confirm = [r for r in flows if r["detail"].startswith("confirm:")]
            self.assertFalse(register[0]["ok"])
            self.assertIn("timed out", register[0]["detail"])
            self.assertFalse(confirm[0]["ok"])
            self.assertIn("timed out", confirm[0]["detail"])
            # The booted server is gone: the pid its make_server
            # recorded no longer exists.
            pid = int(
                (root / "freetalon" / "pairing" / "server.pid").read_text(
                    encoding="utf-8"
                )
            )
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail(f"probe left orphan server process {pid} running")

    def test_no_records_when_the_server_module_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = run_contract_probes(SMOKE_CLAUSES, tmp)
            self.assertFalse(any(r["probe"].startswith("http_smoke") for r in results))
            # The absence is the declared_files probe's failure to
            # report — the live probe does not double-report it.
            self.assertTrue(probe_failures(results))


# ── Part 1: judge failure classes ────────────────────────────────


def _judge_node(**overrides) -> PlanNode:
    node = PlanNode(
        id="node-1",
        objective="a step",
        assigned_claw="bash",
        inputs={"command": "run the thing"},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="it works",
    )
    for key, value in overrides.items():
        setattr(node, key, value)
    return node


def _judge_plan(nodes, status=PlanStatus.FAILED) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-judge",
        intent=TaskIntent(goal="judge goal"),
        nodes=nodes,
        status=status,
    )


def _measurement(verdict="FAIL", envelope_status="completed"):
    return {
        "node_id": "node-1",
        "verdict": verdict,
        "results": [],
        "envelope_status": envelope_status,
        "duration_s": 1.0,
    }


class TestJudgeFailureClasses(unittest.TestCase):
    def test_timeout_exhausted_carries_the_convertible_class(self):
        judge = Judge(RunBudget())
        node = _judge_node(
            status=PlanStatus.FAILED,
            error="Node timed out after 120.0s.",
            measurement=_measurement(envelope_status="timeout"),
            max_retries=0,
        )
        judge.record_pass([node])
        decision = judge.decide(_judge_plan([node]), [_measurement("FAIL", "timeout")])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertEqual(decision.failure_class, "timeout_retries_exhausted")

    def test_missing_tool_carries_a_non_convertible_class(self):
        judge = Judge(RunBudget())
        node = _judge_node(status=PlanStatus.NEEDS_TOOL, assigned_claw="ghost")
        decision = judge.decide(_judge_plan([node]), [])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertEqual(decision.failure_class, "missing_tool")

    def test_non_escalations_carry_no_failure_class(self):
        judge = Judge(RunBudget())
        node = _judge_node(status=PlanStatus.FAILED, measurement=_measurement())
        decision = judge.decide(_judge_plan([node]), [_measurement()])
        self.assertEqual(decision.decision, Decision.REPLAN)
        self.assertEqual(decision.failure_class, "")


# ── Part 1: the loop conversion ──────────────────────────────────


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [event for event, _fields in self.events]


def _loop_runner(tmp: str, audit: _AuditCapture) -> LoopRunner:
    store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)

    async def hang(inputs):
        await asyncio.sleep(30)
        return {"ok": True}

    registry.register("hang", hang)
    return LoopRunner(
        store,
        registry,
        run_store=RunStore(Path(tmp) / "runs"),
        memory_store=ExperimentStore(Path(tmp) / "memory.db"),
        audit_logger=audit,
    )


def _timeout_node(max_retries: int = 0) -> PlanNode:
    return PlanNode(
        id="node-11",
        objective="run the pairing contract tests",
        assigned_claw="hang",
        inputs={"command": "run the pairing contract tests"},
        criteria=[{"type": "exit_code", "equals": 0}],
        timeout_s=0.5,
        max_retries=max_retries,
        hypothesis="the contract tests pass",
    )


def _passing_node() -> PlanNode:
    return PlanNode(
        id="node-11",
        objective="run the pairing contract tests",
        assigned_claw="bash",
        inputs={"command": "true"},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the contract tests pass",
    )


class TestEscalationConversion(unittest.TestCase):
    def test_conversion_fires_and_the_brief_carries_the_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = _fixture_tree(Path(tmp) / "tree", FIXTURE_SERVER_400)
            plan = ExecutionPlan(
                plan_id="plan-convert",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[_timeout_node()],
                metadata={
                    "contract_clauses": SMOKE_CLAUSES,
                    "repo_root": str(tree),
                },
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            briefs: list[dict] = []

            def replan_fn(brief):
                briefs.append(brief)
                # The delta plan's effect, simulated: the repair
                # fixes the shipped server on disk.
                _write(tree / "freetalon" / "pairing" / "server.py", FIXTURE_SERVER_GOOD)
                return [_passing_node()]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(len(briefs), 1)
            brief = briefs[0]
            conversion = brief.get("escalation_conversion") or {}
            self.assertEqual(
                conversion.get("failure_class"), "timeout_retries_exhausted"
            )
            self.assertEqual(conversion.get("timeout_nodes"), ["node-11"])
            diagnostics = brief.get("timeout_diagnostics") or []
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["node_id"], "node-11")
            self.assertEqual(
                diagnostics[0]["command"], "run the pairing contract tests"
            )
            self.assertEqual(diagnostics[0]["timeout_s"], 0.5)
            self.assertEqual(diagnostics[0]["attempts"], 1)
            self.assertEqual(
                diagnostics[0]["criteria"], [{"type": "exit_code", "equals": 0}]
            )
            probe_failures_brief = brief.get("contract_probe_failures") or []
            smoke_failures = [
                r for r in probe_failures_brief if r["probe"] == "http_smoke"
            ]
            self.assertTrue(smoke_failures)
            self.assertTrue(
                any("400" in r["detail"] for r in smoke_failures),
                f"probe failures in brief: {probe_failures_brief}",
            )
            self.assertIn("loop.replan.escalation_converted", audit.names())
            saved = runner._store.load("plan-convert")
            self.assertIsNotNone(saved)
            self.assertEqual(
                (saved.metadata.get("escalation_conversion") or {}).get(
                    "failure_class"
                ),
                "timeout_retries_exhausted",
            )
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )

    def test_arm15_shape_retry_then_convert(self):
        # Arm #15 exactly: the node is retried once, times out
        # again, and only then does the escalation convert.
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            plan = ExecutionPlan(
                plan_id="plan-arm15",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[_timeout_node(max_retries=1)],
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            briefs: list[dict] = []

            def replan_fn(brief):
                briefs.append(brief)
                return [_passing_node()]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["retry", "replan", "continue"],
            )
            self.assertIn("loop.replan.escalation_converted", audit.names())
            diagnostics = briefs[0].get("timeout_diagnostics") or []
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["attempts"], 2)

    def test_no_conversion_when_the_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            plan = ExecutionPlan(
                plan_id="plan-nobudget",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[_timeout_node()],
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=0)
            )
            calls: list[dict] = []

            def replan_fn(brief):
                calls.append(brief)
                return [_passing_node()]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertIsNotNone(result.escalation)
            self.assertIn(
                "timed-out nodes exhausted their retries", result.escalation
            )
            self.assertEqual(calls, [])
            self.assertNotIn("loop.replan.escalation_converted", audit.names())
            self.assertEqual(result.plan_versions, 1)

    def test_missing_tool_escalation_is_not_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            node = PlanNode(
                id="node-1",
                objective="use a tool that does not exist",
                assigned_claw="ghost",
                status=PlanStatus.NEEDS_TOOL,
                error="Missing capability 'ghost'",
            )
            plan = ExecutionPlan(
                plan_id="plan-ghost",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[node],
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            calls: list[dict] = []

            def replan_fn(brief):
                calls.append(brief)
                return [_passing_node()]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertIsNotNone(result.escalation)
            self.assertIn("missing capability", result.escalation)
            self.assertEqual(calls, [])
            self.assertNotIn("loop.replan.escalation_converted", audit.names())
            self.assertEqual(result.plan_versions, 1)


# ── Part 2 in the loop: probe failures force re-plans ────────────


class TestProbeFailureForcesReplan(unittest.TestCase):
    def test_smoke_failure_replans_through_the_standard_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = _fixture_tree(Path(tmp) / "tree", FIXTURE_SERVER_400)
            worker = PlanNode(
                id="node-1",
                objective="a step that passes on its own terms",
                assigned_claw="bash",
                inputs={"command": "true"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the step passes",
            )
            plan = ExecutionPlan(
                plan_id="plan-probe-replan",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[worker],
                metadata={
                    "contract_clauses": SMOKE_CLAUSES,
                    "repo_root": str(tree),
                },
            )
            runner._store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )
            briefs: list[dict] = []

            def replan_fn(brief):
                briefs.append(brief)
                _write(
                    tree / "freetalon" / "pairing" / "server.py",
                    FIXTURE_SERVER_GOOD,
                )
                return [
                    PlanNode(
                        id="node-1",
                        objective="a step that passes on its own terms",
                        assigned_claw="bash",
                        inputs={"command": "true"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the step passes",
                    )
                ]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(len(briefs), 1)
            first = result.decision_history[0]
            self.assertEqual(first["decision"], "replan")
            self.assertIn("contract probes failing", first["reason"])
            brief_failures = briefs[0].get("contract_probe_failures") or []
            self.assertTrue(
                any(r["probe"] == "http_smoke" for r in brief_failures),
                f"brief probe failures: {brief_failures}",
            )


# ── The brief prompt renders the new evidence ────────────────────


class TestBriefRendering(unittest.TestCase):
    def test_conversion_evidence_renders_in_the_prompt(self):
        brief = {
            "goal": {"objective": "pairing goal"},
            "plan_id": "plan-x",
            "plan_version": 1,
            "escalation_conversion": {
                "failure_class": "timeout_retries_exhausted",
                "original_reason": "timed-out nodes exhausted their retries",
                "from_version": 1,
                "timeout_nodes": ["node-11"],
            },
            "timeout_diagnostics": [
                {
                    "node_id": "node-11",
                    "command": "run the pairing contract tests",
                    "timeout_s": 120.0,
                    "attempts": 2,
                }
            ],
            "contract_probe_failures": [
                {
                    "clause_id": "C09-http-api-server-py",
                    "probe": "http_smoke",
                    "ok": False,
                    "detail": "register: POST /pairing/register -> 400",
                }
            ],
        }
        prompt = planner_module._format_brief_for_prompt(brief)
        self.assertIn("Escalation converted (system)", prompt)
        self.assertIn("node-11", prompt)
        self.assertIn("timed out after 120.0s", prompt)
        self.assertIn("attempts: 2", prompt)
        self.assertIn("C09-http-api-server-py [http_smoke]", prompt)
        self.assertIn("register: POST /pairing/register -> 400", prompt)

    def test_a_brief_without_the_fields_renders_as_before(self):
        brief = {"goal": {"objective": "g"}, "plan_id": "p", "plan_version": 1}
        prompt = planner_module._format_brief_for_prompt(brief)
        self.assertNotIn("Escalation converted", prompt)
        self.assertNotIn("Timeout diagnostics", prompt)
        self.assertNotIn("Contract probe failures", prompt)


if __name__ == "__main__":
    unittest.main()
