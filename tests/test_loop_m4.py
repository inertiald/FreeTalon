"""M4 done tests: a plan that would flash pauses and only proceeds after
an explicit approval. The gate actually blocks: the irreversible handler
is never called while the packet is undecided.
"""

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.gates import GateStore
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    RunBudget,
    SuccessMeasure,
    TaskIntent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws


def _no_replanner(brief):
    raise RuntimeError("re-planner disabled in this test")


class TestLoopM4(unittest.TestCase):
    def _setup(self, tmp):
        root = Path(tmp)
        store = ExecutionPlanStateStore(root / "state.db")
        registry = ToolRegistry(strict=True)
        register_default_claws(registry)
        calls: list[str] = []

        async def flash_handler(inputs):
            calls.append("flash")
            return {"status": "success", "metrics": {"flashed": 1}}

        registry.register("flash_ecu", flash_handler)
        run_store = RunStore(root / "runs")
        gates = GateStore(root / "state.db")
        runner = LoopRunner(
            store, registry, run_store=run_store, gate_store=gates
        )
        return store, registry, run_store, gates, runner, calls

    def _plan(self):
        return ExecutionPlan(
            plan_id="ecu-flash",
            intent=TaskIntent(goal="Flash the ECU with the analysed tune"),
            nodes=[
                PlanNode(
                    id="prep",
                    objective="prepare the tune file",
                    assigned_claw="bash",
                    inputs={"command": "echo tune > tune.bin"},
                    criteria=[{"type": "file_exists", "path": "tune.bin"}],
                ),
                PlanNode(
                    id="flash",
                    objective="flash the ECU with the tune file",
                    assigned_claw="flash_ecu",
                    inputs={"action": "flash_ecu", "file": "tune.bin", "risk": "brick"},
                    depends_on=["prep"],
                    reversible=False,
                    criteria=[
                        {"type": "metric", "name": "flashed", "op": ">=", "value": 1}
                    ],
                ),
            ],
        )

    def _contract(self):
        return GoalContract(
            objective="Flash the ECU with the analysed tune",
            success_measures=[
                SuccessMeasure(name="flashed", metric="flashed", op=">=", value=1)
            ],
            budget=RunBudget(max_replans=0),
        )

    def test_gate_blocks_until_approved_then_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _, gates, runner, calls = self._setup(tmp)
            plan = self._plan()
            store.save(plan)

            # Pass 1: prep runs, flash pauses at the gate — handler not called.
            result = runner.run_plan(plan, self._contract(), replan_fn=_no_replanner)
            self.assertEqual(result.plan.status, PlanStatus.AWAITING_APPROVAL)
            self.assertIsNotNone(result.gate_packet_id)
            self.assertEqual(calls, [])
            flash = [n for n in result.plan.nodes if n.id == "flash"][0]
            self.assertEqual(flash.status, PlanStatus.AWAITING_APPROVAL)
            packet = gates.get(result.gate_packet_id)
            assert packet is not None
            self.assertEqual(packet.action, "flash_ecu")
            self.assertEqual(packet.reversible, False)
            self.assertEqual(packet.status, "pending")
            packet_path = (
                Path(tmp) / "runs" / result.run_id / "gates" / f"{packet.packet_id}.json"
            )
            self.assertTrue(packet_path.is_file())

            # Approve, then resume: the flash runs and the goal passes.
            gates.decide(packet.packet_id, "approve", "pat")
            resumed = runner.resume_run(result.run_id, "ecu-flash", self._contract())
            self.assertEqual(calls, ["flash"])
            self.assertEqual(resumed.plan.status, PlanStatus.COMPLETED)
            self.assertEqual(resumed.goal_verdict, "PASS")

    def test_gate_denied_fails_the_node_and_is_never_reasked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _, gates, runner, calls = self._setup(tmp)
            plan = self._plan()
            store.save(plan)

            result = runner.run_plan(plan, self._contract(), replan_fn=_no_replanner)
            assert result.gate_packet_id is not None
            gates.decide(result.gate_packet_id, "deny", "pat", note="too risky")
            resumed = runner.resume_run(result.run_id, "ecu-flash", self._contract())
            self.assertEqual(calls, [])
            flash = [n for n in resumed.plan.nodes if n.id == "flash"][0]
            self.assertEqual(flash.status, PlanStatus.FAILED)
            self.assertIn("denied", flash.error or "")
            self.assertEqual(resumed.goal_verdict, "FAIL")

            # A second run of the same goal with the same irreversible node
            # does not re-ask: M2 memory refuses the measured-failed
            # approach before anything runs, so no new packet is created.
            plan2 = self._plan()
            plan2.plan_id = "ecu-flash-2"
            store.save(plan2)
            result2 = runner.run_plan(plan2, self._contract(), replan_fn=_no_replanner)
            self.assertEqual(calls, [])
            self.assertIsNone(result2.gate_packet_id)
            self.assertIn("measured-failed approach", result2.escalation or "")
            flash2 = [n for n in result2.plan.nodes if n.id == "flash"][0]
            self.assertEqual(flash2.status, PlanStatus.DRAFT)

    def test_gate_expired_is_deny_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _, gates, runner, calls = self._setup(tmp)
            plan = self._plan()
            store.save(plan)

            result = runner.run_plan(plan, self._contract(), replan_fn=_no_replanner)
            assert result.gate_packet_id is not None
            # Simulate the packet expiring before any decision.
            from datetime import UTC, datetime, timedelta

            gates.expire_overdue(now=datetime.now(UTC) + timedelta(seconds=7200))
            resumed = runner.resume_run(result.run_id, "ecu-flash", self._contract())
            self.assertEqual(calls, [])
            flash = [n for n in resumed.plan.nodes if n.id == "flash"][0]
            self.assertEqual(flash.status, PlanStatus.FAILED)
            self.assertIn("expired", flash.error or "")

    def test_resume_with_pending_packet_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _, _, runner, _ = self._setup(tmp)
            plan = self._plan()
            store.save(plan)
            result = runner.run_plan(plan, self._contract(), replan_fn=_no_replanner)
            with self.assertRaises(ValueError):
                runner.resume_run(result.run_id, "ecu-flash", self._contract())

    def test_decide_after_decision_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _, gates, runner, _ = self._setup(tmp)
            plan = self._plan()
            store.save(plan)
            result = runner.run_plan(plan, self._contract(), replan_fn=_no_replanner)
            assert result.gate_packet_id is not None
            gates.decide(result.gate_packet_id, "approve", "pat")
            with self.assertRaises(ValueError):
                gates.decide(result.gate_packet_id, "deny", "pat")

    def test_irreversible_node_without_a_gate_fails(self):
        # A gate callback is always configured by LoopRunner, but the
        # executor on its own must refuse to run an irreversible node.
        import asyncio

        from freetalon.orchestrator.executor import Executor

        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionPlanStateStore(Path(tmp) / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            calls: list[str] = []

            async def flash_handler(inputs):
                calls.append("flash")
                return {"status": "success"}

            registry.register("flash_ecu", flash_handler)
            plan = self._plan()
            store.save(plan)
            executor = Executor(store, registry)
            finished = asyncio.run(executor.run("ecu-flash"))
            flash = [n for n in finished.nodes if n.id == "flash"][0]
            self.assertEqual(flash.status, PlanStatus.FAILED)
            self.assertIn("approval gate", flash.error or "")
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
