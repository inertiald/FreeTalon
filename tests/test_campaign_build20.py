"""Campaign build #20: duplicate_code new-package exemption.

The held-out screening set (tools/heldout, preregistration frozen
2026-11-25) measured the plan validator rejecting
freetalon/jobqueue/store.py and freetalon/jobqueue/server.py as
duplicate_code of freetalon/pairing/store.py / server.py — the
contract-correct plan for a second service package failed planning
terminally in both baseline runs (task A 0/24, no code written).

The fix is a plan-level waiver in planner._grounding_error, NOT a
change to the standing-tree check: duplicate_scaffold_error keeps
its exact behavior, and a module-stem rejection is waived only
when the plan itself creates the target's package (it writes the
directory's __init__.py), the directory is not already a package,
the stem is not a route name, and no same-stem module lives in the
target's package lineage. These tests pin:

- the held-out A plan shape (a plan-created sibling package with
  conventional module names) is accepted at parse level and at
  fill level;
- the same creates WITHOUT the package-creation node still
  decline — a bare same-stem create is the pinned pre-#20 case;
- a create inside the existing module's own package tree (a
  subpackage of pairing) still declines — the lineage condition;
- a create in the existing module's parent package still
  declines;
- duplicate_scaffold_error itself is unchanged (it still flags
  the jobqueue target against the standing tree — the waiver is
  the plan's, not the tree's);
- package_lineage_overlap's geometry.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.planner import (
    _filled_candidate_errors,
    parse_planner_response,
)
from freetalon.orchestrator.repo_index import (
    build_repo_index,
    duplicate_scaffold_error,
    package_lineage_overlap,
)

ROOT = Path(__file__).resolve().parent.parent
GENERIC_GOAL = "Add a logging helper to the app and verify it works."
JOBQUEUE = ROOT / "freetalon" / "jobqueue"

STORE_CONTENT = '''"""Job queue store."""


class JobStore:
    """In-memory job store with idempotency keys."""

    def __init__(self):
        self._jobs = {}
        self._keys = {}

    def submit(self, payload, idempotency_key):
        if idempotency_key in self._keys:
            return self._keys[idempotency_key]
        job_id = f"job-{len(self._jobs) + 1:06d}"
        job = {"id": job_id, "payload": payload, "status": "queued"}
        self._jobs[job_id] = job
        self._keys[idempotency_key] = job
        return job

    def get(self, job_id):
        return self._jobs.get(job_id)
'''

SERVER_CONTENT = '''"""Job queue HTTP server."""

from http.server import BaseHTTPRequestHandler


class JobHandler(BaseHTTPRequestHandler):
    """Answers job-queue health requests."""

    def do_GET(self):
        if self.path == "/health":
            body = b"{\\"status\\": \\"ok\\"}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)
'''


def _dag(nodes: list[dict]) -> str:
    return json.dumps({"nodes": nodes, "metadata": {}})


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


def _rejection(nodes: list[dict]) -> LLMResponseError:
    try:
        parse_planner_response(_dag(nodes), goal_text=GENERIC_GOAL)
    except LLMResponseError as error:
        return error
    raise AssertionError("plan was accepted, expected a rejection")


class TestPackageLineageOverlap(unittest.TestCase):
    def test_same_directory_overlaps(self) -> None:
        self.assertTrue(
            package_lineage_overlap(("freetalon", "pairing"), ("freetalon", "pairing"))
        )

    def test_package_and_subpackage_overlap_both_directions(self) -> None:
        self.assertTrue(
            package_lineage_overlap(
                ("freetalon", "pairing"), ("freetalon", "pairing", "sub")
            )
        )
        self.assertTrue(
            package_lineage_overlap(
                ("freetalon", "pairing", "sub"), ("freetalon", "pairing")
            )
        )

    def test_top_level_package_overlaps_everything_under_it(self) -> None:
        self.assertTrue(
            package_lineage_overlap(("freetalon",), ("freetalon", "jobqueue"))
        )
        self.assertTrue(package_lineage_overlap((), ("freetalon", "jobqueue")))

    def test_sibling_packages_do_not_overlap(self) -> None:
        self.assertFalse(
            package_lineage_overlap(
                ("freetalon", "pairing"), ("freetalon", "jobqueue")
            )
        )
        self.assertFalse(
            package_lineage_overlap(
                ("freetalon", "orchestrator"), ("freetalon", "other")
            )
        )


class TestNewPackageExemption(unittest.TestCase):
    def test_held_out_a_plan_shape_is_accepted(self) -> None:
        # The held-out task A plan: the plan creates the jobqueue
        # package (its __init__.py) and its conventional modules,
        # while freetalon/pairing/store.py + server.py exist.
        nodes = [
            _scaffold("step_1", JOBQUEUE / "__init__.py", '"""Jobqueue package."""\n'),
            _scaffold("step_2", JOBQUEUE / "store.py", STORE_CONTENT),
            _scaffold("step_3", JOBQUEUE / "server.py", SERVER_CONTENT),
        ]
        parsed, _ = parse_planner_response(_dag(nodes), goal_text=GENERIC_GOAL)
        self.assertEqual(len(parsed), 3)

    def test_same_creates_without_package_creation_still_decline(self) -> None:
        # No __init__.py node: the creates are bare same-stem
        # creates in a non-package directory — the pinned pre-#20
        # behavior (cf. test_planner_validator_alignment).
        nodes = [
            _scaffold("step_1", JOBQUEUE / "store.py", STORE_CONTENT),
            _scaffold("step_2", JOBQUEUE / "server.py", SERVER_CONTENT),
        ]
        error = _rejection(nodes)
        self.assertIn(
            "duplicate_code", {record.code for record in error.plan_errors}
        )
        self.assertIn("duplicates the existing module 'store'", str(error))

    def test_create_inside_existing_modules_package_tree_still_declines(self) -> None:
        # A subpackage of pairing is pairing's own tree: creating
        # pairing/sub/store.py dodges pairing/store.py, package
        # creation node or not.
        sub = ROOT / "freetalon" / "pairing" / "sub"
        nodes = [
            _scaffold("step_1", sub / "__init__.py", '"""Sub package."""\n'),
            _scaffold("step_2", sub / "store.py", STORE_CONTENT),
        ]
        error = _rejection(nodes)
        self.assertIn(
            "duplicate_code", {record.code for record in error.plan_errors}
        )

    def test_create_in_parent_package_still_declines(self) -> None:
        # freetalon/store.py would sit in pairing's parent package.
        node = _scaffold("step_1", ROOT / "freetalon" / "store.py", STORE_CONTENT)
        error = _rejection([node])
        self.assertIn(
            "duplicate_code", {record.code for record in error.plan_errors}
        )

    def test_standing_tree_check_unchanged(self) -> None:
        # The waiver is plan-level: against the standing tree alone,
        # the jobqueue target still flags as a duplicate.
        index = build_repo_index(ROOT)
        error = duplicate_scaffold_error(str(JOBQUEUE / "store.py"), index)
        self.assertIsNotNone(error)
        self.assertIn("duplicates the existing module 'store'", error)


class TestFillPhaseExemption(unittest.TestCase):
    def _candidate(self) -> dict:
        return _scaffold("step_2", JOBQUEUE / "store.py", STORE_CONTENT)

    def test_fill_without_plan_context_declines_duplicate(self) -> None:
        errors = _filled_candidate_errors(
            self._candidate(), [], ROOT, GENERIC_GOAL
        )
        self.assertTrue(
            any("duplicates the existing module 'store'" in e for e in errors),
            f"expected a duplicate_code error, got: {errors}",
        )

    def test_fill_with_plan_written_accepts_new_package_module(self) -> None:
        plan_written = {
            str(JOBQUEUE / "__init__.py"),
            str(JOBQUEUE / "store.py"),
            str(JOBQUEUE / "server.py"),
        }
        errors = _filled_candidate_errors(
            self._candidate(), [], ROOT, GENERIC_GOAL, plan_written=plan_written
        )
        self.assertFalse(
            any("duplicates the existing module" in e for e in errors),
            f"duplicate_code must be waived for the plan-created package, got: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
