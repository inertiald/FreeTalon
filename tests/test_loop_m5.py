"""M5 done tests: both worked examples run end-to-end on one node with a
human gate before the irreversible step. No real money, no live ECU,
no actual flashing — the gated handlers are never called.
"""

import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.ecu import register_ecu_claws
from freetalon.orchestrator.finance import register_finance_claws, synthetic_prices
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
from freetalon.orchestrator.research import register_research_claws
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.sandbox import register_sandbox_claws
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws
from tests.test_loop_m3 import _ecu_http


def _no_replanner(brief):
    raise RuntimeError("re-planner disabled in this test")


def _write_csv(path: Path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class TestLoopM5Finance(unittest.TestCase):
    def test_finance_flow_pauses_at_the_invest_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = root / "prices.csv"
            _write_csv(prices, synthetic_prices(), ["date", "close"])
            train_data = root / "train.csv"
            _write_csv(
                train_data,
                [{"x": float(i), "y": 2.0 * i + 1.0} for i in range(20)],
                ["x", "y"],
            )
            store = ExecutionPlanStateStore(root / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            register_finance_claws(registry)
            invest_calls: list[str] = []

            async def invest_recorder(inputs):
                invest_calls.append("invest")
                return {"status": "success"}

            registry.register("invest", invest_recorder)
            run_store = RunStore(root / "runs")
            gates = GateStore(root / "state.db")
            runner = LoopRunner(store, registry, run_store=run_store, gate_store=gates)

            plan = ExecutionPlan(
                plan_id="finance-flow",
                intent=TaskIntent(goal="Automate a financial strategy"),
                nodes=[
                    PlanNode(
                        id="backtest",
                        objective="backtest the SMA crossover strategy",
                        assigned_claw="backtest",
                        inputs={"prices": str(prices), "position_cap_pct": 10.0},
                        criteria=[
                            {"type": "metric", "name": "trade_count", "op": ">=", "value": 1}
                        ],
                    ),
                    PlanNode(
                        id="train",
                        objective="train a price model",
                        assigned_claw="train_model",
                        inputs={"data": str(train_data), "feature": "x", "target": "y"},
                        depends_on=["backtest"],
                        criteria=[
                            {"type": "metric", "name": "r2", "op": ">=", "value": 0.9}
                        ],
                    ),
                    PlanNode(
                        id="paper-trade",
                        objective="paper-trade the strategy",
                        assigned_claw="paper_trade",
                        inputs={"prices": str(prices), "position_cap_pct": 10.0},
                        depends_on=["train"],
                        criteria=[
                            {"type": "metric", "name": "max_position_pct", "op": "<=", "value": 10}
                        ],
                    ),
                    PlanNode(
                        id="invest",
                        objective="invest real money in the strategy",
                        assigned_claw="invest",
                        inputs={"action": "invest", "amount": 1000.0, "cost": 1000.0},
                        depends_on=["paper-trade"],
                        reversible=False,
                    ),
                ],
            )
            contract = GoalContract(
                objective="Automate a financial strategy",
                success_measures=[
                    SuccessMeasure(name="traded", metric="trade_count", op=">=", value=1)
                ],
                budget=RunBudget(max_replans=0),
            )
            store.save(plan)
            result = runner.run_plan(plan, contract, replan_fn=_no_replanner)

            self.assertEqual(result.plan.status, PlanStatus.AWAITING_APPROVAL)
            self.assertIsNotNone(result.gate_packet_id)
            # The irreversible step never ran: no real money moved.
            self.assertEqual(invest_calls, [])
            packet = gates.get(result.gate_packet_id)
            assert packet is not None
            self.assertEqual(packet.action, "invest")
            self.assertEqual(packet.cost, 1000.0)
            # The paper ledger exists and positions stayed under the cap.
            ledger = root / "runs" / result.run_id / "paper_ledger.json"
            self.assertTrue(ledger.is_file())
            import json

            data = json.loads(ledger.read_text())
            self.assertEqual(data["mode"], "paper")
            self.assertLessEqual(data["max_position_pct"], 10.0)
            # The backtest report exists too.
            self.assertTrue((root / "runs" / result.run_id / "backtest.json").is_file())


class TestLoopM5Ecu(unittest.TestCase):
    def test_ecu_flow_pauses_at_the_flash_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "stock.bin"
            backup.write_bytes(b"STOCK-TUNE")
            digest = hashlib.sha256(b"STOCK-TUNE").hexdigest()
            tune = root / "tune.bin"
            tune.write_bytes(
                b"\x00\x01FUEL-MAP\x00ignition timing table\x00\xff"
            )
            store = ExecutionPlanStateStore(root / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            register_research_claws(registry, http_get=_ecu_http)
            register_sandbox_claws(registry)
            register_ecu_claws(registry)
            flash_calls: list[str] = []

            async def flash_recorder(inputs):
                flash_calls.append("flash")
                return {"status": "success"}

            registry.register("flash_ecu", flash_recorder)
            run_store = RunStore(root / "runs")
            gates = GateStore(root / "state.db")
            runner = LoopRunner(store, registry, run_store=run_store, gate_store=gates)

            plan = ExecutionPlan(
                plan_id="ecu-flow",
                intent=TaskIntent(goal="Debug the ECU and flash a tuned file"),
                nodes=[
                    PlanNode(
                        id="research",
                        objective="research ECU brick risks",
                        assigned_claw="research",
                        inputs={
                            "topic": "ECU brick risk",
                            "questions": ["ECU brick risk flashing"],
                        },
                        criteria=[
                            {"type": "metric", "name": "source_count", "op": ">=", "value": 2}
                        ],
                    ),
                    PlanNode(
                        id="verify-backup",
                        objective="verify the stock backup exists",
                        assigned_claw="verify_backup",
                        inputs={
                            "backup_path": str(backup),
                            "expected_sha256": digest,
                        },
                        depends_on=["research"],
                        criteria=[
                            {"type": "metric", "name": "backup_verified", "op": ">=", "value": 1}
                        ],
                    ),
                    PlanNode(
                        id="can-capture",
                        objective="capture CAN frames (simulated)",
                        assigned_claw="can_capture",
                        inputs={"source": "simulated", "count": 200},
                        depends_on=["verify-backup"],
                        criteria=[
                            {"type": "metric", "name": "frames", "op": ">=", "value": 100}
                        ],
                    ),
                    PlanNode(
                        id="analyse-tune",
                        objective="analyse the tune file in the sandbox",
                        assigned_claw="sandbox_analyze",
                        inputs={"path": str(tune)},
                        depends_on=["can-capture"],
                        criteria=[
                            {"type": "file_exists", "path": "tune.analysis.json"}
                        ],
                    ),
                    PlanNode(
                        id="tune-surface",
                        objective="map the tune surface from the analysis",
                        assigned_claw="tune_surface",
                        inputs={"report": "{{analyse-tune.report_path}}"},
                        depends_on=["analyse-tune"],
                        criteria=[
                            {"type": "metric", "name": "maps_found", "op": ">=", "value": 1}
                        ],
                    ),
                    PlanNode(
                        id="flash",
                        objective="flash the ECU with the tune file",
                        assigned_claw="flash_ecu",
                        inputs={"action": "flash_ecu", "file": "tune.bin", "risk": "brick"},
                        depends_on=["tune-surface", "verify-backup"],
                        reversible=False,
                    ),
                ],
            )
            contract = GoalContract(
                objective="Debug the ECU and flash a tuned file",
                success_measures=[
                    SuccessMeasure(name="frames", metric="frames", op=">=", value=100)
                ],
                budget=RunBudget(max_replans=0),
            )
            store.save(plan)
            result = runner.run_plan(plan, contract, replan_fn=_no_replanner)

            self.assertEqual(result.plan.status, PlanStatus.AWAITING_APPROVAL)
            self.assertIsNotNone(result.gate_packet_id)
            # The flash never ran: no live ECU, no flashing.
            self.assertEqual(flash_calls, [])
            packet = gates.get(result.gate_packet_id)
            assert packet is not None
            self.assertEqual(packet.action, "flash_ecu")
            # CAN frames were captured (simulated) and the tune surface mapped.
            frames = root / "runs" / result.run_id / "can_frames.jsonl"
            self.assertTrue(frames.is_file())
            self.assertEqual(len(frames.read_text().strip().split("\n")), 200)
            import json

            surface = json.loads(
                (root / "runs" / result.run_id / "tune_surface.json").read_text()
            )
            self.assertGreaterEqual(len(surface["maps_found"]), 1)


if __name__ == "__main__":
    unittest.main()
