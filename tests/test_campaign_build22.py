"""Campaign build #22: loop test-authorship fidelity.

The held-out screening at build #21 measured the loop's own test
modules failing against its own contract-conformant code in both
task-B candidate runs (hidden score 17/17, internal verdict
FAILED/INCONCLUSIVE both times): a test module importing
``NotesStore`` from a store that defines ``NoteStore`` (v1 of
both runs), a migration fixture written in an invented file
shape, an imagined ``save()`` method, an int-id expectation
against the standing ``note-000001`` format. Diagnosis (build #22
log): content fills happen in one plan-time pass, and a fill's
prompt carries only the target file's OWN standing content — a
test file is a create, so it saw nothing of the code it tests,
and no validator resolved its imports against the tree.

The fix is two layers, and these tests pin both:

- fill validation (planner._filled_candidate_errors via
  grounding.import_grounding_findings): a filled file's
  from-imports of project modules must name only names the
  module defines, judged against the materialized tree (disk
  overlaid with the plan's own fills). Conservative exits:
  unknowable module text, planned-but-unfilled modules, dynamic
  target modules, and imports a rewrite does not introduce.
- fill context (planner._code_under_test_block): a test file's
  fill prompt carries the actual content of the code under test
  — earlier fills of the same plan, then standing disk content
  for the plan's other write targets.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.grounding import import_grounding_violations
from freetalon.orchestrator.planner import (
    _code_under_test_block,
    _filled_candidate_errors,
    _is_test_file_target,
)

GOAL = "Extend the notes app with tags and verify the change works."

STORE_SRC = '''"""Notes store."""

import json


class NoteError(Exception):
    """Base error for the notes store."""


class InvalidNote(NoteError, ValueError):
    """A note failed validation."""


class NoteNotFound(NoteError):
    """No such note."""


class StoreCorruptError(NoteError):
    """The store file is unreadable."""


class NoteStore:
    """JSON-file-backed note store."""

    def __init__(self, path):
        self._path = path
        self._notes = {}
        self._next_id = 1

    def _save(self):
        data = {"version": 1, "next_id": self._next_id, "notes": self._notes}
        self._path.write_text(json.dumps(data, sort_keys=True))
'''

QUEUE_STORE_SRC = '''"""Job queue store."""


class SubmitOutcome:
    """The result of a submit: the job and whether it was created."""

    def __init__(self, job, created):
        self.job = job
        self.created = created


class JobQueueStore:
    """In-memory job queue store."""

    def __init__(self):
        self._jobs = {}

    def submit(self, payload):
        job = {"id": "job-000001", "payload": payload}
        self._jobs[job["id"]] = job
        return SubmitOutcome(job, True)
'''

BAD_IMPORT_TEST = '''"""Contract tests for the notes change."""

import unittest

from notesapp.store import NotesStore, InvalidNote


class TestNotes(unittest.TestCase):
    def test_store_exists(self):
        self.assertTrue(NotesStore is not None)
        self.assertTrue(InvalidNote is not None)
'''

GOOD_IMPORT_TEST = BAD_IMPORT_TEST.replace("NotesStore", "NoteStore")


def _scaffold(node_id: str, path: Path, content: str) -> dict:
    return {
        "id": node_id,
        "objective": f"Write {path.name}",
        "depends_on": [],
        "assigned_claw": "scaffold",
        "inputs": {"file_path": str(path), "content": content},
        "outputs": [str(path)],
        "criteria": [{"type": "file_exists", "path": str(path)}],
    }


def _import_errors(errors: list[str]) -> list[str]:
    return [e for e in errors if "ungrounded imports" in e]


class _TmpTreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "notesapp").mkdir()
        (self.root / "notesapp" / "__init__.py").write_text(
            '"""Notes app."""\n', encoding="utf-8"
        )
        (self.root / "notesapp" / "store.py").write_text(
            STORE_SRC, encoding="utf-8"
        )
        (self.root / "tests").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestImportGroundingAtFill(_TmpTreeCase):
    def test_test_create_importing_nonexistent_name_declines(self) -> None:
        # The held-out task-B shape, twice measured live: the
        # loop's test module imports NotesStore; the store defines
        # NoteStore. Before this build the payload passed every
        # validator and detonated at execution.
        candidate = _scaffold(
            "step_4", self.root / "tests" / "test_notes_contract.py",
            BAD_IMPORT_TEST,
        )
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        joined = "\n".join(_import_errors(errors))
        self.assertTrue(
            _import_errors(errors),
            f"expected an ungrounded-import error, got: {errors}",
        )
        self.assertIn(
            "NotesStore is imported from notesapp.store", joined
        )
        self.assertIn("NoteStore", joined)

    def test_test_create_importing_defined_names_passes(self) -> None:
        candidate = _scaffold(
            "step_4", self.root / "tests" / "test_notes_contract.py",
            GOOD_IMPORT_TEST,
        )
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        self.assertEqual(_import_errors(errors), [])

    def test_import_resolves_against_prior_fill(self) -> None:
        # Greenfield (the held-out task-A shape): the module under
        # test exists only as an earlier fill of the same plan —
        # nothing is on disk. Its real names resolve; an invented
        # plural does not.
        prior = _scaffold(
            "step_1", self.root / "queueapp" / "store.py", QUEUE_STORE_SRC
        )
        plan_written = {
            str(self.root / "queueapp" / "__init__.py"),
            str(self.root / "queueapp" / "store.py"),
            str(self.root / "tests" / "test_queue_contract.py"),
        }
        good = _scaffold(
            "step_2",
            self.root / "tests" / "test_queue_contract.py",
            '"""Queue tests."""\n\nfrom queueapp.store import '
            "JobQueueStore, SubmitOutcome\n",
        )
        errors = _filled_candidate_errors(
            good, [prior], self.root, GOAL, plan_written=plan_written
        )
        self.assertEqual(_import_errors(errors), [])
        bad = _scaffold(
            "step_2",
            self.root / "tests" / "test_queue_contract.py",
            '"""Queue tests."""\n\nfrom queueapp.store import '
            "JobQueueStores\n",
        )
        errors = _filled_candidate_errors(
            bad, [prior], self.root, GOAL, plan_written=plan_written
        )
        self.assertTrue(_import_errors(errors), f"got: {errors}")
        self.assertIn(
            "JobQueueStores is imported from queueapp.store",
            "\n".join(errors),
        )

    def test_planned_but_unfilled_module_is_not_judged(self) -> None:
        candidate = _scaffold(
            "step_4",
            self.root / "tests" / "test_notes_contract.py",
            '"""Tests."""\n\nfrom notesapp.future import Whatever\n'
            "from notesapp import future\n",
        )
        plan_written = {
            str(self.root / "notesapp" / "future.py"),
            str(self.root / "tests" / "test_notes_contract.py"),
        }
        errors = _filled_candidate_errors(
            candidate, [], self.root, GOAL, plan_written=plan_written
        )
        self.assertEqual(_import_errors(errors), [])

    def test_external_and_stdlib_imports_are_not_judged(self) -> None:
        candidate = _scaffold(
            "step_4",
            self.root / "tests" / "test_notes_contract.py",
            '"""Tests."""\n\nimport os\nimport unittest\n'
            "from collections import OrderedDict\n"
            "from pathlib import Path\n"
            "from pydantic import BaseModel\n",
        )
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        self.assertEqual(_import_errors(errors), [])

    def test_dynamic_target_modules_are_not_judged(self) -> None:
        (self.root / "notesapp" / "dyn.py").write_text(
            "from os import *\n\n\ndef helper():\n    return 1\n",
            encoding="utf-8",
        )
        (self.root / "notesapp" / "lazy.py").write_text(
            "def __getattr__(name):\n    return 1\n", encoding="utf-8"
        )
        candidate = _scaffold(
            "step_4",
            self.root / "tests" / "test_notes_contract.py",
            '"""Tests."""\n\nfrom notesapp.dyn import AnyName\n'
            "from notesapp.lazy import Whatever\n",
        )
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        self.assertEqual(_import_errors(errors), [])

    def test_rewrite_is_judged_only_on_introduced_imports(self) -> None:
        standing = self.root / "tests" / "test_standing.py"
        standing.write_text(BAD_IMPORT_TEST, encoding="utf-8")
        # A whole-file rewrite that keeps the standing (bad) import
        # is not this fill's sin.
        candidate = _scaffold("step_4", standing, BAD_IMPORT_TEST)
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        self.assertEqual(_import_errors(errors), [])
        # A rewrite that ADDS a bad import is judged on the addition.
        worse = BAD_IMPORT_TEST.replace(
            "from notesapp.store import NotesStore, InvalidNote",
            "from notesapp.store import NotesStore, InvalidNote\n"
            "from notesapp.store import NotesStores",
        )
        candidate = _scaffold("step_4", standing, worse)
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        joined = "\n".join(_import_errors(errors))
        self.assertIn("NotesStores is imported from", joined)
        self.assertNotIn("NotesStore is imported from", joined)

    def test_relative_import_is_grounded(self) -> None:
        (self.root / "notesapp" / "tests").mkdir()
        candidate = _scaffold(
            "step_4",
            self.root / "notesapp" / "tests" / "test_store_rel.py",
            '"""Tests."""\n\nfrom ..store import NotesStore\n',
        )
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        self.assertTrue(_import_errors(errors), f"got: {errors}")
        candidate = _scaffold(
            "step_4",
            self.root / "notesapp" / "tests" / "test_store_rel.py",
            '"""Tests."""\n\nfrom ..store import NoteStore\n',
        )
        errors = _filled_candidate_errors(candidate, [], self.root, GOAL)
        self.assertEqual(_import_errors(errors), [])


class TestImportGroundingUnits(unittest.TestCase):
    def _resolver(self, modules: dict[str, str]):
        return lambda dotted: modules.get(dotted)

    def test_missing_name_in_known_module(self) -> None:
        modules = {"pkg.store": "class NoteStore:\n    pass\n"}
        violations = import_grounding_violations(
            "from pkg.store import NotesStore\n",
            importer_package=("tests",),
            importer_module="tests.test_x",
            resolve_module=self._resolver(modules),
            module_planned=lambda dotted: False,
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("NotesStore is imported from pkg.store", violations[0])
        self.assertIn("NoteStore", violations[0])

    def test_submodule_import_resolves(self) -> None:
        modules = {"pkg": "", "pkg.store": "X = 1\n"}
        violations = import_grounding_violations(
            "from pkg import store\n",
            importer_package=("tests",),
            importer_module="tests.test_x",
            resolve_module=self._resolver(modules),
            module_planned=lambda dotted: False,
        )
        self.assertEqual(violations, [])

    def test_unknown_module_is_never_judged(self) -> None:
        violations = import_grounding_violations(
            "from somewhere.else import Anything\n",
            importer_package=("tests",),
            importer_module="tests.test_x",
            resolve_module=self._resolver({}),
            module_planned=lambda dotted: False,
        )
        self.assertEqual(violations, [])


class TestCodeUnderTestBlock(_TmpTreeCase):
    def test_is_test_file_target(self) -> None:
        self.assertTrue(_is_test_file_target("tests/test_x.py"))
        self.assertTrue(_is_test_file_target("/abs/repo/tests/test_x.py"))
        self.assertTrue(_is_test_file_target("pkg/test_utils.py"))
        self.assertTrue(_is_test_file_target("pkg/utils_test.py"))
        self.assertFalse(_is_test_file_target("pkg/store.py"))
        self.assertFalse(_is_test_file_target("tests/README.md"))

    def test_block_carries_prior_fill_contents(self) -> None:
        # The fixture-shape mitigation: the store's real _save
        # format rides in the test fill's context.
        prior = _scaffold(
            "step_1", self.root / "notesapp" / "store.py", STORE_SRC
        )
        block = _code_under_test_block(
            str(self.root / "tests" / "test_notes_contract.py"),
            [prior],
            self.root,
            None,
        )
        self.assertIn("class NoteStore", block)
        self.assertIn('"version": 1', block)
        self.assertIn("notesapp/store.py", block)

    def test_block_falls_back_to_disk_for_plan_targets(self) -> None:
        # The re-plan case: no earlier fills; the implementation
        # stands on disk from the previous version.
        block = _code_under_test_block(
            str(self.root / "tests" / "test_notes_contract.py"),
            [],
            self.root,
            {
                str(self.root / "notesapp" / "store.py"),
                str(self.root / "tests" / "test_notes_contract.py"),
            },
        )
        self.assertIn("class NoteStore", block)

    def test_block_excludes_test_files_and_target(self) -> None:
        other_test = _scaffold(
            "step_1",
            self.root / "tests" / "test_other.py",
            '"""Other tests."""\n\nSECRET_OTHER_TEST = 1\n',
        )
        prior = _scaffold(
            "step_2", self.root / "notesapp" / "store.py", STORE_SRC
        )
        block = _code_under_test_block(
            str(self.root / "tests" / "test_notes_contract.py"),
            [other_test, prior],
            self.root,
            None,
        )
        self.assertIn("class NoteStore", block)
        self.assertNotIn("SECRET_OTHER_TEST", block)

    def test_block_truncates_oversized_files(self) -> None:
        big = '"""Big module."""\n\n' + ("X = 1\n" * 4000)
        prior = _scaffold("step_1", self.root / "notesapp" / "big.py", big)
        block = _code_under_test_block(
            str(self.root / "tests" / "test_notes_contract.py"),
            [prior],
            self.root,
            None,
        )
        self.assertIn("[truncated for length]", block)


if __name__ == "__main__":
    unittest.main()
