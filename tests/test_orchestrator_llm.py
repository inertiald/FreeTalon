"""Tests for orchestrator intake, prompts, and planner flows."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import (
    LLMResponseError,
    LLMSettings,
    intake_request,
    parse_task_intent_response,
)
from freetalon.orchestrator.models import TaskIntent
from freetalon.orchestrator.planner import (
    build_execution_plan,
    normalize_plan_nodes,
    parse_planner_response,
    plan_task_intent,
)
from freetalon.orchestrator.prompts import INTAKE_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT


class TestPrompts(unittest.TestCase):
    """Prompt contracts should remain strict and centralized."""

    def test_intake_prompt_requires_json_only(self) -> None:
        self.assertIn("Return exactly one JSON object", INTAKE_SYSTEM_PROMPT)
        self.assertIn('"goal"', INTAKE_SYSTEM_PROMPT)
        self.assertIn("Do not wrap the JSON in markdown", INTAKE_SYSTEM_PROMPT)

    def test_intake_prompt_has_no_full_example_to_echo(self) -> None:
        # Greedy decoding copies a full few-shot JSON example verbatim;
        # the schema enforces the shape, so no example JSON may appear.
        self.assertNotIn("Summarize directory structure", INTAKE_SYSTEM_PROMPT)
        self.assertIn("Keep the goal concrete", INTAKE_SYSTEM_PROMPT)

    def test_planner_prompt_requires_dag_constraints(self) -> None:
        self.assertIn("dependency-safe directed acyclic graph", PLANNER_SYSTEM_PROMPT)
        self.assertIn('"nodes"', PLANNER_SYSTEM_PROMPT)
        self.assertIn("Do not create cycles", PLANNER_SYSTEM_PROMPT)


class TestIntake(unittest.TestCase):
    """Intake should parse and validate strict TaskIntent output."""

    def test_parse_task_intent_response_accepts_fenced_json(self) -> None:
        response = """```json
{"goal":"Ship a dashboard","project_type":"web_app","capabilities":["ui"],"constraints":{"budget":"low"},"missing_inputs":[]}
```"""
        intent = parse_task_intent_response(response)
        self.assertEqual(intent.goal, "Ship a dashboard")
        self.assertEqual(intent.project_type, "web_app")

    def test_parse_task_intent_response_rejects_non_json(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "not valid JSON"):
            parse_task_intent_response("Sure, I can help with that.")

    def test_llm_settings_from_env_supports_both_backends(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FREETALON_LLM_BACKEND": "ollama",
                "FREETALON_OLLAMA_BASE_URL": "http://ollama.local:11434",
                "FREETALON_OLLAMA_MODEL": "mistral",
            },
            clear=False,
        ):
            ollama_settings = LLMSettings.from_env()
        self.assertEqual(ollama_settings.backend, "ollama")
        self.assertEqual(ollama_settings.base_url, "http://ollama.local:11434")
        self.assertEqual(ollama_settings.model, "mistral")

        with patch.dict(
            os.environ,
            {
                "FREETALON_LLM_BACKEND": "openai_compatible",
                "FREETALON_OPENAI_BASE_URL": "https://example.test/v1",
                "FREETALON_OPENAI_MODEL": "gpt-test",
                "FREETALON_OPENAI_API_KEY": "secret",
            },
            clear=False,
        ):
            openai_settings = LLMSettings.from_env()
        self.assertEqual(openai_settings.backend, "openai_compatible")
        self.assertEqual(openai_settings.base_url, "https://example.test/v1")
        self.assertEqual(openai_settings.model, "gpt-test")
        self.assertEqual(openai_settings.api_key, "secret")

    def test_llm_settings_preserves_explicit_empty_openai_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FREETALON_LLM_BACKEND": "openai_compatible",
                "FREETALON_OPENAI_API_KEY": "",
            },
            clear=False,
        ):
            settings = LLMSettings.from_env()
        self.assertEqual(settings.api_key, "")

    def test_intake_request_uses_structured_llm_output(self) -> None:
        response = """
        {"goal":"Automate CI triage","project_type":"devops","capabilities":["logs"],"constraints":{},"missing_inputs":["repo name"]}
        """
        settings = LLMSettings(backend="ollama", base_url="http://localhost:11434", model="test")
        with patch("freetalon.orchestrator.intake.call_llm", return_value=response) as call_mock:
            intent = intake_request("Please automate my CI triage workflow.", settings=settings)
        self.assertEqual(intent.goal, "Automate CI triage")
        call_mock.assert_called_once()
        user_prompt = call_mock.call_args[0][1]
        self.assertTrue(user_prompt.startswith("User Request: "))
        self.assertIn("Please automate my CI triage workflow.", user_prompt)


class TestPlanner(unittest.TestCase):
    """Planner should enforce valid dependency semantics."""

    def setUp(self) -> None:
        self.intent = TaskIntent(goal="Deploy a service", project_type="ops")

    def test_normalize_plan_nodes_rejects_missing_dependency(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "unknown node"):
            normalize_plan_nodes(
                [
                    {
                        "id": "step-1",
                        "objective": "Do the work",
                        "depends_on": ["missing-step"],
                    }
                ]
            )

    def test_normalize_plan_nodes_rejects_cycles(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {
                        "id": "a",
                        "objective": "First",
                        "depends_on": ["b"],
                    },
                    {
                        "id": "b",
                        "objective": "Second",
                        "depends_on": ["a"],
                    },
                ]
            )

    def test_parse_planner_response_normalizes_ids_and_order(self) -> None:
        nodes, metadata = parse_planner_response(
            """
            {
              "nodes": [
                {"id": "deploy", "objective": "Deploy service", "depends_on": ["build"],
                 "assigned_claw": "bash", "inputs": {"command": "docker run app"},
                 "criteria": [{"type": "exit_code", "equals": 0}]},
                {"id": "build", "objective": "Build image", "depends_on": [],
                 "assigned_claw": "bash", "inputs": {"command": "docker build -t app ."},
                 "criteria": [{"type": "exit_code", "equals": 0}]}
              ],
              "metadata": {"strategy": "serial"}
            }
            """
        )
        self.assertEqual([node.id for node in nodes], ["node-1", "node-2"])
        self.assertEqual(nodes[0].objective, "Build image")
        self.assertEqual(nodes[1].depends_on, ["node-1"])
        self.assertEqual(metadata, {"strategy": "serial"})

    def test_build_execution_plan_returns_valid_plan(self) -> None:
        plan = build_execution_plan(
            self.intent,
            [
                {"id": "prepare", "objective": "Prepare release", "depends_on": []},
                {"id": "deploy", "objective": "Deploy release", "depends_on": ["prepare"]},
            ],
            {"owner": "planner"},
            plan_id="plan-fixed",
        )
        self.assertEqual(plan.plan_id, "plan-fixed")
        self.assertEqual([node.id for node in plan.nodes], ["node-1", "node-2"])
        self.assertEqual(plan.nodes[1].depends_on, ["node-1"])
        self.assertEqual(plan.metadata["owner"], "planner")

    def test_plan_task_intent_builds_execution_plan_from_llm_output(self) -> None:
        response = """
        {
          "nodes": [
            {"id": "verify", "objective": "Verify inputs", "depends_on": [],
             "assigned_claw": "bash", "inputs": {"command": "ls inputs"},
             "criteria": [{"type": "exit_code", "equals": 0}]},
            {"id": "execute", "objective": "Execute rollout", "depends_on": ["verify"],
             "assigned_claw": "bash", "inputs": {"command": "deploy.sh"},
             "criteria": [{"type": "exit_code", "equals": 0}]}
          ],
          "metadata": {"planner": "llm"}
        }
        """
        settings = LLMSettings(backend="ollama", base_url="http://localhost:11434", model="test")
        with patch("freetalon.orchestrator.planner.call_llm", return_value=response) as call_mock:
            plan = plan_task_intent(self.intent, settings=settings, plan_id="plan-123")
        self.assertEqual(plan.plan_id, "plan-123")
        self.assertEqual(plan.nodes[1].depends_on, ["node-1"])
        self.assertEqual(plan.metadata, {"planner": "llm"})
        call_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestPlannerHardening(unittest.TestCase):
    """Planner hardening: claw allowlist, criteria, repair retry."""

    def setUp(self) -> None:
        self.intent = TaskIntent(goal="Write and verify a script", project_type="script")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _scaffold_node(self, **overrides: object) -> dict:
        node: dict = {
            "id": "step_1",
            "objective": "Write the script",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": "/home/seeker/freetalon-workspace/fib.py",
                "content": "print(1)\n",
            },
            "outputs": ["/home/seeker/freetalon-workspace/fib.py"],
            "criteria": [
                {
                    "type": "file_exists",
                    "path": "/home/seeker/freetalon-workspace/fib.py",
                }
            ],
        }
        node.update(overrides)
        return node

    def test_parse_rejects_unknown_claw(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "unknown claw"):
            parse_planner_response(
                self._dag([self._scaffold_node(assigned_claw="script")])
            )

    def test_parse_rejects_node_without_criteria(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "no 'criteria'"):
            parse_planner_response(self._dag([self._scaffold_node(criteria=[])]))

    def test_parse_rejects_criterion_with_foreign_path(self) -> None:
        node = self._scaffold_node(
            criteria=[{"type": "file_exists", "path": "/tmp/other.py"}]
        )
        with self.assertRaisesRegex(LLMResponseError, "not a file this node"):
            parse_planner_response(self._dag([node]))

    def test_parse_accepts_criterion_path_inside_bash_command(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(
                [
                    {
                        "id": "run",
                        "objective": "Run the script",
                        "depends_on": [],
                        "assigned_claw": "bash",
                        "inputs": {
                            "command": "python3 /home/seeker/freetalon-workspace/fib.py"
                        },
                        "criteria": [
                            {
                                "type": "file_exists",
                                "path": "/home/seeker/freetalon-workspace/fib.py",
                            }
                        ],
                    }
                ]
            )
        )
        self.assertEqual(nodes[0].assigned_claw, "bash")

    def test_parse_rejects_bad_metric_criterion(self) -> None:
        node = self._scaffold_node(
            criteria=[{"type": "metric", "name": "r2", "op": ">=", "value": "high"}]
        )
        with self.assertRaisesRegex(LLMResponseError, "numeric 'value'"):
            parse_planner_response(self._dag([node]))

    def test_parse_rejects_unknown_criterion_type(self) -> None:
        node = self._scaffold_node(criteria=[{"type": "vibes", "value": 1}])
        with self.assertRaisesRegex(LLMResponseError, "unknown criterion type"):
            parse_planner_response(self._dag([node]))

    def test_plan_repairs_unknown_claw_then_succeeds(self) -> None:
        bad = self._dag([self._scaffold_node(assigned_claw="script")])
        good = self._dag([self._scaffold_node()])
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        self.assertEqual(call_mock.call_count, 2)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("unknown claw", repair_prompt)

    def test_plan_gives_up_after_repair_budget(self) -> None:
        bad = self._dag([self._scaffold_node(assigned_claw="script")])
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=bad
        ) as call_mock:
            with self.assertRaisesRegex(LLMResponseError, "unknown claw"):
                plan_task_intent(self.intent, settings=self.settings)
        # Initial attempt + 2 repair retries, then the last error is raised.
        self.assertEqual(call_mock.call_count, 3)

    def test_replan_repairs_echoed_brief_then_succeeds(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        echo = '{"goal": {"objective": "x"}, "failed_nodes": [], "instruction": "fix it"}'
        good = self._dag(
            [
                self._scaffold_node(
                    responds_to="node-1",
                    differs_by="use the registered scaffold claw",
                )
            ]
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[echo, good]
        ) as call_mock:
            nodes = replan_from_brief({"goal": {}, "failed_nodes": []})
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(call_mock.call_count, 2)

    def test_replan_raises_after_exhausted_repairs(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        echo = '{"goal": {"objective": "x"}, "failed_nodes": []}'
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=echo
        ) as call_mock:
            with self.assertRaisesRegex(LLMResponseError, "'nodes' array"):
                replan_from_brief({"goal": {}, "failed_nodes": []})
        self.assertEqual(call_mock.call_count, 3)

    def test_prompts_state_allowlist_criteria_and_anti_echo(self) -> None:
        from freetalon.orchestrator.prompts import (
            PLANNER_SYSTEM_PROMPT,
            REPLAN_SYSTEM_PROMPT,
        )

        self.assertIn('There is no "script" claw', PLANNER_SYSTEM_PROMPT)
        self.assertIn("MEASURABLE CRITERIA", PLANNER_SYSTEM_PROMPT)
        self.assertIn("COVER EVERY STEP", PLANNER_SYSTEM_PROMPT)
        self.assertIn("echo the brief", REPLAN_SYSTEM_PROMPT)
        self.assertIn("Never invent a claw name", REPLAN_SYSTEM_PROMPT)

    def test_known_claw_allowlist_matches_registered_claws(self) -> None:
        from freetalon.orchestrator.ecu import register_ecu_claws
        from freetalon.orchestrator.finance import register_finance_claws
        from freetalon.orchestrator.research import register_research_claws
        from freetalon.orchestrator.sandbox import register_sandbox_claws
        from freetalon.orchestrator.tool_registry import (
            KNOWN_CLAW_NAMES,
            ToolRegistry,
            register_default_claws,
        )

        self.assertNotIn("script", KNOWN_CLAW_NAMES)
        self.assertIn("scaffold", KNOWN_CLAW_NAMES)
        self.assertIn("bash", KNOWN_CLAW_NAMES)
        registry = ToolRegistry(strict=True)
        register_default_claws(registry)
        register_research_claws(registry)
        register_sandbox_claws(registry)
        register_finance_claws(registry)
        register_ecu_claws(registry)
        self.assertEqual(set(registry.capabilities()), set(KNOWN_CLAW_NAMES))


class TestPlannerGroundingFixes(unittest.TestCase):
    """Fixes for the failed /health goal (run-20261116T013052Z-917367).

    (1) Repo grounding: plans must extend existing endpoints/modules,
    never scaffold duplicates. (2) Bash commands must parse and use known
    service ports. (3) Metric criteria only on claws that report them.
    """

    def setUp(self) -> None:
        self.intent = TaskIntent(goal="Extend the health endpoint", project_type="api")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _bash_node(self, command: str, **overrides: object) -> dict:
        node: dict = {
            "id": "run",
            "objective": "Run a command",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": command},
            "outputs": ["result"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        node.update(overrides)
        return node

    # --- Fix 1: repo grounding ---

    def test_repo_index_finds_health_route_and_api_port(self) -> None:
        from freetalon.orchestrator.repo_index import build_repo_index

        index = build_repo_index()
        self.assertEqual(index.routes.get("/health"), "freetalon/api.py")
        self.assertEqual(index.api_port, 8765)

    def test_repo_index_context_names_health_and_ports(self) -> None:
        from freetalon.orchestrator.repo_index import (
            build_repo_index,
            format_repo_context,
        )

        context = format_repo_context(build_repo_index())
        self.assertIn("/health", context)
        self.assertIn("8765", context)
        self.assertIn("11434", context)

    def test_parse_rejects_scaffold_duplicating_health_endpoint(self) -> None:
        # The exact node-1 from the failed run: a new api/health.py while
        # GET /health already exists in freetalon/api.py.
        node = {
            "id": "step_1",
            "objective": "ScaffoldAPIendpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": "/home/seeker/freetalon-workspace/api/health.py",
                "content": "#TODO:ImplementGET/healthendpoint",
            },
            "outputs": ["/home/seeker/freetalon-workspace/api/health.py"],
            "criteria": [
                {
                    "type": "file_exists",
                    "path": "/home/seeker/freetalon-workspace/api/health.py",
                }
            ],
        }
        with self.assertRaisesRegex(LLMResponseError, "duplicates.* /health"):
            parse_planner_response(self._dag([node]))

    def test_parse_rejects_duplicate_in_synthetic_repo(self) -> None:
        import tempfile

        from freetalon.orchestrator.repo_index import build_repo_index

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "freetalon"
            package.mkdir()
            (package / "api.py").write_text(
                'if path == "/health":\n    pass\n', encoding="utf-8"
            )
            (package / "config.py").write_text(
                "port: int = Field(default=9999)\n", encoding="utf-8"
            )
            index = build_repo_index(tmp)
            self.assertEqual(index.routes.get("/health"), "freetalon/api.py")
            self.assertEqual(index.api_port, 9999)
            dup = {
                "id": "step_1",
                "objective": "Scaffold health",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": str(Path(tmp) / "workspace" / "api" / "health.py")},
                "outputs": [str(Path(tmp) / "workspace" / "api" / "health.py")],
                "criteria": [
                    {
                        "type": "file_exists",
                        "path": str(Path(tmp) / "workspace" / "api" / "health.py"),
                    }
                ],
            }
            with self.assertRaisesRegex(LLMResponseError, "duplicates"):
                parse_planner_response(self._dag([dup]), repo_root=tmp)
            fresh = dict(dup)
            fresh["inputs"] = {"file_path": str(Path(tmp) / "workspace" / "analysis.py")}
            fresh["outputs"] = [str(Path(tmp) / "workspace" / "analysis.py")]
            fresh["criteria"] = [
                {
                    "type": "file_exists",
                    "path": str(Path(tmp) / "workspace" / "analysis.py"),
                }
            ]
            nodes, _ = parse_planner_response(self._dag([fresh]), repo_root=tmp)
            self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_parse_accepts_plan_extending_existing_api_module(self) -> None:
        api_path = str(Path(__file__).resolve().parent.parent / "freetalon" / "api.py")
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": api_path, "content": "# extended\n"},
            "outputs": [api_path],
            "criteria": [{"type": "file_exists", "path": api_path}],
        }
        nodes, _ = parse_planner_response(self._dag([node]))
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    # --- Fix 2: command / port validation ---

    def test_parse_rejects_glued_test_command(self) -> None:
        # Exact node-2 command from the failed run (missing spaces).
        node = self._bash_node(
            "python3-munittestdiscover-s/home/seeker/freetalon-workspace/api/health.py"
        )
        with self.assertRaisesRegex(LLMResponseError, "glued together"):
            parse_planner_response(self._dag([node]))

    def test_parse_rejects_glued_curl_command(self) -> None:
        # Exact node-3 command from the failed run.
        node = self._bash_node("curl-shttp://localhost:8000/health")
        with self.assertRaisesRegex(LLMResponseError, "glued together"):
            parse_planner_response(self._dag([node]))

    def test_parse_rejects_wrong_api_port_naming_correct_port(self) -> None:
        node = self._bash_node("curl -s http://localhost:8000/health")
        with self.assertRaisesRegex(LLMResponseError, "8765.*not 8000"):
            parse_planner_response(self._dag([node]))

    def test_parse_accepts_known_service_ports(self) -> None:
        for command in (
            "curl -s http://localhost:8765/health",
            "curl -s http://localhost:11434/api/tags",
        ):
            nodes, _ = parse_planner_response(self._dag([self._bash_node(command)]))
            self.assertEqual(nodes[0].assigned_claw, "bash")

    def test_parse_rejects_unbalanced_quotes(self) -> None:
        node = self._bash_node('echo "unclosed')
        with self.assertRaisesRegex(LLMResponseError, "does not parse"):
            parse_planner_response(self._dag([node]))

    def test_known_service_ports_match_config_and_intake(self) -> None:
        from freetalon.config import HiveConfig
        from freetalon.orchestrator import planner as planner_module
        from freetalon.orchestrator.intake import _OLLAMA_BASE_URL

        self.assertEqual(HiveConfig().port, 8765)
        self.assertIn(8765, planner_module._KNOWN_SERVICE_PORTS)
        self.assertIn(":11434", _OLLAMA_BASE_URL)
        self.assertIn(11434, planner_module._KNOWN_SERVICE_PORTS)

    # --- Fix 3: metric / claw capability map ---

    def test_parse_rejects_metric_on_bash(self) -> None:
        # Exact node-2 criterion from the failed run: bash never reports metrics.
        node = self._bash_node(
            "python3 -m unittest discover -s tests",
            criteria=[
                {"type": "exit_code", "equals": 0},
                {"type": "metric", "name": "test_count", "op": ">=", "value": 1},
            ],
        )
        with self.assertRaisesRegex(
            LLMResponseError, "test_count.*not reported by claw 'bash'"
        ):
            parse_planner_response(self._dag([node]))

    def test_parse_accepts_metric_reported_by_claw(self) -> None:
        llm_node = {
            "id": "step_1",
            "objective": "Summarize",
            "depends_on": [],
            "assigned_claw": "llm_synthesis",
            "inputs": {"instruction": "Sum it up", "context": "text"},
            "outputs": ["summary"],
            "criteria": [
                {"type": "metric", "name": "summary_chars", "op": ">=", "value": 1}
            ],
        }
        nodes, _ = parse_planner_response(self._dag([llm_node]))
        self.assertEqual(nodes[0].assigned_claw, "llm_synthesis")
        backtest_node = {
            "id": "step_1",
            "objective": "Backtest",
            "depends_on": [],
            "assigned_claw": "backtest",
            "inputs": {"data": "prices.csv"},
            "outputs": ["backtest.json"],
            "criteria": [
                {"type": "metric", "name": "trade_count", "op": ">=", "value": 1}
            ],
        }
        nodes, _ = parse_planner_response(self._dag([backtest_node]))
        self.assertEqual(nodes[0].assigned_claw, "backtest")

    def test_metric_map_covers_all_known_claws(self) -> None:
        from freetalon.orchestrator.tool_registry import CLA_METRICS, KNOWN_CLAW_NAMES

        self.assertEqual(set(CLA_METRICS), set(KNOWN_CLAW_NAMES))

    # --- Prompt wiring ---

    def test_plan_prompt_includes_repo_context(self) -> None:
        # The mocked plan must engage the /health route (anti-fake rule):
        # scaffold the existing API module itself, not an unrelated file.
        api_path = str(
            Path(__file__).resolve().parent.parent / "freetalon" / "api.py"
        )
        node = TestPlannerHardening()._scaffold_node(
            inputs={"file_path": api_path, "content": "# extended health"},
            outputs=[api_path],
            criteria=[{"type": "file_exists", "path": api_path}],
        )
        response = TestPlannerHardening._dag([node])
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=response
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        user_prompt = call_mock.call_args[0][1]
        self.assertIn("REPO CONTEXT", user_prompt)
        self.assertIn("/health", user_prompt)
        self.assertIn("8765", user_prompt)

    def test_prompts_state_grounding_and_ports(self) -> None:
        from freetalon.orchestrator.prompts import (
            PLANNER_SYSTEM_PROMPT,
            REPLAN_SYSTEM_PROMPT,
        )

        self.assertIn("GROUND IN THE REPO", PLANNER_SYSTEM_PROMPT)
        self.assertIn("8765", PLANNER_SYSTEM_PROMPT)
        self.assertIn("report NO metrics", PLANNER_SYSTEM_PROMPT)
        self.assertIn("8765", REPLAN_SYSTEM_PROMPT)


class TestPlannerCollectsAllErrors(unittest.TestCase):
    """Validation must collect ALL errors into one rejection.

    Previously parse_planner_response raised on the first failing check, so
    a plan with several independent errors burned one repair retry per
    error. Now every node is checked against every rule and the single
    rejection lists them all, so one repair round can fix everything.
    """

    def setUp(self) -> None:
        self.intent = TaskIntent(goal="Extend the health endpoint", project_type="api")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    @staticmethod
    def _failed_health_plan_nodes() -> list[dict]:
        """The three nodes of the real failed /health plan, verbatim.

        Embedded (not read from the run directory) so the test stays
        hermetic in CI: the run directory is not part of the repo.
        """
        return [
            {
                "id": "node-1",
                "objective": "ScaffoldAPIendpoint",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "/home/seeker/freetalon-workspace/api/health.py",
                    "content": "#TODO:ImplementGET/healthendpoint",
                },
                "outputs": ["/home/seeker/freetalon-workspace/api/health.py"],
                "criteria": [
                    {
                        "type": "file_exists",
                        "path": "/home/seeker/freetalon-workspace/api/health.py",
                    }
                ],
            },
            {
                "id": "node-2",
                "objective": "RunAPIendpointtests",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": "python3-munittestdiscover-s/home/seeker/freetalon-workspace/api/health.py"
                },
                "outputs": ["testresults"],
                "criteria": [
                    {"type": "exit_code", "equals": 0},
                    {"type": "metric", "name": "test_count", "op": ">=", "value": 1},
                ],
            },
            {
                "id": "node-3",
                "objective": "Verifyendpointlive",
                "depends_on": ["node-2"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl-shttp://localhost:8000/health"},
                "outputs": ["verificationresults"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    def _rejection(self, nodes: list[dict]) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._dag(nodes))
        return str(caught.exception)

    def test_failed_health_plan_surfaces_all_five_errors_at_once(self) -> None:
        message = self._rejection(self._failed_health_plan_nodes())
        self.assertIn("5 errors", message)
        self.assertIn("fix ALL of them", message)
        # node-1: scaffold duplicates the existing GET /health endpoint.
        self.assertIn("node-1", message)
        self.assertIn("duplicates", message)
        self.assertIn("/health", message)
        # node-2: glued test command AND a metric bash cannot report.
        self.assertIn("node-2", message)
        self.assertIn("glued together", message)
        self.assertIn("test_count", message)
        # node-3: glued curl AND the wrong API port (8765 named).
        self.assertIn("node-3", message)
        self.assertIn("8765", message)
        self.assertIn("not 8000", message)

    def test_single_error_rejection_names_the_one_error(self) -> None:
        node = TestPlannerHardening()._scaffold_node(assigned_claw="script")
        message = self._rejection([node])
        self.assertIn("1 error", message)
        self.assertNotIn("2 errors", message)
        self.assertIn("unknown claw", message)

    def test_valid_plan_still_passes(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(
                [
                    {
                        "id": "build",
                        "objective": "Build image",
                        "depends_on": [],
                        "assigned_claw": "bash",
                        "inputs": {"command": "docker build -t app ."},
                        "criteria": [{"type": "exit_code", "equals": 0}],
                    },
                    {
                        "id": "deploy",
                        "objective": "Deploy service",
                        "depends_on": ["build"],
                        "assigned_claw": "bash",
                        "inputs": {"command": "docker run app"},
                        "criteria": [{"type": "exit_code", "equals": 0}],
                    },
                ]
            )
        )
        self.assertEqual([node.id for node in nodes], ["node-1", "node-2"])

    def test_multiple_bad_criteria_on_one_node_all_named(self) -> None:
        node = TestPlannerHardening()._scaffold_node(
            criteria=[
                {"type": "vibes", "value": 1},
                {"type": "metric", "name": "r2", "op": ">=", "value": "high"},
            ]
        )
        message = self._rejection([node])
        self.assertIn("2 errors", message)
        self.assertIn("unknown criterion type", message)
        self.assertIn("numeric 'value'", message)

    def test_repair_fixes_multi_error_plan_in_one_round(self) -> None:
        api_path = str(Path(__file__).resolve().parent.parent / "freetalon" / "api.py")
        fixed = self._dag(
            [
                {
                    "id": "node-1",
                    "objective": "Extend the health endpoint",
                    "depends_on": [],
                    "assigned_claw": "scaffold",
                    "inputs": {"file_path": api_path, "content": "# extended\n"},
                    "outputs": [api_path],
                    "criteria": [{"type": "file_exists", "path": api_path}],
                },
                {
                    "id": "node-2",
                    "objective": "Run the tests",
                    "depends_on": ["node-1"],
                    "assigned_claw": "bash",
                    "inputs": {"command": "python3 -m unittest discover -s tests"},
                    "outputs": ["testresults"],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                },
                {
                    "id": "node-3",
                    "objective": "Verify the endpoint live",
                    "depends_on": ["node-2"],
                    "assigned_claw": "bash",
                    "inputs": {"command": "curl -s http://localhost:8765/health"},
                    "outputs": ["verificationresults"],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                },
            ]
        )
        bad = self._dag(self._failed_health_plan_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, fixed]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # One repair round fixed all five errors — not five rounds.
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        for marker in ("duplicates", "glued together", "test_count", "8765"):
            self.assertIn(marker, repair_prompt)


class TestPlannerEchoFix(unittest.TestCase):
    """Planner echo fix: no copyable JSON at the end of the prompt, and
    the repair re-prompt never pastes the failed output back verbatim.

    Regression case: the /health retry (2026-11-16), where Gemma returned
    the TaskIntent JSON verbatim 3/3 times instead of a plan DAG, because
    plan_task_intent embedded the intent JSON at the end of the prompt and
    the repair loop pasted the echo back in. At temperature 0 the model
    copies the JSON it last saw; the fix removes the copyable JSON and
    the reinforcement.
    """

    def setUp(self) -> None:
        self.intent = TaskIntent(
            goal="Add a GET /health endpoint to FreeTalon's API that returns JSON reporting the service status, whether Ollama at localhost:11434 is reachable, and the GPU temperature when nvidia-smi is available. Add unit tests for the endpoint, keep the full test suite green, and verify the endpoint live.",
            project_type="bash",
            capabilities=["scaffold", "bash", "llm_synthesis"],
            constraints={"workspace": "/home/seeker/freetalon-workspace"},
            missing_inputs=[],
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self.api_path = str(
            Path(__file__).resolve().parent.parent / "freetalon" / "api.py"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _good_node(self) -> dict:
        return {
            "id": "step_1",
            "objective": "Extend the health endpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.api_path, "content": "# extended"},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }

    def test_plan_prompt_contains_no_intent_json(self) -> None:
        response = self._dag([self._good_node()])
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=response
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        user_prompt = call_mock.call_args[0][1]
        # The raw intent JSON must not appear anywhere in the prompt:
        # it is the text a temp-0 model copies verbatim.
        self.assertNotIn(self.intent.model_dump_json(indent=2), user_prompt)
        self.assertNotIn('"project_type": "bash"', user_prompt)
        self.assertNotIn('"capabilities":', user_prompt)
        # The same facts are present as prose, and the prompt ends by
        # demanding a NEW plan DAG.
        self.assertIn("Goal: Add a GET /health endpoint", user_prompt)
        self.assertIn(
            "Capabilities to use: scaffold, bash, llm_synthesis", user_prompt
        )
        self.assertIn("Workspace: /home/seeker/freetalon-workspace", user_prompt)
        self.assertTrue(user_prompt.rstrip().endswith("JSON Output:"))

    def test_plan_repairs_echoed_intent_then_succeeds(self) -> None:
        # The exact failure from the /health retry: the model returns the
        # intent JSON verbatim instead of a plan DAG. The repair round
        # must break the echo and produce a valid plan in one round.
        echo = self.intent.model_dump_json(indent=2)
        good = self._dag([self._good_node()])
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[echo, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        self.assertEqual(call_mock.call_count, 2)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        # The failed output is described, never pasted back: the verbatim
        # intent JSON must not appear in the repair prompt.
        self.assertNotIn(echo, repair_prompt)
        self.assertNotIn('"project_type": "bash"', repair_prompt)
        self.assertIn("no 'nodes' array", repair_prompt)

    def test_plan_gives_up_after_echo_budget(self) -> None:
        echo = self.intent.model_dump_json(indent=2)
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=echo
        ) as call_mock:
            with self.assertRaisesRegex(LLMResponseError, "'nodes' array"):
                plan_task_intent(self.intent, settings=self.settings)
        # Initial attempt + 2 repair retries, then the last error is raised.
        self.assertEqual(call_mock.call_count, 3)

    def test_describe_failed_output_names_the_problem_without_quoting_it(
        self,
    ) -> None:
        from freetalon.orchestrator.planner import _describe_failed_output

        echo = self.intent.model_dump_json(indent=2)
        description = _describe_failed_output(echo)
        self.assertIn("no 'nodes' array", description)
        self.assertIn("goal", description)
        self.assertNotIn("Add a GET /health endpoint", description)
        self.assertNotIn('"project_type"', description)

        bad_dag = self._dag(
            [{"id": "step_1", "assigned_claw": "script", "depends_on": []}]
        )
        self.assertIn("failed validation", _describe_failed_output(bad_dag))
        self.assertNotIn("script", _describe_failed_output(bad_dag))

        self.assertIn("not valid JSON", _describe_failed_output("hello there"))

    def test_planner_prompt_states_anti_echo_rule(self) -> None:
        self.assertIn(
            "Do NOT repeat, quote, or echo the task description",
            PLANNER_SYSTEM_PROMPT,
        )
        self.assertNotIn("TaskIntent JSON object", PLANNER_SYSTEM_PROMPT)

    def test_replan_prompt_ends_with_new_plan_framing(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        good = self._dag(
            [
                {
                    "id": "step_1",
                    "objective": "Run the tests",
                    "depends_on": [],
                    "assigned_claw": "bash",
                    "inputs": {"command": "python3 -m unittest discover -s tests"},
                    "criteria": [{"type": "exit_code", "equals": 0}],
                    "responds_to": "node-1",
                    "differs_by": "run the suite to confirm the failure",
                }
            ]
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            replan_from_brief({"goal": {}, "failed_nodes": []})
        user_prompt = call_mock.call_args[0][1]
        self.assertTrue(user_prompt.rstrip().endswith("JSON Output:"))
        self.assertIn("Do NOT repeat, quote, or echo the brief", user_prompt)


class TestPlannerFakeAndMemoryFix(unittest.TestCase):
    """Anti-fake validation and prose re-plan briefs (retry #2).

    Retry #2 (run-20261117T054715Z-698173) produced a real plan DAG first
    try, but the plan faked the deliverable: node-1 echoed a hardcoded
    api.json instead of extending freetalon/api.py, node-2 echoed an
    assert sentence into test.sh, and the replan then echoed an
    experiment-memory record verbatim as the nodes array. Two fixes:
    (1) plan validation rejects static stand-in files and plans that
    claim route work without touching the route module; (2) the re-plan
    brief renders memory as prose, never as JSON.
    """

    HEALTH_GOAL = (
        "Add a GET /health endpoint to FreeTalon's API that returns JSON "
        "reporting the service status, whether Ollama at localhost:11434 is "
        "reachable, and the GPU temperature when nvidia-smi is available. "
        "Add unit tests for the endpoint, keep the full test suite green, "
        "and verify the endpoint live."
    )

    # The experiment-memory record the retry-#2 replan echoed verbatim as
    # its nodes array (run-20261116T013052Z-917367, node-1), in the shape
    # ExperimentStore._row_to_record returns.
    _MEMORY_RECORD = {
        "run_id": "run-20261116T013052Z-917367",
        "goal_key": "07ab30f88fe6662b",
        "domain": "bash",
        "node_id": "node-1",
        "ts": "2026-11-16T01:30:52.241891+00:00",
        "hypothesis": "ScaffoldAPIendpoint",
        "approach": {
            "capability": "scaffold",
            "key_params": {
                "file_path": "/home/seeker/freetalon-workspace/api/health.py"
            },
            "data_window": "",
        },
        "measurement": {
            "verdict": "FAIL",
            "exit_code": 127,
            "duration_s": 0.5,
            "results": [
                {"passed": False, "measured": True, "detail": "exit_code 127 != 0"}
            ],
        },
        "decision": "ESCALATE",
        "lesson": (
            "scaffold (ScaffoldAPIendpoint): verdict FAIL — failed criterion: "
            "exit_code 127 != 0; exit 127; 0.5s; decision ESCALATE."
        ),
    }

    def setUp(self) -> None:
        self.intent = TaskIntent(
            goal=self.HEALTH_GOAL,
            project_type="bash",
            capabilities=["scaffold", "bash", "llm_synthesis"],
            constraints={"workspace": "/home/seeker/freetalon-workspace"},
            missing_inputs=[],
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self.api_path = str(
            Path(__file__).resolve().parent.parent / "freetalon" / "api.py"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    @staticmethod
    def _fake_health_plan_nodes() -> list[dict]:
        """The three nodes of the retry-#2 plan, verbatim from plan-v001.json.

        Embedded (not read from the run directory) so the test stays
        hermetic in CI: the run directory is not part of the repo.
        """
        ws = "/home/seeker/freetalon-workspace"
        return [
            {
                "id": "node-1",
                "objective": "Add health endpoint to FreeTalon's API",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": "echo '{\"health\": \"OK\", \"ollama_reachable\": true, \"gpu_temp\": 60}' > /home/seeker/freetalon-workspace/api.json"
                },
                "outputs": [f"{ws}/api.json"],
                "acceptance": ["File exists"],
                "error": None,
                "criteria": [
                    {"type": "file_exists", "path": f"{ws}/api.json"},
                ],
                "reversible": True,
                "timeout_s": 300.0,
                "max_retries": 0,
                "hypothesis": "",
                "responds_to": "",
                "differs_by": "",
                "measurement": {},
                "duration_s": None,
                "exit_code": None,
            },
            {
                "id": "node-2",
                "objective": "Add unit tests for the endpoint",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": "echo 'assert api.json contains \"health\": \"OK\"' > /home/seeker/freetalon-workspace/test.sh && chmod +x /home/seeker/freetalon-workspace/test.sh"
                },
                "outputs": [f"{ws}/test.sh"],
                "acceptance": ["File exists"],
                "error": None,
                "criteria": [
                    {"type": "file_exists", "path": f"{ws}/test.sh"},
                ],
                "reversible": True,
                "timeout_s": 300.0,
                "max_retries": 0,
                "hypothesis": "",
                "responds_to": "",
                "differs_by": "",
                "measurement": {},
                "duration_s": None,
                "exit_code": None,
            },
            {
                "id": "node-3",
                "objective": "Verify the endpoint live",
                "depends_on": ["node-2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": f"{ws}/test.sh"},
                "outputs": ["verification"],
                "acceptance": ["Exit code 0"],
                "error": None,
                "criteria": [{"type": "exit_code", "equals": 0}],
                "reversible": True,
                "timeout_s": 300.0,
                "max_retries": 0,
                "hypothesis": "",
                "responds_to": "",
                "differs_by": "",
                "measurement": {},
                "duration_s": None,
                "exit_code": None,
            },
        ]

    def _valid_health_plan_nodes(self) -> list[dict]:
        """A real /health plan: extend the module, run tests, curl live."""
        return [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "content": "# extended health"},
                "outputs": [self.api_path],
                "criteria": [{"type": "file_exists", "path": self.api_path}],
            },
            {
                "id": "step_2",
                "objective": "Run the test suite",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": "python3 -m unittest discover -s tests"},
                "outputs": ["test results"],
                "criteria": [{"type": "tests_pass", "suite": "tests"}],
            },
            {
                "id": "step_3",
                "objective": "Verify the endpoint live",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    def _good_delta_node(self) -> dict:
        return {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.api_path, "content": "# extended health"},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
            "responds_to": "node-1",
            "differs_by": "edit the existing module instead of a new file",
        }

    def _brief(self, memory: list[dict] | None = None) -> dict:
        failed_measurement = {
            "verdict": "FAIL",
            "exit_code": 127,
            "duration_s": 0.002,
            "results": [
                {"passed": False, "measured": True, "detail": "exit_code 127 != 0"}
            ],
        }
        return {
            "goal": {
                "objective": self.HEALTH_GOAL,
                "success_measures": [
                    {"name": "endpoint live", "metric": "health_ok", "op": "==", "value": 1}
                ],
                "budget": {"max_wall_clock_s": 3600.0, "max_replans": 2},
                "risk_class": "read_only",
                "stop_conditions": [],
                "irreversible_actions": [],
            },
            "plan_id": "plan-7885a4ef9d99",
            "plan_version": 1,
            "failed_nodes": [
                {
                    "id": "node-3",
                    "objective": "Verify the endpoint live",
                    "hypothesis": "Verify the endpoint live",
                    "assigned_claw": "bash",
                    "measurement": failed_measurement,
                    "error": None,
                }
            ],
            "measurements": [
                dict({"node_id": "node-3"}, **failed_measurement),
            ],
            "memory": memory if memory is not None else [self._MEMORY_RECORD],
            "remaining_budget": {
                "replans": 1,
                "wall_clock_s": 3000.0,
                "max_attempts_per_hypothesis": 3,
                "max_nodes": 10,
                "max_money": 0.0,
            },
            "instruction": (
                "Produce a delta plan: name which failed hypothesis each new "
                "node responds to (responds_to) and what it does differently "
                "(differs_by). Do not repeat a recorded failed approach "
                "without a stated reason. Shrink scope before growing it."
            ),
        }

    # --- Weakness 1: anti-fake validation ---

    def test_parse_rejects_verbatim_fake_health_plan(self) -> None:
        message = ""
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._fake_health_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("3 errors", message)
        self.assertIn("fix ALL of them", message)
        # node-1: a static JSON file that asserts the answer.
        self.assertIn("node-1", message)
        self.assertIn("static file", message)
        # node-2: a static assert string posing as a test.
        self.assertIn("node-2", message)
        # No node reads, modifies, or calls the real /health route.
        self.assertIn("freetalon/api.py", message)
        self.assertIn("/health", message)

    def test_parse_accepts_valid_health_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._valid_health_plan_nodes()),
            goal_text=self.HEALTH_GOAL,
        )
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_static_rule_ignores_plain_echo(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Write a scratch file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "echo hello > /tmp/out.txt"},
            "outputs": ["/tmp/out.txt"],
            "criteria": [{"type": "file_exists", "path": "/tmp/out.txt"}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text="Write a scratch file"
        )
        self.assertEqual(len(nodes), 1)

    def test_static_rule_flags_json_literal_without_route(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Write a JSON file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "echo '{\"ok\": true}' > /tmp/x.json"},
            "outputs": ["/tmp/x.json"],
            "criteria": [{"type": "file_exists", "path": "/tmp/x.json"}],
        }
        with self.assertRaisesRegex(LLMResponseError, "static file"):
            parse_planner_response(
                self._dag([node]), goal_text="Write a JSON file"
            )

    def test_route_rule_only_fires_when_goal_names_a_route(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Run the test suite",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "python3 -m unittest discover -s tests"},
            "outputs": ["test results"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        # No route named: the same plan is fine.
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text="Run the test suite"
        )
        self.assertEqual(len(nodes), 1)
        # The /health goal names the route, and this plan never touches it.
        with self.assertRaisesRegex(LLMResponseError, "freetalon/api.py"):
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )

    def test_plan_repairs_fake_plan_then_succeeds(self) -> None:
        fake = self._dag(self._fake_health_plan_nodes())
        good = self._dag(self._valid_health_plan_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[fake, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(len(plan.nodes), 3)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        self.assertEqual(call_mock.call_count, 2)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("static file", repair_prompt)
        self.assertIn("freetalon/api.py", repair_prompt)

    # --- Weakness 2: prose re-plan brief ---

    def test_replan_brief_renders_memory_as_prose(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        good = self._dag([self._good_delta_node()])
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            replan_from_brief(self._brief())
        user_prompt = call_mock.call_args[0][1]
        # No verbatim memory JSON anywhere in the prompt: no record keys.
        self.assertNotIn('"run_id"', user_prompt)
        self.assertNotIn('"goal_key"', user_prompt)
        self.assertNotIn('"node_id"', user_prompt)
        self.assertNotIn('"approach"', user_prompt)
        self.assertNotIn('"measurement"', user_prompt)
        self.assertNotIn('"lesson"', user_prompt)
        # The same facts are present as prose, measurements exact.
        self.assertIn("ScaffoldAPIendpoint", user_prompt)
        self.assertIn("exit 127", user_prompt)
        self.assertIn("verdict FAIL", user_prompt)
        self.assertIn("failed criterion: exit_code 127 != 0", user_prompt)
        self.assertIn("Decision: ESCALATE", user_prompt)
        self.assertTrue(user_prompt.rstrip().endswith("JSON Output:"))

    def test_format_brief_keeps_measurements_exact(self) -> None:
        from freetalon.orchestrator.planner import _format_brief_for_prompt

        text = _format_brief_for_prompt(self._brief())
        self.assertIn("exit 127", text)
        self.assertIn("verdict FAIL", text)
        self.assertIn("failed criterion: exit_code 127 != 0", text)
        self.assertIn("Success measure: endpoint live", text)
        self.assertIn("health_ok == 1", text)
        self.assertIn("Remaining budget: replans=1", text)
        self.assertNotIn('"run_id"', text)
        self.assertNotIn('"goal_key"', text)
        self.assertNotIn("{", text.split("Re-plan brief:")[1].split("Goal:")[0])

    def test_replan_repairs_echoed_memory_record_then_succeeds(self) -> None:
        import json

        from freetalon.orchestrator.planner import replan_from_brief

        # The exact failure from retry #2: the model returns the memory
        # record verbatim as the nodes array. The repair round must break
        # the echo and produce a valid delta plan in one round.
        echo = json.dumps({"nodes": [self._MEMORY_RECORD], "metadata": {}})
        good = self._dag([self._good_delta_node()])
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[echo, good]
        ) as call_mock:
            nodes = replan_from_brief(self._brief())
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(call_mock.call_count, 2)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        # The failed output is described, never pasted back: the verbatim
        # memory record must not appear in the repair prompt.
        self.assertNotIn(echo, repair_prompt)
        self.assertNotIn('"run_id"', repair_prompt)
        self.assertNotIn('"goal_key"', repair_prompt)

    def test_replan_gives_up_after_memory_echo_budget(self) -> None:
        import json

        from freetalon.orchestrator.planner import replan_from_brief

        echo = json.dumps({"nodes": [self._MEMORY_RECORD], "metadata": {}})
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=echo
        ) as call_mock:
            with self.assertRaisesRegex(
                LLMResponseError, "did not match PlanNode schema"
            ):
                replan_from_brief(self._brief())
        # Initial attempt + 2 repair retries, then the last error is raised.
        self.assertEqual(call_mock.call_count, 3)

    def test_prompts_state_no_fake_deliverables_and_prose_brief(self) -> None:
        from freetalon.orchestrator.prompts import (
            PLANNER_SYSTEM_PROMPT,
            REPLAN_SYSTEM_PROMPT,
        )

        self.assertIn("Never fake a deliverable", PLANNER_SYSTEM_PROMPT)
        self.assertIn("live service", PLANNER_SYSTEM_PROMPT)
        self.assertIn("re-plan brief as prose", REPLAN_SYSTEM_PROMPT)
        self.assertNotIn("Re-plan brief JSON", REPLAN_SYSTEM_PROMPT)



class TestPlannerHeredocFix(unittest.TestCase):
    """A valid multi-line file write (retry #3).

    Retry #3 (log llm-end-to-end-health-retry3-2026-11-16.log) failed at
    the planning stage: the model tried 3 times (initial plan + 2
    repairs) to write the test file as a single-line echo with all
    newlines collapsed, and the validator correctly rejected it every
    time as glued words. The model had no valid move: the repair prompt
    never named an alternative. Two fixes: (1) the planner prompt names
    the scaffold claw and the bash heredoc as THE ways to write a
    multi-line file, and the glued-word rejection names them too; (2)
    validation accepts a heredoc file write - heredoc bodies are file
    content, not shell, so a URL inside the file is not flagged as
    glued words, and a multi-line test file is not flagged as a static
    stand-in answer.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL

    # The exact node-1 command from the retry-#3 final rejection: a
    # single-line echo with every newline collapsed.
    GLUED_ECHO = (
        "echo '#import unittest, requests, json from freetalon.api import api"
        "class TestHealthEndpoint(unittest.TestCase): "
        "def test_health_endpoint(self): "
        'response = requests.get("http://localhost:8765/health") '
        "self.assertEqual(response.status_code, 200) "
        "data = json.loads(response.text) "
        'self.assertEqual(data["service_status"], "running") '
        'self.assertEqual(data["ollama_reachable"], True) '
        'self.assertTrue(isinstance(data["gpu_temperature"], int)) '
        'if __name__ == "__main__": unittest.main()\' '
        "> /home/seeker/freetalon-workspace/test_health.py "
        "&& python3 /home/seeker/freetalon-workspace/test_health.py"
    )

    # The same test file as a heredoc: the valid replacement.
    HEREDOC_TEST = (
        "cat > /home/seeker/freetalon-workspace/test_health.py <<'EOF'\n"
        "import unittest, requests, json\n"
        "from freetalon.api import api\n"
        "class TestHealthEndpoint(unittest.TestCase):\n"
        "    def test_health_endpoint(self):\n"
        '        response = requests.get("http://localhost:8765/health")\n'
        "        self.assertEqual(response.status_code, 200)\n"
        "        data = json.loads(response.text)\n"
        '        self.assertEqual(data["service_status"], "running")\n'
        '        self.assertEqual(data["ollama_reachable"], True)\n'
        '        self.assertTrue(isinstance(data["gpu_temperature"], int))\n'
        "EOF"
    )

    def setUp(self) -> None:
        self.intent = TaskIntent(
            goal=self.HEALTH_GOAL,
            project_type="bash",
            capabilities=["scaffold", "bash", "llm_synthesis"],
            constraints={"workspace": "/home/seeker/freetalon-workspace"},
            missing_inputs=[],
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _bash_node(self, command: str, **overrides: object) -> dict:
        node: dict = {
            "id": "step_1",
            "objective": "Write the health endpoint test file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": command},
            "outputs": ["/home/seeker/freetalon-workspace/test_health.py"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        node.update(overrides)
        return node

    def _heredoc_plan_nodes(self) -> list[dict]:
        ws = "/home/seeker/freetalon-workspace"
        return [
            {
                "id": "step_1",
                "objective": "Write the health endpoint test file",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": self.HEREDOC_TEST},
                "outputs": [f"{ws}/test_health.py"],
                "criteria": [{"type": "file_exists", "path": f"{ws}/test_health.py"}],
            },
            {
                "id": "step_2",
                "objective": "Run the health endpoint tests",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": f"python3 {ws}/test_health.py"},
                "outputs": ["test results"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_3",
                "objective": "Verify the endpoint live",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    def test_parse_rejects_retry3_glued_echo_and_names_alternative(self) -> None:
        node = self._bash_node(self.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_glued_echo_repairs_to_heredoc_in_one_round(self) -> None:
        bad = self._dag([self._bash_node(self.GLUED_ECHO)])
        good = self._dag(self._heredoc_plan_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(plan.nodes[0].assigned_claw, "bash")
        self.assertIn("<<'EOF'", plan.nodes[0].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("heredoc", repair_prompt)

    def test_parse_accepts_heredoc_with_url_in_body(self) -> None:
        node = self._bash_node(
            self.HEREDOC_TEST,
            criteria=[
                {
                    "type": "file_exists",
                    "path": "/home/seeker/freetalon-workspace/test_health.py",
                }
            ],
        )
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(nodes[0].assigned_claw, "bash")

    def test_parse_still_rejects_heredoc_fake_json_answer(self) -> None:
        fake = (
            "cat > /home/seeker/freetalon-workspace/api.json <<'EOF'\n"
            '{"health": "OK", "ollama_reachable": true, "gpu_temp": 60}\n'
            "EOF"
        )
        node = self._bash_node(
            fake,
            outputs=["/home/seeker/freetalon-workspace/api.json"],
            criteria=[
                {
                    "type": "file_exists",
                    "path": "/home/seeker/freetalon-workspace/api.json",
                }
            ],
        )
        with self.assertRaisesRegex(LLMResponseError, "hardcoded answer"):
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)

    def test_parse_accepts_scaffold_test_file(self) -> None:
        # A scaffold node alone cannot engage the /health route, so the
        # plan also needs a node that calls the live endpoint.
        nodes_in = [
            {
                "id": "step_1",
                "objective": "Write the health endpoint test file",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "/home/seeker/freetalon-workspace/test_health.py",
                    "content": "import unittest\n\n\nclass TestHealth(unittest.TestCase):\n    pass\n",
                },
                "outputs": ["/home/seeker/freetalon-workspace/test_health.py"],
                "criteria": [
                    {
                        "type": "file_exists",
                        "path": "/home/seeker/freetalon-workspace/test_health.py",
                    }
                ],
            },
            {
                "id": "step_2",
                "objective": "Verify the endpoint live",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]
        nodes, _ = parse_planner_response(
            self._dag(nodes_in), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_prompts_name_heredoc_or_scaffold_for_multiline_files(self) -> None:
        from freetalon.orchestrator.prompts import (
            PLANNER_SYSTEM_PROMPT,
            REPLAN_SYSTEM_PROMPT,
        )

        for prompt in (PLANNER_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT):
            self.assertIn("heredoc", prompt)
            self.assertIn("scaffold", prompt)
        self.assertIn("single-line echo", PLANNER_SYSTEM_PROMPT)

    def test_strip_heredoc_bodies_keeps_shell_around_it(self) -> None:
        from freetalon.orchestrator.planner import _strip_heredoc_bodies

        command = (
            "cat > /tmp/x.py <<'EOF'\n"
            "x = 1\n"
            "EOF\n"
            "python3 /tmp/x.py"
        )
        stripped = _strip_heredoc_bodies(command)
        self.assertNotIn("x = 1", stripped)
        self.assertIn("python3 /tmp/x.py", stripped)
        self.assertIn("<<'EOF'", stripped)

    def test_strip_heredoc_bodies_leaves_unterminated_alone(self) -> None:
        from freetalon.orchestrator.planner import _strip_heredoc_bodies

        unterminated = "cat > /tmp/x.py <<'EOF'\nx = 1\n"
        self.assertEqual(_strip_heredoc_bodies(unterminated), unterminated)


class TestPlannerRouteEngagementFix(unittest.TestCase):
    """Tight route engagement + replan no-copy (retry #4).

    Retry #4 (run-20261117T062659Z-bcd569) failed two ways. (1) The
    accepted plan never engaged freetalon/api.py: node-1 curled
    http://localhost:11434/health — the route literal on Ollama's port —
    and the old engagement rule counted ANY "/health" string, so the plan
    passed validation while curling a service with no /health endpoint.
    (2) The replan echoed the brief's own delta-plan steps back verbatim
    (instruction + delta_plan.steps, including retry #2's test.sh /
    glued-unittest failures) because the brief's memory prose still
    handed the model verbatim command strings. Three fixes: engagement
    now requires the serving-module mention or a call to the route on
    the API's own port; memory parameters are described by kind, never
    quoted; a brief-echo-shaped reply is rejected by name, and the
    replan prompt carries a grounded ROUTE GOAL shape for route goals.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The three nodes of the retry-#4 plan, verbatim from
    # runs/run-20261117T062659Z-bcd569/plan-v001.json. Embedded (not read
    # from the run directory) so the test stays hermetic in CI.
    @staticmethod
    def _retry4_plan_nodes() -> list[dict]:
        ws = "/home/seeker/freetalon-workspace"
        return [
            {
                "id": "node-1",
                "objective": "Verify OLLAMA is reachable at localhost:11434 and return status.",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl -s http://localhost:11434/health | grep '{\"status\":\"running\"'"},
                "outputs": ["ollama_reachable"],
                "acceptance": ["OLLAMA reachable and status is running."],
                "error": None,
                "criteria": [{"type": "exit_code", "equals": 0}],
                "reversible": True,
                "timeout_s": 300.0,
                "max_retries": 0,
                "hypothesis": "",
                "responds_to": "",
                "differs_by": "",
                "measurement": {},
                "duration_s": None,
                "exit_code": None,
            },
            {
                "id": "node-2",
                "objective": "Verify GPU temperature is available via nvidia-smi.",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "nvidia-smi"},
                "outputs": ["gpu_temperature"],
                "acceptance": ["GPU temperature information is available."],
                "error": None,
                "criteria": [{"type": "exit_code", "equals": 0}],
                "reversible": True,
                "timeout_s": 300.0,
                "max_retries": 0,
                "hypothesis": "",
                "responds_to": "",
                "differs_by": "",
                "measurement": {},
                "duration_s": None,
                "exit_code": None,
            },
            {
                "id": "node-3",
                "objective": "Return JSON reporting service status, OLLAMA reachability and GPU temperature.",
                "depends_on": ["node-1", "node-2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "echo '{\"service_status\":\"running\", \"ollama_reachable\": true, \"gpu_temperature\": \"available\"}'"},
                "outputs": ["health_json"],
                "acceptance": ["JSON reporting service status, OLLAMA reachability and GPU temperature is returned."],
                "error": None,
                "criteria": [{"type": "exit_code", "equals": 0}],
                "reversible": True,
                "timeout_s": 300.0,
                "max_retries": 0,
                "hypothesis": "",
                "responds_to": "",
                "differs_by": "",
                "measurement": {},
                "duration_s": None,
                "exit_code": None,
            },
        ]

    # The exact first replan reply from retry #4 (log
    # llm-end-to-end-health-retry4-2026-11-16.log): the brief re-serialized
    # as {"instruction": ..., "delta_plan": {"steps": [...]}}.
    @staticmethod
    def _replan1_echo() -> dict:
        return {
            "instruction": "Produce a NEW delta plan DAG for the brief above. Do NOT repeat, quote, or echo the brief — it is your input, not your output.",
            "delta_plan": {
                "replans_remaining": 1,
                "wall_clock_s": 3599.9614646220007,
                "max_attempts_per_hypothesis": 3,
                "max_nodes": 50,
                "max_money": 0.0,
                "steps": [
                    {
                        "step_id": "1",
                        "action": "REPLAN",
                        "node": "node-1",
                        "command": "curl -s http://localhost:11434/health | grep '{\"status\":\"running\"';",
                        "exit_code": 1,
                        "stderr": "",
                        "stdout": "",
                        "reason": "Failed to reach OLLAMA at localhost:11434 and return status.",
                        "hypothesis": "Verify OLLAMA is reachable at localhost:11434 and return status.",
                        "outcome": "VERDICT FAIL",
                        "measurement_did_not_pass": True,
                        "decision": "REPLAN",
                    },
                    {
                        "step_id": "2",
                        "action": "REPLAN",
                        "node": "node-3",
                        "command": "/home/seeker/freetalon-workspace/test.sh",
                        "exit_code": 127,
                        "stderr": "/home/seeker/freetalon-workspace/test.sh: 1: assert: not found",
                        "stdout": "",
                        "reason": "Failed to verify the endpoint live.",
                        "hypothesis": "Verify the endpoint live.",
                        "outcome": "VERDICT FAIL",
                        "measurement_did_not_pass": True,
                        "decision": "REPLAN",
                    },
                    {
                        "step_id": "3",
                        "action": "REPLAN",
                        "node": "node-2",
                        "command": "python3 -munittestdiscover -s/home/seeker/freetalon-workspace/api/health.py",
                        "exit_code": 127,
                        "stderr": "/bin/sh: 1: python3 -munittestdiscover -s/home/seeker/freetalon-workspace/api/heal",
                        "stdout": "",
                        "reason": "Failed to run API endpoint tests.",
                        "hypothesis": "Run API endpoint tests.",
                        "outcome": "VERDICT FAIL",
                        "measurement_did_not_pass": True,
                        "decision": "REPLAN",
                    },
                ],
            },
        }

    # The retry-#4 node-1 failure as an experiment-memory record, in the
    # shape ExperimentStore._row_to_record returns. Its approach carries
    # the failed curl as a key_param — the copy source for the echo.
    _MEMORY_RECORD_RETRY4 = {
        "run_id": "run-20261117T062659Z-bcd569",
        "goal_key": "07ab30f88fe6662b",
        "domain": "bash",
        "node_id": "node-1",
        "ts": "2026-11-17T06:26:59.053190+00:00",
        "hypothesis": "Verify OLLAMA is reachable at localhost:11434 and return status.",
        "approach": {
            "capability": "bash",
            "key_params": {
                "command": "curl -s http://localhost:11434/health | grep '{\"status\":\"running\"'"
            },
            "data_window": "",
        },
        "measurement": {
            "verdict": "FAIL",
            "exit_code": 1,
            "duration_s": 0.008,
            "results": [
                {"passed": False, "measured": True, "detail": "exit_code 1 != 0"}
            ],
        },
        "decision": "REPLAN",
        "lesson": (
            "bash (Verify OLLAMA is reachable at localhost:11434 and return "
            "status.): verdict FAIL — failed criterion: exit_code 1 != 0; "
            "exit 1; 0.008s; decision REPLAN."
        ),
    }

    def setUp(self) -> None:
        self.intent = TaskIntent(
            goal=self.HEALTH_GOAL,
            project_type="bash",
            capabilities=["scaffold", "bash", "llm_synthesis"],
            constraints={"workspace": "/home/seeker/freetalon-workspace"},
            missing_inputs=[],
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self.api_path = str(
            Path(__file__).resolve().parent.parent / "freetalon" / "api.py"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _good_delta_nodes(self) -> list[dict]:
        """A real delta plan: edit the module, then call the live route."""
        return [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "content": "# extended health"},
                "outputs": [self.api_path],
                "criteria": [{"type": "file_exists", "path": self.api_path}],
                "responds_to": "node-1",
                "differs_by": "edit the existing module instead of probing Ollama",
            },
            {
                "id": "step_2",
                "objective": "Verify the endpoint live on the API port",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "responds_to": "node-1",
                "differs_by": "call the FreeTalon API on 8765, not Ollama on 11434",
            },
        ]

    def _brief(self, memory: list[dict] | None = None) -> dict:
        failed_measurement = {
            "verdict": "FAIL",
            "exit_code": 1,
            "duration_s": 0.008,
            "results": [
                {"passed": False, "measured": True, "detail": "exit_code 1 != 0"}
            ],
        }
        return {
            "goal": {
                "objective": self.HEALTH_GOAL,
                "success_measures": [
                    {"name": "endpoint live", "metric": "health_ok", "op": "==", "value": 1}
                ],
                "budget": {"max_wall_clock_s": 3600.0, "max_replans": 2},
                "risk_class": "read_only",
                "stop_conditions": [],
                "irreversible_actions": [],
            },
            "plan_id": "plan-7885a4ef9d99",
            "plan_version": 1,
            "failed_nodes": [
                {
                    "id": "node-1",
                    "objective": "Verify OLLAMA is reachable at localhost:11434 and return status.",
                    "hypothesis": "Verify OLLAMA is reachable at localhost:11434 and return status.",
                    "assigned_claw": "bash",
                    "measurement": failed_measurement,
                    "error": None,
                }
            ],
            "measurements": [
                dict({"node_id": "node-1"}, **failed_measurement),
            ],
            "memory": memory if memory is not None else [self._MEMORY_RECORD_RETRY4],
            "remaining_budget": {
                "replans": 1,
                "wall_clock_s": 3000.0,
                "max_attempts_per_hypothesis": 3,
                "max_nodes": 10,
                "max_money": 0.0,
            },
            "instruction": (
                "Produce a delta plan: name which failed hypothesis each new "
                "node responds to (responds_to) and what it does differently "
                "(differs_by). Do not repeat a recorded failed approach "
                "without a stated reason. Shrink scope before growing it."
            ),
        }

    # --- Weakness 1: route engagement is port-aware ---

    def test_parse_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        # Only the engagement rule fires: the curl is a known port, the
        # echo goes to stdout (no file written), nvidia-smi is fine.
        self.assertIn("1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_api_port_call_engages_wrong_port_does_not(self) -> None:
        curl_8765 = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "curl -s http://localhost:8765/health"},
            "outputs": ["verification"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        # A live call on the API port engages the route on its own.
        nodes, _ = parse_planner_response(
            self._dag([curl_8765]), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 1)
        # With a module-engaging node alongside, same result.
        nodes, _ = parse_planner_response(
            self._dag(self._good_delta_nodes()), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 2)
        # Only curling /health on Ollama's port does not engage it.
        curl_11434 = dict(
            curl_8765,
            inputs={"command": "curl -s http://localhost:11434/health"},
        )
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag([curl_11434]), goal_text=self.HEALTH_GOAL
            )

    # --- Weakness 2: the replan brief cannot be copied back ---

    def test_parse_names_brief_echo(self) -> None:
        import json

        echo = json.dumps(self._replan1_echo())
        with self.assertRaisesRegex(
            LLMResponseError, "copied the re-plan brief"
        ):
            parse_planner_response(echo, goal_text=self.HEALTH_GOAL)

    def test_brief_never_quotes_failed_commands(self) -> None:
        from freetalon.orchestrator.planner import _format_brief_for_prompt

        memory = [
            self._MEMORY_RECORD_RETRY4,
            TestPlannerFakeAndMemoryFix._MEMORY_RECORD,
        ]
        text = _format_brief_for_prompt(self._brief(memory=memory))
        # The failed curl is never handed over whole.
        self.assertNotIn(
            "curl -s http://localhost:11434/health", text
        )
        self.assertNotIn("test.sh", text)
        self.assertIn("command=a shell command", text)
        # A file path is described by kind, not quoted.
        self.assertNotIn(
            "/home/seeker/freetalon-workspace/api/health.py", text
        )
        self.assertIn("file_path=a file path", text)
        # The facts are still there as prose.
        self.assertIn("verdict FAIL", text)
        self.assertIn("exit 1", text)
        self.assertIn("Decision: REPLAN", text)

    def test_describe_param_value_names_kinds_not_text(self) -> None:
        from freetalon.orchestrator.planner import _describe_param_value

        self.assertEqual(
            _describe_param_value("command", "curl -s http://localhost:11434/health"),
            "command=a shell command",
        )
        self.assertEqual(
            _describe_param_value("file_path", "/home/seeker/x/api/health.py"),
            "file_path=a file path",
        )
        self.assertEqual(
            _describe_param_value("model", "llama3"), "model=llama3"
        )
        self.assertEqual(_describe_param_value("count", 3), "count=3")
        self.assertEqual(_describe_param_value("flag", True), "flag=True")

    def test_replan_repairs_brief_echo_in_one_round(self) -> None:
        import json

        from freetalon.orchestrator.planner import replan_from_brief

        echo = json.dumps(self._replan1_echo())
        good = self._dag(self._good_delta_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[echo, good]
        ) as call_mock:
            nodes = replan_from_brief(self._brief())
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(call_mock.call_count, 2)
        first_prompt = call_mock.call_args_list[0][0][1]
        repair_prompt = call_mock.call_args[0][1]
        # The first prompt hands the model a grounded shape, not copyable
        # commands: the ROUTE GOAL block is present, the failed curl is not.
        self.assertIn("ROUTE GOAL", first_prompt)
        self.assertIn("curl http://localhost:8765/health", first_prompt)
        self.assertIn("freetalon/api.py", first_prompt)
        self.assertNotIn("curl -s http://localhost:11434/health", first_prompt)
        self.assertNotIn("test.sh", first_prompt)
        # The repair names the echo and never pastes the echoed output back.
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("copied the re-plan brief", repair_prompt)
        self.assertNotIn(echo, repair_prompt)
        # The repair names the echo shape ("delta_plan" in the rejection
        # reason) — that naming is the fix — but carries none of the
        # echoed step content: no failed commands, no test.sh.
        self.assertNotIn("python3 -munittestdiscover", repair_prompt)
        self.assertNotIn("test.sh", repair_prompt)
        self.assertNotIn("curl -s http://localhost:11434/health", repair_prompt)

    def test_replan_prompt_has_route_shape_only_for_route_goals(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        good = self._dag(self._good_delta_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            replan_from_brief(self._brief())
        self.assertIn("ROUTE GOAL", call_mock.call_args[0][1])

        non_route_brief = {
            "goal": {
                "objective": "Run the test suite",
                "success_measures": [],
                "budget": {},
                "risk_class": "read_only",
                "stop_conditions": [],
                "irreversible_actions": [],
            },
            "plan_id": "plan-x",
            "plan_version": 1,
            "failed_nodes": [],
            "measurements": [],
            "memory": [],
            "remaining_budget": {"replans": 1},
            "instruction": "Produce a delta plan.",
        }
        plain = self._dag(
            [
                {
                    "id": "step_1",
                    "objective": "Run the test suite",
                    "depends_on": [],
                    "assigned_claw": "bash",
                    "inputs": {"command": "python3 -m unittest discover -s tests"},
                    "outputs": ["test results"],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                }
            ]
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=plain
        ) as call_mock:
            nodes = replan_from_brief(non_route_brief)
        self.assertEqual(len(nodes), 1)
        self.assertNotIn("ROUTE GOAL", call_mock.call_args[0][1])

    # --- Regression guards: the heredoc fix still holds ---

    def test_parse_accepts_valid_heredoc_health_plan(self) -> None:
        ws = self.WS
        nodes_in = [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "content": "# extended health"},
                "outputs": [self.api_path],
                "criteria": [{"type": "file_exists", "path": self.api_path}],
            },
            {
                "id": "step_2",
                "objective": "Write the health endpoint test file",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": TestPlannerHeredocFix.HEREDOC_TEST},
                "outputs": [f"{ws}/test_health.py"],
                "criteria": [{"type": "file_exists", "path": f"{ws}/test_health.py"}],
            },
            {
                "id": "step_3",
                "objective": "Run the health endpoint tests",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": f"python3 {ws}/test_health.py"},
                "outputs": ["test results"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_4",
                "objective": "Verify the endpoint live",
                "depends_on": ["step_3"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]
        nodes, _ = parse_planner_response(
            self._dag(nodes_in), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_parse_still_rejects_retry3_glued_echo(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Write the health endpoint test file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": TestPlannerHeredocFix.GLUED_ECHO},
            "outputs": [f"{self.WS}/test_health.py"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_prompts_forbid_re_serializing_the_brief(self) -> None:
        from freetalon.orchestrator.prompts import REPLAN_SYSTEM_PROMPT

        self.assertIn("never re-serialize the brief", REPLAN_SYSTEM_PROMPT)
        self.assertIn('"delta_plan"', REPLAN_SYSTEM_PROMPT)
