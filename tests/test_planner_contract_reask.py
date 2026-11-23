"""Constrained contract re-ask (L1, 2026-11-23): when an
accepted plan couples NEW modules its own "contract" declaration
omitted, _call_llm_with_repair makes ONE extra call under the
contract-required envelope schema (Ollama constrained decoding:
"contract" promoted to a required key, minItems 1) and adopts
the re-asked plan only if it declares the full contract itself.

Why this shape: the prompt already taught the contract with a
worked example, the repair feedback already spelled out the
required shape, and two models still emitted the array zero
times (arm 88C on the 7B; exam re-arm #2 on qwen3.8:27b). The
cross-model consultation's strongest mechanism was making the
omission un-emittable instead of better-explained. The default
schema keeps "contract" optional — simple plans must not be
forced to invent hollow entries — so the constraint is applied
as a targeted re-ask on the measured omission, with derivation
(the L4 fallback) still completing the first plan's contract
when the re-ask does not produce a fully declared one.

call_llm is mocked throughout: no GPU is touched.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMSettings
from freetalon.orchestrator.planner import (
    _call_llm_with_repair,
    _contract_constrained_reask,
    _contract_omitted_modules,
    parse_planner_response,
    plan_envelope_schema,
)

ROOT = Path(__file__).resolve().parent.parent
GOAL = "Implement the pairing server endpoint as a new package and test it."
PKG = ROOT / "freetalon" / "probe_reask"
TEST_FILE = ROOT / "tests" / "test_probe_reask.py"

STORE = '''"""Probe reask registry."""

import json
import os


class CodeNotFound(Exception):
    """Raised when a pairing code is unknown."""


class PairingStore:
    """File-backed store for pairing codes."""

    def __init__(self, path):
        self.path = path

    def register(self, code):
        record = {"code": code, "state": "pending"}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return record

    def get(self, code):
        if not os.path.exists(self.path):
            raise CodeNotFound(code)
        with open(self.path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
        if record.get("code") != code:
            raise CodeNotFound(code)
        return record
'''

SERVER = '''"""Probe reask server."""

from freetalon.probe_reask.registry import CodeNotFound, PairingStore


class PairingServer:
    """Pairing operations over the store."""

    def __init__(self, store):
        self.store = store

    def poll(self, code):
        try:
            record = self.store.get(code)
        except CodeNotFound:
            return {"status": "not-found"}
        return {"status": record["state"]}
'''

TESTS = '''"""Tests for the probe reask package."""

import os
import tempfile
import unittest

from freetalon.probe_reask.endpoint import PairingServer
from freetalon.probe_reask.registry import CodeNotFound, PairingStore


class TestProbeReask(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "store.json")
        self.store = PairingStore(self.path)
        self.server = PairingServer(self.store)

    def test_poll_pending(self):
        self.store.register("KESTREL-0001")
        result = self.server.poll("KESTREL-0001")
        self.assertEqual(result["status"], "pending")

    def test_poll_unknown(self):
        result = self.server.poll("NOPE-0000")
        self.assertEqual(result["status"], "not-found")

    def test_get_unknown_raises(self):
        with self.assertRaises(CodeNotFound):
            self.store.get("NOPE-0000")


if __name__ == "__main__":
    unittest.main()
'''

DECLARED_CONTRACT = [
    {
        "module": "freetalon/probe_reask/endpoint.py",
        "defines": ["PairingServer"],
        "statuses": ["not-found"],
    },
    {
        "module": "freetalon/probe_reask/registry.py",
        "defines": ["CodeNotFound", "PairingStore"],
        "statuses": ["pending"],
    },
]

DERIVED_CONTRACT = [
    {
        "module": "freetalon/probe_reask/endpoint.py",
        "defines": ["PairingServer"],
        "statuses": ["not-found"],
    },
    {
        "module": "freetalon/probe_reask/registry.py",
        "defines": ["CodeNotFound", "PairingStore"],
        "statuses": ["pending"],
    },
]

COUPLED_MODULES = [
    "freetalon/probe_reask/endpoint.py",
    "freetalon/probe_reask/registry.py",
]


def _scaffold(node_id: str, path: Path, content: str, deps: list[str]) -> dict:
    return {
        "id": node_id,
        "objective": f"Write {path.name}",
        "depends_on": deps,
        "assigned_claw": "scaffold",
        "inputs": {"file_path": str(path), "content": content},
        "outputs": [str(path)],
        "criteria": [{"type": "file_exists", "path": str(path)}],
    }


def _mkdir(node_id: str, directory: Path) -> dict:
    return {
        "id": node_id,
        "objective": f"Create the {directory.name} package directory",
        "depends_on": [],
        "assigned_claw": "bash",
        "inputs": {"command": f"mkdir -p {directory}"},
        "outputs": ["result"],
        "criteria": [{"type": "exit_code", "equals": 0}],
    }


def _coupled_nodes() -> list[dict]:
    return [
        _mkdir("step_1", PKG),
        _scaffold("step_2", PKG / "registry.py", STORE, ["step_1"]),
        _scaffold("step_3", PKG / "endpoint.py", SERVER, ["step_2"]),
        _scaffold("step_4", TEST_FILE, TESTS, ["step_3"]),
    ]


def _dag(nodes: list[dict], contract: list | None = None) -> str:
    payload: dict = {"nodes": nodes, "metadata": {}}
    if contract is not None:
        payload["contract"] = contract
    return json.dumps(payload)


def _simple_dag() -> str:
    lonely = ROOT / "freetalon" / "probe_reask" / "lonely.py"
    node = _scaffold(
        "step_1", lonely, '"""Standalone probe script."""\n\nprint("hi")\n', []
    )
    return _dag([node])


class _ScriptedLLM:
    """call_llm stand-in: returns queued responses, records calls."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, system_prompt, user_prompt, settings=None):
        self.calls.append(
            {"system": system_prompt, "prompt": user_prompt, "settings": settings}
        )
        if not self.responses:
            raise AssertionError("unexpected extra LLM call")
        return self.responses.pop(0)


def _plan_with(fake: _ScriptedLLM, **kwargs):
    with mock.patch.object(planner_module, "call_llm", fake):
        return _call_llm_with_repair(
            "system", "user", LLMSettings(), goal_text=GOAL, **kwargs
        )


class TestEnvelopeSchemaVariant(unittest.TestCase):
    def test_default_schema_keeps_contract_optional(self) -> None:
        schema = plan_envelope_schema()
        self.assertEqual(schema["required"], ["nodes"])
        self.assertNotIn("minItems", schema["properties"]["contract"])

    def test_required_variant_shape(self) -> None:
        schema = plan_envelope_schema(require_contract=True)
        self.assertEqual(schema["required"], ["nodes", "contract"])
        contract = schema["properties"]["contract"]
        self.assertEqual(contract["minItems"], 1)
        self.assertEqual(contract["items"]["required"], ["module", "defines"])
        self.assertEqual(
            contract["items"]["properties"]["defines"]["minItems"], 1
        )

    def test_variant_does_not_leak_into_default(self) -> None:
        plan_envelope_schema(require_contract=True)
        schema = plan_envelope_schema()
        self.assertEqual(schema["required"], ["nodes"])
        self.assertNotIn("minItems", schema["properties"]["contract"])


class TestOmittedModules(unittest.TestCase):
    def test_coupled_plan_without_contract_reports_both_modules(self) -> None:
        text = _dag(_coupled_nodes())
        nodes, _metadata = parse_planner_response(text, goal_text=GOAL)
        self.assertEqual(
            _contract_omitted_modules(nodes, text, None), COUPLED_MODULES
        )

    def test_declared_contract_reports_no_omission(self) -> None:
        text = _dag(_coupled_nodes(), contract=DECLARED_CONTRACT)
        nodes, _metadata = parse_planner_response(text, goal_text=GOAL)
        self.assertEqual(_contract_omitted_modules(nodes, text, None), [])

    def test_simple_plan_reports_no_omission(self) -> None:
        text = _simple_dag()
        nodes, _metadata = parse_planner_response(
            text, goal_text="Write a standalone probe script."
        )
        self.assertEqual(_contract_omitted_modules(nodes, text, None), [])


class TestConstrainedReask(unittest.TestCase):
    def test_reask_fires_and_adopts_declared_plan(self) -> None:
        fake = _ScriptedLLM(
            [
                _dag(_coupled_nodes()),
                _dag(_coupled_nodes(), contract=DECLARED_CONTRACT),
            ]
        )
        nodes, metadata = _plan_with(fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(nodes), 4)
        # First pass ran under the default (contract-optional) schema.
        self.assertEqual(
            fake.calls[0]["settings"].format_schema, plan_envelope_schema()
        )
        # The re-ask ran under the contract-required variant and
        # named the requirement in the prompt.
        self.assertEqual(
            fake.calls[1]["settings"].format_schema,
            plan_envelope_schema(require_contract=True),
        )
        self.assertIn("CONTRACT DECLARATION REQUIRED", fake.calls[1]["prompt"])
        # The adopted plan's contract is the model's own declaration.
        self.assertEqual(metadata["module_contract"], DECLARED_CONTRACT)

    def test_reask_falls_back_when_reask_still_omits(self) -> None:
        fake = _ScriptedLLM([_dag(_coupled_nodes()), _dag(_coupled_nodes())])
        nodes, metadata = _plan_with(fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(nodes), 4)
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)

    def test_reask_falls_back_when_reask_unparseable(self) -> None:
        fake = _ScriptedLLM([_dag(_coupled_nodes()), "this is not json"])
        nodes, metadata = _plan_with(fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(nodes), 4)
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)

    def test_no_reask_for_simple_plan(self) -> None:
        fake = _ScriptedLLM([_simple_dag()])
        with mock.patch.object(planner_module, "call_llm", fake):
            nodes, _metadata = _call_llm_with_repair(
                "system",
                "user",
                LLMSettings(),
                goal_text="Write a standalone probe script.",
            )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(nodes), 1)

    def test_no_reask_when_contract_declared_first_time(self) -> None:
        fake = _ScriptedLLM([_dag(_coupled_nodes(), contract=DECLARED_CONTRACT)])
        _nodes, metadata = _plan_with(fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(metadata["module_contract"], DECLARED_CONTRACT)

    def test_no_reask_under_pinned_contract(self) -> None:
        fake = _ScriptedLLM([_dag(_coupled_nodes())])
        _nodes, metadata = _plan_with(fake, pinned_contract=DECLARED_CONTRACT)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(metadata["module_contract"], DECLARED_CONTRACT)

    def test_no_reask_with_caller_supplied_schema(self) -> None:
        caller_schema = {"type": "object"}
        fake = _ScriptedLLM([_dag(_coupled_nodes())])
        with mock.patch.object(planner_module, "call_llm", fake):
            _nodes, metadata = _call_llm_with_repair(
                "system",
                "user",
                LLMSettings(format_schema=caller_schema),
                goal_text=GOAL,
            )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(
            fake.calls[0]["settings"].format_schema, caller_schema
        )
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)



DOTTED_CONTRACT = [
    {
        "module": "freetalon/probe_reask/endpoint.py",
        "defines": ["PairingServer"],
        "statuses": ["not-found"],
    },
    {
        "module": "freetalon/probe_reask/registry.py",
        "defines": [
            "CodeNotFound",
            "PairingStore",
            "PairingStore.get",
            "PairingStore.register",
        ],
        "statuses": ["pending"],
    },
]


class TestReaskOutcomeRecord(unittest.TestCase):
    """The re-ask's outcome lands in the plan metadata.

    Exam re-arm #3's post-mortem had to replay the raw re-ask
    output against a reconstructed tree to learn why the richer
    declared contract was not adopted — every rejection path
    returned a bare None and the reason was recorded nowhere.
    These tests pin the record for each gate.
    """

    def test_adopted_outcome_recorded(self) -> None:
        fake = _ScriptedLLM(
            [
                _dag(_coupled_nodes()),
                _dag(_coupled_nodes(), contract=DECLARED_CONTRACT),
            ]
        )
        _nodes, metadata = _plan_with(fake)
        self.assertEqual(metadata["contract_reask"], {"outcome": "adopted"})

    def test_parse_rejection_recorded(self) -> None:
        fake = _ScriptedLLM([_dag(_coupled_nodes()), "this is not json"])
        _nodes, metadata = _plan_with(fake)
        record = metadata["contract_reask"]
        self.assertEqual(record["outcome"], "rejected")
        self.assertEqual(record["gate"], "parse")
        self.assertTrue(record["errors"])
        # The non-JSON rejection is a bare LLMResponseError with no
        # structured records attached (parse's own behavior), so the
        # record names the exception type and carries its message.
        self.assertEqual(record["errors"][0]["code"], "LLMResponseError")
        self.assertIn("not valid JSON", record["errors"][0]["message"])

    def test_contract_shape_rejection_recorded(self) -> None:
        # The re-arm #3 shape exactly: the re-ask declares the
        # contract with DOTTED method spellings ("PairingStore.get"),
        # which _normalize_contract rejects — the whole declaration
        # is discarded and both coupled modules read as missing.
        # The record must name the gate and the codes.
        fake = _ScriptedLLM(
            [
                _dag(_coupled_nodes()),
                _dag(_coupled_nodes(), contract=DOTTED_CONTRACT),
            ]
        )
        _nodes, metadata = _plan_with(fake)
        record = metadata["contract_reask"]
        self.assertEqual(record["outcome"], "rejected")
        self.assertEqual(record["gate"], "parse")
        codes = [error["code"] for error in record["errors"]]
        self.assertIn("contract_shape", codes)
        self.assertIn("contract_missing", codes)
        # The accepted plan still stands on the derived contract.
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)

    def test_still_omitted_rejection_recorded(self) -> None:
        fake = _ScriptedLLM([_dag(_coupled_nodes()), _dag(_coupled_nodes())])
        _nodes, metadata = _plan_with(fake)
        record = metadata["contract_reask"]
        self.assertEqual(record["outcome"], "rejected")
        self.assertEqual(record["gate"], "omitted_modules")
        self.assertEqual(record["modules"], COUPLED_MODULES)

    def test_no_record_when_reask_does_not_fire(self) -> None:
        fake = _ScriptedLLM([_simple_dag()])
        with mock.patch.object(planner_module, "call_llm", fake):
            _nodes, metadata = _call_llm_with_repair(
                "system",
                "user",
                LLMSettings(),
                goal_text="Write a standalone probe script.",
            )
        self.assertNotIn("contract_reask", metadata)


class TestReaskDirectGates(unittest.TestCase):
    """The gates a scripted _call_llm_with_repair cannot reach —
    a scope baseline (set only by an earlier rejection) and a
    dead backend — are exercised against the re-ask directly."""

    def _direct(self, fake, **overrides):
        params = dict(
            omitted=COUPLED_MODULES,
            goal_text=GOAL,
            workspace=None,
            repo_root=None,
            formatter=None,
            baseline_paths=None,
            baseline_installs=False,
            endorsed_paths=set(),
        )
        params.update(overrides)
        with mock.patch.object(planner_module, "call_llm", fake):
            return _contract_constrained_reask(
                "system", "user", LLMSettings(), **params
            )

    def test_call_failure_recorded(self) -> None:
        def boom(system_prompt, user_prompt, settings=None):
            raise RuntimeError("backend down")

        adopted, record = self._direct(boom)
        self.assertIsNone(adopted)
        self.assertEqual(record["outcome"], "rejected")
        self.assertEqual(record["gate"], "call")
        self.assertEqual(record["errors"][0]["code"], "RuntimeError")
        self.assertIn("backend down", record["errors"][0]["message"])

    def test_scope_lock_rejection_recorded(self) -> None:
        baseline_paths, baseline_installs = (
            planner_module._response_write_scope(_dag(_coupled_nodes()))
        )
        widening_nodes = _coupled_nodes() + [
            _scaffold(
                "step_5",
                PKG / "extra.py",
                '"""Extra module outside the accepted scope."""\n\nVALUE = 1\n',
                ["step_1"],
            )
        ]
        fake = _ScriptedLLM([_dag(widening_nodes, contract=DECLARED_CONTRACT)])
        adopted, record = self._direct(
            fake,
            baseline_paths=baseline_paths,
            baseline_installs=baseline_installs,
        )
        self.assertIsNone(adopted)
        self.assertEqual(record["outcome"], "rejected")
        self.assertEqual(record["gate"], "scope_lock")
        self.assertEqual(record["errors"][0]["code"], "scope_widening")
if __name__ == "__main__":
    unittest.main()
