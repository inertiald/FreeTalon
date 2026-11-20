"""Validator/executor alignment (2026-11-19): duplicate_code dunder
exemption and missing_directory mkdir crediting.

The pairing proof arm (logs/pairing-proof-driver.log) failed at
planning with the rejection signature (duplicate_code,
missing_directory) repeated 3x -> StuckRepairError, on a plan that
was CORRECT by executor semantics:

- its new package's __init__.py / __main__.py were flagged as
  duplicates of the existing '__init__' / '__main__' module stems —
  but every package directory must carry its own dunder files, so
  the collision is by construction and "extend that module instead"
  cannot satisfy it;
- its writes into the new package directory were flagged
  missing_directory even though step_1 ran `mkdir -p` on the
  directory and every write depended on it — the check's own hint
  said "create the directory first" but plan-created directories
  were never credited (the docstring admitted: "noted, not
  covered").

Executor ground truth these tests pin the alignment to:

- the scaffold claw creates parent directories itself
  (tool_registry scaffold_handler: parent.mkdir(parents=True,
  exist_ok=True) before write_text), so a scaffold write into a
  new directory lands — no missing-parent rejection, EXCEPT
  freetalon/tests/, which is not merely missing but the wrong
  location (repo tests live at tests/);
- the bash claw runs a real shell, where a heredoc/redirect into
  a missing directory fails — those writes stay rejected UNLESS
  the plan itself creates the directory in a step the write
  transitively depends on (or earlier in the write's own command);
- duplicate_code keeps its full force for substantive stems and
  route names; only the two dunder filenames are exempt.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.planner import (
    _mkdir_created_targets,
    parse_planner_response,
)
from freetalon.orchestrator.prompts import PLANNER_SYSTEM_PROMPT
from freetalon.orchestrator.repo_index import (
    build_repo_index,
    duplicate_scaffold_error,
)

ROOT = Path(__file__).resolve().parent.parent
GENERIC_GOAL = "Add a logging helper to the app and verify it works."
# A package directory that does not exist in the checkout.
PKG = ROOT / "freetalon" / "probe_pkg"


def _dag(nodes: list[dict]) -> str:
    return json.dumps({"nodes": nodes, "metadata": {}})


def _scaffold(node_id: str, path: Path, content: str, deps: list[str] | None = None) -> dict:
    return {
        "id": node_id,
        "objective": f"Write {path.name}",
        "depends_on": deps or [],
        "assigned_claw": "scaffold",
        "inputs": {"file_path": str(path), "content": content},
        "outputs": [str(path)],
        "criteria": [{"type": "file_exists", "path": str(path)}],
    }


def _bash(node_id: str, command: str, deps: list[str] | None = None) -> dict:
    return {
        "id": node_id,
        "objective": "Run a command",
        "depends_on": deps or [],
        "assigned_claw": "bash",
        "inputs": {"command": command},
        "outputs": ["result"],
        "criteria": [{"type": "exit_code", "equals": 0}],
    }


def _rejection(nodes: list[dict]) -> LLMResponseError:
    try:
        parse_planner_response(_dag(nodes), goal_text=GENERIC_GOAL)
    except LLMResponseError as error:
        return error
    raise AssertionError("plan was accepted, expected a rejection")


class TestDuplicateCodeDunderExemption(unittest.TestCase):
    def _synthetic_index(self, tmp: str):
        package = Path(tmp) / "freetalon"
        package.mkdir()
        (package / "__init__.py").write_text('"""Pkg."""\n', encoding="utf-8")
        (package / "planner.py").write_text("x = 1\n", encoding="utf-8")
        (package / "api.py").write_text(
            'if path == "/health":\n    pass\n', encoding="utf-8"
        )
        content = package / "content"
        content.mkdir()
        (content / "__init__.py").write_text('"""C."""\n', encoding="utf-8")
        (content / "__main__.py").write_text("x = 1\n", encoding="utf-8")
        return build_repo_index(tmp)

    def test_new_package_dunder_files_are_not_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._synthetic_index(tmp)
            for name in ("__init__.py", "__main__.py"):
                target = str(Path(tmp) / "freetalon" / "pairing" / name)
                self.assertIsNone(
                    duplicate_scaffold_error(target, index),
                    f"{name} must be exempt — every package needs its own",
                )

    def test_substantive_stem_collision_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._synthetic_index(tmp)
            target = str(Path(tmp) / "freetalon" / "other" / "planner.py")
            error = duplicate_scaffold_error(target, index)
            self.assertIsNotNone(error)
            self.assertIn("duplicates the existing module 'planner'", error)

    def test_route_name_collision_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._synthetic_index(tmp)
            target = str(Path(tmp) / "freetalon" / "other" / "health.py")
            error = duplicate_scaffold_error(target, index)
            self.assertIsNotNone(error)
            self.assertIn("/health", error)

    def test_parse_rejects_duplicate_substantive_stem(self) -> None:
        node = _scaffold("step_1", ROOT / "freetalon" / "other" / "planner.py", "x = 1\n")
        error = _rejection([node])
        self.assertIn("duplicates the existing module 'planner'", str(error))
        self.assertEqual(
            {record.code for record in error.plan_errors}, {"duplicate_code"}
        )


class TestMissingDirectoryAlignment(unittest.TestCase):
    def test_bash_write_into_new_dir_without_mkdir_is_rejected(self) -> None:
        target = PKG / "mod.py"
        node = _bash(
            "step_1",
            f"cat > {target} <<'EOF'\nx = 1\nEOF",
        )
        node["outputs"] = [str(target)]
        node["criteria"] = [{"type": "file_exists", "path": str(target)}]
        error = _rejection([node])
        self.assertIn("does not exist in the repo", str(error))
        self.assertEqual(
            {record.code for record in error.plan_errors}, {"missing_directory"}
        )

    def test_bash_write_credited_by_ancestor_mkdir(self) -> None:
        mkdir = _bash("step_1", f"mkdir -p {PKG}")
        target = PKG / "mod.py"
        write = _bash(
            "step_2",
            f"cat > {target} <<'EOF'\nx = 1\nEOF",
            deps=["step_1"],
        )
        write["outputs"] = [str(target)]
        write["criteria"] = [{"type": "file_exists", "path": str(target)}]
        nodes, _ = parse_planner_response(
            _dag([mkdir, write]), goal_text=GENERIC_GOAL
        )
        self.assertEqual(len(nodes), 2)

    def test_bash_write_not_credited_by_unrelated_mkdir(self) -> None:
        # The mkdir step exists but the write does not depend on it:
        # the executor gives no ordering guarantee between siblings,
        # so the write is still rejected.
        mkdir = _bash("step_1", f"mkdir -p {PKG}")
        target = PKG / "mod.py"
        write = _bash(
            "step_2",
            f"cat > {target} <<'EOF'\nx = 1\nEOF",
        )
        write["outputs"] = [str(target)]
        write["criteria"] = [{"type": "file_exists", "path": str(target)}]
        error = _rejection([mkdir, write])
        self.assertIn("does not exist in the repo", str(error))

    def test_bash_write_credited_by_mkdir_earlier_in_same_command(self) -> None:
        target = PKG / "mod.py"
        node = _bash(
            "step_1",
            f"mkdir -p {PKG} && cat > {target} <<'EOF'\nx = 1\nEOF",
        )
        node["outputs"] = [str(target)]
        node["criteria"] = [{"type": "file_exists", "path": str(target)}]
        nodes, _ = parse_planner_response(_dag([node]), goal_text=GENERIC_GOAL)
        self.assertEqual(len(nodes), 1)

    def test_scaffold_write_into_new_dir_needs_no_mkdir(self) -> None:
        # The scaffold claw creates parent directories at execution.
        node = _scaffold("step_1", PKG / "mod.py", "x = 1\n")
        nodes, _ = parse_planner_response(_dag([node]), goal_text=GENERIC_GOAL)
        self.assertEqual(len(nodes), 1)

    def test_scaffold_write_into_freetalon_tests_still_rejected(self) -> None:
        # freetalon/tests/ is not merely missing, it is the WRONG
        # location — the scaffold claw would silently create a shadow
        # tree the suite never discovers, so the rejection (and its
        # correct-location hint) stands for every claw.
        node = _scaffold(
            "step_1", ROOT / "freetalon" / "tests" / "test_probe.py",
            "import unittest\n",
        )
        error = _rejection([node])
        message = str(error)
        self.assertIn("does not exist in the repo", message)
        self.assertIn("Repo tests live at", message)

    def test_proof_arm_plan_shape_is_accepted(self) -> None:
        # The pairing proof arm's plan shape: a mkdir step, then the
        # new package's scaffold writes depending on it — including
        # the two dunder files duplicate_code used to reject. The
        # module stem is probe_store, not store: freetalon/pairing/
        # store.py exists now, and a second store.py anywhere is a
        # genuine stem collision duplicate_code is right to reject.
        mkdir = _bash("step_1", f"mkdir -p {PKG}")
        init = _scaffold(
            "step_2", PKG / "__init__.py",
            '"""Probe package."""\n\n__all__ = ["probe_store"]\n',
            deps=["step_1"],
        )
        store = _scaffold(
            "step_3", PKG / "probe_store.py",
            '"""Store."""\n\n\nclass Store:\n    def get(self):\n        return None\n',
            deps=["step_1"],
        )
        main = _scaffold(
            "step_4", PKG / "__main__.py",
            '"""Entry point."""\n\n\ndef main():\n    return 0\n\n\n'
            'if __name__ == "__main__":\n    raise SystemExit(main())\n',
            deps=["step_1"],
        )
        nodes, _ = parse_planner_response(
            _dag([mkdir, init, store, main]), goal_text=GENERIC_GOAL
        )
        self.assertEqual(len(nodes), 4)


class TestMkdirTargetExtraction(unittest.TestCase):
    def test_flags_and_multiple_targets(self) -> None:
        created = _mkdir_created_targets("mkdir -p /a/b /a/c && mkdir /a/d")
        self.assertEqual(created, {"/a/b", "/a/c", "/a/d"})

    def test_quoted_and_heredoc_mkdirs_are_data_not_commands(self) -> None:
        command = (
            'echo "mkdir -p /not/real"\n'
            "cat > /x.py <<'EOF'\nmkdir -p /also/not/real\nEOF\n"
            "mkdir -p /real"
        )
        self.assertEqual(_mkdir_created_targets(command), {"/real"})

    def test_expansion_targets_are_not_credited(self) -> None:
        created = _mkdir_created_targets("mkdir -p $BASE/pkg ${OTHER}/pkg /plain")
        self.assertEqual(created, {"/plain"})


class TestPromptTeachesNewDirectories(unittest.TestCase):
    def test_planner_prompt_names_the_mkdir_rule(self) -> None:
        self.assertIn("NEW DIRECTORIES", PLANNER_SYSTEM_PROMPT)
        self.assertIn("mkdir -p", PLANNER_SYSTEM_PROMPT)
        self.assertIn("depends_on", PLANNER_SYSTEM_PROMPT)

class TestLenientNodeViews(unittest.TestCase):
    """_collect_plan_error_records also runs on _lenient_node_view
    SimpleNamespaces (the duplicate-keys path): they carry id /
    assigned_claw / inputs / criteria / outputs and NO depends_on.
    The missing-directory check must work on them — the first cut
    of the mkdir crediting read node.depends_on directly and
    crashed all 15 duplicate-key repair tests with AttributeError.
    """

    def test_missing_directory_errors_tolerate_lenient_views(self) -> None:
        from types import SimpleNamespace

        from freetalon.orchestrator.planner import _missing_directory_errors

        target = str(PKG / "mod.py")
        views = [
            SimpleNamespace(
                id="step_1",
                assigned_claw="bash",
                inputs={"command": f"cat > {target} <<'EOF'\nx = 1\nEOF"},
                criteria=[],
                outputs=[target],
            ),
            SimpleNamespace(
                id="step_2",
                assigned_claw="bash",
                inputs={"command": f"mkdir -p {PKG}"},
                criteria=[],
                outputs=["result"],
            ),
        ]
        errors = _missing_directory_errors(views, ROOT)
        # No crash; the write is still flagged (a lenient view has
        # no depends_on, so the mkdir in the sibling view earns no
        # credit on this advisory path).
        self.assertEqual(len(errors), 1)
        self.assertIn("does not exist in the repo", errors[0])



if __name__ == "__main__":
    unittest.main()
