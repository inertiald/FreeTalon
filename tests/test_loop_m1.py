"""Integration tests for the M1 judge loop: re-plan, retry, escalation.

The M1 done test: a seeded failing task re-plans once from its
measurement, does something demonstrably different, and escalates cleanly
when its budget ends.
"""

from __future__ import annotations

import asyncio
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
    return LoopRunner(store, registry, run_store=run_store), store, registry


def _plan(plan_id: str, nodes: list[PlanNode]) -> ExecutionPlan:
    return ExecutionPlan(plan_id=plan_id, intent=TaskIntent(goal="test"), nodes=nodes)


class TestLoopReplan(unittest.TestCase):
    def test_failing_plan_replans_once_and_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            v1 = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the command exits zero",
            )
            plan = _plan("plan-replan", [v1])
            store.save(plan)

            def replan_fn(brief):
                # A demonstrably different approach: write a file instead.
                self.assertEqual(brief["plan_version"], 1)
                self.assertEqual(len(brief["failed_nodes"]), 1)
                return [PlanNode(
                    id="fix",
                    objective="write a marker file",
                    assigned_claw="bash",
                    inputs={"command": "echo fixed > fixed.txt"},
                    criteria=[{"type": "file_exists", "path": "fixed.txt"}],
                    hypothesis="a marker file proves the step ran",
                    responds_to="node-1",
                    differs_by="writes an artifact instead of relying on exit code",
                )]

            contract = GoalContract(
                objective="re-plan demo", budget=RunBudget(max_replans=1)
            )
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(result.plan_versions, 2)
            self.assertEqual(len(result.attempts), 2)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            # Both plan versions are kept in the run directory.
            run_dir = runner._run_store.run_dir(result.run_id)
            self.assertTrue((run_dir / "plan-v001.json").exists())
            self.assertTrue((run_dir / "plan-v002.json").exists())
            verdicts = [m["verdict"] for m in result.measurements]
            self.assertIn("FAIL", verdicts)
            self.assertIn("PASS", verdicts)

    def test_budget_exhausted_escalates_with_full_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)
            calls = {"n": 0}

            def always_fails(brief):
                calls["n"] += 1
                return [PlanNode(
                    id="try",
                    objective=f"attempt {calls['n']}",
                    assigned_claw="bash",
                    inputs={"command": "exit 1"},
                    criteria=[{"type": "exit_code", "equals": 0}],
                    hypothesis=f"attempt {calls['n']} exits zero",
                    responds_to="node-1",
                    differs_by=f"attempt number {calls['n']}",
                )]

            v1 = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="attempt 0 exits zero",
            )
            plan = _plan("plan-budget", [v1])
            store.save(plan)
            contract = GoalContract(
                objective="budget demo", budget=RunBudget(max_replans=1)
            )
            result = runner.run_plan(plan, contract, replan_fn=always_fails)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertEqual(result.plan_versions, 2)
            self.assertEqual(len(result.attempts), 2)
            self.assertIsNotNone(result.escalation)
            self.assertIn("re-plan budget spent", result.escalation)
            self.assertEqual(calls["n"], 1)  # exactly one re-plan happened
            summary = (runner._run_store.run_dir(result.run_id) / "summary.md").read_text()
            self.assertIn("Escalation", summary)

    def test_duplicate_replan_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)

            def repeat_fn(brief):
                # Same capability and hypothesis, no differs_by: a repeat.
                return [PlanNode(
                    id="same",
                    objective="exit zero again",
                    assigned_claw="bash",
                    inputs={"command": "exit 1"},
                    criteria=[{"type": "exit_code", "equals": 0}],
                    hypothesis="the command exits zero",
                    responds_to="node-1",
                )]

            v1 = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the command exits zero",
            )
            plan = _plan("plan-dup", [v1])
            store.save(plan)
            contract = GoalContract(
                objective="dup demo", budget=RunBudget(max_replans=3)
            )
            result = runner.run_plan(plan, contract, replan_fn=repeat_fn)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertEqual(result.plan_versions, 1)
            self.assertIsNotNone(result.escalation)
            self.assertIn("repeats failed approach", result.escalation)

    def test_timeout_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, registry = _make_runner(tmp)
            calls = {"n": 0}

            async def flaky(inputs):
                calls["n"] += 1
                if calls["n"] == 1:
                    await asyncio.sleep(0.7)  # exceeds the node timeout
                return {"ok": True}

            registry.register("flaky", flaky)
            node = PlanNode(
                id="node-1",
                objective="flaky step",
                assigned_claw="flaky",
                timeout_s=0.5,
                max_retries=1,
                hypothesis="flaky completes in time",
            )
            plan = _plan("plan-retry", [node])
            store.save(plan)
            result = runner.run_plan(plan, GoalContract(objective="retry demo"))
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["retry", "continue"],
            )

    def test_repeated_unmeasurable_escalates(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, store, _ = _make_runner(tmp)

            def replan_fn(brief):
                return [PlanNode(
                    id="check",
                    objective="check for the missing file",
                    assigned_claw="bash",
                    inputs={"command": "true"},
                    criteria=[{"type": "file_exists", "path": "never-created.txt"}],
                    hypothesis="the file appears",
                    responds_to="node-1",
                    differs_by="checks again after a no-op",
                )]

            v1 = PlanNode(
                id="node-1",
                objective="check for the missing file",
                assigned_claw="bash",
                inputs={"command": "true"},
                criteria=[{"type": "file_exists", "path": "never-created.txt"}],
                hypothesis="the file exists",
            )
            plan = _plan("plan-unmeas", [v1])
            store.save(plan)
            contract = GoalContract(
                objective="unmeasurable demo", budget=RunBudget(max_replans=3)
            )
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertIsNotNone(result.escalation)
            self.assertIn("unmeasurable twice", result.escalation)


class TestLoopLocalRepair(unittest.TestCase):
    """Cycle 8: node-local repair inside the loop.

    The /health shape end to end: v1 extends the endpoint's module
    but returns the definition unextended (the re-run #5 failure),
    its own test node catches exactly that ('service_status' not
    found), and the judge's re-plan decision is served FIRST by a
    node-local repair — one focused call that gets the definition
    as it stands on disk plus the assertion and returns only its
    replacement — instead of a whole-task re-plan. Budget
    accounting is the hard invariant: the local attempt rides the
    re-plan decision (replans_used is incremented before either
    path runs), so a local repair and a whole-task re-plan cost
    exactly one unit each.
    """

    GOAL = (
        "Add a GET /health endpoint to the API that returns JSON "
        "reporting the service status, whether Ollama at "
        "localhost:11434 is reachable, and the GPU temperature when "
        "nvidia-smi is available."
    )

    # The module v1 writes: a /health branch that reports only the
    # pre-existing fields — the definition copied back unextended.
    API_SOURCE = (
        "from urllib.parse import urlparse\n"
        "from urllib.request import urlopen\n"
        "\n"
        "\n"
        "class Server:\n"
        "    def start(self):\n"
        "        class Handler:\n"
        "            def do_GET(self):\n"
        "                path = urlparse(self.path).path\n"
        '                if path == "/health":\n'
        '                    return self._send(200, {"ok": True, "status": "ready"})\n'
        '                return self._send(404, {"ok": False})\n'
        "\n"
        "            def _send(self, code, payload):\n"
        "                return code, payload\n"
        "\n"
        "        return Handler\n"
    )

    # The same module with the fields really computed — what a
    # whole-task re-plan would have to produce wholesale, and what
    # the local repair's splice produces from REPAIR_RESPONSE.
    API_EXTENDED = API_SOURCE.replace(
        '                    return self._send(200, {"ok": True, "status": "ready"})\n',
        '                    payload = {"ok": True, "status": "ready"}\n'
        '                    payload["service_status"] = "ready"\n'
        "                    try:\n"
        '                        with urlopen("http://localhost:11434/api/tags", timeout=2) as response:\n'
        '                            payload["ollama_reachable"] = response.status == 200\n'
        "                    except Exception:\n"
        '                        payload["ollama_reachable"] = False\n'
        '                    payload["gpu_temperature"] = None\n'
        "                    return self._send(200, payload)\n",
    )

    # The local repair call's answer: only do_GET, at its file
    # indentation (12 spaces).
    REPAIR_RESPONSE = (
        "            def do_GET(self):\n"
        "                path = urlparse(self.path).path\n"
        '                if path == "/health":\n'
        '                    payload = {"ok": True, "status": "ready"}\n'
        '                    payload["service_status"] = "ready"\n'
        "                    try:\n"
        '                        with urlopen("http://localhost:11434/api/tags", timeout=2) as response:\n'
        '                            payload["ollama_reachable"] = response.status == 200\n'
        "                    except Exception:\n"
        '                        payload["ollama_reachable"] = False\n'
        '                    payload["gpu_temperature"] = None\n'
        "                    return self._send(200, payload)\n"
        '                return self._send(404, {"ok": False})\n'
    )

    # A repair answer that changes the definition (so the splice is
    # no no-op) but still does not compute service_status: the
    # repaired version fails the same test, with the same assertion.
    REPAIR_STILL_BROKEN = (
        "            def do_GET(self):\n"
        "                path = urlparse(self.path).path\n"
        '                if path == "/health":\n'
        '                    return self._send(200, {"ok": True, "status": "ready", "v": 2})\n'
        '                return self._send(404, {"ok": False})\n'
    )

    TEST_SOURCE = (
        "import sys\n"
        'sys.path.insert(0, "REPO_ROOT")\n'
        "from api import Server\n"
        "\n"
        "handler = Server().start()\n"
        "request = handler()\n"
        'request.path = "/health"\n'
        "code, payload = request.do_GET()\n"
        "assert code == 200, code\n"
        'assert "service_status" in payload, (\n'
        '    f"\'service_status\' not found in {payload}"\n'
        ")\n"
        'assert isinstance(payload["ollama_reachable"], bool)\n'
        'assert "gpu_temperature" in payload\n'
        'print("health ok")\n'
    )

    def _v1_nodes(self, root: Path, extra_writer: bool = False) -> list[PlanNode]:
        api_path = str(root / "api.py")
        test_path = str(root / "test_health.py")
        test_source = self.TEST_SOURCE.replace("REPO_ROOT", str(root))
        nodes = [
            PlanNode(
                id="node-1",
                objective="Extend the API module's /health endpoint",
                hypothesis="extending do_GET makes the endpoint report the fields",
                assigned_claw="scaffold",
                inputs={"file_path": api_path, "content": self.API_SOURCE},
                outputs=[api_path],
                acceptance=["The module is extended."],
                criteria=[{"type": "file_exists", "path": api_path}],
            )
        ]
        if extra_writer:
            nodes.append(
                PlanNode(
                    id="node-1b",
                    objective="Rewrite the API module wholesale",
                    hypothesis="a full rewrite also extends the endpoint",
                    assigned_claw="scaffold",
                    inputs={"file_path": api_path, "content": self.API_SOURCE},
                    outputs=[api_path],
                    acceptance=["The module is rewritten."],
                    criteria=[{"type": "file_exists", "path": api_path}],
                    depends_on=["node-1"],
                )
            )
        nodes += [
            PlanNode(
                id="node-2",
                objective="Write the /health test script",
                hypothesis="a script can assert on the endpoint payload",
                assigned_claw="bash",
                inputs={
                    "command": f"cat > {test_path} <<'EOF'\n{test_source}EOF\n"
                },
                outputs=[test_path],
                acceptance=["The test script exists."],
                criteria=[{"type": "file_exists", "path": test_path}],
                depends_on=["node-1b" if extra_writer else "node-1"],
            ),
            PlanNode(
                id="node-3",
                objective="Run the /health test script",
                hypothesis="the endpoint reports service_status",
                assigned_claw="bash",
                inputs={"command": f"python3 {test_path}"},
                outputs=[],
                acceptance=["The test passes."],
                criteria=[{"type": "exit_code", "expected": 0}],
                depends_on=["node-2"],
            ),
        ]
        return nodes

    def _plan(self, root: Path, nodes: list[PlanNode]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-loop-local-repair",
            intent=TaskIntent(
                goal=self.GOAL, constraints={"workspace": str(root)}
            ),
            nodes=nodes,
            metadata={"repo_root": str(root)},
        )

    def _whole_task_nodes(self, root: Path, content: str) -> list[PlanNode]:
        nodes = self._v1_nodes(root)
        nodes[0].inputs = {
            "file_path": str(root / "api.py"),
            "content": content,
        }
        nodes[0].responds_to = "node-3"
        nodes[0].differs_by = "rewrites the module wholesale from the brief"
        return nodes

    def test_local_repair_serves_the_replan_decision(self):
        from unittest import mock

        from freetalon.orchestrator import loop as loop_module
        from freetalon.orchestrator import planner as planner_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            runner, store, _ = _make_runner(tmp)
            plan = self._plan(root, self._v1_nodes(root))
            store.save(plan)
            prompts: list[str] = []

            def fake_call_llm(system_prompt, user_prompt, settings=None):
                prompts.append(user_prompt)
                return self.REPAIR_RESPONSE

            def forbidden_replan(brief):
                raise AssertionError("whole-task re-plan must not run")

            contract = GoalContract(
                objective=self.GOAL, budget=RunBudget(max_replans=2)
            )
            with mock.patch.object(planner_module, "call_llm", fake_call_llm):
                with mock.patch.object(
                    loop_module, "replan_from_brief", forbidden_replan
                ):
                    result = runner.run_plan(plan, contract)
            self.assertEqual(result.goal_verdict, "PASS")
            # One re-plan decision, served locally: v1 + repaired v2.
            self.assertEqual(result.plan_versions, 2)
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            self.assertEqual(len(prompts), 1)
            # The focused call carried the definition as it stood on
            # disk after v1 and the test's own assertion.
            self.assertIn("def do_GET(self):", prompts[0])
            self.assertIn("'service_status' not found", prompts[0])
            # The repaired module is what v2 executed and left behind.
            self.assertEqual(
                (root / "api.py").read_text(encoding="utf-8"), self.API_EXTENDED
            )

    def test_local_plus_whole_task_exhausts_the_budget_exactly(self):
        from unittest import mock

        from freetalon.orchestrator import loop as loop_module
        from freetalon.orchestrator import planner as planner_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            runner, store, _ = _make_runner(tmp)
            plan = self._plan(root, self._v1_nodes(root))
            store.save(plan)
            llm_calls = {"n": 0}
            replan_calls = {"n": 0}

            def fake_call_llm(system_prompt, user_prompt, settings=None):
                llm_calls["n"] += 1
                if llm_calls["n"] == 1:
                    # A real splice, but the substance is still
                    # missing: v2 fails the identical assertion.
                    return self.REPAIR_STILL_BROKEN
                # The second decision's local attempt cannot even
                # extract a definition, so the loop falls back to
                # the whole-task re-plan within the same unit.
                return "no definition here"

            def stub_replan(brief):
                replan_calls["n"] += 1
                return self._whole_task_nodes(root, self.API_SOURCE)

            contract = GoalContract(
                objective=self.GOAL, budget=RunBudget(max_replans=2)
            )
            with mock.patch.object(planner_module, "call_llm", fake_call_llm):
                with mock.patch.object(
                    loop_module, "replan_from_brief", stub_replan
                ):
                    result = runner.run_plan(plan, contract)
            # v1 + local v2 + whole-task v3, then the budget is
            # spent: exactly the two re-plans two whole-task
            # re-plans would have bought.
            self.assertEqual(result.goal_verdict, "FAIL")
            self.assertEqual(result.plan_versions, 3)
            self.assertEqual(llm_calls["n"], 2)
            self.assertEqual(replan_calls["n"], 1)
            self.assertIsNotNone(result.escalation)
            self.assertIn("re-plan budget spent", result.escalation)

    def test_ambiguous_culprit_falls_back_to_whole_task_replan(self):
        from unittest import mock

        from freetalon.orchestrator import loop as loop_module
        from freetalon.orchestrator import planner as planner_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            runner, store, _ = _make_runner(tmp)
            plan = self._plan(root, self._v1_nodes(root, extra_writer=True))
            store.save(plan)
            llm_calls = {"n": 0}
            replan_calls = {"n": 0}

            def fake_call_llm(system_prompt, user_prompt, settings=None):
                llm_calls["n"] += 1
                return self.REPAIR_RESPONSE

            def stub_replan(brief):
                replan_calls["n"] += 1
                return self._whole_task_nodes(root, self.API_EXTENDED)

            contract = GoalContract(
                objective=self.GOAL, budget=RunBudget(max_replans=2)
            )
            with mock.patch.object(planner_module, "call_llm", fake_call_llm):
                with mock.patch.object(
                    loop_module, "replan_from_brief", stub_replan
                ):
                    result = runner.run_plan(plan, contract)
            # Two completed writers of api.py: no confident culprit,
            # so the local seam never calls the model and the
            # whole-task re-plan serves the decision, as before.
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(result.plan_versions, 2)
            self.assertEqual(llm_calls["n"], 0)
            self.assertEqual(replan_calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
