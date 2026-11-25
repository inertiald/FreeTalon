"""Campaign build #14 (2026-11-24): the seam transport-format fix.

Arm #23 is the evidence. The scoped-edit seam fired in all three
cycles and build #13's persistence finally showed what the model
answered: six of six recorded responses were fabricated API-error
JSON bodies — {"error": {"message": ..., "type":
"invalid_request_error", "code": ...}} — one naming a model
('claude-sonnet-4-5-20250929') that exists nowhere on the box.
The mechanism was in the transport: intake._call_ollama sent
"format": settings.format_schema if set else "json", so a call
that dropped its plan-envelope schema (both repair seams, by
design) silently acquired a bare "json" directive. The seam is
the one call whose expected answer is NOT JSON (search/replace
edit blocks); ordered by the prompt to emit blocks and by the
transport to emit JSON, the model invented the error that
explained the missing blocks. Arms #21/#22's no_edit_blocks
declines were almost certainly this same failure, unrecorded.

The fix makes the format directive explicit: LLMSettings gains
format_mode ("schema" / "json" / "unconstrained"); a call that
expects free-form output states "unconstrained" and reaches
Ollama with NO format field, while JSON-expecting calls keep an
explicit directive (schema where a schema exists, "json" for
content fill, whose payload is parsed as a JSON object). The
audit behind the per-site treatment is in the build log: every
call_llm site was classified by what it actually expects back —
the seams, bootstrap codegen, and the synthesis handler are
free-form; intake, the planner, the formatter, the contract
re-ask, and the critic are schema'd JSON; fill is bare JSON.

These tests pin: (a) the seams' outgoing settings carry no JSON
constraint and their transport payload carries no format field;
(b) the JSON-expecting sites still carry theirs; (c) an
end-to-end seam run through the REAL transport (only the HTTP
post is mocked) where the model answers with real edit blocks
and the edit lands — the first time this path could be
exercised; (d) the arm #23 fabricated-error shapes are
classified as error_body_response if they ever recur.

No GPU is touched: every LLM call is mocked or scripted.
"""

from __future__ import annotations

import unittest
from unittest import mock

from freetalon.orchestrator import intake as intake_module
from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import (
    LLMSettings,
    resolved_format_mode,
)
from freetalon.orchestrator.models import TaskIntent
from tests.test_campaign_build12 import FIX_BLOCK, ProbeSeamCase
from tests.test_campaign_build13 import (
    GOOD_DEFINITION,
    LOCAL_DIAGNOSTICS,
)
from tests.test_campaign_build3 import HandlerCase
from tests.test_planner_schema_redesign import _bash_node, _dag

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

# Arm #23's three recorded response shapes, verbatim in structure
# (run-20261125T004008Z-3d42f7, seam_attempts.jsonl).
ERROR_BODY_MODEL = (
    '{\n  "error": {\n    "message": "We\'re unable to route your '
    "request because the model 'claude-sonnet-4-5-20250929' is "
    'not supported. Please use a different model.",\n    "type": '
    '"invalid_request_error",\n    "param": null,\n    "code": '
    '"model_not_supported"\n  }\n}'
)
ERROR_BODY_JSON = (
    '{\n  "error": {\n    "message": "Invalid JSON payload: '
    'Expecting value: line 1 column 1 (char 0)",\n    "type": '
    '"invalid_request_error",\n    "param": null,\n    "code": '
    '"invalid_json"\n  }\n}'
)
ERROR_BODY_TEMPORARY = (
    '{\n  "error": {\n    "message": "We\'re unable to process '
    "your request at this time. This is most likely a temporary "
    'issue.",\n    "type": "invalid_request_error",\n    '
    '"param": null,\n    "code": null\n  }\n}'
)


def _payload_for(settings: LLMSettings) -> dict:
    """Run _call_ollama with the HTTP post mocked; return the payload."""
    with mock.patch.object(
        intake_module,
        "_post_json",
        return_value={"message": {"content": "ok"}},
    ) as post_mock:
        intake_module._call_ollama(settings, "sys", "user")
    return post_mock.call_args[0][1]


def _scripted_settings(responses):
    """A call_llm fake answering from a queue, capturing settings."""
    queue = list(responses)
    calls: list[dict] = []

    def fake_call_llm(system_prompt, user_prompt, settings=None):
        calls.append(
            {"system": system_prompt, "user": user_prompt, "settings": settings}
        )
        return queue.pop(0) if queue else ""

    return fake_call_llm, calls


# ── The representation ──────────────────────────────────────────


class TestResolvedFormatMode(unittest.TestCase):
    def test_explicit_mode_wins(self):
        schema = {"type": "object"}
        self.assertEqual(
            resolved_format_mode(
                LLMSettings(format_schema=schema, format_mode="unconstrained")
            ),
            "unconstrained",
        )
        self.assertEqual(
            resolved_format_mode(LLMSettings(format_mode="json")), "json"
        )

    def test_unstated_mode_resolves_from_schema(self):
        # The pre-#14 behavior for every call that states nothing:
        # schema when one is set, the bare JSON directive otherwise.
        self.assertEqual(
            resolved_format_mode(LLMSettings(format_schema={"type": "object"})),
            "schema",
        )
        self.assertEqual(resolved_format_mode(LLMSettings()), "json")


class TestOllamaPayloadFormat(unittest.TestCase):
    def test_default_settings_keep_the_json_directive(self):
        self.assertEqual(_payload_for(LLMSettings())["format"], "json")

    def test_schema_settings_carry_the_schema(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        payload = _payload_for(LLMSettings(format_schema=schema))
        self.assertEqual(payload["format"], schema)

    def test_explicit_json_mode_carries_json(self):
        payload = _payload_for(LLMSettings(format_mode="json"))
        self.assertEqual(payload["format"], "json")

    def test_unconstrained_omits_the_format_field(self):
        payload = _payload_for(LLMSettings(format_mode="unconstrained"))
        self.assertNotIn("format", payload)

    def test_unconstrained_wins_over_a_set_schema(self):
        # The seam's exact situation before the fix was the mirror
        # image of this: a dropped schema silently became "json".
        # An explicit mode is the caller's stated expectation and
        # the transport honors it even with a schema present.
        payload = _payload_for(
            LLMSettings(
                format_schema={"type": "object"},
                format_mode="unconstrained",
            )
        )
        self.assertNotIn("format", payload)


# ── (b) JSON-expecting sites keep their directive ───────────────


class TestJsonExpectingSites(unittest.TestCase):
    def test_intake_request_states_schema_mode(self):
        captured: dict = {}

        def fake_call(system, user, settings=None):
            captured["settings"] = settings
            return "{}"

        with mock.patch.object(intake_module, "call_llm", fake_call):
            try:
                intake_module.intake_request("Build a thing", settings=SETTINGS)
            except Exception:
                # Parsing "{}" may fail downstream; the settings
                # the call carried are what this test pins.
                pass
        self.assertEqual(captured["settings"].format_mode, "schema")
        self.assertEqual(
            captured["settings"].format_schema,
            TaskIntent.model_json_schema(),
        )

    def test_planner_call_states_schema_mode(self):
        captured: list = []

        def fake_call(system, user, settings=None):
            captured.append(settings)
            return _dag([_bash_node()])

        intent = TaskIntent(goal="Deploy a service", project_type="ops")
        with mock.patch.object(planner_module, "call_llm", fake_call):
            planner_module.plan_task_intent(intent, settings=SETTINGS)
        self.assertTrue(captured)
        for settings in captured:
            self.assertEqual(settings.format_mode, "schema")
            self.assertEqual(
                settings.format_schema, planner_module.plan_envelope_schema()
            )

    def test_formatter_states_schema_mode(self):
        captured: dict = {}

        def fake_call(system, user, settings=None):
            captured["settings"] = settings
            return "{}"

        formatter = planner_module.FormatterSettings(
            base_url=None, model=None
        )
        with mock.patch.object(planner_module, "call_llm", fake_call):
            planner_module.reformat_plan_output("{}", SETTINGS, formatter)
        self.assertEqual(captured["settings"].format_mode, "schema")
        self.assertEqual(
            captured["settings"].format_schema,
            planner_module.plan_envelope_schema(),
        )

    def test_content_fill_states_json_mode(self):
        # Fill's payload is parsed as a JSON object downstream —
        # the envelope schema goes, the JSON directive stays, now
        # as a declared mode instead of a transport accident.
        effective = planner_module._content_fill_settings(
            LLMSettings(format_schema={"type": "object"})
        )
        self.assertIsNone(effective.format_schema)
        self.assertEqual(effective.format_mode, "json")
        self.assertEqual(_payload_for(effective)["format"], "json")


# ── (a) The seams state unconstrained ───────────────────────────


class TestScopedEditSeamSettings(ProbeSeamCase):
    def test_seam_calls_carry_no_json_constraint(self):
        fake, calls = _scripted_settings([FIX_BLOCK])
        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_scoped_edit_repair(
                self._plan(), self._brief(), SETTINGS, declines=declines
            )
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)
        settings = calls[0]["settings"]
        self.assertIsNone(settings.format_schema)
        self.assertEqual(settings.format_mode, "unconstrained")
        self.assertEqual(resolved_format_mode(settings), "unconstrained")


class TestLocalRepairSeamSettings(HandlerCase):
    def _run_scripted(self, responses):
        nodes = [
            self._write_node("node-1"),
            self._run_node(
                "node-2",
                LOCAL_DIAGNOSTICS.format(path=self.handler_path),
            ),
        ]
        plan = self._plan(nodes)
        brief = self._brief(
            LOCAL_DIAGNOSTICS.format(path=self.handler_path)
        )
        fake, calls = _scripted_settings(responses)
        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_local_repair(
                plan, brief, SETTINGS, declines=declines
            )
        return result, calls, declines, records

    def test_seam_calls_carry_no_json_constraint(self):
        result, calls, declines, records = self._run_scripted(
            [GOOD_DEFINITION]
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)
        settings = calls[0]["settings"]
        self.assertIsNone(settings.format_schema)
        self.assertEqual(settings.format_mode, "unconstrained")


# ── (c) End to end through the real transport ───────────────────


class TestSeamEndToEndThroughTransport(ProbeSeamCase):
    def test_edit_blocks_land_with_no_format_field_sent(self):
        payloads: list[dict] = []

        def fake_post(url, payload, timeout_seconds, headers=None):
            payloads.append(payload)
            return {"message": {"content": FIX_BLOCK}}

        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(
                planner_module, "call_llm", intake_module.call_llm
            ),
            mock.patch.object(intake_module, "_post_json", fake_post),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_scoped_edit_repair(
                self._plan(), self._brief(), SETTINGS, declines=declines
            )
        # The whole channel, exercised for the first time: the
        # seam's request reached the transport with NO format
        # field, the model answered with real edit blocks, and
        # the candidate carries the repaired content.
        self.assertEqual(declines, [])
        self.assertIsNotNone(result)
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("format", payloads[0])
        by_id = {node.id: node for node in result}
        content = by_id["node-1"].inputs["content"]
        self.assertNotIn("time.sleep(30)", content)
        self.assertIn("return 201", content)
        self.assertEqual(records[0]["outcome"], "accepted")


# ── (d) Fabricated error bodies are named what they are ─────────


class TestFabricatedErrorBodies(unittest.TestCase):
    def test_classifier_accepts_the_arm23_shapes(self):
        for body in (
            ERROR_BODY_MODEL,
            ERROR_BODY_JSON,
            ERROR_BODY_TEMPORARY,
        ):
            self.assertTrue(
                planner_module._is_fabricated_error_body(body), body[:60]
            )

    def test_classifier_rejects_ordinary_answers(self):
        for text in (
            FIX_BLOCK,
            "The handler sleeps; remove the sleep.",
            "{}",
            '{"nodes": []}',
            '{"error": {}}',
            '{"error": ""}',
            "Error: the file was not found",
        ):
            self.assertFalse(
                planner_module._is_fabricated_error_body(text), text[:60]
            )


class TestScopedEditErrorBodyDecline(ProbeSeamCase):
    def _run_scripted(self, responses):
        fake, calls = _scripted_settings(responses)
        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_scoped_edit_repair(
                self._plan(), self._brief(), SETTINGS, declines=declines
            )
        return result, calls, declines, records

    def test_two_error_bodies_decline_as_error_body_response(self):
        result, calls, declines, records = self._run_scripted(
            [ERROR_BODY_MODEL, ERROR_BODY_JSON]
        )
        self.assertIsNone(result)
        # The error-body class earns the same one re-ask as any
        # no-blocks answer — and declines under its own name.
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [d["reason"] for d in declines], ["error_body_response"]
        )
        self.assertEqual(
            [r["decline_reason"] for r in records],
            ["error_body_response", "error_body_response"],
        )

    def test_error_body_then_blocks_lands(self):
        result, calls, declines, records = self._run_scripted(
            [ERROR_BODY_MODEL, FIX_BLOCK]
        )
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])
        self.assertEqual(records[0]["decline_reason"], "error_body_response")
        self.assertEqual(records[1]["outcome"], "accepted")


class TestLocalRepairErrorBodyDecline(HandlerCase):
    def test_two_error_bodies_decline_as_error_body_response(self):
        nodes = [
            self._write_node("node-1"),
            self._run_node(
                "node-2",
                LOCAL_DIAGNOSTICS.format(path=self.handler_path),
            ),
        ]
        plan = self._plan(nodes)
        brief = self._brief(
            LOCAL_DIAGNOSTICS.format(path=self.handler_path)
        )
        fake, calls = _scripted_settings(
            [ERROR_BODY_TEMPORARY, ERROR_BODY_JSON]
        )
        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_local_repair(
                plan, brief, SETTINGS, declines=declines
            )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [d["reason"] for d in declines], ["error_body_response"]
        )
        self.assertEqual(
            [r["decline_reason"] for r in records],
            ["error_body_response", "error_body_response"],
        )


if __name__ == "__main__":
    unittest.main()
