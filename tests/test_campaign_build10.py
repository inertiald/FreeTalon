"""Campaign build #10 (2026-11-24): clause-slot independence,
stuck-detector payload identity, command-form prompt steering,
and attempt-sink coverage for initial planning.

Arm #19's re-plan produced three skeleton attempts; attempts 1
and 3 were declaration-complete deltas covering the open clauses
exactly — including the first-ever proposal to create
__main__.py — and the run still died, for three distinct
reasons, each addressed here:

(1) VALIDATOR ORDERING: the clause slot in the collector's gated
authored-plan sequence fired only when NO other error records
existed. Every attempt carried a 'command' record, so
clause_coverage never ran — build #7's delta rule has still
never judged a live delta, and attempt 2's dropped declarations
went unflagged for the same reason. The clause slot is now
independent: it contributes its records alongside any others.

(2) STUCK DETECTOR: step_5 in every attempt was a live smoke
check written as an inline python -c command; the command
validator rejected every form, the payloads were NOT identical,
but the same error CODE three times in a row tripped the repair
loop's stuck detector and the re-planner failed terminally.
The stuck signature now keys on (code, node, payload identity)
— whitespace-normalized, the judge-gate principle (92448dc):
same code on a materially different payload is iteration, not
stuckness. Both planner prompts also steer live checks to the
scaffold+run form (and, for contract work, to the loop's own
probes) so the form is right before validation has to judge it.

(3) PERSISTENCE GAP: the run's planner_attempts.jsonl held only
the re-plan records; the initial planning attempts stood in the
driver log's stdout blocks only, because the sink is installed
in run_plan — after initial planning has already happened.
Initial-phase records made with no sink installed now wait in a
bounded pending buffer that run_plan drains into the run's
jsonl on entry.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import clauses_for_specs
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    GoalContract,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.prompts import (
    planner_system_prompt,
    replan_system_prompt,
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

GOAL = "Implement the pairing service per freetalon/pairing/CONTRACT.md."

# A command the glued-words heuristic rejects deterministically:
# the first token jams a program, its flags, and a path together.
GLUED_A = "python3-munittestdiscover-s/tests_alpha"
GLUED_B = "python3-munittestdiscover-s/tests_beta"
GLUED_C = "python3-munittestdiscover-s/tests_gamma"


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


def _bash_dag(command: str, node_id: str = "step_5") -> str:
    return json.dumps(
        {
            "nodes": [
                _raw_node(
                    node_id,
                    "bash",
                    {"command": command},
                    [{"type": "exit_code", "equals": 0}],
                )
            ],
            "metadata": {},
        }
    )


def _codes(exc: BaseException) -> set[str]:
    return {record.code for record in getattr(exc, "plan_errors", []) or []}


class ClauseFixtureCase(unittest.TestCase):
    """A tmp repo root carrying the fixture contract + its clauses."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write(self.root / "freetalon" / "__init__.py", "")
        _write(
            self.root / "freetalon" / "pairing" / "CONTRACT.md",
            CONTRACT_TEXT,
        )
        (self.root / "tests").mkdir(exist_ok=True)
        specs = goal_named_specs(GOAL, self.root)
        self.clauses = clauses_for_specs(specs)
        self.assertEqual(len(self.clauses), 3)
        self.open_clause = self.clauses[-1]["id"]
        self.assertIn("http-api", self.open_clause)

    def _parse(self, payload: str, **kwargs):
        return planner_module.parse_planner_response(
            payload,
            goal_text=GOAL,
            repo_root=self.root,
            clauses=self.clauses,
            **kwargs,
        )


class TestClauseSlotIndependence(ClauseFixtureCase):
    """Fix A: the clause slot runs even when other records exist."""

    def test_command_and_clause_errors_reported_together(self) -> None:
        # A glued command (a 'command' record) AND no clause
        # declarations at all (a clause_coverage record): before
        # build #10 the command record suppressed the clause slot
        # entirely, so the rejection named only the command.
        payload = json.dumps(
            {
                "nodes": [
                    _raw_node(
                        "step_1",
                        "bash",
                        {"command": "true"},
                        [{"type": "exit_code", "equals": 0}],
                    ),
                    _raw_node(
                        "step_5",
                        "bash",
                        {"command": GLUED_A},
                        [{"type": "exit_code", "equals": 0}],
                        depends_on=["step_1"],
                    ),
                ],
                "metadata": {},
            }
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(payload)
        codes = _codes(caught.exception)
        self.assertIn("command", codes)
        self.assertIn("clause_coverage", codes)

    def test_delta_missing_declarations_flagged_despite_other_error(
        self,
    ) -> None:
        # Arm #19 attempt 2's shape: a delta whose node dropped
        # its clause declaration, beside an unrelated command
        # error. The delta rule must still judge it.
        payload = json.dumps(
            {
                "nodes": [
                    _raw_node(
                        "step_5",
                        "bash",
                        {"command": GLUED_A},
                        [{"type": "exit_code", "equals": 0}],
                    )
                ],
                "metadata": {},
            }
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                payload,
                clause_mode="delta",
                clause_required=[self.open_clause],
            )
        codes = _codes(caught.exception)
        self.assertIn("command", codes)
        self.assertIn("clause_coverage", codes)

    def test_delta_with_declarations_gets_no_manufactured_clause_error(
        self,
    ) -> None:
        # Independence is not manufacture: a delta that declares
        # its mapping and covers the open clause, beside the same
        # command error, draws no clause_coverage record.
        node = _raw_node(
            "step_5",
            "bash",
            {"command": GLUED_A},
            [{"type": "exit_code", "equals": 0}],
        )
        node["inputs"]["clauses"] = {
            "implements": [self.open_clause],
            "verifies": [self.open_clause],
        }
        payload = json.dumps({"nodes": [node], "metadata": {}})
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                payload,
                clause_mode="delta",
                clause_required=[self.open_clause],
            )
        codes = _codes(caught.exception)
        self.assertIn("command", codes)
        self.assertNotIn("clause_coverage", codes)


class _PlanDriver:
    """A scripted call_llm for plan_task_intent (last repeats)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, system_prompt, user_prompt, settings=None):
        self.calls.append(user_prompt)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class TestStuckPayloadIdentity(unittest.TestCase):
    """Fix B: stuck detection keys on code + payload identity."""

    def setUp(self) -> None:
        # plan_task_intent without a sink buffers initial-phase
        # records (fix D); leave the global buffer as found.
        self.addCleanup(
            planner_module.drain_pending_planner_attempts,
            lambda record: None,
        )

    def _drive(self, responses, **kwargs):
        driver = _PlanDriver(responses)
        intent = TaskIntent(
            goal="Run the pairing smoke check", project_type="general"
        )
        with mock.patch.object(planner_module, "call_llm", driver):
            plan = planner_module.plan_task_intent(
                intent, settings=SETTINGS, **kwargs
            )
        return plan, driver.calls

    def test_same_code_different_payloads_do_not_trip_stuck(self) -> None:
        # Arm #19's shape: three 'command' rejections of the same
        # node whose commands are materially different attempts,
        # then a good plan. No stuck escalation — the fourth
        # attempt happens and is accepted.
        good = _bash_dag("true", node_id="step_1")
        plan, calls = self._drive(
            [_bash_dag(GLUED_A), _bash_dag(GLUED_B), _bash_dag(GLUED_C), good],
            max_repairs=4,
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(plan.nodes), 1)

    def test_same_code_same_payload_trips_stuck(self) -> None:
        # The identical rejection three times in a row is still
        # stuckness: the detector fires at the third attempt and
        # the remaining budget is not spent re-asking.
        driver = _PlanDriver([_bash_dag(GLUED_A)])
        intent = TaskIntent(
            goal="Run the pairing smoke check", project_type="general"
        )
        with mock.patch.object(planner_module, "call_llm", driver):
            with self.assertRaises(
                planner_module.StuckRepairError
            ) as caught:
                planner_module.plan_task_intent(
                    intent, settings=SETTINGS, max_repairs=4
                )
        self.assertEqual(len(driver.calls), 3)
        self.assertIn("command", _codes(caught.exception))

    def test_signature_distinguishes_payload_not_layout(self) -> None:
        record = planner_module.PlanError(
            code="command", message="m", node_id="step_5"
        )
        sig_a = planner_module._rejection_signature(
            _bash_dag("python3  check.py   --fast"), [record]
        )
        sig_b = planner_module._rejection_signature(
            _bash_dag("python3 check.py --fast"), [record]
        )
        sig_c = planner_module._rejection_signature(
            _bash_dag("python3 check.py --slow"), [record]
        )
        # Whitespace-only differences are the same substance;
        # a different argument is a different payload.
        self.assertEqual(sig_a, sig_b)
        self.assertNotEqual(sig_a, sig_c)


class TestCommandFormSteering(unittest.TestCase):
    """Fix C: both planner prompts carry the scaffold+run rule."""

    def _assert_steering(self, prompt: str) -> None:
        flat = " ".join(prompt.split())
        self.assertIn("python -c", flat)
        self.assertIn("glued words", flat)
        self.assertIn("scaffold", flat)
        self.assertIn("python3 <path>", flat)
        self.assertIn("probes re-verify the executed tree", flat)

    def test_planner_prompt_steers_live_checks(self) -> None:
        self._assert_steering(planner_system_prompt())

    def test_replan_prompt_steers_live_checks(self) -> None:
        self._assert_steering(replan_system_prompt())


class TestPendingAttemptBuffer(unittest.TestCase):
    """Fix D, unit level: the buffer holds initial phases only,
    and a drain delivers them once, in order."""

    def setUp(self) -> None:
        planner_module.drain_pending_planner_attempts(lambda record: None)
        self.addCleanup(
            planner_module.drain_pending_planner_attempts,
            lambda record: None,
        )

    def _record(self, phase: str, attempt: int) -> None:
        planner_module._record_planner_attempt(
            phase=phase,
            attempt=attempt,
            outcome="rejected",
            response_text=f"raw {phase} {attempt}",
            error_codes=["command"],
            errors=["boom"],
        )

    def test_initial_phases_buffer_and_drain_once(self) -> None:
        self._record("plan_skeleton", 1)
        self._record("content_fill", 1)
        self._record("replan_skeleton", 1)
        drained: list[dict] = []
        count = planner_module.drain_pending_planner_attempts(
            drained.append
        )
        # The re-plan record was made with no sink too, but only
        # the initial phases buffer — a re-plan attempt belongs to
        # the run (or caller) that made it, never to a later run.
        self.assertEqual(count, 2)
        self.assertEqual(
            [record["phase"] for record in drained],
            ["plan_skeleton", "content_fill"],
        )
        self.assertEqual(
            planner_module.drain_pending_planner_attempts(
                drained.append
            ),
            0,
        )


class TestInitialAttemptsReachRunJsonl(unittest.TestCase):
    """Fix D, end to end: a loop-driven run's planner_attempts.jsonl
    opens with the initial planning that produced its plan."""

    def setUp(self) -> None:
        planner_module.drain_pending_planner_attempts(lambda record: None)
        self.addCleanup(
            planner_module.drain_pending_planner_attempts,
            lambda record: None,
        )

    def test_run_jsonl_contains_initial_and_replan_records(self) -> None:
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
                            "run",
                            "bash",
                            {"command": "false"},
                            [{"type": "exit_code", "equals": 0}],
                            depends_on=["w-store"],
                        ),
                    ],
                    "metadata": {},
                }
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
            store_content = (
                "class Store:\n    def get(self):\n        return 1\n"
            )

            class _LLM:
                """One script for both planning calls: non-fill
                calls consume the queue in order (initial skeleton,
                then the re-plan delta skeleton); fill calls answer
                the named target's payload."""

                def __init__(self):
                    self.queue = [skeleton, delta]
                    self.calls: list[str] = []

                def __call__(self, system_prompt, user_prompt, settings=None):
                    self.calls.append(user_prompt)
                    if "CONTENT FILL" in user_prompt:
                        return json.dumps({"content": store_content})
                    if len(self.queue) > 1:
                        return self.queue.pop(0)
                    return self.queue[0]

            llm = _LLM()
            intent = TaskIntent(goal="Create a store module in pkg")
            with mock.patch.object(planner_module, "call_llm", llm):
                # The driver flow: plan FIRST, with no sink
                # installed anywhere — the attempts buffer.
                plan = planner_module.plan_task_intent(
                    intent, settings=SETTINGS, repo_root=root
                )
                store = ExecutionPlanStateStore(root / "state.db")
                registry = ToolRegistry(strict=True)
                register_default_claws(registry)
                run_store = RunStore(root / "runs")
                runner = LoopRunner(
                    store,
                    registry,
                    run_store=run_store,
                    memory_store=ExperimentStore(root / "memory.db"),
                )
                store.save(plan)
                contract = GoalContract(
                    objective="Create a store module in pkg",
                    budget=RunBudget(max_replans=2),
                )
                result = runner.run_plan(plan, contract)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            attempts = run_store.read_planner_attempts(result.run_id)
            phases = [record["phase"] for record in attempts]
            self.assertIn("plan_skeleton", phases)
            self.assertIn("content_fill", phases)
            self.assertIn("replan_skeleton", phases)
            # The run's trail opens with the planning that
            # produced its plan, ahead of its own re-plan records.
            self.assertLess(
                phases.index("plan_skeleton"),
                phases.index("replan_skeleton"),
            )
            self.assertLess(
                phases.index("content_fill"),
                phases.index("replan_skeleton"),
            )
            skeleton_records = [
                record
                for record in attempts
                if record["phase"] == "plan_skeleton"
            ]
            self.assertIn("w-store", skeleton_records[0]["response_text"])


if __name__ == "__main__":
    unittest.main()
