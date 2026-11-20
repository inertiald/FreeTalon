"""Planner schema redesign (2026-11-19): system-owned delta fields,
prose bounds, and the generation fuse.

The assessment (freetalon-workspace/logs/planner-assessment-
2026-11-19.md) measured the model-emitted ``differs_by`` field at
93.6% of the pairing run's planner output — one value of 29,358
chars degenerating into a repetition loop, zero complete nodes —
with zero code consumers reading its content. These tests pin the
redesign:

- the envelope schema no longer asks the model for ``status``,
  ``responds_to`` or ``differs_by`` (the PlanNode fields stay,
  system-owned);
- parse strips model-emitted ``responds_to`` / ``differs_by``, so
  the repeat checks can only ever read the repair path's own note;
- prose fields carry hard caps (``field_too_long``) and a
  deterministic repetition-loop detector (``repetition_loop``),
  proven against the pairing run's actual degenerate field, kept
  as a fixture;
- planner calls carry a ``num_predict`` generation ceiling.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import PlanNode, PlanStatus, TaskIntent
from freetalon.orchestrator.planner import parse_planner_response

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "pairing_output1_differs_by.txt"
)


def _bash_node(command: str = "ls", **overrides: object) -> dict:
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


def _dag(nodes: list[dict]) -> str:
    return json.dumps({"nodes": nodes, "metadata": {}})


def _varied_prose(chars: int) -> str:
    """Deterministic prose of ~``chars`` chars with no repetition:
    every sentence is numbered, so no clause or word 6-gram repeats."""
    sentences = []
    total = 0
    index = 0
    while total < chars:
        index += 1
        sentence = (
            f"Step {index} verifies component {index} against "
            f"requirement {index} using method {index} and records "
            f"outcome {index}."
        )
        sentences.append(sentence)
        total += len(sentence) + 1
    return " ".join(sentences)[:chars]


class TestSystemOwnedDeltaFields(unittest.TestCase):
    def test_envelope_schema_omits_system_owned_fields(self) -> None:
        schema = planner_module.plan_envelope_schema()
        properties = schema["properties"]["nodes"]["items"]["properties"]
        self.assertNotIn("status", properties)
        self.assertNotIn("responds_to", properties)
        self.assertNotIn("differs_by", properties)
        # The contract fields stay — they are the work.
        for kept in (
            "id",
            "objective",
            "depends_on",
            "assigned_claw",
            "inputs",
            "criteria",
            "reversible",
            "timeout_s",
            "max_retries",
            "hypothesis",
        ):
            self.assertIn(kept, properties)
        # The PlanNode fields themselves stay: system-owned state,
        # populated by the repair path, read by the repeat checks.
        for field in ("status", "responds_to", "differs_by"):
            self.assertIn(field, PlanNode.model_fields)

    def test_parse_strips_model_emitted_delta_fields(self) -> None:
        node = _bash_node(
            differs_by="model-written justification prose",
            responds_to="step_0",
            status="completed",
        )
        nodes, _ = parse_planner_response(_dag([node]))
        self.assertEqual(nodes[0].differs_by, "")
        self.assertEqual(nodes[0].responds_to, "")
        self.assertEqual(nodes[0].status, PlanStatus.DRAFT)

    def test_system_repair_note_still_waves_delta_check(self) -> None:
        # The escape hatch survives — as the system's note, on a
        # materialized PlanNode, exactly as the repair path writes
        # it. What changed is provenance: the model can no longer
        # inject the note through parse (previous test).
        from freetalon.orchestrator.judge import Judge
        from freetalon.orchestrator.models import (
            ExecutionPlan,
            GoalContract,
            RunBudget,
        )

        judge = Judge(RunBudget())
        measurement = {
            "node_id": "step_1",
            "verdict": "FAIL",
            "results": [],
            "envelope_status": "completed",
        }
        failed = PlanNode(
            id="step_1",
            objective="Build the widget",
            assigned_claw="scaffold",
            inputs={"file_path": "/repo/api.py", "content": "# v1\n"},
            status=PlanStatus.FAILED,
            measurement=measurement,
        )
        plan = ExecutionPlan(
            plan_id="plan-test",
            intent=TaskIntent(goal="g"),
            nodes=[failed],
            status=PlanStatus.FAILED,
        )
        brief = judge.build_replan_brief(
            GoalContract(objective="g"), plan, [measurement], [], 1
        )
        repeat = PlanNode(
            id="step_1",
            objective="Build the widget, reworded",
            assigned_claw="scaffold",
            inputs={"file_path": "/repo/api.py", "content": "# v2\n"},
        )
        error = judge.validate_delta_plan(brief, [repeat])
        self.assertIsNotNone(error)
        self.assertIn("repeats failed approach", error)
        self.assertIn("without a system repair note", error)
        noted = repeat.model_copy(
            update={
                "differs_by": (
                    "node-local repair: handler rewritten in response "
                    "to the measured failure at step_1"
                )
            }
        )
        self.assertIsNone(judge.validate_delta_plan(brief, [noted]))


class TestProseBounds(unittest.TestCase):
    def test_loop_detector_flags_pairing_fixture(self) -> None:
        # The genuine article: output 1's differs_by value from the
        # pairing run's driver log (29,357 chars as extracted; the
        # assessment measured 29,358 from the live stream — the
        # block delimiter accounts for the difference). Its top
        # clause, "no 7zr", repeats 898 times.
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertGreater(len(text), 29_000)
        self.assertEqual(text.count("no 7zr"), 898)
        detail = planner_module._repetition_loop_detail(text)
        self.assertIsNotNone(detail)
        self.assertIn("repeats", detail or "")

    def test_parse_rejects_looping_prose_field(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        node = _bash_node(objective=text)
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag([node]))
        codes = {record.code for record in caught.exception.plan_errors}
        self.assertIn("repetition_loop", codes)
        self.assertIn("field_too_long", codes)

    def test_parse_rejects_overlong_prose_without_loop(self) -> None:
        node = _bash_node(objective=_varied_prose(500))
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag([node]))
        codes = {record.code for record in caught.exception.plan_errors}
        self.assertIn("field_too_long", codes)
        self.assertNotIn("repetition_loop", codes)

    def test_loop_detector_ignores_healthy_text(self) -> None:
        self.assertIsNone(
            planner_module._repetition_loop_detail(_varied_prose(500))
        )
        # Under the length gate a repeated fragment is emphasis, not
        # a runaway — the detector only judges strings past 200 chars.
        self.assertIsNone(
            planner_module._repetition_loop_detail("no; " * 10)
        )

    def test_payload_repetition_is_not_scanned(self) -> None:
        # Repetition inside inputs (commands, code) can be
        # legitimate; the prose guards look at prose fields only.
        node = PlanNode(
            id="step_1",
            objective="Run the repeated command",
            assigned_claw="bash",
            inputs={"command": "echo same words here now; " * 60},
        )
        self.assertEqual(planner_module._repetition_loop_errors([node]), [])
        self.assertEqual(planner_module._prose_length_errors([node]), [])


class TestGenerationFuse(unittest.TestCase):
    def test_num_predict_env_knob(self) -> None:
        with patch.dict(os.environ, {"FREETALON_LLM_NUM_PREDICT": "1234"}):
            self.assertEqual(LLMSettings.from_env().num_predict, 1234)
        with patch.dict(os.environ, {"FREETALON_LLM_NUM_PREDICT": "junk"}):
            self.assertIsNone(LLMSettings.from_env().num_predict)
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "FREETALON_LLM_NUM_PREDICT"
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(LLMSettings.from_env().num_predict)

    def test_planner_call_carries_the_fuse(self) -> None:
        captured: dict = {}

        def fake_call(system, user, settings=None):
            captured["settings"] = settings
            return _dag([_bash_node()])

        intent = TaskIntent(goal="Deploy a service", project_type="ops")
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            planner_module.plan_task_intent(intent, settings=LLMSettings())
        self.assertEqual(
            captured["settings"].num_predict,
            planner_module._PLANNER_NUM_PREDICT,
        )
        # A caller-set ceiling is respected, never overridden — the
        # num_ctx rule.
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=fake_call
        ):
            planner_module.plan_task_intent(
                intent, settings=LLMSettings(num_predict=42)
            )
        self.assertEqual(captured["settings"].num_predict, 42)

    def test_ollama_payload_sends_num_predict_only_when_configured(self) -> None:
        from freetalon.orchestrator import intake as intake_module

        payloads: list[dict] = []

        def fake_post(url, payload, timeout_seconds, headers=None):
            payloads.append(payload)
            return {"message": {"content": "{}"}}

        with patch.object(intake_module, "_post_json", side_effect=fake_post):
            intake_module._call_ollama(LLMSettings(), "sys", "user")
            intake_module._call_ollama(
                LLMSettings(num_predict=99), "sys", "user"
            )
        self.assertNotIn("num_predict", payloads[0]["options"])
        self.assertEqual(payloads[1]["options"]["num_predict"], 99)


if __name__ == "__main__":
    unittest.main()
