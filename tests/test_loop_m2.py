"""Integration tests for M2 experiment memory.

The M2 done test: a second run of the same goal does not repeat a
measured-failed approach.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import ExperimentStore, goal_key_for
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
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws


def _make_runner(tmpdir: str):
    store = ExecutionPlanStateStore(db_path=Path(tmpdir) / "state.db")
    registry = ToolRegistry(strict=True)
    register_default_claws(registry)
    run_store = RunStore(Path(tmpdir) / "runs")
    memory = ExperimentStore(Path(tmpdir) / "memory.db")
    runner = LoopRunner(store, registry, run_store=run_store, memory_store=memory)
    return runner, store, registry, memory


def _plan(plan_id: str, nodes: list[PlanNode]) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id, intent=TaskIntent(goal="test goal"), nodes=nodes
    )


def _failing_node():
    return PlanNode(
        id="node-1",
        objective="exit zero",
        assigned_claw="bash",
        inputs={"command": "exit 1"},
        criteria=[{"type": "exit_code", "equals": 0}],
        hypothesis="the command exits zero",
    )


def _no_replanner(brief):
    raise RuntimeError("no re-planner in this test")


class TestLoopMemory(unittest.TestCase):
    def test_second_run_does_not_repeat_failed_approach(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            # Run 1: fails, and the failure is recorded in memory.
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            result1 = runner.run_plan(plan1, contract, replan_fn=_no_replanner)
            self.assertEqual(result1.goal_verdict, "FAIL")
            self.assertEqual(memory.count(), 1)
            # The run's own memory.jsonl has the record too.
            mem_records = runner._run_store.read_memory(result1.run_id)
            self.assertEqual(len(mem_records), 1)
            self.assertEqual(mem_records[0]["decision"], "ESCALATE")

            # Run 2: the same goal, the same approach — refused before running.
            plan2 = _plan("plan-mem-2", [_failing_node()])
            store.save(plan2)
            result2 = runner.run_plan(plan2, contract, replan_fn=_no_replanner)
            self.assertEqual(result2.goal_verdict, "FAIL")
            self.assertIsNotNone(result2.escalation)
            self.assertIn("repeats a measured-failed approach", result2.escalation)
            self.assertEqual(result2.measurements, [])  # nothing executed
            self.assertEqual(result2.plan.nodes[0].status, PlanStatus.DRAFT)
            self.assertEqual(memory.count(), 1)  # no new experiment recorded

    def test_second_run_allowed_when_it_states_what_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            runner.run_plan(plan1, contract, replan_fn=_no_replanner)
            self.assertEqual(memory.count(), 1)

            # Same approach, but differs_by states the change — allowed.
            changed = PlanNode(
                id="node-1",
                objective="write a marker instead",
                assigned_claw="bash",
                inputs={"command": "echo ok > ok.txt"},
                criteria=[{"type": "file_exists", "path": "ok.txt"}],
                hypothesis="the command exits zero",
                differs_by="writes a file instead of relying on exit code",
            )
            plan2 = _plan("plan-mem-2", [changed])
            store.save(plan2)
            result2 = runner.run_plan(plan2, contract, replan_fn=_no_replanner)
            self.assertEqual(result2.goal_verdict, "PASS")
            self.assertEqual(memory.count(), 2)

    def test_replan_rejected_when_it_repeats_a_recorded_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=3)
            )
            # Run 1 records the bash/exit-zero failure.
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            runner.run_plan(plan1, contract, replan_fn=_no_replanner)

            # Run 2: a different first approach fails; its re-plan tries
            # the recorded-failed approach again, without differs_by.
            other = PlanNode(
                id="node-1",
                objective="write a missing file",
                assigned_claw="bash",
                inputs={"command": "true"},
                criteria=[{"type": "file_exists", "path": "nope.txt"}],
                hypothesis="the file exists",
            )
            plan2 = _plan("plan-mem-2", [other])
            store.save(plan2)

            def repeat_recorded_failure(brief):
                return [_failing_node()]

            result2 = runner.run_plan(
                plan2, contract, replan_fn=repeat_recorded_failure
            )
            self.assertEqual(result2.goal_verdict, "FAIL")
            self.assertIsNotNone(result2.escalation)
            self.assertIn("repeats failed approach", result2.escalation)

    def test_different_goal_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            plan1 = _plan("plan-mem-1", [_failing_node()])
            store.save(plan1)
            runner.run_plan(plan1, contract, replan_fn=_no_replanner)
            self.assertEqual(memory.count(), 1)

            other_contract = GoalContract(
                objective="a completely different goal",
                budget=RunBudget(max_replans=0),
            )
            plan2 = _plan("plan-mem-3", [_failing_node()])
            store.save(plan2)
            result2 = runner.run_plan(plan2, other_contract, replan_fn=_no_replanner)
            # Runs (and fails on its own merit) — memory for the other goal
            # does not block it.
            self.assertEqual(len(result2.measurements), 1)
            self.assertEqual(memory.count(), 2)


def _measured(verdict="FAIL", exit_code=1):
    return {
        "node_id": "n",
        "verdict": verdict,
        "results": [
            {"criterion": {"type": "exit_code", "equals": 0},
             "passed": verdict == "PASS", "measured": True,
             "detail": f"exit_code {exit_code} != 0" if verdict == "FAIL" else "ok"}
        ],
        "metrics": {},
        "exit_code": exit_code,
        "duration_s": 0.1,
    }


def _seed(memory, node, verdict="FAIL", decision="REPLAN"):
    return memory.record(
        run_id="run-history",
        goal_key=goal_key_for("test goal"),
        domain="general",
        node=node,
        measurement=_measured(verdict),
        decision=decision,
    )


class TestLoopMemorySignature(unittest.TestCase):
    """Cycle 3: repeat detection matches substance, not wording.

    The fixture mirrors the 2026-11-17 live matrix run: Arm A's plan
    (plan-7885a4ef9d99) was accepted, then blocked before node-1
    because its generic verification node text-matched a historical
    curl failure. The historical records below mirror the real memory
    state for that goal (executed curl/test/server failures whose
    causes were upstream; scaffold writes that passed).
    """

    def _matrix_history(self, memory):
        _seed(memory, PlanNode(
            id="node-4", objective="Verify /health live", assigned_claw="bash",
            inputs={"command": "curl -s http://localhost:8765/health"},
            criteria=[{"type": "exit_code", "equals": 0}],
        ), verdict="FAIL")
        _seed(memory, PlanNode(
            id="node-2", objective="Run tests after modifying api.py",
            assigned_claw="bash",
            inputs={"command": "cd /repo && ./.venv/bin/python -m unittest discover -s tests"},
            criteria=[{"type": "exit_code", "equals": 0}],
        ), verdict="FAIL")
        _seed(memory, PlanNode(
            id="node-3", objective="Start the FreeTalon API server",
            assigned_claw="bash",
            inputs={"command": "cd /repo && ./venv/bin/python -m freetalon.cli start"},
            criteria=[{"type": "exit_code", "equals": 0}],
        ), verdict="FAIL")
        _seed(memory, PlanNode(
            id="node-1", objective="Extend the API module",
            assigned_claw="scaffold",
            inputs={"file_path": "/repo/freetalon/api.py", "content": "# code\n"},
            criteria=[{"type": "file_exists", "path": "/repo/freetalon/api.py"}],
        ), verdict="PASS", decision="CONTINUE")

    def _matrix_plan(self):
        # The Arm A plan's shape: two substantive writes, then the
        # universal plumbing tail (run tests, start server, verify).
        return _plan("plan-matrix", [
            PlanNode(
                id="node-1",
                objective="Extend /repo/freetalon/api.py in place",
                assigned_claw="scaffold",
                inputs={"file_path": "/repo/freetalon/api.py",
                        "content": "# extended module\n"},
                criteria=[{"type": "file_exists", "path": "/repo/freetalon/api.py"}],
            ),
            PlanNode(
                id="node-2", objective="Write the tests for /health",
                assigned_claw="scaffold",
                inputs={"file_path": "/ws/test_health.py",
                        "content": "# tests\n"},
                criteria=[{"type": "file_exists", "path": "/ws/test_health.py"}],
                depends_on=["node-1"],
            ),
            PlanNode(
                id="node-3", objective="Run the tests for /health",
                assigned_claw="bash",
                inputs={"command": "./.venv/bin/python /ws/test_health.py"},
                criteria=[{"type": "exit_code", "equals": 0}],
                depends_on=["node-2"],
            ),
            PlanNode(
                id="node-4", objective="Start the API server for /health",
                assigned_claw="bash",
                inputs={"command": "cd /repo && ./.venv/bin/python -m freetalon.cli stop; "
                                   "./.venv/bin/python -m freetalon.cli start"},
                criteria=[{"type": "exit_code", "equals": 0}],
                depends_on=["node-3"],
            ),
            PlanNode(
                id="node-5", objective="Verify /health live",
                assigned_claw="bash",
                inputs={"command": "curl -s http://localhost:8765/health"},
                criteria=[{"type": "exit_code", "equals": 0}],
                depends_on=["node-4"],
            ),
        ])

    def test_matrix_plan_is_not_a_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, memory = _make_runner(tmp)
            self._matrix_history(memory)
            plan = self._matrix_plan()
            repeat = runner._find_repeated_approach(
                plan, goal_key_for("test goal"), "general"
            )
            self.assertIsNone(repeat)

    def test_executed_write_repeat_identical_payload_still_escalates(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            _seed(memory, PlanNode(
                id="node-1", objective="Write the module the first way",
                assigned_claw="scaffold",
                inputs={"file_path": "/ws/api.py", "content": "# v1\n"},
                criteria=[{"type": "file_exists", "path": "/ws/api.py"}],
            ), verdict="FAIL")
            self.assertEqual(memory.count(), 1)
            # Same mechanism on the same file, completely different
            # wording — and the IDENTICAL payload that was measured
            # FAIL. A true repeat: still escalates before running.
            plan = _plan("plan-repeat", [PlanNode(
                id="node-1", objective="Completely reworded objective",
                assigned_claw="scaffold",
                inputs={"file_path": "/ws/api.py", "content": "# v1\n"},
                criteria=[{"type": "file_exists", "path": "/ws/api.py"}],
            )])
            store.save(plan)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            result = runner.run_plan(plan, contract, replan_fn=_no_replanner)
            self.assertIsNotNone(result.escalation)
            self.assertIn("repeats a measured-failed approach", result.escalation)
            self.assertIn("/ws/api.py", result.escalation)
            self.assertEqual(result.measurements, [])  # nothing executed
            self.assertEqual(memory.count(), 1)  # nothing new recorded

    def test_executed_write_repeat_new_payload_proceeds_with_warning(self):
        # The 2026-11-23 live-proof defect (run
        # run-20261124T003551Z-523bc2): the signature-only gate
        # vetoed a substantive new plan because a scaffold write of
        # the same file had once been measured FAIL. A different
        # payload is a different substance: the run proceeds, and
        # the historical failure rides in the plan metadata as a
        # warning — recorded, never silent.
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            target = f"{tmp}/api.py"
            _seed(memory, PlanNode(
                id="node-1", objective="Write the module the first way",
                assigned_claw="scaffold",
                inputs={"file_path": target, "content": "# v1\n"},
                criteria=[{"type": "file_exists", "path": target}],
            ), verdict="FAIL")
            self.assertEqual(memory.count(), 1)
            plan = _plan("plan-repeat", [PlanNode(
                id="node-1", objective="Completely reworded objective",
                assigned_claw="scaffold",
                inputs={"file_path": target, "content": "# v2, new code\n"},
                criteria=[{"type": "file_exists", "path": target}],
            )])
            store.save(plan)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            result = runner.run_plan(plan, contract, replan_fn=_no_replanner)
            self.assertIsNone(result.escalation)
            self.assertEqual(result.goal_verdict, "PASS")  # it executed
            self.assertEqual(memory.count(), 2)  # and was measured
            warnings = (result.plan.metadata or {}).get(
                "repeated_approach_warnings"
            )
            self.assertIsNotNone(warnings)
            self.assertEqual(len(warnings), 1)
            self.assertIn("measured FAIL", warnings[0])
            self.assertIn(target, warnings[0])

    def test_same_wording_different_mechanism_is_not_a_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, memory = _make_runner(tmp)
            _seed(memory, PlanNode(
                id="node-1", objective="Verify /health live",
                assigned_claw="bash", inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="Verify /health live",
            ), verdict="FAIL")
            plan = _plan("plan-wording", [PlanNode(
                id="node-1", objective="Verify /health live",
                assigned_claw="scaffold",
                inputs={"file_path": "/ws/api.py", "content": "# code\n"},
                criteria=[{"type": "file_exists", "path": "/ws/api.py"}],
                hypothesis="Verify /health live",
            )])
            repeat = runner._find_repeated_approach(
                plan, goal_key_for("test goal"), "general"
            )
            self.assertIsNone(repeat)

    def test_plumbing_history_does_not_block_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _, memory = _make_runner(tmp)
            self._matrix_history(memory)
            plan = _plan("plan-runs", [PlanNode(
                id="node-1", objective="a trivially passing command",
                assigned_claw="bash", inputs={"command": "true"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="true exits zero",
            )])
            store.save(plan)
            contract = GoalContract(
                objective="test goal", budget=RunBudget(max_replans=0)
            )
            result = runner.run_plan(plan, contract, replan_fn=_no_replanner)
            self.assertIsNone(result.escalation)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(len(result.measurements), 1)


if __name__ == "__main__":
    unittest.main()
