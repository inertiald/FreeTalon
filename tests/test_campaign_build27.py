"""Campaign build #27 (2026-11-26): probe-gated clause
declarations on the re-plan path + class-7 verdict scope.

Defect 1 (arm #35): the re-plan was rejected because its delta
declared the probe-gated clauses C06-expiry / C08-persistence only
in a "verifies" list — no node implemented them. The bookkeeping
was intact (47 probes ran, the brief carried the failures
verbatim, the delta rule demanded exactly those clauses); the
seam was that neither the re-plan prompt's clauses block nor the
delta validator's coverage error ever identifies a probe-gated
clause or what declaring it requires: the "implements"
declaration must land on a node that repairs the module the
probe exercises (the probe records carry that file), and no
node's tests "verify" such a clause — the system's probe re-run
does. The fix renders the requirement in
format_replan_clauses_block (from the brief's probe-failure
records) and in clause_delta_errors' coverage errors for
probe-gated clauses, threaded from replan_from_brief through the
parse. The gate is unchanged: a clause still counts only when
its probe passes (clause_states, build #25's invariant).

Defect 2 (class-7): held-out B run 2's v1 passed every in-scope
node and its probes; its sole failed node was the full-suite
discover gate, which timed out at its 900s floor (the worktree
discover runs far longer than the floor in that environment).
The suite evaluates the whole repo corpus — far beyond the
goal-named contract's scope — yet _derive_plan_status promoted
its failure into the plan's FAILED status, the judge burned the
re-plan budget on it, and the degrading deltas closed the run
INCONCLUSIVE while the shipped tree scored hidden 17/17. The
fix: in a contract-scoped plan (a frozen clause list in plan
metadata), a failed node whose command runs the full suite is
out-of-scope evidence — its node record and measurement stand
exactly as before, but it does not by itself derive a FAILED
plan status, and in clause_states it counts as a verification
that has not passed, never as a failed verification. In-scope
failures (a scoped test node, a write node) still fail exactly
as before, and plans without a contract are judged exactly as
before (build #22's conservatism: own-test failures still count
and still block PASS — nothing is waived).

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    clause_delta_errors,
    clause_states,
    clauses_for_specs,
    format_replan_clauses_block,
)
from freetalon.orchestrator.executor import _derive_plan_status
from freetalon.orchestrator.intake import LLMSettings
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from freetalon.orchestrator.repo_index import goal_named_specs

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

PAIRING_CLAUSES = [
    {"id": "C01-roles", "heading": "Roles"},
    {"id": "C06-expiry", "heading": "Expiry"},
    {"id": "C08-persistence", "heading": "Persistence"},
    {"id": "C09-http-api-server-py", "heading": "HTTP API (server.py)"},
]

STORE_PROBE_FAILURES = [
    {
        "clause_id": "C06-expiry",
        "probe": "store_behavior",
        "ok": False,
        "detail": (
            "confirm() marked the record confirmed but the on-disk "
            "file still carries the credential bundle after expiry"
        ),
        "flow": "store_expiry_wipe",
        "file": "freetalon/pairing/store.py",
    },
    {
        "clause_id": "C08-persistence",
        "probe": "store_behavior",
        "ok": False,
        "detail": "a corrupt store file was silently reset instead of raising",
        "flow": "store_corrupt_raises",
        "file": "freetalon/pairing/store.py",
    },
]

REQUIRED = ["C06-expiry", "C08-persistence", "C09-http-api-server-py"]


def _node(node_id: str, clauses) -> PlanNode:
    return PlanNode(
        id=node_id,
        objective=f"step {node_id}",
        assigned_claw="bash",
        inputs={"command": "true", "clauses": clauses},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the step holds",
    )


def _attempt3_nodes() -> list[PlanNode]:
    """Arm #35's third re-plan skeleton, in declaration shape:
    the server fix implements C09; the store clauses are declared
    only as verified by the full-suite node."""
    return [
        _node("step_1", {"implements": [], "verifies": []}),
        _node(
            "step_4",
            {"implements": ["C09-http-api-server-py"], "verifies": []},
        ),
        _node(
            "step_6",
            {
                "implements": [],
                "verifies": [
                    "C06-expiry",
                    "C08-persistence",
                    "C09-http-api-server-py",
                ],
            },
        ),
    ]


# ── Part 1a: the delta rule names the probe-gated requirement ────


class TestDeltaProbeGatedErrors(unittest.TestCase):
    def test_errors_name_probe_flow_and_file(self):
        errors = clause_delta_errors(
            _attempt3_nodes(),
            PAIRING_CLAUSES,
            REQUIRED,
            probe_failures=STORE_PROBE_FAILURES,
        )
        self.assertEqual(len(errors), 2)
        for error in errors:
            # The build #7 sentence stands (its test pins it); the
            # probe-gated requirement is appended, not substituted.
            self.assertIn(
                "not implemented by any node in this re-plan", error
            )
        joined = "\n".join(errors)
        self.assertIn("store_behavior", joined)
        self.assertIn("store_expiry_wipe", joined)
        self.assertIn("store_corrupt_raises", joined)
        self.assertIn("freetalon/pairing/store.py", joined)

    def test_errors_without_probe_failures_are_the_generic_errors(self):
        errors = clause_delta_errors(
            _attempt3_nodes(), PAIRING_CLAUSES, REQUIRED
        )
        self.assertEqual(len(errors), 2)
        joined = "\n".join(errors)
        self.assertNotIn("store_behavior", joined)
        self.assertNotIn("store.py", joined)
        for error in errors:
            self.assertIn(
                "not implemented by any node in this re-plan", error
            )

    def test_repair_node_implementing_probe_gated_clauses_is_clean(self):
        nodes = _attempt3_nodes() + [
            _node(
                "step_5",
                {
                    "implements": ["C06-expiry", "C08-persistence"],
                    "verifies": [],
                },
            )
        ]
        self.assertEqual(
            clause_delta_errors(
                nodes,
                PAIRING_CLAUSES,
                REQUIRED,
                probe_failures=STORE_PROBE_FAILURES,
            ),
            [],
        )


# ── Part 1b: the re-plan prompt block carries the requirement ────


class TestReplanBlockProbeGatedGuidance(unittest.TestCase):
    def test_block_names_probe_gated_clauses_and_their_files(self):
        block = format_replan_clauses_block(
            PAIRING_CLAUSES, REQUIRED, probe_failures=STORE_PROBE_FAILURES
        )
        self.assertIn("PROBE-GATED", block)
        self.assertIn("C06-expiry", block)
        self.assertIn("C08-persistence", block)
        self.assertIn("store_behavior", block)
        self.assertIn("freetalon/pairing/store.py", block)

    def test_block_without_probe_failures_has_no_probe_guidance(self):
        block = format_replan_clauses_block(PAIRING_CLAUSES, REQUIRED)
        self.assertNotIn("PROBE-GATED", block)
        self.assertEqual(
            block,
            format_replan_clauses_block(
                PAIRING_CLAUSES, REQUIRED, probe_failures=[]
            ),
        )


# ── Part 1c: replan_from_brief end to end (mocked LLM) ───────────

CONTRACT_TEXT = """# Pairing contract (fixture)

The pairing service pairs fleet nodes with the control plane.

## Roles

The operator registers a machine and confirms it with a token.

## Expiry

A confirmed code expires and its credential bundle is wiped.

## Persistence

The store persists records on disk; a corrupt file raises.

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

STORE_TEXT = '''"""Pairing store (fixture)."""

import json
from pathlib import Path


class PairingStore:
    def __init__(self, path):
        self._path = Path(path)
        if self._path.exists():
            self._records = json.loads(self._path.read_text())
        else:
            self._records = {}

    def poll(self, code):
        return self._records.get(code)
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


class TestReplanProbeGatedEndToEnd(unittest.TestCase):
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
        by_slug = {c["id"].split("-", 1)[1]: c["id"] for c in self.clauses}
        self.expiry = by_slug["expiry"]
        self.persistence = by_slug["persistence"]
        self.http = next(
            c["id"] for c in self.clauses if "http-api" in c["id"]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _brief(self) -> dict:
        failures = []
        for clause_id, flow in (
            (self.expiry, "store_expiry_wipe"),
            (self.persistence, "store_corrupt_raises"),
        ):
            failures.append(
                {
                    "clause_id": clause_id,
                    "probe": "store_behavior",
                    "ok": False,
                    "detail": f"probe flow {flow} failed against the store",
                    "flow": flow,
                    "file": "freetalon/pairing/store.py",
                }
            )
        return {
            "goal": {"objective": GOAL},
            "failed_nodes": [],
            "measurements": [],
            "memory": [],
            "remaining_budget": {"replans": 2},
            "workspace": str(self.root / "ws"),
            "uncovered_clauses": [
                {"id": cid, "heading": "", "reasons": ["probe failing"]}
                for cid in (self.expiry, self.persistence, self.http)
            ],
            "contract_probe_failures": failures,
        }

    def _server_node(self, node_id="step_2"):
        path = str(self.root / "freetalon" / "pairing" / "server.py")
        return _raw_node(
            node_id,
            "scaffold",
            {
                "file_path": path,
                "content": SERVER_TEXT,
                "clauses": {"implements": [self.http], "verifies": []},
            },
            [{"type": "file_exists", "path": path}],
            outputs=[path],
        )

    def _suite_node(self, node_id, depends_on):
        return _raw_node(
            node_id,
            "bash",
            {
                "command": "python3 -m unittest discover -s tests",
                "clauses": {
                    "implements": [],
                    "verifies": [self.expiry, self.persistence, self.http],
                },
            },
            [{"type": "exit_code", "equals": 0}],
            depends_on=depends_on,
        )

    def _attempt3_delta(self) -> list[dict]:
        return [self._server_node(), self._suite_node("step_3", ["step_2"])]

    def _repaired_delta(self) -> list[dict]:
        store_path = str(self.root / "freetalon" / "pairing" / "store.py")
        store_node = _raw_node(
            "step_2",
            "scaffold",
            {
                "file_path": store_path,
                "content": STORE_TEXT,
                "clauses": {
                    "implements": [self.expiry, self.persistence],
                    "verifies": [],
                },
            },
            [{"type": "file_exists", "path": store_path}],
            outputs=[store_path],
        )
        server = self._server_node("step_3")
        return [
            store_node,
            server,
            self._suite_node("step_4", ["step_2", "step_3"]),
        ]

    def test_repair_prompt_names_probe_and_corrected_delta_accepted(self):
        calls: list[tuple[str, str]] = []
        responses = [
            json.dumps({"nodes": self._attempt3_delta()}),
            json.dumps({"nodes": self._repaired_delta()}),
        ]

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            index = min(len(calls) - 1, len(responses) - 1)
            return responses[index]

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            nodes = planner_module.replan_from_brief(
                self._brief(), settings=SETTINGS, repo_root=self.root
            )
        self.assertGreaterEqual(len(calls), 2)
        repair_prompt = calls[1][1]
        self.assertIn("store_behavior", repair_prompt)
        self.assertIn("freetalon/pairing/store.py", repair_prompt)
        implemented = set()
        for node in nodes:
            declared = (node.inputs or {}).get("clauses") or {}
            implemented.update(declared.get("implements") or [])
        self.assertIn(self.expiry, implemented)
        self.assertIn(self.persistence, implemented)


# ── Part 2: class-7 verdict scope ────────────────────────────────

SUITE_COMMAND = "./.venv/bin/python -m unittest discover -s tests -p 'test_*.py'"
SCOPED_COMMAND = "./.venv/bin/python -m unittest tests.test_notes_change_request"


def _status_node(
    node_id: str,
    status: PlanStatus,
    *,
    claw: str = "bash",
    command: str | None = None,
    clauses: dict | None = None,
    measurement: dict | None = None,
) -> PlanNode:
    inputs: dict = {}
    if command is not None:
        inputs["command"] = command
    if clauses is not None:
        inputs["clauses"] = clauses
    node = PlanNode(
        id=node_id,
        objective=f"step {node_id}",
        assigned_claw=claw,
        inputs=inputs,
        criteria=[{"type": "exit_code", "equals": 0}],
        status=status,
    )
    if measurement is not None:
        node.measurement = measurement
    return node


def _plan(nodes, *, contract: bool) -> ExecutionPlan:
    plan = ExecutionPlan(
        plan_id="p27",
        intent=TaskIntent(goal="fixture"),
        nodes=nodes,
    )
    if contract:
        plan.metadata["contract_clauses"] = PAIRING_CLAUSES
    return plan


class TestDerivePlanStatusScope(unittest.TestCase):
    def test_suite_only_failure_in_contract_plan_derives_completed(self):
        # Held-out B run 2, v1: every in-scope node passed; the
        # sole failure is the full-suite discover gate (900s
        # timeout in the run record; status FAILED here).
        plan = _plan(
            [
                _status_node(
                    "node-2", PlanStatus.COMPLETED, claw="scaffold"
                ),
                _status_node(
                    "node-7",
                    PlanStatus.COMPLETED,
                    command=SCOPED_COMMAND,
                ),
                _status_node(
                    "node-8", PlanStatus.FAILED, command=SUITE_COMMAND
                ),
            ],
            contract=True,
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.COMPLETED)

    def test_suite_failure_without_contract_still_fails(self):
        plan = _plan(
            [
                _status_node(
                    "node-7",
                    PlanStatus.COMPLETED,
                    command=SCOPED_COMMAND,
                ),
                _status_node(
                    "node-8", PlanStatus.FAILED, command=SUITE_COMMAND
                ),
            ],
            contract=False,
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.FAILED)

    def test_scoped_failure_still_fails_contract_plan(self):
        plan = _plan(
            [
                _status_node(
                    "node-7", PlanStatus.FAILED, command=SCOPED_COMMAND
                ),
                _status_node(
                    "node-8", PlanStatus.FAILED, command=SUITE_COMMAND
                ),
            ],
            contract=True,
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.FAILED)

    def test_needs_tool_still_fails_contract_plan(self):
        plan = _plan(
            [
                _status_node("node-1", PlanStatus.NEEDS_TOOL),
                _status_node(
                    "node-8", PlanStatus.FAILED, command=SUITE_COMMAND
                ),
            ],
            contract=True,
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.FAILED)


class TestClauseStatesSuiteScope(unittest.TestCase):
    CLAUSES = [{"id": "C04-what-does-not-change", "heading": "Invariants"}]

    def _states(self, verify_node: PlanNode):
        implementer = _status_node(
            "node-2",
            PlanStatus.COMPLETED,
            claw="scaffold",
            clauses={
                "implements": ["C04-what-does-not-change"],
                "verifies": [],
            },
        )
        return clause_states(self.CLAUSES, [implementer, verify_node], [])

    def test_failed_suite_verify_node_is_unverified_not_failed(self):
        suite = _status_node(
            "node-8",
            PlanStatus.FAILED,
            command=SUITE_COMMAND,
            clauses={"implements": [], "verifies": ["C04-what-does-not-change"]},
            measurement={"verdict": "FAIL"},
        )
        states = self._states(suite)
        info = states["C04-what-does-not-change"]
        self.assertEqual(info["state"], "unverified")
        self.assertIn("verification node has not passed", info["reasons"])

    def test_failed_scoped_verify_node_still_fails_clause(self):
        scoped = _status_node(
            "node-7",
            PlanStatus.FAILED,
            command=SCOPED_COMMAND,
            clauses={"implements": [], "verifies": ["C04-what-does-not-change"]},
            measurement={"verdict": "FAIL"},
        )
        states = self._states(scoped)
        self.assertEqual(
            states["C04-what-does-not-change"]["state"], "failed"
        )

    def test_passed_suite_verify_node_still_verifies_clause(self):
        suite = _status_node(
            "node-8",
            PlanStatus.COMPLETED,
            command=SUITE_COMMAND,
            clauses={"implements": [], "verifies": ["C04-what-does-not-change"]},
            measurement={"verdict": "PASS"},
        )
        states = self._states(suite)
        self.assertEqual(
            states["C04-what-does-not-change"]["state"], "verified"
        )


if __name__ == "__main__":
    unittest.main()
