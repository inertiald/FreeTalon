"""Tests for the Track B substance critic (GAN pairing).

The critic is a second model wired as an internal MEASUREMENT:
after a plan version is accepted and before it executes, each
write node's content is reviewed for substance only. A hollow
verdict becomes a measured failure on that node in the existing
judge machinery — the version does not execute, the re-plan brief
carries the critic's reason as the diagnostics, and the re-plan
proceeds under the existing budget. The critic client is stubbed
throughout (patching freetalon.orchestrator.critic.call_llm, the
formatter tests' pattern): no live model is ever called here.

Fixtures: (i) the real plan-7885a4ef9d99 Ellipsis shape with a
stubbed hollow verdict is not executed and its brief carries the
critic's reason; (ii) the filled-slots variant (real probe code)
with a substantive verdict executes normally; (iii) critic OFF is
byte-identical behavior — the same Ellipsis plan executes exactly
as it did before the critic existed; (iv) a critic that raises
degrades to no-critic, the version executes, and the degradation
is audit-logged; (v) a critic-triggered re-plan consumes exactly
one unit of the existing re-plan budget.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from freetalon.audit import AuditLogger
from freetalon.orchestrator.critic import (
    CriticSettings,
    critic_settings_from_env,
    critic_verdict_schema,
    review_write_nodes,
)
from freetalon.orchestrator.intake import LLMBackendError, LLMSettings
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import goal_key_for
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws

HEALTH_GOAL = (
    "Add a GET /health endpoint to FreeTalon's API that returns JSON "
    "reporting the service status, whether Ollama at localhost:11434 "
    "is reachable, and the GPU temperature when nvidia-smi is "
    "available. Add unit tests for the endpoint, keep the full test "
    "suite green, and verify the endpoint live."
)

HOLLOW_REASON = (
    "the /health fields are assigned the placeholder ... instead of "
    "being computed"
)

# The fixture serving module as it stands before the plan runs:
# the /health branch answers only {"ok", "status"}.
MODULE_SOURCE = (
    '"""Fixture serving module for critic checks."""\n'
    "import subprocess\n"
    "import urllib.request\n"
    "from urllib.parse import urlparse\n"
    "\n"
    "\n"
    "def alpha():\n"
    "    return 1\n"
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
    "        return Handler\n"
    "\n"
    "    def stop(self):\n"
    "        return None\n"
)

# The live plan-7885a4ef9d99 step-1 shape as a full-module write:
# the three new fields assigned the bare Ellipsis.
ELLIPSIS_MODULE = MODULE_SOURCE.replace(
    '                    return self._send(200, {"ok": True, "status": "ready"})\n',
    '                    payload = {"ok": True, "status": "ready"}\n'
    '                    payload["service_status"] = ...\n'
    '                    payload["ollama_reachable"] = ...\n'
    '                    payload["gpu_temperature"] = ...\n'
    "                    return self._send(200, payload)\n",
)

# The cycle-6 filled-slots variant: the same write with the fields
# computed by real probes, attempt-and-fallback.
FILLED_MODULE = MODULE_SOURCE.replace(
    '                    return self._send(200, {"ok": True, "status": "ready"})\n',
    '                    payload = {"ok": True, "status": "ready"}\n'
    '                    payload["service_status"] = "ready"\n'
    "                    try:\n"
    "                        with urllib.request.urlopen(\n"
    '                            "http://localhost:11434/api/tags", timeout=2\n'
    "                        ) as response:\n"
    '                            payload["ollama_reachable"] = response.status == 200\n'
    "                    except Exception:\n"
    '                        payload["ollama_reachable"] = False\n'
    "                    try:\n"
    "                        out = subprocess.run(\n"
    '                            ["nvidia-smi", "--query-gpu=temperature.gpu",\n'
    '                             "--format=csv,noheader"],\n'
    "                            capture_output=True, text=True, timeout=5,\n"
    "                        )\n"
    '                        payload["gpu_temperature"] = float(out.stdout.strip())\n'
    "                    except Exception:\n"
    '                        payload["gpu_temperature"] = None\n'
    "                    return self._send(200, payload)\n",
)

TEST_SOURCE = (
    '"""Tests for the /health endpoint."""\n'
    "import unittest\n"
    "\n"
    "\n"
    "class HealthTests(unittest.TestCase):\n"
    "    def test_fields_present(self):\n"
    "        self.assertTrue(True)\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    unittest.main()\n"
)

_CRITIC_ENV_KEYS = (
    "FREETALON_CRITIC_ENABLED",
    "FREETALON_CRITIC_OLLAMA_BASE_URL",
    "FREETALON_CRITIC_OLLAMA_MODEL",
)


def _critic_env(**overrides: str) -> dict[str, str]:
    env = {key: "" for key in _CRITIC_ENV_KEYS}
    env.update(overrides)
    return env


def _stub_client(system_prompt: str, user_prompt: str, settings=None) -> str:
    """A stubbed critic: hollow exactly when the content carries the
    Ellipsis assignments, substantive otherwise — keyed on the same
    hollowness marker the live defect carries."""
    if "= ..." in user_prompt:
        return json.dumps({"verdict": "hollow", "reason": HOLLOW_REASON})
    return json.dumps(
        {"verdict": "substantive", "reason": "the content computes the fields"}
    )


class TestCriticSettingsFromEnv(unittest.TestCase):
    def test_unconfigured_is_none(self) -> None:
        with patch.dict(os.environ, _critic_env(), clear=False):
            self.assertIsNone(critic_settings_from_env())

    def test_enabled_alone_configures_with_fallbacks(self) -> None:
        with patch.dict(
            os.environ, _critic_env(FREETALON_CRITIC_ENABLED="1"), clear=False
        ):
            settings = critic_settings_from_env()
        self.assertEqual(settings, CriticSettings(base_url=None, model=None))

    def test_endpoint_vars_configure_and_enable(self) -> None:
        env = _critic_env(
            FREETALON_CRITIC_OLLAMA_BASE_URL="http://100.87.140.12:11434",
            FREETALON_CRITIC_OLLAMA_MODEL="llama3:latest",
        )
        with patch.dict(os.environ, env, clear=False):
            settings = critic_settings_from_env()
        self.assertEqual(
            settings,
            CriticSettings(
                base_url="http://100.87.140.12:11434", model="llama3:latest"
            ),
        )

    def test_verdict_schema_shape(self) -> None:
        schema = critic_verdict_schema()
        self.assertEqual(schema["required"], ["verdict", "reason"])
        self.assertEqual(
            schema["properties"]["verdict"]["enum"], ["substantive", "hollow"]
        )


class TestCriticReview(unittest.TestCase):
    def _plan(self, tmp: str) -> ExecutionPlan:
        target = str(Path(tmp) / "api.py")
        Path(target).write_text(MODULE_SOURCE, encoding="utf-8")
        nodes = [
            PlanNode(
                id="write-ellipsis",
                objective="Extend the endpoint",
                assigned_claw="scaffold",
                inputs={"file_path": target, "content": ELLIPSIS_MODULE},
                criteria=[{"type": "file_exists", "path": target}],
            ),
            PlanNode(
                id="write-note",
                objective="Write a note file",
                assigned_claw="file_writer",
                inputs={"file_path": str(Path(tmp) / "note.txt"), "content": "hello"},
            ),
            PlanNode(
                id="run-tests",
                objective="Run the tests",
                assigned_claw="bash",
                inputs={"command": "python3 -m unittest"},
            ),
            PlanNode(
                id="write-empty",
                objective="A write with nothing to judge",
                assigned_claw="scaffold",
                inputs={"file_path": str(Path(tmp) / "empty.py")},
            ),
        ]
        return ExecutionPlan(
            plan_id="plan-critic-unit",
            intent=TaskIntent(goal=HEALTH_GOAL),
            nodes=nodes,
        )

    def test_reviews_only_write_nodes_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp)
            seen: list[dict] = []

            def stub(system_prompt, user_prompt, settings=None):
                seen.append({"prompt": user_prompt, "settings": settings})
                return _stub_client(system_prompt, user_prompt, settings)

            with patch("freetalon.orchestrator.critic.call_llm", side_effect=stub):
                review = review_write_nodes(
                    plan,
                    HEALTH_GOAL,
                    CriticSettings(
                        base_url="http://100.87.140.12:11434", model="llama3:latest"
                    ),
                    settings=LLMSettings(base_url="http://planner:11434", model="gemma"),
                )
            self.assertIsNone(review.degraded)
            self.assertEqual(
                [v.node_id for v in review.verdicts], ["write-ellipsis", "write-note"]
            )
            hollow = review.hollow
            self.assertEqual(len(hollow), 1)
            self.assertEqual(hollow[0].node_id, "write-ellipsis")
            self.assertEqual(hollow[0].reason, HOLLOW_REASON)
            # Settings derivation: the critic endpoint wins, the
            # planner's model is the fallback only where the critic
            # leaves a field unset — here both are set by the critic.
            used = seen[0]["settings"]
            self.assertEqual(used.base_url, "http://100.87.140.12:11434")
            self.assertEqual(used.model, "llama3:latest")
            self.assertEqual(used.temperature, 0.0)
            self.assertEqual(used.num_ctx, 12288)
            self.assertEqual(used.format_schema, critic_verdict_schema())
            # The prompt carries the goal, the content, and the
            # target's current on-disk source (it exists here).
            prompt = seen[0]["prompt"]
            self.assertIn(HEALTH_GOAL, prompt)
            self.assertIn('payload["gpu_temperature"] = ...', prompt)
            self.assertIn("as it stands on disk now", prompt)

    def test_settings_fallback_to_planner_when_critic_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp)
            seen: list = []

            def stub(system_prompt, user_prompt, settings=None):
                seen.append(settings)
                return json.dumps({"verdict": "substantive", "reason": "ok"})

            with patch("freetalon.orchestrator.critic.call_llm", side_effect=stub):
                review_write_nodes(
                    plan,
                    HEALTH_GOAL,
                    CriticSettings(),
                    settings=LLMSettings(
                        base_url="http://planner:11434", model="planner-model"
                    ),
                )
            self.assertEqual(seen[0].base_url, "http://planner:11434")
            self.assertEqual(seen[0].model, "planner-model")

    def test_hollow_without_reason_gets_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp)
            with patch(
                "freetalon.orchestrator.critic.call_llm",
                return_value=json.dumps({"verdict": "hollow", "reason": ""}),
            ):
                review = review_write_nodes(
                    plan, HEALTH_GOAL, CriticSettings(), settings=LLMSettings()
                )
            self.assertTrue(review.hollow)
            self.assertTrue(all(v.reason for v in review.hollow))

    def test_backend_error_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp)
            with patch(
                "freetalon.orchestrator.critic.call_llm",
                side_effect=LLMBackendError("connection refused"),
            ):
                review = review_write_nodes(
                    plan, HEALTH_GOAL, CriticSettings(), settings=LLMSettings()
                )
            self.assertEqual(review.verdicts, [])
            self.assertIsNotNone(review.degraded)
            self.assertIn("connection refused", review.degraded)

    def test_unparseable_verdict_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp)
            with patch(
                "freetalon.orchestrator.critic.call_llm",
                return_value="the content looks fine to me",
            ):
                review = review_write_nodes(
                    plan, HEALTH_GOAL, CriticSettings(), settings=LLMSettings()
                )
            self.assertEqual(review.verdicts, [])
            self.assertIsNotNone(review.degraded)

    def test_no_write_nodes_makes_no_calls(self) -> None:
        plan = ExecutionPlan(
            plan_id="plan-critic-none",
            intent=TaskIntent(goal=HEALTH_GOAL),
            nodes=[
                PlanNode(
                    id="run",
                    objective="Run something",
                    assigned_claw="bash",
                    inputs={"command": "true"},
                )
            ],
        )
        with patch("freetalon.orchestrator.critic.call_llm") as mock:
            review = review_write_nodes(
                plan, HEALTH_GOAL, CriticSettings(), settings=LLMSettings()
            )
        self.assertEqual(review.verdicts, [])
        self.assertIsNone(review.degraded)
        mock.assert_not_called()


class TestCriticLoop(unittest.TestCase):
    """Loop-level fixtures (i)-(v), in the live plan's shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.module_path = str(self.tmp_root / "api.py")
        self.test_path = str(self.tmp_root / "test_health.py")
        Path(self.module_path).write_text(MODULE_SOURCE, encoding="utf-8")
        self.assertNotEqual(ELLIPSIS_MODULE, MODULE_SOURCE)
        self.assertNotEqual(FILLED_MODULE, MODULE_SOURCE)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_runner(self, critic_settings=None, audit_logger=None):
        store = ExecutionPlanStateStore(db_path=self.tmp_root / "state.db")
        registry = ToolRegistry(strict=True)
        register_default_claws(registry)
        run_store = RunStore(self.tmp_root / "runs")
        runner = LoopRunner(
            store,
            registry,
            run_store=run_store,
            audit_logger=audit_logger,
            critic_settings=critic_settings,
        )
        return runner, store

    def _health_plan(self, module_content: str) -> ExecutionPlan:
        nodes = [
            PlanNode(
                id="node-1",
                objective="Extend the /health endpoint in the serving module",
                assigned_claw="scaffold",
                inputs={"file_path": self.module_path, "content": module_content},
                outputs=[self.module_path],
                criteria=[{"type": "file_exists", "path": self.module_path}],
                hypothesis="the endpoint reports status, reachability, temperature",
            ),
            PlanNode(
                id="node-2",
                objective="Add unit tests for the endpoint",
                depends_on=["node-1"],
                assigned_claw="scaffold",
                inputs={"file_path": self.test_path, "content": TEST_SOURCE},
                outputs=[self.test_path],
                criteria=[{"type": "file_exists", "path": self.test_path}],
                hypothesis="tests cover the endpoint fields",
            ),
            PlanNode(
                id="node-3",
                objective="Run the endpoint tests",
                depends_on=["node-2"],
                assigned_claw="bash",
                inputs={
                    "command": (
                        f"python3 -m py_compile {self.module_path} {self.test_path}"
                    )
                },
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the written code parses and runs",
            ),
            PlanNode(
                id="node-4",
                objective="Keep the full test suite green",
                depends_on=["node-3"],
                assigned_claw="bash",
                inputs={"command": "python3 -c \"import sys; sys.exit(0)\""},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the full suite passes",
            ),
            PlanNode(
                id="node-5",
                objective="Verify /health live",
                depends_on=["node-4"],
                assigned_claw="bash",
                inputs={"command": "python3 -c \"import sys; sys.exit(0)\""},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the endpoint answers live",
            ),
        ]
        return ExecutionPlan(
            plan_id="plan-7885a4ef9d99",
            intent=TaskIntent(goal=HEALTH_GOAL),
            nodes=nodes,
        )

    # (i) Hollow verdict: not executed; the brief carries the reason.
    def test_hollow_plan_not_executed_brief_carries_reason(self) -> None:
        runner, store = self._make_runner(critic_settings=CriticSettings())
        plan = self._health_plan(ELLIPSIS_MODULE)
        store.save(plan)
        briefs: list[dict] = []

        def replan_fn(brief):
            briefs.append(brief)
            return [
                PlanNode(
                    id="fix",
                    objective="write a marker file instead",
                    assigned_claw="bash",
                    inputs={"command": "echo fixed > fixed.txt"},
                    criteria=[{"type": "file_exists", "path": "fixed.txt"}],
                    hypothesis="a marker file proves the step ran",
                    responds_to="node-1",
                    differs_by="writes a marker instead of the hollow module",
                )
            ]

        contract = GoalContract(
            objective=HEALTH_GOAL, budget=RunBudget(max_replans=2)
        )
        with patch(
            "freetalon.orchestrator.critic.call_llm", side_effect=_stub_client
        ) as mock:
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)

        self.assertEqual(result.goal_verdict, "PASS")
        self.assertEqual(result.plan_versions, 2)
        self.assertEqual(
            [d["decision"] for d in result.decision_history], ["replan", "continue"]
        )
        # The hollow version never executed: the module on disk is
        # byte-identical to what it was before the run.
        self.assertEqual(
            Path(self.module_path).read_text(encoding="utf-8"), MODULE_SOURCE
        )
        self.assertFalse(Path(self.test_path).exists())
        # The critic measurement is in the run's measurements.
        critic_measurements = [
            m
            for m in result.measurements
            if m.get("node_id") == "node-1" and m.get("plan_version") == 1
        ]
        self.assertEqual(len(critic_measurements), 1)
        self.assertEqual(critic_measurements[0]["verdict"], "FAIL")
        self.assertEqual(
            critic_measurements[0]["results"][0]["criterion"]["type"],
            "critic_review",
        )
        # The judge brief carries the critic's reason as the
        # failure's diagnostics (cycle 5) and in the measurement.
        self.assertEqual(len(briefs), 1)
        failed = briefs[0]["failed_nodes"]
        self.assertEqual([f["id"] for f in failed], ["node-1"])
        self.assertIn("critic review (hollow)", failed[0]["diagnostics"])
        self.assertIn("placeholder", failed[0]["diagnostics"])
        self.assertIn(
            "critic review (hollow)",
            failed[0]["measurement"]["results"][0]["detail"],
        )
        # Both write nodes were reviewed in v1; v2 has none.
        self.assertEqual(mock.call_count, 2)
        # The critic record is planning-stage in experiment memory:
        # the content-blind failed-approach signatures stay clean.
        self.assertEqual(
            runner._memory_store.failed_approaches(
                goal_key_for(HEALTH_GOAL), "general"
            ),
            set(),
        )

    # (ii) Substantive verdict: executes normally.
    def test_substantive_plan_executes_normally(self) -> None:
        runner, store = self._make_runner(critic_settings=CriticSettings())
        plan = self._health_plan(FILLED_MODULE)
        store.save(plan)
        contract = GoalContract(
            objective=HEALTH_GOAL, budget=RunBudget(max_replans=2)
        )
        with patch(
            "freetalon.orchestrator.critic.call_llm", side_effect=_stub_client
        ) as mock:
            result = runner.run_plan(plan, contract)
        self.assertEqual(result.goal_verdict, "PASS")
        self.assertEqual(result.plan_versions, 1)
        self.assertEqual(
            [d["decision"] for d in result.decision_history], ["continue"]
        )
        self.assertEqual(
            Path(self.module_path).read_text(encoding="utf-8"), FILLED_MODULE
        )
        self.assertEqual(mock.call_count, 2)

    # (iii) Critic OFF: the same Ellipsis plan executes exactly as
    # it did before the critic existed, and the critic is never
    # consulted.
    def test_critic_off_executes_as_before(self) -> None:
        runner, store = self._make_runner()
        plan = self._health_plan(ELLIPSIS_MODULE)
        store.save(plan)
        contract = GoalContract(
            objective=HEALTH_GOAL, budget=RunBudget(max_replans=2)
        )
        with patch.dict(os.environ, _critic_env(), clear=False):
            with patch(
                "freetalon.orchestrator.critic.call_llm",
                side_effect=AssertionError("critic must not be called"),
            ) as mock:
                result = runner.run_plan(plan, contract)
        self.assertEqual(result.goal_verdict, "PASS")
        self.assertEqual(result.plan_versions, 1)
        self.assertEqual(
            Path(self.module_path).read_text(encoding="utf-8"), ELLIPSIS_MODULE
        )
        mock.assert_not_called()

    # (iv) Critic raising: degrades to no-critic, executes, and the
    # degradation is recorded in the run's audit log.
    def test_critic_error_degrades_and_executes(self) -> None:
        audit_path = self.tmp_root / "audit.jsonl"
        runner, store = self._make_runner(
            critic_settings=CriticSettings(),
            audit_logger=AuditLogger(audit_path),
        )
        plan = self._health_plan(FILLED_MODULE)
        store.save(plan)
        contract = GoalContract(
            objective=HEALTH_GOAL, budget=RunBudget(max_replans=2)
        )
        with patch(
            "freetalon.orchestrator.critic.call_llm",
            side_effect=LLMBackendError("connection refused"),
        ):
            result = runner.run_plan(plan, contract)
        self.assertEqual(result.goal_verdict, "PASS")
        self.assertEqual(result.plan_versions, 1)
        self.assertEqual(
            Path(self.module_path).read_text(encoding="utf-8"), FILLED_MODULE
        )
        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertIn("loop.critic.degraded", audit_text)
        self.assertIn("connection refused", audit_text)

    # (v) Budget accounting: a critic-triggered re-plan consumes
    # exactly one unit; the second hollow version escalates on the
    # spent budget, exactly as an executed failure would.
    def test_critic_replan_consumes_one_budget_unit(self) -> None:
        runner, store = self._make_runner(critic_settings=CriticSettings())
        plan = self._health_plan(ELLIPSIS_MODULE)
        store.save(plan)
        calls = {"n": 0}

        def replan_fn(brief):
            calls["n"] += 1
            return [
                PlanNode(
                    id="node-1",
                    objective="Extend the /health endpoint in the serving module",
                    assigned_claw="scaffold",
                    inputs={
                        "file_path": self.module_path,
                        "content": ELLIPSIS_MODULE,
                    },
                    outputs=[self.module_path],
                    criteria=[{"type": "file_exists", "path": self.module_path}],
                    hypothesis="the endpoint reports status, reachability, temperature",
                    responds_to="node-1",
                    differs_by="a stated second attempt at the same write",
                )
            ]

        contract = GoalContract(
            objective=HEALTH_GOAL, budget=RunBudget(max_replans=1)
        )
        with patch(
            "freetalon.orchestrator.critic.call_llm", side_effect=_stub_client
        ):
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.plan_versions, 2)
        self.assertEqual(
            [d["decision"] for d in result.decision_history],
            ["replan", "escalate"],
        )
        self.assertIn("re-plan budget spent", result.escalation or "")
        self.assertEqual(
            Path(self.module_path).read_text(encoding="utf-8"), MODULE_SOURCE
        )


if __name__ == "__main__":
    unittest.main()
