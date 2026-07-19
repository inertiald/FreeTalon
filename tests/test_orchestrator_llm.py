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
                "FREETALON_OLLAMA_BASE_URL": "http://ollama.local:11434/",
                "FREETALON_OLLAMA_MODEL": "mistral",
            },
            clear=False,
        ):
            ollama_settings = LLMSettings.from_env()
        self.assertEqual(ollama_settings.backend, "ollama")
        self.assertEqual(ollama_settings.base_url, "http://ollama.local:11434/")
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
                {"id": "deploy", "objective": "Deploy service", "depends_on": ["build"]},
                {"id": "build", "objective": "Build image", "depends_on": []}
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
            {"id": "verify", "objective": "Verify inputs", "depends_on": []},
            {"id": "execute", "objective": "Execute rollout", "depends_on": ["verify"]}
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
