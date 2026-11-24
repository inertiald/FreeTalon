"""Scoped-edit repair (architecture campaign step 1, 2026-11-23).

Exam re-arms #7-#9: whole-task re-plans rewrite whole files, and
the rewrites oscillate — a version's server.py passes a test the
next version's rewrite breaks again. The scoped-edit seam answers
with the opposite primitive: for the ONE produced file the failing
tests exercise, the model returns Aider-style search/replace
blocks and the harness applies them mechanically (exact match,
exactly-once, no overlaps, all-or-nothing), so everything outside
the blocks is preserved by construction.

Two layers are pinned here: the pure machinery
(freetalon.orchestrator.scoped_edits — parse + apply + syntax
check) and the planner seam (try_scoped_edit_repair) against a
synthetic tree, with call_llm mocked throughout: no GPU is touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMSettings
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from freetalon.orchestrator.scoped_edits import (
    ScopedEdit,
    ScopedEditError,
    apply_scoped_edits,
    parse_scoped_edits,
    python_syntax_error,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)


def _block(search: str, replace: str) -> str:
    return (
        "<<<<<<< SEARCH\n"
        f"{search}\n"
        "=======\n"
        f"{replace}\n"
        ">>>>>>> REPLACE\n"
    )


class TestParseScopedEdits(unittest.TestCase):
    def test_single_block(self):
        edits = parse_scoped_edits(_block("old line", "new line"))
        self.assertEqual(
            edits, [ScopedEdit(search="old line", replace="new line")]
        )

    def test_multiple_blocks_with_prose_between(self):
        text = (
            "Here is the fix:\n"
            + _block("aaa", "bbb")
            + "and the second:\n"
            + _block("ccc", "ddd")
        )
        edits = parse_scoped_edits(text)
        self.assertEqual(
            edits,
            [
                ScopedEdit(search="aaa", replace="bbb"),
                ScopedEdit(search="ccc", replace="ddd"),
            ],
        )

    def test_multiline_texts_preserved_verbatim(self):
        edits = parse_scoped_edits(
            _block("def f():\n    return 1", "def f():\n    return 2")
        )
        self.assertEqual(edits[0].search, "def f():\n    return 1")
        self.assertEqual(edits[0].replace, "def f():\n    return 2")

    def test_no_blocks_is_an_empty_list(self):
        self.assertEqual(parse_scoped_edits("the whole file, rewritten"), [])
        self.assertEqual(parse_scoped_edits(""), [])

    def test_search_without_divider_is_malformed(self):
        self.assertIsNone(
            parse_scoped_edits("<<<<<<< SEARCH\nfoo\n>>>>>>> REPLACE\n")
        )

    def test_search_without_replace_marker_is_malformed(self):
        self.assertIsNone(
            parse_scoped_edits("<<<<<<< SEARCH\nfoo\n=======\nbar\n")
        )

    def test_unknown_conflict_marker_is_malformed(self):
        self.assertIsNone(
            parse_scoped_edits("<<<<<<< FOO\nfoo\n=======\nbar\n>>>>>>> REPLACE\n")
        )

    def test_one_malformed_block_voids_the_response(self):
        text = _block("aaa", "bbb") + "<<<<<<< SEARCH\nccc\n"
        self.assertIsNone(parse_scoped_edits(text))


class TestApplyScopedEdits(unittest.TestCase):
    CONTENT = "alpha\nbeta\ngamma\ndelta\n"

    def test_clean_single_apply(self):
        result = apply_scoped_edits(
            self.CONTENT, [ScopedEdit(search="beta", replace="BETA")]
        )
        self.assertEqual(result, "alpha\nBETA\ngamma\ndelta\n")

    def test_multiple_blocks_apply(self):
        result = apply_scoped_edits(
            self.CONTENT,
            [
                ScopedEdit(search="beta", replace="BETA"),
                ScopedEdit(search="delta", replace="DELTA"),
            ],
        )
        self.assertEqual(result, "alpha\nBETA\ngamma\nDELTA\n")

    def test_adjacent_spans_do_not_overlap(self):
        result = apply_scoped_edits(
            "abcdef",
            [
                ScopedEdit(search="abc", replace="x"),
                ScopedEdit(search="def", replace="y"),
            ],
        )
        self.assertEqual(result, "xy")

    def test_non_matching_search_rejected(self):
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(
                self.CONTENT, [ScopedEdit(search="omega", replace="x")]
            )

    def test_search_occurring_twice_rejected(self):
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(
                "beta\nbeta\n", [ScopedEdit(search="beta", replace="x")]
            )

    def test_overlapping_blocks_rejected(self):
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(
                "abcdef",
                [
                    ScopedEdit(search="abc", replace="x"),
                    ScopedEdit(search="cde", replace="y"),
                ],
            )

    def test_empty_search_rejected(self):
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(
                self.CONTENT, [ScopedEdit(search="", replace="x")]
            )

    def test_noop_edit_rejected(self):
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(
                self.CONTENT, [ScopedEdit(search="beta", replace="beta")]
            )

    def test_empty_edit_set_rejected(self):
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(self.CONTENT, [])

    def test_all_or_nothing_when_a_later_block_fails(self):
        # The first block would apply cleanly; the set still fails
        # whole — no partial result exists to observe.
        with self.assertRaises(ScopedEditError):
            apply_scoped_edits(
                self.CONTENT,
                [
                    ScopedEdit(search="beta", replace="BETA"),
                    ScopedEdit(search="omega", replace="x"),
                ],
            )


class TestPythonSyntaxError(unittest.TestCase):
    def test_valid_python(self):
        self.assertIsNone(python_syntax_error("def f():\n    return 1\n"))

    def test_broken_python_reports(self):
        self.assertIsNotNone(python_syntax_error("def f(:\n    return 1\n"))


# ── The seam, against a synthetic tree ─────────────────────────────

STORE_TEXT = '''"""Fixture store."""

CONSTANT = 42


def helper():
    return CONSTANT


def register(name):
    # DEFECT: returns the name unchanged instead of a code.
    return name


def unregister(name):
    return None
'''

FIXED_REGISTER = '''def register(name):
    return "code-" + name'''

DEFECTIVE_REGISTER = '''def register(name):
    # DEFECT: returns the name unchanged instead of a code.
    return name'''

TEST_TEXT = '''import unittest

from pkg import store


class StoreTests(unittest.TestCase):
    def test_register_returns_code(self):
        self.assertEqual(store.register("alpha"), "code-alpha")
'''

SERVER_TEXT = "def make_server():\n    return None\n"

SERVER_TEST_TEXT = '''import unittest

from pkg import server


class ServerTests(unittest.TestCase):
    def test_make_server(self):
        self.assertIsNotNone(server.make_server())
'''

GOAL = (
    "Build a small fixture package whose store register function "
    "returns a code string for a name."
)

FAILING_ID = "tests.test_store.StoreTests.test_register_returns_code"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ScopedEditSeamCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store_path = self.root / "pkg" / "store.py"
        self.test_path = self.root / "tests" / "test_store.py"
        _write(self.root / "pkg" / "__init__.py", "")
        _write(self.store_path, STORE_TEXT)
        _write(self.test_path, TEST_TEXT)

    def _nodes(self, with_server: bool = False) -> list[PlanNode]:
        nodes = [
            PlanNode(
                id="node-1",
                objective="Write the fixture store module",
                hypothesis="the store computes codes",
                assigned_claw="scaffold",
                inputs={
                    "file_path": str(self.store_path),
                    "content": STORE_TEXT,
                },
                outputs=[str(self.store_path)],
                acceptance=["The module exists."],
                criteria=[
                    {"type": "file_exists", "path": str(self.store_path)}
                ],
                depends_on=[],
                status=PlanStatus.COMPLETED,
            ),
            PlanNode(
                id="node-2",
                objective="Write the store test",
                hypothesis="the test measures register",
                assigned_claw="file_writer",
                inputs={
                    "file_path": str(self.test_path),
                    "content": TEST_TEXT,
                },
                outputs=[str(self.test_path)],
                acceptance=["The test exists."],
                criteria=[
                    {"type": "file_exists", "path": str(self.test_path)}
                ],
                depends_on=["node-1"],
                status=PlanStatus.COMPLETED,
            ),
            PlanNode(
                id="node-3",
                objective="Run the store test",
                hypothesis="register returns a code",
                assigned_claw="bash",
                inputs={"command": f"./.venv/bin/python {self.test_path}"},
                outputs=[],
                acceptance=["The test passes."],
                criteria=[{"type": "exit_code", "expected": 0}],
                depends_on=["node-2"],
                status=PlanStatus.FAILED,
                error="exit_code 1 != 0",
                exit_code=1,
                measurement={
                    "verdict": "FAIL",
                    "results": [
                        {
                            "criterion": {"type": "exit_code", "expected": 0},
                            "detail": "exit_code 1 != 0",
                            "measured": True,
                            "passed": False,
                        }
                    ],
                    "exit_code": 1,
                },
            ),
        ]
        if with_server:
            server_path = self.root / "pkg" / "server.py"
            server_test_path = self.root / "tests" / "test_server.py"
            _write(server_path, SERVER_TEXT)
            _write(server_test_path, SERVER_TEST_TEXT)
            nodes.append(
                PlanNode(
                    id="node-4",
                    objective="Write the fixture server module",
                    hypothesis="the server constructs",
                    assigned_claw="scaffold",
                    inputs={
                        "file_path": str(server_path),
                        "content": SERVER_TEXT,
                    },
                    outputs=[str(server_path)],
                    acceptance=["The module exists."],
                    criteria=[
                        {"type": "file_exists", "path": str(server_path)}
                    ],
                    depends_on=[],
                    status=PlanStatus.COMPLETED,
                )
            )
        return nodes

    def _plan(self, nodes: list[PlanNode]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-scoped-edit",
            intent=TaskIntent(
                goal=GOAL, constraints={"workspace": str(self.root)}
            ),
            nodes=nodes,
            metadata={"repo_root": str(self.root)},
        )

    def _brief(self, **overrides) -> dict:
        brief = {
            "goal": {"objective": GOAL},
            "plan_id": "plan-scoped-edit",
            "plan_version": 1,
            "failing_tests": [FAILING_ID],
            "produced_files": ["pkg/store.py", "tests/test_store.py"],
            "failed_nodes": [
                {
                    "id": "node-3",
                    "diagnostics": (
                        "AssertionError: 'alpha' != 'code-alpha'"
                    ),
                }
            ],
            "measurements": [],
            "past_experiments": [],
            "remaining_budget": {"replans": 2},
        }
        brief.update(overrides)
        return brief

    def _run(self, response: str, nodes=None, brief=None):
        nodes = self._nodes() if nodes is None else nodes
        plan = self._plan(nodes)
        calls: list[tuple[str, str]] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            calls.append((system_prompt, user_prompt))
            return response

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan,
                self._brief() if brief is None else brief,
                SETTINGS,
            )
        return result, calls


class TestScopedEditSeam(ScopedEditSeamCase):
    # --- The happy path: only the targeted region changes ---

    def test_repair_changes_only_the_targeted_region(self):
        result, calls = self._run(_block(DEFECTIVE_REGISTER, FIXED_REGISTER))
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)
        system_prompt, user_prompt = calls[0]
        self.assertIn("scoped-edit repairer", system_prompt)
        self.assertIn(f"THE TASK IS UNCHANGED: {GOAL}", user_prompt)
        # The file as it stands and the failing test reach the ask.
        self.assertIn(STORE_TEXT.rstrip("\n"), user_prompt)
        self.assertIn(FAILING_ID, user_prompt)
        self.assertIn("Return ONLY search/replace edit blocks", user_prompt)
        # The candidate: node-1's content is the file with exactly
        # the register definition replaced.
        expected = STORE_TEXT.replace(DEFECTIVE_REGISTER, FIXED_REGISTER)
        self.assertNotEqual(expected, STORE_TEXT)
        by_id = {node.id: node for node in result}
        new_content = by_id["node-1"].inputs["content"]
        self.assertEqual(new_content, expected)
        # Untouched regions are byte-identical, not merely similar:
        # everything before the replaced region and after it.
        head = STORE_TEXT[: STORE_TEXT.index(DEFECTIVE_REGISTER)]
        tail = STORE_TEXT[
            STORE_TEXT.index(DEFECTIVE_REGISTER) + len(DEFECTIVE_REGISTER):
        ]
        self.assertTrue(new_content.startswith(head))
        self.assertTrue(new_content.endswith(tail))
        self.assertIn("def helper():", new_content)
        # The repair is recorded as the system's own note.
        self.assertIn(
            "scoped-edit repair", by_id["node-1"].differs_by or ""
        )
        self.assertEqual(by_id["node-1"].responds_to, "node-3")
        # Every node is preserved in substance, state reset.
        originals = {node.id: node for node in self._nodes()}
        for node_id in ("node-2", "node-3"):
            candidate, original = by_id[node_id], originals[node_id]
            self.assertEqual(candidate.objective, original.objective)
            self.assertEqual(candidate.inputs, original.inputs)
        for node in result:
            self.assertEqual(node.status, PlanStatus.DRAFT)
            self.assertEqual(node.measurement, {})
            self.assertIsNone(node.error)

    # --- Fallbacks: any doubt returns None, no candidate ---

    def test_whole_file_answer_declines(self):
        # The failure mode this seam exists to refuse: the model
        # re-emits the entire corrected file instead of edit blocks.
        # Campaign build #13: a no-blocks answer earns one format
        # re-ask inside the same invocation (the stub answers the
        # whole file again), so the decline now follows TWO calls;
        # this assertion pinned the pre-#13 single call.
        whole = STORE_TEXT.replace(DEFECTIVE_REGISTER, FIXED_REGISTER)
        result, calls = self._run(whole)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)

    def test_non_matching_search_text_declines(self):
        result, _calls = self._run(
            _block("def register(name):\n    return 'code-'", FIXED_REGISTER)
        )
        self.assertIsNone(result)

    def test_syntax_breaking_edit_declines(self):
        result, _calls = self._run(
            _block(
                DEFECTIVE_REGISTER,
                "def register(name:\n    return 'code-' + name",
            )
        )
        self.assertIsNone(result)

    def test_two_suspect_modules_attempt_a_pair_repair(self):
        # Campaign build #2 superseded this test's original shape
        # ("two suspect modules decline before any call"): a span
        # of exactly two files is now ONE pair candidate. The seam
        # calls the model once with the pair prompt; a bare
        # single-file response (no FILE: sections) cannot apply to
        # a pair, so the seam still declines — after the call.
        nodes = self._nodes(with_server=True)
        brief = self._brief(
            failing_tests=[
                FAILING_ID,
                "tests.test_server.ServerTests.test_make_server",
            ],
            produced_files=[
                "pkg/store.py",
                "pkg/server.py",
                "tests/test_store.py",
                "tests/test_server.py",
            ],
        )
        result, calls = self._run(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER),
            nodes=nodes,
            brief=brief,
        )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("PAIR FORM", calls[0][0])

    def test_no_failing_tests_declines_before_any_call(self):
        result, calls = self._run(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER),
            brief=self._brief(failing_tests=[]),
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_disk_drift_declines_before_any_call(self):
        # A later hand edit changed the file on disk: the culprit
        # node is no longer the file's current author.
        _write(
            self.store_path,
            STORE_TEXT.replace("CONSTANT = 42", "CONSTANT = 43"),
        )
        result, calls = self._run(_block(DEFECTIVE_REGISTER, FIXED_REGISTER))
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_two_writers_of_the_suspect_decline(self):
        nodes = self._nodes()
        nodes.insert(
            1,
            PlanNode(
                id="node-1b",
                objective="Rewrite the store wholesale",
                hypothesis="a rewrite also computes codes",
                assigned_claw="scaffold",
                inputs={
                    "file_path": str(self.store_path),
                    "content": STORE_TEXT,
                },
                outputs=[str(self.store_path)],
                acceptance=["The module is rewritten."],
                criteria=[
                    {"type": "file_exists", "path": str(self.store_path)}
                ],
                depends_on=["node-1"],
                status=PlanStatus.COMPLETED,
            ),
        )
        result, calls = self._run(
            _block(DEFECTIVE_REGISTER, FIXED_REGISTER), nodes=nodes
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
