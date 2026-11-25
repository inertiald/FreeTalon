"""Campaign build #12 (2026-11-24): two changes from exam arm #21.

Arm #21 was the first arm whose re-plans all landed — and its
three cycles each exposed the same two structural gaps:

1. The repair seams never fired. The plan's own test node TIMED
   OUT against a hanging server, so the brief named no failing
   tests and no suspect: local_repair declined suspect_not_unique
   and scoped_edit declined no_diagnostics in all three cycles,
   while the loop's own smoke probe held precise per-flow
   diagnostics (register hang, confirm hang, polls refused)
   against exactly one produced file. Part A — probe-derived
   suspect map: live-probe records now carry the flow name and
   the produced file they exercised; contract_clauses gains
   probe_suspect_map (the failures name a suspect only when they
   name exactly one file); the loop stamps the map into every
   re-plan brief that carries probe failures (and the brief
   prompt renders it as the delta's target list); and the
   scoped-edit seam, when the failing-tests source is empty,
   derives its diagnostics block + suspect map from that probe
   evidence — the failed flows stand in for failing tests, under
   the same cap, writer/disk/size preconditions, and grounding
   checks. Cycle 8's local repair is deliberately untouched: its
   targeting needs a definition window (a traceback frame or a
   route anchor), which a flow record does not carry.

2. Probe/acceptance divergence on unknown routes. The smoke
   probe's wrong-method flow checks a KNOWN path (405), but the
   acceptance set also checks an UNKNOWN route expecting 404 —
   arm #21's v4 server answered 405 there too and the probe
   never saw it. Part B — one more deterministic flow,
   unknown_route: GET an unregistered path, expect 404, with a
   405 called out by name in the failure detail.

No GPU is touched: the seam tests mock call_llm, the loop test
uses the registered hang claw and an injected replan function,
and the probe tests boot fixture servers on loopback.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    probe_failures,
    probe_suspect_map,
    run_contract_probes,
)
from freetalon.orchestrator.intake import LLMSettings
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
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)
from tests.test_campaign_build6 import (
    FIXTURE_SERVER_400,
    FIXTURE_SERVER_GOOD,
    FIXTURE_SERVER_HANG,
    SMOKE_CLAUSES,
    _fixture_tree,
    _smoke_records,
    _write,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

SERVER_REL = "freetalon/pairing/server.py"

# ── Part B: the unknown-route flow ───────────────────────────────

# The good fixture with exactly one change: an unregistered path
# answers the wrong-method code instead of 404 (arm #21's v4
# defect — 405 leaking onto unknown routes).
FIXTURE_SERVER_405_UNKNOWN = FIXTURE_SERVER_GOOD.replace(
    '        self._json(404, {"error": "not found"})\n\n    def do_PUT',
    '        self._json(405, {"error": "method not allowed"})\n\n    def do_PUT',
)
assert FIXTURE_SERVER_405_UNKNOWN != FIXTURE_SERVER_GOOD


def _flow(results, name, clause_id="C09-http-api-server-py"):
    return [
        r for r in _smoke_records(results, clause_id) if r.get("flow") == name
    ]


class TestUnknownRouteFlow(unittest.TestCase):
    def test_records_carry_flow_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_GOOD)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            flows = _smoke_records(results)
            self.assertEqual(len(flows), 10)
            for record in flows:
                self.assertEqual(record["file"], SERVER_REL)
                self.assertTrue(record["flow"])

    def test_unknown_route_passes_against_a_correct_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_GOOD)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            unknown = _flow(results, "unknown_route")
            self.assertEqual(len(unknown), 1)
            self.assertTrue(unknown[0]["ok"])
            self.assertEqual(probe_failures(results), [])

    def test_unknown_route_fails_against_a_405_everywhere_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_405_UNKNOWN)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            unknown = _flow(results, "unknown_route")
            self.assertEqual(len(unknown), 1)
            self.assertFalse(unknown[0]["ok"])
            self.assertIn("405", unknown[0]["detail"])
            self.assertIn("404", unknown[0]["detail"])
            self.assertIn(unknown[0], probe_failures(results))
            # The divergence is the only failure: the known-path
            # flows the probe already had still pass.
            wrong = _flow(results, "wrong_method")
            self.assertTrue(wrong[0]["ok"])

    def test_hang_fixture_still_cannot_stall_the_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_HANG)
            started = time.monotonic()
            results = run_contract_probes(SMOKE_CLAUSES, root)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 20.0)
            flows = _smoke_records(results)
            self.assertEqual(len(flows), 10)
            register = _flow(results, "register")
            self.assertFalse(register[0]["ok"])
            self.assertIn("timed out", register[0]["detail"])
            # The new flow was attempted and recorded too (a GET
            # against the hang fixture answers — only POSTs hang).
            unknown = _flow(results, "unknown_route")
            self.assertEqual(len(unknown), 1)
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


# ── Part A: the suspect derivation ───────────────────────────────


def _record(flow, detail, file=SERVER_REL, ok=False, clause="C09-x"):
    return {
        "clause_id": clause,
        "probe": "http_smoke",
        "ok": ok,
        "detail": detail,
        "flow": flow,
        "file": file,
    }


class TestProbeSuspectMap(unittest.TestCase):
    def test_single_file_failures_name_the_suspect(self):
        suspect = probe_suspect_map(
            [
                _record("register", "register: timed out"),
                _record("confirm", "confirm: timed out"),
            ]
        )
        self.assertIsNotNone(suspect)
        self.assertEqual(suspect["file"], SERVER_REL)
        self.assertEqual(
            [entry["flow"] for entry in suspect["flows"]],
            ["register", "confirm"],
        )

    def test_two_files_decline(self):
        suspect = probe_suspect_map(
            [
                _record("register", "register: timed out"),
                _record(
                    "register",
                    "register: timed out",
                    file="freetalon/pairing/store.py",
                ),
            ]
        )
        self.assertIsNone(suspect)

    def test_records_without_a_file_decline(self):
        record = _record("register", "register: timed out")
        del record["file"]
        self.assertIsNone(probe_suspect_map([record]))
        self.assertIsNone(probe_suspect_map([]))

    def test_passing_records_and_duplicates_collapse(self):
        suspect = probe_suspect_map(
            [
                _record("register", "register: timed out"),
                # The same flow recorded under a second clause.
                _record("register", "register: timed out", clause="C10-y"),
                _record("poll_claim", "poll_claim: fine", ok=True),
            ]
        )
        self.assertIsNotNone(suspect)
        self.assertEqual(len(suspect["flows"]), 1)
        self.assertEqual(suspect["flows"][0]["flow"], "register")


# ── Part A: the scoped-edit seam on probe evidence ───────────────

SEAM_SERVER_TEXT = '''"""Fixture pairing server: register hangs (arm #21's defect)."""

import time


def handle_register(body):
    time.sleep(30)
    return 201, {"code": body.get("code", ""), "status": "pending"}


def handle_poll(code):
    return 404, {"code": code, "status": "pending"}
'''

SEAM_GOAL = (
    "Build the pairing service so a node can register a code and "
    "poll for its credential bundle."
)

REGISTER_RECORD = _record(
    "register",
    "register: POST /pairing/register timed out after 3.0s — "
    "the server did not answer (a hang, not an error response)",
)
CONFIRM_RECORD = _record(
    "confirm",
    "confirm: POST /pairing/SMOKE-0001/confirm failed at "
    "transport: [Errno 104] Connection reset by peer",
)


def _block(search: str, replace: str) -> str:
    return (
        "<<<<<<< SEARCH\n"
        f"{search}\n"
        "=======\n"
        f"{replace}\n"
        ">>>>>>> REPLACE\n"
    )


FIX_BLOCK = _block(
    "def handle_register(body):\n    time.sleep(30)\n"
    '    return 201, {"code": body.get("code", ""), "status": "pending"}',
    "def handle_register(body):\n"
    '    return 201, {"code": body.get("code", ""), "status": "pending"}',
)


class ProbeSeamCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.server_path = self.root / "freetalon" / "pairing" / "server.py"
        _write(self.server_path, SEAM_SERVER_TEXT)

    def _plan(self, writer_target: str | None = None) -> ExecutionPlan:
        target = writer_target or str(self.server_path)
        nodes = [
            PlanNode(
                id="node-1",
                objective="Write the pairing server module",
                hypothesis="the server answers the contract flows",
                assigned_claw="scaffold",
                inputs={"file_path": target, "content": SEAM_SERVER_TEXT},
                outputs=[target],
                acceptance=["The module exists."],
                criteria=[{"type": "file_exists", "path": target}],
                depends_on=[],
                status=PlanStatus.COMPLETED,
            ),
            PlanNode(
                id="node-9",
                objective="Run the pairing contract tests",
                hypothesis="the contract tests pass",
                assigned_claw="bash",
                inputs={
                    "command": "python3 -m unittest tests.test_pairing_contract"
                },
                outputs=[],
                acceptance=["The tests pass."],
                criteria=[{"type": "exit_code", "equals": 0}],
                depends_on=["node-1"],
                status=PlanStatus.FAILED,
                error="Node timed out after 120.0s.",
                measurement={
                    "verdict": "FAIL",
                    "envelope_status": "timeout",
                    "results": [],
                    "duration_s": 120.0,
                },
            ),
        ]
        return ExecutionPlan(
            plan_id="plan-probe-seam",
            intent=TaskIntent(
                goal=SEAM_GOAL, constraints={"workspace": str(self.root)}
            ),
            nodes=nodes,
            metadata={"repo_root": str(self.root)},
        )

    def _brief(self, **overrides) -> dict:
        brief = {
            "goal": {"objective": SEAM_GOAL},
            "plan_id": "plan-probe-seam",
            "plan_version": 1,
            # The timeout class: no failing tests are named.
            "failing_tests": [],
            "produced_files": [SERVER_REL],
            "failed_nodes": [{"id": "node-9", "diagnostics": ""}],
            "measurements": [],
            "remaining_budget": {"replans": 2},
            "contract_probe_failures": [REGISTER_RECORD, CONFIRM_RECORD],
            "probe_suspect": {
                "file": SERVER_REL,
                "flows": [
                    {
                        "flow": "register",
                        "clause_id": "C09-x",
                        "probe": "http_smoke",
                        "detail": REGISTER_RECORD["detail"],
                    },
                    {
                        "flow": "confirm",
                        "clause_id": "C09-x",
                        "probe": "http_smoke",
                        "detail": CONFIRM_RECORD["detail"],
                    },
                ],
            },
        }
        brief.update(overrides)
        return brief

    def _run(self, response, plan=None, brief=None):
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return response

        declines: list[dict] = []
        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan if plan is not None else self._plan(),
                brief if brief is not None else self._brief(),
                SETTINGS,
                declines=declines,
            )
        return result, calls, declines


class TestScopedEditOnProbeEvidence(ProbeSeamCase):
    def test_probe_flows_reach_the_seam_as_diagnostics(self):
        result, calls, declines = self._run(FIX_BLOCK)
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])
        self.assertEqual(len(calls), 1)
        _system_prompt, user_prompt = calls[0]
        # The flows are the diagnostics the model reads.
        self.assertIn("Probe diagnostics (system", user_prompt)
        self.assertIn("probe:register", user_prompt)
        self.assertIn("probe:confirm", user_prompt)
        self.assertIn("timed out after 3.0s", user_prompt)
        self.assertIn(SERVER_REL, user_prompt)
        # The candidate: node-1's content with exactly the hang
        # removed, everything else preserved.
        by_id = {node.id: node for node in result}
        expected = SEAM_SERVER_TEXT.replace(
            "    time.sleep(30)\n", ""
        )
        self.assertEqual(by_id["node-1"].inputs["content"], expected)
        self.assertIn(
            "scoped-edit repair", by_id["node-1"].differs_by or ""
        )
        self.assertEqual(by_id["node-1"].responds_to, "node-9")

    def test_the_seam_derives_the_suspect_when_the_brief_lacks_it(self):
        brief = self._brief()
        del brief["probe_suspect"]
        result, calls, declines = self._run(FIX_BLOCK, brief=brief)
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])
        self.assertIn("Probe diagnostics (system", calls[0][1])

    def test_multi_file_probe_failures_still_decline(self):
        brief = self._brief(
            contract_probe_failures=[
                REGISTER_RECORD,
                _record(
                    "register",
                    "register: timed out",
                    file="freetalon/pairing/store.py",
                ),
            ],
            probe_suspect=None,
        )
        result, calls, declines = self._run(FIX_BLOCK, brief=brief)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertEqual(len(declines), 1)
        self.assertEqual(declines[0]["reason"], "no_diagnostics")

    def test_no_probe_evidence_still_declines_no_diagnostics(self):
        brief = self._brief(contract_probe_failures=[], probe_suspect=None)
        result, calls, declines = self._run(FIX_BLOCK, brief=brief)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertEqual(len(declines), 1)
        self.assertEqual(declines[0]["seam"], "scoped_edit_repair")
        self.assertEqual(declines[0]["reason"], "no_diagnostics")

    def test_existing_preconditions_still_gate_the_probe_path(self):
        # The probe names server.py, but no completed write node
        # wrote it: the seam declines on the writer precondition,
        # exactly as on the failing-tests path.
        plan = self._plan(
            writer_target=str(
                self.root / "freetalon" / "pairing" / "store.py"
            )
        )
        result, calls, declines = self._run(FIX_BLOCK, plan=plan)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertEqual(len(declines), 1)
        self.assertEqual(declines[0]["reason"], "no_completed_writer")


# ── Part A: the loop stamps the suspect into the brief ───────────


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


class TestLoopStampsProbeSuspect(unittest.TestCase):
    def test_conversion_brief_carries_the_probe_suspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _AuditCapture()
            runner = _loop_runner(tmp, audit)
            tree = _fixture_tree(Path(tmp) / "tree", FIXTURE_SERVER_400)
            node = PlanNode(
                id="node-11",
                objective="run the pairing contract tests",
                assigned_claw="hang",
                inputs={"command": "run the pairing contract tests"},
                criteria=[{"type": "exit_code", "equals": 0}],
                timeout_s=0.5,
                max_retries=0,
                hypothesis="the contract tests pass",
            )
            plan = ExecutionPlan(
                plan_id="plan-probe-suspect",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[node],
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
                        id="node-11",
                        objective="run the pairing contract tests",
                        assigned_claw="bash",
                        inputs={"command": "true"},
                        criteria=[{"type": "exit_code", "equals": 0}],
                        hypothesis="the contract tests pass",
                    )
                ]

            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(len(briefs), 1)
            brief = briefs[0]
            self.assertEqual(
                (brief.get("escalation_conversion") or {}).get(
                    "failure_class"
                ),
                "timeout_retries_exhausted",
            )
            suspect = brief.get("probe_suspect") or {}
            self.assertEqual(suspect.get("file"), SERVER_REL)
            flows = suspect.get("flows") or []
            self.assertEqual([entry["flow"] for entry in flows], ["register"])
            self.assertIn("400", flows[0]["detail"])
            self.assertIn("loop.replan.probe_suspect", audit.names())
            # The brief prompt renders the suspect as the delta's
            # target list.
            prompt = planner_module._format_brief_for_prompt(brief)
            self.assertIn("Probe suspect (system)", prompt)
            self.assertIn(SERVER_REL, prompt)
            self.assertIn("register", prompt)


if __name__ == "__main__":
    unittest.main()
