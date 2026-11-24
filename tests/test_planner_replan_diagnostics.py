"""Re-plan diagnostics (2026-11-23): the suspect map + precedence rule.

Exam re-arm #8: the same three tests failed in all four plan
versions; every re-plan rewrote store.py while the defects sat in
server.py (never revised) and in a produced test that contradicted
the contract. The re-plan prompt now carries a system-computed
diagnostics block — each failing test's own source, the produced
modules it exercises, and the precedence rule (the spec, or without
one the goal, outranks a produced test) — and the suspect map is
stamped back into the brief for the judge's stagnation gate.

call_llm is mocked throughout: no GPU is touched. Fixtures are
synthetic trees in temp dirs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMSettings

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

CONTRACT_TEXT = "# Pairing contract (fixture)\n\nThe contract governs.\n"

STORE_TEXT = "class CodeConflict(Exception):\n    pass\n"

SERVER_TEXT = "def make_server():\n    return None\n"

TEST_TEXT = '''import unittest

from pkg import store


class PairingStoreTests(unittest.TestCase):
    def test_re_register_claimed_noop(self):
        with self.assertRaises(store.CodeConflict):
            raise store.CodeConflict()
'''

FAILING_ID = (
    "tests.test_pairing.PairingStoreTests.test_re_register_claimed_noop"
)
PYTEST_ID = "tests/test_pairing.py::PairingStoreTests::test_re_register_claimed_noop"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PairingTreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write(self.root / "CONTRACT.md", CONTRACT_TEXT)
        _write(self.root / "pkg" / "__init__.py", "")
        _write(self.root / "pkg" / "store.py", STORE_TEXT)
        _write(self.root / "pkg" / "server.py", SERVER_TEXT)
        _write(self.root / "tests" / "test_pairing.py", TEST_TEXT)

    def _brief(self, **overrides):
        brief = {
            "failing_tests": [FAILING_ID],
            "produced_files": ["pkg/store.py", "pkg/server.py"],
        }
        brief.update(overrides)
        return brief


class TestSuspectMap(PairingTreeCase):
    def test_block_names_source_modules_and_spec_precedence(self):
        block, suspect_map = planner_module._replan_diagnostics(
            self._brief(),
            self.root,
            has_spec=True,
            spec_paths=["CONTRACT.md"],
        )
        # The failing test's own source reaches the block.
        self.assertIn("with self.assertRaises(store.CodeConflict):", block)
        self.assertIn("defined in tests/test_pairing.py", block)
        # The test exercises the store it imports — not the server.
        entry = suspect_map[FAILING_ID]
        self.assertEqual(entry["file"], "tests/test_pairing.py")
        self.assertEqual(entry["modules"], ["pkg/store.py"])
        self.assertIn("exercises produced modules: pkg/store.py", block)
        self.assertNotIn("pkg/server.py", block)
        # The precedence rule, spec form.
        self.assertIn(
            "Precedence (system): CONTRACT.md is the authoritative", block
        )
        self.assertIn("fix the test, not the code", block)
        self.assertIn("the module AND the test are both suspects", block)

    def test_generic_precedence_without_spec(self):
        block, _ = planner_module._replan_diagnostics(
            self._brief(), self.root, has_spec=False
        )
        self.assertIn("no external spec governs this run", block)
        self.assertIn("the goal text governs", block)
        self.assertIn("fix the test, not the code", block)

    def test_empty_without_failing_tests(self):
        block, suspect_map = planner_module._replan_diagnostics(
            {"produced_files": ["pkg/store.py"]}, self.root, has_spec=False
        )
        self.assertEqual(block, "")
        self.assertEqual(suspect_map, {})

    def test_pytest_nodeid_located(self):
        block, suspect_map = planner_module._replan_diagnostics(
            self._brief(failing_tests=[PYTEST_ID]),
            self.root,
            has_spec=False,
        )
        entry = suspect_map[PYTEST_ID]
        self.assertEqual(entry["file"], "tests/test_pairing.py")
        self.assertEqual(entry["modules"], ["pkg/store.py"])
        self.assertIn("assertRaises", block)

    def test_unlocatable_test_reported(self):
        block, suspect_map = planner_module._replan_diagnostics(
            {"failing_tests": ["zzz.test_missing_thing"]},
            self.root,
            has_spec=False,
        )
        self.assertIn("not located", block)
        self.assertIsNone(suspect_map["zzz.test_missing_thing"]["file"])
        self.assertEqual(suspect_map["zzz.test_missing_thing"]["modules"], [])


class TestBriefRendering(unittest.TestCase):
    def _brief(self, **overrides):
        brief = {
            "goal": {"objective": "g"},
            "plan_id": "p1",
            "plan_version": 3,
            "failed_nodes": [],
            "measurements": [],
            "memory": [],
            "remaining_budget": {},
            "instruction": "",
        }
        brief.update(overrides)
        return brief

    def test_diagnostics_rendered_when_stamped(self):
        text = planner_module._format_brief_for_prompt(
            self._brief(
                failing_tests=[FAILING_ID],
                stagnation={
                    "failing_tests": [FAILING_ID],
                    "unchanged_since_version": 1,
                    "last_change_files": ["pkg/store.py"],
                },
                never_revised_files=["pkg/server.py"],
                missing_declared_files=["pkg/__main__.py"],
            )
        )
        self.assertIn("Failing tests (named by the system", text)
        self.assertIn(FAILING_ID, text)
        self.assertIn("Stagnation (system): the failing tests above "
                      "are IDENTICAL", text)
        self.assertIn("plan version 1", text)
        self.assertIn("The last change modified: pkg/store.py", text)
        self.assertIn("did not revise: pkg/server.py", text)
        self.assertIn("missing on disk", text)
        self.assertIn("pkg/__main__.py", text)

    def test_no_stagnation_line_when_set_not_stamped(self):
        text = planner_module._format_brief_for_prompt(
            self._brief(failing_tests=[FAILING_ID])
        )
        self.assertIn("Failing tests (named by the system", text)
        self.assertNotIn("Stagnation (system)", text)
        self.assertNotIn("did not revise", text)


class TestReplanEndToEnd(PairingTreeCase):
    def test_replan_prompt_carries_diagnostics_and_stamps_map(self):
        # Campaign build #2: the mock delta writes a NEW module —
        # a whole-file rewrite of the existing store.py is now a
        # plan error (the modify-path rule) unless it carries the
        # gated override; this test's subject is the diagnostics
        # and the suspect map, not the write's shape.
        node = {
            "id": "step_1",
            "objective": "Write the store implementation module",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": str(self.root / "pkg" / "store_impl.py"),
                "content": "x = 1\n",
            },
            "outputs": [str(self.root / "pkg" / "store_impl.py")],
            "criteria": [
                {
                    "type": "file_exists",
                    "path": str(self.root / "pkg" / "store_impl.py"),
                }
            ],
            "responds_to": "node-1",
            "differs_by": "rewrite per the contract",
        }
        brief = {
            "goal": {
                "objective": "Implement the pairing service per CONTRACT.md."
            },
            "plan_id": "p1",
            "plan_version": 2,
            "failed_nodes": [],
            "measurements": [],
            "memory": [],
            "remaining_budget": {},
            "failing_tests": [FAILING_ID],
            "produced_files": ["pkg/store.py", "pkg/server.py"],
            "workspace": str(self.root),
        }
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return json.dumps({"nodes": [node]})

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            nodes = planner_module.replan_from_brief(
                brief, settings=SETTINGS, repo_root=self.root
            )
        self.assertEqual(len(nodes), 1)
        prompt = calls[0][1]
        self.assertIn("Re-plan diagnostics (system", prompt)
        self.assertIn(
            "Precedence (system): CONTRACT.md is the authoritative", prompt
        )
        self.assertIn("Failing test " + FAILING_ID, prompt)
        # The suspect map rode back into the brief for the judge.
        self.assertEqual(
            brief["suspect_map"][FAILING_ID]["modules"], ["pkg/store.py"]
        )
        self.assertEqual(
            brief["suspect_map"][FAILING_ID]["file"], "tests/test_pairing.py"
        )


if __name__ == "__main__":
    unittest.main()
