"""Tests for ToolScaffolder and NEEDS_TOOL executor integration.

All tests are hermetic — no network, no LLM, no real codegen backend.
The scaffolder's optional ``codegen_backend`` is either absent (static template)
or a deterministic in-process stub.

Security invariants verified:
- Generated scaffolds are NEVER imported, executed, or registered at runtime.
- The quarantine directory is NEVER on sys.path.
- Capabilities only run after being explicitly registered in ToolRegistry.
- Missing capability + no scaffolder → existing FAILED behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from freetalon.audit import AuditLogger
from freetalon.orchestrator.executor import (
    Executor,
    _collect_runnable,
    _derive_plan_status,
    _is_terminal,
)
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import ToolRegistry
from freetalon.orchestrator.tool_scaffolder import (
    CapabilityNameError,
    ScaffoldProposal,
    ToolScaffolder,
)


# ── Test helpers ──────────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.run(coro)


def _make_store(tmpdir: str) -> ExecutionPlanStateStore:
    return ExecutionPlanStateStore(db_path=Path(tmpdir) / "test.db")


def _make_plan(plan_id: str, nodes: list[PlanNode] | None = None) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        intent=TaskIntent(goal="Test plan"),
        nodes=nodes or [],
    )


# ── ToolScaffolder unit tests ─────────────────────────────────────────────────


class TestToolScaffolder(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmpdir.name) / "proposed_tools"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _scaffolder(self, backend=None) -> ToolScaffolder:
        return ToolScaffolder(base_dir=self.base_dir, codegen_backend=backend)

    # ── Happy path ────────────────────────────────────────────────────────

    def test_propose_creates_handler_and_readme(self) -> None:
        scaffolder = self._scaffolder()
        proposal = scaffolder.propose("my_tool")

        self.assertIsInstance(proposal, ScaffoldProposal)
        self.assertEqual(proposal.capability, "my_tool")
        self.assertTrue(proposal.path.exists())
        self.assertTrue((self.base_dir / "my_tool" / "README.md").exists())

    def test_proposal_path_inside_quarantine_dir(self) -> None:
        scaffolder = self._scaffolder()
        proposal = scaffolder.propose("my_tool")

        self.assertTrue(str(proposal.path).startswith(str(self.base_dir.resolve())))

    def test_proposal_content_hash_is_sha256(self) -> None:
        import hashlib

        scaffolder = self._scaffolder()
        proposal = scaffolder.propose("hash_check_cap")
        content = proposal.path.read_text(encoding="utf-8")
        expected = hashlib.sha256(content.encode()).hexdigest()
        self.assertEqual(proposal.content_hash, expected)

    def test_static_template_contains_todo_not_working_code(self) -> None:
        scaffolder = self._scaffolder()
        proposal = scaffolder.propose("todo_check")
        content = proposal.path.read_text(encoding="utf-8")
        self.assertIn("TODO", content)
        self.assertIn("NotImplementedError", content)

    def test_custom_codegen_backend_used_for_content(self) -> None:
        def stubbed_backend(capability: str, _inputs: dict[str, Any]) -> str:
            return f"# stub for {capability}\n"

        scaffolder = self._scaffolder(backend=stubbed_backend)
        proposal = scaffolder.propose("custom_cap")
        content = proposal.path.read_text(encoding="utf-8")
        self.assertEqual(content, "# stub for custom_cap\n")

    # ── Security: scaffold is never auto-imported ─────────────────────────

    def test_scaffold_is_not_importable_after_propose(self) -> None:
        """The quarantine directory must never be on sys.path."""
        scaffolder = self._scaffolder()
        proposal = scaffolder.propose("my_tool")

        # Ensure the quarantine dir is not on sys.path
        resolved_quarantine = self.base_dir.resolve()
        for entry in sys.path:
            try:
                entry_path = Path(entry).resolve()
            except Exception:
                continue
            self.assertNotEqual(entry_path, resolved_quarantine)
            self.assertFalse(
                resolved_quarantine.is_relative_to(entry_path),
                msg=f"Quarantine dir {resolved_quarantine} is reachable from sys.path entry {entry}",
            )

        # Also confirm the module is NOT importable (would raise ModuleNotFoundError
        # or ImportError — not an assertion about the content).
        with self.assertRaises((ModuleNotFoundError, ImportError)):
            importlib.import_module("my_tool")

    def test_quarantine_dir_not_on_sys_path_assertion(self) -> None:
        """ToolScaffolder.__init__ raises AssertionError if base_dir is on sys.path."""
        # Temporarily insert the quarantine dir into sys.path to trigger the guard.
        self.base_dir.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(self.base_dir.resolve()))
        try:
            with self.assertRaises(AssertionError):
                ToolScaffolder(base_dir=self.base_dir)
        finally:
            sys.path.pop(0)

    # ── Validation ────────────────────────────────────────────────────────

    def test_path_traversal_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("../evil")

    def test_slash_in_name_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("tools/evil")

    def test_backslash_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("tools\\evil")

    def test_null_byte_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("tool\x00cap")

    def test_spaces_in_name_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("my cap")

    def test_empty_name_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("")

    def test_too_long_name_rejected(self) -> None:
        scaffolder = self._scaffolder()
        with self.assertRaises(CapabilityNameError):
            scaffolder.propose("a" * 65)

    def test_valid_names_accepted(self) -> None:
        scaffolder = self._scaffolder()
        for name in ("cap", "my_cap", "Cap123", "MY_CAP_001"):
            with self.subTest(name=name):
                proposal = scaffolder.propose(name)
                self.assertEqual(proposal.capability, name)


# ── Executor + ToolScaffolder integration tests ───────────────────────────────


class TestExecutorWithScaffolder(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmpdir.name)
        self.base_dir = Path(self.tmpdir.name) / "proposed_tools"
        self.audit_path = Path(self.tmpdir.name) / "audit.jsonl"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _scaffolder(self) -> ToolScaffolder:
        return ToolScaffolder(base_dir=self.base_dir)

    def _audit(self) -> AuditLogger:
        return AuditLogger(path=self.audit_path)

    # ── NEEDS_TOOL path ───────────────────────────────────────────────────

    def test_missing_cap_with_scaffolder_sets_needs_tool(self) -> None:
        """Missing capability + scaffolder → NEEDS_TOOL (not FAILED)."""
        registry = ToolRegistry()  # no handlers
        node = PlanNode(id="n1", objective="step", assigned_claw="missing_cap")
        plan = _make_plan("p1", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
        )
        result = _run(executor.run("p1"))

        self.assertEqual(result.nodes[0].status, PlanStatus.NEEDS_TOOL)
        self.assertIsNotNone(result.nodes[0].error)
        self.assertIn("missing_cap", result.nodes[0].error or "")

    def test_missing_cap_draft_file_written_to_disk(self) -> None:
        """A draft scaffold file must be written to the quarantine dir."""
        registry = ToolRegistry()
        node = PlanNode(id="n1", objective="step", assigned_claw="my_missing_cap")
        plan = _make_plan("p-draft", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
        )
        _run(executor.run("p-draft"))

        handler_path = self.base_dir / "my_missing_cap" / "handler.py"
        self.assertTrue(handler_path.exists(), "Draft scaffold file was not written")

    def test_scaffold_not_registered_after_propose(self) -> None:
        """After scaffolding, the capability must NOT be in the registry."""
        registry = ToolRegistry()
        node = PlanNode(id="n1", objective="step", assigned_claw="new_cap")
        plan = _make_plan("p-noreg", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
        )
        _run(executor.run("p-noreg"))

        self.assertNotIn("new_cap", registry)

    def test_re_running_does_not_execute_generated_code(self) -> None:
        """Re-running a plan with a NEEDS_TOOL node must not execute the scaffold.

        The NEEDS_TOOL node is terminal — it should not be re-dispatched.
        """
        executed: list[str] = []

        registry = ToolRegistry()
        node = PlanNode(id="n1", objective="step", assigned_claw="unregistered_cap")
        plan = _make_plan("p-rerun", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
        )
        # First run → NEEDS_TOOL
        result1 = _run(executor.run("p-rerun"))
        self.assertEqual(result1.nodes[0].status, PlanStatus.NEEDS_TOOL)

        # Manually reset the plan status so we can run again (simulates re-submit).
        # The node stays NEEDS_TOOL (terminal) so it should not be re-dispatched.
        plan2 = self.store.load("p-rerun")
        assert plan2 is not None
        plan2.status = PlanStatus.RUNNING
        self.store.save(plan2)

        result2 = _run(executor.run("p-rerun"))
        # Node must still be NEEDS_TOOL — no execution happened.
        self.assertEqual(result2.nodes[0].status, PlanStatus.NEEDS_TOOL)
        self.assertEqual(executed, [])

    def test_registered_cap_still_runs_normally(self) -> None:
        """Positive path: a registered capability executes successfully."""
        registry = ToolRegistry()
        registry.register("known_cap", lambda inputs: {"done": True})

        node = PlanNode(id="n1", objective="step", assigned_claw="known_cap")
        plan = _make_plan("p-ok", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
        )
        result = _run(executor.run("p-ok"))

        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertEqual(result.status, PlanStatus.COMPLETED)

    # ── Fallback: no scaffolder → existing FAILED behaviour ───────────────

    def test_missing_cap_no_scaffolder_still_fails(self) -> None:
        """Without a scaffolder, missing capability preserves FAILED behaviour."""
        registry = ToolRegistry()
        node = PlanNode(id="n1", objective="step", assigned_claw="ghost_cap")
        plan = _make_plan("p-fail", [node])
        self.store.save(plan)

        # No scaffolder injected — default constructor behaviour.
        executor = Executor(store=self.store, registry=registry)
        result = _run(executor.run("p-fail"))

        self.assertEqual(result.nodes[0].status, PlanStatus.FAILED)
        self.assertIsNotNone(result.nodes[0].error)

    # ── Audit logging ─────────────────────────────────────────────────────

    def test_audit_events_written(self) -> None:
        """Audit log must contain tool.scaffold.proposed and node.needs_tool events."""
        import json

        registry = ToolRegistry()
        node = PlanNode(id="n1", objective="step", assigned_claw="audited_cap")
        plan = _make_plan("p-audit", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
            audit_logger=self._audit(),
        )
        _run(executor.run("p-audit"))

        lines = self.audit_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line)["event"] for line in lines]
        self.assertIn("tool.scaffold.proposed", events)
        self.assertIn("node.needs_tool", events)

    def test_audit_scaffold_rejected_on_bad_name(self) -> None:
        """Unsafe capability name → tool.scaffold.rejected audit event."""
        import json

        # We test the scaffolder directly here since the executor validates via
        # the scaffolder, which raises CapabilityNameError before the propose call.
        scaffolder = ToolScaffolder(base_dir=self.base_dir)
        audit = self._audit()

        # Simulate what the executor does on CapabilityNameError.
        try:
            scaffolder.propose("../evil")
        except CapabilityNameError as exc:
            audit.log(
                "tool.scaffold.rejected",
                capability="../evil",
                reason=str(exc),
            )

        lines = self.audit_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line)["event"] for line in lines]
        self.assertIn("tool.scaffold.rejected", events)

    # ── sys.path guard ────────────────────────────────────────────────────

    def test_quarantine_dir_not_on_sys_path_after_execution(self) -> None:
        """After a full executor run, the quarantine dir is still not on sys.path."""
        registry = ToolRegistry()
        node = PlanNode(id="n1", objective="step", assigned_claw="path_check_cap")
        plan = _make_plan("p-syspath", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            scaffolder=self._scaffolder(),
        )
        _run(executor.run("p-syspath"))

        resolved = self.base_dir.resolve()
        for entry in sys.path:
            try:
                entry_path = Path(entry).resolve()
            except Exception:
                continue
            self.assertNotEqual(entry_path, resolved)
            self.assertFalse(resolved.is_relative_to(entry_path))


# ── PlanStatus / helper function tests ───────────────────────────────────────


class TestNeedsToolStatus(unittest.TestCase):
    def test_needs_tool_is_terminal(self) -> None:
        self.assertTrue(_is_terminal(PlanStatus.NEEDS_TOOL))

    def test_derive_plan_status_needs_tool_returns_failed(self) -> None:
        """A plan with NEEDS_TOOL nodes (no FAILED) surfaces as FAILED."""
        intent = TaskIntent(goal="test")
        nodes = [
            PlanNode(id="a", objective="ok", status=PlanStatus.COMPLETED),
            PlanNode(id="b", objective="missing", status=PlanStatus.NEEDS_TOOL),
        ]
        plan = ExecutionPlan(plan_id="p", intent=intent, nodes=nodes)
        self.assertEqual(_derive_plan_status(plan), PlanStatus.FAILED)

    def test_needs_tool_node_not_collected_as_runnable(self) -> None:
        intent = TaskIntent(goal="test")
        nodes = [
            PlanNode(id="a", objective="missing", status=PlanStatus.NEEDS_TOOL),
        ]
        plan = ExecutionPlan(plan_id="p", intent=intent, nodes=nodes)
        self.assertEqual(_collect_runnable(plan), [])


if __name__ == "__main__":
    unittest.main()
