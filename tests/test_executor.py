"""Tests for tool_registry and executor modules."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.executor import (
    Executor,
    ExecutorError,
    _all_dependencies_met,
    _collect_runnable,
    _count_in_flight,
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
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    UnknownCapabilityError,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_store(tmpdir: str) -> ExecutionPlanStateStore:
    db_path = Path(tmpdir) / "test_exec.db"
    return ExecutionPlanStateStore(db_path=db_path)


def _make_plan(plan_id: str, nodes: list[PlanNode] | None = None) -> ExecutionPlan:
    intent = TaskIntent(goal="Test plan")
    return ExecutionPlan(plan_id=plan_id, intent=intent, nodes=nodes or [])


def _run(coro):  # type: ignore[no-untyped-def]
    """Convenience wrapper to run an async coroutine in tests."""
    return asyncio.run(coro)


# ── ToolRegistry tests ────────────────────────────────────────────────────────


class TestToolRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()

    def test_register_and_resolve_async_callable(self) -> None:
        async def my_handler(inputs: dict) -> dict:
            return {"ok": True}

        self.registry.register("test_cap", my_handler)
        handler = self.registry.resolve("test_cap")
        result = _run(handler({}))
        self.assertEqual(result, {"ok": True})

    def test_register_and_resolve_sync_callable(self) -> None:
        def sync_handler(inputs: dict) -> dict:
            return {"sync": True}

        self.registry.register("sync_cap", sync_handler)
        result = _run(self.registry.resolve("sync_cap")({}))
        self.assertEqual(result, {"sync": True})

    def test_decorator_usage(self) -> None:
        @self.registry.register("deco_cap")
        async def handler(inputs: dict) -> dict:
            return {"decorated": True}

        result = _run(self.registry.resolve("deco_cap")({}))
        self.assertEqual(result, {"decorated": True})

    def test_resolve_unknown_raises(self) -> None:
        with self.assertRaises(UnknownCapabilityError):
            self.registry.resolve("ghost_cap")

    def test_strict_false_returns_noop(self) -> None:
        registry = ToolRegistry(strict=False)
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            handler = registry.resolve("missing")
            result = _run(handler({}))
        self.assertEqual(result, {})
        self.assertTrue(any("missing" in str(warning.message) for warning in w))

    def test_capabilities_returns_sorted(self) -> None:
        self.registry.register("zzz", lambda inputs: {})
        self.registry.register("aaa", lambda inputs: {})
        caps = self.registry.capabilities()
        self.assertEqual(caps, ["aaa", "zzz"])

    def test_contains(self) -> None:
        self.registry.register("present", lambda inputs: {})
        self.assertIn("present", self.registry)
        self.assertNotIn("absent", self.registry)

    def test_inputs_passed_to_sync_handler(self) -> None:
        def echo(inputs: dict) -> dict:
            return {"received": inputs.get("x")}

        self.registry.register("echo", echo)
        result = _run(self.registry.resolve("echo")({"x": 42}))
        self.assertEqual(result["received"], 42)


# ── Executor helper function tests ────────────────────────────────────────────


class TestExecutorHelpers(unittest.TestCase):
    def _node(
        self,
        nid: str,
        status: PlanStatus = PlanStatus.DRAFT,
        depends_on: list[str] | None = None,
    ) -> PlanNode:
        return PlanNode(
            id=nid,
            objective=f"objective for {nid}",
            status=status,
            depends_on=depends_on or [],
        )

    def test_is_terminal(self) -> None:
        self.assertTrue(_is_terminal(PlanStatus.COMPLETED))
        self.assertTrue(_is_terminal(PlanStatus.FAILED))
        self.assertTrue(_is_terminal(PlanStatus.CANCELLED))
        self.assertFalse(_is_terminal(PlanStatus.RUNNING))
        self.assertFalse(_is_terminal(PlanStatus.DRAFT))
        self.assertFalse(_is_terminal(PlanStatus.READY))

    def test_all_dependencies_met_no_deps(self) -> None:
        node = self._node("a")
        self.assertTrue(_all_dependencies_met(node, {}))

    def test_all_dependencies_met_completed_dep(self) -> None:
        dep = self._node("dep", status=PlanStatus.COMPLETED)
        node = self._node("n", depends_on=["dep"])
        self.assertTrue(_all_dependencies_met(node, {"dep": dep}))

    def test_all_dependencies_not_met(self) -> None:
        dep = self._node("dep", status=PlanStatus.RUNNING)
        node = self._node("n", depends_on=["dep"])
        self.assertFalse(_all_dependencies_met(node, {"dep": dep}))

    def test_collect_runnable_no_deps(self) -> None:
        plan = _make_plan("p", [self._node("a"), self._node("b")])
        runnable = _collect_runnable(plan)
        self.assertEqual({n.id for n in runnable}, {"a", "b"})

    def test_collect_runnable_dep_not_completed(self) -> None:
        plan = _make_plan(
            "p",
            [
                self._node("a", status=PlanStatus.RUNNING),
                self._node("b", depends_on=["a"]),
            ],
        )
        runnable = _collect_runnable(plan)
        self.assertEqual(runnable, [])

    def test_collect_runnable_dep_completed(self) -> None:
        plan = _make_plan(
            "p",
            [
                self._node("a", status=PlanStatus.COMPLETED),
                self._node("b", depends_on=["a"]),
            ],
        )
        runnable = _collect_runnable(plan)
        self.assertEqual([n.id for n in runnable], ["b"])

    def test_count_in_flight(self) -> None:
        plan = _make_plan(
            "p",
            [
                self._node("a", status=PlanStatus.RUNNING),
                self._node("b", status=PlanStatus.COMPLETED),
                self._node("c", status=PlanStatus.RUNNING),
            ],
        )
        self.assertEqual(_count_in_flight(plan), 2)

    def test_derive_plan_status_all_completed(self) -> None:
        plan = _make_plan(
            "p",
            [
                self._node("a", PlanStatus.COMPLETED),
                self._node("b", PlanStatus.COMPLETED),
            ],
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.COMPLETED)

    def test_derive_plan_status_has_failed(self) -> None:
        plan = _make_plan(
            "p",
            [
                self._node("a", PlanStatus.COMPLETED),
                self._node("b", PlanStatus.FAILED),
            ],
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.FAILED)

    def test_derive_plan_status_running(self) -> None:
        plan = _make_plan(
            "p",
            [
                self._node("a", PlanStatus.COMPLETED),
                self._node("b", PlanStatus.RUNNING),
            ],
        )
        self.assertEqual(_derive_plan_status(plan), PlanStatus.RUNNING)


# ── Executor integration tests ────────────────────────────────────────────────


class TestExecutor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _registry_with(self, caps: dict[str, dict]) -> ToolRegistry:
        registry = ToolRegistry()
        for cap, result in caps.items():

            async def _handler(inputs: dict, _r: dict = result) -> dict:
                return _r

            registry.register(cap, _handler)
        return registry

    def test_single_node_completes(self) -> None:
        registry = self._registry_with({"worker": {"done": True}})
        node = PlanNode(id="n1", objective="step 1", assigned_claw="worker")
        plan = _make_plan("p1", [node])
        self.store.save(plan)

        executor = Executor(store=self.store, registry=registry)
        result = _run(executor.run("p1"))

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)

    def test_plan_not_found_raises(self) -> None:
        registry = ToolRegistry()
        executor = Executor(store=self.store, registry=registry)
        with self.assertRaises(ExecutorError):
            _run(executor.run("nonexistent"))

    def test_dependency_ordering(self) -> None:
        execution_order: list[str] = []

        def make_handler(nid: str):
            async def handler(inputs: dict) -> dict:
                execution_order.append(nid)
                return {}

            return handler

        registry = ToolRegistry()
        registry.register("cap_a", make_handler("a"))
        registry.register("cap_b", make_handler("b"))

        node_a = PlanNode(id="a", objective="first", assigned_claw="cap_a")
        node_b = PlanNode(
            id="b", objective="second", assigned_claw="cap_b", depends_on=["a"]
        )
        plan = _make_plan("dep-plan", [node_a, node_b])
        self.store.save(plan)

        _run(Executor(store=self.store, registry=registry).run("dep-plan"))
        self.assertEqual(execution_order, ["a", "b"])

    def test_unknown_capability_marks_node_failed(self) -> None:
        registry = ToolRegistry()  # no handlers registered
        node = PlanNode(id="n1", objective="step", assigned_claw="missing_cap")
        plan = _make_plan("fail-plan", [node])
        self.store.save(plan)

        result = _run(Executor(store=self.store, registry=registry).run("fail-plan"))
        self.assertEqual(result.status, PlanStatus.FAILED)
        self.assertEqual(result.nodes[0].status, PlanStatus.FAILED)
        self.assertIsNotNone(result.nodes[0].error)

    def test_handler_exception_marks_node_failed(self) -> None:
        async def exploding(inputs: dict) -> dict:
            raise ValueError("boom")

        registry = ToolRegistry()
        registry.register("boom_cap", exploding)

        node = PlanNode(id="n1", objective="step", assigned_claw="boom_cap")
        plan = _make_plan("exc-plan", [node])
        self.store.save(plan)

        result = _run(Executor(store=self.store, registry=registry).run("exc-plan"))
        self.assertEqual(result.nodes[0].status, PlanStatus.FAILED)
        self.assertIn("boom", result.nodes[0].error or "")

    def test_empty_plan_completes(self) -> None:
        registry = ToolRegistry()
        plan = _make_plan("empty-plan", [])
        self.store.save(plan)

        result = _run(Executor(store=self.store, registry=registry).run("empty-plan"))
        self.assertEqual(result.status, PlanStatus.COMPLETED)

    def test_multi_node_no_deps_run_concurrently(self) -> None:
        """Nodes without deps should all be dispatched in the same batch."""
        started: list[str] = []

        def make_handler(nid: str):
            async def handler(inputs: dict) -> dict:
                started.append(nid)
                return {}

            return handler

        registry = ToolRegistry()
        nodes = []
        for i in range(3):
            cap = f"cap_{i}"
            registry.register(cap, make_handler(cap))
            nodes.append(PlanNode(id=f"n{i}", objective=f"node {i}", assigned_claw=cap))

        plan = _make_plan("concurrent-plan", nodes)
        self.store.save(plan)
        result = _run(
            Executor(store=self.store, registry=registry, max_concurrency=3).run(
                "concurrent-plan"
            )
        )
        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(sorted(started), ["cap_0", "cap_1", "cap_2"])


if __name__ == "__main__":
    unittest.main()
