"""Module contract derivation (2026-11-23): the contract skeleton
is system-owned.

Exam re-arm #2 (2026-11-22) reproduced arm 88C's failure on the
27B: the planner prompt already taught the module contract with a
worked example, the envelope schema already carried the key, and
the contract_missing repair feedback already spelled out the exact
shape — and qwen3.8:27b still emitted the top-level "contract"
array zero times across three candidate plans, so a from-scratch
multi-module build died at planning without its coherence ever
being judged.

Per the differs_by precedent (fields the model emits unreliably
are taken out of the model's hands), parse now derives the entries
a plan omits from the plan's own coupling (_derive_contract_entries
in planner.py): one entry per coupled NEW module, defines = the
names other plan files import from it, statuses = the module's own
status literals. The derived contract is validated by the same
deterministic checks and pinned into the plan metadata exactly
like a declared one. These tests pin that behavior on a synthetic
mirror of the re-arm #2 shape — a new package whose __init__,
registry and endpoint modules are imported across nodes, plus the test
file that imports the package — and on the two incoherence shapes
derivation must still catch (an imported name the module never
defines; a status literal outside the implementation's vocabulary,
the proof-arm-3 failure).

The probe packages below exist only inside these plan payloads —
they are never written to disk.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.planner import parse_planner_response

ROOT = Path(__file__).resolve().parent.parent
GOAL = "Implement the pairing server endpoint as a new package and test it."
PKG = ROOT / "freetalon" / "probe_pairing"
TEST_FILE = ROOT / "tests" / "test_probe_pairing.py"
BARE_PKG = ROOT / "freetalon" / "probe_bare"

INIT = '''"""Probe pairing package."""

from freetalon.probe_pairing.endpoint import PairingServer
from freetalon.probe_pairing.registry import PairingStore

__all__ = ["PairingServer", "PairingStore"]
'''

STORE = '''"""Probe pairing store."""

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

SERVER = '''"""Probe pairing server."""

from freetalon.probe_pairing.registry import CodeNotFound, PairingStore


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

TESTS = '''"""Tests for the probe pairing package."""

import os
import tempfile
import unittest

from freetalon.probe_pairing import PairingServer, PairingStore
from freetalon.probe_pairing.registry import CodeNotFound


class TestProbePairing(unittest.TestCase):
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

DERIVED_CONTRACT = [
    {
        "module": "freetalon/probe_pairing/__init__.py",
        "defines": ["PairingServer", "PairingStore"],
        "statuses": [],
    },
    {
        "module": "freetalon/probe_pairing/endpoint.py",
        "defines": ["PairingServer"],
        "statuses": ["not-found"],
    },
    {
        "module": "freetalon/probe_pairing/registry.py",
        "defines": ["CodeNotFound", "PairingStore"],
        "statuses": ["pending"],
    },
]

CORE = '''"""Probe bare-import core module."""


class Core:
    """A thing made by the factory."""


def make_core() -> Core:
    """Create a Core."""
    return Core()
'''

CONSUMER = '''"""Probe bare-import consumer."""

import freetalon.probe_bare.core


def run():
    """Build via a bare module import (no names imported)."""
    return freetalon.probe_bare.core.make_core()
'''


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


def _pairing_nodes(tests: str = TESTS) -> list[dict]:
    return [
        _mkdir("step_1", PKG),
        _scaffold("step_2", PKG / "__init__.py", INIT, ["step_1"]),
        _scaffold("step_3", PKG / "registry.py", STORE, ["step_1"]),
        _scaffold("step_4", PKG / "endpoint.py", SERVER, ["step_3"]),
        _scaffold("step_5", TEST_FILE, tests, ["step_4"]),
    ]


def _dag(nodes: list[dict], contract: list | None = None) -> str:
    payload: dict = {"nodes": nodes, "metadata": {}}
    if contract is not None:
        payload["contract"] = contract
    return json.dumps(payload)


def _codes(error: LLMResponseError) -> set[str]:
    return {record.code for record in error.plan_errors}


def _rejection(payload: str, **kwargs) -> LLMResponseError:
    try:
        parse_planner_response(payload, goal_text=GOAL, **kwargs)
    except LLMResponseError as error:
        return error
    raise AssertionError("plan was accepted, expected a rejection")


class TestContractDerivation(unittest.TestCase):
    def test_from_scratch_plan_without_contract_accepted_and_derived(
        self,
    ) -> None:
        # The exact re-arm #2 shape: __init__ + store + server
        # imported across nodes, tests importing the package, and
        # NO "contract" key anywhere in the payload.
        nodes, metadata = parse_planner_response(
            _dag(_pairing_nodes()), goal_text=GOAL
        )
        self.assertEqual(len(nodes), 5)
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)

    def test_derived_contract_catches_undefined_imported_name(
        self,
    ) -> None:
        # The proof-arm-3 shape, now reachable: the tests import a
        # name the store never defines. Derivation puts the name
        # in the derived defines, and the defined-name check —
        # not contract_missing — names it.
        tests = TESTS.replace(
            "from freetalon.probe_pairing.registry import CodeNotFound",
            "from freetalon.probe_pairing.registry import CodeNotFound, Missing",
        )
        error = _rejection(_dag(_pairing_nodes(tests=tests)))
        self.assertIn("contract_incoherence", _codes(error))
        self.assertNotIn("contract_missing", _codes(error))
        self.assertIn("Missing", str(error))

    def test_derived_vocabulary_catches_foreign_status(self) -> None:
        # The implementation's vocabulary is pending / not-found;
        # a test asserting "gone" is the store/test divergence the
        # vocabulary check exists to catch.
        tests = TESTS.replace(
            '        self.assertEqual(result["status"], "not-found")',
            '        self.assertTrue(result["status"] == "gone")',
        )
        error = _rejection(_dag(_pairing_nodes(tests=tests)))
        self.assertIn("contract_incoherence", _codes(error))
        self.assertIn("gone", str(error))

    def test_partial_declaration_completed_by_derivation(self) -> None:
        # A declared entry governs verbatim; derivation fills only
        # the omitted modules.
        declared = [DERIVED_CONTRACT[2]]
        _nodes, metadata = parse_planner_response(
            _dag(_pairing_nodes(), contract=declared), goal_text=GOAL
        )
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)

    def test_derived_contract_pins_for_replan(self) -> None:
        # The derived contract pins like a declared one: a delta
        # plan with no contract of its own is validated against it
        # and carries it in metadata.
        _nodes, metadata = parse_planner_response(
            _dag(_pairing_nodes()),
            goal_text=GOAL,
            pinned_contract=DERIVED_CONTRACT,
        )
        self.assertEqual(metadata["module_contract"], DERIVED_CONTRACT)

    def test_bare_import_falls_back_to_public_defined_names(self) -> None:
        # A module another file only bare-imports contributes no
        # imported names; its derived defines are its content's
        # public defined names.
        nodes = [
            _mkdir("step_1", BARE_PKG),
            _scaffold("step_2", BARE_PKG / "core.py", CORE, ["step_1"]),
            _scaffold("step_3", BARE_PKG / "consumer.py", CONSUMER, ["step_2"]),
        ]
        _parsed, metadata = parse_planner_response(
            _dag(nodes), goal_text=GOAL
        )
        self.assertEqual(
            metadata["module_contract"],
            [
                {
                    "module": "freetalon/probe_bare/core.py",
                    "defines": ["Core", "make_core"],
                    "statuses": [],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
