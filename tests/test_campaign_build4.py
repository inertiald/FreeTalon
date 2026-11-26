"""Campaign build #4: planner repair convergence.

Three mechanisms, each tested at the level it operates:

- plan-attempt anchoring: per-attempt plan-shape facts extracted
  from synthetic rejected outputs, the comparative DO-NOT-REGRESS
  constraints they produce, and the anchoring block + metadata
  trail the repair loop emits end to end;
- the existing-file inventory: computation over a synthetic tree,
  byte-stability across calls, and its presence in the planner
  prompt for a goal naming a spec in an inventoried package;
- the plan-repair budget: the named constant, its place as
  plan_task_intent's default, and the call count it produces.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import PlanStatus, TaskIntent
from freetalon.orchestrator.planner import (
    PLAN_TASK_INTENT_MAX_REPAIRS,
    PlanError,
    _attempt_anchoring_block,
    _attempt_anchoring_constraints,
    _plan_attempt_facts,
    plan_task_intent,
)
from freetalon.orchestrator.repo_index import (
    existing_file_inventory,
    format_existing_file_inventory,
)

CLAUSES = [{"id": "C01-store"}, {"id": "C02-server"}]

GOOD = json.dumps(
    {
        "nodes": [
            {
                "id": "verify",
                "objective": "Verify inputs",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "ls inputs"},
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
            {
                "id": "execute",
                "objective": "Execute rollout",
                "depends_on": ["verify"],
                "assigned_claw": "bash",
                "inputs": {"command": "deploy.sh"},
                "criteria": [{"type": "exit_code", "equals": 0}],
            },
        ],
        "metadata": {},
    }
)

BAD_UNKNOWN_CLAW = json.dumps(
    {
        "nodes": [
            {
                "id": "a",
                "objective": "Teleport the work",
                "depends_on": [],
                "assigned_claw": "teleport",
                "inputs": {},
                "criteria": [{"type": "exit_code", "equals": 0}],
            }
        ]
    }
)

BAD_SCHEMA = json.dumps(
    {
        "nodes": [
            {
                "id": "a",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {"command": "true"},
                "criteria": [{"type": "exit_code", "equals": 0}],
            }
        ]
    }
)


def _scaffold_node(node_id: str, target: str, content: str, clauses=None):
    node = {
        "id": node_id,
        "objective": f"Write {target}",
        "depends_on": [],
        "assigned_claw": "scaffold",
        "inputs": {"file_path": target, "content": content},
        "criteria": [{"type": "exit_code", "equals": 0}],
    }
    if clauses:
        node["inputs"]["clauses"] = clauses
    return node


def _payload(*nodes) -> str:
    return json.dumps({"nodes": list(nodes)})


class _TreeCase(unittest.TestCase):
    """A synthetic target tree: a pairing package + a tests dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "freetalon" / "pairing"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "server.py").write_text(
            "def serve():\n    pass\n", encoding="utf-8"
        )
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_pairing.py").write_text(
            "def helper():\n    pass\n\n"
            "class TestPairing:\n"
            "    def test_a(self):\n        pass\n"
            "    def test_b(self):\n        pass\n"
            "    def test_c(self):\n        pass\n"
            "    def test_d(self):\n        pass\n",
            encoding="utf-8",
        )


def _arm13_attempts(root) -> list:
    """The three arm-#13 attempt shapes as fact dicts, against a
    _TreeCase tree: scaffolds with no mappings; a NEW test file and
    no implementation; full implementation + full mappings that
    scaffold the existing test module."""
    attempt1 = _payload(
        _scaffold_node(
            "s1", "freetalon/pairing/store.py", "class Store:\n    pass\n"
        )
    )
    attempt2 = _payload(
        _scaffold_node(
            "s1",
            "tests/test_pairing_new.py",
            "def test_new():\n    pass\n",
        ),
        {
            "id": "s2",
            "objective": "Read the existing server",
            "depends_on": [],
            "assigned_claw": "bash",
            "inputs": {"command": "cat freetalon/pairing/server.py"},
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
    )
    attempt3 = _payload(
        _scaffold_node(
            "s1",
            "freetalon/pairing/server.py",
            "def serve():\n    pass\n",
            clauses={"implements": ["C01-store", "C02-server"]},
        ),
        _scaffold_node(
            "s2",
            "tests/test_pairing.py",
            "def test_x():\n    pass\n",
            clauses={"verifies": ["C01-store", "C02-server"]},
        ),
    )
    return [
        _plan_attempt_facts(
            attempt1,
            [PlanError(code="clause_coverage", message="m")],
            root,
            CLAUSES,
        ),
        _plan_attempt_facts(
            attempt2,
            [PlanError(code="clause_coverage", message="m")],
            root,
            CLAUSES,
        ),
        _plan_attempt_facts(
            attempt3,
            [PlanError(code="scaffold_overwrite", message="m")],
            root,
            CLAUSES,
        ),
    ]


class TestAttemptFacts(_TreeCase):
    """_plan_attempt_facts on arm-#13-shaped synthetic attempts."""

    def _attempts(self):
        return _arm13_attempts(self.root)

    def test_facts_describe_each_attempt(self) -> None:
        first, second, third = self._attempts()
        self.assertEqual(first["errors"], ["clause_coverage"])
        self.assertEqual(first["clauses_implemented"], 0)
        self.assertEqual(first["clauses_verified"], 0)
        self.assertEqual(first["clauses_total"], 2)
        self.assertEqual(
            first["implementation_writes"], ["freetalon/pairing/store.py"]
        )
        self.assertEqual(first["scaffolded_existing"], [])
        self.assertEqual(second["implementation_writes"], [])
        self.assertEqual(second["test_writes_new"], ["tests/test_pairing_new.py"])
        self.assertEqual(second["test_writes_existing"], [])
        self.assertEqual(third["clauses_implemented"], 2)
        self.assertEqual(third["clauses_verified"], 2)
        self.assertEqual(
            third["implementation_writes"], ["freetalon/pairing/server.py"]
        )
        self.assertEqual(third["test_writes_existing"], ["tests/test_pairing.py"])
        self.assertEqual(
            third["scaffolded_existing"],
            ["freetalon/pairing/server.py", "tests/test_pairing.py"],
        )

    def test_facts_are_fail_soft(self) -> None:
        facts = _plan_attempt_facts(
            "this is not json",
            [PlanError(code="node_schema", message="m")],
            self.root,
            CLAUSES,
        )
        self.assertEqual(facts["errors"], ["node_schema"])
        self.assertEqual(facts["implementation_writes"], [])
        self.assertIsNone(facts["clauses_implemented"])

    def test_invented_clause_ids_do_not_count(self) -> None:
        payload = _payload(
            _scaffold_node(
                "s1",
                "freetalon/pairing/store.py",
                "x = 1\n",
                clauses={"implements": ["C99-invented"]},
            )
        )
        facts = _plan_attempt_facts(payload, [], self.root, CLAUSES)
        self.assertEqual(facts["clauses_implemented"], 0)


class TestAnchoringConstraints(_TreeCase):
    def _history(self):
        return _arm13_attempts(self.root)

    def test_arm13_history_names_the_test_file_regression(self) -> None:
        attempts = self._history()
        constraints = _attempt_anchoring_constraints(attempts)
        joined = "\n".join(constraints)
        # Attempt 3 was the clause peak and held the implementation
        # writes: no clause or implementation constraint may fire.
        self.assertNotIn("Clause implementation coverage peaked", joined)
        self.assertNotIn("wrote no implementation files", joined)
        # It scaffolded the existing test module attempt 2 had
        # avoided, and introduced a code no earlier attempt had.
        self.assertIn("NEW", joined)
        self.assertIn("tests/test_pairing.py", joined)
        self.assertIn("scaffold_overwrite", joined)

    def test_clause_regression_is_named_with_its_peak(self) -> None:
        attempts = self._history()
        regressed = [attempts[2], attempts[0]]
        joined = "\n".join(_attempt_anchoring_constraints(regressed))
        self.assertIn("Clause implementation coverage peaked at 2/2", joined)
        self.assertIn("attempt 1", joined)
        self.assertIn("Clause verification coverage peaked at 2/2", joined)

    def test_implementation_write_regression_is_named(self) -> None:
        attempts = self._history()
        regressed = [attempts[2], attempts[1]]
        joined = "\n".join(_attempt_anchoring_constraints(regressed))
        self.assertIn("wrote no implementation files", joined)
        self.assertIn("freetalon/pairing/server.py", joined)

    def test_monotone_history_has_no_constraints(self) -> None:
        attempts = self._history()
        first = dict(attempts[0])
        third = dict(attempts[2])
        third["errors"] = ["clause_coverage"]
        third["test_writes_existing"] = []
        third["scaffolded_existing"] = []
        self.assertEqual(
            _attempt_anchoring_constraints([first, third]), []
        )

    def test_block_shape(self) -> None:
        attempts = self._history()
        self.assertEqual(_attempt_anchoring_block(attempts[:1]), "")
        block = _attempt_anchoring_block(attempts)
        self.assertIn("PLAN-ATTEMPT ANCHORING", block)
        self.assertIn("Attempt 1", block)
        self.assertIn("Attempt 3", block)
        self.assertIn("DO NOT REGRESS:", block)
        self.assertIn("satisfy ALL the validators at once", block)


class TestAnchoringEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.intent = TaskIntent(
            goal="Write and verify a script", project_type="script"
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_repair_prompts_carry_anchoring_and_metadata_records_it(self) -> None:
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[BAD_UNKNOWN_CLAW, BAD_SCHEMA, GOOD],
        ) as call_mock:
            plan = plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )
        prompts = [call.args[1] for call in call_mock.call_args_list]
        self.assertEqual(len(prompts), 3)
        # One attempt in history: no anchoring block yet.
        self.assertNotIn("PLAN-ATTEMPT ANCHORING", prompts[1])
        # Two attempts: the block rides the next repair prompt.
        self.assertIn("PLAN-ATTEMPT ANCHORING", prompts[2])
        record = plan.metadata["attempt_anchoring"]
        self.assertEqual(len(record["attempts"]), 2)
        self.assertIn("unknown_claw", record["attempts"][0]["errors"])

    def test_first_attempt_acceptance_has_no_anchoring_record(self) -> None:
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=GOOD
        ):
            plan = plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )
        self.assertNotIn("attempt_anchoring", plan.metadata)


class TestInventory(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "freetalon"
        (package / "pkg").mkdir(parents=True)
        (package / "api.py").write_text(
            "def alpha():\n    pass\n\n"
            "class Beta:\n"
            "    def gamma(self):\n        pass\n",
            encoding="utf-8",
        )
        (package / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (package / "pkg" / "mod.py").write_text(
            "def one():\n    pass\n\ndef two():\n    pass\n",
            encoding="utf-8",
        )
        (package / "pkg" / "mod.py.bak").write_text(
            "def stale():\n    pass\n", encoding="utf-8"
        )
        tests = self.root / "tests"
        (tests / "nested").mkdir(parents=True)
        (tests / "__pycache__").mkdir()
        (tests / "test_a.py").write_text(
            "def test_one():\n    pass\n\n"
            "class TestB:\n"
            "    def test_two(self):\n        pass\n",
            encoding="utf-8",
        )
        (tests / "nested" / "test_b.py").write_text(
            "def test_three():\n    pass\n", encoding="utf-8"
        )
        (tests / "__pycache__" / "cached.py").write_text(
            "def cached():\n    pass\n", encoding="utf-8"
        )
        (self.root / "outside.py").write_text(
            "def outside():\n    pass\n", encoding="utf-8"
        )

    def test_inventory_scopes_and_counts(self) -> None:
        entries = existing_file_inventory(self.root, ["freetalon/pkg"])
        self.assertEqual(
            entries,
            [
                ("freetalon/api.py", 3),
                ("freetalon/pkg/__init__.py", 0),
                ("freetalon/pkg/mod.py", 2),
                ("tests/nested/test_b.py", 1),
                ("tests/test_a.py", 3),
            ],
        )

    def test_inventory_is_byte_stable(self) -> None:
        first = existing_file_inventory(self.root, ["freetalon/pkg"])
        second = existing_file_inventory(self.root, ["freetalon/pkg"])
        self.assertEqual(first, second)
        self.assertEqual(
            format_existing_file_inventory(first),
            format_existing_file_inventory(second),
        )

    def test_inventory_block_renders_the_standing_rule(self) -> None:
        block = format_existing_file_inventory(
            existing_file_inventory(self.root, ["freetalon/pkg"])
        )
        self.assertIn("EXISTING FILES", block)
        self.assertIn("NEVER scaffold an existing file", block)
        self.assertIn("- tests/test_a.py (3 definitions)", block)
        self.assertIn("- freetalon/api.py (3 definitions)", block)

    def test_empty_tree_has_no_block(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(existing_file_inventory(empty), [])
        self.assertEqual(format_existing_file_inventory([]), "")


class TestInventoryInPrompt(_TreeCase):
    def test_planner_prompt_carries_the_inventory(self) -> None:
        contract = self.root / "freetalon" / "pairing" / "CONTRACT.md"
        contract.write_text(
            "Pairing server contract. No section headings: the "
            "preamble is framing, not obligations.\n",
            encoding="utf-8",
        )
        intent = TaskIntent(
            goal=(
                "Implement the pairing server described in "
                "freetalon/pairing/CONTRACT.md and verify it."
            ),
            project_type="script",
        )
        settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        with patch(
            "freetalon.orchestrator.planner.call_llm", return_value=GOOD
        ) as call_mock:
            plan_task_intent(intent, settings=settings, repo_root=self.root)
        prompt = call_mock.call_args_list[0].args[1]
        self.assertIn("EXISTING FILES", prompt)
        # tests/test_pairing.py above defines helper + TestPairing
        # + its four test methods = 6 qualified definitions.
        self.assertIn("- tests/test_pairing.py (6 definitions)", prompt)
        self.assertIn("- freetalon/pairing/server.py (1 definitions)", prompt)


class TestRepairBudget(unittest.TestCase):
    def setUp(self) -> None:
        self.intent = TaskIntent(
            goal="Write and verify a script", project_type="script"
        )
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_budget_constant_is_the_default(self) -> None:
        self.assertEqual(PLAN_TASK_INTENT_MAX_REPAIRS, 4)
        default = inspect.signature(plan_task_intent).parameters[
            "max_repairs"
        ].default
        self.assertEqual(default, PLAN_TASK_INTENT_MAX_REPAIRS)

    def test_default_budget_allows_five_attempts(self) -> None:
        bads = [
            BAD_UNKNOWN_CLAW,
            BAD_SCHEMA,
            BAD_UNKNOWN_CLAW.replace('"a"', '"b"'),
            BAD_SCHEMA.replace('"a"', '"b"'),
            BAD_UNKNOWN_CLAW.replace('"a"', '"c"'),
        ]
        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=bads
        ) as call_mock:
            plan = plan_task_intent(
                self.intent, settings=self.settings, repo_root=self.root
            )
        # Build #26: the exhausted budget returns a FAILED plan
        # carrying the planning_failure stamp; nothing raises.
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertIn("planning_failure", plan.metadata)
        self.assertEqual(call_mock.call_count, 5)

    def test_explicit_budget_still_governs(self) -> None:
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=[BAD_UNKNOWN_CLAW, BAD_SCHEMA, BAD_UNKNOWN_CLAW],
        ) as call_mock:
            plan = plan_task_intent(
                self.intent,
                settings=self.settings,
                repo_root=self.root,
                max_repairs=1,
            )
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertEqual(call_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
