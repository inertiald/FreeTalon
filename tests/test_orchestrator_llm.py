"""Tests for orchestrator intake, prompts, and planner flows."""

from __future__ import annotations

import os
import unittest
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
