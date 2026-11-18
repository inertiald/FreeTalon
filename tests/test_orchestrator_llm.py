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
    sanitize_llm_output,
)
from freetalon.orchestrator.prompts import INTAKE_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT

# Real code for the grounded /health extension: the do_GET branch
# fragment the grounded move names. Retry #14: a comment-only
# scaffold to an existing code file is now rejected as a placeholder,
# so the fixtures must carry real code. Planner A/B refactor: as
# FULL scaffold content for api.py this fragment is exactly the
# retry-#28/#29 unsound shape (it drops all 12 of the module's
# definitions), so fixtures now extend api.py with the anchored
# edit below (GROUNDED_DO_GET_DELTA); this constant remains as the
# fragment itself, for the tests that pin the fragment's verdicts.
GROUNDED_API_CONTENT = (
    "# extended health endpoint\n"
    "if path == \"/health\":\n"
    "    self._send({\"ok\": True, \"status\": \"ready\"})"
)

# The grounded /health extension as an ANCHORED EDIT (planner A/B
# refactor): the verbatim current body of
# HiveAPIServer.start.Handler.do_GET — which already carries the
# /health branch — offered as the replace-delta for that anchor.
# Before the refactor the grounded fixtures scaffolded api.py
# with GROUNDED_API_CONTENT, a three-line branch fragment, as
# FULL file content: byte-identical in shape to the retry-#28/#29
# fragments the corpus scores as unsound accepts (defect 1). The
# retention guard no longer exempts fragment-scale scaffolds, so
# the grounded plan takes the edit form the planner itself
# teaches; materialization restores the full module by
# construction and the fragment constant survives only as the
# corpus-shaped counterexample in the guard tests.
GROUNDED_DO_GET_DELTA = (
    '            def do_GET(self) -> None:  # noqa: N802\n'
    '                parsed = urlparse(self.path)\n'
    '                path = parsed.path\n'
    '                qs = parse_qs(parsed.query)\n'
    '\n'
    '                if path == "/health":\n'
    '                    return self._send(200, {"ok": True, "status": "ready"})\n'
    '\n'
    '                if path == "/status":\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    return self._send(200, {"ok": True, "status": api.controller.status()})\n'
    '\n'
    '                if path == "/metrics":\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    status = api.controller.status()\n'
    '                    metrics = {\n'
    '                        "workers_healthy": status["workers"]["healthy"],\n'
    '                        "workers_configured": status["workers"]["configured"],\n'
    '                        "tasks_running": status["queue"]["running"],\n'
    '                        "tasks_queued": status["queue"]["queued"],\n'
    '                        "queue_max": status["queue"]["max"],\n'
    '                    }\n'
    '                    return self._send(200, {"ok": True, "metrics": metrics})\n'
    '\n'
    '                if path == "/tasks":\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    status_filter = qs.get("status", [None])[0]\n'
    '                    tasks = api.controller.list_tasks(status=status_filter)\n'
    '                    return self._send(200, {"ok": True, "tasks": tasks, "count": len(tasks)})\n'
    '\n'
    '                if path.startswith("/tasks/") and len(path.split("/")) == 3:\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    task_id = path.split("/")[2]\n'
    '                    task = api.controller.get_task(task_id)\n'
    '                    if task is None:\n'
    '                        return self._send(404, {"ok": False, "error": "Task not found"})\n'
    '                    return self._send(200, {"ok": True, "task": task})\n'
    '\n'
    '                if path == "/resources":\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    return self._send(200, {"ok": True, "resources": resource_summary_safe()})\n'
    '\n'
    '                # M4: approval gates — list packets, optionally filtered.\n'
    '                if path == "/gates":\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    status_filter = qs.get("status", [None])[0]\n'
    '                    run_filter = qs.get("run_id", [None])[0]\n'
    '                    if run_filter:\n'
    '                        packets = api.gate_store.list_for_run(run_filter)\n'
    '                    elif status_filter == "pending":\n'
    '                        packets = api.gate_store.pending()\n'
    '                    else:\n'
    '                        packets = api.gate_store.all()\n'
    '                    return self._send(\n'
    '                        200,\n'
    '                        {\n'
    '                            "ok": True,\n'
    '                            "gates": [pkt.model_dump(mode="json") for pkt in packets],\n'
    '                            "count": len(packets),\n'
    '                        },\n'
    '                    )\n'
    '\n'
    '                if path.startswith("/gates/") and len(path.split("/")) == 3:\n'
    '                    if not self._authorized():\n'
    '                        return\n'
    '                    packet_id = path.split("/")[2]\n'
    '                    packet = api.gate_store.get(packet_id)\n'
    '                    if packet is None:\n'
    '                        return self._send(404, {"ok": False, "error": "Gate not found"})\n'
    '                    return self._send(200, {"ok": True, "gate": packet.model_dump(mode="json")})\n'
    '\n'
    '                return self._send(404, {"ok": False, "error": "Not Found"})\n'
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


def _rejection_codes(raw: str, **kwargs: object) -> set:
    """The structured error codes of the rejection ``raw`` raises.

    Repair steering keys off codes since the planner A/B refactor
    (it used to key off substrings of the rejection message, and the
    tests passed the message text); this helper re-derives the codes
    from the same parse the test already asserted on, so call sites
    keep asserting on the message text AND pass the codes.
    """
    try:
        parse_planner_response(raw, **kwargs)
    except LLMResponseError as exc:
        return {record.code for record in getattr(exc, "plan_errors", [])}
    raise AssertionError("expected the plan to be rejected")

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
        # _call_llm_with_repair augments the model's metadata with the
        # approaches rejected in this planning call (retry #20): the list
        # is empty here, but the key is always present so the loop can
        # seed the next replan from plan.metadata without guessing.
        self.assertEqual(
            plan.metadata, {"planner": "llm", "rejected_approaches": []}
        )
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
            "inputs": {"file_path": api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
            inputs={"file_path": api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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

    def test_failed_health_plan_surfaces_all_seven_errors_at_once(self) -> None:
        message = self._rejection(self._failed_health_plan_nodes())
        # 7 errors: the original five below, plus one interpreter
        # existence error each for node-2's and node-3's glued tokens
        # (they contain '/', so they are judged as repo paths that do
        # not exist) -- repo-root fix (retry #26): _rejection parses
        # with no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely.
        self.assertIn("7 errors", message)
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
                    "inputs": {"file_path": api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
            "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
            "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
        # Retry #30: the bare '"run_id"' literal now legitimately
        # appears — the replan prompt carries freetalon/api.py's full
        # source as EXISTING MODULE grounding, and its /gates handler
        # reads a run_id query parameter (qs.get("run_id", ...)).
        # The anti-echo pin therefore targets the record's JSON KEY
        # form ("run_id": <value>), which stays absent.
        self.assertNotIn('"run_id":', user_prompt)
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
        # Retry #30: the record's JSON key form, as above — api.py's
        # source in the prompt uses "run_id" as a query literal.
        self.assertNotIn('"run_id":', repair_prompt)
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
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
        # Planner A/B refactor (defect 1): now 2 errors -- the Flask
        # fragment also faces the retention guard (as a full-content
        # scaffold of api.py it drops all 12 of the module's
        # definitions); the fragment-scale floor that exempted it is
        # gone. The framework error itself is unchanged.
        self.assertIn("2 errors", message)
        self.assertIn("Flask", message)
        self.assertIn("stdlib http.server", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("different web framework", message)
        self.assertIn("drops 12 of the existing module's 12 definitions", message)

    def test_http_server_write_for_http_server_route_is_not_flagged(self) -> None:
        # The same shape in the serving module's own framework passes.
        # Planner A/B refactor (defect 1): the content is the module's
        # own current source -- the smallest full-content scaffold of
        # api.py that retains every definition. The original fixture
        # (a one-class http.server shell as the FULL content of
        # api.py) is a defect-1 overwrite: executed, it would delete
        # the module it claims to extend, and the retention guard now
        # rejects it. The framework judgment this test pins -- an
        # http.server write to the http.server module is not flagged
        # -- is unchanged.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": Path(self.api_path).read_text(encoding="utf-8"),
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the same rejection also names the unparseable Flask
        # scaffold (def health(): followed by a top-level try:) and
        # node-2's bare `python` (does not resolve on Ubuntu).
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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
        # Planner A/B refactor (defect 1): the content is the module's
        # own current source -- a full-content scaffold that retains
        # every definition. The original fixture (a two-definition
        # http.server fragment as the FULL content of api.py) is a
        # defect-1 overwrite and the retention guard now rejects it;
        # the framework judgment this test pins is unchanged.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": Path(self.api_path).read_text(encoding="utf-8"),
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
        # Repo-root fix (retry #26): the total was 3; it is 5 now --
        # node-1's and node-2's citation tokens are also named as repo
        # paths that do not exist, because the parse passes no repo_root
        # (the driver path), where the interpreter existence check used
        # to skip path tokens entirely.
        self.assertIn("was rejected with 5 errors", message)
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
        # Node-3: ./test.sh, which does not exist in the repo.
        # Framework-guard fix (retry #23): the interpreter check names
        # the missing ./test.sh the old rejection missed.
        self.assertIn("was rejected with 3 errors", message)
        self.assertIn("./test.sh", message)
        self.assertIn("does not exist in the repo", message)
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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
        # 8 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error + 1 interpreter error.
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
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
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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
        # Anchored-edit fix (retry #30): step-1 is no longer a
        # full-module scaffold — the "Write the FULL module"
        # instruction this test used to pin IS the retry-#30 defect
        # (the model copied the sketch under it verbatim as a comment
        # stub), so the phrase must be GONE from the skeleton, and
        # step-1 is an anchored edit of the handler definition.
        self.assertNotIn("Write the FULL module", skeleton)
        # Delta-blank fix (retry #32): the step-1 delta used to be
        # instruction prose ("Write the COMPLETE new version ...",
        # "Do NOT write a TODO comment") with the branch riding as
        # comment lines, and this test pinned those phrases plus
        # is_placeholder_content(content) == True. Retry #31's model
        # copied the prose into its edit verbatim, so both pins
        # invert: the phrases are GONE from the content slot (the
        # instructions live in the skeleton's guidance text around
        # it), and the content is the definition's real code — the
        # branch in code form, at its true indentation — which is
        # NOT placeholder content. The branch lines carry quotes,
        # which the JSON dump escapes, so they are asserted on the
        # parsed content, not the raw skeleton string.
        parsed = json.loads(skeleton.split("\n", 1)[1])
        inputs = parsed["nodes"][0]["inputs"]
        self.assertEqual(
            inputs["anchor"], "HiveAPIServer.start.Handler.do_GET"
        )
        self.assertEqual(inputs["position"], "replace")
        content = inputs["content"]
        self.assertIn('if path == "/health":', content)
        self.assertIn("self._send", content)
        self.assertNotIn("Write the COMPLETE new version", content)
        self.assertNotIn("Do NOT write a TODO comment", content)
        self.assertFalse(is_placeholder_content(content))
        first_line = content.splitlines()[0]
        self.assertTrue(first_line.startswith("            def do_GET"))

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
        # 8 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error + 1 interpreter error.
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
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
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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
        # 6 = step-2 parse error + step-1 framework error (Flask use,
        # no import) + step-2 missing-directory error (freetalon/tests/)
        # + step-3 /tmp redirect in a compound command + step-3 bare
        # `python` (no bare `python` on Ubuntu). Framework-guard fix
        # (retry #23): the interpreter check names the bare `python`
        # the old rejection missed.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        self.assertIn("hardcoded answer", message)
        self.assertIn('"ollama_reachable": True', message)
        self.assertIn('"gpu_temperature": 0', message)
        self.assertNotIn("bare `python`", message)
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
        # 6 = the 3 errors the retry saw (invented response_content
        # criterion, step-2 parse error, step-2 Flask app) + the sed
        # no-op on step-1 + the missing-directory error on step-2
        # + step-3 bare `python`. Framework-guard fix (retry #23):
        # the interpreter check names the bare `python`.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        self.assertNotIn("bare `python`", message)
        self.assertIn("response_content", message)
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
        # 8 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error + 1 interpreter error.
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
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
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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

    def test_retry19_attempt2_rejected_with_5_errors(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do NOT write a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("Repo tests live at", message)
        self.assertIn("/tmp/health_response.json", message)
        self.assertIn("not a /tmp scratch file", message)

    def test_retry19_attempt3_rejected_with_3_errors(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        # Framework-guard fix (retry #23): was 1 error (Flask only). Now 3 — the rejection also names the unparseable step-2 scaffold and step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- step-3's bare
        # `python` is rewritten at parse time and no longer errors.
        # Planner A/B refactor (defect 1): now 3 -- step-1's Flask
        # fragment also faces the retention guard (it drops all 12
        # of api.py's definitions); the fragment-scale floor that
        # exempted sub-four-definition scaffolds is gone.
        self.assertIn("does not parse as Python", message)
        self.assertNotIn("bare `python`", message)
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

    # --- Code coverage: every forbidden approach is detected from
    #     its own rejection's structured codes, whatever the
    #     rejection shape (A/B refactor: this keyed off substrings
    #     of the rejection wording before the codes existed) ---

    def test_every_approach_code_matches_its_rejection(self) -> None:
        from freetalon.orchestrator.planner import _rejected_approaches

        # Flask (heredoc, no import) and /tmp, from retry #19 attempt 2.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        approaches = _rejected_approaches(
            {record.code for record in caught.exception.plan_errors}
        )
        self.assertTrue(any("Flask app" in a for a in approaches))
        self.assertTrue(any("/tmp" in a for a in approaches))
        # Prose, from retry #19 attempt 1.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY19_ATTEMPT1_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        approaches = _rejected_approaches(
            {record.code for record in caught.exception.plan_errors}
        )
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
        approaches = _rejected_approaches(
            {record.code for record in caught.exception.plan_errors}
        )
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
        approaches = _rejected_approaches(
            {record.code for record in caught.exception.plan_errors}
        )
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
        # 6 = step-2 parse error + step-1 framework error (Flask use,
        # no import) + step-2 missing-directory error (freetalon/tests/)
        # + step-3 /tmp redirect in a compound command + step-3 bare
        # `python` (no bare `python` on Ubuntu). Framework-guard fix
        # (retry #23): the interpreter check names the bare `python`
        # the old rejection missed.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        self.assertIn("hardcoded answer", message)
        self.assertIn('"ollama_reachable": True', message)
        self.assertIn('"gpu_temperature": 0', message)
        self.assertNotIn("bare `python`", message)
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
        # 6 = the 3 errors the retry saw (invented response_content
        # criterion, step-2 parse error, step-2 Flask app) + the sed
        # no-op on step-1 + the missing-directory error on step-2
        # + step-3 bare `python`. Framework-guard fix (retry #23):
        # the interpreter check names the bare `python`.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        self.assertNotIn("bare `python`", message)
        self.assertIn("response_content", message)
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
        # 8 = duplicate keys + nesting + 2 parse errors + 2 /tmp errors +
        # 1 framework error + 1 interpreter error.
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
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
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertEqual(
            message.count("bash command is prose, not a shell command"), 3
        )

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)
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
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
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


class TestPlannerSeededReplanFix(unittest.TestCase):
    """Forbidden approaches are forgotten between planning calls (retry #20).

    Retry #20 (log llm-end-to-end-health-retry20-2026-11-16.log, run
    run-20261117T130347Z-325642, plan plan-7885a4ef9d99) FAILED in 32.6s,
    escalated: re-planner failed. First retry since #8/#14/#16 where a
    plan was accepted AND executed. Initial planning (4 calls, 1 repair
    + 1 free used): (1) the prose-command plan again — rejected with
    exactly 3 errors; (2) the retry-#15 Flask heredoc plan again —
    rejected with exactly 4 errors (step-1 heredoc into api.py breaks
    the module, line 6; step-2 test heredoc does not parse, line 4;
    step-1 Flask app, new imperative form + grounded move; step-2
    freetalon/tests/ missing directory); (3) {"nodes":[]} — EmptyPlanError,
    cheap, free re-prompt (skeleton carried); (4) ACCEPTED — a
    placeholder-heredoc plan: step-1/2 write "#TODO: replace this
    comment with the real code/test code for /health" via cat>
    heredocs into the workspace, step-3 runs the comment-only test
    file, step-4 curls :8765/health. Execution: node-1 PASS (health.py
    exists), node-2 PASS (test_health.py exists), node-3 PASS (exit 0 —
    a comment-only file), node-4 FAIL (curl exit 7, connection refused
    — nothing serves :8765; the plan never starts the server). Judge
    chose REPLAN. Replan (3 calls, both repairs spent): (1) a
    single-node object with no nodes array and criteria nested in
    inputs — rejected with 2 errors (nesting + route engagement); (2) a
    scaffold of /health + curl /health — rejected with 2 errors
    (duplicates existing /health route + route engagement); (3) a
    scaffold of freetalon/api.py with a Flask app — rejected with
    exactly 1 error: step_1 writes a Flask app (new forbidden-block
    form, grounded move named). Budget exhausted, escalated FAIL.

    Forbidden-steer verdict for retry #20: PARTIAL. In initial planning
    the Flask ban HELD — attempt 4 was placeholder heredocs, not Flask
    (retry #19 attempt 3 was a Flask scaffold). BUT the replan phase
    starts with a FRESH forbidden list — `rejected_approaches` is a
    local inside _call_llm_with_repair, and replan_from_brief calls it
    separately. Flask, rejected in initial planning, was never named as
    forbidden in the replan repair prompts, and it came back as replan
    attempt 3.

    Fix, in priority order: (1) thread the rejected-approaches list from
    initial planning into replan_from_brief (seed_forbidden, carried
    through the plan metadata into the replan brief by the loop) so the
    first replan repair prompt already names Flask with its replacement
    and consequence; (2) extend the placeholder guard to bash
    heredoc/echo writes of comment-only/TODO-only content claimed by
    file_exists or run as a test — a cheap rejection, like placeholder
    scaffolds; (3) the ROUTE GOAL shape and grounded skeleton gain a
    server-start step (freetalon.cli start, then wait for readiness)
    before the live curl — shape only, no new validation error, because
    the canonical grounded plan curls without starting and is asserted
    to pass in the earlier fix classes. The repair budget is NOT raised.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#20 replan attempt-1 raw output, verbatim from the
    # fifth RAW PLANNER OUTPUT block of
    # logs/probe-retry20-rejections.log: a single-node object, no "nodes"
    # array, criteria nested inside inputs.
    RETRY20_REPLAN1_RAW = r'''{"id":"step_1","objective":"ScaffoldAPIendpoint","depends_on":[],"assigned_claw":"scaffold","inputs":{"file_path":"/health","content":"{\"status\":\"ok\"}","criteria":[{"type":"exit_code","equals":0}]},"outputs":["/health"],"acceptance":"Health endpoint is served.","decision":"CONTINUE"}'''

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

    def _brief(self) -> dict:
        """The re-plan brief, from the fake-and-memory fix's test class."""
        fake = TestPlannerFakeAndMemoryFix()
        fake.setUp()
        return fake._brief()

    def _seed_from_retry19_attempt2(self) -> list[str]:
        """The approaches initial planning rejected, as the loop sees them.

        Parses the verbatim retry-#19 attempt-2 output (Flask heredoc)
        and extracts the rejected approaches from its rejection — the
        same list plan_task_intent returns in the plan metadata.
        """
        from freetalon.orchestrator.planner import _rejected_approaches

        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        seed = _rejected_approaches(
            {record.code for record in caught.exception.plan_errors}
        )
        self.assertTrue(any("Flask app" in a for a in seed))
        return seed

    def _retry20_v1_nodes(self) -> list[dict]:
        """The exact retry-#20 v1 plan, from the fourth RAW PLANNER OUTPUT
        block of logs/probe-retry20-rejections.log (and
        runs/run-20261117T130347Z-325642/plan-v001.json)."""
        ws = self.WS
        return [
            {
                "id": "step-1",
                "objective": "Write/health",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat> {ws}/health.py <<'EOF'\n"
                        "#TODO: replace this comment with the real code for /health\n"
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/health.py"],
                "acceptance": ["The/health endpoint exists."],
                "criteria": [{"type": "file_exists", "path": f"{ws}/health.py"}],
            },
            {
                "id": "step-2",
                "objective": "Write tests for /health",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat> {ws}/test_health.py <<'EOF'\n"
                        "#TODO: replace this comment with the real test code for /health\n"
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/test_health.py"],
                "acceptance": ["The tests for /health exist."],
                "criteria": [{"type": "file_exists", "path": f"{ws}/test_health.py"}],
            },
            {
                "id": "step-3",
                "objective": "Run the tests for /health",
                "depends_on": ["step-2"],
                "assigned_claw": "bash",
                "inputs": {"command": f"python3 {ws}/test_health.py"},
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

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan, from the previous fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    def _grounded_with_start_nodes(self) -> list[dict]:
        """The grounded plan with the server-start step (weakness 3).

        Same shape as the grounded plan, plus a node that stops any stale
        server, starts the API server (freetalon.cli start daemonizes
        itself), and waits until /health answers — before the live curl.
        """
        nodes = self._grounded_health_nodes()
        start = {
            "id": "step_4",
            "objective": "Start the API server",
            "depends_on": ["step_3"],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    f"cd {self.repo_root} && ./.venv/bin/python -m freetalon.cli "
                    "stop; ./.venv/bin/python -m freetalon.cli start && "
                    "for i in $(seq 1 30); do curl -sf "
                    "http://localhost:8765/health && break; sleep 1; done; "
                    "curl -sf http://localhost:8765/health"
                )
            },
            "outputs": ["API server running"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        curl = dict(nodes[3])
        curl["id"] = "step_5"
        curl["depends_on"] = ["step_4"]
        return nodes[:3] + [start, curl]

    # --- (1) Seeded replan: the first repair prompt names Flask ---

    def test_seeded_replan_repair_prompt_forbids_flask_with_replacement(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        seed = self._seed_from_retry19_attempt2()
        brief = self._brief()
        brief["seed_forbidden"] = seed
        junk = self.RETRY20_REPLAN1_RAW
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[junk, good]
        ) as call_mock:
            nodes = replan_from_brief(brief)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn("Do NOT write a Flask app", repair_prompt)
        self.assertIn("Instead, extend it in place", repair_prompt)
        self.assertIn("The grounded move is:", repair_prompt)
        self.assertIn("do_GET method of the Handler class", repair_prompt)
        self.assertIn("will be rejected again", repair_prompt)
        self.assertIn("This is repair 1 of 2", repair_prompt)
        # The accumulated list is written back into the brief for the
        # loop to carry into the next replan's seed.
        self.assertTrue(
            any("Flask app" in a for a in brief.get("rejected_approaches", []))
        )

    def test_replan_seed_forbidden_kwarg(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        seed = self._seed_from_retry19_attempt2()
        junk = self.RETRY20_REPLAN1_RAW
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[junk, good]
        ) as call_mock:
            nodes = replan_from_brief(self._brief(), seed_forbidden=seed)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn("Do NOT write a Flask app", repair_prompt)
        self.assertIn("The grounded move is:", repair_prompt)

    def test_seeded_replan_junk_then_flask_repairs_to_grounded(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        seed = self._seed_from_retry19_attempt2()
        brief = self._brief()
        brief["seed_forbidden"] = seed
        junk = self.RETRY20_REPLAN1_RAW
        flask = steer.RETRY19_ATTEMPT3_RAW
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[junk, flask, good],
        ) as call_mock:
            nodes = replan_from_brief(brief)
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(nodes), 4)
        second_repair = call_mock.call_args_list[2][0][1]
        self.assertIn("DO NOT repeat approaches", second_repair)
        self.assertIn("Do NOT write a Flask app", second_repair)
        self.assertIn("The grounded move is:", second_repair)
        self.assertIn("will be rejected again", second_repair)
        self.assertIn("This is repair 2 of 2", second_repair)

    def test_seeded_replan_flask_repeated_escalates_with_flask_forbidden(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        seed = self._seed_from_retry19_attempt2()
        brief = self._brief()
        brief["seed_forbidden"] = seed
        junk = self.RETRY20_REPLAN1_RAW
        flask = steer.RETRY19_ATTEMPT3_RAW
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[junk, flask, flask],
        ) as call_mock:
            with self.assertRaises(LLMResponseError) as caught:
                replan_from_brief(brief)
        self.assertEqual(call_mock.call_count, 3)
        self.assertIn("writes a Flask app", str(caught.exception))
        third_prompt = call_mock.call_args_list[2][0][1]
        self.assertIn("DO NOT repeat approaches", third_prompt)
        self.assertIn("Do NOT write a Flask app", third_prompt)

    def test_plan_task_intent_returns_rejected_approaches_in_metadata(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        bad = steer.RETRY19_ATTEMPT2_RAW
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        seed = plan.metadata.get("rejected_approaches", [])
        self.assertTrue(any("Flask app" in a for a in seed))
        self.assertTrue(any("/tmp" in a for a in seed))

    # --- (2) Placeholder heredocs are rejected cheaply ---

    def test_retry20_v1_rejected_cheaply_naming_both_placeholder_heredocs(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(self._retry20_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("placeholder", message)
        self.assertIn("health.py", message)
        self.assertIn("test_health.py", message)
        self.assertIn("grounded move", message)

    def test_placeholder_heredoc_repairs_to_grounded_plan_for_free(self) -> None:
        bad = self._dag(self._retry20_v1_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # The placeholder round is a free re-prompt, not a repair.
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn("placeholder", repair_prompt)

    def test_heredoc_with_real_test_file_still_passes(self) -> None:
        heredoc = TestPlannerHeredocFix()
        heredoc.setUp()
        node = heredoc._bash_node(
            heredoc.HEREDOC_TEST,
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

    def test_heredoc_with_real_module_still_passes(self) -> None:
        ws = self.WS
        nodes = [
            {
                "id": "step-1",
                "objective": "Write the health module",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat > {ws}/health.py <<'EOF'\n"
                        "import json\n"
                        "\n"
                        "def health():\n"
                        '    return {"ok": True}\n'
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/health.py"],
                "criteria": [{"type": "file_exists", "path": f"{ws}/health.py"}],
            },
            {
                "id": "step-2",
                "objective": "Verify the endpoint live",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]
        parsed, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 2)

    def test_comment_only_heredoc_into_existing_code_file_rejected(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        node = {
            "id": "step-1",
            "objective": "Extend api.py",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    f"cat > {self.api_path} <<'EOF'\n"
                    "# TODO: replace this comment with the real code\n"
                    "EOF"
                )
            },
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        # The code-append check cannot catch this — a comment parses
        # fine — so before this fix the plan was ACCEPTED (probe
        # tools/probe-retry20.py). The placeholder-heredoc guard
        # rejects it, cheaply.
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("placeholder", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("grounded move", message)

    def test_unclaimed_placeholder_heredoc_is_left_alone(self) -> None:
        # A placeholder heredoc nobody claims (no file_exists, nobody
        # runs it) is not a fake deliverable — the guard stays out of
        # the way. Scope check for the new rule.
        ws = self.WS
        nodes = [
            {
                "id": "step-1",
                "objective": "Write a scratch note",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat > {ws}/notes.py <<'EOF'\n"
                        "# TODO: scratch\n"
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/notes.py"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step-2",
                "objective": "Verify the endpoint live",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]
        parsed, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 2)

    # --- (3) The shape names the server-start step ---

    def test_route_goal_shape_names_server_start_before_curl(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        shape = _route_goal_shape(self.HEALTH_GOAL, self.repo_root, delta=False)
        self.assertIn("node 3 starts the API server", shape)
        self.assertIn("freetalon.cli start", shape)
        self.assertIn("node 4 verifies live", shape)
        self.assertIn("curl http://localhost:8765/health", shape)

    def test_grounded_skeleton_has_start_step_before_curl(self) -> None:
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(self.HEALTH_GOAL, workspace=self.WS)
        parsed = json.loads(skeleton.split("\n", 1)[1])
        self.assertEqual(len(parsed["nodes"]), 5)
        self.assertEqual(
            [n["id"] for n in parsed["nodes"]],
            ["step-1", "step-2", "step-3", "step-4", "step-5"],
        )
        self.assertIn("Start the API server", parsed["nodes"][3]["objective"])
        self.assertIn(
            "freetalon.cli start", parsed["nodes"][3]["inputs"]["command"]
        )
        self.assertIn(
            "curl -s http://localhost:8765/health",
            parsed["nodes"][4]["inputs"]["command"],
        )

    def test_grounded_plan_with_server_start_passes(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_with_start_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 5)

    def test_grounded_move_names_starting_the_server(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("start the API server", str(caught.exception))

    # --- (f) Regression guards: every earlier fix still holds ---

    def test_retry19_attempt2_rejected_with_5_errors(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("/tmp/health_response.json", message)

    def test_retry19_attempt3_rejected_with_3_errors(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        # Framework-guard fix (retry #23): was 1 error (Flask only). Now 3 — the rejection also names the unparseable step-2 scaffold and step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- step-3's bare
        # `python` is rewritten at parse time and no longer errors.
        # Planner A/B refactor (defect 1): now 3 -- step-1's Flask
        # fragment also faces the retention guard (it drops all 12
        # of api.py's definitions); the fragment-scale floor that
        # exempted sub-four-definition scaffolds is gone.
        self.assertIn("does not parse as Python", message)
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("The grounded move is:", message)
        self.assertIn("do_GET method of the Handler class", message)

    def test_retry18_attempt2_rejected_with_5_errors(self) -> None:
        nodes = TestPlannerSignalLossFix._retry18_attempt2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("/tmp/health_response.json", message)

    def test_retry18_attempt3_rejected_with_5_errors(self) -> None:
        nodes = TestPlannerSignalLossFix._retry18_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 5 errors. Now 6 — the rejection also names step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("response_content", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("sed -i", message)
        self.assertIn("freetalon/tests/test_api.py", message)

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
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("duplicate", message)
        self.assertIn("Flask", message)
        self.assertIn("/tmp", message)

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertIn("prose, not a shell command", message)

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)
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
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        self.assertIn("overwrites code with a hardcoded answer", message)
        self.assertIn("grounded move", message)

    def test_retry13_attempt4_flask_scaffold_names_handler_branch(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do_GET", message)

    def test_retry12_attempt2_string_ids_is_a_code_overwrite(self) -> None:
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

    def test_retry10_attempt3_is_numbered(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors", message
        )
        self.assertIn("depends on unknown node", message)

    def test_retry9_v1_rejected_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaisesRegex(LLMResponseError, "workspace"):
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )

    def test_retry8_v1_rejected_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        self.assertIn("health.json", str(caught.exception))

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

    def test_retry5_fake_rejected_with_grounded_move(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)

    def test_retry4_plan_rejected_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("1 error", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("11434", message)
        self.assertIn("8765", message)
        self.assertIn("different service", message)

    def test_retry3_glued_echo_rejected_heredoc_named(self) -> None:
        heredoc = TestPlannerHeredocFix()
        heredoc.setUp()
        node = heredoc._bash_node(heredoc.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together"):
            parse_planner_response(self._dag([node]), goal_text=self.HEALTH_GOAL)

    def test_bare_dict_criteria_still_coerces_and_rejected_by_name(self) -> None:
        nodes = TestPlannerCriteriaShapeFix._retry6_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
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

class TestPlannerSchemaSignalFix(unittest.TestCase):
    """The placeholder signal is lost behind a schema error (retry #21).

    Retry #21 (log llm-end-to-end-health-retry21-2026-11-16.log, no run
    created) failed at planning in five planner calls: (1) {"nodes":[]}
    -- EmptyPlanError, cheap, free re-prompt. (2) The retry-#20
    placeholder-heredoc plan again -- step-1 cat-heredoc TODO test into
    workspace test_health.py, step-2 run it, step-3 start the API server,
    step-4 curl live. Rejected with exactly 1 error: step-4 has unknown
    field assigned_claws (a typo for assigned_claw). The
    placeholder-heredoc error did NOT fire here: the schema-error path
    runs only the stand-in check, so the placeholder defect was masked by
    the typo. (3) A Flask scaffold plan -- rejected with exactly 1 error:
    step_1 writes a Flask app (framework rule, grounded move named, now
    including the server-start step). (4) {"nodes":[]} again --
    EmptyPlanError, cheap, free re-prompt (second free used). (5) The
    placeholder-heredoc plan again, typo fixed -- rejected CHEAPLY with
    exactly 1 error: step-1 writes workspace test_health.py with
    placeholder content only via a bash heredoc, claimed by a file_exists
    criterion; grounded move named. Budget exhausted (2 free + 2 repairs
    spent) -> driver raised at plan_task_intent. Judge/replan never ran.
    Endpoint NOT extended, no new tests, no live curl. freetalon/api.py
    untouched (219 lines, 0-line diff). GPU survived (post-run 60C
    cooling, 300W, x16, zero Xid). Seeded-replan fix verdict: (1) seeded
    forbidden list NOT exercised live (failed in initial planning, so no
    replan branch ran); (2) placeholder-heredoc guard FIRED (attempt 5);
    (3) server-start shape STEERING (attempts 2 and 5 both include a
    start-server step) but the model wrote ./venv/bin/python -- wrong,
    the venv is .venv.

    Four fixes, in priority order: (1) the schema-error path
    (_validate_raw_nodes) runs the placeholder checks on lenient node
    views too, so one rejection names the typo AND the placeholder; the
    full _collect_plan_errors is deliberately NOT run there (several of
    its errors duplicate the schema errors, which would change the
    pinned counts for the retry-#7/#11 invented-schema fixtures). (2)
    The skeleton is carried more forcefully after an empty plan: a second
    empty in the same call gets the skeleton again with the count and the
    consequence named (free re-prompts are capped, so the next empty
    spends a repair) -- the budget itself is NOT changed. (3) The
    skeleton's step-2 is runnable real test code instead of a TODO
    heredoc, and the repair prompt after a placeholder rejection carries
    the skeleton too, so the model gets the fill-in-the-blanks with the
    actual branch code right when it needs it. (4) The grounded-move
    sentence names ./.venv/bin/python explicitly for the server start.
    The repair budget is NOT raised.
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

    def _retry21_attempt2_nodes(self) -> list[dict]:
        """The exact retry-#21 attempt-2 output, from the second RAW
        PLANNER OUTPUT block of
        logs/llm-end-to-end-health-retry21-2026-11-16.log (and
        logs/probe-retry21-rejections.log).

        Step-1 is the placeholder-heredoc plan (TODO test into workspace
        test_health.py, run it, start the server with ./venv, curl live);
        step-4 has the typo "assigned_claws" for "assigned_claw". The
        criteria are bare dicts, exactly as the model emitted them.
        """
        ws = self.WS
        return [
            {
                "id": "step-1",
                "objective": "Write the tests for /health",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat > {ws}/test_health.py <<'EOF'\n"
                        "# TODO: replace this comment with the real test code for /health\n"
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/test_health.py"],
                "acceptance": ["The tests for /health exist."],
                "criteria": {"type": "file_exists", "path": f"{ws}/test_health.py"},
            },
            {
                "id": "step-2",
                "objective": "Run the tests for /health",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {"command": f"python3 {ws}/test_health.py"},
                "outputs": ["test results"],
                "acceptance": ["The tests for /health pass."],
                "criteria": {"type": "exit_code", "equals": 0},
            },
            {
                "id": "step-3",
                "objective": "Start the API server for /health",
                "depends_on": ["step-2"],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        "cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m "
                        "freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && "
                        "for i in $(seq 1 30); do curl -sf http://localhost:8765/health "
                        "&& break; sleep 1; done; curl -sf http://localhost:8765/health"
                    )
                },
                "outputs": ["API server running"],
                "acceptance": ["The API server answers /health."],
                "criteria": {"type": "exit_code", "equals": 0},
            },
            {
                "id": "step-4",
                "objective": "Verify /health live",
                "depends_on": ["step-3"],
                "assigned_claws": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "acceptance": ["/health returns its JSON live."],
                "criteria": {"type": "exit_code", "equals": 0},
            },
        ]

    def _retry21_attempt5_nodes(self) -> list[dict]:
        """Attempt 2 with the typo fixed: the model fixed assigned_claws
        and returned the same placeholder plan (the fifth RAW PLANNER
        OUTPUT block of the retry-#21 log)."""
        nodes = self._retry21_attempt2_nodes()
        step4 = dict(nodes[3])
        step4["assigned_claw"] = step4.pop("assigned_claws")
        nodes[3] = step4
        return nodes

    # --- (1) The placeholder signal survives the schema error ---

    def test_retry21_attempt2_names_typo_and_placeholder_in_one_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(self._retry21_attempt2_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 2 = the placeholder-heredoc error (step-1) + the typo (step-4).
        # Before this fix the rejection carried only the typo, and the
        # model fixed the typo and returned the same placeholder plan.
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("assigned_claws", message)
        self.assertIn("placeholder content only", message)
        self.assertIn("test_health.py", message)
        self.assertIn("grounded move", message)

    def test_retry21_attempt5_rejected_with_1_error_cheap(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry21_attempt5_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        # Repo-root fix (retry #26): was 1 error, and cheap. The parse
        # passes no repo_root -- the driver path -- where the
        # interpreter existence check used to skip path tokens, so
        # step-3's two ./venv/bin/python commands (the venv is .venv;
        # the path does not exist) were never named and the placeholder
        # error stood alone. Now 3 errors, and NOT cheap: cheap covers
        # only stand-in/placeholder fakes, and this plan also carries
        # two real interpreter defects a repair round must fix.
        # Interpreter normalization (retry #27 fix): step-3's two
        # ./venv/bin/python commands are rewritten to ./.venv/bin/python at
        # parse time, so the interpreter errors are gone and the placeholder
        # error stands alone again -- 1 error, and cheap once more.
        self.assertIsInstance(caught.exception, CheapRejectionError)
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertNotIn("./venv/bin/python", message)
        self.assertIn("placeholder content only", message)
        self.assertIn("test_health.py", message)
        self.assertIn("grounded move", message)

    # --- (2) The skeleton is carried more forcefully after an empty plan ---

    def test_empty_empty_grounded_lands_within_budget(self) -> None:
        empty = '{"nodes": []}'
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, empty, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)
        # The second empty gets the skeleton again, now with the count
        # and the consequence named -- the repeat is forceful, not the
        # same block shown unchanged.
        third_prompt = call_mock.call_args_list[2][0][1]
        self.assertIn("already returned an empty plan 2 times", third_prompt)
        self.assertIn("free re-prompts are used up", third_prompt)
        self.assertIn("CONCRETE PLAN SKELETON", third_prompt)
        # The first repair prompt (after the first empty) carries the
        # skeleton but does NOT name a repeat -- there has been only one.
        second_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("CONCRETE PLAN SKELETON", second_prompt)
        self.assertNotIn("already returned an empty plan 2 times", second_prompt)

    def test_empty_placeholder_placeholder_grounded_lands_within_budget(self) -> None:
        empty = '{"nodes": []}'
        placeholder = self._dag(self._retry21_attempt5_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, placeholder, placeholder, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Empty (free 1) + placeholder (cheap, free 2) + placeholder
        # (cheap but the free cap is used, repair 1) + grounded: 4 calls,
        # inside the existing 1 + 2 free + 2 repairs ceiling.
        self.assertEqual(call_mock.call_count, 4)
        self.assertEqual(len(plan.nodes), 4)

    # --- (3) The skeleton's step-2 is real code; the repair prompt after a
    # placeholder rejection carries the skeleton ---

    def test_placeholder_heredoc_repairs_to_grounded_in_one_round(self) -> None:
        placeholder = self._dag(self._retry21_attempt5_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[placeholder, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        # The repair prompt after the placeholder rejection carries the
        # skeleton (not just the prohibition), with the placeholder
        # lead-in -- never the empty-plan lead-in.
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("CONCRETE PLAN SKELETON", repair_prompt)
        self.assertIn("rejected as a placeholder stub", repair_prompt)
        self.assertNotIn("your previous reply was an empty plan", repair_prompt)
        self.assertIn("test_route_answers", repair_prompt)

    def test_flask_placeholder_grounded_lands_within_budget(self) -> None:
        flask = TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW
        placeholder = self._dag(self._retry21_attempt5_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[flask, placeholder, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Flask (repair 1) + placeholder (cheap, free 1) + grounded:
        # 3 calls, inside the existing budget.
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)

    def test_skeleton_step2_is_runnable_real_test_code(self) -> None:
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(self.HEALTH_GOAL, self.repo_root, self.WS)
        parsed = json.loads(skeleton.split("\n", 1)[1])
        step2 = parsed["nodes"][1]
        self.assertEqual(step2["id"], "step-2")
        command = step2["inputs"]["command"]
        self.assertNotIn("# TODO", command)
        self.assertIn("<<'EOF'", command)
        # The test starts the API server in-process (the tests/test_api.py
        # pattern) and asserts the goal's three fields, so it fails on the
        # unextended endpoint and passes once the endpoint is extended.
        self.assertIn("HiveAPIServer", command)
        self.assertIn("service_status", command)
        self.assertIn("ollama_reachable", command)
        self.assertIn("gpu_temperature", command)
        self.assertIn("unittest.main()", command)

    def test_skeleton_step2_test_runs_and_enforces_the_deliverable(self) -> None:
        """The skeleton's step-2 test, run as-is, starts the server
        in-process and fails ONLY on the missing goal fields.

        Probed live (tools/probe-skeleton-test.py): the test GETs /health
        on the unextended endpoint, gets {"ok": True, "status": "ready"},
        and fails with AssertionError: 'service_status' not found. A test
        that passed here would prove nothing -- the skeleton would be
        steering the model toward a test that cannot catch a missing
        endpoint.
        """
        import json
        import subprocess
        import sys
        import tempfile

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(self.HEALTH_GOAL, self.repo_root, self.WS)
        parsed = json.loads(skeleton.split("\n", 1)[1])
        command = parsed["nodes"][1]["inputs"]["command"]
        body = command.split("<<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]
        # The scratch file lives in the system temp dir, not the
        # workspace: the workspace path is a seeker7 path that does not
        # exist in CI. sys.executable runs it: on seeker7 that IS the
        # project .venv python, and in CI it is the CI python with the
        # repo's deps installed.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            os.unlink(tmp_path)
        # The test runs (the server starts, /health answers 200) and
        # fails on the first missing goal field -- nothing else.
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("service_status", proc.stderr)
        self.assertIn("AssertionError", proc.stderr)
        self.assertNotIn("ImportError", proc.stderr)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    # --- (4) The server-start shape names the exact venv path ---

    def test_server_start_shape_names_venv_python(self) -> None:
        from freetalon.orchestrator.planner import (
            _grounded_move_sentence,
            _route_goal_shape,
        )

        shape = _route_goal_shape(self.HEALTH_GOAL, self.repo_root, delta=False)
        self.assertIn("./.venv/bin/python -m freetalon.cli start", shape)
        self.assertIn("./.venv/bin/python -m freetalon.cli stop", shape)
        move = _grounded_move_sentence(
            "/health",
            self.api_path,
            8765,
            "the do_GET method of the Handler class (defined inside HiveAPIServer.start)",
        )
        self.assertIn("./.venv/bin/python -m freetalon.cli start", move)
        self.assertIn("./.venv/bin/python -m freetalon.cli stop", move)
        self.assertIn("do_GET method", move)
        self.assertIn("curl http://localhost:8765/health", move)

    # --- (e) Legitimate plans still pass ---

    def test_grounded_plan_still_passes(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)

    def test_heredoc_with_real_test_code_passes(self) -> None:
        # The skeleton's step-2 heredoc, dropped into the grounded plan
        # in place of the old HEREDOC_TEST, is real code -- the
        # placeholder guard stays out of the way.
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(self.HEALTH_GOAL, self.repo_root, self.WS)
        parsed = json.loads(skeleton.split("\n", 1)[1])
        step2_cmd = parsed["nodes"][1]["inputs"]["command"]
        nodes = self._grounded_health_nodes()
        nodes[1]["inputs"]["command"] = step2_cmd
        nodes[1]["outputs"] = [f"{self.WS}/test_health.py"]
        nodes[1]["criteria"] = [{"type": "file_exists", "path": f"{self.WS}/test_health.py"}]
        parsed_nodes, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed_nodes), 4)

    def test_heredoc_with_real_module_code_passes(self) -> None:
        # A heredoc that writes real module code into a NEW file passes
        # untouched -- scope check for the placeholder rule in the schema
        # path: only comment-only content is a placeholder.
        ws = self.WS
        nodes = [
            {
                "id": "step-1",
                "objective": "Write a health-check helper",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat > {ws}/health_check.py <<'EOF'\n"
                        "import json\n"
                        "\n"
                        "\n"
                        "def check(payload):\n"
                        '    return payload.get("ok", False)\n'
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/health_check.py"],
                "criteria": [{"type": "file_exists", "path": f"{ws}/health_check.py"}],
            },
            {
                "id": "step-2",
                "objective": "Verify /health live",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]
        parsed, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 2)

    # --- (f) Regression guards: every earlier fix still holds ---

    def test_retry20_v1_rejected_cheaply_naming_placeholder_heredocs(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        seeded = TestPlannerSeededReplanFix()
        seeded.setUp()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(seeded._retry20_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("placeholder", message)
        self.assertIn("health.py", message)
        self.assertIn("test_health.py", message)

    def test_retry19_attempt2_rejected_with_5_errors(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("/tmp/health_response.json", message)

    def test_retry19_attempt3_rejected_with_3_errors_naming_flask(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        # Framework-guard fix (retry #23): was 1 error (Flask only). Now 3 — the rejection also names the unparseable step-2 scaffold and step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- step-3's bare
        # `python` is rewritten at parse time and no longer errors.
        # Planner A/B refactor (defect 1): now 3 -- step-1's Flask
        # fragment also faces the retention guard (it drops all 12
        # of api.py's definitions); the fragment-scale floor that
        # exempted sub-four-definition scaffolds is gone.
        self.assertIn("does not parse as Python", message)
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do_GET method", message)

    def test_retry18_attempt2_rejected_with_5_errors(self) -> None:
        nodes = TestPlannerSignalLossFix._retry18_attempt2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("/tmp/health_response.json", message)

    def test_retry18_attempt3_rejected_with_5_errors(self) -> None:
        nodes = TestPlannerSignalLossFix._retry18_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 5 errors. Now 6 — the rejection also names step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("response_content", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("sed -i", message)
        self.assertIn("freetalon/tests/test_api.py", message)

    def test_retry17_attempt4_is_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        cheap = TestPlannerPlaceholderCheapFix()
        cheap.setUp()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(cheap._retry17_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("placeholder", message)
        self.assertIn("step-1", message)
        self.assertIn("freetalon/api.py", message)

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
        self.assertIn(f"line {_expected_retry16_error_line()}", message)
        _assert_code_append_parse_error(self, message)

    def test_retry15_attempt2_with_8_errors(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("duplicate", message.lower())
        self.assertIn("writes a Flask app", message)
        self.assertIn("/tmp", message)

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertIn("prose", message)

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([guard._retry14_v3_node()]),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)

    def test_retry13_attempt2_is_cheap_rejection(self) -> None:
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

    def test_retry13_attempt4_flask_scaffold_names_handler_branch(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do_GET method", message)

    def test_retry12_attempt2_string_ids_is_code_overwrite(self) -> None:
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

    def test_retry11_attempt3_invented_schema_is_numbered_rejection(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("objective", message)

    def test_retry10_attempt3_is_numbered_rejection(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors", message
        )
        self.assertIn("depends on unknown node", message)

    def test_retry9_v1_rejected_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("2 errors", message)
        self.assertIn("repo root", message)
        self.assertIn(str(self.repo_root), message)
        self.assertIn(f"{self.WS}/freetalon/api.py", message)

    def test_retry8_v1_rejected_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_invented_schema_is_numbered_rejection(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("PlanNode object", message)

    def test_retry5_fake_rejected_with_grounded_move(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)

    def test_retry4_plan_rejected_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("11434", message)
        self.assertIn("8765", message)

    def test_retry3_glued_echo_rejected_with_heredoc_alternative(self) -> None:
        heredoc = TestPlannerHeredocFix()
        heredoc.setUp()
        node = heredoc._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together"):
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )

    def test_bare_dict_criteria_coerced_and_rejected_by_name(self) -> None:
        nodes = self._grounded_health_nodes()
        nodes[0]["criteria"] = {"tests_pass": True}
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_duplicate_ids_cycles_self_dependency_raise_first(self) -> None:
        base = {
            "objective": "First",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "true"},
            "outputs": ["result"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes([dict(base, id="a"), dict(base, id="a")])
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    dict(base, id="a", depends_on=["b"]),
                    dict(base, id="b", depends_on=["a"]),
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes([dict(base, id="a", depends_on=["a"])])


class TestPlannerEmptyEscalationFix(unittest.TestCase):
    """The planner prompts do not fit in the context window (retry #22).

    Retry #22 (log llm-end-to-end-health-retry22-2026-11-16.log, no run
    created) failed at planning in ~40s: all five raw planner outputs
    are byte-identical {"nodes":[]} — no prose plan, no Flask plan, no
    placeholder plan. Intake succeeded (valid TaskIntent, no echo). The
    second-empty skeleton force WAS exercised — and ignored: the final
    escalation names the repeated empties and the grounded move verbatim,
    the repair prompts carried the skeleton with the count and the
    consequence named, and the model still returned empty. Budget
    exhausted (2 free + 2 repairs spent) -> driver raised at
    plan_task_intent. Judge/replan never ran. Endpoint NOT extended, no
    new tests, no live curl. freetalon/api.py untouched (219 lines,
    0-line diff). GPU survived (post-run 52C idle, 300W, x16, zero Xid).
    The schema-signal fix was NOT exercised live — no attempt carried a
    placeholder or a schema error; the seeded forbidden list is still
    unproven live (third retry in a row: #20/#21/#22); the real skeleton
    test step-2 and the server-start step never appeared in any plan.

    Root cause, measured, not guessed: the prompts exceed Ollama's
    4096-token context window (num_ctx, hardcoded in _call_ollama).
    Token counts with the Gemma 3 tokenizer from models/tokenizer.json
    against the exact strings the driver assembles (tools/dump-prompts.py;
    logs/measure-prompt-*.txt): initial call 4,255 tokens (system 3,030
    + user 1,225) — already 159 tokens OVER the window before a single
    output token; first repair prompt 6,415 tokens (2,319 over); a
    grounded-plan output is 1,687 tokens, so a full repair round needs
    8,102 tokens of context. The model never received the skeleton
    intact and could not emit a full plan — the 5-token {"nodes":[]} is
    the only output that fits the leftover headroom, which is why all
    five replies are byte-identical.

    Two fixes: (1) planner/replan LLM calls use _PLANNER_NUM_CTX =
    12288, threaded through LLMSettings.num_ctx (a new optional field;
    None keeps the 4096 default). 12288 covers the measured 8,102-token
    repair round with ~4,000 tokens spare for longer goals and replan
    briefs. The intake path keeps 4096 (its prompts are small). KV cost
    at 12288 is ~4.6 GB vs ~1.5 GB at 4096 (0.375 MB/token for this 12B
    model) — Ollama fits it by offloading a few more layers to CPU if
    VRAM runs short, a speed cost, not a failure. (2) The ROUTE GOAL
    shape moves last in the initial prompt (and in the replan prompt):
    it sat at ~70% of the initial prompt (offset 2845 of 4093 chars),
    ahead of the instruction paragraph; now only "\\n\\nJSON Output:"
    follows it. The repair budget is NOT raised and no fail-fast is
    added: the skeleton mechanism was already built and proven in
    mocks — the live failure was delivery, not content — so ending
    failures sooner would not produce a plan.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    # Token counts measured 2026-11-17 with the Gemma 3 tokenizer from
    # models/tokenizer.json (see the class docstring). The tokenizer is
    # not available in the test environment, so the counts are pinned
    # here as constants with their provenance, and the live-assembled
    # prompts are checked by length in the demo tool instead.
    INITIAL_PROMPT_TOKENS = 4255
    FIRST_REPAIR_PROMPT_TOKENS = 6415
    GROUNDED_PLAN_OUTPUT_TOKENS = 1687
    FULL_REPAIR_ROUND_TOKENS = 8102

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

    def _retry21_attempt2_nodes(self) -> list[dict]:
        signal = TestPlannerSchemaSignalFix()
        signal.setUp()
        return signal._retry21_attempt2_nodes()

    def _retry21_attempt5_nodes(self) -> list[dict]:
        signal = TestPlannerSchemaSignalFix()
        signal.setUp()
        return signal._retry21_attempt5_nodes()

    def _assert_planner_num_ctx(self, call_mock) -> None:
        """Every planner call in the mocked sequence carried the wider
        window — and the caller's settings object was not mutated."""
        from freetalon.orchestrator.planner import _PLANNER_NUM_CTX

        self.assertEqual(_PLANNER_NUM_CTX, 12288)
        for call in call_mock.call_args_list:
            self.assertEqual(call[1]["settings"].num_ctx, 12288)
        self.assertIsNone(self.settings.num_ctx)

    # --- (a) Verbatim retry-#22 outputs: five identical empty plans ---

    def test_retry22_empty_outputs_raise_empty_plan_error(self) -> None:
        from freetalon.orchestrator.planner import EmptyPlanError

        # All five RAW PLANNER OUTPUT blocks of
        # logs/llm-end-to-end-health-retry22-2026-11-16.log are
        # byte-identical {"nodes":[]} (probe tools/probe-empty-retry22.py,
        # logs/probe-retry22-empty.log). Each raises EmptyPlanError —
        # there is no numbered rejection; there is nothing to count.
        raw = '{"nodes":[]}'
        for _ in range(5):
            with self.assertRaises(EmptyPlanError) as caught:
                parse_planner_response(
                    raw, goal_text=self.HEALTH_GOAL, workspace=self.WS
                )
            self.assertIn("at least one plan node", str(caught.exception))

    # --- (b) Empty runs land within budget; the budget is unchanged ---

    def test_empty_empty_grounded_lands_within_budget(self) -> None:
        empty = '{"nodes": []}'
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, empty, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)
        self._assert_planner_num_ctx(call_mock)

    def test_empty_empty_empty_grounded_lands_within_budget(self) -> None:
        empty = '{"nodes": []}'
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, empty, empty, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # 1 initial + 2 free re-prompts + 1 repair: inside the existing
        # 1 + 2 free + 2 repairs ceiling. A third empty does not need a
        # fail-fast — it lands on the next call, within budget.
        self.assertEqual(call_mock.call_count, 4)
        self.assertEqual(len(plan.nodes), 4)
        self._assert_planner_num_ctx(call_mock)

    def test_empty_five_times_still_escalates_with_grounded_move(self) -> None:
        from freetalon.orchestrator.planner import EmptyPlanError

        # The budget is NOT changed by this fix: five empties still burn
        # all 5 calls (1 initial + 2 free + 2 repairs) and escalate with
        # the grounded move. The fix is delivery (the window), not a
        # shorter fuse — ending the failure sooner would not produce a
        # plan.
        empty = '{"nodes": []}'
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, empty, empty, empty, empty],
        ) as call_mock:
            with self.assertRaises(EmptyPlanError) as caught:
                plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 5)
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)
        self.assertIn("empty plans", message)
        self._assert_planner_num_ctx(call_mock)

    def test_planner_num_ctx_covers_measured_repair_round(self) -> None:
        from freetalon.orchestrator.planner import _PLANNER_NUM_CTX

        # The measured full repair round needs 8,102 tokens of context
        # (first repair prompt 6,415 + grounded-plan output 1,687).
        # The window covers it with ~4,000 tokens spare for longer goals
        # and replan briefs; 8192 would leave only ~90 spare.
        self.assertGreaterEqual(
            _PLANNER_NUM_CTX, self.FULL_REPAIR_ROUND_TOKENS + 1000
        )
        self.assertGreater(_PLANNER_NUM_CTX, self.FIRST_REPAIR_PROMPT_TOKENS)
        self.assertGreater(_PLANNER_NUM_CTX, self.INITIAL_PROMPT_TOKENS)

    def test_planner_respects_explicit_num_ctx(self) -> None:
        from dataclasses import replace as dataclass_replace

        settings = dataclass_replace(self.settings, num_ctx=8192)
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            return_value=good,
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=settings)
        # An explicit num_ctx is respected, never overridden with the
        # planner default.
        self.assertEqual(call_mock.call_args_list[0][1]["settings"].num_ctx, 8192)
        self.assertEqual(len(plan.nodes), 4)

    # --- (c) Mixed attractor sequences land within budget ---

    def test_empty_placeholder_grounded_lands_within_budget(self) -> None:
        empty = '{"nodes": []}'
        placeholder = self._dag(self._retry21_attempt5_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[empty, placeholder, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Empty (free 1) + placeholder (cheap, free 2) + grounded:
        # 3 calls, inside the existing budget.
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)
        self._assert_planner_num_ctx(call_mock)

    def test_flask_empty_grounded_lands_within_budget(self) -> None:
        flask = TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW
        empty = '{"nodes": []}'
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[flask, empty, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        # Flask (repair 1) + empty (free 1) + grounded: 3 calls, inside
        # the existing budget.
        self.assertEqual(call_mock.call_count, 3)
        self.assertEqual(len(plan.nodes), 4)
        self._assert_planner_num_ctx(call_mock)

    # --- (d) The ROUTE GOAL shape is last in the prompt the model gets ---

    def test_initial_prompt_carries_shape_last(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            return_value=good,
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(len(plan.nodes), 4)
        user_prompt = call_mock.call_args_list[0][0][1]
        shape = _route_goal_shape(self.HEALTH_GOAL, self.repo_root, delta=False)
        self.assertIn("ROUTE GOAL", user_prompt)
        self.assertIn(shape, user_prompt)
        # The instruction comes first; the shape is the last content
        # before the output marker — only "\n\nJSON Output:" follows it.
        # Before this fix the shape sat at ~70% of the prompt with 182
        # chars (the instruction paragraph) after it.
        self.assertLess(
            user_prompt.find("Produce a NEW execution plan DAG"),
            user_prompt.find(shape),
        )
        self.assertTrue(user_prompt.endswith("JSON Output:"))
        self.assertEqual(
            user_prompt[user_prompt.find(shape) + len(shape) :],
            "\n\nJSON Output:",
        )

    def test_replan_prompt_carries_shape_last(self) -> None:
        from freetalon.orchestrator.planner import (
            _goal_text_from_brief,
            _route_goal_shape,
            replan_from_brief,
        )

        fake = TestPlannerFakeAndMemoryFix()
        fake.setUp()
        brief = fake._brief()
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            return_value=good,
        ) as call_mock:
            nodes = replan_from_brief(brief, settings=self.settings)
        self.assertEqual(len(nodes), 4)
        user_prompt = call_mock.call_args_list[0][0][1]
        shape = _route_goal_shape(_goal_text_from_brief(brief), None, delta=True)
        self.assertIn("ROUTE GOAL", user_prompt)
        self.assertIn(shape, user_prompt)
        self.assertLess(
            user_prompt.find("Produce a NEW delta plan DAG"),
            user_prompt.find(shape),
        )
        self.assertTrue(user_prompt.endswith("JSON Output:"))
        self.assertEqual(
            user_prompt[user_prompt.find(shape) + len(shape) :],
            "\n\nJSON Output:",
        )
        self._assert_planner_num_ctx(call_mock)

    # --- num_ctx plumbing: intake keeps 4096, planner gets the window ---

    def test_ollama_uses_default_num_ctx_without_setting(self) -> None:
        from freetalon.orchestrator.intake import _call_ollama

        settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self.assertIsNone(settings.num_ctx)
        with patch(
            "freetalon.orchestrator.intake._post_json",
            return_value={"message": {"content": "ok"}},
        ) as post_mock:
            content = _call_ollama(settings, "sys", "user")
        self.assertEqual(content, "ok")
        # The intake path keeps the 4096 default — its prompts are small.
        self.assertEqual(post_mock.call_args[0][1]["options"]["num_ctx"], 4096)

    def test_ollama_uses_explicit_num_ctx(self) -> None:
        from dataclasses import replace as dataclass_replace

        from freetalon.orchestrator.intake import _call_ollama

        settings = dataclass_replace(
            LLMSettings(
                backend="ollama", base_url="http://localhost:11434", model="test"
            ),
            num_ctx=12288,
        )
        with patch(
            "freetalon.orchestrator.intake._post_json",
            return_value={"message": {"content": "ok"}},
        ) as post_mock:
            _call_ollama(settings, "sys", "user")
        self.assertEqual(post_mock.call_args[0][1]["options"]["num_ctx"], 12288)

    # --- (e) Legitimate plans still pass ---

    def test_grounded_plan_still_passes(self) -> None:
        nodes, _ = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)

    def test_heredoc_with_real_test_code_passes(self) -> None:
        # The skeleton's step-2 heredoc, dropped into the grounded plan
        # in place of the old HEREDOC_TEST, is real code -- the
        # placeholder guard stays out of the way.
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(self.HEALTH_GOAL, self.repo_root, self.WS)
        parsed = json.loads(skeleton.split("\n", 1)[1])
        step2_cmd = parsed["nodes"][1]["inputs"]["command"]
        nodes = self._grounded_health_nodes()
        nodes[1]["inputs"]["command"] = step2_cmd
        nodes[1]["outputs"] = [f"{self.WS}/test_health.py"]
        nodes[1]["criteria"] = [{"type": "file_exists", "path": f"{self.WS}/test_health.py"}]
        parsed_nodes, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed_nodes), 4)

    def test_heredoc_with_real_module_code_passes(self) -> None:
        # A heredoc that writes real module code into a NEW file passes
        # untouched -- scope check for the placeholder rule in the schema
        # path: only comment-only content is a placeholder.
        ws = self.WS
        nodes = [
            {
                "id": "step-1",
                "objective": "Write a health-check helper",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": (
                        f"cat > {ws}/health_check.py <<'EOF'\n"
                        "import json\n"
                        "\n"
                        "\n"
                        "def check(payload):\n"
                        '    return payload.get("ok", False)\n'
                        "EOF"
                    )
                },
                "outputs": [f"{ws}/health_check.py"],
                "criteria": [{"type": "file_exists", "path": f"{ws}/health_check.py"}],
            },
            {
                "id": "step-2",
                "objective": "Verify /health live",
                "depends_on": ["step-1"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -s http://localhost:8765/health"},
                "outputs": ["verification"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]
        parsed, _ = parse_planner_response(
            self._dag(nodes), goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(parsed), 2)

    # --- retry #21 guards: the schema-signal fix still holds ---

    def test_retry21_attempt2_names_typo_and_placeholder_in_one_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(self._retry21_attempt2_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # 2 = the placeholder-heredoc error (step-1) + the typo (step-4).
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("assigned_claws", message)
        self.assertIn("placeholder content only", message)
        self.assertIn("test_health.py", message)
        self.assertIn("grounded move", message)

    def test_retry21_attempt5_rejected_with_1_error_cheap(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(self._retry21_attempt5_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        # Repo-root fix (retry #26): was 1 error, and cheap. The parse
        # passes no repo_root -- the driver path -- where the
        # interpreter existence check used to skip path tokens, so
        # step-3's two ./venv/bin/python commands (the venv is .venv;
        # the path does not exist) were never named and the placeholder
        # error stood alone. Now 3 errors, and NOT cheap: cheap covers
        # only stand-in/placeholder fakes, and this plan also carries
        # two real interpreter defects a repair round must fix.
        # Interpreter normalization (retry #27 fix): step-3's two
        # ./venv/bin/python commands are rewritten to ./.venv/bin/python at
        # parse time, so the interpreter errors are gone and the placeholder
        # error stands alone again -- 1 error, and cheap once more.
        self.assertIsInstance(caught.exception, CheapRejectionError)
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertNotIn("./venv/bin/python", message)
        self.assertIn("placeholder content only", message)
        self.assertIn("test_health.py", message)
        self.assertIn("grounded move", message)

    # --- (f) Regression guards: every earlier fix still holds ---

    def test_retry20_v1_rejected_cheaply_naming_placeholder_heredocs(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        seeded = TestPlannerSeededReplanFix()
        seeded.setUp()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(seeded._retry20_v1_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("placeholder", message)
        self.assertIn("health.py", message)
        self.assertIn("test_health.py", message)

    def test_retry19_attempt2_rejected_with_5_errors(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("/tmp/health_response.json", message)

    def test_retry19_attempt3_rejected_with_3_errors_naming_flask(self) -> None:
        steer = TestPlannerForbiddenSteerFix()
        steer.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                steer.RETRY19_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 3 errors", message)
        # Framework-guard fix (retry #23): was 1 error (Flask only). Now 3 — the rejection also names the unparseable step-2 scaffold and step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- step-3's bare
        # `python` is rewritten at parse time and no longer errors.
        # Planner A/B refactor (defect 1): now 3 -- step-1's Flask
        # fragment also faces the retention guard (it drops all 12
        # of api.py's definitions); the fragment-scale floor that
        # exempted sub-four-definition scaffolds is gone.
        self.assertIn("does not parse as Python", message)
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do_GET method", message)

    def test_retry18_attempt2_rejected_with_5_errors(self) -> None:
        nodes = TestPlannerSignalLossFix._retry18_attempt2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 4 errors. Now 5 — the rejection also names step-3's bare `python`.
        # Interpreter-grounding fix (retry #25): was 5 errors. Now 6 -- stand-in shape 3 also names step-1's heredoc payload binding "ollama_reachable": True / "gpu_temperature": 0 as constants.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("freetalon/tests/test_api.py", message)
        self.assertIn("/tmp/health_response.json", message)

    def test_retry18_attempt3_rejected_with_5_errors(self) -> None:
        nodes = TestPlannerSignalLossFix._retry18_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 4 errors", message)
        # Planner cycle 2 (payload channel): now 4 -- step-2's
        # heredoc payload is repaired and rerouted to the scaffold
        # form at materialization, so its parse error no longer
        # fires; the remaining defects are unchanged and still
        # reject the plan.
        # Framework-guard fix (retry #23): was 5 errors. Now 6 — the rejection also names step-3's bare `python`.
        # Interpreter normalization (retry #27 fix): now 5 -- step-3's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the other five defects are unchanged.
        self.assertNotIn("bare `python`", message)
        self.assertIn("response_content", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("sed -i", message)
        self.assertIn("freetalon/tests/test_api.py", message)

    def test_retry17_attempt4_is_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        cheap = TestPlannerPlaceholderCheapFix()
        cheap.setUp()
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                self._dag(cheap._retry17_attempt4_nodes()),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("placeholder", message)
        self.assertIn("step-1", message)
        self.assertIn("freetalon/api.py", message)

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
        self.assertIn(f"line {_expected_retry16_error_line()}", message)
        _assert_code_append_parse_error(self, message)

    def test_retry15_attempt2_with_8_errors(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                TestPlannerDuplicateKeysFix.RETRY15_ATTEMPT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): the parse passes no repo_root
        # (the driver path), where the interpreter existence check
        # used to skip path tokens; node-1's mangled command now also
        # names '@app.route(/health)' as a path that does not exist
        # in the repo.
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("duplicate", message.lower())
        self.assertIn("writes a Flask app", message)
        self.assertIn("/tmp", message)

    def test_retry14_v1_three_prose_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 3 errors. Now 5 -- the parse
        # passes no repo_root (the driver path), where the interpreter
        # existence check used to skip path tokens entirely; node-1's
        # and node-2's citation tokens ('.../freetalon/api.py:HiveAPIServer,'
        # and '.../freetalon/api.py:Unit') are now also named as paths
        # that do not exist in the repo. The 3 prose errors are unchanged.
        self.assertIn("was rejected with 5 errors", message)
        self.assertIn("prose", message)

    def test_retry14_v2_three_errors(self) -> None:
        nodes = TestPlannerScaffoldGuardFix._retry14_v2_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Repo-root fix (retry #26): was 2 errors. Now 3 -- node-3's
        # './test.sh' is now also named as a path that does not exist
        # in the repo: the parse passes no repo_root (the driver
        # path), where the interpreter existence check used to skip
        # path tokens entirely.
        self.assertIn("was rejected with 3 errors", message)

    def test_retry14_v3_one_error_names_api(self) -> None:
        guard = TestPlannerScaffoldGuardFix()
        guard.setUp()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([guard._retry14_v3_node()]),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("freetalon/api.py", message)

    def test_retry13_attempt2_is_cheap_rejection(self) -> None:
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

    def test_retry13_attempt4_flask_scaffold_names_handler_branch(self) -> None:
        nodes = TestPlannerFrameworkSteerFix._retry13_attempt4_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        # Framework-guard fix (retry #23): was 1 error (Flask only).
        # Now 3 — the rejection also names the unparseable Flask
        # scaffold and node-2's bare `python`.
        # Interpreter normalization (retry #27 fix): now 2 -- node-2's bare
        # `python` is rewritten to ./.venv/bin/python at parse time and no
        # longer errors; the Flask and scaffold-parse errors remain.
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("writes a Flask app", message)
        self.assertIn("do_GET method", message)

    def test_retry12_attempt2_string_ids_is_code_overwrite(self) -> None:
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

    def test_retry11_attempt3_invented_schema_is_numbered_rejection(self) -> None:
        nodes = TestPlannerShapeSurvivesRepair._retry11_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 8 errors", message)
        self.assertIn("objective", message)

    def test_retry10_attempt3_is_numbered_rejection(self) -> None:
        nodes = TestPlannerDependsOnFix._retry10_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn(
            f"was rejected with {_expected_retry10_error_count()} errors", message
        )
        self.assertIn("depends on unknown node", message)

    def test_retry9_v1_rejected_wrong_location(self) -> None:
        nodes = TestPlannerRepoRootFix._retry9_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("2 errors", message)
        self.assertIn("repo root", message)
        self.assertIn(str(self.repo_root), message)
        self.assertIn(f"{self.WS}/freetalon/api.py", message)

    def test_retry8_v1_rejected_unwritten_file(self) -> None:
        nodes = TestPlannerUnwrittenFileFix._retry8_v1_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("health.json", message)

    def test_retry7_attempt3_invented_schema_is_numbered_rejection(self) -> None:
        nodes = TestPlannerNodeShapeFix._retry7_attempt3_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("was rejected with", message)
        self.assertIn("PlanNode object", message)

    def test_retry5_fake_rejected_with_grounded_move(self) -> None:
        nodes = TestPlannerGroundedShapeFix._retry5_fake_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("grounded move", message)
        self.assertIn("freetalon/api.py", message)

    def test_retry4_plan_rejected_wrong_service(self) -> None:
        nodes = TestPlannerRouteEngagementFix._retry4_plan_nodes()
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("11434", message)
        self.assertIn("8765", message)

    def test_retry3_glued_echo_rejected_with_heredoc_alternative(self) -> None:
        heredoc = TestPlannerHeredocFix()
        heredoc.setUp()
        node = heredoc._bash_node(TestPlannerHeredocFix.GLUED_ECHO)
        with self.assertRaisesRegex(LLMResponseError, "glued together"):
            parse_planner_response(
                self._dag([node]), goal_text=self.HEALTH_GOAL
            )

    def test_bare_dict_criteria_coerced_and_rejected_by_name(self) -> None:
        nodes = self._grounded_health_nodes()
        nodes[0]["criteria"] = {"tests_pass": True}
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("criteria MUST be a JSON array", message)

    def test_duplicate_ids_cycles_self_dependency_raise_first(self) -> None:
        base = {
            "objective": "First",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "true"},
            "outputs": ["result"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }
        with self.assertRaisesRegex(LLMResponseError, "duplicate node ids"):
            normalize_plan_nodes([dict(base, id="a"), dict(base, id="a")])
        with self.assertRaisesRegex(LLMResponseError, "dependency cycle"):
            normalize_plan_nodes(
                [
                    dict(base, id="a", depends_on=["b"]),
                    dict(base, id="b", depends_on=["a"]),
                ]
            )
        with self.assertRaisesRegex(LLMResponseError, "cannot depend on itself"):
            normalize_plan_nodes([dict(base, id="a", depends_on=["a"])])


class TestPlannerFrameworkGuardFix(unittest.TestCase):
    """The framework guard disarms itself after a bad scaffold write (retry #23).

    Retry #23 (log llm-end-to-end-health-retry23-2026-11-16.log, run
    run-20261117T150407Z-a461f2, plan plan-7885a4ef9d99) FAILED in 208s,
    escalated: re-planner failed, DRIVER_EXIT:0. First plan accepted since
    retry #20. Initial planning: (1) a stdlib http.server scaffold plan —
    ACCEPTED, became v1; (2) replan 1, a Flask plan — ACCEPTED, became v2;
    (3)(4)(5) replan 2 calls REJECTED with exactly 1 error each (unknown
    criterion type string_matches; the placeholder stub in step-1 was NOT
    named). Execution: v1 node-3 FAIL (exit 127, ./venv/bin/python not
    found), v2 node-3 FAIL (exit 127, python: not found). Two measured
    mechanisms: (a) the framework guard disarms itself after the first
    bad write — _framework_errors reads index.frameworks, built from
    _imported_modules, which returns an empty set on SyntaxError, so
    once v1 overwrote api.py with unparseable scaffold the index
    recorded NO framework and the Flask check skipped every later plan
    (proven by re-probe with api.py restored: output 2 then REJECTED
    with 2 errors, outputs 4/5 with 2); (b) exit_code criteria never
    run their command — _eval_exit_code compares the NODE's exit code
    and ignores the criterion's command field, so v1 node-1 "passed"
    its curl -sf criterion because the scaffold write exited 0.

    Fixes, in priority order: (1) the serving-framework lookup reads
    the committed module (git HEAD) and falls back to the working tree
    only when HEAD names no framework; scaffold writes of unparseable
    Python to a code file are rejected at plan time, like bash heredoc
    writes; a placeholder comment plus a trivial stub is rejected as a
    placeholder stub. (2) An exit_code criterion with a command runs
    that command as the measurement; a bare criterion still measures
    the node command. (3) Full signal in replan: the constituent checks
    above now name every defect, so one rejection carries framework +
    placeholder + interpreter + unusable-criterion together — verified
    by replaying the retry-#23 probe outputs against the restored
    repo. (4) A bash command's interpreter/binary must exist in the
    repo (bare `python` never resolves on Ubuntu — the grounded form
    is ./.venv/bin/python). (5) The replan is seeded with the
    repo-level known-bad approaches (Flask, placeholder) from the
    start, in the FIRST prompt — not only in repair prompts.

    Fixtures below are the retry-#23 probe outputs (restored-log
    numbering), rebuilt as node dicts with the real scaffold contents,
    commands, and criteria from logs/probe-retry23-rejections-
    restored.log. Expected counts were verified by parsing each
    scaffold content with ast.parse: every Flask scaffold in retry #23
    is unparseable too (def/class bodies at column 0), so the
    scaffold-parse error fires alongside the framework error.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    API_PATH = "/home/seeker/projects/FreeTalon/freetalon/api.py"
    TEST_API_PATH = "/home/seeker/projects/FreeTalon/tests/test_api.py"

    # Output 1 (v1, ACCEPTED live): the http.server scaffold. Step-1
    # content is the real scaffold's head — `def handle_health(self):`
    # followed by a top-level `try:` does not parse. Step-2's test
    # scaffold is the real content's head — the class body is at
    # column 0 and does not parse either.
    RETRY23_OUTPUT1_STEP1_CONTENT = (
        "import json\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "\n"
        "def handle_health(self):\n"
        "try:\n"
        "import subprocess\n"
    )
    RETRY23_OUTPUT1_STEP2_CONTENT = (
        "import unittest\n"
        "from freetalon.api import HiveAPIServer\n"
        "\n"
        "class TestHealthEndpoint(unittest.TestCase):\n"
        "def test_health_endpoint(self):\n"
        "# Assuming the server is running on localhost:8765\n"
        "response = requests.get('http://localhost:8765/health')\n"
    )

    # Output 3 step-1 content, verbatim: the placeholder stub the
    # live rejection never named.
    RETRY23_OUTPUT3_STEP1_CONTENT = (
        "# Add health check logic to api.py\n"
        "# This is a placeholder, replace with actual implementation.\n"
        "def health():\n"
        " return {'status': 'OK'}\n"
    )
    # Output 3 step-2 content, verbatim: the method body is at the
    # same indent as the def, so it does not parse (line 6).
    RETRY23_OUTPUT3_STEP2_CONTENT = (
        "import unittest\n"
        "from freetalon.api import health\n"
        "\n"
        "class TestHealthEndpoint(unittest.TestCase):\n"
        " def test_health_endpoint(self):\n"
        " response = health()\n"
        " self.assertEqual(response, {'status': 'OK'})\n"
    )

    # Output 4 step-1 content, verbatim: Flask + unparseable.
    RETRY23_OUTPUT4_STEP1_CONTENT = (
        "# Add health check logic here\n"
        "def health():\n"
        "return {'status': 'OK'}\n"
        "\n"
        "@app.route('/health')\n"
        "def health_endpoint():\n"
        "return jsonify(health())"
    )
    # Output 4 step-2 content, verbatim: unparseable, no Flask.
    RETRY23_OUTPUT4_STEP2_CONTENT = (
        "import unittest\n"
        "from freetalon.api import health_endpoint\n"
        "\n"
        "class TestHealthEndpoint(unittest.TestCase):\n"
        "def test_health(self):\n"
        "response = health_endpoint()\n"
        "self.assertEqual(response.status_code, 200)\n"
        "self.assertEqual(response.json(), {'status': 'OK'})"
    )

    # Output 2 step-1 content, verbatim: Flask + unparseable.
    RETRY23_OUTPUT2_STEP1_CONTENT = (
        "# Add health check logic to api.py\n"
        "from flask import Flask, jsonify\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/health')\n"
        "def health_check():\n"
        "return jsonify({\n"
        "'status': 'OK',\n"
        "'ollama_reachable': True,\n"
        "'gpu_temperature': 'N/A'\n"
        "})\n"
        "\n"
        "if __name__ == '__main__':\n"
        "app.run(debug=True)"
    )
    # Output 2 step-2 content, verbatim: Flask + unparseable.
    RETRY23_OUTPUT2_STEP2_CONTENT = (
        "import unittest\n"
        "from flask import Flask\n"
        "from freetalon.api import app\n"
        "\n"
        "class TestHealthEndpoint(unittest.TestCase):\n"
        "def test_health_endpoint(self):\n"
        "response = app.get('/health')\n"
        "self.assertEqual(response.status_code, 200)\n"
        "data = response.get_json()\n"
        "self.assertEqual(data['status'], 'OK')\n"
        "self.assertTrue(data['ollama_reachable'])\n"
        "self.assertEqual(data['gpu_temperature'], 'N/A')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "unittest.main()"
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
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _brief(self) -> dict:
        """A fresh re-plan brief, carrying no seed (the retry-#23 condition)."""
        fake = TestPlannerFakeAndMemoryFix()
        fake.setUp()
        return fake._brief()

    def _grounded_health_nodes(self) -> list[dict]:
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    def _retry23_output1_nodes(self) -> list[dict]:
        api = self.API_PATH
        ws = self.WS
        return [
            {
                "id": "step_1",
                "objective": "Modify freetalon/api.py to implement the /health endpoint",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": api, "content": self.RETRY23_OUTPUT1_STEP1_CONTENT},
                "outputs": [api],
                "acceptance": ["File exists", "API responds to /health"],
                "criteria": [
                    {"type": "file_exists", "path": api},
                    {"type": "exit_code", "equals": 0, "command": "curl -sf http://localhost:8765/health"},
                ],
            },
            {
                "id": "step_2",
                "objective": "Write and run unit tests for the /health endpoint",
                "depends_on": ["step_1"],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": f"{ws}/test_api.py", "content": self.RETRY23_OUTPUT1_STEP2_CONTENT},
                "outputs": [f"{ws}/test_api.py"],
                "acceptance": ["Tests pass"],
                "criteria": [
                    {"type": "exit_code", "equals": 0, "command": f"python {ws}/test_api.py"},
                ],
            },
            {
                "id": "step_3",
                "objective": "Start the FreeTalon API server and verify it's live",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": "cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"},
                "outputs": ["server running"],
                "acceptance": ["Server is live and responding to requests"],
                "criteria": [
                    {"type": "exit_code", "equals": 0, "command": "curl -sf http://localhost:8765/health"},
                ],
            },
        ]

    def _retry23_output3_nodes(self) -> list[dict]:
        api = self.API_PATH
        test_api = self.TEST_API_PATH
        return [
            {
                "id": "step_1",
                "objective": "Modify freetalon/api.py to implement the /health endpoint.",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": api, "content": self.RETRY23_OUTPUT3_STEP1_CONTENT},
                "outputs": [api],
                "acceptance": ["The /health endpoint returns JSON with status OK."],
                "criteria": [{"type": "file_exists", "path": api}],
            },
            {
                "id": "step_2",
                "objective": "Write unit tests for the /health endpoint.",
                "depends_on": ["step_1"],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": test_api, "content": self.RETRY23_OUTPUT3_STEP2_CONTENT},
                "outputs": [test_api],
                "acceptance": ["Unit tests pass for the /health endpoint."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_3",
                "objective": "Start the FreeTalon API server and verify it's live.",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": "/home/seeker/projects/FreeTalon/.venv/bin/python -m freetalon.cli start"},
                "outputs": ["server running"],
                "acceptance": ["The API server is running and accessible."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_4",
                "objective": "Verify the /health endpoint returns OK.",
                "depends_on": ["step_3"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -sf http://localhost:8765/health"},
                "outputs": ["response from curl"],
                "acceptance": ["The /health endpoint returns JSON with status OK."],
                "criteria": [
                    {"type": "string_matches", "pattern": "'status': 'OK'", "input": "curl -sf http://localhost:8765/health"}
                ],
            },
        ]

    def _retry23_output4_nodes(self) -> list[dict]:
        api = self.API_PATH
        test_api = self.TEST_API_PATH
        return [
            {
                "id": "step_1",
                "objective": "Modify freetalon/api.py to implement the /health endpoint",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": api, "content": self.RETRY23_OUTPUT4_STEP1_CONTENT},
                "outputs": [api],
                "acceptance": ["The /health endpoint is implemented in api.py"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_2",
                "objective": "Write unit tests for the /health endpoint",
                "depends_on": ["step_1"],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": test_api, "content": self.RETRY23_OUTPUT4_STEP2_CONTENT},
                "outputs": [test_api],
                "acceptance": ["Unit tests for the /health endpoint are written"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_3",
                "objective": "Run unit tests for the /health endpoint",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": "python -m unittest discover"},
                "outputs": ["test results"],
                "acceptance": ["Unit tests pass"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_4",
                "objective": "Verify the API is live and returns OK",
                "depends_on": ["step_3"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -sf http://localhost:8765/health"},
                "outputs": ["API response"],
                "acceptance": ["The /health endpoint returns a 200 status code with JSON body {'status': 'OK'}"],
                "criteria": [{"type": "string_matches", "equals": ".*{\"status\": \"OK\"}.*", "field": "stdout"}],
            },
        ]

    def _retry23_output2_nodes(self) -> list[dict]:
        api = self.API_PATH
        test_api = self.TEST_API_PATH
        return [
            {
                "id": "step_1",
                "objective": "Modify freetalon/api.py to implement the /health endpoint.",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": api, "content": self.RETRY23_OUTPUT2_STEP1_CONTENT},
                "outputs": [api],
                "acceptance": ["The /health endpoint is implemented in api.py."],
                "criteria": [{"type": "file_exists", "path": api}],
            },
            {
                "id": "step_2",
                "objective": "Write unit tests for the /health endpoint.",
                "depends_on": ["step_1"],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": test_api, "content": self.RETRY23_OUTPUT2_STEP2_CONTENT},
                "outputs": [test_api],
                "acceptance": ["Unit tests for the /health endpoint are written."],
                "criteria": [{"type": "file_exists", "path": test_api}],
            },
            {
                "id": "step_3",
                "objective": "Run the unit tests for /health.",
                "depends_on": ["step_2"],
                "assigned_claw": "bash",
                "inputs": {"command": "python /home/seeker/projects/FreeTalon/tests/test_api.py"},
                "outputs": [],
                "acceptance": ["The unit tests pass."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_4",
                "objective": "Start the FreeTalon API server and verify it's live.",
                "depends_on": ["step_3"],
                "assigned_claw": "bash",
                "inputs": {"command": "cd /home/seeker/projects && ./freetalon/api.py start"},
                "outputs": [],
                "acceptance": ["The API server is running and accessible."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "step_5",
                "objective": "Verify the /health endpoint returns OK.",
                "depends_on": ["step_4"],
                "assigned_claw": "bash",
                "inputs": {"command": "curl -sf http://localhost:8765/health"},
                "outputs": [],
                "acceptance": ["The /health endpoint returns a JSON response with status OK."],
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ]

    def _reject(self, nodes: list[dict]) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=self.repo_root,
            )
        return str(caught.exception)

    # --- (1a) HEAD framework lookup survives a clobbered working tree ---

    def _init_git_repo_with_api(self, root: Path) -> Path:
        """A temp git repo whose committed api.py serves /health on stdlib http.server."""
        import subprocess

        api = root / "freetalon" / "api.py"
        api.parent.mkdir(parents=True, exist_ok=True)
        api.write_text(
            "from http.server import BaseHTTPRequestHandler\n"
            "\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        if self.path == '/health':\n"
            "            self.send_response(200)\n",
            encoding="utf-8",
        )
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="test",
            GIT_AUTHOR_EMAIL="test@test",
            GIT_COMMITTER_NAME="test",
            GIT_COMMITTER_EMAIL="test@test",
        )
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "api"], check=True, env=env
        )
        return api

    def test_head_framework_survives_unparseable_working_tree(self) -> None:
        import shutil
        import tempfile

        from freetalon.orchestrator.repo_index import build_repo_index

        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            api = self._init_git_repo_with_api(Path(tmp))
            # (a) Unparseable scaffold overwrite: the working-tree import
            # scan returns nothing, but HEAD still names http.server.
            api.write_text("def health():\ntry:\n", encoding="utf-8")
            index = build_repo_index(Path(tmp))
            self.assertEqual(
                index.frameworks.get("freetalon/api.py"), "stdlib http.server"
            )
            # (b) Parseable overwrite in a different framework: the
            # working tree now says Flask, but HEAD still wins.
            api.write_text(
                "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
            )
            index = build_repo_index(Path(tmp))
            self.assertEqual(
                index.frameworks.get("freetalon/api.py"), "stdlib http.server"
            )

    def test_head_source_returns_none_outside_git(self) -> None:
        import tempfile

        from freetalon.orchestrator.repo_index import _head_source, build_repo_index

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "freetalon").mkdir()
            (root / "freetalon" / "api.py").write_text(
                "from http.server import BaseHTTPRequestHandler\n", encoding="utf-8"
            )
            # Fail-soft: not a git checkout, so no HEAD source — and the
            # working-tree framework is still recorded.
            self.assertIsNone(_head_source(root, "freetalon/api.py"))
            index = build_repo_index(root)
            self.assertEqual(
                index.frameworks.get("freetalon/api.py"), "stdlib http.server"
            )

    def test_flask_plan_rejected_when_working_tree_clobbered(self) -> None:
        import shutil
        import tempfile

        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            api = self._init_git_repo_with_api(Path(tmp))
            api.write_text("def health():\ntry:\n", encoding="utf-8")
            flask_node = {
                "id": "step_1",
                "objective": "Write a Flask app for /health",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "freetalon/api.py",
                    "content": "from flask import Flask\napp = Flask(__name__)\n",
                },
                "outputs": ["freetalon/api.py"],
                "acceptance": ["Flask app serves /health"],
                "criteria": [{"type": "file_exists", "path": "freetalon/api.py"}],
            }
            # Judged against the temp repo, not the real repo root.
            with self.assertRaises(LLMResponseError) as caught:
                parse_planner_response(
                    self._dag([flask_node]),
                    goal_text=self.HEALTH_GOAL,
                    workspace=self.WS,
                    repo_root=Path(tmp),
                )
            self.assertIn("writes a Flask app", str(caught.exception))

    # --- (1b) Scaffold parse check + placeholder-stub rule ---

    def test_retry23_output1_rejected_with_two_errors(self) -> None:
        # Was ACCEPTED live (v1). Then 4 errors: scaffold-parse on
        # step-1 and step-2, bare `python` in step-2's criterion
        # command, and the missing ./venv/bin/python in step-3's
        # command. Interpreter normalization (retry #27 fix): both
        # interpreter spellings are rewritten to ./.venv/bin/python
        # at parse time -- now 2 errors, the two scaffold parses.
        message = self._reject(self._retry23_output1_nodes())
        self.assertIn("was rejected with 2 errors", message)
        self.assertEqual(message.count("does not parse as Python"), 2)
        self.assertNotIn("bare `python`", message)
        self.assertNotIn("./venv/bin/python", message)
        self.assertIn("./.venv/bin/python", message)

    def test_retry23_output3_rejected_with_three_errors(self) -> None:
        # Was REJECTED live with exactly 1 error (string_matches only).
        # Now the placeholder stub in step-1 and the unparseable test
        # scaffold in step-2 are named too. Step-3's command is the
        # correct .venv path, so no interpreter error fires there.
        message = self._reject(self._retry23_output3_nodes())
        self.assertIn("was rejected with 3 errors", message)
        self.assertIn("placeholder stub", message)
        self.assertEqual(message.count("does not parse as Python"), 1)
        self.assertIn("string_matches", message)

    def test_placeholder_stub_fires_for_foreign_absolute_path(self) -> None:
        # CI determinism (hosted run 44140283101): the output-3 step-1
        # stub, but with the target written as an absolute path from a
        # DIFFERENT machine (the /home/runner/... shape). Judged
        # against this repo, that target is freetalon/api.py, which
        # exists — so the stub rejection must fire identically
        # everywhere. Before the repo-relative existence fix, the
        # raw-path existence check silently skipped the node wherever
        # the foreign path did not exist on disk: output 3 rejected
        # with 3 errors on seeker7 but only 2 in CI.
        foreign = "/home/runner/work/FreeTalon/FreeTalon/freetalon/api.py"
        node = {
            "id": "step_1",
            "objective": "Modify freetalon/api.py to implement the /health endpoint.",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": foreign,
                "content": self.RETRY23_OUTPUT3_STEP1_CONTENT,
            },
            "outputs": [foreign],
            "acceptance": ["The /health endpoint returns JSON with status OK."],
            "criteria": [{"type": "file_exists", "path": foreign}],
        }
        message = self._reject([node])
        self.assertIn("placeholder stub", message)

    def test_retry23_output4_rejected_with_four_errors(self) -> None:
        # Was REJECTED live (restored probe) with 2 errors
        # (string_matches + step-1 Flask). Then: Flask step-1,
        # scaffold-parse step-1 and step-2, bare `python` step-3,
        # string_matches step-4 (5 errors). Interpreter normalization
        # (retry #27 fix): the bare `python` is rewritten at parse
        # time -- now 4 errors.
        message = self._reject(self._retry23_output4_nodes())
        self.assertIn("was rejected with 4 errors", message)
        self.assertEqual(message.count("writes a Flask app"), 1)
        self.assertEqual(message.count("does not parse as Python"), 2)
        self.assertNotIn("bare `python`", message)
        self.assertIn("string_matches", message)

    def test_retry23_output2_rejected_with_four_errors(self) -> None:
        # Was REJECTED live (restored probe) with 2 errors (Flask
        # step-1 + step-2). Then the unparseable scaffolds and the
        # bare `python` in step-3 were named too (5 errors). Step-4's
        # ./freetalon/api.py exists in the repo, so no interpreter
        # error fires there. Interpreter normalization (retry #27
        # fix): the bare `python` is rewritten at parse time -- now
        # 4 errors.
        message = self._reject(self._retry23_output2_nodes())
        self.assertIn("was rejected with 4 errors", message)
        self.assertEqual(message.count("writes a Flask app"), 2)
        self.assertEqual(message.count("does not parse as Python"), 2)
        self.assertNotIn("bare `python`", message)

    def test_is_placeholder_stub(self) -> None:
        from freetalon.orchestrator.planner import _is_placeholder_stub

        # The retry-#23 output-3 step-1 stub: marker comment + 2 lines.
        self.assertTrue(_is_placeholder_stub(self.RETRY23_OUTPUT3_STEP1_CONTENT))
        # A real implementation that merely mentions "placeholder" in a
        # comment is not a stub — more than three lines of code.
        real = (
            "# placeholder note, replace later if needed\n"
            "def health():\n"
            "    a = 1\n"
            "    b = 2\n"
            "    c = 3\n"
            "    d = 4\n"
            "    return {'status': 'OK'}\n"
        )
        self.assertFalse(_is_placeholder_stub(real))
        # No placeholder word at all — not a stub either.
        self.assertFalse(
            _is_placeholder_stub(
                "# Add health check logic here\n"
                "def health():\n"
                "    return {'status': 'OK'}\n"
            )
        )
        # // comments count too.
        self.assertTrue(
            _is_placeholder_stub(
                "// placeholder, TODO: implement this\nint main() { return 0; }\n"
            )
        )

    def test_placeholder_stub_heredoc_rejected_when_claimed(self) -> None:
        # The stub rule applies to heredocs too: a marker comment plus
        # a trivial stub, claimed by file_exists, is a fake deliverable.
        stub = self.RETRY23_OUTPUT3_STEP1_CONTENT
        node = {
            "id": "step_1",
            "objective": "Write the health endpoint",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": f"cat > tests/test_api.py <<'EOF'\n{stub}EOF"
            },
            "outputs": ["tests/test_api.py"],
            "acceptance": ["The health endpoint is implemented"],
            "criteria": [{"type": "file_exists", "path": "tests/test_api.py"}],
        }
        message = self._reject([node])
        self.assertIn("placeholder stub", message)

    def test_scaffold_parse_skips_literal_answer(self) -> None:
        # A scaffold whose content is already a hardcoded answer is
        # the stand-in rule's defect, not a parse error.
        node = {
            "id": "step_1",
            "objective": "Answer /health",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": '{"ok": true, "status": "ready"}',
            },
            "outputs": [self.api_path],
            "acceptance": ["The endpoint answers"],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        message = self._reject([node])
        self.assertNotIn("does not parse as Python", message)

    # --- (2) exit_code criteria run their command ---

    def test_exit_code_criterion_runs_its_command(self) -> None:
        import tempfile

        from freetalon.orchestrator.measure import evaluate_criterion

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env = {
                "node_id": "n1",
                "status": "completed",
                "exit_code": 0,
                "duration_s": 0.1,
                "stdout": "",
                "stderr": "",
                "artifacts": [],
                "metrics": {},
                "error": None,
            }
            # The command is the measurement: `false` fails even
            # though the node exited 0 (the retry-#23 shape — v1
            # node-1 "passed" its curl criterion on the write's exit).
            failed = evaluate_criterion(
                {"type": "exit_code", "equals": 0, "command": "false"},
                dict(env, exit_code=0),
                workdir,
            )
            self.assertFalse(failed["passed"])
            self.assertTrue(failed["measured"])
            self.assertIn("criterion command exit code 1", failed["detail"])
            # And a command whose exit matches `equals` passes even
            # when the node itself failed.
            passed = evaluate_criterion(
                {"type": "exit_code", "equals": 1, "command": "false"},
                dict(env, exit_code=0),
                workdir,
            )
            self.assertTrue(passed["passed"])
            self.assertTrue(passed["measured"])
            # A bare criterion still measures the node command's exit
            # code — the explicit default.
            bare = evaluate_criterion(
                {"type": "exit_code", "equals": 0}, dict(env, exit_code=1), workdir
            )
            self.assertFalse(bare["passed"])
            self.assertTrue(bare["measured"])
            # An unknown command is a measured failure (shell exit
            # 127), not a silent pass.
            missing = evaluate_criterion(
                {"type": "exit_code", "equals": 0, "command": "definitely-not-a-real-command-xyz"},
                env,
                workdir,
            )
            self.assertFalse(missing["passed"])
            self.assertTrue(missing["measured"])
            # A command that times out is a measured failure too.
            timed_out = evaluate_criterion(
                {"type": "exit_code", "equals": 0, "command": "sleep 2", "timeout_s": 0.05},
                env,
                workdir,
            )
            self.assertFalse(timed_out["passed"])
            self.assertTrue(timed_out["measured"])
            self.assertIn("timed out", timed_out["detail"])

    # --- (4) Bash interpreter existence ---

    def test_bash_interpreter_errors(self) -> None:
        from freetalon.orchestrator.planner import _bash_interpreter_errors

        root = self.repo_root
        bare = _bash_interpreter_errors("python -m unittest discover", root)
        self.assertEqual(len(bare), 1)
        self.assertIn("bare `python`", bare[0])
        self.assertIn("./.venv/bin/python", bare[0])
        wrong_venv = _bash_interpreter_errors(
            "cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start",
            root,
        )
        self.assertEqual(len(wrong_venv), 1)
        self.assertIn("./venv/bin/python", wrong_venv[0])
        # The grounded form is allowed: the repo venv is a machine
        # fact, not a repo-tree fact (CI has no .venv).
        self.assertEqual(
            _bash_interpreter_errors("./.venv/bin/python -m freetalon.cli start", root),
            [],
        )
        self.assertEqual(
            _bash_interpreter_errors("python3 tests/test_api.py", root), []
        )
        self.assertEqual(
            _bash_interpreter_errors("curl -sf http://localhost:8765/health", root),
            [],
        )
        self.assertEqual(
            _bash_interpreter_errors(
                "cd /home/seeker/projects && ./freetalon/api.py start", root
            ),
            [],
        )
        # Env assignments are stripped before the command is read.
        self.assertEqual(
            len(_bash_interpreter_errors("FOO=1 python foo.py", root)), 1
        )
        # Builtins and multi-segment commands: only real interpreters
        # are judged.
        self.assertEqual(
            _bash_interpreter_errors(
                "echo hi && python3 -m unittest discover -s tests", root
            ),
            [],
        )

    def test_exit_code_criterion_command_validated_at_plan_time(self) -> None:
        # A criterion command naming a nonexistent interpreter is
        # rejected at plan time, naming the grounded form. (Bare
        # `python` in a criterion command no longer reaches this
        # check: interpreter normalization rewrites it at parse
        # time -- pinned in TestPlannerInterpreterNormalizationFix.)
        node = {
            "id": "step_1",
            "objective": "Run the tests",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "python3 tests/test_api.py"},
            "outputs": ["test results"],
            "acceptance": ["Tests pass"],
            "criteria": [
                {"type": "exit_code", "equals": 0, "command": "./bin/python tests/test_api.py"},
            ],
        }
        message = self._reject([node])
        self.assertIn("unusable criterion", message)
        self.assertIn("./bin/python", message)
        self.assertIn("./.venv/bin/python", message)
        # A criterion command that resolves is fine — and a bare
        # exit_code criterion on a bash node stays valid (it measures
        # the node command). Both plans carry the scaffold step that
        # engages the route, so the route-engagement check is not the
        # thing under test here.
        scaffold = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        good = {
            "id": "step_2",
            "objective": "Run the tests",
            "depends_on": ["step_1"],
            "assigned_claw": "bash",
            "inputs": {"command": "python3 tests/test_api.py"},
            "outputs": ["test results"],
            "acceptance": ["Tests pass"],
            "criteria": [
                {"type": "exit_code", "equals": 0, "command": "curl -sf http://localhost:8765/health"},
            ],
        }
        nodes, _meta = parse_planner_response(
            self._dag([scaffold, good]),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=self.repo_root,
        )
        self.assertEqual(len(nodes), 2)
        bare = dict(good)
        bare["criteria"] = [{"type": "exit_code", "equals": 0}]
        nodes, _meta = parse_planner_response(
            self._dag([scaffold, bare]),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=self.repo_root,
        )
        self.assertEqual(len(nodes), 2)

    # --- (5) Seeded forbidden list carries the repo-level known-bad approaches ---

    def test_repo_forbidden_approaches(self) -> None:
        from freetalon.orchestrator.planner import _repo_forbidden_approaches
        from freetalon.orchestrator.repo_index import build_repo_index

        index = build_repo_index(self.repo_root)
        seed = _repo_forbidden_approaches(self.HEALTH_GOAL, index)
        self.assertTrue(any("Flask app" in a for a in seed))
        self.assertTrue(any("placeholder" in a for a in seed))
        # A goal naming no existing route seeds nothing.
        self.assertEqual(
            _repo_forbidden_approaches("Do something unrelated", index), []
        )
        self.assertEqual(_repo_forbidden_approaches(None, index), [])

    def test_replan_first_prompt_carries_repo_forbidden_seed(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        brief = self._brief()
        brief.pop("seed_forbidden", None)
        flask_raw = self._dag(self._retry23_output4_nodes())
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[flask_raw, good]
        ) as call_mock:
            nodes = replan_from_brief(brief, repo_root=self.repo_root)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 4)
        first_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("DO NOT use these approaches", first_prompt)
        self.assertIn("Do NOT write a Flask app", first_prompt)
        self.assertIn("The grounded move is:", first_prompt)
        self.assertIn("do_GET method of the Handler class", first_prompt)
        # The ROUTE GOAL shape stays last (retry #22): the seed block
        # goes before it, and only "JSON Output:" follows it.
        self.assertLess(
            first_prompt.index("DO NOT use these approaches"),
            first_prompt.index("ROUTE GOAL"),
        )
        self.assertTrue(first_prompt.rstrip().endswith("JSON Output:"))
        # The repair prompt still names Flask as forbidden too.
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("Do NOT write a Flask app", repair_prompt)
        # The accumulated list is written back into the brief for the
        # loop to carry into the next replan's seed.
        self.assertTrue(
            any("Flask app" in a for a in brief.get("rejected_approaches", []))
        )


class TestPlannerSanitizeFix(unittest.TestCase):
    """Retry #24: sanitize_llm_output deleted bare U+2581 space markers.

    Gemma 3 emits indentation in longer scaffold generations as runs of
    U+2581 (the SentencePiece space marker), not ASCII spaces. The
    sanitizer converted the mojibake form to a space but DELETED the
    genuine marker, so every affected scaffold arrived flush-left and
    failed the plan-time parse check -- all three retry-#24 attempts.
    The fix converts bare markers to the spaces they are. The UNK_BYTE
    pass runs first and consumes the bracketed artifacts whole; the
    artifact hex (0xe29681) is U+2581's own UTF-8 encoding, so both
    passes restore the same space arriving through two decode paths.
    """

    def test_bare_marker_runs_become_indentation(self) -> None:
        raw = "def health():\n▁▁▁▁return {'ok': True}\n"
        self.assertEqual(
            sanitize_llm_output(raw),
            "def health():\n    return {'ok': True}\n",
        )

    def test_bare_marker_mid_line_becomes_space(self) -> None:
        self.assertEqual(sanitize_llm_output("import▁json"), "import json")
        self.assertEqual(sanitize_llm_output("'a▁b'"), "'a b'")

    def test_unk_byte_artifact_and_bare_runs_together(self) -> None:
        # The interaction case from the captured wire response
        # (logs/indent-measure-raw.json): mid-line spaces ride inside
        # UNK_BYTE artifacts while the indentation is bare marker runs.
        # Converting the markers alone leaves the artifacts mangling
        # the lines; the artifact pass alone deletes the indentation.
        # Both together reproduce the code the model emitted.
        import ast

        raw = (
            "import[UNK_BYTE_0xe29681▁json]json\n"
            "\n"
            "def[UNK_BYTE_0xe29681▁health]health():\n"
            "▁▁▁▁return[UNK_BYTE_0xe29681▁json]json.dumps("
            "{'ok':[UNK_BYTE_0xe29681▁True]True})\n"
        )
        cleaned = sanitize_llm_output(raw)
        self.assertEqual(
            cleaned,
            "import json\n"
            "\n"
            "def health():\n"
            "    return json.dumps({'ok': True})\n",
        )
        ast.parse(cleaned)

    def test_mojibake_marker_still_becomes_space(self) -> None:
        # Pre-existing behavior, unchanged: the latin-1-mangled 3-char
        # form of the marker converts to a space.
        self.assertEqual(sanitize_llm_output("a\xe2\x96\x81b"), "a b")

    def test_plain_text_unchanged(self) -> None:
        text = "def f():\n    return 1\n"
        self.assertEqual(sanitize_llm_output(text), text)

    def test_marker_indented_scaffold_survives_parse(self) -> None:
        # Regression: a scaffold whose indentation is U+2581 runs (the
        # retry-#24 shape) must come through parse_planner_response
        # with its content converted to real spaces that parse --
        # before the fix the same plan was rejected for unparseable
        # scaffold code. ensure_ascii=False keeps the markers literal
        # in the raw text, exactly as the wire delivers them.
        # Planner A/B refactor (defect 1): the target is a scratch
        # module that does not exist yet -- the original fixture
        # scaffolded this fragment as the FULL content of
        # freetalon/api.py, which is a defect-1 overwrite (it drops
        # all 12 of the module's definitions) and the retention guard
        # now rejects it. The sanitize conversion this test pins is
        # target-independent.
        import ast
        import json
        import tempfile

        api_path = str(Path(tempfile.mkdtemp()) / "health_fragment.py")
        content = (
            "# extended health endpoint\n"
            "if path == \"/health\":\n"
            "▁▁▁▁self._send({\"ok\": True, \"status\": \"ready\"})"
        )
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": api_path, "content": content},
            "outputs": [api_path],
            "criteria": [{"type": "file_exists", "path": api_path}],
        }
        raw = json.dumps(
            {"nodes": [node], "metadata": {}}, ensure_ascii=False
        )
        nodes, _ = parse_planner_response(raw)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        converted = nodes[0].inputs["content"]
        self.assertNotIn("▁", converted)
        self.assertIn("\n    self._send(", converted)
        ast.parse(converted)


class TestIntakeSanitizeFix(unittest.TestCase):
    """Intake twin of the retry-#24 sanitize fix (planner: b3a0dfb).

    freetalon/orchestrator/intake.py carries a duplicate
    sanitize_llm_output for TaskIntent parsing, and it kept the old
    behavior the planner fix removed: bare U+2581 markers (Gemma's
    SentencePiece space marker) were DELETED instead of converted to
    spaces, so a marker-carrying goal arrived fused
    ("AddaGET/healthendpointtotheAPI") and any code-ish field lost its
    indentation. The fix converts bare markers to the spaces they are.
    The UNK_BYTE pass runs first and consumes the bracketed artifacts
    whole; the artifact hex (0xe29681) is U+2581's own UTF-8 encoding,
    so both passes restore the same space arriving through two decode
    paths.
    """

    @staticmethod
    def _sanitize(raw: str) -> str:
        from freetalon.orchestrator.intake import sanitize_llm_output

        return sanitize_llm_output(raw)

    def test_bare_marker_runs_become_indentation(self) -> None:
        raw = "def health():\n▁▁▁▁return {'ok': True}\n"
        self.assertEqual(
            self._sanitize(raw),
            "def health():\n    return {'ok': True}\n",
        )

    def test_bare_marker_mid_line_becomes_space(self) -> None:
        self.assertEqual(self._sanitize("import▁json"), "import json")
        self.assertEqual(
            self._sanitize("Add▁a▁health▁endpoint"),
            "Add a health endpoint",
        )

    def test_unk_byte_artifact_and_bare_runs_together(self) -> None:
        # The interaction case from the captured wire response
        # (logs/indent-measure-raw.json): mid-line spaces ride inside
        # UNK_BYTE artifacts while the indentation is bare marker runs.
        # Converting the markers alone leaves the artifacts mangling
        # the lines; the artifact pass alone deletes the indentation.
        # Both together reproduce the code the model emitted.
        import ast

        raw = (
            "import[UNK_BYTE_0xe29681▁json]json\n"
            "\n"
            "def[UNK_BYTE_0xe29681▁health]health():\n"
            "▁▁▁▁return[UNK_BYTE_0xe29681▁json]json.dumps("
            "{'ok':[UNK_BYTE_0xe29681▁True]True})\n"
        )
        cleaned = self._sanitize(raw)
        self.assertEqual(
            cleaned,
            "import json\n"
            "\n"
            "def health():\n"
            "    return json.dumps({'ok': True})\n",
        )
        ast.parse(cleaned)

    def test_mojibake_marker_still_becomes_space(self) -> None:
        # Pre-existing behavior, unchanged: the latin-1-mangled 3-char
        # form of the marker converts to a space.
        self.assertEqual(self._sanitize("a\xe2\x96\x81b"), "a b")

    def test_plain_text_unchanged(self) -> None:
        text = "def f():\n    return 1\n"
        self.assertEqual(self._sanitize(text), text)

    def test_marker_carrying_intent_survives_parse(self) -> None:
        # Regression: a TaskIntent whose goal words and code-ish
        # constraint value are separated by U+2581 markers (the wire
        # shape) must come through parse_task_intent_response with real
        # spaces -- before the fix the goal arrived fused and the
        # constraint lost its indentation. ensure_ascii=False keeps
        # the markers literal in the raw text, exactly as the wire
        # delivers them.
        import json

        payload = {
            "goal": "Add▁a▁GET▁/health▁endpoint▁to▁the▁API",
            "project_type": "api",
            "capabilities": ["scaffold"],
            "constraints": {
                "verify": "handler:\n▁▁▁▁return▁{'ok':▁True}",
            },
            "missing_inputs": [],
        }
        raw = json.dumps(payload, ensure_ascii=False)
        intent = parse_task_intent_response(raw)
        self.assertEqual(intent.goal, "Add a GET /health endpoint to the API")
        self.assertEqual(
            intent.constraints["verify"],
            "handler:\n    return {'ok': True}",
        )


class TestPlannerInterpreterGroundingFix(unittest.TestCase):
    """Interpreter paths + the sed attractor block every attempt (retry #25).

    Retry #25 (log llm-end-to-end-health-retry25-2026-11-17.log,
    DRIVER_EXIT:1, no run created) was the first live run with the
    sanitize fix confirmed: attempt 1's scaffold plan was GOOD code --
    indented, parsing, real subprocess nvidia-smi + requests probes --
    and failed ONLY on two interpreter strings the model composed from
    prose: a bare `python` in step_2's criterion command and
    `./venv/bin/python` (missing the dot) in step_3's start command,
    even though the rejections named ./.venv/bin/python explicitly.
    Attempts 2/3 then abandoned the scaffold shape for sed -i
    one-liners editing api.py (the sed attractor): attempt 2's sed did
    not parse as shell, attempt 3's pattern did not occur in api.py,
    and attempt 3's sed payload hardcoded "ollama_reachable": True /
    "gpu_temp": "N/A" inside a bash string, which no check named --
    stand-in detection covered scaffold content and literal writes,
    not bash payloads.

    Three fixes, repair budget NOT raised: (1) the ROUTE GOAL shape
    carries the exact interpreter string in copyable command /
    criterion slots (the model copies shapes reliably and composes
    strings unreliably); (2) sed -i edits of Python files join the
    seeded forbidden approaches for route goals, and the repair prompt
    steers back to the scaffold shape when a scaffold plan failed only
    on command/criterion strings; (3) stand-in detection extends to
    bash payloads that write Python files (heredoc/echo bodies and
    sed -i replacement texts) binding measured fields to constants.
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
        """The grounded plan, from the grounded-shape fix's test class."""
        grounded = TestPlannerGroundedShapeFix()
        grounded.setUp()
        return grounded._grounded_health_nodes()

    # The exact retry-#25 attempt-1 raw output, verbatim from the first
    # RAW PLANNER OUTPUT block of the retry-#25 driver section of
    # logs/llm-end-to-end-health-retry25-2026-11-17.log. Embedded (not
    # read from the log) so the test stays hermetic in CI.
    RETRY25_ATTEMPT1_RAW = r'''{
"nodes":[
{"id":"step_1","objective":"Modify /home/seeker/projects/FreeTalon/freetalon/api.py to implement the /health endpoint","depends_on":[],"assigned_claw":"scaffold","status":"draft","inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py","content":"import json\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n\ndef handle_health(self):\n    try:\n        import subprocess\n        gpu_temp = subprocess.check_output(['nvidia-smi', '--query-gpu=temperature.gpu', '--format=csv,noheader'], universal_newlines=True).strip()\n    except FileNotFoundError:\n        gpu_temp = 'N/A'\n\n    try:\n        import requests\n        ollama_status = requests.get('http://localhost:11434', timeout=2).status_code == 200\n    except requests.exceptions.RequestException:\n        ollama_status = False\n\n    response = {'service': 'FreeTalon API', 'status': 'OK', 'gpu_temperature': gpu_temp, 'ollama_reachable': ollama_status}\n    self.send_response(200)\n    self.send_header('Content-type', 'application/json')\n    self.end_headers()\n    self.wfile.write(json.dumps(response).encode())\n\nclass HiveAPIServer(BaseHTTPRequestHandler):\n    def do_GET(self):\n        if self.path == '/health':\n            handle_health(self)\n        else:\n            super().do_GET()\n\nif __name__ == '__main__':\n    server_address = ('', 8765)\n    httpd = HTTPServer(server_address, HiveAPIServer)\n    print('Starting FreeTalon API server...')\n    httpd.serve_forever()"},"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],"acceptance":["File exists","API responds to /health"],
"criteria":[{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/freetalon/api.py"},{"type":"exit_code","equals":0,"command":"curl -sf http://localhost:8765/health"}],
"error":null},
{"id":"step_2","objective":"Write and run unit tests for the /health endpoint","depends_on":["step_1"],"assigned_claw":"scaffold","status":"draft",
"inputs":{"file_path":"/home/seeker/freetalon-workspace/test_api.py","content":"import unittest\nfrom freetalon.api import HiveAPIServer\n\nclass TestHealthEndpoint(unittest.TestCase):\n    def test_health_endpoint(self):\n        # Assuming the server is running on localhost:8765\n        response = requests.get('http://localhost:8765/health')\n        self.assertEqual(response.status_code, 200)\n        data = response.json()\n        self.assertIn('service', data)\n        self.assertIn('status', data)\n        self.assertIn('gpu_temperature', data)\n        self.assertIn('ollama_reachable', data)\n\nif __name__ == '__main__':\n    unittest.main()"},"outputs":["/home/seeker/freetalon-workspace/test_api.py"],"acceptance":["Tests pass"],
"criteria":[{"type":"exit_code","equals":0,"command":"python /home/seeker/freetalon-workspace/test_api.py"}],
"error":null},
{"id":"step_3","objective":"Start the FreeTalon API server and verify it's live","depends_on":["step_2"],"assigned_claw":"bash","status":"draft",
"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"},"outputs":["server running"],
"acceptance":["Server is live and responding to requests"],
"criteria":[{"type":"exit_code","equals":0,"command":"curl -sf http://localhost:8765/health"}],
"error":null}
]
}'''

    # The exact retry-#25 attempt-3 raw output, verbatim from the third
    # RAW PLANNER OUTPUT block of the same driver section.
    RETRY25_ATTEMPT3_RAW = r'''{
"nodes":
[
{
"id":
"step_1",
"objective":
"Edit /home/seeker/projects/FreeTalon/freetalon/api.py to implement the /health endpoint",
"depends_on":
[],
"assigned_claw":
"bash",
"inputs":
{
"command":
"sed -i 's/def do_GET(self):/def do_GET(self):\n        import json\n        status = { \"service\": \"FreeTalon\", \"ollama_reachable\": True, \"gpu_temp\": \"N/A\"}\n        return json.dumps(status)/' /home/seeker/projects/FreeTalon/freetalon/api.py"
},
"outputs":
[
"/home/seeker/projects/FreeTalon/freetalon/api.py"
],
"acceptance":
"The /health endpoint is implemented",
"criteria":
[
{
"type":
"file_exists",
"path":
"/home/seeker/projects/FreeTalon/freetalon/api.py"
}
],
"error":
null
},
{
"id":
"step_2",
"objective":
"Run unit tests for the /health endpoint",
"depends_on":
[
"step_1"
],
"assigned_claw":
"bash",
"inputs":
{
"command":
"python -m unittest discover -s /home/seeker/projects/FreeTalon/tests"
},
"outputs":
[
"unit test results"
],
"acceptance":
"All unit tests pass",
"criteria":
[
{
"type":
"exit_code",
"equals":
0
}
],
"error":
null
},
{
"id":
"step_3",
"objective":
"Start the FreeTalon API server and verify /health endpoint is live",
"depends_on":
[
"step_2"
],
"assigned_claw":
"bash",
"inputs":
{
"command":
"./.venv/bin/python -m freetalon.cli start"
},
"outputs":
[
"/health endpoint is live"
],
"acceptance":
"The /health endpoint returns a 200 OK status and valid JSON",
"criteria":
[
{
"type":
"exit_code",
"equals":
0
}
],
"error":
null
}
],
"metadata":
{
"estimated_steps":
3
}
}'''

    # --- (1) Interpreter grounding: the exact string in copyable slots ---

    def test_route_goal_shape_carries_verbatim_interpreter_commands(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        shape = _route_goal_shape(self.HEALTH_GOAL, self.repo_root, delta=False)
        root = str(self.repo_root)
        # The test run as a quoted bash-command slot...
        self.assertIn(
            f'"cd {root} && ./.venv/bin/python -m unittest discover -s tests"',
            shape,
        )
        # ...and the same string named as an exit_code criterion's
        # command (retry #25 attempt 1 put a bare `python` there).
        self.assertIn("exit_code criterion", shape)
        self.assertIn("criterion's", shape)
        # The server start as a quoted bash-command slot.
        self.assertIn(
            f'"cd {root} && ./.venv/bin/python -m freetalon.cli start"', shape
        )
        # The two wrong spellings are named as rejected.
        self.assertIn("bare `python` does not exist on Ubuntu", shape)
        self.assertIn("./venv/bin/python is not a real path", shape)

    def test_initial_prompt_carries_verbatim_interpreter_commands(self) -> None:
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[good]
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        first_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn(
            "./.venv/bin/python -m unittest discover -s tests", first_prompt
        )
        self.assertIn("./.venv/bin/python -m freetalon.cli start", first_prompt)

    # --- (2a) The sed edit joins the seeded forbidden approaches ---

    def test_repo_forbidden_seed_includes_sed_edit(self) -> None:
        from freetalon.orchestrator.planner import (
            _repo_forbidden_approaches,
            _seed_forbidden_block,
        )
        from freetalon.orchestrator.repo_index import build_repo_index

        index = build_repo_index(self.repo_root)
        seed = _repo_forbidden_approaches(self.HEALTH_GOAL, index)
        self.assertTrue(any("sed -i" in a for a in seed))
        self.assertTrue(any("scaffold claw" in a for a in seed))
        # The earlier seeds are unchanged.
        self.assertTrue(any("Flask app" in a for a in seed))
        self.assertTrue(any("placeholder" in a for a in seed))
        block = _seed_forbidden_block(seed, self.HEALTH_GOAL, self.repo_root)
        self.assertIn("sed -i", block)
        # A goal naming no existing route seeds nothing.
        self.assertEqual(
            _repo_forbidden_approaches(self.GENERIC_GOAL, index), []
        )

    def test_sed_noop_rejection_forbids_sed_in_repair_prompt(self) -> None:
        # A sed -i edit whose pattern does not occur in api.py is
        # rejected naming sed; the repair prompt then forbids the
        # approach (with the scaffold replacement), and the grounded
        # plan is accepted on the next call.
        bad = self._dag(
            [
                {
                    "id": "step_1",
                    "objective": "Edit the /health endpoint in api.py",
                    "depends_on": [],
                    "assigned_claw": "bash",
                    "inputs": {
                        "command": (
                            "sed -i 's/def nope():/def nope():\\npass/' "
                            f"{self.api_path}"
                        )
                    },
                    "outputs": [self.api_path],
                    "acceptance": ["Edited."],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                }
            ]
        )
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn("Do NOT edit a Python file with sed -i", repair_prompt)
        self.assertIn("scaffold claw", repair_prompt)

    # --- (2b) Shape steering: a scaffold plan one fix away keeps its shape ---

    def test_repair_prompt_no_shape_blessing_for_retry25_attempt1(self) -> None:
        # Repair steering: a scaffold plan rejected ONLY for
        # interpreter strings gets a repair prompt that says the
        # shape was right; the grounded plan is then accepted. This
        # test used to drive that path with the verbatim retry-#25
        # attempt-1 output, its two interpreter spellings swapped
        # for ./bin/python (a nonexistent interpreter normalization
        # does not cover, so the rejection stayed interpreter-only
        # via the validator backstop).
        # Planner A/B refactor (defect 1): attempt 1's rejection is
        # no longer interpreter-only -- its scaffold drops 11 of
        # api.py's 12 definitions, and with the fragment-scale floor
        # gone the retention guard names that loss alongside the two
        # interpreter errors. Blessing the shape now would repeat
        # the retry-#28 contradiction (the repair prompt praising a
        # plan whose code was rejected), so the prompt must carry
        # the extension block instead and must NOT claim the shape
        # was right. The string-only steering case itself is pinned
        # by TestPlannerExtensionSteeringFix's
        # test_extension_block_silent_for_string_only_rejection.
        good = self._dag(self._grounded_health_nodes())
        attempt1 = self.RETRY25_ATTEMPT1_RAW.replace(
            "./venv/bin/python", "./bin/python"
        ).replace(
            "python /home/seeker/freetalon-workspace/test_api.py",
            "./bin/python /home/seeker/freetalon-workspace/test_api.py",
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[attempt1, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertNotIn("SHAPE was right", repair_prompt)
        self.assertIn("EXISTING-MODULE EXTENSION", repair_prompt)
        self.assertIn("./.venv/bin/python", repair_prompt)

    def test_repair_prompt_no_steering_when_code_was_rejected(self) -> None:
        # A scaffold plan whose CODE does not parse was not one fix
        # away, so the repair prompt must not claim the shape was right.
        bad = self._dag(
            [
                {
                    "id": "step_1",
                    "objective": "Extend the /health endpoint in api.py",
                    "depends_on": [],
                    "assigned_claw": "scaffold",
                    "inputs": {
                        "file_path": self.api_path,
                        "content": "def broken(:\n    pass\n",
                    },
                    "outputs": [self.api_path],
                    "acceptance": ["Extended."],
                    "criteria": [
                        {"type": "file_exists", "path": self.api_path}
                    ],
                }
            ]
        )
        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[bad, good]
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("does not parse as Python", repair_prompt)
        self.assertNotIn("SHAPE was right", repair_prompt)

    # --- (3) Stand-in detection inside bash payloads ---

    def test_retry25_attempt3_gains_standin_error(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self.RETRY25_ATTEMPT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=self.repo_root,
            )
        message = str(caught.exception)
        # 2 = the sed no-op (step_1) + the stand-in error naming the
        # values attempt 3 hardcoded in its sed payload, which retry
        # #25's live rejection (2 errors) never named. Was 3 errors
        # here until interpreter normalization (retry #27 fix):
        # step_2's bare `python` is rewritten to ./.venv/bin/python
        # at parse time and no longer errors.
        self.assertIn("was rejected with 2 errors", message)
        self.assertIn("hardcoded answer", message)
        self.assertIn('"ollama_reachable": True', message)
        self.assertIn('"gpu_temp": "N/A"', message)
        self.assertNotIn("bare `python`", message)

    def test_standin_fires_on_heredoc_payload_with_hardcoded_values(self) -> None:
        target = f"{self.WS}/health_impl.py"
        node = {
            "id": "step_1",
            "objective": "Write the health implementation",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    f"cat > {target} <<'EOF'\n"
                    "import json\n\n\n"
                    "def health():\n"
                    "    return json.dumps({\"service\": \"FreeTalon\", "
                    "\"ollama_reachable\": True, \"gpu_temp\": \"N/A\"})\n"
                    "EOF"
                )
            },
            "outputs": [target],
            "acceptance": ["The implementation exists."],
            "criteria": [{"type": "file_exists", "path": target}],
        }
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._dag([node]), goal_text=self.GENERIC_GOAL, workspace=self.WS
            )
        message = str(caught.exception)
        self.assertIn("hardcoded answer", message)
        self.assertIn('"ollama_reachable": True', message)
        self.assertIn('"gpu_temp": "N/A"', message)

    def test_standin_ignores_payload_that_computes_values(self) -> None:
        # The same heredoc shape with probes instead of constants: the
        # dict binds variables, so nothing is fabricated and the plan
        # is accepted untouched.
        target = f"{self.WS}/health_impl.py"
        node = {
            "id": "step_1",
            "objective": "Write the health implementation",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    f"cat > {target} <<'EOF'\n"
                    "import subprocess\n"
                    "import requests\n\n\n"
                    "def health():\n"
                    "    try:\n"
                    "        gpu_temp = subprocess.check_output("
                    "[\"nvidia-smi\", \"--query-gpu=temperature.gpu\", "
                    "\"--format=csv,noheader\"], universal_newlines=True).strip()\n"
                    "    except Exception:\n"
                    "        gpu_temp = \"unavailable\"\n"
                    "    ollama_reachable = requests.get("
                    "\"http://localhost:11434\", timeout=2).status_code == 200\n"
                    "    return {\"service\": \"FreeTalon\", "
                    "\"ollama_reachable\": ollama_reachable, "
                    "\"gpu_temp\": gpu_temp}\n"
                    "EOF"
                )
            },
            "outputs": [target],
            "acceptance": ["The implementation exists."],
            "criteria": [{"type": "file_exists", "path": target}],
        }
        nodes, _metadata = parse_planner_response(
            self._dag([node]), goal_text=self.GENERIC_GOAL, workspace=self.WS
        )
        self.assertEqual(len(nodes), 1)

    def test_sed_replacements_survive_delimiter_inside_string(self) -> None:
        from freetalon.orchestrator.planner import (
            _hardcoded_measured_bindings,
            _sed_script_replacements,
        )

        # Retry #25 attempt 3's script shape: the / inside "N/A"
        # closes the strict replacement scan early; the tolerant
        # remainder still carries the fabricated values.
        script = (
            "s/def do_GET(self):/def do_GET(self):\\n"
            "        status = { \"ollama_reachable\": True, "
            "\"gpu_temp\": \"N/A\" }/"
        )
        bindings = [
            binding
            for replacement in _sed_script_replacements(script)
            for binding in _hardcoded_measured_bindings(replacement)
        ]
        self.assertIn('"ollama_reachable": True', bindings)
        self.assertIn('"gpu_temp": "N/A"', bindings)
        # A plain substitution still parses strictly, with no remainder.
        self.assertEqual(_sed_script_replacements("s/a/b/"), ["b"])
        # Computed values are not bindings.
        self.assertEqual(
            _hardcoded_measured_bindings(
                '{"ollama_reachable": reachable, "gpu_temp": temp}'
            ),
            [],
        )

    # --- Regression guard: legitimate bash steps are untouched ---

    def test_grounded_plan_with_bash_steps_passes_untouched(self) -> None:
        nodes, _metadata = parse_planner_response(
            self._dag(self._grounded_health_nodes()),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 4)
class TestPlannerRepoRootOverwriteFix(unittest.TestCase):
    """repo_root=None validation bypass + wholesale-overwrite hole (retry #26).

    Retry #26 (log llm-end-to-end-health-retry26-2026-11-17.log, run
    run-20261117T194345Z-f15fbd, goal verdict FAIL) broke new ground --
    the first plan ACCEPTED since the framework-guard fix, and the
    first live proof that exit_code criteria run their commands
    (node-2 measured the real exit 127) -- but the acceptance came
    through a measured validation asymmetry, and the accepted plan
    then destroyed the module it was extending:

    (1) The driver calls plan_task_intent with repo_root=None, and
    _bash_interpreter_errors kept root=None in that case and skipped
    EVERY path token. Output 2 (a good scaffold; both bash commands
    ./venv/bin/python, missing the dot) rejects with 2 interpreter
    errors under the explicit root every test and probe passes, yet
    was ACCEPTED live. Fix A: the root is resolved unconditionally
    (resolve_repo_root(None) is the checkout holding this package --
    the same fallback build_repo_index uses). Audit of every other
    repo_root consumer in planner.py: _sed_noop_errors,
    _missing_directory_errors, _placeholder_scaffold_errors,
    _code_append_errors, _unwritten_file_errors and every index-based
    check already resolve unconditionally or read the index; the
    interpreter check was the ONLY None-sensitive one (repo-wide grep:
    the only `repo_root is not None else None` in freetalon/).

    (2) The accepted plan's node-1 scaffold replaced freetalon/api.py
    wholesale (219 lines -> 37) and passed on a file_exists criterion:
    the parse check rejects only UNparseable scaffolds, so a parseable
    fragment that deletes most of the module sailed through. Fix B:
    _scaffold_overwrite_errors compares the QUALIFIED def/class names
    (every nesting depth -- api.py has only 2 top-level names, and the
    replacement kept a class NAMED HiveAPIServer while dropping
    HiveAPIServer.start, the Handler inside it, and every handler
    method: 11 of 12 qualified definitions) of the existing module
    and the scaffold content, and rejects when a module-scale
    replacement (the content itself defines >= 4 qualified
    definitions) drops at least half (and at least 3) of an existing
    module's >= 4 definitions. Fragment-scale scaffolds (the grounded
    fixtures' branch snippet has zero definitions) and tiny modules
    are exempt; new files are exempt; content another rule owns
    (unparseable, placeholder, literal answer) is skipped.

    Direction C (goal asks for tests -> plan must write tests) is
    deliberately NOT implemented here: it would change this fix's own
    pinned probe profile (retry-#26 output 1 has no test-writing node
    and must stay at exactly 3 errors), and the /health goal text is
    the fixture goal for ~25 retries of pinned counts, most without a
    test-writing node -- that change deserves its own fixture audit.
    The judge already caught the shortfall at run level in retry #26
    (goal verdict FAIL).

    Pinned counts changed by fix A elsewhere in this file (each named
    in its test): retry-14 v1 3 -> 5, retry-14 v2 2 -> 3, retry-15
    attempt-2 7 -> 8, the collects-all-errors plan 5 -> 7, and
    retry-21 attempt-5 is no longer a cheap rejection (1 -> 3 errors).
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    GENERIC_GOAL = "Add a logging helper to the app and verify it works."
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#26 outputs 1 and 2, verbatim from the first two
    # RAW PLANNER OUTPUT blocks of the retry-#26 driver section of
    # logs/llm-end-to-end-health-retry26-2026-11-17.log. Embedded (not
    # read from the log) so the tests stay hermetic in CI.
    RETRY26_OUTPUT1_RAW = '{\n"nodes":\n[\n{\n"id":\n"step_1",\n"objective":\n"Add /health endpoint to FreeTalon\'s API",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"status":\n"draft",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"import os\\nimport json\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\n\\ndef handle_health(self):\\n self.send_response(200)\\n self.send_header(\'Content-type\', \'application/json\')\\n service_status = { \\"service\\": \\"FreeTalon\\", \\"status\\": \\"running\\"}\\n ollama_reachable = True # Placeholder, implement actual check\\n gpu_temp = \\"N/A\\" # Placeholder, implement actual check\\n response_data = json.dumps(service_status)\\n self.end_headers()\\n self.wfile.write(response_data.encode(\'utf-8\'))\\n\\nclass HiveAPIServer(BaseHTTPRequestHandler):\\n def do_GET(self):\\n if self.path == \'/health\':\\n handle_health(self)\\n else:\\n super().do_GET()\\n\\ndef start():\\n httpd = HTTPServer((\'localhost\', 8765), HiveAPIServer)\\n print(\'serving at port\', 8765)\\n httpd.serve_forever()"\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n"File exists",\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_2",\n"objective":\n"Write and run unit tests for the /health endpoint",\n"depends_on":\n[\n"step_1"\n],\n"assigned_claw":\n"bash",\n"status":\n"draft",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"\n},\n"outputs":\n[\n"test_results"\n],\n"acceptance":\n"Tests pass",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_3",\n"objective":\n"Start the API server and verify /health endpoint is live",\n"depends_on":\n[\n"step_2"\n],\n"assigned_claw":\n"bash",\n"status":\n"draft",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"\n},\n"outputs":\n[\n"server_running"\n],\n"acceptance":\n"Endpoint is live",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_4",\n"objective":\n"Verify /health endpoint returns expected JSON",\n"depends_on":\n[\n"step_3"\n],\n"assigned_claw":\n"bash",\n"status":\n"draft",\n"inputs":\n{\n"command":\n"curl -sf http://localhost:8765/health | jq \'.service\' && curl -sf http://localhost:8765/health | jq \'.status\'"\n},\n"outputs":\n[\n"verification_result"\n],\n"acceptance":\n"Endpoint returns expected JSON",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n}\n]\n}'
    RETRY26_OUTPUT2_RAW = '{\n"nodes":[\n{"id":"step_1",\n"objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py to implement the /health endpoint",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":"import json\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\n\\ndef handle_health(self):\\n    status = \\"OK\\"\\n    if not check_ollama_reachability():\\n        status = \\"Ollama unreachable\\"\\n    gpu_temp = get_gpu_temperature()\\n    response = json.dumps({\\"status\\": status, \\"gpu_temp\\": gpu_temp})\\n    self.send_response(200)\\n    self.send_header(\'Content-type\', \'application/json\')\\n    self.end_headers()\\n    self.wfile.write(response.encode())\\n\\ndef check_ollama_reachability():\\n    import urllib.request\\n    try:\\n        urllib.request.urlopen(\\"http://localhost:11434/api/health\\", timeout=1)\\n        return True\\n    except:\\n        return False\\n\\ndef get_gpu_temperature():\\n    import subprocess\\n    try:\\n        result = subprocess.run([\'nvidia-smi\', \'--query-gpu=temperature.gpu\', \'--format=csv,noheader\'], capture_output=True, text=True)\\n        temp = float(result.stdout.strip())\\n        return temp\\n    except:\\n        return \\"N/A\\"\\n\\nclass HiveAPIServer(BaseHTTPRequestHandler):\\n    def do_GET(self):\\n        if self.path == \'/health\':\\n            handle_health(self)\\n        else:\\n            super().do_GET()\\n"},\n"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],\n"acceptance":"File exists and parses as Python",\n"criteria":[{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/freetalon/api.py"}],\n"error":null\n},\n{"id":"step_2",\n"objective":"Run tests after modifying api.py",\n"depends_on":["step_1"],\n"assigned_claw":"bash",\n"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"},\n"outputs":[],\n"acceptance":"Tests pass",\n"criteria":[{"type":"exit_code","equals":0}],\n"error":null\n},\n{"id":"step_3",\n"objective":"Start the API server and verify /health endpoint is live",\n"depends_on":["step_2"],\n"assigned_claw":"bash",\n"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"},\n"outputs":[],\n"acceptance":"API server started and /health endpoint returns 200 OK",\n"criteria":[{"type":"exit_code","equals":0}],\n"error":null\n}\n],\n"metadata":{"estimated_steps":3}\n}'

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    # --- Fix A: repo_root=None validates identically to explicit root ---

    def test_retry26_output2_rejected_identically_none_and_explicit(self) -> None:
        none_message = self._rejection(
            self.RETRY26_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        explicit_message = self._rejection(
            self.RETRY26_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=str(self.repo_root),
        )
        # The driver path (repo_root=None) produces the SAME rejection
        # as the explicit-root path: was 3 errors (both interpreter
        # errors plus the overwrite error, fix B). Interpreter
        # normalization (retry #27 fix): the two ./venv/bin/python
        # commands are rewritten to ./.venv/bin/python at parse time,
        # so the overwrite error is the one rejection left -- 1 error,
        # identical on both paths.
        self.assertEqual(none_message, explicit_message)
        self.assertIn("was rejected with 1 error", none_message)
        self.assertEqual(none_message.count("does not exist in the repo"), 0)
        self.assertNotIn("./venv/bin/python", none_message)
        self.assertIn("drops 11 of the existing module", none_message)

    def test_retry26_output1_rejected_identically_none_and_explicit(self) -> None:
        none_message = self._rejection(
            self.RETRY26_OUTPUT1_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        explicit_message = self._rejection(
            self.RETRY26_OUTPUT1_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(none_message, explicit_message)
        # Parse error only. No overwrite error: the scaffold does not
        # parse, and the parse check owns it. Was 3 errors (parse +
        # both interpreter errors) until interpreter normalization
        # (retry #27 fix) rewrote the two ./venv/bin/python commands
        # at parse time.
        self.assertIn("was rejected with 1 error", none_message)
        self.assertEqual(none_message.count("does not exist in the repo"), 0)
        self.assertIn("does not parse as Python", none_message)
        self.assertNotIn("existing module's", none_message)

    def test_criterion_command_interpreter_parity(self) -> None:
        # The interpreter check also guards exit_code criterion
        # commands (via _usable_criterion_error) -- the same bypass
        # applied there. A criterion command naming a nonexistent
        # interpreter normalization does not cover (./bin/python) is
        # rejected identically with repo_root=None and explicit.
        node = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "curl -s http://localhost:8765/health"},
            "outputs": ["verification"],
            "criteria": [
                {
                    "type": "exit_code",
                    "equals": 0,
                    "command": "./bin/python -m unittest discover -s tests",
                }
            ],
        }
        none_message = self._rejection(
            self._dag([node]), goal_text=self.GENERIC_GOAL, repo_root=None
        )
        explicit_message = self._rejection(
            self._dag([node]),
            goal_text=self.GENERIC_GOAL,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(none_message, explicit_message)
        self.assertIn("unusable criterion", none_message)
        self.assertIn("./bin/python", none_message)
        # Interpreter normalization (retry #27 fix): the covered
        # spelling ./venv/bin/python in a criterion command is
        # rewritten to the grounded form at parse time and ACCEPTED
        # on both paths, and the accepted node carries the rewrite.
        covered = dict(node)
        covered["criteria"] = [
            {
                "type": "exit_code",
                "equals": 0,
                "command": "./venv/bin/python -m unittest discover -s tests",
            }
        ]
        for root in (None, str(self.repo_root)):
            nodes, _ = parse_planner_response(
                self._dag([covered]), goal_text=self.GENERIC_GOAL, repo_root=root
            )
            self.assertEqual(len(nodes), 1)
            self.assertEqual(
                nodes[0].criteria[0]["command"],
                "./.venv/bin/python -m unittest discover -s tests",
            )
        # The grounded interpreter in a criterion command passes on
        # both paths (the .venv exemption is machine-independent).
        good = dict(node)
        good["criteria"] = [
            {
                "type": "exit_code",
                "equals": 0,
                "command": "./.venv/bin/python -m unittest discover -s tests",
            }
        ]
        for root in (None, str(self.repo_root)):
            nodes, _ = parse_planner_response(
                self._dag([good]), goal_text=self.GENERIC_GOAL, repo_root=root
            )
            self.assertEqual(len(nodes), 1)

    def test_placeholder_rejection_identical_none_and_explicit(self) -> None:
        # Audit control: the placeholder existence gate was already
        # root-independent (the 7e7e13e CI-determinism fix) -- pin
        # that None and explicit roots reject it identically, so the
        # parity claim covers a check fix A did not need to touch.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.api_path,
                "content": "# Placeholder for health endpoint logic.\n",
            },
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        none_message = self._rejection(
            self._dag([node]),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        explicit_message = self._rejection(
            self._dag([node]),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(none_message, explicit_message)
        self.assertIn("was rejected with 1 error", none_message)
        self.assertIn("placeholder content only", none_message)

    # --- Fix B: the scaffold-overwrite guard ---

    def test_overwrite_guard_rejects_retry26_output2_naming_dropped(self) -> None:
        message = self._rejection(
            self.RETRY26_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=str(self.repo_root),
        )
        # The replacement keeps a class named HiveAPIServer but guts
        # it: 11 of the module's 12 qualified definitions are dropped,
        # and the error names them.
        self.assertIn("drops 11 of the existing module's 12 definitions", message)
        self.assertIn("_ThreadingHTTPServer", message)
        self.assertIn("HiveAPIServer.__init__", message)
        self.assertIn("HiveAPIServer.start", message)
        self.assertIn("HiveAPIServer.start.Handler", message)
        self.assertIn("HiveAPIServer.start.Handler.do_GET", message)
        # (The message lists the first 8 dropped names, then a count
        # of the rest -- HiveAPIServer.stop is among the unlisted 3.)
        self.assertIn("Scaffold the FULL updated module", message)

    def test_qualified_def_names_sees_nested_definitions(self) -> None:
        from freetalon.orchestrator.repo_index import qualified_def_names

        source = (self.repo_root / "freetalon" / "api.py").read_text(
            encoding="utf-8"
        )
        names = qualified_def_names(source)
        # api.py's surface is mostly nested: the Handler class lives
        # inside HiveAPIServer.start. (Other fixtures pin the same
        # module shape -- its routes and handler names.)
        self.assertEqual(len(names), 12)
        self.assertIn("HiveAPIServer", names)
        self.assertIn("HiveAPIServer.start.Handler.do_GET", names)
        self.assertIn("_ThreadingHTTPServer", names)

    def test_scaffold_preserving_definitions_passes(self) -> None:
        # The grounded shape done literally: the FULL existing module
        # plus the new code. Nothing is dropped, so the guard passes
        # on both the driver path and the explicit-root path.
        source = (self.repo_root / "freetalon" / "api.py").read_text(
            encoding="utf-8"
        )
        content = source + "\n\ndef _health_probe() -> bool:\n    return True\n"
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint in freetalon/api.py",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.api_path, "content": content},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        for root in (None, str(self.repo_root)):
            nodes, _ = parse_planner_response(
                self._dag([node]),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertEqual(len(nodes), 1)

    def test_new_file_scaffold_passes(self) -> None:
        # A new file has nothing to delete: exempt however complete
        # the module is.
        target = str(
            self.repo_root / "freetalon" / "orchestrator" / "health_helper.py"
        )
        content = (
            "import json\n\n\n"
            "def build_payload():\n    return json.dumps({'ok': True})\n\n\n"
            "def check_a():\n    return True\n\n\n"
            "def check_b():\n    return True\n\n\n"
            "def check_c():\n    return True\n"
        )
        node = {
            "id": "step_1",
            "objective": "Add a health helper module",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": target, "content": content},
            "outputs": [target],
            "criteria": [{"type": "file_exists", "path": target}],
        }
        nodes, _ = parse_planner_response(
            self._dag([node]),
            goal_text=self.GENERIC_GOAL,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(len(nodes), 1)

    def test_small_module_overwrite_not_flagged(self) -> None:
        # Tiny modules are exempt: too little structure to judge a
        # rewrite (3 qualified definitions < the >= 4 floor), even
        # when the replacement drops all of them.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "freetalon"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "tiny.py").write_text(
                "class Widget:\n"
                "    def start(self):\n        return 1\n"
                "    def stop(self):\n        return 2\n",
                encoding="utf-8",
            )
            content = (
                "def alpha():\n    return 1\n\n\n"
                "def beta():\n    return 2\n\n\n"
                "def gamma():\n    return 3\n\n\n"
                "def delta():\n    return 4\n\n\n"
                "def epsilon():\n    return 5\n"
            )
            node = {
                "id": "step_1",
                "objective": "Rewrite the tiny helper module",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": "freetalon/tiny.py", "content": content},
                "outputs": ["freetalon/tiny.py"],
                "criteria": [
                    {"type": "file_exists", "path": "freetalon/tiny.py"}
                ],
            }
            nodes, _ = parse_planner_response(
                self._dag([node]),
                goal_text=self.GENERIC_GOAL,
                repo_root=tmp,
            )
            self.assertEqual(len(nodes), 1)

    def test_fragment_scaffold_not_flagged(self) -> None:
        # Fragment-scale content (zero definitions -- the grounded
        # fixtures' branch snippet) is not a module replacement and
        # stays unflagged by the overwrite guard.
        node = {
            "id": "step_1",
            "objective": "Extend the health endpoint",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
            "outputs": [self.api_path],
            "criteria": [{"type": "file_exists", "path": self.api_path}],
        }
        nodes, _ = parse_planner_response(self._dag([node]))
        self.assertEqual(nodes[0].assigned_claw, "scaffold")


class TestPlannerInterpreterNormalizationFix(unittest.TestCase):
    """Deterministic interpreter-token normalization (retry #27).

    Retry #27 (log llm-end-to-end-health-retry27-2026-11-17.log,
    DRIVER_EXIT 1) failed at planning with the repo-root bypass
    already closed (9ea9a43): attempt 3 was the best scaffold yet --
    parse-clean, real computed values, correct shape -- and was
    rejected ONLY for the interpreter pair. Across retries #25-#27
    the model never once copied './.venv/bin/python' verbatim into a
    bash command (zero occurrences in #26 and #27); it writes
    './venv/bin/python' instead, and the repair loop, handed the
    exact rejection naming the grounded form, cannot steer the
    string. Prompt grounding (69c0ece) and validation (9ea9a43) have
    done what they can; the fix is deterministic: parse_planner_response
    rewrites the interpreter token in command position --
    './venv/bin/python', 'venv/bin/python', bare 'python' / 'python3'
    -- to './.venv/bin/python' before validation
    (_normalize_plan_interpreters / _normalize_bash_interpreters in
    planner.py), identifying the token exactly as
    _bash_interpreter_errors does (same segment split, same
    env-assignment stripping, first token only; heredoc bodies and
    quoted / argument occurrences are never rewritten). The validator
    stays as the backstop: spellings outside the covered set (e.g.
    ./bin/python) error exactly as before.

    Pinned count changes elsewhere in this file (each named in its
    test): the interpreter error disappears from the retry-#13
    attempt-4 pins (3 -> 2), the retry-#18 / #19 attempt pins
    (6 -> 5, 3 -> 2), the retry-#23 output pins (4 -> 2, 5 -> 4),
    the retry-#25 attempt-3 pin (3 -> 2) and the retry-#26 output
    pins (3 -> 1: output 1 keeps only its scaffold parse error,
    output 2 only the overwrite error). Retry-#21 attempt-5 is a
    cheap rejection again (3 -> 1: the placeholder stands alone once
    the two interpreter errors are normalized away). The
    criterion-parity and exit-code-criterion tests now drive the
    validator backstop with ./bin/python, and the repair-steering
    test drives the repair loop with ./bin/python in place of the
    verbatim retry-#25 attempt-1 spellings, which are accepted at
    parse time now.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    GENERIC_GOAL = "Add a logging helper to the app and verify it works."
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#27 outputs 1-3, verbatim from the RAW PLANNER
    # OUTPUT blocks of the retry-#27 driver section of
    # logs/llm-end-to-end-health-retry27-2026-11-17.log (the same
    # extraction tools/probe-retry27-rejections.py performs). Embedded
    # (not read from the log) so the tests stay hermetic in CI.
    RETRY27_OUTPUT1_RAW = '{\n"nodes":\n[\n{\n"id":\n"step_1",\n"objective":\n"Add /health endpoint to FreeTalon\'s API",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"status":\n"draft",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"import os\\nimport json\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\n\\ndef handle_health(self):\\n self.send_response(200)\\n self.send_header(\'Content-type\', \'application/json\')\\n service_status = { \\"service\\": \\"FreeTalon\\", \\"status\\": \\"running\\"}\\n ollama_reachable = True # Placeholder, implement actual check\\n gpu_temp = \\"N/A\\" # Placeholder, implement actual check\\n response_data = json.dumps(service_status)\\n self.end_headers()\\n self.wfile.write(response_data.encode(\'utf-8\'))\\n\\nclass HiveAPIServer(BaseHTTPRequestHandler):\\n def do_GET(self):\\n if self.path == \'/health\':\\n handle_health(self)\\n else:\\n super().do_GET()\\n\\ndef start():\\n httpd = HTTPServer((\'localhost\', 8765), HiveAPIServer)\\n print(\'serving at port\', 8765)\\n httpd.serve_forever()"\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n"File exists",\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_2",\n"objective":\n"Write and run unit tests for the /health endpoint",\n"depends_on":\n[\n"step_1"\n],\n"assigned_claw":\n"bash",\n"status":\n"draft",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"\n},\n"outputs":\n[\n"test_results"\n],\n"acceptance":\n"Tests pass",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_3",\n"objective":\n"Start the API server and verify /health endpoint is live",\n"depends_on":\n[\n"step_2"\n],\n"assigned_claw":\n"bash",\n"status":\n"draft",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"\n},\n"outputs":\n[\n"server_running"\n],\n"acceptance":\n"Endpoint is live",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_4",\n"objective":\n"Verify /health endpoint returns expected JSON",\n"depends_on":\n[\n"step_3"\n],\n"assigned_claw":\n"bash",\n"status":\n"draft",\n"inputs":\n{\n"command":\n"curl -sf http://localhost:8765/health | jq \'.service\' && curl -sf http://localhost:8765/health | jq \'.status\'"\n},\n"outputs":\n[\n"verification_result"\n],\n"acceptance":\n"Endpoint returns expected JSON",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n}\n]\n}'
    RETRY27_OUTPUT2_RAW = '{\n"nodes":[\n{"id":"step_1",\n"objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py to implement the /health endpoint",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":"import json\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\n\\ndef handle_health(self):\\n    response = {\'status\': \'ok\', \'ollama_reachable\': True, \'gpu_temp\': \'N/A\'}\\n    self.send_response(200)\\n    self.send_header(\'Content-type\', \'application/json\')\\n    self.end_headers()\\n    self.wfile.write(json.dumps(response).encode())\\n\\nclass HiveAPIServer(BaseHTTPRequestHandler):\\n    def do_GET(self):\\n        if self.path == \'/health\':\\n            handle_health(self)\\n        else:\\n            super().do_GET()\\n\\nif __name__ == \'__main__\':\\n    server_address = (\'\', 8765)\\n    httpd = HTTPServer(server_address, HiveAPIServer)\\n    print(\'Starting server...\')\\n    httpd.serve_forever()"\n},'
    RETRY27_OUTPUT3_RAW = '{\n"nodes":\n[\n{\n"id":\n"step_1",\n"objective":\n"Edit /home/seeker/projects/FreeTalon/freetalon/api.py to implement the /health endpoint",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"import json\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\nimport socket\\nimport os\\nimport subprocess\\n\\ndef handle(request):\\n    if request.path == \'/health\':\\n        try:\\n            # Check if Ollama is reachable\\n            socket.create_connection((\'localhost\', 11434), timeout=2)\\n            # Check GPU temperature using nvidia-smi\\n            gpu_temp = subprocess.check_output([\'nvidia-smi\', \'--query-gpu=temperature.gpu\', \'--format=csv,noheader\']).decode().strip()\\n            response = json.dumps({\\n                \\"status\\": \\"OK\\",\\n                \\"ollama_reachable\\": True,\\n                \\"gpu_temperature\\": gpu_temp\\n            })\\n        except (socket.error, subprocess.CalledProcessError):\\n            response = json.dumps({\\n                \\"status\\": \\"ERROR\\",\\n                \\"ollama_reachable\\": False,\\n                \\"gpu_temperature\\": \\"N/A\\"\\n            })\\n        request.send_response(200)\\n        request.send_header(\'Content-type\', \'application/json\')\\n        request.end_headers()\\n        request.write(response.encode(\'utf-8\'))\\n    else:\\n        request.send_response(404)\\n        request.end_headers()\\n\\nclass HiveAPIServer(BaseHTTPRequestHandler):\\n    def do_GET(self):\\n        handle(self)\\n\\nif __name__ == \'__main__\':\\n    server_address = (\'\', 8765)\\n    httpd = HTTPServer(server_address, HiveAPIServer)\\n    print(\'Starting server...\')\\n    httpd.serve_forever()\\n"\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n"File exists and /health endpoint is implemented",\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_2",\n"objective":\n"Run unit tests for the /health endpoint",\n"depends_on":\n[\n"step_1"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"\n},\n"outputs":\n[\n"test_results"\n],\n"acceptance":\n"All unit tests pass",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_3",\n"objective":\n"Start the FreeTalon API server",\n"depends_on":\n[\n"step_2"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"\n},\n"outputs":\n[\n"server_running"\n],\n"acceptance":\n"Server is running and accessible",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_4",\n"objective":\n"Verify the /health endpoint is live",\n"depends_on":\n[\n"step_3"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"curl -sf http://localhost:8765/health"\n},\n"outputs":\n[\n"verification_result"\n],\n"acceptance":\n"Endpoint returns a 200 OK status and valid JSON",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n}\n]\n}'

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    # --- The retry-#27 pins ---

    def test_retry27_output3_rejected_by_retention_guard_both_roots(self) -> None:
        # Before the interpreter-normalization fix: rejected with 2
        # errors under BOTH roots -- the interpreter pair on step_2 /
        # step_3, nothing else. The normalization fix made it
        # ACCEPTED under both roots, and this test pinned that.
        # Planner A/B refactor (defect 1): the acceptance was
        # unsound. The scaffold replaces freetalon/api.py wholesale
        # -- 2 definitions kept of the module's 12, deleting
        # HiveAPIServer.start, its Handler's do_POST / _send /
        # log_message, stop, and the controller wiring -- and the
        # old retention guard exempted it for carrying fewer than
        # four definitions of its own. With the fragment floor
        # removed it is rejected under both roots for exactly that
        # loss; and the single-error profile still proves the
        # normalization this test was written for: no command error
        # on step_2 / step_3 survives.
        for root in (None, str(self.repo_root)):
            with self.assertRaises(LLMResponseError) as caught:
                parse_planner_response(
                    self.RETRY27_OUTPUT3_RAW,
                    goal_text=self.HEALTH_GOAL,
                    workspace=self.WS,
                    repo_root=root,
                )
            exc = caught.exception
            self.assertEqual(
                {record.code for record in exc.plan_errors},
                {"scaffold_overwrite"},
            )
            self.assertIn(
                "drops 11 of the existing module's 12 definitions", str(exc)
            )

    def test_retry27_output1_only_scaffold_parse_error_remains(self) -> None:
        # Before this fix: 3 errors under both roots (the interpreter
        # pair + the scaffold parse error). The interpreter errors are
        # normalized away; the parse error -- the output's real defect
        # -- is the one rejection left, identical on both paths.
        none_message = self._rejection(
            self.RETRY27_OUTPUT1_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        explicit_message = self._rejection(
            self.RETRY27_OUTPUT1_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(none_message, explicit_message)
        self.assertIn("was rejected with 1 error", none_message)
        self.assertIn("does not parse as Python", none_message)
        self.assertNotIn("does not exist in the repo", none_message)
        self.assertNotIn("bare `python`", none_message)

    def test_retry27_output2_still_invalid_json(self) -> None:
        # The truncated second output never reached validation; it
        # stays a JSON rejection on both paths.
        for root in (None, str(self.repo_root)):
            message = self._rejection(
                self.RETRY27_OUTPUT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("not valid JSON", message)

    # --- The normalizer itself ---

    def test_normalizer_rewrites_covered_tokens(self) -> None:
        from freetalon.orchestrator.planner import _normalize_bash_interpreters

        cases = [
            (
                "./venv/bin/python -m unittest discover -s tests",
                "./.venv/bin/python -m unittest discover -s tests",
            ),
            (
                "venv/bin/python -m freetalon.cli start",
                "./.venv/bin/python -m freetalon.cli start",
            ),
            ("python -m unittest discover", "./.venv/bin/python -m unittest discover"),
            ("python3 tests/test_api.py", "./.venv/bin/python tests/test_api.py"),
            (
                "cd /home/seeker/projects/FreeTalon"
                " && ./venv/bin/python -m unittest discover -s tests",
                "cd /home/seeker/projects/FreeTalon"
                " && ./.venv/bin/python -m unittest discover -s tests",
            ),
            (
                "./venv/bin/python -m a; python -m b | python3 -m c",
                "./.venv/bin/python -m a; ./.venv/bin/python -m b"
                " | ./.venv/bin/python -m c",
            ),
            ("FOO=1 python -m x", "FOO=1 ./.venv/bin/python -m x"),
            (
                "curl -sf http://localhost:8765/health"
                " | ./venv/bin/python -m json.tool",
                "curl -sf http://localhost:8765/health"
                " | ./.venv/bin/python -m json.tool",
            ),
            (
                "cd /repo\n./venv/bin/python -m x",
                "cd /repo\n./.venv/bin/python -m x",
            ),
        ]
        for given, expected in cases:
            self.assertEqual(_normalize_bash_interpreters(given), expected, given)

    def test_normalizer_leaves_uncovered_and_content_untouched(self) -> None:
        from freetalon.orchestrator.planner import _normalize_bash_interpreters

        untouched = [
            # Already grounded: byte-identical.
            "./.venv/bin/python -m freetalon.cli start",
            # Quoted / argument occurrences are not command position.
            'echo "./venv/bin/python"',
            "echo python",
            "grep -rn 'python' freetalon/",
            # A filename argument containing the word.
            "./.venv/bin/python scripts/python_helper.py",
            # Outside the covered set: the validator's backstop case.
            "./bin/python -m x",
            # Heredoc bodies are file content, never rewritten.
            "cat > /tmp/x.sh <<'EOF'\n./venv/bin/python -m foo\npython -m bar\nEOF",
        ]
        for given in untouched:
            self.assertEqual(_normalize_bash_interpreters(given), given, given)
        # A heredoc head line is shell: a command sharing it is
        # rewritten, while the body lines stay content. (The bodies
        # protected here are the ones _strip_heredoc_bodies strips --
        # the cat-heredoc file-writing shape, the only heredoc the
        # validator treats as content rather than shell.)
        self.assertEqual(
            _normalize_bash_interpreters(
                "cat > /tmp/x.sh <<'EOF' && ./venv/bin/python /tmp/x.sh\n"
                "./venv/bin/python -m foo\nEOF"
            ),
            "cat > /tmp/x.sh <<'EOF' && ./.venv/bin/python /tmp/x.sh\n"
            "./venv/bin/python -m foo\nEOF",
        )

    def test_normalizer_walks_only_command_strings(self) -> None:
        from freetalon.orchestrator.planner import _normalize_plan_interpreters

        nodes = [
            {
                "id": "step_1",
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "x.py",
                    "content": "run ./venv/bin/python -m foo",
                    "command": "./venv/bin/python -m foo",
                },
                "criteria": [],
            },
            {
                "id": "step_2",
                "assigned_claw": "bash",
                "inputs": {"command": "./venv/bin/python -m foo"},
                "criteria": [
                    {"type": "exit_code", "equals": 0, "command": "python -m bar"}
                ],
            },
        ]
        _normalize_plan_interpreters(nodes)
        # Scaffold content is file content: untouched. A non-bash
        # node's 'command' input is not a validated command string:
        # untouched (the walk mirrors the validator's scope exactly).
        self.assertEqual(
            nodes[0]["inputs"]["content"], "run ./venv/bin/python -m foo"
        )
        self.assertEqual(nodes[0]["inputs"]["command"], "./venv/bin/python -m foo")
        self.assertEqual(
            nodes[1]["inputs"]["command"], "./.venv/bin/python -m foo"
        )
        self.assertEqual(
            nodes[1]["criteria"][0]["command"], "./.venv/bin/python -m bar"
        )

    def test_validator_backstop_still_errors_on_uncovered(self) -> None:
        from freetalon.orchestrator.planner import _bash_interpreter_errors

        errors = _bash_interpreter_errors("./bin/python -m x", self.repo_root)
        self.assertEqual(len(errors), 1)
        self.assertIn("./bin/python", errors[0])
        self.assertIn("./.venv/bin/python", errors[0])

    def test_bare_python_criterion_command_normalized(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Verify the endpoint live",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "curl -s http://localhost:8765/health"},
            "outputs": ["verification"],
            "criteria": [
                {
                    "type": "exit_code",
                    "equals": 0,
                    "command": "python -m unittest discover -s tests",
                }
            ],
        }
        for root in (None, str(self.repo_root)):
            nodes, _ = parse_planner_response(
                self._dag([node]), goal_text=self.GENERIC_GOAL, repo_root=root
            )
            self.assertEqual(len(nodes), 1)
            self.assertEqual(
                nodes[0].criteria[0]["command"],
                "./.venv/bin/python -m unittest discover -s tests",
            )


class TestPlannerExtensionSteeringFix(unittest.TestCase):
    """Ground an existing module's definitions + steer repairs to extend it (retry #28).

    Retry #28 (log llm-end-to-end-health-retry28-2026-11-17.log,
    DRIVER_EXIT 1) was the first run with interpreter normalization
    live: zero interpreter errors across all three outputs. Attempts 2
    and 3 were parse-clean scaffolds for freetalon/api.py that
    REPLACED the module wholesale -- a new 38-line module dropping 11
    of the existing 12 qualified definitions, then a 107-line
    restructure that hoisted the Handler class out of
    HiveAPIServer.start to module level, dropping the 8 nested
    HiveAPIServer.start.Handler.* definitions -- and both were
    correctly rejected by the overwrite guard (its first live
    exercise). The repair loop could not steer out of the shape:

    (1) Nothing in the initial prompt ever named the module's
    existing definitions. The repo context lists only TOP-LEVEL names
    (for api.py: HiveAPIServer, _ThreadingHTTPServer), and the ROUTE
    GOAL shape said "add a /health branch to the do_GET method of the
    Handler class (defined inside HiveAPIServer.start)" without
    listing what else lives in the file or stating that a scaffold
    must contain all of it. The model was asked to reproduce a
    219-line module it had never seen. Fix A: _route_goal_shape now
    appends an EXISTING MODULE block for each serving module --
    every qualified definition (repo_index.qualified_def_names via
    the _existing_module_definitions helper, same >= 4 floor as the
    guard) plus the preservation contract: the scaffold content is
    the full updated module, every definition in the same place with
    the same nesting, plus the new code.

    (2) The repair prompt after a guard rejection carried only the
    error text (whose dropped list truncates at 8 names -- attempt
    2's last three dropped definitions never reached the model), and
    two steering mechanisms misfired around it: the guard's wording
    matched no _APPROACH_MARKERS entry, so no DO-NOT-repeat block
    fired; and _shape_steering_block DID fire, telling the model
    "none of its code was rejected -- keep the same code; fix ONLY
    the named strings", because the guard's phrase was not a content
    defect marker. Fix B: the guard's phrase ("of the existing
    module's") is now a _CONTENT_DEFECT_MARKERS entry (the shape
    block stays silent on overwrite rejections) and an
    _APPROACH_MARKERS entry (later repairs name wholesale replacement
    as forbidden, with its replacement); and a new
    _extension_steering_block recomputes the failed scaffold against
    the module on disk with the guard's own thresholds and puts the
    FULL dropped list, the FULL existing list, and the concrete
    in-place pattern into the repair prompt.

    The node model cannot express a partial scaffold (the scaffold
    handler write_text()s the whole content; a bash heredoc can
    append but cannot insert a branch inside an existing method), so
    no new node type is introduced: the full-updated-module shape is
    the in-place pattern, grounded and steered at the prompt layer.
    The overwrite guard itself is untouched -- attempts 2 and 3 still
    reject with exactly 1 error each.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#28 outputs 2 and 3, verbatim from the RAW
    # PLANNER OUTPUT blocks of the retry-#28 driver section of
    # logs/llm-end-to-end-health-retry28-2026-11-17.log (the same
    # extraction tools/probe-retry28-rejections.py performs). Embedded
    # (not read from the log) so the tests stay hermetic in CI.
    # (Output 2 is byte-identical to retry #26's output 2 -- the
    # temperature-0 model produced the same replacement again.)
    RETRY28_OUTPUT2_RAW = '{\n"nodes":[\n{"id":"step_1",\n"objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py to implement the /health endpoint",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":"import json\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\n\\ndef handle_health(self):\\n    status = \\"OK\\"\\n    if not check_ollama_reachability():\\n        status = \\"Ollama unreachable\\"\\n    gpu_temp = get_gpu_temperature()\\n    response = json.dumps({\\"status\\": status, \\"gpu_temp\\": gpu_temp})\\n    self.send_response(200)\\n    self.send_header(\'Content-type\', \'application/json\')\\n    self.end_headers()\\n    self.wfile.write(response.encode())\\n\\ndef check_ollama_reachability():\\n    import urllib.request\\n    try:\\n        urllib.request.urlopen(\\"http://localhost:11434/api/health\\", timeout=1)\\n        return True\\n    except:\\n        return False\\n\\ndef get_gpu_temperature():\\n    import subprocess\\n    try:\\n        result = subprocess.run([\'nvidia-smi\', \'--query-gpu=temperature.gpu\', \'--format=csv,noheader\'], capture_output=True, text=True)\\n        temp = float(result.stdout.strip())\\n        return temp\\n    except:\\n        return \\"N/A\\"\\n\\nclass HiveAPIServer(BaseHTTPRequestHandler):\\n    def do_GET(self):\\n        if self.path == \'/health\':\\n            handle_health(self)\\n        else:\\n            super().do_GET()\\n"},\n"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],\n"acceptance":"File exists and parses as Python",\n"criteria":[{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/freetalon/api.py"}],\n"error":null\n},\n{"id":"step_2",\n"objective":"Run tests after modifying api.py",\n"depends_on":["step_1"],\n"assigned_claw":"bash",\n"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"},\n"outputs":[],\n"acceptance":"Tests pass",\n"criteria":[{"type":"exit_code","equals":0}],\n"error":null\n},\n{"id":"step_3",\n"objective":"Start the API server and verify /health endpoint is live",\n"depends_on":["step_2"],\n"assigned_claw":"bash",\n"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"},\n"outputs":[],\n"acceptance":"API server started and /health endpoint returns 200 OK",\n"criteria":[{"type":"exit_code","equals":0}],\n"error":null\n}\n],\n"metadata":{"estimated_steps":3}\n}'
    RETRY28_OUTPUT3_RAW = '{\n"nodes":\n[\n{\n"id":\n"step_1",\n"objective":\n"Scaffold /home/seeker/projects/FreeTalon/freetalon/api.py with full updated module content",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"# -*- coding: utf-8 -*-\\n\\n\\"\\"\\"API server for FreeTalon.\\"\\"\\"\\n\\nimport logging\\nimport os\\nimport sys\\nfrom threading import Thread\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\n\\n# Import modules from the project directory.\\n# Add the project root to the Python path.\\nproject_root = os.path.dirname(os.path.abspath(__file__))  # noqa: E501\\nsys.path.append(project_root)\\n\\nfrom freetalon import audit, bootstrap, config, docker_manager, hardware, hive, inference, jsonutil, mesh, orchestrator\\n\\n# Configure logging.\\nlogging.basicConfig(level=logging.INFO, format=\'%(asctime)s - %(name)s - %(levelname)s - %(message)s\')\\nlogger = logging.getLogger(__name__)\\n\\nclass HiveAPIServer:\\n    def __init__(self, config):\\n        self.config = config\\n        self.server = None\\n        self.thread = None\\n\\n    def start(self):\\n        \\"\\"\\"Start the API server.\\"\\"\\"\\n        try:\\n            # Create a handler for the HTTP requests.\\n            handler = Handler(self.config)\\n\\n            # Create an HTTP server.\\n            self.server = HTTPServer((self.config.host, self.config.port), handler)\\n\\n            # Start the server in a separate thread.\\n            self.thread = Thread(target=self.server.serve_forever)\\n            self.thread.daemon = True\\n            self.thread.start()\\n\\n            logger.info(\\"API server started on %s:%d\\", self.config.host, self.config.port)\\n        except Exception as e:\\n            logger.error(\\"Failed to start API server: %s\\", str(e))\\n\\n    def stop(self):\\n        \\"\\"\\"Stop the API server.\\"\\"\\"\\n        try:\\n            if self.server:\\n                self.server.shutdown()\\n                self.server.server_close()\\n                logger.info(\\"API server stopped.\\")\\n            else:\\n                logger.warning(\\"API server is not running.\\")\\n        except Exception as e:\\n            logger.error(\\"Failed to stop API server: %s\\", str(e))\\n\\nclass Handler(BaseHTTPRequestHandler):\\n    def __init__(self, config):\\n        self.config = config\\n        super().__init__()\\n\\n    def _authorized(self):\\n        \\"\\"\\"Check if the request is authorized.\\"\\"\\"\\n        # TODO: Implement authorization logic.\\n        return True\\n\\n    def _json_body(self):\\n        \\"\\"\\"Parse the JSON body of the request.\\"\\"\\"\\n        try:\\n            from json import loads\\n            content_length = int(self.headers[\'Content-Length\'])\\n            body = self.rfile.read(content_length)\\n            return loads(body)\\n        except Exception as e:\\n            logger.error(\\"Failed to parse JSON body: %s\\", str(e))\\n            return None\\n\\n    def do_GET(self):\\n        \\"\\"\\"Handle GET requests.\\"\\"\\"\\n        if self.path == \'/health\':\\n            self._send_response(200)\\n            self._headers[\'Content-Type\'] = \'application/json\'\\n            response = {  # noqa: E501\\n                \'status\': \'OK\',\\n                \'ollama_reachable\': True,  # Replace with actual check.\\n                \'gpu_temperature\': \'N/A\'  # Replace with actual check.\\n            }\\n            self._send_header(\'Content-Type\', \'application/json\')\\n            self._write_end_headers()\\n            self._write(jsonutil.dumps(response))\\n        else:\\n            super().do_GET()\\n\\n    def do_POST(self):\\n        \\"\\"\\"Handle POST requests.\\"\\"\\"\\n        if self.path == \'/shutdown\':\\n            # TODO: Implement shutdown logic.\\n            pass\\n        else:\\n            super().do_POST()\\n\\n\\nif __name__ == \'__main__\':\\n    config = config.HiveConfig()\\n    server = HiveAPIServer(config)\\n    server.start()"\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n[\n"File exists"\n],\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_2",\n"objective":\n"Run the squares script and save its output",\n"depends_on":\n[\n"step_1"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"python3 /home/seeker/projects/FreeTalon/freetalon/api.py > /home/seeker/freetalon-workspace/squares_out.txt"\n},\n"outputs":\n[\n"/home/seeker/freetalon-workspace/squares_out.txt"\n],\n"acceptance":\n[\n"Command exits with code 0",\n"Output file exists"\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n},\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/freetalon-workspace/squares_out.txt"\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_3",\n"objective":\n"Verify the squares output is exactly right",\n"depends_on":\n[\n"step_2"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"head -1 /home/seeker/freetalon-workspace/squares_out.txt | grep -qx \'1\' && tail -1 /home/seeker/freetalon-workspace/squares_out.txt | grep -qx \'25\' && grep -c . /home/seeker/freetalon-workspace/squares_out.txt | grep -qx \'5\'"\n},\n"outputs":\n[\n"verification"\n],\n"acceptance":\n[\n"Output verified"\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n}\n]\n}'

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")
        self.intent = TaskIntent(
            goal=self.HEALTH_GOAL,
            project_type="bash",
            capabilities=["scaffold", "bash", "llm_synthesis"],
            constraints={"workspace": self.WS},
            missing_inputs=[],
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan: edit the module, heredoc the tests, curl 8765."""
        ws = self.WS
        return [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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

    # --- Fix A: the shape block names the existing definitions ---

    def test_route_goal_shape_names_existing_definitions(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape
        from freetalon.orchestrator.repo_index import qualified_def_names

        source = (self.repo_root / "freetalon" / "api.py").read_text(
            encoding="utf-8"
        )
        names = qualified_def_names(source)
        self.assertEqual(len(names), 12)
        for delta in (False, True):
            shape = _route_goal_shape(
                self.HEALTH_GOAL, str(self.repo_root), delta=delta
            )
            self.assertIn("EXISTING MODULE", shape)
            self.assertIn(self.api_path, shape)
            # Every qualified definition is named -- including the
            # nested ones the repo context never showed.
            for name in names:
                self.assertIn(name, shape)
            self.assertIn("full updated module", shape)
            self.assertIn("same nesting", shape)
            # The COMMANDS block still closes the shape (retry #25's
            # arrangement: the copyable commands stay last).
            self.assertLess(shape.index("EXISTING MODULE"), shape.index("COMMANDS"))

    def test_initial_prompt_carries_existing_module_block(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[good]
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 1)
        initial_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("EXISTING MODULE", initial_prompt)
        self.assertIn("HiveAPIServer.start.Handler.do_GET", initial_prompt)
        self.assertIn("HiveAPIServer.stop", initial_prompt)

    def test_existing_module_definitions_helper(self) -> None:
        import tempfile

        from freetalon.orchestrator.planner import _existing_module_definitions

        # An existing parseable module: every qualified name, nested
        # ones included.
        names = _existing_module_definitions(self.api_path)
        self.assertEqual(len(names), 12)
        self.assertIn("HiveAPIServer.start.Handler.do_GET", names)
        self.assertIn("_ThreadingHTTPServer", names)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A new file: nothing exists to preserve.
            self.assertEqual(
                _existing_module_definitions(root / "brand_new.py"), []
            )
            # An unparseable file: already broken, no contract stated.
            broken = root / "broken.py"
            broken.write_text("def nope(:\n", encoding="utf-8")
            self.assertEqual(_existing_module_definitions(broken), [])
            # A tiny module (< 4 definitions, the guard's floor): no
            # preservation contract.
            tiny = root / "tiny.py"
            tiny.write_text(
                "class Widget:\n"
                "    def start(self):\n        return 1\n"
                "    def stop(self):\n        return 2\n",
                encoding="utf-8",
            )
            self.assertEqual(_existing_module_definitions(tiny), [])
            # Exactly at the floor: the names are returned.
            four = root / "four.py"
            four.write_text(
                "def a():\n    return 1\n\n\n"
                "def b():\n    return 2\n\n\n"
                "def c():\n    return 3\n\n\n"
                "def d():\n    return 4\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _existing_module_definitions(four), ["a", "b", "c", "d"]
            )

    # --- The guard itself is unchanged ---

    def test_retry28_attempt2_still_rejected_with_one_error(self) -> None:
        for root in (None, str(self.repo_root)):
            message = self._rejection(
                self.RETRY28_OUTPUT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 1 error", message)
            self.assertIn(
                "drops 11 of the existing module's 12 definitions", message
            )

    def test_retry28_attempt3_still_rejected_with_one_error(self) -> None:
        for root in (None, str(self.repo_root)):
            message = self._rejection(
                self.RETRY28_OUTPUT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 1 error", message)
            self.assertIn(
                "drops 8 of the existing module's 12 definitions", message
            )

    # --- Fix B: repair steering ---

    def test_steering_blocks_on_overwrite_rejections(self) -> None:
        from freetalon.orchestrator.planner import (
            _extension_steering_block,
            _shape_steering_block,
        )

        for raw, dropped_count in (
            (self.RETRY28_OUTPUT2_RAW, 11),
            (self.RETRY28_OUTPUT3_RAW, 8),
        ):
            message = self._rejection(
                raw,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=None,
            )
            codes = _rejection_codes(
                raw,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=None,
            )
            # The shape block no longer blesses a wholesale
            # replacement: the guard's code is a content defect.
            self.assertEqual(_shape_steering_block(raw, codes), "")
            # The extension block fires with the full dropped list.
            block = _extension_steering_block(raw, codes, None)
            self.assertIn("EXISTING-MODULE EXTENSION", block)
            self.assertIn(
                f"dropped these {dropped_count} existing definitions", block
            )
            self.assertIn("The module's full definition list is:", block)
            self.assertIn("FULL updated module", block)
        # Attempt 2's dropped list in the block is NOT truncated at
        # eight names the way the guard's error is: the last three
        # dropped definitions are named.
        message2 = self._rejection(
            self.RETRY28_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        codes2 = _rejection_codes(
            self.RETRY28_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        block2 = _extension_steering_block(self.RETRY28_OUTPUT2_RAW, codes2, None)
        dropped_segment = block2.split("The module's full definition list")[0]
        self.assertIn("HiveAPIServer.start.Handler._send", dropped_segment)
        self.assertIn("HiveAPIServer.start.Handler.log_message", dropped_segment)
        self.assertIn("HiveAPIServer.stop", dropped_segment)

    def test_extension_block_silent_for_string_only_rejection(self) -> None:
        from freetalon.orchestrator.planner import (
            _extension_steering_block,
            _shape_steering_block,
        )

        # A scaffold plan whose only defect is an interpreter spelling
        # normalization does not cover (the validator backstop,
        # ./bin/python): the retry-#25 shape steering still fires and
        # the extension block stays out of it.
        nodes = self._grounded_health_nodes()
        nodes[2]["inputs"]["command"] = "./bin/python -m unittest discover -s tests"
        raw = self._dag(nodes)
        message = self._rejection(
            raw,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        self.assertIn("./bin/python", message)
        self.assertNotIn("existing module's", message)
        codes = _rejection_codes(
            raw,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        self.assertIn("SHAPE was right", _shape_steering_block(raw, codes))
        self.assertEqual(_extension_steering_block(raw, codes, None), "")

    def test_retry28_attempt2_repair_prompt_carries_extension_block(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[self.RETRY28_OUTPUT2_RAW, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("REJECTED", repair_prompt)
        # The forbidden-approach block now names wholesale replacement
        # with its replacement (the _APPROACH_MARKERS entry).
        self.assertIn("DO NOT repeat approaches", repair_prompt)
        self.assertIn(
            "Do NOT replace an existing module with a new, smaller module",
            repair_prompt,
        )
        # The extension block carries the full dropped list (all 11,
        # not the guard error's truncated 8) and the full module list.
        self.assertIn("EXISTING-MODULE EXTENSION", repair_prompt)
        self.assertIn("dropped these 11 existing definitions", repair_prompt)
        segment = repair_prompt.split("EXISTING-MODULE EXTENSION")[1].split(
            "The module's full definition list"
        )[0]
        self.assertIn("HiveAPIServer.stop", segment)
        self.assertIn("HiveAPIServer.start.Handler.log_message", segment)
        self.assertIn("The module's full definition list is:", repair_prompt)
        # The misfiring shape-steering text is gone.
        self.assertNotIn("SHAPE was right", repair_prompt)
        self.assertNotIn("none of its code was rejected", repair_prompt)

    def test_retry28_attempt3_repair_prompt_carries_extension_block(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[self.RETRY28_OUTPUT3_RAW, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("EXISTING-MODULE EXTENSION", repair_prompt)
        self.assertIn("dropped these 8 existing definitions", repair_prompt)
        segment = repair_prompt.split("EXISTING-MODULE EXTENSION")[1].split(
            "The module's full definition list"
        )[0]
        # The definitions attempt 3 dropped by hoisting Handler out of
        # HiveAPIServer.start are named as dropped...
        self.assertIn("HiveAPIServer.start.Handler", segment)
        self.assertIn("HiveAPIServer.start.Handler.do_GET", segment)
        self.assertIn("HiveAPIServer.start.Handler.log_message", segment)
        # ...while a definition it kept is not in the dropped segment.
        self.assertNotIn("HiveAPIServer.__init__", segment)
        self.assertNotIn("SHAPE was right", repair_prompt)


class TestPlannerSkeletonGroundingFix(unittest.TestCase):
    """Show the model the existing module's structure, not just its names (retry #29).

    Retry #29 (log llm-end-to-end-health-retry29-2026-11-17.log,
    DRIVER_EXIT 1) was the first run with the extension-steering fix
    (95a8c8e) live, and the steering measurably worked: the repair
    outputs adopted the in-place framing verbatim and the scaffold
    definition count rose 9 -> 11. All three outputs still failed,
    and every failure was the same root cause — the model had the
    module's definition NAMES but had never seen the module's BODY,
    so it guessed the qualified-name layout:

    - outputs 1 and 2 hoisted the Handler class to module level,
      dropping the 7 nested HiveAPIServer.start.Handler.* names
      (and, in output 2, _ThreadingHTTPServer) — the overwrite guard
      fired (8 of 12 dropped) on both;
    - output 3 wrote ``class HiveAPIServer.start.Handler(...)`` —
      the dotted qualified name used as a class name, a syntax
      error.

    Fix: the EXISTING MODULE grounding now carries the module's
    structure with its real indentation. _route_goal_shape appends
    the module's FULL SOURCE when it fits the prompt budget
    (_STRUCTURE_FULL_SOURCE_MAX_CHARS = 10,000 chars — measured
    2026-11-17 with the Gemma 3 tokenizer: num_ctx 12288, the
    current initial call is 4,644 tokens, api.py's source is 2,194
    tokens, a successful full-module plan costs ~2,642 tokens to
    emit, and the worst realistic repair round totals ~10,850), and
    the signature skeleton from _module_skeleton (headers verbatim,
    bodies elided to ``...``, hard-capped at 8,000 chars with an
    explicit truncation marker) above it. _extension_steering_block
    appends the skeleton beside the dropped list. The definition
    list itself is never truncated. The overwrite guard and the
    interpreter normalization are untouched: the retry-#29 outputs
    (and retry-#28's) reject with exactly the same error profiles
    as before, under both repo roots.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#29 outputs, verbatim from the RAW PLANNER
    # OUTPUT blocks of the retry-#29 driver section of
    # logs/llm-end-to-end-health-retry29-2026-11-17.log (the same
    # extraction tools/probe-retry29-rejections.py performs).
    # Embedded (not read from the log) so the tests stay hermetic
    # in CI.
    RETRY29_OUTPUT1_RAW = '{\n"nodes":[\n{"id":"step_1",\n"objective":"Add /health endpoint to freetalon/api.py",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"status":"draft",\n"inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":"import http.server\\nimport socketserver\\nimport threading\\nimport json\\nimport os\\nfrom urllib.parse import urlparse, parse_qs\\n\\ndef _ThreadingHTTPServer(port):\\n    Handler = http.server.SimpleHTTPRequestHandler\\n    with socketserver.TCPServer((\'\', port), Handler) as httpd:\\n        print(\'serving at port\', port)\\n        httpd.serve_forever()\\n\\ndef HiveAPIServer():\\n    def start():\\n        port = 8765\\n        _ThreadingHTTPServer(port)\\n\\n    def stop():\\n        pass\\n\\n    return start\\n\\nstart = HiveAPIServer().start\\n\\ndef _authorized(request):\\n    return True\\n\\ndef _json_body(request):\\n    try:\\n        payload = json.loads(request.read().decode(\'utf-8\'))\\n        return payload\\n    except json.JSONDecodeError:\\n        return None\\n\\ndef _send(response, status_code, body=None):\\n    response.status = status_code\\n    if body is not None:\\n        response.write(body.encode(\'utf-8\'))\\n\\ndef do_GET(self):\\n    path = urlparse(self.path).path\\n    if path == \'/health\':\\n        import requests\\n        try:\\n            ollama_status = requests.get(\'http://localhost:11434/api/health\').status_code == 200\\n        except requests.exceptions.RequestException:\\n            ollama_status = False\\n\\n        try:\\n            import subprocess\\n            gpu_temp = subprocess.check_output([\'nvidia-smi\', \'--query-gpu=temperature.gpu\', \'--format=csv,noheader\']).decode(\'utf-8\').strip()\\n        except FileNotFoundError:\\n            gpu_temp = \'N/A\'\\n\\n        health_data = {\'status\': \'OK\', \'ollama_reachable\': ollama_status, \'gpu_temperature\': gpu_temp}\\n        self.send_response(200)\\n        self.send_header(\'Content-type\', \'application/json\')\\n        self.end_headers()\\n        self.wfile.write(json.dumps(health_data).encode(\'utf-8\'))\\n    else:\\n        super().do_GET(self)\\n\\ndef do_POST(self):\\n    pass"},\n"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],\n"acceptance":"/health endpoint returns JSON with status, ollama reachability and GPU temperature",\n"criteria":[{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/freetalon/api.py"}],\n"error":null\n},\n{"id":"step_2",\n"objective":"Run tests",\n"depends_on":["step_1"],\n"assigned_claw":"bash",\n"status":"draft",\n"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"},\n"outputs":[],\n"acceptance":"Tests pass",\n"criteria":[{"type":"exit_code","equals":0}],\n"error":null\n},\n{"id":"step_3",\n"objective":"Start the server and verify /health endpoint",\n"depends_on":["step_2"],\n"assigned_claw":"bash",\n"status":"draft",\n"inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"},\n"outputs":[],\n"acceptance":"Server starts and /health returns OK",\n"criteria":[{"type":"exit_code","equals":0},\n{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/.venv"}],\n"error":null\n}\n]\n}'

    RETRY29_OUTPUT2_RAW = '{\n"nodes":\n[\n{\n"id":\n"step_1",\n"objective":\n"Extend /home/seeker/projects/FreeTalon/freetalon/api.py with a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"# coding=utf-8\\n# Import necessary modules\\nimport http.server\\nimport socketserver\\nimport threading\\nimport json\\nfrom urllib.parse import urlparse, parse_qs\\n\\nclass HiveAPIServer(socketserver.ThreadingMixIn, socketserver.TCPServer):\\n    def __init__(self, server_address, HandlerClass):\\n        super().__init__(server_address, HandlerClass)\\n        self.daemon = True\\n\\n    def start(self):\\n        self.socket = self.socket # Ensure the socket is accessible\\n        thread = threading.Thread(target=self.serve_forever)\\n        thread.daemon = True\\n        thread.start()\\n\\n    def stop(self):\\n        self.shutdown()\\n\\nclass Handler(http.server.SimpleHTTPRequestHandler):\\n    def do_GET(self):\\n        if self.path == \'/health\':\\n            self.send_response(200)\\n            self.send_header(\'Content-type\', \'application/json\')\\n            self.end_headers()\\n            data = {\'status\': \'OK\', \'ollama_reachable\': True, \'gpu_temperature\': \'N/A\'}\\n            self.wfile.write(json.dumps(data).encode(\'utf-8\'))\\n        else:\\n            super().do_GET()\\n\\n    def do_POST(self):\\n        pass\\n\\n    def _authorized(self):\\n        return True\\n\\n    def _json_body(self):\\n        try:\\n            length = int(self.headers[\'Content-Length\'])\\n            body = self.rfile.read(length)\\n            return json.loads(body)\\n        except (KeyError, ValueError):\\n            return None\\n\\n    def _send(self, data):\\n        self.send_response(200)\\n        self.send_header(\'Content-type\', \'application/json\')\\n        self.end_headers()\\n        self.wfile.write(json.dumps(data).encode(\'utf-8\'))\\n\\n    def log_message(self, format, *args):\\n        pass"\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n[\n"File exists and contains the /health endpoint"\n],\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_2",\n"objective":\n"Run the tests and verify they pass",\n"depends_on":\n[\n"step_1"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"\n},\n"outputs":\n[\n"test results"\n],\n"acceptance":\n[\n"All tests pass"\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n},\n{\n"id":\n"step_3",\n"objective":\n"Start the API server and verify it answers on /health",\n"depends_on":\n[\n"step_2"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli start"\n},\n"outputs":\n[\n"/health endpoint is reachable"\n],\n"acceptance":\n[\n"API server starts and responds to /health"\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n}\n]\n}'

    RETRY29_OUTPUT3_RAW = '{\n"nodes":\n[\n{\n"id":\n"step_1",\n"objective":\n"Extend /home/seeker/projects/FreeTalon/freetalon/api.py with a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"# coding: utf-8\\n# flake8: noqa\\n\\"\\"\\"API for FreeTalon.\\"\\"\\"\\nimport logging\\nfrom threading import Thread\\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\\nimport json\\nimport os\\n\\nclass HiveAPIServer:\\n    def __init__(self):\\n        self._httpd = None\\n        self._thread = None\\n        self._port = 8765\\n        self._config = self.load_config()\\n\\n    def load_config(self):\\n        return { \\"port\\": 8765 } # Dummy config for now\\n\\n    def start(self):\\n        self._httpd = HTTPServer((\\"localhost\\", self._config[\\"port\\"]), HiveAPIServer.start.Handler)\\n        self._thread = Thread(target=self._httpd.serve_forever)\\n        self._thread.daemon = True\\n        self._thread.start()\\n\\n    def stop(self):\\n        if self._httpd:\\n            self._httpd.shutdown()\\n\\nclass HiveAPIServer.start.Handler(BaseHTTPRequestHandler):\\n    def do_GET(self):\\n        if self.path == \'/\': # Default route\\n            self.send_response(200)\\n            self.send_header(\'Content-type\', \'text/html\')\\n            self.end_headers()\\n            message = \\"<html><body><h1>FreeTalon API</h1></body></html>\\"\\n            self.wfile.write(bytes(message, \\"utf8\\"))\\n        elif self.path == \'/health\': # New health route\\n            self.send_response(200)\\n            self.send_header(\'Content-type\', \'application/json\')\\n            self.end_headers()\\n            # Check OLLAMA reachability (dummy check for now)\\n            ollama_reachable = True # Replace with actual check\\n            # Check GPU temperature (dummy check for now)\\n            gpu_temp = \\"N/A\\" # Replace with actual check\\n            response = { \\"status\\": \\"OK\\", \\"ollama_reachable\\": ollama_reachable, \\"gpu_temperature\\": gpu_temp}\\n            self.wfile.write(bytes(json.dumps(response), \\"utf8\\"))\\n        else:\\n            super().do_GET()\\n\\n    def do_POST(self):\\n        pass # Placeholder for POST requests\\n\\n    def _authorized(self):\\n        return True  # Dummy authorization\\n\\n    def _json_body(self):\\n        try:\\n            length = int(self.headers[\'Content-Length\']) if \'Content-Length\' in self.headers else 0\\n            if length > 0:\\n                body = self.rfile.read(length)\\n                return json.loads(body)\\n            else:\\n                return None\\n        except Exception as e:\\n            logging.error(f\\"Error parsing JSON body: {e}\\")\\n            return None\\n\\n    def _send(self, code, message):\\n        self.send_response(code)\\n        self.end_headers()\\n        if isinstance(message, str):\\n            self.wfile.write(bytes(message, \\"utf8\\"))\\n        else:\\n            self.wfile.write(bytes(json.dumps(message), \\"utf8\\"))\\n\\n    def log_message(self, format, *args, **kwargs):\\n        pass # Disable logging for now"\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n"API /health endpoint returns JSON reporting service status",\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n],\n"error":\nnull\n}\n]\n}'

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")
        self.intent = TaskIntent(
            goal=self.HEALTH_GOAL,
            project_type="bash",
            capabilities=["scaffold", "bash", "llm_synthesis"],
            constraints={"workspace": self.WS},
            missing_inputs=[],
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    def _grounded_health_nodes(self) -> list[dict]:
        """The grounded plan: edit the module, heredoc the tests, curl 8765."""
        ws = self.WS
        return [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {"file_path": self.api_path, "anchor": "HiveAPIServer.start.Handler.do_GET", "position": "replace", "content": GROUNDED_DO_GET_DELTA},
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

    # --- The shape block carries the structure ---

    def test_route_goal_shape_carries_module_source_with_real_nesting(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        handler_line = "        class Handler(BaseHTTPRequestHandler):"
        for delta in (False, True):
            shape = _route_goal_shape(
                self.HEALTH_GOAL, str(self.repo_root), delta=delta
            )
            self.assertIn("EXISTING MODULE", shape)
            # api.py is 9,380 chars — under the full-source threshold,
            # so the shape carries the module itself, not the skeleton.
            self.assertIn("The module's full current source is below", shape)
            # The Handler class line appears with its REAL leading
            # indentation (8 spaces), as a whole line of the block.
            self.assertIn(handler_line, shape.splitlines())
            # ...inside the start method's region of the block: after
            # start's header line and before stop's.
            start_idx = shape.index("    def start(self, host: str, port: int) -> None:")
            handler_idx = shape.index(handler_line)
            stop_idx = shape.index("    def stop(self) -> None:")
            self.assertLess(start_idx, handler_idx)
            self.assertLess(handler_idx, stop_idx)
            # Body fidelity: a line from deep inside do_GET rides
            # along — this is the source, not a summary.
            self.assertIn('                if path == "/health":', shape)
            # The COMMANDS block still closes the shape.
            self.assertLess(shape.index("EXISTING MODULE"), shape.index("COMMANDS"))

    def test_route_goal_shape_skeleton_branch_when_source_too_big(self) -> None:
        from freetalon.orchestrator import planner
        from freetalon.orchestrator.planner import _module_skeleton, _route_goal_shape

        source = (self.repo_root / "freetalon" / "api.py").read_text(
            encoding="utf-8"
        )
        skeleton = _module_skeleton(source)
        with patch.object(
            planner,
            "_existing_module_structure",
            return_value=("skeleton", skeleton),
        ):
            shape = _route_goal_shape(
                self.HEALTH_GOAL, str(self.repo_root), delta=False
            )
        self.assertIn("The module's structure is below, with its real", shape)
        self.assertIn("never copy a ... ", shape)
        self.assertIn(
            "        class Handler(BaseHTTPRequestHandler):", shape.splitlines()
        )
        self.assertNotIn("The module's full current source is below", shape)

    def test_initial_prompt_carries_module_structure(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[good]
        ) as call_mock:
            plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 1)
        initial_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("The module's full current source is below", initial_prompt)
        self.assertIn(
            "        class Handler(BaseHTTPRequestHandler):",
            initial_prompt.splitlines(),
        )

    def test_replan_prompt_carries_module_structure(self) -> None:
        from freetalon.orchestrator.planner import replan_from_brief

        echo = '{"goal": {"objective": "x"}, "failed_nodes": []}'
        good = self._dag(self._grounded_health_nodes())
        brief = {
            "goal": {"objective": self.HEALTH_GOAL},
            "failed_nodes": [],
            "workspace": self.WS,
        }
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=[echo, good]
        ) as call_mock:
            nodes = replan_from_brief(
                brief, settings=self.settings, repo_root=str(self.repo_root)
            )
        self.assertEqual(len(nodes), 4)
        initial_prompt = call_mock.call_args_list[0][0][1]
        self.assertIn("The module's full current source is below", initial_prompt)
        self.assertIn(
            "        class Handler(BaseHTTPRequestHandler):",
            initial_prompt.splitlines(),
        )

    # --- The skeleton builder ---

    def test_module_skeleton_of_api_module(self) -> None:
        from freetalon.orchestrator.planner import _module_skeleton
        from freetalon.orchestrator.repo_index import qualified_def_names

        source = (self.repo_root / "freetalon" / "api.py").read_text(
            encoding="utf-8"
        )
        skeleton = _module_skeleton(source)
        lines = skeleton.splitlines()
        # The nested layout, at the file's own indentation.
        self.assertIn("class HiveAPIServer:", lines)
        self.assertIn("        class Handler(BaseHTTPRequestHandler):", lines)
        self.assertIn("            def do_GET(self) -> None:  # noqa: N802", lines)
        self.assertIn("    def stop(self) -> None:", lines)
        # The multi-line __init__ signature is reproduced whole.
        self.assertIn("    def __init__(", lines)
        self.assertIn("        gate_store: GateStore | None = None,", lines)
        self.assertIn("    ) -> None:", lines)
        # Bodies are elided, and the skeleton itself parses — and
        # carries exactly the same qualified names as the source.
        self.assertIn("        ...", lines)
        import ast

        ast.parse(skeleton)
        self.assertEqual(
            qualified_def_names(skeleton), qualified_def_names(source)
        )
        # Imports ride verbatim.
        self.assertIn("from .hive import HiveController", lines)

    def test_module_skeleton_constructs(self) -> None:
        from freetalon.orchestrator.planner import _module_skeleton
        from freetalon.orchestrator.repo_index import qualified_def_names

        source = "\n".join(
            [
                '"""Module docstring."""',
                "import os",
                "from typing import Any",
                "",
                "CONSTANT = (",
                '    "a long value"',
                ")",
                "",
                "",
                "def deco(fn):",
                "    return fn",
                "",
                "",
                "@deco",
                "def top_level(",
                "    arg: int,",
                ") -> int:",
                "    return arg + 1",
                "",
                "",
                "async def fetch(url: str) -> bytes:",
                "    return b''",
                "",
                "",
                "def one_liner() -> int: return 42",
                "",
                "",
                "class Outer:",
                "    kind = 'outer'",
                "",
                "    class Inner:",
                "        def deep(self) -> None:",
                "            return None",
                "",
                "    def method(self) -> None:",
                "        def helper() -> int:",
                "            return 1",
                "        return None",
                "",
            ]
        )
        skeleton = _module_skeleton(source)
        lines = skeleton.splitlines()
        # Decorator rides directly above its definition.
        deco_idx = lines.index("@deco")
        self.assertEqual(lines[deco_idx + 1], "def top_level(")
        # Multi-line signature whole; async header; one-liner whole.
        self.assertIn("    arg: int,", lines)
        self.assertIn("async def fetch(url: str) -> bytes:", lines)
        self.assertIn("def one_liner() -> int: return 42", lines)
        # Nesting at true indentation, at every depth.
        self.assertIn("    class Inner:", lines)
        self.assertIn("        def deep(self) -> None:", lines)
        self.assertIn("        def helper() -> int:", lines)
        # A multi-line module-level statement is reduced to its
        # first line, marked as elided.
        self.assertIn("CONSTANT = ( ...", lines)
        # Every definition's header line survives at its nesting.
        # (Checked line-wise here: this skeleton does NOT parse —
        # a multi-line statement reduced to its first line, like
        # CONSTANT above, can leave brackets open — so
        # qualified_def_names, which needs a parse, cannot be the
        # checker. The skeleton is prompt text, never executed.
        # The api module's skeleton does parse, and the test above
        # pins full qualified-name equality for the real target.)
        self.assertIn("def deco(fn):", lines)
        self.assertIn("class Outer:", lines)
        self.assertIn("    def method(self) -> None:", lines)
        # Unparseable source has no skeleton.
        self.assertEqual(_module_skeleton("def nope(:\n"), "")

    def test_existing_module_structure_threshold_and_floor(self) -> None:
        import tempfile

        from freetalon.orchestrator.planner import (
            _existing_module_skeleton,
            _existing_module_structure,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Missing, unparseable, and under-floor modules ground
            # nothing — the same gate as _existing_module_definitions.
            self.assertIsNone(_existing_module_structure(root / "nope.py"))
            broken = root / "broken.py"
            broken.write_text("def nope(:\n", encoding="utf-8")
            self.assertIsNone(_existing_module_structure(broken))
            tiny = root / "tiny.py"
            tiny.write_text(
                "class Widget:\n"
                "    def start(self):\n        return 1\n"
                "    def stop(self):\n        return 2\n",
                encoding="utf-8",
            )
            self.assertIsNone(_existing_module_structure(tiny))
            self.assertEqual(_existing_module_skeleton(tiny), "")
            # A small module at the floor: full source, verbatim.
            small = root / "small.py"
            small_src = (
                "def a():\n    return 1\n\n\n"
                "def b():\n    return 2\n\n\n"
                "def c():\n    return 3\n\n\n"
                "def d():\n    return 4\n"
            )
            small.write_text(small_src, encoding="utf-8")
            self.assertEqual(
                _existing_module_structure(small), ("source", small_src)
            )
            # A module over the source threshold: the skeleton form.
            big = root / "big.py"
            big_src = "\n".join(
                "def fn_%02d():\n    return %r\n" % (i, "x" * 200)
                for i in range(60)
            )
            self.assertGreater(len(big_src), 10_000)
            big.write_text(big_src, encoding="utf-8")
            kind, text = _existing_module_structure(big)
            self.assertEqual(kind, "skeleton")
            self.assertIn("def fn_00():", text)
            self.assertIn("def fn_59():", text)
            self.assertLess(len(text), len(big_src))
            # The api module's skeleton via the path helper carries
            # the nested Handler line.
            api_skeleton = _existing_module_skeleton(self.api_path)
            self.assertIn(
                "        class Handler(BaseHTTPRequestHandler):",
                api_skeleton.splitlines(),
            )

    def test_skeleton_cap_marks_truncation(self) -> None:
        import tempfile

        from freetalon.orchestrator.planner import (
            _STRUCTURE_SKELETON_MAX_CHARS,
            _cap_skeleton,
            _existing_module_structure,
        )

        long_skeleton = "\n".join(
            "def function_number_%03d():" % i for i in range(400)
        )
        self.assertGreater(len(long_skeleton), _STRUCTURE_SKELETON_MAX_CHARS)
        capped = _cap_skeleton(long_skeleton)
        self.assertIn("structure truncated here", capped)
        self.assertIn("the definition list above is complete", capped)
        self.assertLessEqual(
            len(capped), _STRUCTURE_SKELETON_MAX_CHARS + 120
        )
        # A module whose skeleton exceeds the cap: kind is skeleton
        # and the marker is present (the definition LIST in the
        # grounding is computed separately and stays complete).
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "huge.py"
            big.write_text(
                "\n".join(
                    "def a_very_long_function_name_%03d():\n"
                    "    return %r\n" % (i, "y" * 120)
                    for i in range(300)
                ),
                encoding="utf-8",
            )
            kind, text = _existing_module_structure(big)
            self.assertEqual(kind, "skeleton")
            self.assertIn("structure truncated here", text)

    # --- The extension block carries the skeleton ---

    def test_extension_block_carries_skeleton(self) -> None:
        from freetalon.orchestrator.planner import _extension_steering_block

        message = self._rejection(
            self.RETRY29_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        self.assertIn("drops 8 of the existing module's 12 definitions", message)
        codes = _rejection_codes(
            self.RETRY29_OUTPUT2_RAW,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        block = _extension_steering_block(self.RETRY29_OUTPUT2_RAW, codes, None)
        self.assertIn("EXISTING-MODULE EXTENSION", block)
        self.assertIn("dropped these 8 existing definitions", block)
        self.assertIn("The module's structure, with its real indentation", block)
        self.assertIn(
            "        class Handler(BaseHTTPRequestHandler):", block.splitlines()
        )
        self.assertIn(
            "            def do_GET(self) -> None:  # noqa: N802",
            block.splitlines(),
        )

    def test_retry29_repair_prompt_carries_extension_skeleton(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        good = self._dag(self._grounded_health_nodes())
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[self.RETRY29_OUTPUT2_RAW, good],
        ) as call_mock:
            plan = plan_task_intent(self.intent, settings=self.settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 4)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("EXISTING-MODULE EXTENSION", repair_prompt)
        self.assertIn("The module's structure, with its real indentation", repair_prompt)
        self.assertIn(
            "        class Handler(BaseHTTPRequestHandler):",
            repair_prompt.splitlines(),
        )

    # --- The guard's behavior is unchanged on the fixtures ---

    def test_retry29_outputs_still_rejected_as_before(self) -> None:
        for root in (None, str(self.repo_root)):
            message1 = self._rejection(
                self.RETRY29_OUTPUT1_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 2 errors", message1)
            self.assertIn(
                "drops 8 of the existing module's 12 definitions", message1
            )
            message2 = self._rejection(
                self.RETRY29_OUTPUT2_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 1 error", message2)
            self.assertIn(
                "drops 8 of the existing module's 12 definitions", message2
            )
            message3 = self._rejection(
                self.RETRY29_OUTPUT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 1 error", message3)
            self.assertIn("does not parse as Python", message3)

    def test_retry28_fixtures_still_rejected_with_one_error(self) -> None:
        for raw, dropped in (
            (TestPlannerExtensionSteeringFix.RETRY28_OUTPUT2_RAW, 11),
            (TestPlannerExtensionSteeringFix.RETRY28_OUTPUT3_RAW, 8),
        ):
            for root in (None, str(self.repo_root)):
                message = self._rejection(
                    raw,
                    goal_text=self.HEALTH_GOAL,
                    workspace=self.WS,
                    repo_root=root,
                )
                self.assertIn("was rejected with 1 error", message)
                self.assertIn(
                    "drops %d of the existing module's 12 definitions" % dropped,
                    message,
                )


class TestPlannerAnchoredEditFix(unittest.TestCase):
    """Anchored-edit scaffolds for existing modules (retry #30).

    Retry #30 (log llm-end-to-end-health-retry30-2026-11-17.log,
    DRIVER_EXIT 1) was the first run with the structure grounding
    (1b6de40) live: the initial prompt carried api.py's FULL source
    and the instruction to write the full updated module. The model
    instead emitted step_1's scaffold as a 5-line COMMENT STUB —
    the grounded skeleton's commented branch sketch plus the
    steering sentences, copied into comments ("# Extend ... in
    place ...", "# Do NOT write a TODO comment") — zero code. The
    placeholder check rejected all three real plans (outputs 3/4/5:
    1, 2, and 1 errors; the rest of those plans was the best-formed
    yet — a real ephemeral-port test, the grounded CLI start, a curl
    verify), and two repairs naming the defect did not move it.

    Conclusion: a 12B model will not retype a 219-line module as one
    node among five. The grounded source must reach the scaffold BY
    CONSTRUCTION: the system seeds from the existing file and the
    model supplies only the delta. A scaffold node for an existing
    parseable module may now carry an anchored edit instead of the
    whole file — inputs "anchor" (a qualified definition name),
    "position" ("replace" substitutes the definition; "after" /
    "before" insert a sibling next to it), and "content" holding
    ONLY the new code, written at the exact indentation it has in
    the file. _materialize_plan_edits runs in parse_planner_response
    on the raw node dicts (the interpreter-normalization seam):
    it reads the current file, splices the delta in at the anchor's
    ast line span (decorators included), validates the result
    (parses; every existing qualified definition still present),
    and rewrites the node to the legacy full-content form BEFORE
    the schema check and _collect_plan_errors run — so the
    overwrite guard, the placeholder check, and the executor see
    exactly the full-content world they see today, unchanged.
    Materialization errors are named and steerable (anchor not
    found / ambiguous, position invalid, delta placeholder or
    comment-only, delta at the wrong indentation, a 'replace' delta
    that is not the one definition, a splice that does not parse or
    loses definitions); a rejection whose only errors are
    placeholder deltas is cheap, like a placeholder scaffold.

    Steering teaches the edit form as the PREFERRED way to change
    an existing module: the EXISTING MODULE block in
    _route_goal_shape (which also names the goal's natural anchor),
    _extension_steering_block after a guard rejection, the
    _APPROACH_MARKERS replacement for wholesale replacement, and
    _grounded_skeleton's step-1 — the literal source of retry
    #30's stub — which is now an anchored edit of the handler
    definition with the branch sketch as the delta blank. The full
    source / skeleton stay in the prompt (anchor choice and exact
    indentation are copied from them), and the preservation
    contract stays as the fallback for a plain full-content
    scaffold. Legacy full-content scaffolds (new files, small
    files, everything else) work unchanged; bash/heredoc writes
    are untouched. The retry-#30 outputs still reject with exactly
    the same profiles under both repo roots, and the retry-#28/#29
    fixtures keep theirs (pinned by their own classes).

    Retry #32 addendum: the delta blank itself turned out to be
    the next echo surface (retry #31 copied its instruction
    prose into the edit), so the blank is now the definition's
    real code with the branch extended in code form, and
    test_verbatim_skeleton_rejected_as_placeholder_delta became
    test_verbatim_skeleton_materializes_as_a_real_plan — see
    TestPlannerDeltaBlankFix.
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#30 outputs 3, 4, and 5 (the three real
    # plans), verbatim from the RAW PLANNER OUTPUT blocks of the
    # retry-#30 driver section of
    # logs/llm-end-to-end-health-retry30-2026-11-17.log (the same
    # extraction tools/probe-retry30-rejections.py performs).
    # Embedded (not read from the log) so the tests stay hermetic
    # in CI.
    RETRY30_OUTPUT3_RAW = '{\n"nodes":[\n{"id":"step-1","objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place   add the fields the goal asks for to the existing /health branch in the do_GET method of the Handler class (defined inside HiveAPIServer.start)","depends_on":[],"assigned_claw":"scaffold","inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py","content":"# Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place   add the fields the goal asks for to the existing /health branch in the do_GET method of the Handler class (defined inside HiveAPIServer.start):\\n#  if path == \\"/health\\":\\n#    return self._send(200,{\\"ok\\":True,\\"status\\":\\"ready\\"})\\n# Write the FULL module with the extended branch   the scaffold replaces the whole file, so a fragment alone is not a plan.\\n# Do NOT write a TODO comment   a comment-only scaffold is rejected."},"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],"acceptance":["The /health endpoint is implemented in /home/seeker/projects/FreeTalon/freetalon/api.py."],"criteria":[{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/freetalon/api.py"}]},{"id":"step-2","objective":"Write the tests for /health","depends_on":["step-1"],"assigned_claw":"bash","inputs":{"command":"cat > /home/seeker/freetalon-workspace/test_health.py <<\'EOF\'\\nimport json\\nimport sys\\nimport tempfile\\nimport unittest\\nimport urllib.request\\nfrom pathlib import Path\\n\\nsys.path.insert(0, \\"/home/seeker/projects/FreeTalon\\")\\n\\nfrom freetalon.api import HiveAPIServer\\nfrom freetalon.config import HiveConfig\\nfrom freetalon.hive import HiveController\\n\\n\\nclass RouteEndpointTests(unittest.TestCase):\\n    def setUp(self):\\n        self.tmpdir = tempfile.TemporaryDirectory()\\n        base = Path(self.tmpdir.name)\\n        config = HiveConfig(\\n            workspace=base,\\n            state_path=base / \\"state.json\\",\\n            audit_log_path=base / \\"audit.log\\",\\n            worker_cap=2,\\n            queue_multiplier=3,\\n            poll_interval_seconds=0.02,\\n        )\\n        self.controller = HiveController(config)\\n        self.controller.start()\\n        self.server = HiveAPIServer(self.controller, \\"test-token\\")\\n        self.server.start(\\"127.0.0.1\\", 0)\\n        port = self.server._httpd.server_address[1]\\n        self.base = f\\"http://127.0.0.1:{port}\\"\\n\\n    def tearDown(self):\\n        self.server.stop()\\n        self.controller.stop()\\n        self.tmpdir.cleanup()\\n\\n    def test_route_answers(self):\\n        with urllib.request.urlopen(f\\"{self.base}/health\\", timeout=3) as resp:\\n            status = resp.status\\n            data = json.loads(resp.read())\\n        self.assertEqual(status, 200)\\n        self.assertTrue(data[\\"ok\\"])\\n        self.assertIn(\\"status\\", data)\\n        self.assertIn(\\"service_status\\", data)\\n        self.assertIn(\\"ollama_reachable\\", data)\\n        self.assertIsInstance(data[\\"ollama_reachable\\"], bool)\\n        self.assertIn(\\"gpu_temperature\\", data)\\n        self.assertIsInstance(data[\\"gpu_temperature\\"], (int, float))\\n\\n\\nif __name__ == \\"__main__\\":\\n    unittest.main()\\nEOF"},"outputs":["/home/seeker/freetalon-workspace/test_health.py"],"acceptance":["The tests for /health exist."],"criteria":[{"type":"file_exists","path":"/home/seeker/freetalon-workspace/test_health.py"}]},{"id":"step-3","objective":"Run the tests for /health","depends_on":["step-2"],"assigned_claw":"bash","inputs":{"command":"python3 /home/seeker/freetalon-workspace/test_health.py"},"outputs":["test results"],"acceptance":["The tests for /health pass."],"criteria":[{"type":"exit_code","equals":0}]},{"id":"step-4","objective":"Start the API server for /health","depends_on":["step-3"],"assigned_claw":"bash","inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && for i in $(seq 1 30); do curl -sf http://localhost:8765/health && break; sleep 1; done; curl -sf http://localhost:8765/health"},"outputs":["API server running"],"acceptance":["The API server answers /health."],"criteria":[{"type":"exit_code","equals":0}]},{"id":"step-5","objective":"Verify /health live","depends_on":["step-4"],"assigned_claw":"bash","inputs":{"command":"curl -s http://localhost:8765/health"},"outputs":["verification"],"acceptance":["/health returns its JSON live."],"criteria":[{"type":"exit_code","equals":0}]}]\n}'
    RETRY30_OUTPUT4_RAW = '{\n"nodes":\n[\n{\n"id":\n"step-1",\n"objective":\n"Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place   add a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"# Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place   add the fields the goal asks for to the existing /health branch in the do_GET method of the Handler class (defined inside HiveAPIServer.start):\\n#\\tif path == \\"/health\\":\\n#\\t\\t\\treturn self._send(200,{\\"ok\\": True, \\"status\\": \\"ready\\"})\\n# Write the FULL module with the extended branch   the scaffold replaces the whole file, so a fragment alone is not a plan.\\n# Do NOT write a TODO comment   a comment-only scaffold is rejected."\n},\n"outputs":\n[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":\n[\n"The /health endpoint is implemented in /home/seeker/projects/FreeTalon/freetalon/api.py."\n],\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n]\n},\n{\n"id":\n"step-2",\n"objective":\n"Write the tests for /health",\n"depends_on":\n[\n"step-1"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cat > /home/seeker/freetalon-workspace/test_health.py <<\'EOF\'\\nimport json\\nimport sys\\nimport tempfile\\nimport unittest\\nimport urllib.request\\nfrom pathlib import Path\\n\\nsys.path.insert(0, \\"/home/seeker/projects/FreeTalon\\")\\n\\nfrom freetalon.api import HiveAPIServer\\nfrom freetalon.config import HiveConfig\\nfrom freetalon.hive import HiveController\\n\\n\\nclass RouteEndpointTests(unittest.TestCase):\\n    def setUp(self):\\n        self.tmpdir = tempfile.TemporaryDirectory()\\n        base = Path(self.tmpdir.name)\\n        config = HiveConfig(\\n            workspace=base,\\n            state_path=base / \\"state.json\\",\\n            audit_log_path=base / \\"audit.log\\",\\n            worker_cap=2,\\n            queue_multiplier=3,\\n            poll_interval_seconds=0.02,\\n        )\\n        self.controller = HiveController(config)\\n        self.controller.start()\\n        self.server = HiveAPIServer(self.controller, \\"test-token\\")\\n        self.server.start(\\"127.0.0.1\\", 0)\\n        port = self.server._httpd.server_address[1]\\n        self.base = f\\"http://127.0.0.1:{port}\\"\\n\\n    def tearDown(self):\\n        self.server.stop()\\n        self.controller.stop()\\n        self.tmpdir.cleanup()\\n\\n    def test_route_answers(self):\\n        with urllib.request.urlopen(f\\"{self.base}/health\\", timeout=3) as resp:\\n            status = resp.status\\n            data = json.loads(resp.read())\\n        self.assertEqual(status, 200)\\n        self.assertTrue(data[\\"ok\\"])\\n        self.assertIn(\\"status\\", data)\\n        self.assertIn(\\"service_status\\", data)\\n        self.assertIn(\\"ollama_reachable\\", data)\\n        self.assertIsInstance(data[\\"ollama_reachable\\"], bool)\\n        self.assertIn(\\"gpu_temperature\\", data)\\n        self.assertIsInstance(data[\\"gpu_temperature\\"], (int, float))\\n\\n\\nif __name__ == \\"__main__\\":\\nunittest.main()\\nEOF"\n},\n"outputs":\n[\n"/home/seeker/freetalon-workspace/test_health.py"\n],\n"acceptance":\n[\n"The tests for /health exist."\n],\n"criteria":\n[\n{\n"type":\n"file_exists",\n"path":\n"/home/seeker/freetalon-workspace/test_health.py"\n}\n]\n},\n{\n"id":\n"step-3",\n"objective":\n"Run the tests for /health",\n"depends_on":\n[\n"step-2"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"python3 /home/seeker/freetalon-workspace/test_health.py"\n},\n"outputs":\n[\n"test results"\n],\n"acceptance":\n[\n"The tests for /health pass."\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n]\n},\n{\n"id":\n"step-4",\n"objective":\n"Start the API server for /health",\n"depends_on":\n[\n"step-3"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && for i in $(seq 1 30); do curl -sf http://localhost:8765/health && break; sleep 1; done; curl -sf http://localhost:8765/health"\n},\n"outputs":\n[\n"API server running"\n],\n"acceptance":\n[\n"The API server answers /health."\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n]\n},\n{\n"id":\n"step-5",\n"objective":\n"Verify /health live",\n"depends_on":\n[\n"step-4"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"curl -s http://localhost:8765/health"\n},\n"outputs":\n[\n"verification"\n],\n"acceptance":\n[\n"/health returns its JSON live."\n],\n"criteria":\n[\n{\n"type":\n"exit_code",\n"equals":\n0\n}\n]\n}\n]\n}'
    RETRY30_OUTPUT5_RAW = '{\n"nodes":[\n{"id":"step-1",\n"objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place   add a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"inputs":{\n"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":"# Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place   add the fields the goal asks for to the existing /health branch in the do_GET method of the Handler class (defined inside HiveAPIServer.start):\\n#\\tif path == \\"/health\\":\\n#\\t\\t\\treturn self._send(200,{\\"ok\\":True,\\"status\\":\\"ready\\",\\"service_status\\":\\"running\\",\\"ollama_reachable\\":true,\\"gpu_temperature\\":25})\\n# Write the FULL module with the extended branch   the scaffold replaces the whole file, so a fragment alone is not a plan.\\n# Do NOT write a TODO comment   a comment-only scaffold is rejected."\n},\n"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],\n"acceptance":["The /health endpoint is implemented in /home/seeker/projects/FreeTalon/freetalon/api.py."],\n"criteria":[{"type":"file_exists",\n"path":"/home/seeker/projects/FreeTalon/freetalon/api.py"}]\n},\n{"id":"step-2",\n"objective":"Write the tests for /health",\n"depends_on":["step-1"],\n"assigned_claw":"bash",\n"inputs":{\n"command":"cat > /home/seeker/freetalon-workspace/test_health.py <<\'EOF\'\\nimport json\\nimport sys\\nimport tempfile\\nimport unittest\\nimport urllib.request\\nfrom pathlib import Path\\n\\nsys.path.insert(0, \\"/home/seeker/projects/FreeTalon\\")\\n\\nfrom freetalon.api import HiveAPIServer\\nfrom freetalon.config import HiveConfig\\nfrom freetalon.hive import HiveController\\n\\n\\nclass RouteEndpointTests(unittest.TestCase):\\n    def setUp(self):\\n        self.tmpdir = tempfile.TemporaryDirectory()\\n        base = Path(self.tmpdir.name)\\n        config = HiveConfig(\\n            workspace=base,\\n            state_path=base / \\"state.json\\",\\n            audit_log_path=base / \\"audit.log\\",\\n            worker_cap=2,\\n            queue_multiplier=3,\\n            poll_interval_seconds=0.02,\\n        )\\n        self.controller = HiveController(config)\\n        self.controller.start()\\n        self.server = HiveAPIServer(self.controller, \\"test-token\\")\\n        self.server.start(\\"127.0.0.1\\", 0)\\n        port = self.server._httpd.server_address[1]\\n        self.base = f\\"http://127.0.0.1:{port}\\"\\n\\n    def tearDown(self):\\n        self.server.stop()\\n        self.controller.stop()\\n        self.tmpdir.cleanup()\\n\\n    def test_route_answers(self):\\n        with urllib.request.urlopen(f\\"{self.base}/health\\", timeout=3) as resp:\\n            status = resp.status\\n            data = json.loads(resp.read())\\n        self.assertEqual(status, 200)\\n        self.assertTrue(data[\\"ok\\"])\\n        self.assertIn(\\"status\\", data)\\n        self.assertIn(\\"service_status\\", data)\\n        self.assertIn(\\"ollama_reachable\\", data)\\n        self.assertIsInstance(data[\\"ollama_reachable\\"], bool)\\n        self.assertIn(\\"gpu_temperature\\", data)\\n        self.assertIsInstance(data[\\"gpu_temperature\\"], (int, float))\\n\\n\\nif __name__ == \\"__main__\\":\\n    unittest.main()\\nEOF"\n},\n"outputs":["/home/seeker/freetalon-workspace/test_health.py"],\n"acceptance":["The tests for /health exist."],\n"criteria":[{"type":"file_exists",\n"path":"/home/seeker/freetalon-workspace/test_health.py"}]\n},\n{"id":"step-3",\n"objective":"Run the tests for /health",\n"depends_on":["step-2"],\n"assigned_claw":"bash",\n"inputs":{\n"command":"python3 /home/seeker/freetalon-workspace/test_health.py"\n},\n"outputs":["test results"],\n"acceptance":["The tests for /health pass."],\n"criteria":[{"type":"exit_code",\n"equals":0}]\n},\n{"id":"step-4",\n"objective":"Start the API server for /health",\n"depends_on":["step-3"],\n"assigned_claw":"bash",\n"inputs":{\n"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && for i in $(seq 1 30); do curl -sf http://localhost:8765/health && break; sleep 1; done; curl -sf http://localhost:8765/health"\n},\n"outputs":["API server running"],\n"acceptance":["The API server answers /health."],\n"criteria":[{"type":"exit_code",\n"equals":0}]\n},\n{"id":"step-5",\n"objective":"Verify /health live",\n"depends_on":["step-4"],\n"assigned_claw":"bash",\n"inputs":{\n"command":"curl -s http://localhost:8765/health"\n},\n"outputs":["verification"],\n"acceptance":["/health returns its JSON live."],\n"criteria":[{"type":"exit_code",\n"equals":0}]\n}\n]\n}'

    # A fixture module with the shapes materialization must handle:
    # a decorated multi-line module-level function, a class whose
    # method defines a nested handler class, a one-line body.
    # Qualified definitions (9): _wrap, _wrap.inner, greet, Server,
    # Server.start, Server.start.Handler,
    # Server.start.Handler.do_GET, Server.start.Handler.do_POST,
    # Server.stop.
    MODULE_SOURCE = (
        '"""Fixture module for anchored edits."""\n'
        "import functools\n"
        "\n"
        "\n"
        "def _wrap(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def inner(*args, **kwargs):\n"
        "        return fn(*args, **kwargs)\n"
        "    return inner\n"
        "\n"
        "\n"
        "@_wrap\n"
        "def greet(\n"
        "    name: str,\n"
        ") -> str:\n"
        '    return "hello " + name\n'
        "\n"
        "\n"
        "class Server:\n"
        "    def start(self):\n"
        "        class Handler:\n"
        "            def do_GET(self):\n"
        '                return "ok"\n'
        "\n"
        "            def do_POST(self):\n"
        '                return "posted"\n'
        "\n"
        "        return Handler\n"
        "\n"
        "    def stop(self):\n"
        "        return None\n"
    )

    def setUp(self) -> None:
        import tempfile

        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.module_path = str(self.tmp_root / "fixture_module.py")
        Path(self.module_path).write_text(self.MODULE_SOURCE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _edit_node(
        self,
        target: str,
        anchor: object,
        position: object,
        content: object,
        node_id: str = "step-1",
    ) -> dict:
        return {
            "id": node_id,
            "objective": "Edit the module in place",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": target,
                "anchor": anchor,
                "position": position,
                "content": content,
            },
            "outputs": [target],
            "acceptance": ["The module is edited."],
            "criteria": [{"type": "file_exists", "path": target}],
        }

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    # --- Materialization correctness ---

    def test_replace_nested_method_materializes_full_module(self) -> None:
        from freetalon.orchestrator.repo_index import qualified_def_names

        delta = (
            "            def do_GET(self):\n"
            '                return "ok-extended"\n'
        )
        nodes, _meta = parse_planner_response(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        delta,
                    )
                ]
            )
        )
        self.assertEqual(len(nodes), 1)
        inputs = nodes[0].inputs
        # The node is now indistinguishable from a legacy
        # full-content scaffold: no edit keys survive.
        self.assertNotIn("anchor", inputs)
        self.assertNotIn("position", inputs)
        content = inputs["content"]
        import ast

        ast.parse(content)
        self.assertEqual(
            qualified_def_names(content),
            qualified_def_names(self.MODULE_SOURCE),
        )
        self.assertIn('"ok-extended"', content)
        # Byte-exact splice: only the anchor's span changed.
        expected = self.MODULE_SOURCE.replace(
            '            def do_GET(self):\n                return "ok"\n',
            delta,
        )
        self.assertEqual(content, expected)

    def test_insert_after_adds_sibling_method(self) -> None:
        from freetalon.orchestrator.repo_index import qualified_def_names

        delta = (
            "            def do_PUT(self):\n"
            '                return "put"\n'
        )
        nodes, _meta = parse_planner_response(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "after",
                        delta,
                    )
                ]
            )
        )
        content = nodes[0].inputs["content"]
        names = qualified_def_names(content)
        # qualified_def_names is in document order: the new sibling
        # appears in position, between the anchor and the next method.
        expected = qualified_def_names(self.MODULE_SOURCE)
        expected.insert(
            expected.index("Server.start.Handler.do_GET") + 1,
            "Server.start.Handler.do_PUT",
        )
        self.assertEqual(names, expected)
        # The sibling lands between the anchor and the next method.
        self.assertLess(content.index("def do_GET"), content.index("def do_PUT"))
        self.assertLess(content.index("def do_PUT"), content.index("def do_POST"))

    def test_insert_before_at_module_level(self) -> None:
        delta = 'def farewell(name: str) -> str:\n    return "bye " + name\n'
        nodes, _meta = parse_planner_response(
            self._dag(
                [self._edit_node(self.module_path, "greet", "before", delta)]
            )
        )
        content = nodes[0].inputs["content"]
        self.assertIn("farewell", content)
        # Inserted before the anchor's DECORATOR line: the decorator
        # still belongs to greet.
        self.assertLess(content.index("def farewell"), content.index("@_wrap"))
        self.assertLess(content.index("@_wrap"), content.index("def greet"))

    def test_replace_decorated_multiline_definition(self) -> None:
        delta = (
            "@_wrap\n"
            "def greet(\n"
            "    name: str,\n"
            ") -> str:\n"
            '    return "hi " + name\n'
        )
        nodes, _meta = parse_planner_response(
            self._dag(
                [self._edit_node(self.module_path, "greet", "replace", delta)]
            )
        )
        content = nodes[0].inputs["content"]
        self.assertIn('"hi " + name', content)
        self.assertNotIn('"hello " + name', content)
        from freetalon.orchestrator.repo_index import qualified_def_names

        self.assertEqual(
            qualified_def_names(content),
            qualified_def_names(self.MODULE_SOURCE),
        )

    def test_multiple_edits_same_module_accumulate(self) -> None:
        first = self._edit_node(
            self.module_path,
            "Server.start.Handler.do_GET",
            "replace",
            '            def do_GET(self):\n                return "ok-extended"\n',
        )
        second = self._edit_node(
            self.module_path,
            "Server.start.Handler.do_POST",
            "after",
            '            def do_PUT(self):\n                return "put"\n',
            node_id="step-2",
        )
        second["depends_on"] = ["step-1"]
        nodes, _meta = parse_planner_response(self._dag([first, second]))
        self.assertEqual(len(nodes), 2)
        # The second node's content is built on the first edit's
        # result (not the stale file): it carries BOTH changes, so
        # executing the nodes in order lands both edits.
        final = nodes[1].inputs["content"]
        self.assertIn('"ok-extended"', final)
        self.assertIn('"put"', final)
        from freetalon.orchestrator.repo_index import qualified_def_names

        self.assertIn(
            "Server.start.Handler.do_PUT", qualified_def_names(final)
        )

    def test_real_module_edit_in_grounded_plan_accepted(self) -> None:
        # The grounded plan shape for the /health goal, with step-1
        # as an anchored edit of the real api.py: replace do_GET
        # with its own text plus the extended /health branch.
        from freetalon.orchestrator.planner import _definition_spans

        source = Path(self.api_path).read_text(encoding="utf-8")
        spans = {
            name: (start, end)
            for name, start, end, _indent in _definition_spans(source)
        }
        start, end = spans["HiveAPIServer.start.Handler.do_GET"]
        do_get = "\n".join(source.splitlines()[start - 1 : end])
        delta = do_get.replace(
            'return self._send(200, {"ok": True, "status": "ready"})',
            'return self._send(200, {"ok": True, "status": "ready", '
            '"service_status": "ready", "ollama_reachable": True, '
            '"gpu_temperature": 42})',
        )
        self.assertNotEqual(delta, do_get)
        ws = self.WS
        nodes = [
            {
                "id": "step_1",
                "objective": "Extend the health endpoint in freetalon/api.py",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": self.api_path,
                    "anchor": "HiveAPIServer.start.Handler.do_GET",
                    "position": "replace",
                    "content": delta,
                },
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
                "criteria": [
                    {"type": "file_exists", "path": f"{ws}/test_health.py"}
                ],
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
        for root in (None, str(self.repo_root)):
            parsed, _meta = parse_planner_response(
                self._dag(nodes),
                goal_text=self.HEALTH_GOAL,
                workspace=ws,
                repo_root=root,
            )
            self.assertEqual(len(parsed), 4)
            content = parsed[0].inputs["content"]
            self.assertNotIn("anchor", parsed[0].inputs)
            self.assertIn('"gpu_temperature": 42', content)
            from freetalon.orchestrator.repo_index import qualified_def_names

            self.assertEqual(
                qualified_def_names(content), qualified_def_names(source)
            )

    # --- Error cases: named and steerable ---

    def test_anchor_not_found_names_the_definitions(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_DELETE",
                        "replace",
                        "            def do_DELETE(self):\n"
                        '                return "gone"\n',
                    )
                ]
            )
        )
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("is not a definition of", message)
        self.assertIn("Server.start.Handler.do_DELETE", message)
        # The full definition list rides along, so the repair can
        # pick a real anchor.
        self.assertIn("Server.start.Handler.do_GET", message)
        self.assertIn("Server.stop", message)
        self.assertIn("ANCHORED EDIT", message)

    def test_ambiguous_anchor(self) -> None:
        dup_path = str(self.tmp_root / "dup.py")
        Path(dup_path).write_text(
            "class Dup:\n"
            "    def m(self):\n"
            "        return 1\n"
            "\n"
            "    def m(self):\n"
            "        return 2\n"
            "\n"
            "\n"
            "def a():\n"
            "    return 1\n"
            "\n"
            "\n"
            "def b():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        dup_path,
                        "Dup.m",
                        "replace",
                        "    def m(self):\n        return 3\n",
                    )
                ]
            )
        )
        self.assertIn("names 2 definitions", message)
        self.assertIn("must name exactly one", message)

    def test_placeholder_delta_is_a_cheap_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        raw = self._dag(
            [
                self._edit_node(
                    self.module_path,
                    "Server.start.Handler.do_GET",
                    "replace",
                    "# TODO: write the extended handler here\n",
                )
            ]
        )
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(raw)
        message = str(caught.exception)
        self.assertIn("placeholder content only", message)
        self.assertIn("anchored edit", message)

    def test_unparseable_splice(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "after",
                        "            def do_PUT(self)\n"
                        '                return "put"\n',
                    )
                ]
            )
        )
        self.assertIn("does not parse as Python", message)
        self.assertIn("Server.start.Handler.do_GET", message)

    def test_non_uniform_indentation_still_rejected(self) -> None:
        # Retry #33 changed this pin (it was test_indent_mismatch):
        # the delta it used before — the anchored definition
        # dedented uniformly to column 0 — is exactly the copy
        # normalization materialization now absorbs (see
        # test_uniformly_dedented_replace_is_reindented below).
        # What stays rejected is a delta whose lines do NOT share
        # one shift: here the first line sits at 2 spaces while a
        # later line dedents below that base, so no uniform
        # re-indent exists and the error still names both indents.
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        '  def do_GET(self):\n      return "ok"\n x = 1\n',
                    )
                ]
            )
        )
        self.assertIn("starts at 2 spaces of indentation", message)
        self.assertIn("sits at 12 spaces in the file", message)

    def test_uniformly_dedented_replace_is_reindented(self) -> None:
        # Retry #33: the model's copy of the anchored definition
        # with the file's uniform indentation stripped (def at
        # column 0, body at 4) is re-indented to the anchor's
        # indentation and materializes; the materialized module is
        # byte-identical to the splice of the correctly-indented
        # delta.
        nodes, _meta = parse_planner_response(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        'def do_GET(self):\n    return "ok"\n',
                    )
                ]
            )
        )
        dedented_content = nodes[0].inputs["content"]
        nodes, _meta = parse_planner_response(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        '            def do_GET(self):\n'
                        '                return "ok"\n',
                    )
                ]
            )
        )
        self.assertEqual(dedented_content, nodes[0].inputs["content"])
        self.assertIn(
            '            def do_GET(self):\n                return "ok"\n',
            dedented_content,
        )

    def test_replace_losing_nested_definitions(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start",
                        "replace",
                        "    def start(self):\n        return None\n",
                    )
                ]
            )
        )
        self.assertIn("loses existing definitions", message)
        self.assertIn("Server.start.Handler", message)
        self.assertIn("Server.start.Handler.do_GET", message)

    def test_replace_with_a_different_definition(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        "            def do_FETCH(self):\n"
                        '                return "x"\n',
                    )
                ]
            )
        )
        self.assertIn("defines do_FETCH at its top level", message)
        self.assertIn(
            "complete new version of 'Server.start.Handler.do_GET'", message
        )

    def test_replace_with_body_fragment(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        "            if True:\n                return 1\n",
                    )
                ]
            )
        )
        self.assertIn("defines no definition at its top level", message)

    def test_replace_with_the_whole_module(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path, "greet", "replace", self.MODULE_SOURCE
                    )
                ]
            )
        )
        self.assertIn("not the whole module", message)

    def test_position_without_anchor_and_bad_position(self) -> None:
        node = self._edit_node(
            self.module_path,
            "Server.stop",
            "replace",
            "    def stop(self):\n        return None\n",
        )
        del node["inputs"]["anchor"]
        message = self._rejection(self._dag([node]))
        self.assertIn("carries a 'position' but no usable 'anchor'", message)

        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.stop",
                        "sideways",
                        "    def stop(self):\n        return None\n",
                    )
                ]
            )
        )
        self.assertIn("'position' must be 'replace'", message)

    def test_anchor_on_new_file(self) -> None:
        new_path = str(self.tmp_root / "brand_new.py")
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        new_path,
                        "thing",
                        "replace",
                        "def thing():\n    return 1\n",
                    )
                ]
            )
        )
        self.assertIn("does not exist", message)
        self.assertIn("without an 'anchor'", message)

    def test_anchor_on_tiny_and_unparseable_modules(self) -> None:
        tiny_path = str(self.tmp_root / "tiny.py")
        Path(tiny_path).write_text(
            "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\n"
            "def c():\n    return 3\n",
            encoding="utf-8",
        )
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        tiny_path, "a", "replace", "def a():\n    return 9\n"
                    )
                ]
            )
        )
        self.assertIn("too small for an anchored edit", message)

        broken_path = str(self.tmp_root / "broken.py")
        Path(broken_path).write_text("def nope(:\n", encoding="utf-8")
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        broken_path, "nope", "replace", "def nope():\n    pass\n"
                    )
                ]
            )
        )
        self.assertIn("does not parse as Python", message)
        self.assertIn("no definition in it can be anchored", message)

    def test_anchor_on_non_python_target(self) -> None:
        text_path = str(self.tmp_root / "notes.txt")
        Path(text_path).write_text("some notes\n", encoding="utf-8")
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        text_path, "thing", "replace", "def thing():\n    pass\n"
                    )
                ]
            )
        )
        self.assertIn("is not a Python module", message)

    # --- Legacy form untouched ---

    def test_legacy_full_content_scaffold_unchanged(self) -> None:
        node = {
            "id": "step-1",
            "objective": "Rewrite the module wholesale (legacy form)",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": self.module_path,
                "content": self.MODULE_SOURCE,
            },
            "outputs": [self.module_path],
            "acceptance": ["The module exists."],
            "criteria": [{"type": "file_exists", "path": self.module_path}],
        }
        nodes, _meta = parse_planner_response(self._dag([node]))
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].inputs["content"], self.MODULE_SOURCE)

    # --- Steering ---

    def test_shape_block_teaches_anchored_edit(self) -> None:
        from freetalon.orchestrator.planner import _route_goal_shape

        for delta in (False, True):
            shape = _route_goal_shape(
                self.HEALTH_GOAL, str(self.repo_root), delta=delta
            )
            self.assertIn("EXISTING MODULE", shape)
            self.assertIn("PREFER an anchored edit", shape)
            self.assertIn('"anchor"', shape)
            # The goal's natural anchor is named concretely.
            self.assertIn(
                "natural anchor is 'HiveAPIServer.start.Handler.do_GET'",
                shape,
            )
            # The preservation contract stays as the fallback.
            self.assertIn("full updated module", shape)
            self.assertIn("same nesting", shape)

    def test_extension_block_teaches_anchored_edit(self) -> None:
        from freetalon.orchestrator.planner import _extension_steering_block

        raw = TestPlannerExtensionSteeringFix.RETRY28_OUTPUT2_RAW
        message = self._rejection(
            raw,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        codes = _rejection_codes(
            raw,
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
            repo_root=None,
        )
        block = _extension_steering_block(raw, codes, None)
        self.assertIn("EXISTING-MODULE EXTENSION", block)
        self.assertIn("ANCHORED EDIT", block)
        self.assertIn('"anchor"', block)
        self.assertIn("return by construction", block)
        # The fallback contract is still stated.
        self.assertIn("FULL updated module", block)

    def test_grounded_skeleton_step1_is_an_anchored_edit(self) -> None:
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(
            self.HEALTH_GOAL, str(self.repo_root), self.WS
        )
        self.assertIn("anchored edit", skeleton)
        parsed = json.loads(skeleton.split("\n", 1)[1])
        step1 = parsed["nodes"][0]
        self.assertEqual(
            step1["inputs"]["anchor"], "HiveAPIServer.start.Handler.do_GET"
        )
        self.assertEqual(step1["inputs"]["position"], "replace")

    def test_completed_skeleton_is_accepted(self) -> None:
        # The money test for retry #30's failure mode: take the
        # grounded skeleton the repair loop hands back, complete
        # step-1 with the real extended do_GET (the work retry #30's
        # model could not do by retyping the module), and the whole
        # plan validates.
        import json

        from freetalon.orchestrator.planner import (
            _definition_spans,
            _grounded_skeleton,
        )

        skeleton = _grounded_skeleton(
            self.HEALTH_GOAL, str(self.repo_root), self.WS
        )
        parsed = json.loads(skeleton.split("\n", 1)[1])
        source = Path(self.api_path).read_text(encoding="utf-8")
        spans = {
            name: (start, end)
            for name, start, end, _indent in _definition_spans(source)
        }
        start, end = spans["HiveAPIServer.start.Handler.do_GET"]
        do_get = "\n".join(source.splitlines()[start - 1 : end])
        parsed["nodes"][0]["inputs"]["content"] = do_get.replace(
            'return self._send(200, {"ok": True, "status": "ready"})',
            'return self._send(200, {"ok": True, "status": "ready", '
            '"service_status": "ready", "ollama_reachable": True, '
            '"gpu_temperature": 42})',
        )
        nodes, _meta = parse_planner_response(
            json.dumps(parsed),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertEqual(len(nodes), 5)
        content = nodes[0].inputs["content"]
        self.assertIn('"gpu_temperature": 42', content)
        from freetalon.orchestrator.repo_index import qualified_def_names

        self.assertEqual(
            qualified_def_names(content), qualified_def_names(source)
        )

    def test_verbatim_skeleton_materializes_as_a_real_plan(self) -> None:
        # Retry #32 changed this pin: it used to assert the
        # verbatim skeleton is rejected as a placeholder delta,
        # because step-1's delta blank was instruction prose and a
        # verbatim copy was a comment-only delta. The delta is now
        # the definition's real code (the branch extended in code
        # form), so a verbatim copy is judged on content like any
        # other delta: it materializes and the plan validates —
        # the run's own tests judge the "..." sketch values. What
        # must not come back is a placeholder rejection for real
        # code (see TestPlannerDeltaBlankFix for the prose side).
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton
        from freetalon.orchestrator.repo_index import qualified_def_names

        skeleton = _grounded_skeleton(
            self.HEALTH_GOAL, str(self.repo_root), self.WS
        )
        raw = skeleton.split("\n", 1)[1]
        nodes, _meta = parse_planner_response(
            raw, goal_text=self.HEALTH_GOAL, workspace=self.WS
        )
        self.assertEqual(len(nodes), 5)
        inputs = nodes[0].inputs
        # Materialized to the legacy full-content form, like any
        # accepted anchored edit.
        self.assertNotIn("anchor", inputs)
        self.assertNotIn("position", inputs)
        content = inputs["content"]
        source = Path(self.api_path).read_text(encoding="utf-8")
        self.assertEqual(
            qualified_def_names(content), qualified_def_names(source)
        )
        self.assertIn('payload["gpu_temperature"] = ...', content)
        # Silence the unused-import lint pattern used elsewhere.
        self.assertIsInstance(json.loads(raw), dict)

    def test_anchor_helpers(self) -> None:
        from freetalon.orchestrator.planner import (
            _definition_spans,
            _route_branch_anchor,
        )
        from freetalon.orchestrator.repo_index import qualified_def_names

        source = Path(self.api_path).read_text(encoding="utf-8")
        self.assertEqual(
            _route_branch_anchor(source, "/health"),
            "HiveAPIServer.start.Handler.do_GET",
        )
        self.assertIsNone(_route_branch_anchor(source, "/no-such-route"))
        self.assertIsNone(_route_branch_anchor("def nope(:\n", "/health"))
        spans = _definition_spans(source)
        self.assertEqual(
            [name for name, _s, _e, _i in spans], qualified_def_names(source)
        )
        by_name = {name: indent for name, _s, _e, indent in spans}
        self.assertEqual(by_name["HiveAPIServer.start.Handler"], 8)
        self.assertEqual(by_name["HiveAPIServer.start.Handler.do_GET"], 12)
        self.assertEqual(_definition_spans("def nope(:\n"), [])

    # --- Retry-#30 fixtures: identical rejections, both roots ---

    def test_retry30_outputs_still_rejected_identically(self) -> None:
        for root in (None, str(self.repo_root)):
            message3 = self._rejection(
                self.RETRY30_OUTPUT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 1 error", message3)
            self.assertIn("placeholder content only", message3)
            message4 = self._rejection(
                self.RETRY30_OUTPUT4_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            # Planner cycle 2 (payload channel): was 2 errors --
            # output 4's second error was its step-2 test heredoc
            # (the __main__ dedent); the payload channel repairs the
            # payload and reroutes the write to the scaffold form,
            # leaving the placeholder scaffold as the only defect.
            # The plan still rejects, on the placeholder alone.
            self.assertIn("was rejected with 1 error", message4)
            self.assertIn("placeholder content only", message4)
            message5 = self._rejection(
                self.RETRY30_OUTPUT5_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 1 error", message5)
            self.assertIn("placeholder content only", message5)


class TestPlannerDeltaBlankFix(unittest.TestCase):
    """Delta blank as real code + prose-delta detection (retry #31).

    Retry #31 (log llm-end-to-end-health-retry31-2026-11-17.log,
    DRIVER_EXIT 1) was the first run with anchored edits live:
    output 3 used the form CORRECTLY — anchor
    HiveAPIServer.start.Handler.do_GET, position "replace", the
    steering's named anchor — but filled the content slot with the
    grounded skeleton's delta-blank instruction text ("# Write the
    COMPLETE new version of ... here — real code only ... Do NOT
    write a TODO comment") instead of writing the method. Worse,
    the model's re-wrap dropped the '#' markers from the prose
    lines, so is_placeholder_content (comment-stripping, then not a
    lone string) and _is_placeholder_stub (needs a self-labeling
    comment) both missed it, and materialization rejected the
    delta at the INDENTATION gate (0 spaces vs the anchor's 12) —
    a true fact naming the wrong defect, at full price: the repair
    never got the grounded skeleton, and output 4 regressed to a
    garbled Flask full-content scaffold. This is the second
    consecutive echo failure (retry #30: the skeleton's step-1
    text; #31: the delta blank): the model copies whatever prose
    occupies an example's content slot.

    Fix (planner.py only; the guard, the placeholder check on
    materialized content, interpreter normalization, and the
    executor untouched):
    1. The grounded skeleton's anchored step-1 delta is REAL CODE
       (_anchored_delta_sketch): the anchored definition's current
       source sliced verbatim from the module (real signature,
       real body, true file indentation), with the route branch
       extended in code form — the branch's real response dict
       bound to a payload name, one payload["<field>"] = ... line
       per derived response field (_SKELETON_ROUTE_FIELDS, the
       same table step-2's assertions are generated from), and
       the branch's real return with the dict replaced by the
       payload. No instruction prose inside the content slot; the
       instructions live in the step-1 guidance around it. A
       verbatim copy now copies plausible code at the right
       indentation, and materialization judges it on content.
    2. Materialization detects comment/prose-only deltas at the
       placeholder check — which already sat BEFORE the
       indentation gate; the defect was detection, not position —
       via _delta_carries_code: a delta carries code when its
       dedented text parses with a real statement, or when a
       non-comment line is code-shaped. An echoed blank is named
       placeholder content (cheap rejection -> grounded skeleton
       repair); real code at the wrong indentation still hits the
       indentation gate with both indents stated.

    The retry-#31 fixtures below keep their profiles under both
    repo roots modulo ONE renamed error: output 3's single error
    is now the placeholder rejection instead of the indentation
    rejection (the correct name for the defect, and cheap).
    Output 4 (the Flask repair regression) is unchanged at 5
    errors. The retry-#28/#29/#30 fixtures keep theirs (pinned by
    their own classes).
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"

    # The exact retry-#31 outputs 3 and 4, verbatim from the RAW
    # PLANNER OUTPUT blocks of the retry-#31 driver section of
    # logs/llm-end-to-end-health-retry31-2026-11-17.log (the same
    # extraction tools/probe-retry31-rejections.py performs).
    # Embedded (not read from the log) so the tests stay hermetic
    # in CI.
    RETRY31_OUTPUT3_RAW = '{\n"nodes":[\n{"id":"step-1","objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place  \\u2014 add a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)","depends_on":[],"assigned_claw":"scaffold","inputs":{"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py","anchor":"HiveAPIServer.start.Handler.do_GET","position":"replace","content":"# Write the COMPLETE new version of HiveAPIServer.start.Handler.do_GET here  \\u2014 real code only, starting with its def line at the indentation it has in the module (12 spaces), with the existing /health branch extended with the fields the goal asks for.\\nThe branch as it exists today:\\n#  if path == \\"/health\\":\\n#      return self._send(200,{\\"ok\\":True,\\"status\\":\\"ready\\"})\\nThe system inserts your code into the current file in place of the old definition  \\u2014 every other definition is preserved.\\nDo NOT write the whole module.\\nDo NOT write a TODO comment  \\u2014 comment-only content is rejected."},"outputs":["/home/seeker/projects/FreeTalon/freetalon/api.py"],"acceptance":["The /health endpoint is implemented in /home/seeker/projects/FreeTalon/freetalon/api.py."],"criteria":[{"type":"file_exists","path":"/home/seeker/projects/FreeTalon/freetalon/api.py"}]},{"id":"step-2","objective":"Write the tests for /health","depends_on":["step-1"],"assigned_claw":"bash","inputs":{"command":"cat > /home/seeker/freetalon-workspace/test_health.py <<\'EOF\'\\nimport json\\nimport sys\\nimport tempfile\\nimport unittest\\nimport urllib.request\\nfrom pathlib import Path\\n\\nsys.path.insert(0, \\"/home/seeker/projects/FreeTalon\\")\\n\\nfrom freetalon.api import HiveAPIServer\\nfrom freetalon.config import HiveConfig\\nfrom freetalon.hive import HiveController\\n\\n\\nclass RouteEndpointTests(unittest.TestCase):\\n    def setUp(self):\\n        self.tmpdir = tempfile.TemporaryDirectory()\\n        base = Path(self.tmpdir.name)\\n        config = HiveConfig(\\n            workspace=base,\\n            state_path=base / \\"state.json\\",\\n            audit_log_path=base / \\"audit.log\\",\\n            worker_cap=2,\\n            queue_multiplier=3,\\n            poll_interval_seconds=0.02,\\n        )\\n        self.controller = HiveController(config)\\n        self.controller.start()\\n        self.server = HiveAPIServer(self.controller, \\"test-token\\")\\n        self.server.start(\\"127.0.0.1\\", 0)\\n        port = self.server._httpd.server_address[1]\\n        self.base = f\\"http://127.0.0.1:{port}\\"\\n\\n    def tearDown(self):\\n        self.server.stop()\\n        self.controller.stop()\\n        self.tmpdir.cleanup()\\n\\n    def test_route_answers(self):\\n        with urllib.request.urlopen(f\\"{self.base}/health\\", timeout=3) as resp:\\n            status = resp.status\\n            data = json.loads(resp.read())\\n        self.assertEqual(status, 200)\\n        self.assertTrue(data[\\"ok\\"])\\n        self.assertIn(\\"status\\", data)\\n        self.assertIn(\\"service_status\\", data)\\n        self.assertIn(\\"ollama_reachable\\", data)\\n        self.assertIsInstance(data[\\"ollama_reachable\\"], bool)\\n        self.assertIn(\\"gpu_temperature\\", data)\\n        self.assertIsInstance(data[\\"gpu_temperature\\"], (int, float))\\n\\n\\nif __name__ == \\"__main__\\":\\n    unittest.main()\\nEOF"},"outputs":["/home/seeker/freetalon-workspace/test_health.py"],"acceptance":["The tests for /health exist."],"criteria":[{"type":"file_exists","path":"/home/seeker/freetalon-workspace/test_health.py"}]},{"id":"step-3","objective":"Run the tests for /health","depends_on":["step-2"],"assigned_claw":"bash","inputs":{"command":"python3 /home/seeker/freetalon-workspace/test_health.py"},"outputs":["test results"],"acceptance":["The tests for /health pass."],"criteria":[{"type":"exit_code","equals":0}]},{"id":"step-4","objective":"Start the API server for /health","depends_on":["step-3"],"assigned_claw":"bash","inputs":{"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && for i in $(seq 1 30); do curl -sf http://localhost:8765/health && break; sleep 1; done; curl -sf http://localhost:8765/health"},"outputs":["API server running"],"acceptance":["The API server answers /health."],"criteria":[{"type":"exit_code","equals":0}]},{"id":"step-5","objective":"Verify /health live","depends_on":["step-4"],"assigned_claw":"bash","inputs":{"command":"curl -s http://localhost:8765/health"},"outputs":["verification"],"acceptance":["/health returns its JSON live."],"criteria":[{"type":"exit_code","equals":0}]}]\n}'

    RETRY31_OUTPUT4_RAW = '{"nodes":\n[\n{\n"id":\n"step-1",\n"objective":\n"Modify HiveAPIServer.start.Handler.do_GET to include logging.",\n"depends_on":\n[],\n"assigned_claw":\n"scaffold",\n"inputs":\n{\n"file_path":\n"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"content":\n"from flask import Flask, request\\nimport logging\\n\\napp = Flask(__name__)\\nlogging.basicConfig(level=logging.INFO)\\n\\ndef start():\\n    Handler = app\\n    @Handler.route(\'/health\', methods=[\'GET\'])\\ndef do_GET():\\n        logging.info(\'Received health check request\')\\n        return \'OK\'\\n\\nif __name__ == \'__main__\':\\n    start()\\n    app.run(debug=True)"\n}\n},\n{\n"id":\n"step-2",\n"objective":\n"Run tests to verify the changes.",\n"depends_on":\n[\n"step-1"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests"\n}\n},\n{\n"id":\n"step-3",\n"objective":\n"Verify that the tests pass.",\n"depends_on":\n[\n"step-2"\n],\n"assigned_claw":\n"bash",\n"inputs":\n{\n"command":\n"echo \'Tests passed\'"\n}\n}\n]\n}'

    # The pre-fix skeleton's step-1 delta blank for the /health
    # goal, byte-exact as _grounded_skeleton emitted it before
    # this fix (generated from the pre-fix code): pure instruction
    # prose plus the branch as comment lines.
    OLD_DELTA_BLANK = '# Write the COMPLETE new version of HiveAPIServer.start.Handler.do_GET here — real code only, starting with its def line at the indentation it has in the module (12 spaces), with the existing /health branch extended with the fields the goal asks for. The branch as it exists today:\n#     if path == "/health":\n#                         return self._send(200, {"ok": True, "status": "ready"})\n# The system inserts your code into the current file in place of the old definition — every other definition is preserved. Do NOT write the whole module.\n# Do NOT write a TODO comment — comment-only content is rejected.'

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _edit_node(self, target: str, anchor: str, content: str) -> dict:
        return {
            "id": "step-1",
            "objective": "Extend the module in place",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": target,
                "anchor": anchor,
                "position": "replace",
                "content": content,
            },
            "outputs": [target],
            "acceptance": ["The module is edited."],
            "criteria": [{"type": "file_exists", "path": target}],
        }

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    # --- The delta blank is real code now ---

    def test_skeleton_step1_delta_is_real_code(self) -> None:
        import ast
        import json
        import textwrap

        from freetalon.orchestrator.planner import (
            _delta_carries_code,
            _delta_top_level_def_names,
            _grounded_skeleton,
        )
        from freetalon.orchestrator.tool_registry import (
            is_placeholder_content,
        )

        skeleton = _grounded_skeleton(
            self.HEALTH_GOAL, str(self.repo_root), self.WS
        )
        parsed = json.loads(skeleton.split("\n", 1)[1])
        inputs = parsed["nodes"][0]["inputs"]
        self.assertEqual(
            inputs["anchor"], "HiveAPIServer.start.Handler.do_GET"
        )
        self.assertEqual(inputs["position"], "replace")
        content = inputs["content"]
        # Real code: parses as the anchored definition, at the
        # anchor's true file indentation (12 spaces), carrying the
        # branch in code form — not as comment lines.
        self.assertEqual(_delta_top_level_def_names(content), ["do_GET"])
        first_line = content.splitlines()[0]
        self.assertEqual(
            len(first_line) - len(first_line.lstrip()), 12
        )
        self.assertTrue(first_line.lstrip().startswith("def do_GET"))
        branch_lines = [
            line for line in content.splitlines()
            if 'if path == "/health":' in line
        ]
        self.assertEqual(len(branch_lines), 1)
        self.assertFalse(branch_lines[0].lstrip().startswith("#"))
        ast.parse(textwrap.dedent(content))
        # The branch is extended in code form: the real response
        # dict bound to payload, the goal's fields as sketch
        # values, the real return sending the payload.
        self.assertIn('payload = {"ok": True, "status": "ready"}', content)
        self.assertIn('payload["service_status"] = ...', content)
        self.assertIn('payload["ollama_reachable"] = ...', content)
        self.assertIn('payload["gpu_temperature"] = ...', content)
        self.assertIn("return self._send(200, payload)", content)
        # No instruction prose inside the content slot: every
        # phrase of the old blank is gone from it.
        for phrase in (
            "Write the COMPLETE new version",
            "The branch as it exists today",
            "The system inserts your code",
            "Do NOT write the whole module",
            "Do NOT write a TODO comment",
            "comment-only content is rejected",
        ):
            self.assertNotIn(phrase, content)
        # And it is not placeholder content by either measure.
        self.assertFalse(is_placeholder_content(content))
        self.assertTrue(_delta_carries_code(content))

    def test_skeleton_step2_assertions_unchanged(self) -> None:
        # The step-2 test code is now generated from the shared
        # _SKELETON_ROUTE_FIELDS table; its assertions must stay
        # byte-identical to the hand-written block they replace.
        import json

        from freetalon.orchestrator.planner import _grounded_skeleton

        skeleton = _grounded_skeleton(
            self.HEALTH_GOAL, str(self.repo_root), self.WS
        )
        parsed = json.loads(skeleton.split("\n", 1)[1])
        command = parsed["nodes"][1]["inputs"]["command"]
        for line in (
            '        self.assertIn("service_status", data)\n',
            '        self.assertIn("ollama_reachable", data)\n',
            '        self.assertIsInstance(data["ollama_reachable"], bool)\n',
            '        self.assertIn("gpu_temperature", data)\n',
            '        self.assertIsInstance(data["gpu_temperature"], (int, float))\n',
        ):
            self.assertIn(line, command)

    def test_anchored_delta_sketch_generic_shapes(self) -> None:
        from freetalon.orchestrator.planner import _anchored_delta_sketch

        source = (
            "class Server:\n"
            "    def start(self):\n"
            "        class Handler:\n"
            "            def do_GET(self):\n"
            '                path = "/health"\n'
            '                if path == "/health":\n'
            '                    return self._send(200, {"ok": True})\n'
            '                return self._send(404, {"ok": False})\n'
        )
        anchor = "Server.start.Handler.do_GET"
        sketch = _anchored_delta_sketch(source, anchor, "/health")
        self.assertIsNotNone(sketch)
        assert sketch is not None
        self.assertIn('payload = {"ok": True}', sketch)
        self.assertIn('payload["gpu_temperature"] = ...', sketch)
        self.assertIn("return self._send(200, payload)", sketch)
        # The other branch is untouched real code.
        self.assertIn('return self._send(404, {"ok": False})', sketch)
        # A route with no derived fields: the definition verbatim.
        verbatim = _anchored_delta_sketch(source, anchor, "/status")
        self.assertIsNotNone(verbatim)
        assert verbatim is not None
        self.assertNotIn("payload", verbatim)
        self.assertIn('return self._send(200, {"ok": True})', verbatim)
        # A branch whose body is not a single dict-return: the
        # definition verbatim (extension is an enhancement, never
        # a requirement).
        two_step = source.replace(
            'return self._send(200, {"ok": True})',
            'data = {"ok": True}\n'
            "                    return self._send(200, data)",
        )
        sketch2 = _anchored_delta_sketch(two_step, anchor, "/health")
        self.assertIsNotNone(sketch2)
        assert sketch2 is not None
        self.assertNotIn("payload", sketch2)
        # No span for the anchor: None.
        self.assertIsNone(
            _anchored_delta_sketch(source, "Server.nope", "/health")
        )

    # --- Prose deltas are named placeholder BEFORE indentation ---

    def test_delta_carries_code_cases(self) -> None:
        from freetalon.orchestrator.planner import _delta_carries_code

        # The retry-#31 echo shape: instruction prose with the
        # comment markers dropped carries no code.
        self.assertFalse(
            _delta_carries_code(
                "# Write the COMPLETE new version here — real code only.\n"
                "The branch as it exists today:\n"
                '#  if path == "/health":\n'
                "The system inserts your code into the current file.\n"
                "Do NOT write the whole module.\n"
                "Do NOT write a TODO comment — comment-only content "
                "is rejected."
            )
        )
        self.assertFalse(_delta_carries_code("# TODO: write it\n"))
        self.assertFalse(_delta_carries_code(""))
        self.assertFalse(_delta_carries_code('"""Only a docstring."""\n'))
        # Real code carries code — whole, fragmented, or broken.
        self.assertTrue(
            _delta_carries_code('def do_GET(self):\n    return "ok"\n')
        )
        self.assertTrue(
            _delta_carries_code(
                "            if True:\n                return 1\n"
            )
        )
        self.assertTrue(
            _delta_carries_code(
                "            def do_PUT(self)\n"
                '                return "put"\n'
            )
        )
        self.assertTrue(_delta_carries_code("import subprocess\n"))
        self.assertTrue(_delta_carries_code("payload = {}\n"))

    def test_old_blank_verbatim_is_a_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        raw = self._dag(
            [
                self._edit_node(
                    self.api_path,
                    "HiveAPIServer.start.Handler.do_GET",
                    self.OLD_DELTA_BLANK,
                )
            ]
        )
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                raw, goal_text=self.HEALTH_GOAL, workspace=self.WS
            )
        message = str(caught.exception)
        self.assertIn("was rejected with 1 error", message)
        self.assertIn("placeholder content only", message)
        self.assertIn("anchored edit", message)
        # Named as the placeholder it is — never as indentation.
        self.assertNotIn("spaces of indentation", message)

    def test_real_code_at_wrong_indentation_still_hits_the_gate(self) -> None:
        # The ordering contrast: a REAL definition whose lines do
        # not share one uniform shift is not a placeholder — it
        # reaches the indentation gate, with both indents stated.
        # Retry #33 changed this pin's delta: the uniformly
        # dedented definition it used before is exactly the copy
        # normalization materialization now absorbs (pinned in
        # TestPlannerReindentFix); here the first line sits at 2
        # spaces while a later line dedents below that base, so no
        # uniform re-indent exists.
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.api_path,
                        "HiveAPIServer.start.Handler.do_GET",
                        '  def do_GET(self):\n      return "ok"\n x = 1\n',
                    )
                ]
            ),
            goal_text=self.HEALTH_GOAL,
            workspace=self.WS,
        )
        self.assertIn("starts at 2 spaces of indentation", message)
        self.assertIn("sits at 12 spaces in the file", message)
        self.assertNotIn("placeholder content only", message)

    # --- Retry-#31 fixtures ---

    def test_retry31_output3_now_a_cheap_placeholder_rejection(self) -> None:
        from freetalon.orchestrator.planner import CheapRejectionError

        for root in (None, str(self.repo_root)):
            with self.assertRaises(CheapRejectionError) as caught:
                parse_planner_response(
                    self.RETRY31_OUTPUT3_RAW,
                    goal_text=self.HEALTH_GOAL,
                    workspace=self.WS,
                    repo_root=root,
                )
            message = str(caught.exception)
            self.assertIn("was rejected with 1 error", message)
            self.assertIn("placeholder content only", message)
            self.assertIn("anchored edit", message)
            # The one renamed error: before this fix, output 3's
            # single error was the indentation rejection ("starts
            # at 0 spaces of indentation ... sits at 12 spaces").
            # The defect was never indentation — the delta is the
            # echoed prose blank — so the placeholder name is the
            # correct one, and being cheap it routes the repair to
            # the grounded skeleton.
            self.assertNotIn("spaces of indentation", message)

    def test_retry31_output4_profile_unchanged(self) -> None:
        for root in (None, str(self.repo_root)):
            message = self._rejection(
                self.RETRY31_OUTPUT4_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertIn("was rejected with 5 errors", message)
            self.assertIn("has no 'criteria'", message)
            self.assertIn("does not parse as Python", message)
            self.assertIn("Flask", message)


class TestPlannerReindentFix(unittest.TestCase):
    """Uniform re-indentation of anchored-edit deltas + materialization repair steering (retry #32).

    Retry #32 (log llm-end-to-end-health-retry32-2026-11-17.log,
    DRIVER_EXIT 1) was the first run with the real-code delta blank
    (38d30eb) live: output 3 used the edit form CORRECTLY — anchor
    HiveAPIServer.start.Handler.do_GET, position "replace", the
    grounded skeleton's real-code sketch as its delta — but the
    model DEDENTED its copy: the uniform 12-space file indentation
    was stripped (def at column 0, body at 4), so materialization
    rejected it at the indentation gate. The placeholder gate
    correctly did NOT fire (the delta carries real code — the
    prose/code discrimination works). Output 4 (the repair)
    regressed to a garbled Flask full-content scaffold — the third
    consecutive near-miss -> Flask regression.

    The copy-normalization pattern is now two runs deep (retry
    #31: the model dropped the '#' markers re-wrapping prose;
    retry #32: it dropped the uniform indent): the model's copy
    normalizes whatever it carries. That is the same class of
    mechanical variance the interpreter normalization (d7aab15)
    absorbs for command strings — normalize the variance, judge
    the substance.

    Audit before coding (logs/reindent-fix-2026-11-17.log):
    (a) The gate compared the delta's first line's indent to the
        anchor span's ast col_offset — the same column for
        "replace" and for an "after"/"before" sibling — so one
        re-indent rule serves all three positions.
    (b) Repair steering after a materialization rejection: the
        extension block did NOT fire (0 chars on output 3's real
        rejection — its trigger is the overwrite guard's phrase
        alone), and the shape block DID fire (489 chars) telling
        the repair round "none of its code was rejected — keep
        the same code; fix ONLY the named strings" about a
        rejection whose only defect was the delta's indentation:
        the retry-#28 misfire one level down, and the plausible
        mechanism for the Flask fallback.
    (c) Output 3's delta with a hand-applied uniform re-indent
        (+12): step-1 materializes and every content check passes
        (parse OK; 12/12 qualified definitions; the overwrite
        guard, the placeholder/stub rules, and the framework
        guard all silent; the three Ellipsis payload lines
        survive verbatim — by design no planning check judges
        them; the plan's own step-2 test asserts the fields'
        types, which Ellipsis fails, so an accepted plan would
        meet the judge/replan loop at execution). The FULL
        output-3 plan still rejects — with exactly ONE error, on
        step-2: its test heredoc does not parse (unittest.main()
        at column 0 under the __main__ guard). In the live run
        that defect was masked, because materialization raises
        before the full-plan checks run.

    Fix (planner.py only; the guard, the placeholder check on
    materialized content, interpreter normalization, and the
    executor untouched):
    1. Re-indentation in _materialize_scaffold_edit: when the
       delta's indentation differs from the anchor's ONLY by a
       uniform shift — dedented by its own base indent it is
       coherent (for "replace", exactly the one anchored
       definition; for "after"/"before", it parses as sibling
       code) — the delta is re-indented to the anchor's file
       indentation and materialization proceeds; every downstream
       check judges the re-indented module exactly as before.
       Non-uniform or incoherent deltas keep the indentation
       error, both indents stated. Deliberately NO new rejection
       for the sketch's '...' values: they are judged downstream,
       by the plan's own tests and the judge/replan loop.
    2. Repair steering: the materialization stems joined
       _CONTENT_DEFECT_MARKERS (the shape block now stays silent
       on materialization rejections, mirroring the retry-#28
       guard fix), and _materialization_steering_block mirrors
       _extension_steering_block for the edit-form failure: the
       module's FULL definition list, the anchor's own
       indentation, the module's skeleton, and the
       keep-the-edit-form pattern. The placeholder-delta cheap
       rejection is excluded — its repair round already gets the
       grounded skeleton.

    Two pre-existing pins changed, both pinning the defect itself
    (a uniformly dedented real definition, now absorbed):
    TestPlannerAnchoredEditFix.test_indent_mismatch (renamed
    test_non_uniform_indentation_still_rejected, with a positive
    re-indent test beside it) and
    TestPlannerDeltaBlankFix.test_real_code_at_wrong_indentation_still_hits_the_gate
    (same name; its delta is now non-uniform). The retry-#28..#31
    fixtures keep their profiles (pinned by their own classes).
    """

    HEALTH_GOAL = TestPlannerFakeAndMemoryFix.HEALTH_GOAL
    WS = "/home/seeker/freetalon-workspace"
    MODULE_SOURCE = TestPlannerAnchoredEditFix.MODULE_SOURCE

    # The exact retry-#32 output 3, verbatim from the RAW PLANNER
    # OUTPUT blocks of the retry-#32 driver section of
    # logs/llm-end-to-end-health-retry32-2026-11-17.log (the same
    # extraction tools/probe-retry32-rejections.py performs).
    # Embedded (not read from the log) so the tests stay hermetic
    # in CI.
    RETRY32_OUTPUT3_RAW = '{\n"nodes":[\n{"id":"step-1",\n"objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place\\u2014add a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"inputs":{\n"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"anchor":"HiveAPIServer.start.Handler.do_GET",\n"position":"replace",\n"content":"\\ndef do_GET(self) -> None:\\n    parsed = urlparse(self.path)\\n    path = parsed.path\\n    qs = parse_qs(parsed.query)\\n\\n    if path == \\"/health\\":\\n        payload ={\\"ok\\":True, \\"status\\":\\"ready\\"}\\n        payload[\\"service_status\\"] = ...\\n        payload[\\"ollama_reachable\\"] = ...\\n        payload[\\"gpu_temperature\\"] = ...\\n        return self._send(200, payload)\\n\\n    if path == \\"/status\\":\\n        if not self._authorized():\\n            return\\n        return self._send(200,{\\"ok\\":True, \\"status\\":api.controller.status()})\\n\\n    if path == \\"/metrics\\":\\n        if not self._authorized():\\n            return\\n        status = api.controller.status()\\n        metrics ={\\n            \\"workers_healthy\\": status[\\"workers\\"][\\"healthy\\"],\\n            \\"workers_configured\\": status[\\"workers\\"][\\"configured\\"],\\n            \\"tasks_running\\": status[\\"queue\\"][\\"running\\"],\\n            \\"tasks_queued\\": status[\\"queue\\"][\\"queued\\"],\\n            \\"queue_max\\": status[\\"queue\\"][\\"max\\"]\\n        }\\n        return self._send(200,{\\"ok\\":True, \\"metrics\\": metrics})\\n\\n    if path.startswith(\\"/tasks/\\") and len(path.split(\\"/\\")) == 3:\\n        if not self._authorized():\\n            return\\n        task_id = path.split(\\"/\\")[2]\\n        task = api.controller.get_task(task_id)\\n        if task is None:\\n            return self._send(404,{\\"ok\\":False, \\"error\\":\\"Task not found\\"})\\n        return self._send(200,{\\"ok\\":True, \\"task\\": task})\\n\\n    if path == \\"/resources\\":\\n        if not self._authorized():\\n            return\\n        return self._send(200,{\\"ok\\":True, \\"resources\\": resource_summary_safe()})\\n\\n    #M4: approval gates\\u2014list packets, optionally filtered.\\n    if path == \\"/gates\\":\\n        if not self._authorized():\\n            return\\n        status_filter = qs.get(\\"status\\", [None])[0]\\n        run_filter = qs.get(\\"run_id\\", [None])[0]\\n        if run_filter:\\n            packets = api.gate_store.list_for_run(run_filter)\\n        elif status_filter == \\"pending\\":\\n            packets = api.gate_store.pending()\\n        else:\\n            packets = api.gate_store.all()\\n        return self._send(\\n            200,\\n            {\\"ok\\":True,\\n             \\"gates\\": [pkt.model_dump(mode=\\"json\\") for pkt in packets],\\n             \\"count\\": len(packets),\\n            }\\n        )\\n\\n    if path.startswith(\\"/gates/\\") and len(path.split(\\"/\\")) == 3:\\n        if not self._authorized():\\n            return\\n        packet_id = path.split(\\"/\\")[2]\\n        packet = api.gate_store.get(packet_id)\\n        if packet is None:\\n            return self._send(404,{\\"ok\\":False, \\"error\\":\\"Gate not found\\"})\\n        return self._send(200,{\\"ok\\":True, \\"gate\\": packet.model_dump(mode=\\"json\\")})\\n\\n    return self._send(404,{\\"ok\\":False, \\"error\\":\\"Not Found\\"})"\n},\n"outputs":[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":[\n"The /health endpoint is implemented in /home/seeker/projects/FreeTalon/freetalon/api.py."\n],\n"criteria":[\n{"type":"file_exists",\n"path":"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n]\n},\n{"id":"step-2",\n"objective":"Write the tests for /health",\n"depends_on":[\n"step-1"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"cat > /home/seeker/freetalon-workspace/test_health.py <<\'EOF\'\\nimport json\\nimport sys\\nimport tempfile\\nimport unittest\\nimport urllib.request\\nfrom pathlib import Path\\n\\nsys.path.insert(0, \\"/home/seeker/projects/FreeTalon\\")\\n\\nfrom freetalon.api import HiveAPIServer\\nfrom freetalon.config import HiveConfig\\nfrom freetalon.hive import HiveController\\n\\n\\nclass RouteEndpointTests(unittest.TestCase):\\n    def setUp(self):\\n        self.tmpdir = tempfile.TemporaryDirectory()\\n        base = Path(self.tmpdir.name)\\n        config = HiveConfig(\\n            workspace=base,\\n            state_path=base / \\"state.json\\",\\n            audit_log_path=base / \\"audit.log\\",\\n            worker_cap=2,\\n            queue_multiplier=3,\\n            poll_interval_seconds=0.02,\\n        )\\n        self.controller = HiveController(config)\\n        self.controller.start()\\n        self.server = HiveAPIServer(self.controller, \\"test-token\\")\\n        self.server.start(\\"127.0.0.1\\", 0)\\n        port = self.server._httpd.server_address[1]\\n        self.base = f\\"http://127.0.0.1:{port}\\"\\n\\n    def tearDown(self):\\n        self.server.stop()\\n        self.controller.stop()\\n        self.tmpdir.cleanup()\\n\\n    def test_route_answers(self):\\n        with urllib.request.urlopen(f\\"{self.base}/health\\", timeout=3) as resp:\\n            status = resp.status\\n            data = json.loads(resp.read())\\n            self.assertEqual(status, 200)\\n            self.assertTrue(data[\\"ok\\"])\\n            self.assertIn(\\"status\\", data)\\n            self.assertIn(\\"service_status\\", data)\\n            self.assertIn(\\"ollama_reachable\\", data)\\n            self.assertIsInstance(data[\\"ollama_reachable\\"], bool)\\n            self.assertIn(\\"gpu_temperature\\", data)\\n            self.assertIsInstance(data[\\"gpu_temperature\\"], (int, float))\\n\\n\\nif __name__ == \\"__main__\\":\\nunittest.main()\\nEOF"\n},\n"outputs":[\n"/home/seeker/freetalon-workspace/test_health.py"\n],\n"acceptance":[\n"The tests for /health exist."\n],\n"criteria":[\n{"type":"file_exists",\n"path":"/home/seeker/freetalon-workspace/test_health.py"\n}\n]\n},\n{"id":"step-3",\n"objective":"Run the tests for /health",\n"depends_on":[\n"step-2"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"python3 /home/seeker/freetalon-workspace/test_health.py"\n},\n"outputs":[\n"test results"\n],\n"acceptance":[\n"The tests for /health pass."\n],\n"criteria":[\n{"type":"exit_code",\n"equals":0\n}\n]\n},\n{"id":"step-4",\n"objective":"Start the API server for /health",\n"depends_on":[\n"step-3"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && for i in $(seq 1 30); do curl -sf http://localhost:8765/health && break; sleep 1; done; curl -sf http://localhost:8765/health"\n},\n"outputs":[\n"API server running"\n],\n"acceptance":[\n"The API server answers /health."\n],\n"criteria":[\n{"type":"exit_code",\n"equals":0\n}\n]\n},\n{"id":"step-5",\n"objective":"Verify /health live",\n"depends_on":[\n"step-4"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"curl -s http://localhost:8765/health"\n},\n"outputs":[\n"verification"\n],\n"acceptance":[\n"/health returns its JSON live."\n],\n"criteria":[\n{"type":"exit_code",\n"equals":0\n}\n]\n}\n]\n}'

    def setUp(self) -> None:
        import tempfile

        self.repo_root = Path(__file__).resolve().parent.parent
        self.api_path = str(self.repo_root / "freetalon" / "api.py")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.module_path = str(self.tmp_root / "fixture_module.py")
        Path(self.module_path).write_text(self.MODULE_SOURCE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _edit_node(
        self,
        target: str,
        anchor: object,
        position: object,
        content: object,
        node_id: str = "step-1",
    ) -> dict:
        return {
            "id": node_id,
            "objective": "Edit the module in place",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": target,
                "anchor": anchor,
                "position": position,
                "content": content,
            },
            "outputs": [target],
            "acceptance": ["The module is edited."],
            "criteria": [{"type": "file_exists", "path": target}],
        }

    def _rejection(self, raw: str, **kwargs: object) -> str:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, **kwargs)
        return str(caught.exception)

    def _content(self, target: str, anchor: str, position: str, delta: str) -> str:
        nodes, _meta = parse_planner_response(
            self._dag([self._edit_node(target, anchor, position, delta)])
        )
        return nodes[0].inputs["content"]

    # --- Re-indentation: replace ---

    def test_replace_uniform_dedent_byte_matches_correct_splice(self) -> None:
        dedented = self._content(
            self.module_path,
            "Server.start.Handler.do_GET",
            "replace",
            'def do_GET(self):\n    return "ok-extended"\n',
        )
        correct = self._content(
            self.module_path,
            "Server.start.Handler.do_GET",
            "replace",
            '            def do_GET(self):\n'
            '                return "ok-extended"\n',
        )
        self.assertEqual(dedented, correct)
        self.assertIn(
            '            def do_GET(self):\n'
            '                return "ok-extended"\n',
            dedented,
        )

    def test_replace_uniform_overindent_absorbed(self) -> None:
        overindented = self._content(
            self.module_path,
            "Server.start.Handler.do_GET",
            "replace",
            '                    def do_GET(self):\n'
            '                        return "ok-extended"\n',
        )
        correct = self._content(
            self.module_path,
            "Server.start.Handler.do_GET",
            "replace",
            '            def do_GET(self):\n'
            '                return "ok-extended"\n',
        )
        self.assertEqual(overindented, correct)

    def test_replace_nested_anchor_reindented(self) -> None:
        from freetalon.orchestrator.repo_index import qualified_def_names

        content = self._content(
            self.module_path,
            "Server.start",
            "replace",
            "def start(self):\n"
            "    class Handler:\n"
            "        def do_GET(self):\n"
            '            return "ok-2"\n'
            "\n"
            "        def do_POST(self):\n"
            '            return "posted"\n'
            "\n"
            "    return Handler\n",
        )
        self.assertIn('                return "ok-2"', content)
        self.assertEqual(
            qualified_def_names(content),
            qualified_def_names(self.MODULE_SOURCE),
        )

    def test_replace_decorated_multiline_definition_reindented(self) -> None:
        from freetalon.orchestrator.repo_index import qualified_def_names

        content = self._content(
            self.module_path,
            "greet",
            "replace",
            "    @_wrap\n"
            "    def greet(\n"
            "        name: str,\n"
            "    ) -> str:\n"
            '        return "hi " + name\n',
        )
        self.assertIn('"hi " + name', content)
        self.assertNotIn('"hello " + name', content)
        self.assertEqual(
            qualified_def_names(content),
            qualified_def_names(self.MODULE_SOURCE),
        )

    # --- Re-indentation: what stays rejected ---

    def test_non_uniform_delta_still_rejected(self) -> None:
        # The def is dedented to column 0 but a body line sits at
        # 2 spaces — below the def's own body indent, above the
        # base: no single shift describes this delta.
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        'def do_GET(self):\n    return "ok"\n  x = 1\n',
                    )
                ]
            )
        )
        self.assertIn("starts at 0 spaces of indentation", message)
        self.assertIn("sits at 12 spaces in the file", message)
        self.assertNotIn("placeholder content only", message)

    def test_incoherent_uniform_delta_still_rejected_at_the_gate(self) -> None:
        # A uniformly shifted FRAGMENT is not a uniform copy of
        # the anchored definition: the gate fires (before the
        # replace shape check, as it always has) rather than the
        # shape error.
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "replace",
                        "if True:\n    return 1\n",
                    )
                ]
            )
        )
        self.assertIn("starts at 0 spaces of indentation", message)
        self.assertIn("sits at 12 spaces in the file", message)
        self.assertNotIn("defines no definition at its top level", message)

    # --- Re-indentation: after/before ---

    def test_after_uniform_dedent_absorbed(self) -> None:
        from freetalon.orchestrator.repo_index import qualified_def_names

        content = self._content(
            self.module_path,
            "Server.start.Handler.do_GET",
            "after",
            'def do_PUT(self):\n    return "put"\n',
        )
        expected = qualified_def_names(self.MODULE_SOURCE)
        expected.insert(
            expected.index("Server.start.Handler.do_GET") + 1,
            "Server.start.Handler.do_PUT",
        )
        self.assertEqual(qualified_def_names(content), expected)
        self.assertLess(content.index("def do_GET"), content.index("def do_PUT"))
        self.assertLess(content.index("def do_PUT"), content.index("def do_POST"))

    def test_before_uniform_overindent_absorbed(self) -> None:
        content = self._content(
            self.module_path,
            "greet",
            "before",
            '    def farewell(name: str) -> str:\n'
            '        return "bye " + name\n',
        )
        self.assertIn("farewell", content)
        # Inserted before the anchor's DECORATOR line, at the
        # anchor's own (zero) indentation.
        self.assertLess(content.index("def farewell"), content.index("@_wrap"))
        self.assertLess(content.index("@_wrap"), content.index("def greet"))

    def test_after_incoherent_delta_still_rejected_at_the_gate(self) -> None:
        message = self._rejection(
            self._dag(
                [
                    self._edit_node(
                        self.module_path,
                        "Server.start.Handler.do_GET",
                        "after",
                        "def broken(:\n    pass\n",
                    )
                ]
            )
        )
        self.assertIn("starts at 0 spaces of indentation", message)
        self.assertIn("sits at 12 spaces in the file", message)

    # --- Retry-#32 output 3 fixture ---

    def _step1_only_raw(self) -> str:
        import json

        payload = json.loads(self.RETRY32_OUTPUT3_RAW)
        return json.dumps({"nodes": payload["nodes"][:1], "metadata": {}})

    def test_retry32_output3_step1_materializes(self) -> None:
        import ast

        from freetalon.orchestrator import planner
        from freetalon.orchestrator.repo_index import (
            build_repo_index,
            qualified_def_names,
        )
        from freetalon.orchestrator.tool_registry import (
            is_placeholder_content,
        )

        api_source = Path(self.api_path).read_text(encoding="utf-8")
        for root in (None, str(self.repo_root)):
            nodes, _meta = parse_planner_response(
                self._step1_only_raw(),
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            content = nodes[0].inputs["content"]
            # Materialized at the anchor's file indentation.
            self.assertIn("            def do_GET(self) -> None:", content)
            # Parses, and every one of api.py's definitions survives.
            ast.parse(content)
            self.assertEqual(
                qualified_def_names(content), qualified_def_names(api_source)
            )
            # The sketch's '...' payload values survive verbatim
            # into the materialized module — by design no planning
            # check judges them; the plan's own tests assert the
            # fields' types (which Ellipsis fails), so substance is
            # judged at execution by the judge/replan loop.
            self.assertIn('payload["service_status"] = ...', content)
            self.assertIn('payload["ollama_reachable"] = ...', content)
            self.assertIn('payload["gpu_temperature"] = ...', content)
            # The content checks the audit measured, run directly:
            # overwrite guard, placeholder/stub rules, framework
            # guard — all silent on the materialized module.
            self.assertEqual(planner._scaffold_overwrite_errors(nodes, root), [])
            self.assertEqual(
                planner._placeholder_scaffold_errors(nodes, root), []
            )
            self.assertEqual(
                planner._framework_errors(
                    nodes, build_repo_index(root), self.HEALTH_GOAL
                ),
                [],
            )
            self.assertFalse(is_placeholder_content(content))
            self.assertFalse(planner._is_placeholder_stub(content))

    def test_retry32_output3_full_plan_profile(self) -> None:
        # Planner cycle 2 (payload channel) — this pin flips, and
        # the flip is the point. The pin used to encode the CP2
        # binding defect exactly: step-1 materializes, the full
        # plan reaches the ordinary checks, and they reject it with
        # exactly ONE error, on step-2 — its test heredoc does not
        # parse (unittest.main() at column 0 under the __main__
        # guard). Cycle 2 closes that defect class: the harness
        # repairs the payload and reroutes the write to the
        # scaffold form at materialization (see TestPlannerCycle2),
        # so the plan — the real 5-node plan — is now ACCEPTED,
        # with step-2 in scaffold form carrying the repaired
        # payload. The same flip is pinned on the CP2 trace itself
        # in TestPlannerABRefactor /
        # TestPlannerCycle2.test_cp2_output3_accepted_via_payload_channel.
        import ast

        for root in (None, str(self.repo_root)):
            nodes, _meta = parse_planner_response(
                self.RETRY32_OUTPUT3_RAW,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
                repo_root=root,
            )
            self.assertEqual(len(nodes), 5)
            test_write = next(
                node
                for node in nodes
                if (node.inputs.get("file_path") or "").endswith(
                    "test_health.py"
                )
            )
            self.assertEqual(test_write.assigned_claw, "scaffold")
            content = test_write.inputs["content"]
            self.assertIn("\n    unittest.main()\n", content)
            ast.parse(content)

    # --- Repair steering for materialization rejections ---

    def _non_uniform_raw(self) -> str:
        return self._dag(
            [
                self._edit_node(
                    self.module_path,
                    "Server.start.Handler.do_GET",
                    "replace",
                    'def do_GET(self):\n    return "ok"\n  x = 1\n',
                )
            ]
        )

    def test_steering_blocks_on_materialization_rejection(self) -> None:
        from freetalon.orchestrator.planner import (
            _extension_steering_block,
            _materialization_steering_block,
            _shape_steering_block,
        )

        raw = self._non_uniform_raw()
        message = self._rejection(raw)
        codes = _rejection_codes(raw)
        self.assertIn("spaces of indentation", message)
        # The retry-#28 misfire, one level down, is closed: the
        # shape block no longer tells the repair round none of its
        # code was rejected.
        self.assertEqual(_shape_steering_block(raw, codes), "")
        # The extension block stays guard-only, as before.
        self.assertEqual(_extension_steering_block(raw, codes, None), "")
        # The materialization block fires with the concrete
        # pattern: the module's full definition list, the anchor's
        # own indentation, and the module's skeleton.
        block = _materialization_steering_block(raw, codes, None)
        self.assertIn("ANCHORED-EDIT REPAIR", block)
        self.assertIn("KEEP the anchored-edit form", block)
        self.assertIn("The module's full definition list is:", block)
        self.assertIn("Server.start.Handler.do_GET", block)
        self.assertIn("12 spaces for 'Server.start.Handler.do_GET'", block)
        self.assertIn("def do_GET", block)

    def test_materialization_block_silent_off_its_trigger(self) -> None:
        from freetalon.orchestrator.planner import (
            CheapRejectionError,
            _materialization_steering_block,
        )

        raw = self._non_uniform_raw()
        # An overwrite-guard rejection carries no materialization
        # code: the extension block's territory, not this one's.
        # (A/B refactor: this case used to hand the block a
        # hand-written guard message; it now hands it the guard's
        # code set, which is what the trigger reads.)
        guard_codes = {"scaffold_overwrite"}
        self.assertEqual(
            _materialization_steering_block(raw, guard_codes, None), ""
        )
        # The placeholder-delta cheap rejection DOES carry the
        # materialization-adjacent code family but is excluded by
        # its own code: its repair round already answers with the
        # grounded skeleton.
        placeholder_raw = self._dag(
            [
                self._edit_node(
                    self.api_path,
                    "HiveAPIServer.start.Handler.do_GET",
                    "replace",
                    TestPlannerDeltaBlankFix.OLD_DELTA_BLANK,
                )
            ]
        )
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                placeholder_raw,
                goal_text=self.HEALTH_GOAL,
                workspace=self.WS,
            )
        message = str(caught.exception)
        self.assertIn("placeholder content only", message)
        codes = {record.code for record in caught.exception.plan_errors}
        self.assertEqual(
            _materialization_steering_block(placeholder_raw, codes, None),
            "",
        )

    def test_materialization_repair_prompt_carries_the_block(self) -> None:
        from freetalon.orchestrator.planner import plan_task_intent

        intent = TaskIntent(
            goal="Extend the fixture module in place", project_type="api"
        )
        settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        good = self._dag(
            [
                self._edit_node(
                    self.module_path,
                    "Server.start.Handler.do_GET",
                    "replace",
                    '            def do_GET(self):\n'
                    '                return "ok-extended"\n',
                )
            ]
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[self._non_uniform_raw(), good],
        ) as call_mock:
            plan = plan_task_intent(intent, settings=settings)
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(plan.nodes), 1)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("REJECTED", repair_prompt)
        self.assertIn("ANCHORED-EDIT REPAIR", repair_prompt)
        self.assertIn("The module's full definition list is:", repair_prompt)
        # The misfiring shape-steering text is gone.
        self.assertNotIn("SHAPE was right", repair_prompt)
        self.assertNotIn("none of its code was rejected", repair_prompt)


class TestPlannerABRefactor(unittest.TestCase):
    """Planner A/B refactor: structured PlanError records on every
    rejection, repair steering keyed off codes, and the three
    retry-#33 defect closures — (1) the elision hole (a full-content
    scaffold of an existing module faces definition-retention
    judgment, and elision markers are a named rejection), (3) the
    heredoc Python parse check as a first-class validator including
    tee writes. (Defect 2, the suite-node timeout, is executor-side
    and pinned in tests/test_executor.py.)
    """

    GOAL = "Extend the fixture module in place"

    MODULE_SOURCE = (
        "VALUE = 1\n"
        "\n"
        "\n"
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
        "\n"
        "\n"
        "def gamma():\n"
        "    return 3\n"
        "\n"
        "\n"
        "def delta():\n"
        "    return 4\n"
        "\n"
        "\n"
        "def epsilon():\n"
        "    return 5\n"
        "\n"
        "\n"
        "def zeta():\n"
        "    return 6\n"
    )

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.module_path = str(self.tmp_root / "fixture_module.py")
        Path(self.module_path).write_text(self.MODULE_SOURCE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    def _scaffold_node(self, content: str, node_id: str = "step-1") -> dict:
        return {
            "id": node_id,
            "objective": "Rewrite the fixture module",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": self.module_path, "content": content},
            "outputs": [self.module_path],
            "acceptance": ["The module is rewritten."],
            "criteria": [{"type": "file_exists", "path": self.module_path}],
        }

    def _caught(self, raw: str) -> LLMResponseError:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, goal_text=self.GOAL)
        return caught.exception

    # --- Defect 1: the retention judgment has no fragment floor ---

    def test_fragment_scaffold_faces_retention_guard(self) -> None:
        # The retry-#28/#29 shape: a branch fragment (zero definitions)
        # offered as the full content of an existing six-definition
        # module. Before the refactor the guard skipped any scaffold
        # with fewer than four definitions of its own, so this passed.
        raw = self._dag(
            [self._scaffold_node("if VALUE:\n    print('health branch')\n")]
        )
        exc = self._caught(raw)
        self.assertIn(
            "drops 6 of the existing module's 6 definitions", str(exc)
        )
        self.assertEqual(
            {record.code for record in exc.plan_errors}, {"scaffold_overwrite"}
        )

    def test_elision_marker_rejected_even_when_definitions_retained(self) -> None:
        # Every definition is present and the content parses — only
        # the elision comment stands in for code. Retention alone
        # cannot see it; the elision rule names it.
        content = self.MODULE_SOURCE.replace(
            "def gamma():\n    return 3\n",
            "def gamma():\n    # ... existing code ...\n    return 3\n",
        )
        exc = self._caught(self._dag([self._scaffold_node(content)]))
        self.assertIn("elides the existing module", str(exc))
        self.assertEqual(
            {record.code for record in exc.plan_errors}, {"elision"}
        )

    def test_elision_owns_its_rejection_when_definitions_also_drop(self) -> None:
        # The retry-#33 output-4 shape: a fragment carrying elision
        # markers that also drops definitions earns exactly ONE
        # error — the elision rejection (the guard defers to it).
        content = (
            "def alpha():\n"
            "    # ... existing code ...\n"
            "    return 1\n"
        )
        exc = self._caught(self._dag([self._scaffold_node(content)]))
        self.assertEqual(
            {record.code for record in exc.plan_errors}, {"elision"}
        )
        self.assertTrue(str(exc).startswith("Planner output was rejected with 1 error"))

    # --- Structured records: code + stage + node id per rejection ---

    def test_records_carry_code_stage_and_node_id(self) -> None:
        raw = self._dag(
            [self._scaffold_node("if VALUE:\n    print('health branch')\n")]
        )
        exc = self._caught(raw)
        (record,) = exc.plan_errors
        self.assertEqual(record.code, "scaffold_overwrite")
        self.assertEqual(record.stage, "validate")
        self.assertEqual(record.node_id, "step-1")
        # The record's message is the validator's own text, unchanged.
        self.assertIn(record.message, str(exc))

    def test_schema_stage_records(self) -> None:
        node = self._scaffold_node("VALUE = 2\n")
        raw = self._dag([node, dict(node)])
        exc = self._caught(raw)
        codes = {record.code for record in exc.plan_errors}
        self.assertIn("duplicate_node_ids", codes)
        for record in exc.plan_errors:
            if record.code == "duplicate_node_ids":
                self.assertEqual(record.stage, "schema")

    def test_materialize_stage_records(self) -> None:
        node = self._scaffold_node("return 1\n")
        node["inputs"]["anchor"] = "NoSuchDefinition"
        node["inputs"]["position"] = "replace"
        exc = self._caught(self._dag([node]))
        self.assertTrue(exc.plan_errors)
        for record in exc.plan_errors:
            self.assertEqual(record.stage, "materialize")
        self.assertIn(
            "anchored_edit", {record.code for record in exc.plan_errors}
        )

    # --- Defect 3: heredoc payloads are parse-checked, tee included ---

    BROKEN_PAYLOAD = (
        "import unittest\n"
        "\n"
        "\n"
        "class TestHealth(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "unittest.main()\n"
    )

    def _bash_node(self, command: str) -> dict:
        return {
            "id": "step-1",
            "objective": "Write the test file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": command},
            "outputs": [],
            "acceptance": ["The test file is written."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }

    def test_heredoc_main_guard_defect_is_materialized_to_scaffold(self) -> None:
        # PIN CHANGED by planner cycle 2 (steal #1b, the payload
        # channel): this test used to pin the retry-#33 output-3
        # shape — the payload's unindented unittest.main() under the
        # __main__ guard — as a one-error heredoc_parse rejection.
        # That rejection WAS the CP2 binding defect: the model's real
        # plan died on one indentation slip inside a shell string.
        # The materialize stage now owns the payload: a sole plain
        # heredoc write of a .py file whose payload is repaired by
        # the deterministic indented-block normalization (verified by
        # re-parsing) is rerouted to the scaffold form and accepted,
        # and the scaffold validators judge the content. The tee
        # shapes in the sibling test below are NOT this shape and
        # keep their heredoc_parse rejection; an unrepairable payload
        # keeps it too (TestPlannerCycle2).
        target = str(self.tmp_root / "test_health.py")
        command = (
            f"cat > {target} <<'PYEOF'\n" + self.BROKEN_PAYLOAD + "PYEOF"
        )
        nodes, _meta = parse_planner_response(
            self._dag([self._bash_node(command)]), goal_text=self.GOAL
        )
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node.assigned_claw, "scaffold")
        self.assertEqual(node.inputs.get("file_path"), target)
        content = node.inputs.get("content") or ""
        self.assertIn("    unittest.main()\n", content)
        import ast

        ast.parse(content)

    def test_tee_heredoc_payloads_are_parse_checked(self) -> None:
        # Both tee shapes were invisible to the payload check before
        # the refactor: no 'cat > target' head exists for either.
        target = str(self.tmp_root / "test_health.py")
        for command in (
            f"cat <<'PYEOF' | tee {target}\n" + self.BROKEN_PAYLOAD + "PYEOF",
            f"tee {target} <<'PYEOF'\n" + self.BROKEN_PAYLOAD + "PYEOF",
        ):
            exc = self._caught(self._dag([self._bash_node(command)]))
            self.assertIn(
                "heredoc_parse",
                {record.code for record in exc.plan_errors},
                command.split("\n", 1)[0],
            )

    def test_valid_tee_heredoc_payload_accepted(self) -> None:
        target = str(self.tmp_root / "test_health.py")
        payload = self.BROKEN_PAYLOAD.replace(
            "unittest.main()\n", "    unittest.main()\n"
        )
        command = f"cat <<'PYEOF' | tee {target}\n" + payload + "PYEOF"
        nodes, _meta = parse_planner_response(
            self._dag([self._bash_node(command)]), goal_text=self.GOAL
        )
        self.assertEqual(len(nodes), 1)


class TestPlannerCycle2(unittest.TestCase):
    """Planner cycle 2 (2026-11-17): the prior-art steal list built
    as solutions inside FreeTalon's design — (1) the plan envelope
    grammar-constrained at decode time (Ollama format schema) plus
    the payload channel: a sole heredoc .py write whose payload is
    deterministically repairable is rerouted to the scaffold form
    at materialization; (5) repair discipline: stuck detection
    (the same structured rejection three times in a row escalates
    to re-planning), the task restated in every repair prompt
    (re-grounding), and the scope lock (a repair may not widen the
    rejected plan's file/dependency scope); (7) formatter plumbing:
    an optional second pass that re-emits a response under the
    envelope schema, endpoint/model configurable by env vars.
    """

    GOAL = (
        "Add a GET /health endpoint to FreeTalon's API that returns JSON "
        "reporting the service status, whether Ollama at localhost:11434 "
        "is reachable, and the GPU temperature when nvidia-smi is "
        "available. Add unit tests for the endpoint, keep the full test "
        "suite green, and verify the endpoint live."
    )

    # The CP2 live run's output 3, byte-exact from
    # logs/cp2-run1.out (md5 a6bf39ec4d62859086e482a3eb82d09d): the
    # real 5-node plan whose only defect was step-2's heredoc
    # payload dedenting unittest.main() out of its __main__ guard.
    CP2_OUTPUT_3 = '{\n"nodes":[\n{"id":"step-1",\n"objective":"Extend /home/seeker/projects/FreeTalon/freetalon/api.py in place\\u2014add a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)",\n"depends_on":[],\n"assigned_claw":"scaffold",\n"inputs":{\n"file_path":"/home/seeker/projects/FreeTalon/freetalon/api.py",\n"anchor":"HiveAPIServer.start.Handler.do_GET",\n"position":"replace",\n"content":"\\ndef do_GET(self) -> None:\\n    parsed = urlparse(self.path)\\n    path = parsed.path\\n    qs = parse_qs(parsed.query)\\n\\n    if path == \\"/health\\":\\n        payload ={\\"ok\\":True, \\"status\\":\\"ready\\"}\\n        payload[\\"service_status\\"] = ...\\n        payload[\\"ollama_reachable\\"] = ...\\n        payload[\\"gpu_temperature\\"] = ...\\n        return self._send(200, payload)\\n\\n    if path == \\"/status\\":\\n        if not self._authorized():\\n            return\\n        return self._send(200,{\\"ok\\":True, \\"status\\":api.controller.status()})\\n\\n    if path == \\"/metrics\\":\\n        if not self._authorized():\\n            return\\n        status = api.controller.status()\\n        metrics ={\\n            \\"workers_healthy\\": status[\\"workers\\"][\\"healthy\\"],\\n            \\"workers_configured\\": status[\\"workers\\"][\\"configured\\"],\\n            \\"tasks_running\\": status[\\"queue\\"][\\"running\\"],\\n            \\"tasks_queued\\": status[\\"queue\\"][\\"queued\\"],\\n            \\"queue_max\\": status[\\"queue\\"][\\"max\\"]\\n        }\\n        return self._send(200,{\\"ok\\":True, \\"metrics\\": metrics})\\n\\n    if path.startswith(\\"/tasks/\\") and len(path.split(\\"/\\")) == 3:\\n        if not self._authorized():\\n            return\\n        task_id = path.split(\\"/\\")[2]\\n        task = api.controller.get_task(task_id)\\n        if task is None:\\n            return self._send(404,{\\"ok\\":False, \\"error\\":\\"Task not found\\"})\\n        return self._send(200,{\\"ok\\":True, \\"task\\": task})\\n\\n    if path == \\"/resources\\":\\n        if not self._authorized():\\n            return\\n        return self._send(200,{\\"ok\\":True, \\"resources\\": resource_summary_safe()})\\n\\n    #M4: approval gates\\u2014list packets, optionally filtered.\\n    if path == \\"/gates\\":\\n        if not self._authorized():\\n            return\\n        status_filter = qs.get(\\"status\\", [None])[0]\\n        run_filter = qs.get(\\"run_id\\", [None])[0]\\n        if run_filter:\\n            packets = api.gate_store.list_for_run(run_filter)\\n        elif status_filter == \\"pending\\":\\n            packets = api.gate_store.pending()\\n        else:\\n            packets = api.gate_store.all()\\n        return self._send(\\n            200,\\n            {\\"ok\\":True,\\n             \\"gates\\": [pkt.model_dump(mode=\\"json\\") for pkt in packets],\\n             \\"count\\": len(packets),\\n            }\\n        )\\n\\n    if path.startswith(\\"/gates/\\") and len(path.split(\\"/\\")) == 3:\\n        if not self._authorized():\\n            return\\n        packet_id = path.split(\\"/\\")[2]\\n        packet = api.gate_store.get(packet_id)\\n        if packet is None:\\n            return self._send(404,{\\"ok\\":False, \\"error\\":\\"Gate not found\\"})\\n        return self._send(200,{\\"ok\\":True, \\"gate\\": packet.model_dump(mode=\\"json\\")})\\n\\n    return self._send(404,{\\"ok\\":False, \\"error\\":\\"Not Found\\"})"\n},\n"outputs":[\n"/home/seeker/projects/FreeTalon/freetalon/api.py"\n],\n"acceptance":[\n"The /health endpoint is implemented in /home/seeker/projects/FreeTalon/freetalon/api.py."\n],\n"criteria":[\n{"type":"file_exists",\n"path":"/home/seeker/projects/FreeTalon/freetalon/api.py"\n}\n]\n},\n{"id":"step-2",\n"objective":"Write the tests for /health",\n"depends_on":[\n"step-1"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"cat > /home/seeker/freetalon-workspace/test_health.py <<\'EOF\'\\nimport json\\nimport sys\\nimport tempfile\\nimport unittest\\nimport urllib.request\\nfrom pathlib import Path\\n\\nsys.path.insert(0, \\"/home/seeker/projects/FreeTalon\\")\\n\\nfrom freetalon.api import HiveAPIServer\\nfrom freetalon.config import HiveConfig\\nfrom freetalon.hive import HiveController\\n\\n\\nclass RouteEndpointTests(unittest.TestCase):\\n    def setUp(self):\\n        self.tmpdir = tempfile.TemporaryDirectory()\\n        base = Path(self.tmpdir.name)\\n        config = HiveConfig(\\n            workspace=base,\\n            state_path=base / \\"state.json\\",\\n            audit_log_path=base / \\"audit.log\\",\\n            worker_cap=2,\\n            queue_multiplier=3,\\n            poll_interval_seconds=0.02,\\n        )\\n        self.controller = HiveController(config)\\n        self.controller.start()\\n        self.server = HiveAPIServer(self.controller, \\"test-token\\")\\n        self.server.start(\\"127.0.0.1\\", 0)\\n        port = self.server._httpd.server_address[1]\\n        self.base = f\\"http://127.0.0.1:{port}\\"\\n\\n    def tearDown(self):\\n        self.server.stop()\\n        self.controller.stop()\\n        self.tmpdir.cleanup()\\n\\n    def test_route_answers(self):\\n        with urllib.request.urlopen(f\\"{self.base}/health\\", timeout=3) as resp:\\n            status = resp.status\\n            data = json.loads(resp.read())\\n            self.assertEqual(status, 200)\\n            self.assertTrue(data[\\"ok\\"])\\n            self.assertIn(\\"status\\", data)\\n            self.assertIn(\\"service_status\\", data)\\n            self.assertIn(\\"ollama_reachable\\", data)\\n            self.assertIsInstance(data[\\"ollama_reachable\\"], bool)\\n            self.assertIn(\\"gpu_temperature\\", data)\\n            self.assertIsInstance(data[\\"gpu_temperature\\"], (int, float))\\n\\n\\nif __name__ == \\"__main__\\":\\nunittest.main()\\nEOF"\n},\n"outputs":[\n"/home/seeker/freetalon-workspace/test_health.py"\n],\n"acceptance":[\n"The tests for /health exist."\n],\n"criteria":[\n{"type":"file_exists",\n"path":"/home/seeker/freetalon-workspace/test_health.py"\n}\n]\n},\n{"id":"step-3",\n"objective":"Run the tests for /health",\n"depends_on":[\n"step-2"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"python3 /home/seeker/freetalon-workspace/test_health.py"\n},\n"outputs":[\n"test results"\n],\n"acceptance":[\n"The tests for /health pass."\n],\n"criteria":[\n{"type":"exit_code",\n"equals":0\n}\n]\n},\n{"id":"step-4",\n"objective":"Start the API server for /health",\n"depends_on":[\n"step-3"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m freetalon.cli stop; ./venv/bin/python -m freetalon.cli start && for i in $(seq 1 30); do curl -sf http://localhost:8765/health && break; sleep 1; done; curl -sf http://localhost:8765/health"\n},\n"outputs":[\n"API server running"\n],\n"acceptance":[\n"The API server answers /health."\n],\n"criteria":[\n{"type":"exit_code",\n"equals":0\n}\n]\n},\n{"id":"step-5",\n"objective":"Verify /health live",\n"depends_on":[\n"step-4"\n],\n"assigned_claw":"bash",\n"inputs":{\n"command":"curl -s http://localhost:8765/health"\n},\n"outputs":[\n"verification"\n],\n"acceptance":[\n"/health returns its JSON live."\n],\n"criteria":[\n{"type":"exit_code",\n"equals":0\n}\n]\n}\n]\n}'

    BROKEN_PAYLOAD = (
        "import unittest\n"
        "\n"
        "\n"
        "class TestHealth(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "unittest.main()\n"
    )

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _dag(nodes: list[dict]) -> str:
        import json

        return json.dumps({"nodes": nodes, "metadata": {}})

    @staticmethod
    def _settings(**kwargs) -> LLMSettings:
        return LLMSettings(
            backend="ollama",
            base_url="http://localhost:11434",
            model="test",
            **kwargs,
        )

    def _bash_node(self, command: str, node_id: str = "step-1") -> dict:
        return {
            "id": node_id,
            "objective": "Write the test file",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": command},
            "outputs": [],
            "acceptance": ["The step is done."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }

    def _scaffold_node(
        self,
        path: str,
        content: str = "VALUE = 1\n",
        node_id: str = "step-1",
        with_criteria: bool = True,
    ) -> dict:
        node = {
            "id": node_id,
            "objective": "Write the file",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {"file_path": path, "content": content},
            "outputs": [path],
            "acceptance": ["The file is written."],
        }
        if with_criteria:
            node["criteria"] = [{"type": "file_exists", "path": path}]
        return node

    def _caught(self, raw: str) -> LLMResponseError:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(raw, goal_text="Extend the fixture module in place")
        return caught.exception

    # --- (1) The grammar-constrained envelope ---

    def test_envelope_schema_matches_plan_contract(self) -> None:
        from freetalon.orchestrator import planner as planner_module
        from freetalon.orchestrator.models import PlanNode
        from freetalon.orchestrator.tool_registry import KNOWN_CLAW_NAMES

        schema = planner_module.plan_envelope_schema()
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["nodes"])
        node_schema = schema["properties"]["nodes"]["items"]
        self.assertEqual(node_schema["required"], ["id", "objective"])
        properties = node_schema["properties"]
        # Every field the schema names is a real PlanNode field, and
        # the claw enum is exactly the registry (plus PlanNode's ""
        # default, whose misuse the claw validator names).
        self.assertLessEqual(set(properties), set(PlanNode.model_fields))
        self.assertEqual(
            set(properties["assigned_claw"]["enum"]),
            set(KNOWN_CLAW_NAMES) | {""},
        )
        # Payloads stay free-form: inputs and criteria are semantic,
        # and the validators own their shape.
        self.assertEqual(properties["inputs"], {"type": "object"})
        self.assertEqual(
            properties["criteria"],
            {"type": "array", "items": {"type": "object"}},
        )

    def test_planner_call_constrains_the_envelope(self) -> None:
        from freetalon.orchestrator import planner as planner_module

        captured: dict = {}

        def fake_call(system, user, settings=None):
            captured["settings"] = settings
            return self._dag([self._bash_node("ls inputs")])

        intent = TaskIntent(goal="Deploy a service", project_type="ops")
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            plan_task_intent(intent, settings=self._settings())
        # The Ollama path carries the envelope schema (and the
        # planner context window, as before).
        self.assertEqual(
            captured["settings"].format_schema,
            planner_module.plan_envelope_schema(),
        )
        self.assertEqual(
            captured["settings"].num_ctx, planner_module._PLANNER_NUM_CTX
        )

        # A caller-supplied schema is respected, never overridden.
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            plan_task_intent(
                intent,
                settings=self._settings(format_schema={"type": "object"}),
            )
        self.assertEqual(captured["settings"].format_schema, {"type": "object"})

        # The openai-compatible path takes no Ollama format schema.
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            plan_task_intent(
                intent,
                settings=LLMSettings(
                    backend="openai_compatible",
                    base_url="http://localhost:8000/v1",
                    model="test",
                ),
            )
        self.assertIsNone(captured["settings"].format_schema)

    # --- (1b) The payload channel ---

    def test_cp2_output3_accepted_via_payload_channel(self) -> None:
        # The dedicated CP2 fixture (byte-exact, above): before
        # cycle 2 this output was rejected with exactly one error —
        # heredoc_parse on step-2 — and the live run died at
        # planning. The plan is the real 5-node plan; it must now be
        # accepted, with step-2 rerouted to the scaffold form
        # carrying the repaired payload, and both written modules
        # parsing as Python.
        import ast

        nodes, _meta = parse_planner_response(
            self.CP2_OUTPUT_3,
            goal_text=self.GOAL,
            workspace="/home/seeker/freetalon-workspace",
        )
        self.assertEqual(len(nodes), 5)
        test_write = next(
            node
            for node in nodes
            if (node.inputs.get("file_path") or "").endswith("test_health.py")
        )
        self.assertEqual(test_write.assigned_claw, "scaffold")
        content = test_write.inputs["content"]
        self.assertIn("\n    unittest.main()\n", content)
        ast.parse(content)
        api_write = next(
            node
            for node in nodes
            if (node.inputs.get("file_path") or "").endswith("freetalon/api.py")
        )
        ast.parse(api_write.inputs["content"])

    def test_valid_heredoc_write_stays_bash(self) -> None:
        # Nothing that works today changes shape: a sole heredoc
        # write whose payload already parses keeps the bash form
        # and its exact command.
        target = str(self.tmp_root / "test_ok.py")
        payload = (
            "VALUE = 1\n"
            "\n"
            "\n"
            "def main():\n"
            "    return VALUE\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        command = f"cat > {target} <<'PYEOF'\n{payload}PYEOF"
        nodes, _meta = parse_planner_response(
            self._dag([self._bash_node(command)]),
            goal_text="Extend the fixture module in place",
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].assigned_claw, "bash")
        self.assertEqual(nodes[0].inputs.get("command"), command)

    def test_unrepairable_heredoc_payload_still_rejected(self) -> None:
        # A payload no deterministic normalization repairs keeps the
        # heredoc_parse rejection, exactly one error, as before.
        target = str(self.tmp_root / "test_broken.py")
        command = f"cat > {target} <<'PYEOF'\ndef broken(:\n    pass\nPYEOF"
        exc = self._caught(self._dag([self._bash_node(command)]))
        self.assertEqual(
            {record.code for record in exc.plan_errors}, {"heredoc_parse"}
        )

    def test_uniformly_indented_payload_is_repaired_and_converted(self) -> None:
        # The whole payload copied one indent level deep: the
        # uniform-dedent normalization (the anchored-edit delta
        # rule, applied to a whole payload) repairs it, and the
        # write is rerouted to the scaffold form.
        target = str(self.tmp_root / "test_shifted.py")
        payload = "    VALUE = 1\n\n    def main():\n        return VALUE\n"
        command = f"cat > {target} <<'PYEOF'\n{payload}PYEOF"
        nodes, _meta = parse_planner_response(
            self._dag([self._bash_node(command)]),
            goal_text="Extend the fixture module in place",
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].assigned_claw, "scaffold")
        self.assertEqual(
            nodes[0].inputs.get("content"),
            "VALUE = 1\n\ndef main():\n    return VALUE\n",
        )

    def test_chained_command_heredoc_is_not_converted(self) -> None:
        # The write is not the node's only effect (a py_compile runs
        # after the marker), so the payload channel does not touch
        # it and the broken payload is the validator's, as before.
        target = str(self.tmp_root / "test_health.py")
        command = (
            f"cat > {target} <<'PYEOF'\n"
            + self.BROKEN_PAYLOAD
            + f"PYEOF\npython3 -m py_compile {target}"
        )
        exc = self._caught(self._dag([self._bash_node(command)]))
        self.assertIn(
            "heredoc_parse", {record.code for record in exc.plan_errors}
        )

    def test_append_heredoc_is_not_converted(self) -> None:
        target = str(self.tmp_root / "test_health.py")
        command = (
            f"cat >> {target} <<'PYEOF'\n" + self.BROKEN_PAYLOAD + "PYEOF"
        )
        exc = self._caught(self._dag([self._bash_node(command)]))
        self.assertIn(
            "heredoc_parse", {record.code for record in exc.plan_errors}
        )

    def test_sole_heredoc_write_shapes(self) -> None:
        from freetalon.orchestrator import planner as planner_module

        target = str(self.tmp_root / "test_health.py")
        sole = f"cat > {target} <<'PYEOF'\nVALUE = 1\nPYEOF"
        self.assertEqual(
            planner_module._sole_heredoc_write(sole), (target, "VALUE = 1\n")
        )
        # A trailing command, an append, and a tee write are not
        # the sole plain shape.
        self.assertIsNone(
            planner_module._sole_heredoc_write(sole + "\npython3 -m py_compile " + target)
        )
        self.assertIsNone(
            planner_module._sole_heredoc_write(
                f"cat >> {target} <<'PYEOF'\nVALUE = 1\nPYEOF"
            )
        )
        self.assertIsNone(
            planner_module._sole_heredoc_write(
                f"tee {target} <<'PYEOF'\nVALUE = 1\nPYEOF"
            )
        )

    # --- (5) Repair discipline ---

    def _bad_claw_dag(self, node_id: str, path: str) -> str:
        return self._dag(
            [
                {
                    "id": node_id,
                    "objective": "Write the file",
                    "depends_on": [],
                    "assigned_claw": "write_file",
                    "inputs": {"file_path": path, "content": "VALUE = 1\n"},
                    "outputs": [path],
                    "acceptance": ["The file is written."],
                    "criteria": [{"type": "file_exists", "path": path}],
                }
            ]
        )

    def _drive_plan(self, responses, intent, **kwargs):
        calls: list = []

        def fake_call(system, user, settings=None):
            calls.append(user)
            return responses[len(calls) - 1]

        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            plan = plan_task_intent(
                intent, settings=self._settings(), **kwargs
            )
        return plan, calls

    def test_stuck_detection_escalates_after_three_identical_rejections(
        self,
    ) -> None:
        from freetalon.orchestrator import planner as planner_module

        target = str(self.tmp_root / "report.py")
        bad = self._bad_claw_dag("step-1", target)
        calls: list = []

        def fake_call(system, user, settings=None):
            calls.append(user)
            return bad

        intent = TaskIntent(goal="Write the report file", project_type="general")
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            with self.assertRaises(planner_module.StuckRepairError) as caught:
                plan_task_intent(intent, settings=self._settings())
        # Three identical rejections, then the loop stops and
        # escalates — it does not spend the remaining budget
        # re-asking. The error is an LLMResponseError (the loop's
        # re-plan path treats it as a planning failure) and carries
        # the repeated rejection's structured records.
        self.assertEqual(len(calls), 3)
        self.assertIsInstance(caught.exception, LLMResponseError)
        self.assertIn("stuck", str(caught.exception).lower())
        self.assertIn(
            "unknown_claw",
            {record.code for record in caught.exception.plan_errors},
        )

    def test_stuck_detection_requires_three_in_a_row(self) -> None:
        # A, B, A, A, good: the identical signature never occurs
        # three times consecutively, so no stuck escalation — the
        # good plan is accepted on the last repair.
        target = str(self.tmp_root / "report.py")
        bad_a = self._bad_claw_dag("step-1", target)
        bad_b = self._bad_claw_dag("step-9", target)
        good = self._dag([self._bash_node("ls inputs")])
        intent = TaskIntent(goal="Write the report file", project_type="general")
        plan, calls = self._drive_plan(
            [bad_a, bad_b, bad_a, bad_a, good], intent, max_repairs=4
        )
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(plan.nodes), 1)

    def test_scope_lock_rejects_a_widening_repair(self) -> None:
        path_a = str(self.tmp_root / "a.py")
        path_b = str(self.tmp_root / "b.py")
        # Attempt 1 writes A but has no criteria: a repairable
        # rejection that sets the scope baseline {A}.
        attempt1 = self._dag(
            [self._scaffold_node(path_a, with_criteria=False)]
        )
        # Attempt 2 fixes the criteria — and quietly adds a second
        # file the rejected plan never wrote. It parses clean, so
        # only the scope lock can catch it.
        attempt2 = self._dag(
            [
                self._scaffold_node(path_a),
                self._scaffold_node(path_b, content="VALUE = 2\n", node_id="step-2"),
            ]
        )
        # Attempt 3 stays within the original file and is accepted.
        attempt3 = self._dag([self._scaffold_node(path_a)])
        intent = TaskIntent(goal="Write the report file", project_type="general")
        plan, calls = self._drive_plan(
            [attempt1, attempt2, attempt3], intent
        )
        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(plan.nodes[0].inputs.get("file_path"), path_a)
        # The widening rejection reached the model by name in the
        # prompt that produced attempt 3.
        self.assertIn("widens the plan's scope", calls[2])

    def test_scope_widening_error_unit(self) -> None:
        from freetalon.orchestrator import planner as planner_module
        from freetalon.orchestrator.models import PlanNode

        def scaffold(nid: str, path: str) -> PlanNode:
            return PlanNode(
                id=nid,
                objective="Write the file",
                assigned_claw="scaffold",
                inputs={"file_path": path, "content": "VALUE = 1\n"},
                criteria=[{"type": "file_exists", "path": path}],
            )

        nodes = [scaffold("n1", "/tmp/a.py"), scaffold("n2", "/tmp/b.py")]
        err = planner_module._scope_widening_error(
            nodes, {"/tmp/a.py"}, False, set(), None
        )
        self.assertIsNotNone(err)
        self.assertEqual(
            {record.code for record in err.plan_errors}, {"scope_widening"}
        )
        # A path the repair prompt itself named is endorsed scope.
        self.assertIsNone(
            planner_module._scope_widening_error(
                nodes, {"/tmp/a.py"}, False, {"/tmp/b.py"}, None
            )
        )
        # The task's workspace subtree is endorsed scope too: the
        # grounded skeleton writes the test file there, so a repair
        # doing the same is grounded, not widening (retry #19's
        # grounded repair is the historical pin for this).
        self.assertIsNone(
            planner_module._scope_widening_error(
                [scaffold("n4", "/tmp/ws/test_health.py")],
                {"/tmp/a.py"},
                False,
                set(),
                None,
                workspace="/tmp/ws",
            )
        )
        # And a test file is standing verification scope even with
        # no workspace known (a re-plan's brief carries none): the
        # grounded shape always writes one, and which test file the
        # rejected plan picked does not bind the repair.
        self.assertIsNone(
            planner_module._scope_widening_error(
                [scaffold("n5", "/tmp/elsewhere/test_health.py")],
                {"/tmp/a.py"},
                False,
                set(),
                None,
            )
        )
        # A dependency install widens a plan that installed nothing;
        # a plan that already installed may be repaired in kind.
        install = PlanNode(
            id="n3",
            objective="Install the dependency",
            assigned_claw="bash",
            inputs={"command": "pip install flask"},
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        err = planner_module._scope_widening_error(
            [install], {"/tmp/a.py"}, False, set(), None
        )
        self.assertIsNotNone(err)
        self.assertIsNone(
            planner_module._scope_widening_error(
                [install], {"/tmp/a.py"}, True, set(), None
            )
        )

    def test_repair_prompt_restates_the_task(self) -> None:
        path_a = str(self.tmp_root / "a.py")
        attempt1 = self._dag(
            [self._scaffold_node(path_a, with_criteria=False)]
        )
        attempt2 = self._dag([self._scaffold_node(path_a)])
        intent = TaskIntent(goal="Write the report file", project_type="general")
        _plan, calls = self._drive_plan([attempt1, attempt2], intent)
        self.assertIn(
            "THE TASK IS UNCHANGED: Write the report file", calls[1]
        )

    # --- (7) Formatter plumbing ---

    def test_formatter_settings_from_env(self) -> None:
        from freetalon.orchestrator import planner as planner_module

        clean = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("FREETALON_FORMATTER")
        }
        with patch.dict(os.environ, clean, clear=True):
            self.assertIsNone(planner_module.formatter_settings_from_env())
        with patch.dict(
            os.environ, {"FREETALON_FORMATTER_OLLAMA_MODEL": "llama3:latest"}
        ):
            settings = planner_module.formatter_settings_from_env()
            self.assertIsNotNone(settings)
            self.assertEqual(settings.model, "llama3:latest")
            self.assertIsNone(settings.base_url)
        with patch.dict(os.environ, {"FREETALON_FORMATTER_ENABLED": "1"}):
            settings = planner_module.formatter_settings_from_env()
            self.assertIsNotNone(settings)
            self.assertIsNone(settings.base_url)
            self.assertIsNone(settings.model)

    def test_formatter_pass_reemits_under_the_envelope(self) -> None:
        from freetalon.orchestrator import planner as planner_module
        from freetalon.orchestrator.prompts import FORMATTER_SYSTEM_PROMPT

        good = self._dag([self._bash_node("ls inputs")])
        calls: list = []

        def fake_call(system, user, settings=None):
            calls.append((system, settings))
            if system == FORMATTER_SYSTEM_PROMPT:
                return good
            return "The plan is to list the inputs and check the result."

        intent = TaskIntent(goal="Deploy a service", project_type="ops")
        env = {
            "FREETALON_FORMATTER_OLLAMA_BASE_URL": "http://formatter.invalid:11434",
            "FREETALON_FORMATTER_OLLAMA_MODEL": "fmt-model",
        }
        with patch.dict(os.environ, env):
            with patch(
                "freetalon.orchestrator.planner.call_llm",
                side_effect=fake_call,
            ):
                plan = plan_task_intent(intent, settings=self._settings())
        # Two calls: the planner's prose (unparseable on its own)
        # and the formatter's re-emission, which is what got parsed.
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(plan.nodes), 1)
        formatter_settings = calls[1][1]
        self.assertEqual(
            formatter_settings.base_url, "http://formatter.invalid:11434"
        )
        self.assertEqual(formatter_settings.model, "fmt-model")
        self.assertEqual(
            formatter_settings.format_schema,
            planner_module.plan_envelope_schema(),
        )

    def test_formatter_backend_failure_degrades_to_the_original(self) -> None:
        from freetalon.orchestrator.intake import LLMBackendError
        from freetalon.orchestrator.prompts import FORMATTER_SYSTEM_PROMPT

        good = self._dag([self._bash_node("ls inputs")])
        calls: list = []

        def fake_call(system, user, settings=None):
            calls.append(system)
            if system == FORMATTER_SYSTEM_PROMPT:
                raise LLMBackendError("formatter backend is down")
            return good

        intent = TaskIntent(goal="Deploy a service", project_type="ops")
        with patch.dict(os.environ, {"FREETALON_FORMATTER_ENABLED": "1"}):
            with patch(
                "freetalon.orchestrator.planner.call_llm",
                side_effect=fake_call,
            ):
                plan = plan_task_intent(intent, settings=self._settings())
        # The formatter was attempted, failed, and planning fell
        # back to the planner's own (valid) output.
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(plan.nodes), 1)
