"""Tests for tool_registry and executor modules."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.executor import (
    DEPENDENCY_MISSING_KEY,
    MAX_INJECTION_ATTEMPTS,
    Executor,
    ExecutorError,
    _all_dependencies_met,
    _collect_runnable,
    _count_in_flight,
    _derive_plan_status,
    _inject_subdag,
    _is_dependency_missing,
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


# ── DependencyMissing / Sub-DAG injection tests ───────────────────────────────


class TestDependencyMissingHelpers(unittest.TestCase):
    """Unit tests for the pure-function DependencyMissing helpers."""

    def test_is_dependency_missing_true(self) -> None:
        self.assertTrue(_is_dependency_missing({DEPENDENCY_MISSING_KEY: {}}))

    def test_is_dependency_missing_false_missing_key(self) -> None:
        self.assertFalse(_is_dependency_missing({"other": "value"}))

    def test_is_dependency_missing_false_not_dict(self) -> None:
        self.assertFalse(_is_dependency_missing(None))
        self.assertFalse(_is_dependency_missing("string"))
        self.assertFalse(_is_dependency_missing(42))

    def test_inject_subdag_single_node(self) -> None:
        """Injecting one sub-node adds it to the plan and rewires the originator."""
        plan = _make_plan(
            "p",
            [PlanNode(id="orig", objective="original task")],
        )
        sub = PlanNode(id="sub-0", objective="prerequisite", assigned_claw="pre-cap")
        _inject_subdag(plan, "orig", [sub])

        ids = {n.id for n in plan.nodes}
        self.assertIn("orig", ids)
        # Injected node has a remapped id
        injected = [n for n in plan.nodes if n.id != "orig"]
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0].objective, "prerequisite")
        self.assertEqual(injected[0].status, PlanStatus.DRAFT)
        # Originator depends on the injected node
        orig_node = plan.node_map()["orig"]
        self.assertIn(injected[0].id, orig_node.depends_on)
        self.assertEqual(orig_node.status, PlanStatus.DRAFT)

    def test_inject_subdag_multi_node_internal_wiring(self) -> None:
        """Internal A→B dependency is preserved after id remapping."""
        plan = _make_plan("p", [PlanNode(id="orig", objective="original")])
        node_a = PlanNode(id="sub-a", objective="step A", assigned_claw="a-cap")
        node_b = PlanNode(
            id="sub-b", objective="step B", assigned_claw="b-cap", depends_on=["sub-a"]
        )
        _inject_subdag(plan, "orig", [node_a, node_b])

        node_map = plan.node_map()
        injected = [n for n in plan.nodes if n.id != "orig"]
        self.assertEqual(len(injected), 2)

        # Find remapped ids
        inj_a = next(n for n in injected if n.objective == "step A")
        inj_b = next(n for n in injected if n.objective == "step B")

        # B must depend on A's remapped id
        self.assertIn(inj_a.id, inj_b.depends_on)

        # Originator should depend only on B (the terminal)
        orig_node = node_map["orig"]
        self.assertIn(inj_b.id, orig_node.depends_on)
        self.assertNotIn(inj_a.id, orig_node.depends_on)

    def test_inject_subdag_id_collision_avoided(self) -> None:
        """Generated id must not collide with pre-existing plan node ids."""
        existing = PlanNode(id="injected-orig-0", objective="already exists")
        plan = _make_plan("p", [
            PlanNode(id="orig", objective="original"),
            existing,
        ])
        sub = PlanNode(id="any-id", objective="new prereq")
        _inject_subdag(plan, "orig", [sub])

        all_ids = [n.id for n in plan.nodes]
        # No duplicates
        self.assertEqual(len(all_ids), len(set(all_ids)))


class TestSubDagInjectionExecutor(unittest.TestCase):
    """Integration tests for the executor's sub-DAG injection feature."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_dependency_missing_then_success(self) -> None:
        """Handler returns DependencyMissing once, then succeeds on retry."""
        calls: list[str] = []

        async def flaky_handler(inputs: dict) -> dict:
            calls.append("called")
            if len(calls) == 1:
                return {
                    DEPENDENCY_MISSING_KEY: {
                        "objectives": ["fetch prerequisite"],
                    }
                }
            return {"done": True}

        async def prereq_handler(inputs: dict) -> dict:
            calls.append("prereq")
            return {"prereq_done": True}

        registry = ToolRegistry()
        registry.register("main-cap", flaky_handler)
        registry.register("prereq-cap", prereq_handler)

        def fake_planner(dependency_request):
            return [
                PlanNode(
                    id="generated-prereq",
                    objective="fetch prerequisite",
                    assigned_claw="prereq-cap",
                )
            ]

        node = PlanNode(id="main", objective="main task", assigned_claw="main-cap")
        plan = _make_plan("subdag-plan", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            subdag_planner=fake_planner,
        )
        result = _run(executor.run("subdag-plan"))

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        main_node = result.node_map()["main"]
        self.assertEqual(main_node.status, PlanStatus.COMPLETED)

        # Injected node must exist with a unique id
        injected = [n for n in result.nodes if n.id != "main"]
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0].status, PlanStatus.COMPLETED)

        # Dependency wiring: main depends on injected node
        self.assertIn(injected[0].id, main_node.depends_on)

        # Execution order: prereq before main (second call)
        self.assertEqual(calls, ["called", "prereq", "called"])

    def test_multi_node_subdag_injection(self) -> None:
        """Planner returns two nodes (A→B); executor runs A, then B, then original."""
        order: list[str] = []
        call_count = {"n": 0}

        async def main_handler(inputs: dict) -> dict:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {DEPENDENCY_MISSING_KEY: {"objectives": ["step A", "step B"]}}
            order.append("main")
            return {}

        async def step_a(inputs: dict) -> dict:
            order.append("A")
            return {}

        async def step_b(inputs: dict) -> dict:
            order.append("B")
            return {}

        registry = ToolRegistry()
        registry.register("main-cap", main_handler)
        registry.register("cap-a", step_a)
        registry.register("cap-b", step_b)

        def fake_planner(dependency_request):
            node_a = PlanNode(id="sub-a", objective="step A", assigned_claw="cap-a")
            node_b = PlanNode(
                id="sub-b",
                objective="step B",
                assigned_claw="cap-b",
                depends_on=["sub-a"],
            )
            return [node_a, node_b]

        node = PlanNode(id="main", objective="main", assigned_claw="main-cap")
        plan = _make_plan("multi-subdag-plan", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            subdag_planner=fake_planner,
        )
        result = _run(executor.run("multi-subdag-plan"))

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        # 3 nodes total: main + 2 injected
        self.assertEqual(len(result.nodes), 3)

        # Order must be A → B → main
        self.assertEqual(order, ["A", "B", "main"])

        # Injected nodes are COMPLETED
        for n in result.nodes:
            self.assertEqual(n.status, PlanStatus.COMPLETED)

        # main depends on the terminal injected node (sub-b's remapped id)
        main_node = result.node_map()["main"]
        injected_b = next(n for n in result.nodes if n.objective == "step B")
        self.assertIn(injected_b.id, main_node.depends_on)

    def test_no_planner_configured_fails_node(self) -> None:
        """DependencyMissing with no planner configured marks node FAILED."""

        async def missing_dep_handler(inputs: dict) -> dict:
            return {DEPENDENCY_MISSING_KEY: {"objectives": ["something"]}}

        registry = ToolRegistry()
        registry.register("dep-cap", missing_dep_handler)

        node = PlanNode(id="n1", objective="needs dep", assigned_claw="dep-cap")
        plan = _make_plan("no-planner-plan", [node])
        self.store.save(plan)

        executor = Executor(store=self.store, registry=registry, subdag_planner=None)
        result = _run(executor.run("no-planner-plan"))

        self.assertEqual(result.status, PlanStatus.FAILED)
        n = result.node_map()["n1"]
        self.assertEqual(n.status, PlanStatus.FAILED)
        self.assertIsNotNone(n.error)
        self.assertIn("subdag_planner", n.error or "")

    def test_injection_loop_guard(self) -> None:
        """A handler that always returns DependencyMissing is capped and FAILED."""

        async def always_missing(inputs: dict) -> dict:
            return {DEPENDENCY_MISSING_KEY: {"objectives": ["forever"]}}

        async def dummy_prereq(inputs: dict) -> dict:
            return {}

        registry = ToolRegistry()
        registry.register("always-cap", always_missing)
        registry.register("dummy-cap", dummy_prereq)

        def fake_planner(dependency_request):
            return [
                PlanNode(
                    id="prereq",
                    objective="forever",
                    assigned_claw="dummy-cap",
                )
            ]

        node = PlanNode(id="looper", objective="loops forever", assigned_claw="always-cap")
        plan = _make_plan("loop-plan", [node])
        self.store.save(plan)

        executor = Executor(
            store=self.store,
            registry=registry,
            subdag_planner=fake_planner,
        )
        result = _run(executor.run("loop-plan"))

        self.assertEqual(result.status, PlanStatus.FAILED)
        looper = result.node_map()["looper"]
        self.assertEqual(looper.status, PlanStatus.FAILED)
        self.assertIsNotNone(looper.error)
        self.assertIn("loop guard", looper.error or "")

    def test_existing_normal_completion_regression(self) -> None:
        """Normal handler results still complete the plan unchanged."""
        registry = ToolRegistry()

        async def handler(inputs: dict) -> dict:
            return {"result": 42}

        registry.register("cap", handler)
        node = PlanNode(id="n1", objective="normal task", assigned_claw="cap")
        plan = _make_plan("normal-plan", [node])
        self.store.save(plan)

        executor = Executor(store=self.store, registry=registry)
        result = _run(executor.run("normal-plan"))

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        n = result.node_map()["n1"]
        self.assertEqual(n.status, PlanStatus.COMPLETED)
        self.assertEqual(n.inputs.get("result"), 42)


if __name__ == "__main__":
    unittest.main()
