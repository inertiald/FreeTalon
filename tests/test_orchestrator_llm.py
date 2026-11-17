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

# Real code for the grounded /health extension, used as scaffold content
# wherever a fixture extends freetalon/api.py. Retry #14: a comment-only
# scaffold to an existing code file is now rejected as a placeholder, so
# the fixtures must carry real code — the do_GET branch fragment the
# grounded move names.
GROUNDED_API_CONTENT = (
    "# extended health endpoint\n"
    "if path == \"/health\":\n"
    "    self._send({\"ok\": True, \"status\": \"ready\"})"
)

# The retry-#10 attempt-3 fixture names the absolute seeker7 path
# /home/seeker/projects/FreeTalon/freetalon/api.py (see
# TestPlannerDependsOnFix._retry10_attempt3_nodes). The rejection count
# for it is disk-dependent: _unwritten_file_errors fires only when that
# path does not exist on disk, so on seeker7 (where the file exists) the
# rejection has 7 errors and in CI (where the checkout lives elsewhere)
# it has 9 -- the same 7 plus one unwritten-file error per node claiming
# file_exists on it. Tests asserting on this fixture must use
# _expected_retry10_error_count() instead of a hard-coded count.
RETRY10_API_PATH = "/home/seeker/projects/FreeTalon/freetalon/api.py"


def _expected_retry10_error_count() -> int:
    """7 on a checkout where the fixture's api.py exists, 9 elsewhere."""
    return 7 if os.path.exists(RETRY10_API_PATH) else 9

# The retry-#16 v1 fixture appends to the same absolute seeker7 path
# (RETRY10_API_PATH). The code-append parse check simulates the write
# against the file on disk when it exists and against the appended
# content alone when it does not, so the reported line of the
# IndentationError is disk-dependent too: on seeker7 the existing
# api.py has 219 lines, the appended def lands on line 220 and the
# missing indented block is reported on line 221; in CI the path
# does not exist, the content alone is parsed (def on line 1, return
# on line 2) and the error is reported on line 2. Tests asserting
# the line must use _expected_retry16_error_line() instead of a
# hard-coded number.
def _expected_retry16_error_line() -> int:
    """221 on a checkout where the fixture's api.py exists, 2 elsewhere."""
    return 221 if os.path.exists(RETRY10_API_PATH) else 2


def _assert_code_append_parse_error(
    testcase: unittest.TestCase, message: str
) -> None:
    """The code-append parse rejection, worded either way.

    _code_append_errors has two branches: against an existing file it
    says the write leaves the file so it "no longer parses as Python";
    against content alone (the file does not exist -- CI, where the
    fixture's absolute seeker7 path is absent) it says the code "does
    not parse as Python". Both wordings name the same defect.
    """
    testcase.assertTrue(
        "parses as Python" in message
        or "does not parse as Python" in message,
        message,
    )


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
            "inputs": {"file_path": api_path, "content": GROUNDED_API_CONTENT},
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
            inputs={"file_path": api_path, "content": GROUNDED_API_CONTENT},
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
                    "inputs": {"file_path": api_path, "content": GROUNDED_API_CONTENT},
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
            "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
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
                "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
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
            "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
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
            # Retry #7: a schema invention is now a numbered, repairable
            # rejection (naming the missing objective and the invented
            # fields) instead of a structural "did not match PlanNode
            # schema" raise — the budget is still exhausted after 3 calls.
            with self.assertRaisesRegex(
                LLMResponseError, "was rejected with"
            ) as caught:
                replan_from_brief(self._brief())
        self.assertIn("missing required field 'objective'", str(caught.exception))
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
                "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
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
                "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
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


class TestPlannerGroundedShapeFix(unittest.TestCase):
    """Name the grounded shape in the anti-fake rejection + first-attempt template (retry #5).

    Retry #5 (log llm-end-to-end-health-retry5-2026-11-16.log) failed at
    the planning stage: attempts 1-2 were the retry-#2 fake verbatim
    (echo a hardcoded JSON answer into api.json, fake test.sh), and
    attempt 3 was a probe/echo plan rejected by the port-aware
    route-engagement rule with the exact fix named — but the repair
    budget (max 2) was already spent, so the model never got a repair
    round for the engagement error. The deeper pattern: the anti-fake
    rejection steers AWAY from the fake but not TOWARD the grounded
    ROUTE GOAL shape (edit freetalon/api.py via scaffold/heredoc ->
    write + run tests -> curl :8765/health), and the first-attempt
    prompt never named the shape at all. Two fixes: (1) when the goal
    names an existing route, the anti-fake rejection names the grounded
    move (serving module, real tests, live API port); (2) the initial
    planner prompt carries the same ROUTE GOAL shape the replan prompt
    already had, so the first attempt has the valid move in front of
    it.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#5 attempt-1 plan, verbatim from the first RAW
    # PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry5-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI. The commands
    # below are the JSON-parsed values (plain double quotes), not the
    # log's backslash-escaped spellings.
    @staticmethod
    def _retry5_fake_nodes() -> list[dict]:
        ws = "/home/seeker/freetalon-workspace"
        return [
            {
                "id": "step_1",
                "objective": "Add health endpoint to FreeTalon's API",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "echo '{\"health\": \"OK\", \"ollama_reachable\": true, \"gpu_temp\": 60}' > /home/seeker/freetalon-workspace/api.json"},
                "outputs": ["/home/seeker/freetalon-workspace/api.json"],
                "acceptance": ["File exists"],
                "criteria": [{"type": "file_exists", "path": "/home/seeker/freetalon-workspace/api.json"}],
                "error": None,
            },
            {
                "id": "step_2",
                "objective": "Add unit tests for the endpoint",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "echo 'assert api.json contains \"health\": \"OK\"' > /home/seeker/freetalon-workspace/test.sh && chmod +x /home/seeker/freetalon-workspace/test.sh"},
                "outputs": ["/home/seeker/freetalon-workspace/test.sh"],
                "acceptance": ["File exists"],
                "criteria": [{"type": "file_exists", "path": "/home/seeker/freetalon-workspace/test.sh"}],
                "error": None,
            },
            {
                "id": "step_3",
                "objective": "Verify the endpoint live",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "/home/seeker/freetalon-workspace/test.sh"},
                "outputs": ["verification"],
                "acceptance": ["Exit code 0"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            },
        ]

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

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan: edit the module, heredoc the tests, curl 8765."""
        ws = self.WS
        return [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
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

    # --- Fix 1: the anti-fake rejection names the grounded move ---

    def test_parse_rejects_retry5_fake_and_names_grounded_shape(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        # node-1 static JSON fake, node-2 static assert fake, route engagement.
        self.assertIn("3 errors", message)
        self.assertIn("static file", message)
        # The rejection now points TOWARD the grounded move, not just away
        # from the fake: serving module, real tests, live API port.
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)
        self.assertIn("scaffold", message)
        self.assertIn("heredoc", message)
        self.assertIn("curl http://localhost:8765/health", message)

    def test_fake_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        fake = self._dag(self._retry5_fake_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[fake, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("grounded move", repair_prompt)
        self.assertIn("freetalon/api.py", repair_prompt)
        self.assertIn("8765", repair_prompt)
        # The first attempt already carried the shape (fix 2), so the
        # repair is not the model's first sight of the grounded move.
        first_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("ROUTE GOAL", first_prompt)

    # --- Fix 2: the first-attempt prompt carries the ROUTE GOAL shape ---

    def test_first_attempt_prompt_has_route_shape_only_for_route_goals(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        prompt = call_mock.call_args[0][1]
        self.assertIn("ROUTE GOAL", prompt)
        self.assertIn("the plan must follow this shape", prompt)
        self.assertNotIn("the delta plan must follow this shape", prompt)
        self.assertIn("freetalon/api.py", prompt)
        self.assertIn("curl http://localhost:8765/health", prompt)

        non_route_intent = TaskIntent(
            goal="Run the test suite",
            project_type="bash",
            capabilities=["bash"],
            constraints={"workspace": self.WS},
            missing_inputs=[],
        )
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
            plan = plan_task_intent(non_route_intent, settings=self.settings)
        self.assertEqual(len(plan.nodes), 1)
        self.assertNotIn("ROUTE GOAL", call_mock.call_args[0][1])

    def test_replan_prompt_still_says_delta_plan(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        shape = _route_goal_shape(self.HEALTH_GOAL)
        self.assertIn("ROUTE GOAL", shape)
        self.assertIn("the delta plan must follow this shape", shape)

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
            )

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

    def test_parse_accepts_valid_heredoc_health_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")


class TestPlannerCriteriaShapeFix(unittest.TestCase):
    """Show the criterion list shape; coerce bare-dict criteria (retry #6).

    Retry #6 (log llm-end-to-end-health-retry6-2026-11-16.log) failed at
    the planning stage: attempt 2 was a heredoc plan whose three nodes
    had NO criteria at all, and attempt 3 "learned" criteria must exist
    but guessed the shape as a bare dict {"tests_pass": true} on every
    node — a PlanNode schema error that raises structurally, before any
    validation rule runs, so the repair budget burned on an error the
    repair loop could never see. Two fixes: (1) the criterion list shape
    is shown in the planner prompt (schema rule 4) and in the
    missing-criteria / no-type rejections, so the model is told the
    shape instead of having to guess it; (2) a bare-dict criteria is
    coerced to a one-element list at parse time, so the guess becomes a
    validation error the repair loop can fix. Secondary: a route-goal
    plan that writes to /tmp (attempt 2's Flask app at /tmp/freetalon/
    api.py, which passed route engagement because the /tmp path contains
    the module name as a substring) is now rejected by name.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    @staticmethod
    def _retry6_attempt3_nodes() -> list[dict]:
        # The exact attempt-3 nodes from the third RAW PLANNER OUTPUT
        # block of logs/llm-end-to-end-health-retry6-2026-11-16.log.
        # Embedded (not read from the log) so the test stays hermetic
        # in CI. Values are the JSON-parsed values (plain double
        # quotes), not the log's backslash-escaped spellings.
        return [
            {
                "id": "node-1",
                "objective": "Implement /health endpoint in freetalon/api.py",
                "criteria": {"tests_pass": True},
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": "echo '{\"service\": \"freetalon\", \"ollama_reachable\": true, \"gpu_temp\": 25}' > /tmp/health.json && freetalon/api.py"
                },
                "outputs": [
                    "/tmp/health.json",
                    "/home/seeker/freetalon-workspace/freetalon/api.py",
                ],
            },
            {
                "id": "node-2",
                "objective": "Write unit tests for /health endpoint",
                "criteria": {"tests_pass": True},
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "python -m unittest freetalon/test_api.py"},
                "outputs": ["freetalon/test_api.py"],
            },
            {
                "id": "node-3",
                "objective": "Verify /health endpoint is live with curl",
                "criteria": {"exit_code": 0},
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["http://localhost:8765/health"],
            },
        ]

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

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix 2: a bare-dict criteria coerces, then fails by name ---

    def test_bare_dict_criteria_is_rejected_not_structural(self) -> None:
        # The exact retry-#6 attempt-3 plan: before the fix this raised
        # a structural "did not match PlanNode schema" error, burning
        # the repair budget. Now it is a collected rejection naming the
        # shape (and the /tmp write on node-1).
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("was rejected with", message)
        self.assertIn("criteria MUST be a JSON array", message)
        self.assertIn('[{"type": "exit_code", "equals": 0}]', message)
        self.assertIn("has no 'type'", message)
        # Node-1 also writes its answer to /tmp — flagged by name.
        self.assertIn("/tmp/health.json", message)
        self.assertIn("not a /tmp scratch file", message)

    def test_bare_dict_criteria_coerces_on_non_route_goal(self) -> None:
        # The coercion is general, not a route-goal special case: a
        # bare-dict criteria on any goal is a validation error, never
        # a structural raise.
        node = {
            "id": "step_1",
            "objective": "Run the test suite",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "python3 -m unittest discover -s tests"},
            "outputs": ["test results"],
            "criteria": {"exit_code": 0},
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]), goal_text="Run the test suite"
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_bare_dict_criteria_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry6_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        # The repair prompt carries the shape example, not just "needs criteria".
        self.assertIn("criteria MUST be a JSON array", repair_prompt)
        self.assertIn('[{"type": "exit_code", "equals": 0}]', repair_prompt)

    # --- Fix 1: the shape is shown in the prompt and the rejection ---

    def test_missing_criteria_rejection_shows_shape_example(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "curl -s http://localhost:8765/health"},
            "outputs": ["verification"],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("has no 'criteria'", message)
        self.assertIn("criteria MUST be a JSON array", message)
        self.assertIn('[{"type": "exit_code", "equals": 0}]', message)
        self.assertIn(
            '[{"type": "file_exists", "path": "<the file the node writes>"}]',
            message,
        )

    def test_prompts_state_criteria_is_a_list_not_an_object(self) -> None:
        from freetalon.orchestrator.prompts import (
            PLANNER_SYSTEM_PROMPT as PLANNER,
        )
        from freetalon.orchestrator.prompts import (
            REPLAN_SYSTEM_PROMPT as REPLAN,
        )

        self.assertIn(
            '"criteria" MUST ALWAYS be a JSON array (a LIST) of criterion objects',
            PLANNER,
        )
        self.assertIn('{"tests_pass": true}', PLANNER)
        self.assertIn("a LIST of criterion objects, never", REPLAN)
        self.assertIn('{"tests_pass": true}', REPLAN)

    # --- Secondary: a route-goal write to /tmp is rejected ---

    def test_tmp_write_for_route_goal_is_rejected(self) -> None:
        # Retry #6 attempt 2, reduced to one node with valid criteria:
        # the heredoc writes a Flask app to /tmp/freetalon/api.py
        # instead of extending freetalon/api.py. Two rules fire on the
        # same node — the /tmp rule and (retry #7) the framework rule,
        # since the heredoc body imports Flask while the serving module
        # is http.server-based.
        node = {
            "id": "node-1",
            "objective": "Implement the /health endpoint in freetalon/api.py on port 8765",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": "cat > /tmp/freetalon/api.py <<'EOF'\nfrom flask import Flask\napp = Flask(__name__)\nEOF"
            },
            "outputs": ["/tmp/freetalon/api.py"],
            "criteria": [{"type": "file_exists", "path": "/tmp/freetalon/api.py"}],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        message = str(caught.exception)
        self.assertIn("2 errors", message)
        self.assertIn("/tmp/freetalon/api.py", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("not a /tmp scratch file", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("stdlib http.server", message)

    def test_tmp_write_for_non_route_goal_is_not_flagged(self) -> None:
        # The /tmp rule is scoped to route goals: a scratch write for an
        # ordinary goal is not this rule's business.
        node = {
            "id": "step_1",
            "objective": "Write a python script at /tmp/app.py that prints hi",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": "/tmp/app.py", "content": "print('hi')\n"},
            "outputs": ["/tmp/app.py"],
            "criteria": [{"type": "file_exists", "path": "/tmp/app.py"}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text="Write a python script at /tmp/app.py"
        )
        self.assertEqual(len(nodes), 1)

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_still_rejects_retry5_fake_with_grounded_move(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
            )

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

    def test_parse_accepts_valid_heredoc_health_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")


class TestPlannerNodeShapeFix(unittest.TestCase):
    """Schema inventions become numbered rejections; the node shape is shown (retry #7).

    Retry #7 (log llm-end-to-end-health-retry7-2026-11-16.log) failed at
    the planning stage: attempt 3 abandoned the PlanNode schema entirely
    and invented node types {"type": "write_file", "path": ..., "content":
    ...} / {"type": "execute_command", ...} with no objective and no
    assigned_claw. The old _validate_raw_nodes raised a structural
    "did not match PlanNode schema" error, which the repair loop could
    not turn into a fix — the budget burned on an error that never named
    the problem. Two fixes: (1) per-node schema errors are collected
    into ONE numbered rejection naming the missing fields and the
    extra-forbidden fields, plus the required node shape; (2) the
    planner prompt now shows the PlanNode shape as an example (NODE
    SHAPE rule), so the model stops drifting off the schema. Secondary:
    the repo index records each module's web framework from its imports
    and the REPO CONTEXT names it, and a route-goal plan that writes a
    different framework than the serving module's is rejected by name —
    freetalon/api.py is stdlib http.server, not Flask (retry #7 attempts
    2-3 wrote a Flask app for /health).
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#7 attempt-3 nodes, verbatim from the third RAW
    # PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry7-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI. Values are
    # the JSON-parsed values (plain double quotes), not the log's
    # backslash-escaped spellings.
    @staticmethod
    def _retry7_attempt3_nodes() -> list[dict]:
        return [
            {
                "id": "node-1",
                "type": "write_file",
                "path": "freetalon/api.py",
                "content": "import json\nfrom flask import Flask, jsonify\n\napp = Flask(__name__)\n\n@app.route('/health')\ndef health():\nstatus = {'status': 'ok', 'version': '1.0'}\nreturn jsonify(status)\n\nif __name__ == '__main__':\napp.run(host='0.0.0.0', port=8765)",
                "criteria": [{"type": "file_exists", "path": "freetalon/api.py"}],
            },
            {
                "id": "node-2",
                "type": "write_file",
                "path": "freetalon/test.sh",
                "content": "#!/bin/bash\ncurl http://localhost:8765/health | jq -r '.status' | grep 'ok'",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "node-3",
                "type": "execute_command",
                "command": "freetalon/test.sh",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

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

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix 1: schema inventions are collected, numbered rejections ---

    def test_invented_node_types_are_numbered_rejection_not_raise(self) -> None:
        # The exact retry-#7 attempt-3 output: before the fix this raised
        # a structural "did not match PlanNode schema" error, burning the
        # repair budget. Now it is a collected rejection naming the missing
        # objective and every invented field, plus the required node shape.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("missing required field 'objective'", message)
        self.assertIn("unknown field 'type'", message)
        self.assertIn("unknown field 'path'", message)
        self.assertIn("unknown field 'content'", message)
        self.assertIn("unknown field 'command'", message)
        # The rejection shows the required shape, not just the complaint.
        self.assertIn("PlanNode object", message)
        self.assertIn('"assigned_claw"', message)
        self.assertIn("write_file", message)
        self.assertIn("execute_command", message)

    def test_non_object_node_entry_is_numbered_rejection(self) -> None:
        # A nodes entry that is not an object at all is per-entry, not
        # DAG-level: it is collected the same way, never a structural raise.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(["not a node"]), goal_text=self.HEALTH_GOAL
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("not a valid node object", message)

    def test_invented_node_types_repair_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry7_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        # The repair prompt carries the schema rejection and the shape.
        self.assertIn("missing required field 'objective'", repair_prompt)
        self.assertIn("PlanNode object", repair_prompt)
        self.assertIn("write_file", repair_prompt)

    # --- Fix 2: the node shape is shown in the prompt ---

    def test_planner_prompt_shows_node_shape(self) -> None:
        from freetalon.orchestrator.prompts import (
            PLANNER_SYSTEM_PROMPT as PLANNER,
        )

        self.assertIn("NODE SHAPE", PLANNER)
        self.assertIn('"id": "step_1"', PLANNER)
        self.assertIn('"assigned_claw"', PLANNER)
        self.assertIn('"criteria": [{"type": "exit_code", "equals": 0}]', PLANNER)
        self.assertIn("write_file", PLANNER)
        self.assertIn("execute_command", PLANNER)
        self.assertIn("never a node \"type\"", PLANNER)

    def test_first_prompt_carries_node_shape(self) -> None:
        # The shape rides in the system prompt, so the first attempt (and
        # every repair) sees it — no LLM call is needed to check this.
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        system_prompt = call_mock.call_args[0][0]
        self.assertIn("NODE SHAPE", system_prompt)
        self.assertIn('"assigned_claw"', system_prompt)

    # --- Fix 1 regression: bare-dict criteria still coerces (798a116) ---

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)
        self.assertIn("has no 'type'", message)

    def test_no_criteria_node_still_rejected_with_shape_example(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "curl -s http://localhost:8765/health"},
            "outputs": ["verification"],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("has no 'criteria'", message)
        self.assertIn("criteria MUST be a JSON array", message)
        self.assertIn('[{"type": "exit_code", "equals": 0}]', message)

    # --- Secondary: the serving module's framework is grounded ---

    def test_flask_app_for_http_server_route_is_rejected(self) -> None:
        # Retry #7 attempt 2, reduced to one schema-valid node so the
        # framework error is isolated: a scaffold that writes a Flask app
        # into freetalon/api.py, which serves /health with http.server.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": "from flask import Flask\napp = Flask(__name__)\n\n@app.route('/health')\ndef health():\n    return {'ok': True}\n",
            },
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("Flask", message)
        self.assertIn("stdlib http.server", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("different web framework", message)

    def test_http_server_write_for_http_server_route_is_not_flagged(self) -> None:
        # The same shape in the serving module's own framework passes.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": "from http.server import BaseHTTPRequestHandler\n\nclass Handler(BaseHTTPRequestHandler):\n    pass\n",
            },
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 1)

    def test_repo_context_names_serving_module_framework(self) -> None:
        from freetalon.orchestrator.repo_index import (
            build_repo_index,
            format_repo_context,
        )

        context = format_repo_context(build_repo_index())
        self.assertIn("freetalon/api.py", context)
        self.assertIn("stdlib http.server", context)
        self.assertIn("do not rewrite it in a different web framework", context)

    def test_flask_heredoc_body_is_flagged_by_framework_rule(self) -> None:
        # Retry #7 attempt 2's heredoc, schema-valid and on the real path
        # so only the framework rule fires: the Flask import lives in the
        # heredoc body, which is file content, not shell.
        node = {
            "id": "node-1",
            "objective": "Implement the /health endpoint in freetalon/api.py on port 8765",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": "cat > /home/seeker/projects/FreeTalon/freetalon/api.py <<'EOF'\nfrom flask import Flask\napp = Flask(__name__)\nEOF"
            },
            "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
            "criteria": [
                {
                    "type": "file_exists",
                    "path": "/home/seeker/projects/FreeTalon/freetalon/api.py",
                }
            ],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        message = str(caught.exception)
        self.assertIn("Flask", message)
        self.assertIn("stdlib http.server", message)

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_still_rejects_retry5_fake_with_grounded_move(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
            )

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

    def test_parse_accepts_valid_heredoc_health_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")


class TestPlannerUnwrittenFileFix(unittest.TestCase):
    """Reject file_exists on a path the plan never writes; name prose re-serialization (retry #8).

    Retry #8 (log llm-end-to-end-health-retry8-2026-11-16.log, run
    run-20261117T071848Z-640cb5) failed in 22.0s: the v1 plan was ACCEPTED
    on the first attempt but was the fake in a new disguise — node-1 echoed
    a JSON answer to STDOUT (no redirect, so the static stand-in rule did
    not fire) while claiming file_exists on health.json, a file the command
    never writes; node-2 was a curl probe mislabeled as a test. Execution
    caught it (node-1 UNMEASURABLE: health.json does not exist) and the
    judge chose REPLAN. The replan then failed 3x: (1) it re-serialized
    the ROUTE GOAL shape prose as a tokenized JSON object (no nodes
    array), (2) {"nodes": []}, (3) invented node/action/target/port
    fields. Two fixes: (1) a file_exists criterion on a path no node in
    the plan writes and that does not exist on disk is rejected at plan
    time — listing the path in "outputs" alone does not count; (2) a
    payload with no 'nodes' array whose keys read like prompt prose
    tokenized into JSON is rejected by name as a prose re-serialization,
    and the ROUTE GOAL shape now forbids re-serializing the instructions
    as JSON.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#8 v1 nodes, verbatim from
    # runs/run-20261117T071848Z-640cb5/plan-v001.json. Embedded (not read
    # from the run directory) so the test stays hermetic in CI. The node-1
    # command is the JSON-parsed value: echo with backslash-escaped quotes,
    # no redirect, claiming file_exists on a file it never writes.
    @staticmethod
    def _retry8_v1_nodes() -> list[dict]:
        ws = "/home/seeker/freetalon-workspace"
        return [
            {
                "id": "node-1",
                "objective": "Implement the /health endpoint in freetalon/api.py and save its output",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "echo \"{\\\"status\\\": \\\"OK\\\", \\\"ollama_reachable\\\": true, \\\"gpu_temp\\\": 25}\""},
                "outputs": [f"{ws}/health.json"],
                "acceptance": ["Output file exists"],
                "criteria": [{"type": "file_exists", "path": f"{ws}/health.json"}],
                "error": None,
            },
            {
                "id": "node-2",
                "objective": "Write unit tests for the /health endpoint and run them",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl http://localhost:8765/health | jq '.status' -r"},
                "outputs": ["test_result"],
                "acceptance": ["Test result is OK"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            },
            {
                "id": "node-3",
                "objective": "Verify live with curl http://localhost:8765/health",
                "depends_on": ["node-2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["live_result"],
                "acceptance": ["Live result is OK"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            },
        ]

    # The exact retry-#8 replan attempt-1 output, verbatim from the second
    # RAW PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry8-2026-11-16.log: the ROUTE GOAL
    # shape prose re-serialized as a tokenized JSON object (no nodes
    # array). Embedded so the test stays hermetic in CI. Duplicate "node"
    # keys collapse on parse, as in the live run.
    RETRY8_REPLAN1_PROSE = """{"health":true,"served":false,"freetalon":"/api.py","on":"port",
"8765":true,"node":"1","edits":"freetalon/api.py",
"(scaffold":"claw,or","a":"bash","heredoc)":"to implement the endpoint;",
"node":"2","writes":"the tests",
"(scaffold":"or","a":"bash","heredoc)":"and runs them;",
"node":"3","verifies":"live with curl http://localhost:8765/health.",
"Do":true,"NOT":true,
"probe":"another service's /health path,",
"and":"do NOT echo a JSON answer."}"""

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

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix 1: file_exists on a path the plan never writes ---

    def test_parse_rejects_retry8_stdout_echo_fake(self) -> None:
        # The exact retry-#8 v1 plan: node-1 echoes to stdout and claims
        # file_exists on health.json, which no node writes and which does
        # not exist on disk. The stand-in rule cannot see it (no redirect);
        # the unwritten-file rule must catch it at plan time.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry8_v1_nodes()), goal_text=self.HEALTH_GOAL
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("health.json", message)
        self.assertIn("no node in the plan writes", message)
        self.assertIn("does not exist on disk", message)

    def test_stdout_echo_fake_repairs_to_grounded_plan_in_one_round(self) -> None:
        bad = self._dag(self._retry8_v1_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("health.json", repair_prompt)
        self.assertIn("does not exist on disk", repair_prompt)
        # The first attempt already carried the ROUTE GOAL shape with the
        # no-serialization rule, so the repair is not the model's first
        # sight of it.
        first_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("ROUTE GOAL", first_prompt)
        self.assertIn("Do NOT re-serialize these instructions as JSON", first_prompt)

    def test_cross_node_file_exists_passes(self) -> None:
        # A later node may check a file an earlier node wrote: the check
        # looks at the whole plan's writes, not just the claiming node.
        shared = f"{self.WS}/shared.txt"
        nodes = [
            {
                "id": "step_1",
                "objective": "Write the shared file",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": f"cat > {shared} <<'EOF'\nhello\nEOF"},
                "outputs": [shared],
                "criteria": [{"type": "file_exists", "path": shared}],
            },
            {
                "id": "step_2",
                "objective": "Check the shared file",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": f"ls {shared}"},
                "outputs": ["check"],
                "criteria": [{"type": "file_exists", "path": shared}],
            },
        ]
        parsed, _ = parse_planner_response(self._dag(nodes))
        self.assertEqual(len(parsed), 2)

    def test_file_exists_on_preexisting_file_passes(self) -> None:
        # A file_exists criterion on a file that already exists on disk
        # passes even when no node writes it: the serving module is the
        # canonical case.
        node = {
            "id": "step_1",
            "objective": "Check the API module exists",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": f"grep -n 'health' {self.api_path}"},
            "outputs": ["check"],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        parsed, _ = parse_planner_response(self._dag([node]))
        self.assertEqual(len(parsed), 1)
        # The disk escape hatch on its own: the path is listed in outputs
        # but named in no command and written by no node — it still passes
        # because the file exists on disk.
        node2 = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "curl -s http://localhost:8765/health"},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        parsed, _ = parse_planner_response(
            self._dag([node2]), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(parsed), 1)

    def test_parse_accepts_valid_heredoc_health_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_planner_prompt_says_outputs_alone_do_not_count(self) -> None:
        self.assertIn(
            'Listing a path in "outputs" alone does NOT count',
            PLANNER_SYSTEM_PROMPT,
        )

    # --- Fix 2: prompt prose re-serialized as JSON ---

    def test_parse_rejects_tokenized_route_goal_prose_by_name(self) -> None:
        # The exact retry-#8 replan attempt-1 output: the ROUTE GOAL shape
        # tokenized into a JSON object with no 'nodes' array. This must be
        # a named prose re-serialization rejection — not an acceptance and
        # not the generic "must contain a 'nodes' array" raise.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self.RETRY8_REPLAN1_PROSE)
        message = str(caught.exception)
        self.assertIn("re-serialized", message)
        self.assertIn("no 'nodes' array", message)
        self.assertNotIn("copied the re-plan brief", message)
        self.assertNotIn("must contain a 'nodes' array", message)

    def test_route_goal_shape_forbids_reserialization(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        shape = _route_goal_shape(self.HEALTH_GOAL)
        self.assertIn("ROUTE GOAL", shape)
        self.assertIn("Do NOT re-serialize these instructions as JSON", shape)
        first = _route_goal_shape(self.HEALTH_GOAL, delta=False)
        self.assertIn("Do NOT re-serialize these instructions as JSON", first)
        self.assertIn("the plan must follow this shape", first)

    def test_replan_prompt_forbids_reserializing_guidance(self) -> None:
        from freetalon.orchestrator.prompts import (
            REPLAN_SYSTEM_PROMPT as REPLAN,
        )

        self.assertIn("never re-serialize", REPLAN)
        self.assertIn("ROUTE", REPLAN)
        self.assertIn("GOAL shape", REPLAN)

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_still_rejects_retry7_invented_schema(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerNodeShapeFix._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("missing required field 'objective'", message)

    def test_parse_still_rejects_retry5_fake(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        self.assertIn("grounded move", str(caught.exception))

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
            )

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

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)
        self.assertIn("has no 'type'", message)


class TestPlannerRepoRootFix(unittest.TestCase):
    """Name the repo root explicitly; replan prompt carries NODE SHAPE (retry #9).

    Retry #9 (log llm-end-to-end-health-retry9-2026-11-16.log, run
    run-20261117T073452Z-2e7e88) failed in 30.5s: the v1 plan was ACCEPTED
    on the first attempt but was a probe plan — node-1 ran head/grep
    against /home/seeker/freetalon-workspace/freetalon/api.py, the
    WORKSPACE path for the module, which does not exist, so it failed
    exit 1; node-2 was the same probe shape against a nonexistent
    /home/seeker/freetalon-workspace/freetalon/tests/test_api.py; node-3
    was a live curl that never ran. The judge chose REPLAN and the replan
    failed 3x: (1) an invented node/action schema with no
    objective/assigned_claw, (2) a scaffold writing /health.py at the
    filesystem root, (3) {"nodes": []} — escalated FAIL. Two fixes: (1)
    the REPO CONTEXT names the repo root and the workspace explicitly,
    the grounded move and the ROUTE GOAL shape use the absolute repo
    path, and a repo-tree path named under the workspace is rejected at
    plan time as a wrong-location hit; (2) the replan prompt carries
    BOTH the ROUTE GOAL shape and the NODE SHAPE block, so the first
    replan attempt already has both shapes in front of it.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#9 v1 nodes, verbatim from
    # runs/run-20261117T073452Z-2e7e88/plan-v001.json. Embedded (not read
    # from the run directory) so the test stays hermetic in CI. The
    # commands are the JSON-parsed values (plain double quotes).
    @staticmethod
    def _retry9_v1_nodes() -> list[dict]:
        ws = "/home/seeker/freetalon-workspace"
        return [
            {
                "id": "node-1",
                "objective": "Implement the /health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": (
                        "head -n 1 " + ws + "/freetalon/api.py | grep -qx "
                        "'def health(self)' && tail -n 1 " + ws
                        + "/freetalon/api.py | grep -qx 'return {'"
                    )
                },
                "outputs": [f"{ws}/freetalon/api.py"],
                "acceptance": ["Output verified"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            },
            {
                "id": "node-2",
                "objective": "Write and run tests for the /health endpoint",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": (
                        "head -n 1 " + ws + "/freetalon/tests/test_api.py | grep -qx "
                        "'def test_health(self)' && tail -n 1 " + ws
                        + "/freetalon/tests/test_api.py | grep -qx 'return {'"
                    )
                },
                "outputs": [f"{ws}/freetalon/tests/test_api.py"],
                "acceptance": ["Output verified"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            },
            {
                "id": "node-3",
                "objective": "Verify live with curl http://localhost:8765/health",
                "depends_on": ["node-2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["verification"],
                "acceptance": ["Output verified"],
                "criteria": [{"type": "exit_code", "equals": 0}],
                "error": None,
            },
        ]

    # The exact retry-#9 replan attempt-1 output, verbatim from the
    # second RAW PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry9-2026-11-16.log: an invented
    # node/action schema with no objective and no assigned_claw.
    # Embedded so the test stays hermetic in CI.
    RETRY9_REPLAN1_INVENTED = (
        '{"nodes":[{"id":"1","action":"scaffold_endpoint",'
        '"description":"Implement the /health endpoint in freetalon/api.py",'
        '"status":"pending"},{"id":"2","action":"test_endpoint",'
        '"description":"Write and run tests for the /health endpoint",'
        '"status":"pending"},{"id":"3","action":"verify_live",'
        '"description":"Verify live with curl http://localhost:8765/health",'
        '"status":"pending"}]}'
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
        self.api_path = str(
            Path(__file__).resolve().parent.parent / "freetalon" / "api.py"
        )
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix 1a: the REPO CONTEXT names both roots ---

    def test_repo_context_names_repo_root_and_workspace(self) -> None:
        from freetalon.orchestrator.repo_index import (
            build_repo_index,
            format_repo_context,
        )

        context = format_repo_context(build_repo_index(), workspace=self.WS)
        self.assertIn("Repo root:", context)
        self.assertIn(self.repo_root, context)
        self.assertIn("Workspace:", context)
        self.assertIn(self.WS, context)
        self.assertIn("NOT the repo root", context)

    def test_repo_context_without_workspace_still_names_repo_root(self) -> None:
        from freetalon.orchestrator.repo_index import (
            build_repo_index,
            format_repo_context,
        )

        context = format_repo_context(build_repo_index())
        self.assertIn("Repo root:", context)
        self.assertIn(self.repo_root, context)

    # --- Fix 1b: workspace-prefixed module paths are wrong-location hits ---

    def test_parse_rejects_retry9_plan_wrong_location(self) -> None:
        # The exact retry-#9 v1 plan: node-1 greps the module under the
        # workspace path, node-2 the same shape for the tests file. Both
        # are repo-tree paths under the workspace root, and the workspace
        # copy does not exist — the plan must be rejected at plan time
        # naming the wrong location and the repo root.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry9_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("2 errors", message)
        self.assertIn("workspace", message)
        self.assertIn("repo root", message)
        self.assertIn(self.repo_root, message)
        self.assertIn(f"{self.WS}/freetalon/api.py", message)
        self.assertIn(f"{self.WS}/freetalon/tests/test_api.py", message)

    def test_workspace_module_plan_repairs_to_repo_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry9_v1_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("repo root", repair_prompt)
        self.assertIn(self.repo_root, repair_prompt)
        self.assertIn(f"{self.WS}/freetalon/api.py", repair_prompt)
        # The first attempt already carried the repo-root grounding.
        first_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("Repo root:", first_prompt)
        self.assertIn(self.repo_root, first_prompt)
        self.assertIn("NOT the repo root", first_prompt)

    # --- Fix 2: the replan prompt carries ROUTE GOAL + NODE SHAPE ---

    def test_replan_prompt_carries_route_and_node_shapes(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        route = TestPlannerRouteEngagementFix()
        route.setUp()
        good = self._dag(route._good_delta_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            nodes = replan_from_brief(route._brief())
        self.assertEqual(len(nodes), 2)
        prompt = call_mock.call_args[0][1]
        self.assertIn("ROUTE GOAL", prompt)
        self.assertIn("NODE SHAPE", prompt)
        self.assertIn('"assigned_claw"', prompt)
        self.assertIn('"id": "step_1"', prompt)
        self.assertIn('There is NO "type" field', prompt)
        # The node shape is general: a non-route replan carries it too,
        # while the ROUTE GOAL shape stays route-scoped.
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
                    "responds_to": "node-1",
                    "differs_by": "run the suite to confirm the failure",
                }
            ]
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=plain
        ) as call_mock:
            nodes = replan_from_brief(non_route_brief)
        self.assertEqual(len(nodes), 1)
        prompt = call_mock.call_args[0][1]
        self.assertIn("NODE SHAPE", prompt)
        self.assertNotIn("ROUTE GOAL", prompt)

    def test_replan_repairs_invented_schema_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        route = TestPlannerRouteEngagementFix()
        route.setUp()
        good = self._dag(route._good_delta_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[self.RETRY9_REPLAN1_INVENTED, good],
        ) as call_mock:
            nodes = replan_from_brief(route._brief())
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        first_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("ROUTE GOAL", first_prompt)
        self.assertIn("NODE SHAPE", first_prompt)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("NODE SHAPE", repair_prompt)

    # --- Fix 1b: genuine generated files are NOT flagged ---

    def test_workspace_generated_file_is_not_flagged(self) -> None:
        # A heredoc that writes a genuine generated file under the
        # workspace — test_health.py — is not a repo-tree path: its first
        # component is not freetalon/ or tests/. The heredoc body even
        # imports from freetalon, which is file content, not a path the
        # node reads.
        node = {
            "id": "step_1",
            "objective": "Write the health endpoint test file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": TestPlannerHeredocFix.HEREDOC_TEST},
            "outputs": [f"{self.WS}/test_health.py"],
            "criteria": [{"type": "file_exists", "path": f"{self.WS}/test_health.py"}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(nodes), 1)

    def test_parse_accepts_valid_heredoc_health_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_rejects_retry8_stdout_echo_fake(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerUnwrittenFileFix._retry8_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("unmeasurable", str(caught.exception))

    def test_parse_rejects_retry7_invented_schema_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerNodeShapeFix._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("every node MUST be a PlanNode object", message)
        self.assertIn('There is NO "type" field', message)

    def test_parse_still_rejects_retry5_fake(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

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
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

class TestPlannerDependsOnFix(unittest.TestCase):
    """Show the depends_on shape; unknown dependencies are numbered rejections (retry #10).

    Retry #10 (log llm-end-to-end-health-retry10-2026-11-16.log) failed
    at planning (~80s, DRIVER_EXIT:1, no run created). Intake succeeded.
    Planner attempts: (1) {"nodes": []} -- empty; (2) the stdout-echo fake
    again, no criteria on any node -- rejected; (3) a comment-only plan
    whose step_1/2 commands are just "# Add code..." comments, with
    file_exists criteria on the CORRECT repo paths -- the repo-root fix
    IS steering -- but step_2 depends_on ["endpoint_implemented"], an
    OUTPUT name, not a node id (step_3 the same with "tests_written"),
    so normalize_plan_nodes raised "Planner node 'step_2' depends on
    unknown node 'endpoint_implemented'" before validation could produce
    a numbered rejection -> repair budget exhausted. Two fixes, same
    family as the bare-dict criteria fix (798a116) and the node-shape
    fix (091ebfd): (1) the planner prompt (and the replan prompt / NODE
    SHAPE block) shows the depends_on shape -- node ids only, never
    output names; (2) unknown-dependency errors are collected into ONE
    numbered rejection naming the node, the bad value, the available
    node ids, and -- when the value is an output of some node -- which
    node produces it and the id to depend on instead.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#10 attempt-3 nodes, verbatim from the third RAW
    # PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry10-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI.
    @staticmethod
    def _retry10_attempt3_nodes() -> list[dict]:
        api = "/home/seeker/projects/FreeTalon/freetalon/api.py"
        return [
            {
                "id": "step_1",
                "objective": "Implement the /health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "# Add code to freetalon/api.py to implement the /health endpoint"},
                "outputs": ["endpoint_implemented"],
                "acceptance": ["Endpoint implemented successfully."],
                "criteria": [{"type": "file_exists", "path": api}],
                "error": None,
            },
            {
                "id": "step_2",
                "objective": "Write unit tests for the /health endpoint",
                "depends_on": ["endpoint_implemented"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "# Add unit tests to freetalon/api.py for the /health endpoint"},
                "outputs": ["tests_written"],
                "acceptance": ["Unit tests written successfully."],
                "criteria": [{"type": "file_exists", "path": api}],
                "error": None,
            },
            {
                "id": "step_3",
                "objective": "Verify the /health endpoint is live with curl",
                "depends_on": ["tests_written"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["endpoint_live"],
                "acceptance": ["Endpoint is live and returns a 200 OK response."],
                "criteria": [{"type": "process_exit", "code": 0}],
                "error": None,
            },
        ]

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
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix: the depends_on shape is shown, unknown deps are numbered ---

    def test_parse_rejects_retry10_attempt3_numbered(self) -> None:
        # The exact retry-#10 attempt-3 output: two unknown dependencies,
        # both output names. This must be a numbered rejection naming the
        # node, the bad value, the available node ids, and the producing
        # node -- NOT a bare structural raise.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry10_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 2 unknown-dependency + 2 unusable file_exists + 2 comment-only
        # commands + 1 bad criterion type -- 7 on seeker7, 9 in CI (see
        # _expected_retry10_error_count).
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("fix ALL of them", message)
        self.assertIn("depends on unknown node 'endpoint_implemented'", message)
        self.assertIn("depends on unknown node 'tests_written'", message)
        self.assertIn("The nodes in this plan are: step_1, step_2, step_3", message)
        self.assertIn("depend on 'step_1' (the node id)", message)
        self.assertIn("depend on 'step_2' (the node id)", message)
        self.assertIn('"depends_on" lists NODE IDS only', message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_output_name_depends_on_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry10_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertEqual(nodes[1].depends_on, ["node-1"])
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("unknown node", repair_prompt)
        self.assertIn("endpoint_implemented", repair_prompt)
        self.assertIn('"depends_on" lists NODE IDS only', repair_prompt)
        # The first attempt already carried the depends_on shape in the
        # system prompt (schema rules).
        system_prompt = call_mock.call_args_list[0][0][0]
        self.assertIn('"depends_on" lists NODE IDS only', system_prompt)
        self.assertIn("never an output name", system_prompt)

    # --- Fix: the shape is in the prompts ---

    def test_prompts_carry_depends_on_shape(self) -> None:
        from freetalon.orchestrator.planner import _DEPENDS_ON_SHAPE, _NODE_SHAPE_EXAMPLE
        from freetalon.orchestrator.prompts import PLANNER_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT

        self.assertIn('"depends_on" lists NODE IDS only', PLANNER_SYSTEM_PROMPT)
        self.assertIn("never an output name", PLANNER_SYSTEM_PROMPT)
        self.assertIn('"depends_on" lists node ids only', REPLAN_SYSTEM_PROMPT)
        self.assertIn("never an output name", REPLAN_SYSTEM_PROMPT)
        self.assertIn('"depends_on" lists NODE IDS only', _DEPENDS_ON_SHAPE)
        self.assertIn('"depends_on" lists node ids only', _NODE_SHAPE_EXAMPLE)

    def test_replan_prompt_carries_depends_on_shape(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        route = TestPlannerRouteEngagementFix()
        route.setUp()
        good = self._dag(route._good_delta_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=good
        ) as call_mock:
            nodes = replan_from_brief(route._brief())
        self.assertEqual(len(nodes), 2)
        prompt = call_mock.call_args[0][1]
        self.assertIn("NODE SHAPE", prompt)
        self.assertIn("node ids only", prompt)

    # --- Valid depends_on still passes ---

    def test_parse_accepts_valid_depends_on(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[1].depends_on, ["node-1"])
        self.assertEqual(nodes[2].depends_on, ["node-2"])
        self.assertEqual(nodes[3].depends_on, ["node-3"])

    def test_parse_accepts_plan_without_depends_on(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Run the test suite",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "python3 -m unittest discover -s tests"},
            "outputs": ["test results"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text="Run the test suite", workspace=self.WS
        )
        self.assertEqual(len(nodes), 1)

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_rejects_retry9_plan_wrong_location(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerRepoRootFix._retry9_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("2 errors", message)
        self.assertIn("repo root", message)

    def test_parse_rejects_retry8_stdout_echo_fake(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerUnwrittenFileFix._retry8_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("unmeasurable", str(caught.exception))

    def test_parse_rejects_retry7_invented_schema_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerNodeShapeFix._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("every node MUST be a PlanNode object", message)
        self.assertIn('There is NO "type" field', message)

    def test_parse_still_rejects_retry5_fake(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

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
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    # --- Structural errors still raise first, unchanged ---

    def test_normalize_still_raises_duplicate_ids_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )

    def test_normalize_still_raises_dependency_cycles_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )

    def test_normalize_still_raises_self_dependency_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )

class TestPlannerShapeSurvivesRepair(unittest.TestCase):
    """The repair prompt carries the full node shape; dropped fields are named (retry #11).

    Retry #11 (log llm-end-to-end-health-retry11-2026-11-16.log) failed
    at planning (~50s, DRIVER_EXIT:1, no run created). Intake succeeded.
    Planner attempts: (1) {"nodes": []} -- empty; (2) the stdout-echo
    fake again, no criteria on any node, depends_on with output names
    again -- rejected; (3) a NEW invented schema: nodes with
    description/output fields and no objective, no assigned_claw, no
    inputs, no criteria -- but depends_on now uses correct node ids
    (["step_1"] / ["step_2"]). The depends_on fix IS steering. The
    node-shape fix worked as designed: attempt 3 was an 8-error numbered
    rejection with the node shape appended, NOT a structural raise -- but
    the repair budget was already spent. The model fixes the named error
    but drops the rest of the PlanNode shape. Fix: the repair prompt
    itself (in _call_llm_with_repair, used by both plan_task_intent and
    replan_from_brief) carries the full minimal valid node, and when a
    repair round's output drops a required field the previous attempt
    had, the prompt names the dropped fields.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#11 attempt-3 nodes, verbatim from the third RAW
    # PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry11-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI.
    @staticmethod
    def _retry11_attempt3_nodes() -> list[dict]:
        return [
            {
                "id": "step_1",
                "description": "Implement /health endpoint in /home/seeker/projects/FreeTalon/freetalon/api.py on port 8765",
                "depends_on": [],
                "output": "endpoint_implemented",
            },
            {
                "id": "step_2",
                "description": "Add tests for the endpoint",
                "depends_on": ["step_1"],
                "output": "tests_added",
            },
            {
                "id": "step_3",
                "description": "Verify live with curl http://localhost:8765/health",
                "depends_on": ["step_2"],
            },
        ]

    # The exact retry-#11 attempt-2 nodes (the stdout-echo fake), verbatim
    # from the second RAW PLANNER OUTPUT block of the same log. It has
    # objective/assigned_claw/inputs on every node but no criteria, and
    # depends_on with output names -- the fields attempt 3 then dropped.
    @staticmethod
    def _retry11_attempt2_nodes() -> list[dict]:
        return [
            {
                "id": "step_1",
                "objective": "Implement /health endpoint in freetalon/api.py to return JSON reporting service status, OLLAMA reachability and GPU temperature.",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "echo \"{\\\"status\\\": \\\"OK\\\", \\\"ollama_reachable\\\": true, \\\"gpu_temp\\\": 25}\""},
                "outputs": ["endpoint_implemented"],
                "acceptance": ["Endpoint returns JSON with status, OLLAMA reachability and GPU temperature."],
            },
            {
                "id": "step_2",
                "objective": "Add unit tests for the /health endpoint.",
                "depends_on": ["endpoint_implemented"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "echo \"# Unit tests for /health endpoint\""},
                "outputs": ["tests_added"],
                "acceptance": ["Unit tests are added for the /health endpoint."],
            },
            {
                "id": "step_3",
                "objective": "Verify live with curl http://localhost:8765/health.",
                "depends_on": ["tests_added"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["endpoint_live"],
                "acceptance": ["Endpoint is live and returns JSON."],
            },
        ]

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
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix: the repair prompt carries the node shape ---

    def test_parse_rejects_retry11_attempt3_numbered_with_shape(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry11_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("fix ALL of them", message)
        self.assertIn("missing required field 'objective'", message)
        self.assertIn("has an unknown field 'description'", message)
        self.assertIn("has an unknown field 'output'", message)
        self.assertIn("every node MUST be a PlanNode object", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry11_attempt3_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry11_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertEqual(nodes[1].depends_on, ["node-1"])
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("missing required field 'objective'", repair_prompt)
        # The repair prompt itself carries the full minimal valid node.
        self.assertIn("NODE SHAPE", repair_prompt)
        self.assertIn('"objective": "<what the node delivers>"', repair_prompt)
        self.assertIn("Every node must match the NODE SHAPE above", repair_prompt)
        self.assertIn("do not invent a field outside it", repair_prompt)
        # No previous attempt on this round, so no dropped-fields note.
        # (The "do not drop a field" instruction is in every repair
        # prompt now; the note itself starts "Your previous attempt".)
        self.assertNotIn("Your previous attempt had", repair_prompt)

    def test_shape_survives_repair_rounds_fake_then_invention(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        fake = self._dag(self._retry11_attempt2_nodes())
        invented = self._dag(self._retry11_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[fake, invented, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        first_repair = call_mock.call_args_list[1][0][1]
        second_repair = call_mock.call_args_list[2][0][1]
        # Both repair rounds carry the node shape in front of the model.
        for repair_prompt in (first_repair, second_repair):
            self.assertIn("NODE SHAPE", repair_prompt)
            self.assertIn("Every node must match the NODE SHAPE above", repair_prompt)
        # The first repair round has no previous attempt, so no note.
        self.assertNotIn("Your previous attempt had", first_repair)
        # The second names the fields the fake had and the invention dropped.
        self.assertIn("do not drop", second_repair)
        self.assertIn("Your previous attempt had", second_repair)
        self.assertIn("'objective'", second_repair)
        self.assertIn("'assigned_claw'", second_repair)
        self.assertIn("'inputs'", second_repair)

    def test_dropped_fields_note(self) -> None:
        from freetalon.orchestrator.planner import _dropped_fields_note

        fake = self._dag(self._retry11_attempt2_nodes())
        invented = self._dag(self._retry11_attempt3_nodes())
        self.assertEqual(_dropped_fields_note(None, fake), "")
        self.assertEqual(_dropped_fields_note("not json", fake), "")
        self.assertEqual(_dropped_fields_note(fake, fake), "")
        self.assertEqual(_dropped_fields_note(invented, fake), "")
        note = _dropped_fields_note(fake, invented)
        self.assertIn("Your previous attempt had", note)
        self.assertIn("'objective'", note)
        self.assertIn("'assigned_claw'", note)
        self.assertIn("'inputs'", note)
        self.assertIn("do not drop", note)

    def test_replan_repair_prompt_carries_node_shape(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        route = TestPlannerRouteEngagementFix()
        route.setUp()
        bad = self._dag(self._retry11_attempt3_nodes())
        good = self._dag(route._good_delta_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            nodes = replan_from_brief(route._brief())
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 2)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("NODE SHAPE", repair_prompt)
        self.assertIn("Every node must match the NODE SHAPE above", repair_prompt)
        self.assertIn('"objective": "<what the node delivers>"', repair_prompt)

    # --- Valid plans still pass untouched ---

    def test_parse_accepts_grounded_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[1].depends_on, ["node-1"])

    # --- Regression guards: the earlier fixes still hold ---

    def test_parse_rejects_retry10_attempt3_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerDependsOnFix._retry10_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 2 unknown-dependency + 2 unusable file_exists + 2 comment-only
        # commands + 1 bad criterion type -- 7 on seeker7, 9 in CI (see
        # _expected_retry10_error_count).
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("depends on unknown node 'endpoint_implemented'", message)
        self.assertIn("depend on 'step_1' (the node id)", message)
        self.assertIn('"depends_on" lists NODE IDS only', message)

    def test_parse_rejects_retry9_plan_wrong_location(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerRepoRootFix._retry9_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("2 errors", message)
        self.assertIn("repo root", message)

    def test_parse_rejects_retry8_stdout_echo_fake(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerUnwrittenFileFix._retry8_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("unmeasurable", str(caught.exception))

    def test_parse_rejects_retry7_invented_schema_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerNodeShapeFix._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("every node MUST be a PlanNode object", message)
        self.assertIn('There is NO "type" field', message)

    def test_parse_still_rejects_retry5_fake(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

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
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    # --- Structural errors still raise first, unchanged ---

    def test_normalize_still_raises_duplicate_ids_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )

    def test_normalize_still_raises_dependency_cycles_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )

    def test_normalize_still_raises_self_dependency_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )

class TestPlannerStandinOverwriteFix(unittest.TestCase):
    """Reject a literal answer written into a code file; cheap rejections are free (retry #12).

    Retry #12 (log llm-end-to-end-health-retry12-2026-11-16.log) failed
    at planning (~70s, DRIVER_EXIT:1, no run created). Intake succeeded.
    Planner attempts: (1) {"nodes": []} -- empty; (2) the stdout-echo
    fake in a new disguise -- echo '{"status": "OK", "ollama":
    "reachable", "gpu_temp": "available"}' > /home/seeker/projects/
    FreeTalon/freetalon/api.py, the JSON answer redirected INTO the
    serving module itself, with node ids as integers (1, 2) and
    depends_on: [1] -- rejected with 3 errors (id must be a valid string
    x2, depends_on.0 must be a valid string), but the stand-in rule did
    NOT fire (criteria is exit_code, not file_exists), so only the
    int-id type error caught it -- string ids would have let the plan
    through and destroyed api.py; (3) a scaffold plan writing a Flask
    app to freetalon/api.py -- otherwise well-shaped, rejected with 1
    error by the framework rule -- but the repair budget (max 2) was
    already spent on the first two junk attempts, so the model never got
    a repair round for it. Two fixes: (1) the stand-in rule is widened
    -- a literal answer written into a CODE file is rejected by name as
    overwriting code with a hardcoded answer, regardless of criteria,
    for bash and scaffold writes alike (data/generated files are exempt:
    writing a JSON config to a new file is legitimate file production);
    (2) cheap rejections -- an empty plan, or a rejection whose only
    errors are stand-in / code-overwrite fakes -- do not spend the
    repair budget: they are re-prompted for free, up to 2 free
    re-prompts, so the [empty, fake, Flask] sequence reaches a grounded
    plan within budget. The max-2 repair budget is NOT raised.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    API_PATH = "/home/seeker/projects/FreeTalon/freetalon/api.py"

    # The exact retry-#12 attempt-2 nodes, verbatim from the second RAW
    # PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry12-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI. Values are
    # the JSON-parsed values (plain double quotes), not the log's
    # backslash-escaped spellings.
    @staticmethod
    def _retry12_attempt2_nodes() -> list[dict]:
        return [
            {
                "id": 1,
                "objective": "Create a health endpoint in the API",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "echo '{\"status\": \"OK\", \"ollama\": \"reachable\", \"gpu_temp\": \"available\"}' > /home/seeker/projects/FreeTalon/freetalon/api.py"},
                "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
                "acceptance": "The health endpoint is created",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": 2,
                "objective": "Verify the health endpoint",
                "depends_on": [1],
                "assigned_claw": "bash",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["http://localhost:8765/health"],
                "acceptance": "The health endpoint returns JSON",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    @staticmethod
    def _retry12_attempt2_string_ids_nodes() -> list[dict]:
        # Same plan with the ids corrected to strings: the schema errors
        # are gone, so only the code-overwrite rule can catch it.
        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        nodes[0]["id"] = "step_1"
        nodes[1]["id"] = "step_2"
        nodes[1]["depends_on"] = ["step_1"]
        return nodes

    # The exact retry-#12 attempt-3 nodes (the Flask scaffold), verbatim
    # from the third RAW PLANNER OUTPUT block of the same log.
    @staticmethod
    def _retry12_attempt3_nodes() -> list[dict]:
        return [
            {
                "id": "step_1",
                "objective": "Create a health endpoint file.",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "/home/seeker/projects/FreeTalon/freetalon/api.py",
                    "content": "from flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/health')\ndef health():\n return 'OK'\n",
                },
                "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
                "acceptance": "The /home/seeker/projects/FreeTalon/freetalon/api.py file exists and contains the health endpoint code.",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_2",
                "objective": "Run the Flask application to serve the /health endpoint.",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {"command": "python /home/seeker/projects/FreeTalon/freetalon/api.py"},
                "outputs": ["The Flask application is running and serving the /health endpoint."],
                "acceptance": "The Flask application is running and accessible on port 8765.",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

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
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix 1: a literal answer into a code file is a stand-in ---

    def test_parse_rejects_retry12_attempt2_int_ids(self) -> None:
        # Verbatim attempt 2: the int-id schema errors fire AND the
        # code-overwrite stand-in error fires -- the fake is named as a
        # fake even though its ids are the wrong type (retry #13: the
        # schema raise used to mask the stand-in check, so the fake spent
        # a repair round instead of a free re-prompt). The rejection is
        # cheap: 1 stand-in error + 3 schema errors.
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(self._retry12_attempt2_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("Input should be a valid string", message)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("grounded move", message)
        self.assertIn("do_GET", message)

    def test_parse_rejects_retry12_attempt2_string_ids_as_code_overwrite(self) -> None:
        # Same plan with string ids: the schema errors are gone, so the
        # code-overwrite rule must name the write into api.py -- the
        # criterion type (exit_code, not file_exists) must not be the
        # escape hatch.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry12_attempt2_string_ids_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_string_ids_rejection_is_cheap(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError):
            parse_planner_response(
                self._dag(self._retry12_attempt2_string_ids_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_empty_plan_rejection_is_cheap(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                '{"nodes": []}', goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("at least one plan node", str(caught.exception))

    def test_scaffold_json_literal_into_code_file_is_rejected(self) -> None:
        # The same destruction via the scaffold claw: JSON content into
        # the serving module must be rejected by name, not just bash.
        node = {
            "id": "step_1",
            "objective": "Create a health endpoint in the API",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.API_PATH,
                "content": "{\"status\": \"OK\", \"ollama\": \"reachable\"}",
            },
            "outputs": [self.API_PATH],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("overwrites code with a hardcoded answer", str(caught.exception))

    def test_code_overwrite_fake_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry12_attempt2_string_ids_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("overwrites code with a hardcoded answer", repair_prompt)

    # --- Fix 2: junk attempts do not spend the repair budget ---

    def test_empty_fake_flask_sequence_repairs_within_budget(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        empty = '{"nodes": []}'
        fake = self._dag(self._retry12_attempt2_string_ids_nodes())
        flask = self._dag(self._retry12_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, fake, flask, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Empty (free 1) + fake (free 2) + Flask (repair 1) + grounded:
        # 4 calls, inside the 2-repair budget with one repair to spare.
        self.assertEqual(call_mock.call_count, 4)
        self.assertEqual(len(plan.nodes), 4)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        self.assertEqual(plan.nodes[0].inputs["file_path"], self.api_path)
        first_repair = call_mock.call_args_list[1][0][1]
        second_repair = call_mock.call_args_list[2][0][1]
        third_repair = call_mock.call_args_list[3][0][1]
        self.assertIn("at least one plan node", first_repair)
        self.assertIn("overwrites code with a hardcoded answer", second_repair)
        self.assertIn("writes a Flask app", third_repair)

    def test_verbatim_int_ids_sequence_repairs_within_budget(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        empty = '{"nodes": []}'
        fake = self._dag(self._retry12_attempt2_nodes())
        flask = self._dag(self._retry12_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, fake, flask, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Empty (free 1) + fake (free 2 -- the stand-in error fires
        # despite the int ids, so the fake is cheap now) + Flask
        # (repair 1) + grounded: 4 calls, inside the 2-repair budget
        # with one repair to spare.
        self.assertEqual(call_mock.call_count, 4)
        self.assertEqual(len(plan.nodes), 4)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")

    def test_free_reprompt_cap_bounds_endless_junk(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        empty = '{"nodes": []}'
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty] * 6,
        ) as call_mock:
            with self.assertRaises(LLMResponseError):
                plan_task_intent(self.intent, settings=self.settings)
        # 1 initial + 2 free re-prompts + 2 repairs = 5 calls, then raise.
        self.assertEqual(call_mock.call_count, 5)

    # --- Legitimate writes still pass ---

    def test_parse_accepts_grounded_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)

    def test_parse_accepts_heredoc_json_config_to_new_file(self) -> None:
        # A heredoc writing a JSON config to a NEW generated file is
        # legitimate file production: the target is not a code file.
        ws = self.WS
        nodes = self._grounded_health_nodes()
        nodes.append(
            {
                "id": "step_5",
                "objective": "Write the service config file",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cat > " + ws + "/config.json <<'EOF'\n"
                        "{\"port\": 8765, \"host\": \"127.0.0.1\"}\n"
                        "EOF"
                    )
                },
                "outputs": [ws + "/config.json"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            }
        )
        parsed, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 5)

    def test_parse_accepts_scaffold_json_config_to_new_file(self) -> None:
        # A scaffold writing a JSON config to a NEW generated file, and
        # claiming file_exists on the file it wrote, is the scaffold
        # claw's normal pattern -- not a stand-in.
        ws = self.WS
        nodes = self._grounded_health_nodes()
        nodes.append(
            {
                "id": "step_5",
                "objective": "Write the service settings file",
                "depends_on": ["step_1"],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": ws + "/settings.json",
                    "content": "{\"debug\": true}",
                },
                "outputs": [ws + "/settings.json"],
                "criteria": [{"type": "file_exists", "path": ws + "/settings.json"}],
            }
        )
        parsed, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 5)

    # --- Regression guards ---

    def test_parse_rejects_retry11_attempt3_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("missing required field 'objective'", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_parse_rejects_retry10_attempt3_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerDependsOnFix._retry10_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("depends on unknown node", message)
        self.assertIn("depends_on", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_parse_rejects_retry9_plan_wrong_location(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(TestPlannerRepoRootFix._retry9_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_rejects_retry8_stdout_echo_fake(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerUnwrittenFileFix._retry8_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("unmeasurable", str(caught.exception))

    def test_parse_rejects_retry7_invented_schema_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerNodeShapeFix._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("every node MUST be a PlanNode object", message)

    def test_parse_still_rejects_retry5_fake(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

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
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_normalize_still_raises_duplicate_ids_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )

    def test_normalize_still_raises_dependency_cycles_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )

    def test_normalize_still_raises_self_dependency_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )



class TestPlannerFrameworkSteerFix(unittest.TestCase):
    """Name the grounded move for a framework rejection; stand-in check runs despite schema errors (retry #13).

    Retry #13 (log llm-end-to-end-health-retry13-2026-11-16.log) failed
    at planning (~86s, DRIVER_EXIT:1, no run created). Intake succeeded.
    Planner attempts: (1) {"nodes": []} -- empty, CHEAP rejection, free
    re-prompt (the new fix worked as designed); (2) the retry-#12 echo
    fake verbatim -- echo '{"status": "OK", ...}' >
    /home/seeker/projects/FreeTalon/freetalon/api.py with integer ids --
    rejected with 3 schema errors, but the code-overwrite stand-in error
    did NOT fire (schema validation raises before the stand-in check
    runs), so it still spent a repair round; (3) a scaffold plan writing
    a Flask app to freetalon/api.py -- otherwise well-shaped, rejected
    with 1 error by the framework rule, naming stdlib http.server; (4) a
    MORE elaborate Flask scaffold -- Ollama/nvidia-smi subprocess checks,
    app.run(debug=True, host='0.0.0.0', port=8765) -- rejected with 1
    error by the framework rule again. The model got a repair round for
    the Flask error and used it to double down on Flask instead of
    switching to http.server; budget exhausted. Two fixes: (1) the
    framework rejection names the grounded move concretely -- extend the
    serving module in place, adding a branch to the request handler that
    already serves the route (the repo index now records each route's
    handler: for /health, the do_GET method of the Handler class defined
    inside HiveAPIServer.start), the way the anti-fake rejection names
    the grounded move; the ROUTE GOAL shape carries the same concrete
    move; (2) the stand-in check runs even when schema errors are
    present -- _validate_raw_nodes runs it on lenient views of the raw
    nodes, so a rejection containing a stand-in error is cheap
    regardless of accompanying schema errors. Genuinely structural
    errors (bad JSON, duplicate ids, dependency cycles, self-dependency)
    still raise first.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    API_PATH = "/home/seeker/projects/FreeTalon/freetalon/api.py"

    # The exact retry-#13 attempt-4 nodes (the elaborate Flask scaffold),
    # verbatim from the fourth RAW PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry13-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI. Values are the
    # JSON-parsed values (plain double quotes), not the log's
    # backslash-escaped spellings.
    @staticmethod
    def _retry13_attempt4_nodes() -> list[dict]:
        return [
            {
                "id": "node-1",
                "objective": "Define a Flask app for /health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "/home/seeker/projects/FreeTalon/freetalon/api.py",
                    "content": (
                        "# Import necessary modules\n"
                        "from flask import Flask, jsonify\n"
                        "import subprocess\n"
                        "\n"
                        "# Create a Flask app instance\n"
                        "app = Flask(__name__)\n"
                        "\n"
                        "# Define the /health endpoint\n"
                        "@app.route('/health')\n"
                        "def health():\n"
                        "try:\n"
                        "# Check if Ollama is reachable\n"
                        "result = subprocess.run(['ollama', 'version'], capture_output=True, text=True)\n"
                        "if result.returncode == 0:\n"
                        "ollama_status = \"reachable\"\n"
                        "else:\n"
                        "ollama_status = \"unreachable\"\n"
                        "\n"
                        "# Check if GPU temperature is available (nvidia-smi)\n"
                        "try:\n"
                        "result = subprocess.run(['nvidia-smi'], capture_output=True, text=True)\n"
                        "gpu_temp_available = True\n"
                        "except FileNotFoundError:\n"
                        "gpu_temp_available = False\n"
                        "\n"
                        "# Create a JSON response\n"
                        "response = jsonify({\n"
                        "\"service\": \"FreeTalon\",\n"
                        "\"status\": \"OK\",\n"
                        "\"ollama_status\": ollama_status,\n"
                        "\"gpu_temperature_available\": gpu_temp_available\n"
                        "})\n"
                        "return response\n"
                        "except Exception as e:\n"
                        "# Return an error message if something goes wrong\n"
                        "response = jsonify({\"error\": str(e)})\n"
                        "return response\n"
                        "\n"
                        "if __name__ == '__main__':\n"
                        "app.run(debug=True, host='0.0.0.0', port=8765)"
                    ),
                },
                "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
                "acceptance": "Flask app for /health is defined in freetalon/api.py",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "node-2",
                "objective": "Run the Flask app and verify /health endpoint returns JSON",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": "python /home/seeker/projects/FreeTalon/freetalon/api.py && curl http://localhost:8765/health | jq ."
                },
                "outputs": ["curl output"],
                "acceptance": "The Flask app runs and the /health endpoint returns JSON",
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

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
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the grounded-shape fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # --- Fix 2: the stand-in check runs despite schema errors ---

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        # Verbatim retry-#13 attempt 2 (== retry-#12 attempt 2): int ids
        # AND a code-overwrite fake. The schema errors used to mask the
        # stand-in check; now the rejection is cheap.
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("Input should be a valid string", message)
        self.assertIn("grounded move", message)
        self.assertIn("do_GET", message)

    def test_standin_check_runs_without_goal_or_index(self) -> None:
        # No goal text means no repo index and no grounded sentence, but
        # the stand-in error still fires and the rejection is still cheap.
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(self._dag(nodes), workspace=self.WS)
        self.assertIn("overwrites code with a hardcoded answer", str(caught.exception))

    def test_schema_errors_without_a_fake_are_not_cheap(self) -> None:
        # The cheap path is for the stand-in family only: a plain schema
        # error (int id, no fake) is still a repair-consuming rejection.
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = [
            {
                "id": 1,
                "objective": "Read the API module",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "head -5 /home/seeker/projects/FreeTalon/freetalon/api.py"},
                "outputs": ["api.py head"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            }
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("Input should be a valid string", message)
        self.assertNotIn("overwrites code", message)
        self.assertNotIsInstance(caught.exception, CheapRejectionError)

    # --- Fix 1: the framework rejection names the grounded move ---

    def test_repo_index_records_route_handlers(self) -> None:
        from freetalon.orchestrator.repo_index import build_repo_index

        index = build_repo_index()
        self.assertEqual(
            index.route_handlers["/health"],
            "the do_GET method of the Handler class (defined inside HiveAPIServer.start)",
        )
        self.assertEqual(
            index.route_handlers["/shutdown"],
            "the do_POST method of the Handler class (defined inside HiveAPIServer.start)",
        )

    def test_repo_context_names_the_handler(self) -> None:
        from freetalon.orchestrator.repo_index import (
            build_repo_index,
            format_repo_context,
        )

        context = format_repo_context(build_repo_index())
        self.assertIn("do_GET method of the Handler class", context)
        self.assertIn("HiveAPIServer.start", context)
        self.assertIn("do not rewrite it in a different web framework", context)

    def test_route_goal_shape_names_the_handler_branch(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        shape = _route_goal_shape(self.HEALTH_GOAL, delta=False)
        self.assertIn("ROUTE GOAL", shape)
        self.assertIn(
            "add a /health branch to the do_GET method of the Handler class",
            shape,
        )
        self.assertIn("HiveAPIServer.start", shape)

    def test_retry13_attempt4_rejected_with_grounded_move_named(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry13_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("stdlib http.server", message)
        self.assertIn("different web framework", message)
        self.assertIn("grounded move", message)
        self.assertIn("add a /health branch to the do_GET method", message)
        self.assertIn("HiveAPIServer.start", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_flask_scaffold_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry13_attempt4_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("writes a Flask app", repair_prompt)
        self.assertIn("add a /health branch to the do_GET method", repair_prompt)

    def test_empty_fake_flask_grounded_sequence_repairs_within_budget(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        empty = '{"nodes": []}'
        fake = self._dag(TestPlannerStandinOverwriteFix._retry12_attempt2_nodes())
        flask = self._dag(TestPlannerStandinOverwriteFix._retry12_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, fake, flask, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Empty (free 1) + int-id fake (free 2 -- the stand-in error
        # fires despite the schema errors) + Flask (repair 1) +
        # grounded: 4 calls, inside the 2-repair budget with one repair
        # to spare.
        self.assertEqual(call_mock.call_count, 4)
        self.assertEqual(len(plan.nodes), 4)
        self.assertEqual(plan.nodes[0].assigned_claw, "scaffold")
        self.assertEqual(plan.nodes[0].inputs["file_path"], self.api_path)
        first_repair = call_mock.call_args_list[1][0][1]
        second_repair = call_mock.call_args_list[2][0][1]
        third_repair = call_mock.call_args_list[3][0][1]
        self.assertIn("at least one plan node", first_repair)
        self.assertIn("overwrites code with a hardcoded answer", second_repair)
        self.assertIn("writes a Flask app", third_repair)
        self.assertIn("add a /health branch to the do_GET method", third_repair)

    # --- Legitimate writes still pass ---

    def test_parse_accepts_grounded_plan(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)

    def test_parse_accepts_scaffold_http_server_write(self) -> None:
        # A scaffold writing real http.server code to the serving module
        # is the grounded move, not a framework violation.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": (
                    "from http.server import BaseHTTPRequestHandler\n"
                    "\n"
                    "class Handler(BaseHTTPRequestHandler):\n"
                    "    def do_GET(self):\n"
                    "        pass\n"
                ),
            },
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]), goal_text=self.HEALTH_GOAL
        )
        self.assertEqual(len(nodes), 1)

    # --- Regression guards ---

    def test_parse_rejects_retry12_string_ids_as_code_overwrite(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(
                    TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
                ),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("do_GET", message)

    def test_parse_rejects_retry11_attempt3_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("missing required field 'objective'", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_parse_rejects_retry10_attempt3_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerDependsOnFix._retry10_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("depends on unknown node", message)
        self.assertIn("depends_on", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_parse_rejects_retry9_plan_wrong_location(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(TestPlannerRepoRootFix._retry9_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_rejects_retry8_stdout_echo_fake(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerUnwrittenFileFix._retry8_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("unmeasurable", str(caught.exception))

    def test_parse_rejects_retry7_invented_schema_numbered(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerNodeShapeFix._retry7_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertIn("every node MUST be a PlanNode object", message)

    def test_parse_still_rejects_retry5_fake(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "grounded move"):
            parse_planner_response(
                self._dag(TestPlannerGroundedShapeFix._retry5_fake_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_parse_still_rejects_retry4_plan_wrong_service(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "different service"):
            parse_planner_response(
                self._dag(TestPlannerRouteEngagementFix._retry4_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

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
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_still_coerces(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_normalize_still_raises_duplicate_ids_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )

    def test_normalize_still_raises_dependency_cycles_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )

    def test_normalize_still_raises_self_dependency_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )


class TestPlannerScaffoldGuardFix(unittest.TestCase):
    """Scaffold nodes are measurable; placeholders never clobber code; bash commands are shell, not prose (retry #14).

    Retry #14 (run run-20261117T090107Z-c77c7a) failed in 54.7s, goal FAIL,
    escalated: node 'node-1' unmeasurable twice in a row. First retry since
    #8 where a plan was accepted AND executed. v1 was accepted on the first
    attempt — 3 bash nodes, correct node-id depends_on, exit_code criteria;
    node-1's command named the real handler but was PROSE, not shell
    ("/home/seeker/projects/FreeTalon/freetalon/api.py:HiveAPIServer,
    _ThreadingHTTPServer - Add a /health branch to the do_GET method ..."),
    so /bin/sh failed on "(" in 0.001s. v2 was accepted: a scaffold of
    freetalon/api.py with "# Scaffolded code for /health endpoint." (39
    bytes) — measurement UNMEASURABLE (no exit code reported). v3 was
    accepted: one scaffold node, absolute file_path
    /home/seeker/projects/FreeTalon/freetalon/api.py, content "# Placeholder
    for health endpoint logic." (40 bytes) — UNMEASURABLE again, and the
    scaffold CLOBBERED the real api.py (219 lines -> 1 line); only git
    saved it. Three fixes: (1) the scaffold claw returns exit_code 0 on a
    successful write, so scaffold nodes are measurable; (2) a scaffold
    whose content is placeholder-only is rejected at plan time when its
    target is an existing code file, and refused at execution time (a
    measured FAIL, file unchanged); (3) a bash command that is prose, not
    shell, is rejected at plan time.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # The exact retry-#14 v1 nodes, verbatim from
    # runs/run-20261117T090107Z-c77c7a/plan-v001.json. Embedded (not read
    # from the run directory) so the test stays hermetic in CI.
    @staticmethod
    def _retry14_v1_nodes() -> list[dict]:
        return [
            {
                "id": "node-1",
                "objective": "Implement the /health endpoint in FreeTalon's API.",
                "depends_on": [],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": (
                        "/home/seeker/projects/FreeTalon/freetalon/api.py:HiveAPIServer, "
                        "_ThreadingHTTPServer - Add a /health branch to the do_GET method "
                        "of the Handler class (defined inside HiveAPIServer.start) that "
                        "returns JSON reporting service status, whether Ollama at "
                        "localhost:11434 is reachable, and GPU temperature when "
                        "nvidia-smi is available."
                    )
                },
                "outputs": ["verification"],
                "acceptance": ["Output verified"],
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
                "objective": "Add unit tests for the /health endpoint.",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": (
                        "/home/seeker/projects/FreeTalon/freetalon/api.py:Unit tests for "
                        "/health endpoint - Verify JSON response, Ollama reachability, "
                        "and GPU temperature reporting."
                    )
                },
                "outputs": ["verification"],
                "acceptance": ["Output verified"],
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
                "objective": "Verify the /health endpoint is live.",
                "depends_on": ["node-2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": (
                        "curl http://localhost:8765/health - Verify JSON response, "
                        "Ollama reachability, and GPU temperature reporting."
                    )
                },
                "outputs": ["verification"],
                "acceptance": ["Output verified"],
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

    # The exact retry-#14 v2 nodes, verbatim from
    # runs/run-20261117T090107Z-c77c7a/plan-v002.json. Embedded (not read
    # from the run directory) so the test stays hermetic in CI.
    @staticmethod
    def _retry14_v2_nodes() -> list[dict]:
        return [
            {
                "id": "node-1",
                "objective": "Scaffold the /health endpoint in freetalon/api.py.",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "status": "draft",
                "inputs": {
                    "file_path": "freetalon/api.py",
                    "content": "# Scaffolded code for /health endpoint.",
                },
                "outputs": ["freetalon/api.py"],
                "acceptance": ["The /health endpoint is now scaffolded in freetalon/api.py."],
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
                "objective": "Define the test for the /health endpoint.",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {
                    "command": "# Define a test command to verify the /health endpoint."
                },
                "outputs": ["test.sh"],
                "acceptance": ["A test command is defined to verify the /health endpoint."],
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
                "objective": "Run the test for the /health endpoint and verify its status.",
                "depends_on": ["node-2"],
                "assigned_claw": "bash",
                "status": "draft",
                "inputs": {"command": "./test.sh"},
                "outputs": ["/health status"],
                "acceptance": ["The /health endpoint returns a 200 OK status code."],
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

    def _retry14_v3_node(self) -> dict:
        """The retry-#14 v3 shape (plan-v003.json), retargeted to this checkout.

        The verbatim v3 node scaffolds the absolute seeker path
        /home/seeker/projects/FreeTalon/freetalon/api.py, which does not
        exist in CI — and the placeholder guard only fires when the target
        exists on disk. Retargeting to this checkout's api.py keeps the
        shape (one scaffold node, placeholder content, exit_code criterion)
        and lets the guard fire hermetically.
        """
        return {
            "id": "node-1",
            "objective": "Add a /health endpoint to FreeTalon's API.",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "status": "draft",
            "inputs": {
                "file_path": self.api_path,
                "content": "# Placeholder for health endpoint logic.",
            },
            "outputs": [self.api_path],
            "acceptance": ["The /health endpoint is now scaffolded in freetalon/api.py."],
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
        }

    # --- Fix 3: prose commands are rejected at plan time ---

    def test_retry14_v1_prose_commands_rejected(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry14_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # One prose error per node: node-1 (citation + "Add a..."), node-2
        # (citation), node-3 (real curl with a " - Verify ..." prose tail).
        self.assertIn("was rejected with 3 errors", message)
        self.assertIn("bash command is prose, not a shell command", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )
        # The rejection names the grounded move: heredoc or scaffold.
        self.assertIn("heredoc", message)
        self.assertIn("scaffold", message)

    def test_retry14_v2_placeholder_and_comment_rejected(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry14_v2_nodes()),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Node-1: placeholder scaffold to the existing freetalon/api.py.
        # Node-2: a comment alone, which does nothing.
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)
        self.assertIn("only a comment", message)

    # --- Fix 2: placeholder scaffolds are rejected at plan time ---

    def test_retry14_v3_placeholder_scaffold_rejected(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([self._retry14_v3_node()]),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    # --- Mocked repairs land on the grounded plan in one round ---

    def test_prose_command_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag(self._retry14_v1_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("prose", repair_prompt)

    def test_placeholder_scaffold_repairs_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._dag([self._retry14_v3_node()])
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("placeholder", repair_prompt)

    # --- Regression guards: every earlier fix still holds ---

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("Input should be a valid string", message)
        self.assertIn("grounded move", message)
        self.assertIn("do_GET", message)

    def test_retry13_attempt4_flask_rejected_with_handler_branch_named(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("add a /health branch to the do_GET method", message)

    def test_retry12_string_ids_rejected_as_code_overwrite(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn(
            "overwrites code with a hardcoded answer", str(caught.exception)
        )

    def test_retry11_attempt3_invented_schema_is_numbered(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("assigned_claw", message)

    def test_retry10_attempt3_unknown_dependency_is_numbered(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("endpoint_implemented", message)

    def test_retry9_v1_rejected_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("freetalon-workspace", message)

    def test_retry8_v1_rejected_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_invented_schema_is_numbered(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry5_fake_rejected_grounded_move_named(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)

    def test_retry4_plan_rejected_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("8765", message)

    def test_retry3_glued_echo_rejected_heredoc_named(self) -> None:
        heredoc = TestPlannerHeredocFix()
        node = heredoc._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_still_coerces_and_rejected(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("was rejected with", message)
        self.assertIn("criteria MUST be a JSON array", message)
        self.assertIn('[{"type": "exit_code", "equals": 0}]', message)
        self.assertIn("has no 'type'", message)

    def test_normalize_still_raises_duplicate_ids_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )

    def test_normalize_still_raises_dependency_cycles_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )

    def test_normalize_still_raises_self_dependency_first(self) -> None:
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )


class TestPlannerDuplicateKeysFix(unittest.TestCase):
    """Duplicate JSON keys collapse silently; the dependency error hides the real defects (retry #15).

    Retry #15 (log llm-end-to-end-health-retry15-2026-11-16.log) failed at
    planning: attempt 2 was a Flask heredoc plan whose three nodes were
    concatenated into a single object -- the `},{` separators between nodes
    were missing, and outputs/acceptance/criteria were nested inside
    `inputs`. The JSON parser's last-wins collapse turned three nodes into
    one node-3 depending on the nonexistent node-2, and the only error the
    model saw was the unknown dependency -- nothing about the real defect
    (a Flask heredoc to /tmp). Two fixes: (1) the raw planner output is
    scanned for duplicate JSON keys before the normal parse collapses it,
    and a duplicate-key collapse is rejected by name, naming the duplicated
    keys and the likely cause (missing `},{` separators; fields nested
    inside `inputs`); (2) when normalize raises an unknown-dependency
    error, the rejection also names the plan's other validation errors
    (framework, /tmp, prose, stand-in) found in the lenient node views,
    so the repair round sees the real defects in the same rejection.
    Duplicate ids, dependency cycles, and self-dependency still raise
    first, as before.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#15 attempt-2 raw planner output, read from the log
    # (logs/llm-end-to-end-health-retry15-2026-11-16.log) and embedded here
    # so the test stays hermetic in CI.
    RETRY15_ATTEMPT2_RAW = r"""{"metadata":{},"nodes":[{"id":"node-1","objective":"Add /health endpoint to FreeTalon API","depends_on":[],"assigned_claw":"bash","inputs":{"command":"cat > /tmp/freetalon_api.py << 'EOF\nfrom flask import Flask\nimport psutil\n\napp = Flask(__name__)\n\n@app.route('/health')\ndef health():\ntry:\ngpu_temp = psutil.sensors_temperatures()['gpu'][0].current\nreturn f'{\"status\": \"OK\", \"gpu_temp\": {gpu_temp}}'\nexcept Exception as e:\nreturn '{\"status\": \"ERROR\"}'\n\nif __name__ == '__main__':\napp.run(host='0.0.0.0', port=8765)'","outputs":["/tmp/freetalon_api.py"],"acceptance":["File /tmp/freetalon_api.py contains the health endpoint code."],"criteria":[{"type":"exit_code","equals":0}]},"id":"node-2","objective":"Create unit tests for /health endpoint","depends_on":["node-1"],"assigned_claw":"bash","inputs":{"command":"cat > /tmp/freetalon_api_test.py << 'EOF\nimport unittest\nfrom freetalon_api import app\n\nclass TestHealthEndpoint(unittest.TestCase):\ndef test_health(self):\nresponse = app.get('/health')\nself.assertEqual(response.status_code, 200)\ndata = response.get_json()\nself.assertEqual(data['status'], 'OK')\n\nif __name__ == '__main__':\nunittest.main()'","outputs":["/tmp/freetalon_api_test.py"],"acceptance":["File /tmp/freetalon_api_test.py contains the unit tests for the health endpoint."],"criteria":[{"type":"exit_code","equals":0}]},"id":"node-3","objective":"Verify JSON response, Ollama reachability, and GPU temperature reporting","depends_on":["node-2"],"assigned_claw":"bash","inputs":{"command":"curl http://localhost:8765/health | jq '.status' && curl http://localhost:8765/health | jq '.gpu_temp'","outputs":["response from curl commands"],"acceptance":["JSON response contains 'OK' and GPU temperature is reported."],"criteria":[{"type":"exit_code","equals":0}]}}]
}"""

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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    def _merged_two_node_raw(self) -> str:
        """Two nodes merged into one object (the `},{` separator removed)."""
        import json

        node1 = {
            "id": "node-1",
            "objective": "Extend the health endpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.api_path, "content": "x = 1\n"},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        node2 = {
            "id": "node-2",
            "objective": "Run the tests",
            "depends_on": ["node-1"],
            "assigned_claw": "bash",
            "inputs": {"command": "python3 -m unittest discover -s tests"},
            "outputs": ["test results"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        first = json.dumps(node1)
        second = json.dumps(node2)
        merged_inner = first[:-1] + ", " + second[1:]
        return '{"nodes": [' + merged_inner + '], "metadata": {}}'

    def _flask_tmp_unknown_dep_nodes(self) -> list[dict]:
        """A Flask heredoc to /tmp whose node depends on a missing node."""
        return [
            {
                "id": "node-1",
                "objective": "Write the Flask health app to /tmp",
                "depends_on": ["node-0"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cat > /tmp/freetalon_api.py <<'EOF'\n"
                        "from flask import Flask\n"
                        "app = Flask(__name__)\n"
                        "\n"
                        "@app.route(\"/health\")\n"
                        "def health():\n"
                        "    return {\"status\": \"ok\"}\n"
                        "EOF"
                    )
                },
                "outputs": ["/tmp/freetalon_api.py"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "node-2",
                "objective": "Run the Flask app",
                "depends_on": ["node-1"],
                "assigned_claw": "bash",
                "inputs": {"command": "python3 /tmp/freetalon_api.py"},
                "outputs": ["app running"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    # --- (a) The exact retry-#15 attempt-2 output ---

    def test_retry15_attempt2_rejected_as_duplicate_key_collapse(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Named as a duplicate-key collapse, not only as an unknown dependency.
        self.assertIn("duplicate JSON keys", message)
        self.assertIn("node object (id 'node-3')", message)
        for key in ("id", "objective", "depends_on", "assigned_claw", "inputs"):
            self.assertIn(f"'{key}' (3 times)", message)
        # The likely cause is named: the missing separators, and the nesting.
        self.assertIn("},{", message)
        self.assertIn("separate each node", message)
        self.assertIn("nests", message)
        self.assertIn('inside "inputs"', message)
        # The misleading dependency error is pre-empted, not surfaced.
        self.assertNotIn("depends on unknown node", message)
        self.assertNotIn("did not match PlanNode schema", message)

    # --- (b) A merged-node plan repairs to a grounded plan in one round ---

    def test_merged_nodes_repair_to_grounded_plan_in_one_round(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        bad = self._merged_two_node_raw()
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        self.assertIn("curl -s http://localhost:8765/health", nodes[-1].inputs["command"])
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("duplicate JSON keys", repair_prompt)
        self.assertIn("'id' (2 times)", repair_prompt)
        self.assertIn("},{", repair_prompt)

    # --- (c) Unknown dependency AND framework//tmp in the same rejection ---

    def test_unknown_dependency_rejection_also_names_framework_and_tmp(self) -> None:
        nodes = self._flask_tmp_unknown_dep_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("depends on unknown node", message)
        self.assertIn("node-0", message)
        # The real defects ride along in the same rejection.
        self.assertIn("Flask", message)
        self.assertIn("/tmp", message)

    # --- (d) Valid plans still pass untouched ---

    def test_valid_grounded_plan_passes_untouched(self) -> None:
        nodes, _metadata = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)

    def test_plan_without_duplicate_keys_and_correct_depends_on_passes(self) -> None:
        nodes = [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "content": GROUNDED_API_CONTENT},
                "outputs": [self.api_path],
                "criteria": [{"type": "file_exists", "path": self.api_path}],
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
        parsed, _metadata = parse_planner_response(
            self._dag(nodes),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(parsed), 2)
        # parse_planner_response renames node ids to node-1, node-2, so
        # assert the order and the claws, not the ids.
        self.assertEqual(parsed[0].assigned_claw, "scaffold")
        self.assertEqual(parsed[0].inputs["file_path"], self.api_path)
        self.assertEqual(parsed[1].assigned_claw, "bash")
        self.assertIn(
            "curl -s http://localhost:8765/health", parsed[1].inputs["command"]
        )

    # --- (e) Regression guards: earlier retries and fixes still hold ---

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_two_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        node = guard._retry14_v3_node()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry13_attempt4_flask_handler_branch_named(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("add a /health branch to the do_GET method", message)

    def test_retry12_string_ids_code_overwrite(self) -> None:
        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)

    def test_retry11_attempt3_numbered(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry10_attempt3_numbered_names_unknown_dependency(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 2 unknown-dependency + 2 unusable file_exists + 2 comment-only
        # commands + 1 bad criterion type -- 7 on seeker7, 9 in CI (see
        # _expected_retry10_error_count).
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("depends on unknown node", message)
        self.assertIn("endpoint_implemented", message)

    def test_retry9_v1_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry8_v1_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_numbered(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry5_fake_grounded_move_named(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)

    def test_retry4_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_retry3_glued_echo_heredoc_named(self) -> None:
        heredoc_fix = TestPlannerHeredocFix()
        heredoc_fix.setUp()
        node = heredoc_fix._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_coerces_and_rejected_by_name(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_structural_errors_still_raise_first(self) -> None:
        # Duplicate ids, dependency cycles, and self-dependency still raise
        # first, before any numbered rejection.
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )

class TestPlannerCodeAppendFix(unittest.TestCase):
    """Bash appends into an existing code file are unguarded (retry #16); the re-planner gives up with empty plans; the duplicate-key rejection loses the Flask//tmp signal.

    Retry #16 (log llm-end-to-end-health-retry16-2026-11-16.log, run
    run-20261117T102447Z-e8bfa4) failed twice over. First, the accepted
    v1 plan echoed two lines into the real freetalon/api.py (a comment,
    then 'def health():\\nreturn "OK"') with only exit_code criteria --
    both measured PASS while the module broke (IndentationError on line
    221; the suite fell to 608 tests, 18 failures + 2 errors). The
    placeholder guard covers scaffold only, and the stand-in rule fires
    only on JSON literals, so an echo-append of broken code was
    unguarded. Second, after the informative failure the re-planner
    returned {"nodes":[]} five times in a row and escalated with no
    grounded guidance. Third, the retry-#15 Flask heredoc plan was
    rejected for duplicate keys only -- the Flask//tmp defects never
    reached the repair round on that path.

    Three fixes: (1) a bash write into an existing code file is
    rejected by name unless the plan measures the file still working --
    for Python the write is simulated at plan time (existing file +
    appended lines) and the result must still parse with ast.parse
    (dash-echo escapes interpreted the way execution interprets them);
    for other code files a validity measurement is required. (2) After
    any empty reply the repair prompt carries a concrete grounded
    skeleton for the route goal (a fill-in-the-blanks plan shape, not a
    canned plan), and an escalation on empty plans names the grounded
    move. (3) The duplicate-key rejection also carries the per-node
    validation errors (framework, /tmp, prose, stand-in, route
    engagement, unwritten file) from the split nodes, mirroring the
    unknown-dependency fix from b66c5ac. The heredoc EOF terminator is
    also fixed: an unterminated heredoc body runs to the end of the
    command, so the framework//tmp rules can see it.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    # A generic (non-route) goal for the validity-measurement tests: the
    # /tmp, framework and route-engagement rules are route-scoped, so a
    # goal that names no route keeps those tests focused on the
    # code-append rule.
    GENERIC_GOAL = "Add a logging helper to the app and verify it works."

    # The exact retry-#16 v1 plan nodes, read from
    # runs/run-20261117T102447Z-e8bfa4/plan-v001.json (the node-2 command
    # carries a real newline inside its quoted literal, exactly as the
    # parsed plan holds it).
    RETRY16_V1_NODES = [
        {
            "id": "node-1",
            "objective": "Create the /health endpoint in api.py",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": "echo '# /health endpoint' >> /home/seeker/projects/FreeTalon/freetalon/api.py"
            },
            "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
            "acceptance": ["The /health endpoint is created in api.py"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
        {
            "id": "node-2",
            "objective": "Add the health check logic to api.py",
            "depends_on": ["node-1"],
            "assigned_claw": "bash",
            "inputs": {
                "command": "echo 'def health():\nreturn \"OK\"' >> /home/seeker/projects/FreeTalon/freetalon/api.py"
            },
            "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
            "acceptance": ["The health check logic is added to api.py"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
        {
            "id": "node-3",
            "objective": "Verify the /health endpoint returns OK",
            "depends_on": ["node-2"],
            "assigned_claw": "bash",
            "inputs": {"command": "curl http://localhost:8765/health | grep \"OK\""},
            "outputs": ["The output of the curl command"],
            "acceptance": ["The /health endpoint returns OK"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
    ]

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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    def _brief(self) -> dict:
        """The re-plan brief, from the fake-and-memory fix's test class."""
        fake = TestPlannerFakeAndMemoryFix()
        fake.setUp()
        return fake._brief()

    def _retry16_v1_nodes(self) -> list[dict]:
        import copy

        return copy.deepcopy(self.RETRY16_V1_NODES)

    @staticmethod
    def _bash_node(
        command: str,
        node_id: str = "step_1",
        depends_on: list[str] | None = None,
        outputs: list[str] | None = None,
    ) -> dict:
        return {
            "id": node_id,
            "objective": "Do the work",
            "depends_on": depends_on or [],
            "assigned_claw": "bash",
            "inputs": {"command": command},
            "outputs": outputs or ["result"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }

    # --- (a) The exact retry-#16 v1 plan is rejected by name ---

    def test_retry16_v1_echo_append_rejected_by_name(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry16_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Exactly one error: node-2's append breaks the module. (In CI the
        # seeker7 path does not exist, so the content alone is
        # parse-checked; on seeker7 the append is simulated against the
        # real file. Both branches reject with one error.)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("node-2", message)
        self.assertIn("freetalon/api.py", message)
        _assert_code_append_parse_error(self, message)
        self.assertIn("expected an indented block", message)
        self.assertIn("grounded move", message)

    # --- (b) A mocked echo-append reply repairs to a grounded plan ---

    def test_echo_append_repairs_to_grounded_plan_in_one_round(self) -> None:
        bad = self._dag(self._retry16_v1_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(nodes[0].inputs["file_path"], self.api_path)
        repair_prompt = call_mock.call_args[0][1]
        self.assertIn("REJECTED", repair_prompt)
        _assert_code_append_parse_error(self, repair_prompt)
        self.assertIn("freetalon/api.py", repair_prompt)

    # --- (c) Validity measurements, new files, and non-code files pass ---

    def test_append_to_js_file_with_test_run_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            js_path = os.path.join(tmp, "app.js")
            Path(js_path).write_text('console.log("start");\n', encoding="utf-8")
            nodes = [
                self._bash_node(
                    f"echo 'console.log(\"hi\");' >> {js_path}", node_id="step_1"
                ),
                self._bash_node(
                    f"python3 -m unittest discover -s {tmp}/tests",
                    node_id="step_2",
                    depends_on=["step_1"],
                ),
            ]
            parsed, _metadata = parse_planner_response(
                self._dag(nodes), goal_text=self.GENERIC_GOAL
            )
        self.assertEqual(len(parsed), 2)

    def test_append_to_js_file_without_measurement_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            js_path = os.path.join(tmp, "app.js")
            Path(js_path).write_text('console.log("start");\n', encoding="utf-8")
            nodes = [
                self._bash_node(
                    f"echo 'console.log(\"hi\");' >> {js_path}", node_id="step_1"
                ),
            ]
            with self.assertRaises(LLMResponseError) as caught:
                parse_planner_response(
                    self._dag(nodes), goal_text=self.GENERIC_GOAL
                )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("app.js", message)
        self.assertIn("still works", message)

    def test_append_to_py_file_with_py_compile_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            py_path = os.path.join(tmp, "helper.py")
            Path(py_path).write_text("x = 0\n", encoding="utf-8")
            nodes = [
                self._bash_node(f"echo 'x = 1' >> {py_path}", node_id="step_1"),
                self._bash_node(
                    f"python3 -m py_compile {py_path}",
                    node_id="step_2",
                    depends_on=["step_1"],
                ),
            ]
            parsed, _metadata = parse_planner_response(
                self._dag(nodes), goal_text=self.GENERIC_GOAL
            )
        self.assertEqual(len(parsed), 2)

    def test_heredoc_to_new_py_file_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            new_path = os.path.join(tmp, "newmod.py")
            command = (
                f"cat > {new_path} <<'EOF'\n"
                "# a new module\n"
                "def greet():\n"
                '    return "hi"\n'
                "EOF"
            )
            nodes = [self._bash_node(command, node_id="step_1")]
            parsed, _metadata = parse_planner_response(
                self._dag(nodes), goal_text=self.GENERIC_GOAL
            )
        self.assertEqual(len(parsed), 1)

    def test_write_to_non_code_file_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            txt_path = os.path.join(tmp, "notes.txt")
            nodes = [
                self._bash_node(f"echo 'hello' >> {txt_path}", node_id="step_1"),
            ]
            parsed, _metadata = parse_planner_response(
                self._dag(nodes), goal_text=self.GENERIC_GOAL
            )
        self.assertEqual(len(parsed), 1)

    # --- (d) Empty re-plans get a skeleton; five empties escalate grounded ---

    def test_empty_replans_then_grounded_plan_lands(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        empty = '{"nodes": [], "metadata": {}}'
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, empty, good],
        ) as call_mock:
            nodes = replan_from_brief(self._brief())
        # Two empty replies are free re-prompts (cap 2); the grounded plan
        # lands on the third call, inside the existing budget.
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        # The second call's repair prompt carries the grounded skeleton.
        second_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("CONCRETE PLAN SKELETON", second_prompt)
        self.assertIn("freetalon/api.py", second_prompt)
        self.assertIn("step_1", second_prompt)

    def test_empty_replans_five_times_escalates_with_grounded_move(self) -> None:
        from freetalon.orchestrator.planner import EmptyPlanError, replan_from_brief

        empty = '{"nodes": [], "metadata": {}}'
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, empty, empty, empty, empty],
        ) as call_mock:
            with self.assertRaises(EmptyPlanError) as caught:
                replan_from_brief(self._brief())
        # 1 initial + 2 free re-prompts + 2 repairs = 5 calls, then the
        # budget is exhausted -- and the escalation names the grounded move.
        self.assertEqual(call_mock.call_count, 5)
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("empty plans", message)

    # --- (e) Retry-#15 attempt 2 names duplicate keys AND Flask//tmp ---

    def test_retry15_attempt2_names_duplicate_keys_and_flask_tmp(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 7 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error.
        self.assertIn("was rejected with 7 errors", message)
        self.assertIn("duplicate JSON keys", message)
        self.assertIn("nests", message)
        self.assertIn("Flask", message)
        self.assertIn("/tmp/freetalon_api.py", message)
        self.assertIn("/tmp/freetalon_api_test.py", message)
        self.assertIn("does not parse as Python", message)

    # --- (f) Regression guards: every earlier fix still holds ---

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_two_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        node = guard._retry14_v3_node()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry13_attempt4_flask_handler_branch_named(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("add a /health branch to the do_GET method", message)

    def test_retry12_string_ids_code_overwrite(self) -> None:
        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)

    def test_retry11_attempt3_numbered(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry10_attempt3_numbered_names_unknown_dependency(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("depends on unknown node", message)
        self.assertIn("endpoint_implemented", message)

    def test_retry9_v1_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry8_v1_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_numbered(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry5_fake_grounded_move_named(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)

    def test_retry4_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_retry3_glued_echo_heredoc_named(self) -> None:
        heredoc_fix = TestPlannerHeredocFix()
        heredoc_fix.setUp()
        node = heredoc_fix._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_coerces_and_rejected_by_name(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_structural_errors_still_raise_first(self) -> None:
        # Duplicate ids, dependency cycles, and self-dependency still raise
        # first, before any numbered rejection.
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )

    def test_retry15_attempt2_still_a_duplicate_key_collapse(self) -> None:
        # The weakness-3 fix adds the Flask//tmp defects to the rejection;
        # the duplicate-key collapse itself is still named first.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("duplicate JSON keys", message)
        self.assertIn("node object (id 'node-3')", message)
        self.assertIn("separate each node", message)
        self.assertNotIn("depends on unknown node", message)


class TestPlannerPlaceholderCheapFix(unittest.TestCase):
    """Placeholder rejections are cheap; the repair prompt forbids repeated approaches; rejections name the model's own node ids (retry #17).

    Retry #17 (log llm-end-to-end-health-retry17-2026-11-16.log, no run
    created) failed at planning in four attempts: (1) the prose-command
    plan again, rejected with exactly 3 errors; (2) the retry-#15 Flask
    heredoc plan again, rejected with exactly 7 errors (duplicate keys,
    nesting, 2 parse errors, 2 /tmp errors, 1 Flask error); (3)
    {"nodes":[]} -- empty plan, free re-prompt, the repair prompt
    carrying the concrete grounded skeleton; (4) a skeleton-shaped plan
    the model DID follow -- but filled with placeholder comments
    ("# TODO: replace this comment with the real code ..."), rejected
    with 1 error by the placeholder guard. The repair budget (initial +
    2 repairs) was already spent on prose and Flask, so the model never
    got a repair round for the placeholder error and the driver raised.

    Three fixes: (1) a rejection whose only errors are placeholder
    scaffolds is CHEAP (free re-prompt, like empty plans and stand-in
    fakes), and the grounded skeleton's step-1 shows the existing
    handler branch as a commented sketch (new RepoIndex.route_branches,
    read from the real module), so the model has real code to extend
    instead of a blank TODO; a verbatim copy of the sketch is still
    placeholder-only, so the guard rejects it, cheaply. The placeholder
    error also names the grounded move for route goals. (2) The repair
    prompt names the approaches already tried and rejected in this
    planning call (Flask, /tmp writes, placeholder scaffolds, prose
    commands, hardcoded answers) -- approach names only, never quoted
    model content. (3) Rejections name the model's own node ids:
    parse_planner_response collects errors on copies of the nodes
    relabeled to their original ids ('step-1', not 'node-1'); the
    returned nodes keep normalized ids. The repair budget is NOT raised.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    GENERIC_GOAL = "Add a logging helper to the app and verify it works."

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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    def _retry17_attempt4_nodes(self) -> list[dict]:
        """The exact retry-#17 attempt-4 output, from the fourth RAW PLANNER
        OUTPUT block of logs/llm-end-to-end-health-retry17-2026-11-16.log.

        The step-1 file_path/outputs/criteria are retargeted to this
        checkout's api.py: the verbatim node scaffolds the absolute
        seeker7 path, which does not exist in CI, and the placeholder
        guard only fires when the target exists on disk (the retry-#14
        v3 precedent). Everything else is verbatim, including the
        model's lowercased handler text and the three-space gaps.
        """
        return [
            {
                "id": "step-1",
                "objective": (
                    "Extend /home/seeker/projects/FreeTalon/freetalon/api.py "
                    "in place   add a /health branch to the do_get method of "
                    "the handler class (defined inside hiveapiserver.start)"
                ),
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": self.api_path,
                    "content": (
                        "# TODO: replace this comment with the real code   "
                        "extend /home/seeker/projects/freetalon/freetalon/api.py "
                        "in place   add a /health branch to the do_get method "
                        "of the handler class (defined inside "
                        "hiveapiserver.start)"
                    ),
                },
                "outputs": [self.api_path],
                "acceptance": ["The /health endpoint is extended in freetalon/api.py."],
                "criteria": [{"type": "file_exists", "path": self.api_path}],
            },
            {
                "id": "step-2",
                "objective": "Write the tests for /health",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cat > /home/seeker/freetalon-workspace/test_health.py "
                        "<<'EOF'\n"
                        "# TODO: replace this comment with the real test code "
                        "for /health\n"
                        "EOF"
                    )
                },
                "outputs": ["/home/seeker/freetalon-workspace/test_health.py"],
                "acceptance": ["The tests for /health exist."],
                "criteria": [
                    {
                        "type": "file_exists",
                        "path": "/home/seeker/freetalon-workspace/test_health.py",
                    }
                ],
            },
            {
                "id": "step-3",
                "objective": "Run the tests for /health",
                "depends_on": ["step-2"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": "python3 /home/seeker/freetalon-workspace/test_health.py"
                },
                "outputs": ["test results"],
                "acceptance": ["The tests for /health pass."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step-4",
                "objective": "Verify /health live",
                "depends_on": ["step-3"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "acceptance": ["/health returns its JSON live."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    # --- (a) Verbatim retry-#17 attempt-4: cheap placeholder rejection ---

    def test_retry17_attempt4_is_a_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(self._retry17_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        # Weakness 3: the rejection names the model's own id, 'step-1',
        # not the normalized 'node-1'.
        self.assertIn("'step-1'", message)
        self.assertNotIn("'node-1'", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)
        # The placeholder error names the grounded move for route goals.
        self.assertIn("grounded move", message)

    # --- (b) Mocked [prose, Flask, empty, placeholder, grounded] lands ---

    def test_retry17_sequence_lands_on_grounded_plan_within_budget(self) -> None:
        prose = self._dag(TestPlannerScaffoldGuardFix._retry14_v1_nodes())
        flask = TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW
        empty = '{"nodes": [], "metadata": {}}'
        placeholder = self._dag(self._retry17_attempt4_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[prose, flask, empty, placeholder, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        nodes = plan.nodes
        # Prose spends repair 1, Flask spends repair 2, the empty plan is
        # free (cap 2), the placeholder rejection is cheap so it is free
        # too -- 5 calls total, inside the existing 1 + 2 free + 2 repairs
        # ceiling, and the grounded plan lands.
        self.assertEqual(call_mock.call_count, 5)
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        # The repair prompt after the prose attempt names prose as
        # forbidden; after the Flask attempt it names Flask too.
        first_repair = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", first_repair)
        self.assertIn("prose commands", first_repair)
        second_repair = call_mock.call_args_list[2][0][1]
        self.assertIn("DO NOT repeat approaches", second_repair)
        self.assertIn("a Flask app", second_repair)
        # The repair prompt after the empty plan carries the skeleton,
        # with the existing /health branch as a commented sketch. (The
        # sketch's quotes are JSON-escaped in the raw prompt, so the
        # branch is asserted by its quote-free prefix here; the exact
        # branch text is asserted on parsed content in
        # test_grounded_skeleton_shows_the_branch_sketch.)
        skeleton_prompt = call_mock.call_args_list[3][0][1]
        self.assertIn("CONCRETE PLAN SKELETON", skeleton_prompt)
        self.assertIn("if path == ", skeleton_prompt)
        self.assertIn("self._send", skeleton_prompt)
        # The repair prompt after the placeholder error names the
        # grounded move.
        placeholder_repair = call_mock.call_args_list[4][0][1]
        self.assertIn("grounded move", placeholder_repair)
        self.assertIn("freetalon/api.py", placeholder_repair)

    # --- (c) Mocked [Flask, Flask, grounded] lands; Flask is forbidden ---

    def test_flask_repeated_is_forbidden_by_name(self) -> None:
        flask = TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[flask, flask, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        nodes = plan.nodes
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(nodes), 4)
        # Both repair prompts name Flask as a forbidden approach.
        first_repair = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", first_repair)
        self.assertIn("a Flask app", first_repair)
        second_repair = call_mock.call_args_list[2][0][1]
        self.assertIn("DO NOT repeat approaches", second_repair)
        self.assertIn("a Flask app", second_repair)
        self.assertIn("stdlib http.server", second_repair)

    def test_placeholder_scaffold_repairs_to_grounded_in_one_round(self) -> None:
        placeholder = self._dag(self._retry17_attempt4_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[placeholder, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        nodes = plan.nodes
        # The placeholder rejection is cheap, so the re-prompt is free
        # and the grounded plan lands on the second call.
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("grounded move", repair_prompt)
        self.assertIn("freetalon/api.py", repair_prompt)

    # --- (d) Grounded plans pass; new-file stubs pass; the sketch is real ---

    def test_grounded_plan_passes_untouched(self) -> None:
        nodes, _metadata = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_placeholder_scaffold_to_new_file_still_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            new_path = os.path.join(tmp, "helper.py")
            nodes = [
                {
                    "id": "step_1",
                    "objective": "Add a logging helper",
                    "depends_on": [],
                    "assigned_claw": "scaffold",
                    "inputs": {
                        "file_path": new_path,
                        "content": "# TODO: implement the logging helper",
                    },
                    "outputs": [new_path],
                    "acceptance": ["The helper exists."],
                    "criteria": [{"type": "file_exists", "path": new_path}],
                }
            ]
            parsed, _metadata = parse_planner_response(
                self._dag(nodes), goal_text=self.GENERIC_GOAL
            )
        # A comment stub is allowed when the file is NEW -- the guard
        # fires only on overwriting an existing code file.
        self.assertEqual(len(parsed), 1)

    def test_route_branches_read_from_the_real_module(self) -> None:
        from freetalon.orchestrator.repo_index import build_repo_index

        index = build_repo_index(self.repo_root)
        self.assertIn("/health", index.route_branches)
        branch = index.route_branches["/health"]
        self.assertIn('if path == "/health"', branch)
        self.assertIn("self._send", branch)
        self.assertIn('"ok": True', branch)

    def test_grounded_skeleton_shows_the_branch_sketch(self) -> None:
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton
        from freetalon.orchestrator.tool_registry import is_placeholder_content

        skeleton = _grounded_skeleton(self.HEALTH_GOAL, workspace=self.WS)
        self.assertIn("Do NOT write a TODO comment", skeleton)
        self.assertIn("Write the FULL module", skeleton)
        # The step-1 content is still placeholder-only (all comment
        # lines), so a verbatim copy of the sketch is rejected cheaply
        # instead of slipping through as a plan. The branch lines carry
        # quotes, which the JSON dump escapes, so they are asserted on
        # the parsed content, not the raw skeleton string.
        parsed = json.loads(skeleton.split("\n", 1)[1])
        content = parsed["nodes"][0]["inputs"]["content"]
        self.assertIn('if path == "/health"', content)
        self.assertIn("self._send", content)
        self.assertTrue(is_placeholder_content(content))

    # --- (e) Regression guards: every earlier fix still holds ---

    def test_retry16_v1_rejected_by_name(self) -> None:
        nodes = TestPlannerCodeAppendFix()._retry16_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("node-2", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("expected an indented block", message)
        # The reported line is disk-dependent: 221 where the fixture
        # path exists, 2 where it does not (see
        # _expected_retry16_error_line).
        self.assertIn(f"line {_expected_retry16_error_line()}", message)
        _assert_code_append_parse_error(self, message)

    def test_retry15_attempt2_names_duplicate_keys_and_flask_tmp(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 7 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error.
        self.assertIn("was rejected with 7 errors", message)
        self.assertIn("duplicate JSON keys", message)
        self.assertIn("nests", message)
        self.assertIn("Flask", message)
        self.assertIn("/tmp/freetalon_api.py", message)
        self.assertIn("/tmp/freetalon_api_test.py", message)
        self.assertIn("does not parse as Python", message)

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_two_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        node = guard._retry14_v3_node()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry13_attempt4_flask_handler_branch_named(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("add a /health branch to the do_GET method", message)

    def test_retry12_string_ids_code_overwrite(self) -> None:
        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)

    def test_retry11_attempt3_numbered(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry10_attempt3_numbered_names_unknown_dependency(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("depends on unknown node", message)
        self.assertIn("endpoint_implemented", message)

    def test_retry9_v1_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry8_v1_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_numbered(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry5_fake_grounded_move_named(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)

    def test_retry4_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_retry3_glued_echo_heredoc_named(self) -> None:
        heredoc_fix = TestPlannerHeredocFix()
        heredoc_fix.setUp()
        node = heredoc_fix._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_coerces_and_rejected_by_name(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_structural_errors_still_raise_first(self) -> None:
        # Duplicate ids, dependency cycles, and self-dependency still raise
        # first, before any numbered rejection.
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )

class TestPlannerSignalLossFix(unittest.TestCase):
    """A parse-only rejection hides the plan's other defects (retry #18).

    Retry #18 (log llm-end-to-end-health-retry18-2026-11-16.log, no run
    created) failed at planning in three attempts: (1) the prose-command
    plan again, rejected with exactly 3 errors; (2) the retry-#15 Flask
    heredoc plan again, now at the real freetalon/api.py with a Flask
    app (@app.route('/health'), jsonify), test file at
    freetalon/tests/test_api.py, curl + jq . > /tmp/health_response.json
    — rejected with exactly 1 error, the step-2 test-heredoc parse
    error, and NOTHING else was named (the Flask overwrite, the
    nonexistent freetalon/tests/ directory, the /tmp redirect); (3) a
    sed + heredoc plan — rejected with exactly 3 errors (invented
    response_content criterion, step-2 test-heredoc parse error, step-2
    Flask app) — NOT named: step-1's sed targets def health(self):,
    which does not exist in api.py (sed -i with a non-matching pattern
    is a silent no-op), and the test path is still freetalon/tests/.
    The budget was exhausted with no free re-prompt spent — no empty
    plan, no placeholder scaffold — so the placeholder-cheap and
    forbid-repeated-approaches fixes were never exercised.

    The signal loss was not a missing aggregation: _collect_plan_errors
    already collects every error into one rejection. Three checks were
    blind to these shapes, so the single parse error was all the repair
    round ever saw. Five fixes: (1) the checks below now fire, so the
    parse-error rejection carries the framework / missing-directory /
    /tmp errors alongside it; (2) the framework check detects framework
    USE (route decorators, jsonify, app construction) as well as
    imports; (3) a sed -i edit whose search pattern never occurs in
    the file is rejected as a silent no-op; (4) a write to a repo-tree
    directory that does not exist (freetalon/tests/) is rejected with
    the correct location; (5) the /tmp check scans every redirect in a
    compound/pipelined command, not only a command that IS one literal
    write. The repair budget is NOT raised.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    GENERIC_GOAL = "Add a logging helper to the app and verify it works."

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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # The exact retry-#18 attempt-2 nodes, verbatim from the second RAW
    # PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry18-2026-11-16.log. Embedded (not
    # read from the log) so the test stays hermetic in CI. Values are
    # the JSON-parsed values (plain double quotes), not the log's
    # backslash-escaped spellings.
    @staticmethod
    def _retry18_attempt2_nodes() -> list[dict]:
        return [
            {
                "id": "step_1",
                "objective": "Create /health endpoint in api.py",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cat > /home/seeker/projects/FreeTalon/freetalon/api.py "
                        "<<'EOF'\n# Add /health endpoint\n@app.route('/health')\n"
                        "def health():\n return jsonify({\n 'status': 'OK',\n"
                        " 'ollama_reachable': True,\n 'gpu_temperature': 0 "
                        "# Replace with actual GPU temperature retrieval\n"
                        " })\nEOF"
                    )
                },
                "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
                "acceptance": ["/health endpoint added to api.py"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_2",
                "objective": "Create unit tests for /health endpoint",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cat > /home/seeker/projects/FreeTalon/freetalon/tests/test_api.py "
                        "<<'EOF'\nimport unittest\nfrom app import app"
                        "# Assuming your Flask app is in app.py\nimport json\n\n"
                        "class TestHealthEndpoint(unittest.TestCase):\n"
                        " def test_health_endpoint(self):\n"
                        " response = app.get('/health')\n"
                        " self.assertEqual(response.status_code, 200)\n"
                        " data = json.loads(response.data.decode('utf-8'))\n"
                        " self.assertEqual(data['status'], 'OK')\n"
                        " # Add more assertions to check other fields\n\n"
                        "if __name__ == '__main__':\n unittest.main()\nEOF"
                    )
                },
                "outputs": [
                    "/home/seeker/projects/FreeTalon/freetalon/tests/test_api.py"
                ],
                "acceptance": ["Unit tests created for /health endpoint"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_3",
                "objective": (
                    "Verify JSON response, Ollama reachability and GPU "
                    "temperature reporting"
                ),
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "python /home/seeker/projects/FreeTalon/freetalon/tests/test_api.py "
                        "&& curl -s http://localhost:8765/health | jq . > "
                        "/tmp/health_response.json && grep 'status': 'OK' "
                        "/tmp/health_response.json"
                    )
                },
                "outputs": ["/tmp/health_response.json"],
                "acceptance": [
                    "JSON response verified, Ollama reachability confirmed "
                    "and GPU temperature reported"
                ],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    # The exact retry-#18 attempt-3 nodes, verbatim from the third RAW
    # PLANNER OUTPUT block of the same log. The step-1 sed command
    # carries a real newline inside its quoted script, exactly as the
    # parsed plan holds it.
    @staticmethod
    def _retry18_attempt3_nodes() -> list[dict]:
        return [
            {
                "id": "step_1",
                "objective": (
                    "Modify /home/seeker/projects/FreeTalon/freetalon/api.py "
                    "to add a /health endpoint."
                ),
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "sed -i 's/def health(self):/def health(self):\n"
                        "return jsonify({\"status\": \"ok\", \"message\": "
                        "\"API is running\"})/' "
                        "/home/seeker/projects/FreeTalon/freetalon/api.py"
                    )
                },
                "outputs": ["/home/seeker/projects/FreeTalon/freetalon/api.py"],
                "acceptance": [
                    "The /health endpoint returns a JSON response with "
                    "status 'ok'."
                ],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_2",
                "objective": "Create a test file for the /health endpoint.",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cat > /home/seeker/projects/FreeTalon/freetalon/tests/test_api.py "
                        "<<EOF\nimport unittest\nfrom flask import jsonify\n"
                        "from freetalon.api import health\n\n"
                        "class TestHealthEndpoint(unittest.TestCase):\n"
                        "def test_health_endpoint(self):\nresponse = health()\n"
                        "self.assertEqual(response.status_code, 200)\n"
                        "self.assertEqual(response.get_json(), jsonify({\"status\": "
                        "\"ok\", \"message\": \"API is running\"}))\nEOF"
                    )
                },
                "outputs": [
                    "/home/seeker/projects/FreeTalon/freetalon/tests/test_api.py"
                ],
                "acceptance": [
                    "A test file for the /health endpoint has been created."
                ],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_3",
                "objective": "Run the tests.",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "python -m unittest /home/seeker/projects/FreeTalon/"
                        "freetalon/tests/test_api.py"
                    )
                },
                "outputs": ["Test results"],
                "acceptance": ["The tests pass."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_4",
                "objective": "Verify the /health endpoint is live.",
                "depends_on": ["step_3"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl http://localhost:8765/health"},
                "outputs": ["Response from curl"],
                "acceptance": [
                    "The /health endpoint returns a JSON response with "
                    "status 'ok'."
                ],
                "criteria": [
                    {"type": "exit_code", "equals": 0},
                    {
                        "type": "response_content",
                        "contains": (
                            '{"status": "ok", "message": "API is running"}'
                        ),
                    },
                ],
            },
        ]

    # --- (a) Verbatim retry-#18 attempt-2: parse + framework + missing dir + /tmp ---

    def test_retry18_attempt2_names_all_four_defects(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry18_attempt2_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 4 = step-2 parse error + step-1 framework error (Flask use,
        # no import) + step-2 missing-directory error (freetalon/tests/)
        # + step-3 /tmp redirect in a compound command. Before this fix
        # the rejection carried only the parse error.
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("does not parse as Python", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("Repo tests live at", message)
        self.assertIn("/tmp/health_response.json", message)
        self.assertIn("not a /tmp scratch file", message)

    # --- (b) Verbatim retry-#18 attempt-3: the 3 old errors + sed no-op + missing dir ---

    def test_retry18_attempt3_names_sed_noop_and_missing_dir(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry18_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 5 = the 3 errors the retry saw (invented response_content
        # criterion, step-2 parse error, step-2 Flask app) + the sed
        # no-op on step-1 + the missing-directory error on step-2.
        self.assertIn("was rejected with 5 errors", message)
        self.assertIn("response_content", message)
        self.assertIn("does not parse as Python", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("sed -i", message)
        self.assertIn("def health(self):", message)
        self.assertIn("does not occur", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("Repo tests live at", message)

    # --- (c) Mocked repairs land on the grounded plan in one round ---

    def test_flask_heredoc_repairs_to_grounded_in_one_round(self) -> None:
        bad = self._dag(self._retry18_attempt2_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("writes a Flask app", repair_prompt)
        self.assertIn("/tmp/health_response.json", repair_prompt)
        self.assertIn("freetalon/tests/test_api.py", repair_prompt)
        # The rejection names Flask and /tmp, so the forbid block names
        # both approaches — the retry-#18 call never forbade Flask
        # because the rejection never named it.
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn("a Flask app", repair_prompt)
        self.assertIn("writing files under /tmp", repair_prompt)

    def test_sed_noop_repairs_to_grounded_in_one_round(self) -> None:
        bad = self._dag(self._retry18_attempt3_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        nodes = plan.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("sed -i", repair_prompt)
        self.assertIn("def health(self):", repair_prompt)
        self.assertIn("does not occur", repair_prompt)

    # --- (d) Valid plans pass: grounded, sed on a real pattern, existing dirs ---

    def test_grounded_plan_passes_untouched(self) -> None:
        nodes, _metadata = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_sed_against_existing_pattern_and_write_to_existing_dir_pass(self) -> None:
        test_path = str(self.repo_root / "tests" / "test_health_check.py")
        nodes = [
            {
                "id": "step_1",
                "objective": "Extend the /health branch in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "sed -i 's/\"status\": \"ready\"/\"status\": \"ready\", "
                        f"\"ollama\": \"reachable\"/' {self.api_path}"
                    )
                },
                "outputs": [self.api_path],
                "acceptance": ["The /health branch reports Ollama."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_2",
                "objective": "Write the health endpoint test file",
                "depends_on": ["step_1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat > {test_path} <<'EOF'\nimport unittest\n\n\n"
                        "class TestHealthCheck(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        self.assertTrue(True)\nEOF"
                    )
                },
                "outputs": [test_path],
                "acceptance": ["The test file exists."],
                "criteria": [{"type": "file_exists", "path": test_path}],
            },
        ]
        parsed, _metadata = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 2)

    # --- Focused unit tests for the three new checks ---

    def test_framework_usage_without_import_is_detected(self) -> None:
        from freetalon.orchestrator.repo_index import framework_in_text

        # Retry #18 attempt 2 step 1: @app.route + jsonify, no import.
        self.assertEqual(
            framework_in_text(
                "@app.route('/health')\ndef health():\n return jsonify({})\n"
            ),
            "Flask",
        )
        self.assertEqual(framework_in_text("app = Flask(__name__)\n"), "Flask")
        self.assertEqual(
            framework_in_text("@router.get('/health')\ndef health():\n ...\n"),
            "FastAPI",
        )
        self.assertEqual(framework_in_text("app = FastAPI()\n"), "FastAPI")
        # A bare app.get(...) call is a test-client call, not a route
        # definition — not a marker.
        self.assertIsNone(framework_in_text("response = app.get('/health')\n"))
        self.assertIsNone(
            framework_in_text("response = requests.get(\"http://x/health\")\n")
        )
        self.assertIsNone(framework_in_text("import json\nprint('hi')\n"))

    def test_tmp_redirect_in_compound_command_is_named(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    "curl -s http://localhost:8765/health | jq . > "
                    "/tmp/out.json"
                )
            },
            "outputs": ["/tmp/out.json"],
            "acceptance": ["Live JSON verified."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("/tmp/out.json", message)
        self.assertIn("not a /tmp scratch file", message)

    def test_sed_noop_is_rejected_and_matching_sed_passes(self) -> None:
        from freetalon.orchestrator.planner import (
            _sed_noop_errors,
            _sed_script_patterns,
        )

        # The pattern parser: s///, /addr/, and an address + s command.
        self.assertEqual(_sed_script_patterns("s/def health(self):/x/"), ["def health(self):"])
        self.assertEqual(_sed_script_patterns("/health/d"), ["health"])
        self.assertEqual(
            _sed_script_patterns("/health/s/def x():/def x():\\npass/"),
            ["health", "def x():"],
        )
        self.assertEqual(_sed_script_patterns("10s/a/b/"), ["a"])
        self.assertEqual(_sed_script_patterns("s|a|b|"), ["a"])
        self.assertEqual(_sed_script_patterns("s/a\\/b/c/"), ["a\\/b"])
        self.assertEqual(_sed_script_patterns("s/unclosed"), [])
        # A non-matching pattern against the real api.py is a no-op.
        bad = {
            "id": "step_1",
            "objective": "Edit the endpoint",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    f"sed -i 's/def nope():/def nope():\\npass/' {self.api_path}"
                )
            },
            "outputs": [self.api_path],
            "acceptance": ["Edited."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        # The generic goal names no route, so only the sed error fires.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([bad]), goal_text=self.GENERIC_GOAL
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("sed -i", message)
        self.assertIn("def nope():", message)
        self.assertIn("does not occur", message)

    def test_missing_directory_is_rejected_with_correct_location(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Write the health endpoint test file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    "cat > freetalon/tests/test_x.py <<'EOF'\nimport unittest\n\n\n"
                    "class TestX(unittest.TestCase):\n"
                    "    def test_ok(self):\n"
                    "        self.assertTrue(True)\nEOF"
                )
            },
            "outputs": ["freetalon/tests/test_x.py"],
            "acceptance": ["The test file exists."],
            "criteria": [
                {"type": "file_exists", "path": "freetalon/tests/test_x.py"}
            ],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]), goal_text=self.GENERIC_GOAL
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/tests/test_x.py", message)
        self.assertIn("does not exist", message)
        self.assertIn("Repo tests live at", message)
        self.assertIn("/tests/test_x.py", message)

    # --- (e) Regression guards: every earlier fix still holds ---

    def test_retry17_attempt4_is_a_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        placeholder = TestPlannerPlaceholderCheapFix()
        placeholder.setUp()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(placeholder._retry17_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("'step-1'", message)
        self.assertNotIn("'node-1'", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry16_v1_rejected_by_name(self) -> None:
        nodes = TestPlannerCodeAppendFix()._retry16_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("node-2", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("expected an indented block", message)
        # The reported line is disk-dependent: 221 where the fixture
        # path exists, 2 where it does not (see
        # _expected_retry16_error_line).
        self.assertIn(f"line {_expected_retry16_error_line()}", message)
        _assert_code_append_parse_error(self, message)

    def test_retry15_attempt2_names_duplicate_keys_and_flask_tmp(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 7 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error.
        self.assertIn("was rejected with 7 errors", message)
        self.assertIn("duplicate JSON keys", message)
        self.assertIn("nests", message)
        self.assertIn("Flask", message)
        self.assertIn("/tmp/freetalon_api.py", message)
        self.assertIn("/tmp/freetalon_api_test.py", message)
        self.assertIn("does not parse as Python", message)

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_two_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        node = guard._retry14_v3_node()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry13_attempt4_flask_handler_branch_named(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("add a /health branch to the do_GET method", message)

    def test_retry12_string_ids_code_overwrite(self) -> None:
        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)

    def test_retry11_attempt3_numbered(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry10_attempt3_numbered_names_unknown_dependency(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("depends on unknown node", message)
        self.assertIn("endpoint_implemented", message)

    def test_retry9_v1_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry8_v1_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_numbered(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry5_fake_grounded_move_named(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)

    def test_retry4_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_retry3_glued_echo_heredoc_named(self) -> None:
        heredoc_fix = TestPlannerHeredocFix()
        heredoc_fix.setUp()
        node = heredoc_fix._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_coerces_and_rejected_by_name(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_structural_errors_still_raise_first(self) -> None:
        # Duplicate ids, dependency cycles, and self-dependency still raise
        # first, before any numbered rejection.
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )


class TestPlannerForbiddenSteerFix(unittest.TestCase):
    """The model ignores the forbidden-approach list (retry #19).

    Retry #19 (log llm-end-to-end-health-retry19-2026-11-16.log, no run
    created) failed at planning in three attempts: (1) the prose-command
    plan again, rejected with exactly 3 errors; (2) the retry-#15 Flask
    heredoc plan again — rejected with exactly 4 errors (step-2 parse
    error, step-3 piped /tmp redirect, step-1 Flask app with no flask
    import, step-2 missing freetalon/tests/ directory). The signal-loss
    fix WORKED — the parse error carried the framework / /tmp /
    missing-directory defects in the same rejection. (3) A Flask
    scaffold plan — rejected with exactly 1 error, the framework rule.
    Budget exhausted with no free re-prompt spent.

    Probe (tools/probe-forbidden.py, log
    logs/probe-forbidden-2026-11-17.log): hypothesis (b) — the mechanism
    DID name Flask as forbidden in the attempt-3 repair prompt ("a Flask
    app — the serving module is built on stdlib http.server, not Flask;
    extend it in place") and the live model wrote a Flask scaffold
    anyway. The marker detection works for every shape (heredoc Flask,
    /tmp, prose); a bare prohibition does not steer a temperature-0
    model whose prior for "implement a web endpoint" is "write a Flask
    app".

    Fix, both (i) and (ii), repair budget NOT raised: (i) every
    forbidden approach now carries its concrete replacement (the
    _APPROACH_MARKERS labels are "Do NOT ... Instead, ..." pairs), the
    Flask entry is followed by the goal's grounded-move sentence when
    one exists, and the framework rejection itself leads with the
    imperative prohibition + the grounded move; (ii) the forbidden block
    states the consequence — a repeat is rejected again — and names the
    repair count ("This is repair N of 2") when the round spent a
    repair.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    GENERIC_GOAL = "Add a logging helper to the app and verify it works."

    # The exact retry-#19 attempt-1 raw output, verbatim from the first
    # RAW PLANNER OUTPUT block of
    # logs/llm-end-to-end-health-retry19-2026-11-16.log. Embedded as raw
    # text (not read from the log) so the test stays hermetic in CI.
    RETRY19_ATTEMPT1_RAW = r'''{
"nodes":
[
{
"id":
"step_1",
"objective":
"Implement the /health endpoint in FreeTalon's API.",
"depends_on":
[],
"assigned_claw":
"bash",
"status":
"draft",
"inputs":
{
"command":
"/home/seeker/projects/FreeTalon/freetalon/api.py:HiveAPIServer, _ThreadingHTTPServer - Add a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start) that returns JSON reporting service status, whether Ollama at localhost:11434 is reachable, and GPU temperature when nvidia-smi is available."
},
"outputs":
[
"verification"
],
"acceptance":
[
"Output verified"
],
"criteria":
[
{
"type":
"exit_code",
"equals":
0
}
]
},
{
"id":
"step_2",
"objective":
"Add unit tests for the /health endpoint.",
"depends_on":
[
"step_1"
],
"assigned_claw":
"bash",
"status":
"draft",
"inputs":
{
"command":
"/home/seeker/projects/FreeTalon/freetalon/api.py:Unit tests for /health endpoint - Verify JSON response, Ollama reachability, and GPU temperature reporting."
},
"outputs":
[
"verification"
],
"acceptance":
[
"Output verified"
],
"criteria":
[
{
"type":
"exit_code",
"equals":
0
}
]
},
{
"id":
"step_3",
"objective":
"Verify the /health endpoint is live.",
"depends_on":
[
"step_2"
],
"assigned_claw":
"bash",
"status":
"draft",
"inputs":
{
"command":
"curl http://localhost:8765/health - Verify JSON response, Ollama reachability, and GPU temperature reporting."
},
"outputs":
[
"verification"
],
"acceptance":
[
"Output verified"
],
"criteria":
[
{
"type":
"exit_code",
"equals":
0
}
]
}
],
"metadata":
{
"estimated_steps":
3
}
}'''

    # The exact retry-#19 attempt-2 raw output, verbatim from the second
    # RAW PLANNER OUTPUT block of the same log.
    RETRY19_ATTEMPT2_RAW = r'''{
"metadata":{
"planner_version":"1.0",
"timestamp":"2024-01-01T00:00:00Z"
},
"nodes":[
{
"id":"step_1",
"objective":"Create /health endpoint in api.py",
"depends_on":[],
"assigned_claw":"bash",
"inputs":{
"command":"cat > /home/seeker/projects/FreeTalon/freetalon/api.py <<'EOF'\n# Add /health endpoint\n@app.route('/health')\ndef health():\n return jsonify({\n 'status': 'OK',\n 'ollama_reachable': True,\n 'gpu_temperature': 0 # Replace with actual GPU temperature retrieval\n })\nEOF"
},
"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],
"acceptance":["/health endpoint added to api.py"],
"criteria":[
{
"type":"exit_code",
"equals":0
}
]
},
{
"id":"step_2",
"objective":"Create unit tests for /health endpoint",
"depends_on":["step_1"],
"assigned_claw":"bash",
"inputs":{
"command":"cat > /home/seeker/projects/FreeTalon/freetalon/tests/test_api.py <<'EOF'\nimport unittest\nfrom app import app# Assuming your Flask app is in app.py\nimport json\n\nclass TestHealthEndpoint(unittest.TestCase):\n def test_health_endpoint(self):\n response = app.get('/health')\n self.assertEqual(response.status_code, 200)\n data = json.loads(response.data.decode('utf-8'))\n self.assertEqual(data['status'], 'OK')\n # Add more assertions to check other fields\n\nif __name__ == '__main__':\n unittest.main()\nEOF"
},
"outputs":["/home/seeker/projects/FreeTalon/freetalon/tests/test_api.py"],
"acceptance":["Unit tests created for /health endpoint"],
"criteria":[
{
"type":"exit_code",
"equals":0
}
]
},
{
"id":"step_3",
"objective":"Verify JSON response, Ollama reachability and GPU temperature reporting",
"depends_on":["step_2"],
"assigned_claw":"bash",
"inputs":{
"command":"python /home/seeker/projects/FreeTalon/freetalon/tests/test_api.py && curl -s http://localhost:8765/health | jq . > /tmp/health_response.json && grep 'status': 'OK' /tmp/health_response.json"
},
"outputs":["/tmp/health_response.json"],
"acceptance":["JSON response verified, Ollama reachability confirmed and GPU temperature reported"],
"criteria":[
{
"type":"exit_code",
"equals":0
}
]
}
]
}'''

    # The exact retry-#19 attempt-3 raw output, verbatim from the third
    # RAW PLANNER OUTPUT block of the same log.
    RETRY19_ATTEMPT3_RAW = r'''{"nodes":
[
{"id":
"step_1",
"objective":
"Create /health endpoint in freetalon/api.py",
"depends_on":
[],
"assigned_claw":
"scaffold",
"inputs":
{"file_path":
"/home/seeker/projects/FreeTalon/freetalon/api.py",
"content":
"# Import necessary modules\nfrom flask import Flask, jsonify\n\napp = Flask(__name__)\n\n@app.route('/health')\ndef health():\n return jsonify({\"status\": \"ok\", \"version\": \"1.0\"})\n\nif __name__ == '__main__':\n app.run(host='0.0.0.0', port=8765)"},
"outputs":
["/home/seeker/projects/FreeTalon/freetalon/api.py"],
"acceptance":
["/health endpoint is accessible at http://localhost:8765/health and returns a JSON response with status 'ok'"],
"criteria":
[{"type":
"exit_code",
"equals":
0}]},
{"id":
"step_2",
"objective":
"Create test_api.py in /home/seeker/projects/FreeTalon/tests/",
"depends_on":
["step_1"],
"assigned_claw":
"scaffold",
"inputs":
{"file_path":
"/home/seeker/projects/FreeTalon/tests/test_api.py",
"content":
"import unittest\nfrom freetalon import app\n\nclass TestHealthEndpoint(unittest.TestCase):\n def test_health_endpoint(self):\n response = app.get('/health')\n self.assertEqual(response.status_code, 200)\n data = response.get_json()\n self.assertEqual(data['status'], 'ok')\n\nif __name__ == '__main__':\n unittest.main()"},
"outputs":
["/home/seeker/projects/FreeTalon/tests/test_api.py"],
"acceptance":
["Test suite passes and verifies the /health endpoint returns a 200 status code and 'ok' status."],
"criteria":
[{"type":
"exit_code",
"equals":
0}]},
{"id":
"step_3",
"objective":
"Run tests and verify health endpoint is accessible.",
"depends_on":
["step_2"],
"assigned_claw":
"bash",
"inputs":
{"command":
"python /home/seeker/projects/FreeTalon/tests/test_api.py && curl http://localhost:8765/health | jq ."},
"outputs":
["Test results and health endpoint response"],
"acceptance":
["Tests pass, and the curl command returns a JSON object with status 'ok'."],
"criteria":
[{"type":
"exit_code",
"equals":
0}]}]}'''

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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # Retry-#18 fixtures live on the signal-loss class; reuse them
    # rather than duplicating the verbatim node lists.
    @staticmethod
    def _retry18_attempt2_nodes() -> list[dict]:
        return TestPlannerSignalLossFix._retry18_attempt2_nodes()

    @staticmethod
    def _retry18_attempt3_nodes() -> list[dict]:
        return TestPlannerSignalLossFix._retry18_attempt3_nodes()

    # --- (a) The captured repair prompt names Flask as forbidden,
    #     with the replacement move and the consequence ---

    def test_repair_prompt_after_retry19_attempt2_forbids_flask_with_replacement(
        self,
    ) -> None:
        bad = self.RETRY19_ATTEMPT2_RAW
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn("Do NOT write a Flask app", repair_prompt)
        self.assertIn("Instead, extend it in place", repair_prompt)
        self.assertIn("The grounded move is:", repair_prompt)
        self.assertIn("do_GET method of the Handler class", repair_prompt)
        self.assertIn("will be rejected again", repair_prompt)
        self.assertIn("This is repair 1 of 2", repair_prompt)

    # --- (b) Verbatim retry-#19 rejections ---

    def test_retry19_attempt2_rejected_with_4_errors(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("does not parse as Python", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do NOT write a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("Repo tests live at", message)
        self.assertIn("/tmp/health_response.json", message)
        self.assertIn("not a /tmp scratch file", message)

    def test_retry19_attempt3_rejected_with_1_error(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do NOT write a Flask app", message)
        self.assertIn("The grounded move is:", message)
        self.assertIn("do_GET method of the Handler class", message)

    # --- (c) Mocked [Flask-heredoc, Flask-scaffold, grounded] lands;
    #     [Flask, Flask, Flask] escalates with Flask forbidden ---

    def test_flask_heredoc_then_scaffold_repairs_to_grounded(self) -> None:
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[
                self.RETRY19_ATTEMPT2_RAW,
                self.RETRY19_ATTEMPT3_RAW,
                good,
            ],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)
        second_repair = call_mock.call_args_list[2][0][1]
        self.assertIn("DO NOT repeat approaches", second_repair)
        self.assertIn("Do NOT write a Flask app", second_repair)
        self.assertIn("The grounded move is:", second_repair)
        self.assertIn("will be rejected again", second_repair)
        self.assertIn("This is repair 2 of 2", second_repair)

    def test_flask_repeated_three_times_escalates(self) -> None:
        flask = TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[flask, flask, flask],
        ) as call_mock:
            with self.assertRaises(LLMResponseError) as caught:
                plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 3)
        message = str(caught.exception)
        self.assertIn("Flask", message)
        # Both repair prompts named Flask as forbidden, with the
        # replacement and the consequence.
        first_repair = call_mock.call_args_list[1][0][1]
        self.assertIn("Do NOT write a Flask app", first_repair)
        self.assertIn("This is repair 1 of 2", first_repair)
        second_repair = call_mock.call_args_list[2][0][1]
        self.assertIn("Do NOT write a Flask app", second_repair)
        self.assertIn("This is repair 2 of 2", second_repair)

    # --- (d) Valid plans pass: grounded, Flask in a new file without
    #     a route goal ---

    def test_grounded_plan_passes_untouched(self) -> None:
        nodes, _metadata = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")

    def test_flask_app_in_new_file_passes_without_a_route_goal(self) -> None:
        # The framework rule is target-agnostic for a route goal: a
        # Flask app in a NEW file is still rejected under the /health
        # goal (route engagement fires too). It passes when the goal
        # names no route, because the rule does not apply at all — the
        # fix must not widen the rule to reject it there.
        node = {
            "id": "step_1",
            "objective": "Write a small Flask service in a new file",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": "/home/seeker/projects/FreeTalon/new_service.py",
                "content": (
                    "from flask import Flask, jsonify\n\n"
                    "app = Flask(__name__)\n\n"
                    "@app.route('/ping')\ndef ping():\n return jsonify({"
                    '"status": "ok"})\n'
                ),
            },
            "outputs": ["/home/seeker/projects/FreeTalon/new_service.py"],
            "acceptance": ["The new service file exists."],
            "criteria": [
                {
                    "type": "file_exists",
                    "path": "/home/seeker/projects/FreeTalon/new_service.py",
                }
            ],
        }
        parsed, _metadata = parse_planner_response(
            self._dag([node]), goal_text=self.GENERIC_GOAL
        )
        self.assertEqual(len(parsed), 1)

    # --- Marker coverage: every forbidden approach is detected from
    #     its own rejection wording, whatever the rejection shape ---

    def test_every_approach_marker_matches_its_rejection(self) -> None:
        from freetalon.orchestrator.planner import _rejected_approaches

        # Flask (heredoc, no import) and /tmp, from retry #19 attempt 2.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        approaches = _rejected_approaches(str(caught.exception))
        self.assertTrue(any("Flask app" in a for a in approaches))
        self.assertTrue(any("/tmp" in a for a in approaches))
        # Prose, from retry #19 attempt 1.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT1_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        approaches = _rejected_approaches(str(caught.exception))
        self.assertTrue(any("prose commands" in a for a in approaches))
        # Placeholder, from retry #17 attempt 4.
        placeholder = TestPlannerPlaceholderCheapFix()
        placeholder.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(placeholder._retry17_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        approaches = _rejected_approaches(str(caught.exception))
        self.assertTrue(any("placeholder" in a for a in approaches))
        # Hardcoded answer, from the retry-#2 fake (stand-in rule).
        fake = TestPlannerFakeAndMemoryFix()
        fake.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(fake._fake_health_plan_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        approaches = _rejected_approaches(str(caught.exception))
        self.assertTrue(any("hardcoded" in a for a in approaches))

    # --- (e) Regression guards: retry #18 still holds, then every earlier fix ---

    def test_retry18_attempt2_names_all_four_defects(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry18_attempt2_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 4 = step-2 parse error + step-1 framework error (Flask use,
        # no import) + step-2 missing-directory error (freetalon/tests/)
        # + step-3 /tmp redirect in a compound command. Before this fix
        # the rejection carried only the parse error.
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("does not parse as Python", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("Repo tests live at", message)
        self.assertIn("/tmp/health_response.json", message)
        self.assertIn("not a /tmp scratch file", message)

    # --- (b) Verbatim retry-#18 attempt-3: the 3 old errors + sed no-op + missing dir ---

    def test_retry18_attempt3_names_sed_noop_and_missing_dir(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry18_attempt3_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 5 = the 3 errors the retry saw (invented response_content
        # criterion, step-2 parse error, step-2 Flask app) + the sed
        # no-op on step-1 + the missing-directory error on step-2.
        self.assertIn("was rejected with 5 errors", message)
        self.assertIn("response_content", message)
        self.assertIn("does not parse as Python", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("sed -i", message)
        self.assertIn("def health(self):", message)
        self.assertIn("does not occur", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("Repo tests live at", message)

    # --- (c) Mocked repairs land on the grounded plan in one round ---


    # --- (e) Regression guards: every earlier fix still holds ---

    def test_retry17_attempt4_is_a_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        placeholder = TestPlannerPlaceholderCheapFix()
        placeholder.setUp()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(placeholder._retry17_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("'step-1'", message)
        self.assertNotIn("'node-1'", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry16_v1_rejected_by_name(self) -> None:
        nodes = TestPlannerCodeAppendFix()._retry16_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("node-2", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("expected an indented block", message)
        # The reported line is disk-dependent: 221 where the fixture
        # path exists, 2 where it does not (see
        # _expected_retry16_error_line).
        self.assertIn(f"line {_expected_retry16_error_line()}", message)
        _assert_code_append_parse_error(self, message)

    def test_retry15_attempt2_names_duplicate_keys_and_flask_tmp(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 7 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error.
        self.assertIn("was rejected with 7 errors", message)
        self.assertIn("duplicate JSON keys", message)
        self.assertIn("nests", message)
        self.assertIn("Flask", message)
        self.assertIn("/tmp/freetalon_api.py", message)
        self.assertIn("/tmp/freetalon_api_test.py", message)
        self.assertIn("does not parse as Python", message)

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_two_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        node = guard._retry14_v3_node()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                repo_root=self.repo_root,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("placeholder", message)

    def test_retry13_attempt2_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_nodes()
        with self.assertRaises(CheapRejectionError):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry13_attempt4_flask_handler_branch_named(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("add a /health branch to the do_GET method", message)

    def test_retry12_string_ids_code_overwrite(self) -> None:
        nodes = TestPlannerStandinOverwriteFix._retry12_attempt2_string_ids_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)

    def test_retry11_attempt3_numbered(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry10_attempt3_numbered_names_unknown_dependency(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors",
            message,
        )
        self.assertIn("depends on unknown node", message)
        self.assertIn("endpoint_implemented", message)

    def test_retry9_v1_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry8_v1_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_numbered(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 11 errors", message)
        self.assertNotIn("did not match PlanNode schema", message)

    def test_retry5_fake_grounded_move_named(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("8765", message)

    def test_retry4_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_retry3_glued_echo_heredoc_named(self) -> None:
        heredoc_fix = TestPlannerHeredocFix()
        heredoc_fix.setUp()
        node = heredoc_fix._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together") as ctx:
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )
        self.assertIn("heredoc", str(ctx.exception))
        self.assertIn("scaffold", str(ctx.exception))

    def test_bare_dict_criteria_coerces_and_rejected_by_name(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
            )
        message = str(caught.exception)
        self.assertNotIn("did not match PlanNode schema", message)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_structural_errors_still_raise_first(self) -> None:
        # Duplicate ids, dependency cycles, and self-dependency still raise
        # first, before any numbered rejection.
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": []},
                    {"id": "a", "objective": "Second", "depends_on": []},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    {"id": "a", "objective": "First", "depends_on": ["b"]},
                    {"id": "b", "objective": "Second", "depends_on": ["a"]},
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes(
                [{"id": "a", "objective": "First", "depends_on": ["a"]}]
            )
