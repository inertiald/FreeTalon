"""Module contract pinning (2026-11-20): one shared contract for
multi-module plans, pinned across re-plans.

The pairing proof arm's third run (run-20261120T083931Z-6c84e7) got
its plan accepted and executed, then failed 6 of its own 7 tests:
the store its plan wrote returned {"state": "not-found"} dicts
while its tests read a .state attribute, expected the goal's word
"gone", and demanded a CodeNotFound raise the store never raises.
Each file's interface was invented independently inside one
generation, and the re-plans — which never see the previous file
contents — regenerated the same incoherence, because nothing made
the failure feedback binding.

The mechanism these tests pin (planner.py, "Module contract
pinning"):

- a plan writing coupled NEW modules declares a top-level
  "contract" (per module: public "defines", the "statuses"
  vocabulary); parse validates the plan's contents against it
  deterministically — declared names defined, imported names
  declared, status literals inside the vocabulary — and since
  2026-11-23 the entries a plan omits are derived from the plan's
  own coupling at parse (_derive_contract_entries), so
  contract_missing survives only for a module no entry can be
  derived for;
- the validated contract is written into the plan metadata under
  "module_contract" (system-owned) and PINNED for the run: the
  loop seeds it into the re-plan brief, the brief renders it as
  binding, and replan_from_brief passes it back into parse, where
  a delta plan declaring a different contract is rejected
  (contract_changed) and its contents are validated against the
  pin.

The probe package below (freetalon/probe_contract/) exists only
inside these plan payloads — it is never written to disk.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.planner import (
    _format_brief_for_prompt,
    parse_planner_response,
    replan_from_brief,
)
from freetalon.orchestrator.prompts import (
    PLANNER_SYSTEM_PROMPT,
    REPLAN_SYSTEM_PROMPT,
)

ROOT = Path(__file__).resolve().parent.parent
GOAL = "Add a small two-module helper package and test it."
PKG = ROOT / "freetalon" / "probe_contract"
TEST_FILE = ROOT / "tests" / "test_probe_contract.py"

ALPHA = '''"""Probe alpha module: a tiny stateful thing factory."""


class ProbeError(Exception):
    """Raised when a probe operation cannot be completed."""


class Thing:
    """A probe thing with a two-state lifecycle."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.state = "pending"

    def finish(self) -> dict:
        """Mark the thing done and report its final state."""
        self.state = "done"
        return {"status": "done", "name": self.name}


def make_thing(name: str) -> Thing:
    """Create a pending Thing, rejecting empty names."""
    if not name:
        raise ProbeError("a thing needs a name")
    return Thing(name)
'''

BETA = '''"""Probe beta module: builds things through alpha."""

from freetalon.probe_contract.alpha import Thing, make_thing


def build(name: str) -> Thing:
    """Build one finished thing via alpha's factory."""
    thing = make_thing(name)
    thing.finish()
    return thing
'''

TESTS = '''"""Tests for the probe contract package."""

import unittest

from freetalon.probe_contract.alpha import ProbeError, Thing, make_thing
from freetalon.probe_contract.beta import build


class TestProbeContract(unittest.TestCase):
    def test_factory_rejects_empty_name(self) -> None:
        with self.assertRaises(ProbeError):
            make_thing("")

    def test_finish_reports_done(self) -> None:
        thing = make_thing("widget")
        self.assertIsInstance(thing, Thing)
        result = thing.finish()
        self.assertEqual(result["status"], "done")

    def test_build_returns_finished_thing(self) -> None:
        thing = build("gadget")
        self.assertEqual(thing.state, "done")


if __name__ == "__main__":
    unittest.main()
'''

CONTRACT = [
    {
        "module": "freetalon/probe_contract/alpha.py",
        "defines": ["ProbeError", "Thing", "finish", "make_thing"],
        "statuses": ["pending", "done"],
    },
    {
        "module": "freetalon/probe_contract/beta.py",
        "defines": ["build"],
        "statuses": [],
    },
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


def _probe_nodes(
    alpha: str = ALPHA, beta: str = BETA, tests: str = TESTS
) -> list[dict]:
    mkdir = {
        "id": "step_1",
        "objective": "Create the probe package directory",
        "depends_on": [],
        "assigned_claw": "bash",
        "inputs": {"command": f"mkdir -p {PKG}"},
        "outputs": ["result"],
        "criteria": [{"type": "exit_code", "equals": 0}],
    }
    return [
        mkdir,
        _scaffold(
            "step_2", PKG / "__init__.py",
            '"""Probe contract package."""\n', ["step_1"],
        ),
        _scaffold("step_3", PKG / "alpha.py", alpha, ["step_1"]),
        _scaffold("step_4", PKG / "beta.py", beta, ["step_3"]),
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


class TestContractValidation(unittest.TestCase):
    def test_coherent_plan_accepted_and_contract_pinned(self) -> None:
        nodes, metadata = parse_planner_response(
            _dag(_probe_nodes(), contract=CONTRACT), goal_text=GOAL
        )
        self.assertEqual(len(nodes), 5)
        self.assertEqual(metadata["module_contract"], CONTRACT)

    def test_coupled_new_modules_without_contract_get_derived_contract(
        self,
    ) -> None:
        # Derivation (2026-11-23): the declaration's skeleton
        # is system-owned now — a coherent coupled plan that
        # omits "contract" is accepted and the derived
        # contract is pinned. Alpha's derived defines are
        # exactly the names beta and the tests import from it;
        # its statuses are its own status literals.
        nodes, metadata = parse_planner_response(
            _dag(_probe_nodes()), goal_text=GOAL
        )
        self.assertEqual(len(nodes), 5)
        self.assertEqual(
            metadata["module_contract"],
            [
                {
                    "module": "freetalon/probe_contract/alpha.py",
                    "defines": ["ProbeError", "Thing", "make_thing"],
                    "statuses": ["done"],
                },
                {
                    "module": "freetalon/probe_contract/beta.py",
                    "defines": ["build"],
                    "statuses": [],
                },
            ],
        )

    def test_undeclared_import_rejected(self) -> None:
        tests = TESTS.replace(
            "from freetalon.probe_contract.alpha import ProbeError, Thing, make_thing",
            "from freetalon.probe_contract.alpha import (\n"
            "    ProbeError,\n"
            "    Thing,\n"
            "    make_thing,\n"
            "    vanish,\n"
            ")",
        ).replace(
            "    def test_build_returns_finished_thing(self) -> None:",
            "    def test_vanish_is_importable(self) -> None:\n"
            "        self.assertTrue(callable(vanish))\n\n"
            "    def test_build_returns_finished_thing(self) -> None:",
        )
        error = _rejection(
            _dag(_probe_nodes(tests=tests), contract=CONTRACT)
        )
        self.assertIn("contract_incoherence", _codes(error))
        self.assertIn("vanish", str(error))

    def test_declared_define_missing_from_module_rejected(self) -> None:
        contract = [dict(CONTRACT[0]), dict(CONTRACT[1])]
        contract[0] = dict(
            CONTRACT[0],
            defines=CONTRACT[0]["defines"] + ["explode"],
        )
        error = _rejection(_dag(_probe_nodes(), contract=contract))
        self.assertIn("contract_incoherence", _codes(error))
        self.assertIn("explode", str(error))

    def test_status_outside_vocabulary_rejected(self) -> None:
        # The synthetic mirror of the real failure: the goal's
        # vocabulary is pending/done; the module invents
        # "not-found" instead. The coherent twin above is accepted.
        alpha = ALPHA.replace(
            'return {"status": "done", "name": self.name}',
            'return {"status": "not-found", "name": self.name}',
        )
        error = _rejection(
            _dag(_probe_nodes(alpha=alpha), contract=CONTRACT)
        )
        self.assertIn("contract_incoherence", _codes(error))
        self.assertIn("not-found", str(error))

    def test_single_new_module_needs_no_contract(self) -> None:
        nodes = [
            _scaffold("step_1", PKG / "alpha.py", ALPHA, []),
        ]
        parsed, metadata = parse_planner_response(
            _dag(nodes), goal_text=GOAL
        )
        self.assertEqual(len(parsed), 1)
        self.assertNotIn("module_contract", metadata)

    def test_model_supplied_metadata_contract_is_not_trusted(self) -> None:
        payload = json.loads(_dag(_probe_nodes(), contract=CONTRACT))
        payload["metadata"] = {
            "module_contract": [
                {"module": "freetalon/evil.py", "defines": ["x"]}
            ]
        }
        _nodes, metadata = parse_planner_response(
            json.dumps(payload), goal_text=GOAL
        )
        self.assertEqual(metadata["module_contract"], CONTRACT)

    def test_malformed_contract_rejected(self) -> None:
        error = _rejection(
            _dag(_probe_nodes(), contract=[{"module": "alpha.py"}])
        )
        self.assertIn("contract_shape", _codes(error))


class TestPinnedContract(unittest.TestCase):
    def test_conforming_delta_accepted_under_pin(self) -> None:
        _nodes, metadata = parse_planner_response(
            _dag(_probe_nodes()),
            goal_text=GOAL,
            pinned_contract=CONTRACT,
        )
        self.assertEqual(metadata["module_contract"], CONTRACT)

    def test_changed_contract_rejected_under_pin(self) -> None:
        changed = [dict(CONTRACT[0]), dict(CONTRACT[1])]
        changed[0] = dict(
            CONTRACT[0], statuses=["pending", "done", "vanished"]
        )
        error = _rejection(
            _dag(_probe_nodes(), contract=changed),
            pinned_contract=CONTRACT,
        )
        self.assertIn("contract_changed", _codes(error))

    def test_delta_contents_validated_against_pin(self) -> None:
        alpha = ALPHA.replace(
            "def make_thing(name: str) -> Thing:",
            "def build_thing(name: str) -> Thing:",
        )
        beta = BETA.replace("make_thing", "build_thing")
        tests = TESTS.replace("make_thing", "build_thing")
        error = _rejection(
            _dag(_probe_nodes(alpha=alpha, beta=beta, tests=tests)),
            pinned_contract=CONTRACT,
        )
        self.assertIn("contract_incoherence", _codes(error))

    def test_delta_status_literal_validated_against_pin(self) -> None:
        tests = TESTS.replace(
            'self.assertEqual(result["status"], "done")',
            'self.assertTrue(result["status"] == "vanished")',
        )
        error = _rejection(
            _dag(_probe_nodes(tests=tests)),
            pinned_contract=CONTRACT,
        )
        self.assertIn("contract_incoherence", _codes(error))
        self.assertIn("vanished", str(error))


class TestBriefAndReplanPinning(unittest.TestCase):
    def test_brief_renders_pinned_contract(self) -> None:
        brief = {
            "plan_id": "plan-probe",
            "plan_version": 1,
            "goal": {"objective": GOAL},
            "failed_nodes": [],
            "module_contract": CONTRACT,
        }
        rendered = _format_brief_for_prompt(brief)
        self.assertIn("MODULE CONTRACT", rendered)
        self.assertIn("PINNED", rendered)
        self.assertIn("freetalon/probe_contract/alpha.py", rendered)
        self.assertIn("make_thing", rendered)

    def test_replan_rejects_changed_contract_then_conforms(self) -> None:
        changed = [dict(CONTRACT[0]), dict(CONTRACT[1])]
        changed[0] = dict(
            CONTRACT[0], statuses=["pending", "done", "vanished"]
        )
        bad = _dag(_probe_nodes(), contract=changed)
        good = _dag(_probe_nodes())
        brief = {
            "goal": {},
            "failed_nodes": [],
            "module_contract": CONTRACT,
        }
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[bad, good],
        ) as call_mock:
            nodes = replan_from_brief(brief)
        # The changed contract was rejected against the pin (one
        # repair round); without the pin it parses cleanly and the
        # re-plan would have returned after a single call.
        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(len(nodes), 5)
        repair_prompt = call_mock.call_args_list[1][0][1]
        self.assertIn("MODULE CONTRACT", repair_prompt)
        self.assertIn("PINNED", repair_prompt)
        self.assertIn("differs from this run's PINNED contract", repair_prompt)


class TestLoopSeedsPinIntoBrief(unittest.TestCase):
    def test_brief_carries_module_contract(self) -> None:
        from freetalon.orchestrator.loop import LoopRunner
        from freetalon.orchestrator.models import (
            ExecutionPlan,
            GoalContract,
            PlanNode,
            RunBudget,
            TaskIntent,
        )
        from freetalon.orchestrator.run_store import RunStore
        from freetalon.orchestrator.state_store import (
            ExecutionPlanStateStore,
        )
        from freetalon.orchestrator.tool_registry import (
            ToolRegistry,
            register_default_claws,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            runner = LoopRunner(
                store, registry, run_store=RunStore(Path(tmp) / "runs")
            )
            failing = PlanNode(
                id="node-1",
                objective="exit zero",
                assigned_claw="bash",
                inputs={"command": "exit 1"},
                criteria=[{"type": "exit_code", "equals": 0}],
                hypothesis="the command exits zero",
            )
            plan = ExecutionPlan(
                plan_id="plan-contract-pin",
                intent=TaskIntent(goal="contract pin demo"),
                nodes=[failing],
                metadata={"module_contract": CONTRACT},
            )
            store.save(plan)
            captured: dict = {}

            def replan_fn(brief):
                captured.update(brief)
                return [
                    PlanNode(
                        id="fix",
                        objective="write a marker file",
                        assigned_claw="bash",
                        inputs={"command": "echo fixed > fixed.txt"},
                        criteria=[
                            {"type": "file_exists", "path": "fixed.txt"}
                        ],
                        hypothesis="a marker file proves the step ran",
                        responds_to="node-1",
                        differs_by="writes an artifact instead of relying on exit code",
                    )
                ]

            contract = GoalContract(
                objective="contract pin demo",
                budget=RunBudget(max_replans=1),
            )
            result = runner.run_plan(plan, contract, replan_fn=replan_fn)
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(captured.get("module_contract"), CONTRACT)


class TestPromptsTeachContract(unittest.TestCase):
    def test_planner_prompt_teaches_module_contract(self) -> None:
        self.assertIn("MODULE CONTRACT", PLANNER_SYSTEM_PROMPT)
        self.assertIn('"contract"', PLANNER_SYSTEM_PROMPT)
        self.assertIn("PINNED", PLANNER_SYSTEM_PROMPT)

    def test_replan_prompt_teaches_pinned_contract(self) -> None:
        self.assertIn("MODULE CONTRACT — PINNED", REPLAN_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
