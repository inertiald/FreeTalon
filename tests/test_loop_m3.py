"""M3 done test: the ECU flow researches brick risks and analyses a tune
file in the sandbox — without touching a live ECU (fake HTTP, no network).
"""

import json
import tempfile
import unittest
from pathlib import Path

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
from tests.test_research import _DDG_HTML, _FORUM_HTML, _fake_http

# The fake tune file contains text that would be dangerous if executed;
# the test asserts it is only ever analysed.
_TUNE_BYTES = b"\x00\x01TUNE-MAP\x00rm -rf /\x00\xff"


def _no_replanner(brief):
    """Re-planning is out of scope for the M3 flow test."""
    raise RuntimeError("re-planner disabled in this test")


def _ecu_http(url: str, timeout: float = 20.0):
    if "tune.example.com" in url:
        return 200, {"content-type": "application/octet-stream"}, _TUNE_BYTES
    return _fake_http(url, timeout)


class TestLoopM3(unittest.TestCase):
    def test_ecu_flow_researches_and_analyses_without_a_live_ecu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExecutionPlanStateStore(root / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            register_research_claws(registry, http_get=_ecu_http)
            register_sandbox_claws(registry)
            run_store = RunStore(root / "runs")

            plan = ExecutionPlan(
                plan_id="ecu-flow",
                intent=TaskIntent(
                    goal="Research ECU brick risks and analyse a tune file in the sandbox"
                ),
                nodes=[
                    PlanNode(
                        id="research",
                        objective="research ECU brick risks from public sources",
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
                        id="fetch-tune",
                        objective="fetch the tune file into the sandbox",
                        assigned_claw="fetch_dataset",
                        inputs={
                            "url": "https://tune.example.com/tune.bin",
                            "dest": "sandbox/tune.bin",
                        },
                        depends_on=["research"],
                        criteria=[
                            {"type": "metric", "name": "bytes", "op": ">=", "value": 1}
                        ],
                    ),
                    PlanNode(
                        id="analyse-tune",
                        objective="analyse the tune file in the sandbox without running it",
                        assigned_claw="sandbox_analyze",
                        inputs={"path": "sandbox/tune.bin"},
                        depends_on=["fetch-tune"],
                        criteria=[
                            {"type": "file_exists", "path": "tune.analysis.json"}
                        ],
                    ),
                ],
            )
            contract = GoalContract(
                objective=plan.intent.goal,
                success_measures=[
                    SuccessMeasure(
                        name="tune analysed", metric="strings_count", op=">=", value=1
                    )
                ],
                budget=RunBudget(max_replans=0),
            )
            store.save(plan)
            runner = LoopRunner(store, registry, run_store=run_store)
            result = runner.run_plan(
                plan, contract, replan_fn=_no_replanner
            )

            self.assertEqual(result.plan.status, PlanStatus.COMPLETED)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(len(result.measurements), 3)
            self.assertTrue(all(m["verdict"] == "PASS" for m in result.measurements))

            # The research artifact exists with sourced, dated material.
            artifact = root / "runs" / result.run_id / "research" / "ecu-brick-risk.json"
            self.assertTrue(artifact.is_file())
            data = json.loads(artifact.read_text())
            self.assertGreaterEqual(len(data["sources"]), 2)
            self.assertIn("https://forum.example.com/ecu-brick",
                          {s["url"] for s in data["sources"]})

            # The tune file was analysed, never executed: its dangerous
            # string appears in the report, and no marker file exists.
            report_path = root / "runs" / result.run_id / "nodes" / "analyse-tune" / "tune.analysis.json"
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text())
            self.assertIn("TUNE-MAP", report["strings"])
            self.assertEqual(report["size_bytes"], len(_TUNE_BYTES))
            self.assertFalse((root / "pwned").exists())
            self.assertFalse((root / "runs" / result.run_id / "pwned").exists())

            # Provenance sidecar exists for the fetched tune file.
            sidecar = root / "runs" / result.run_id / "sandbox" / "tune.bin.provenance.json"
            self.assertTrue(sidecar.is_file())
            prov = json.loads(sidecar.read_text())
            self.assertEqual(prov["url"], "https://tune.example.com/tune.bin")


if __name__ == "__main__":
    unittest.main()
