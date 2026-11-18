"""Tests for tool_registry and executor modules."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.executor import (
    DEPENDENCY_MISSING_KEY,
    MAX_INJECTION_ATTEMPTS,
    Executor,
    ExecutorError,
    _SUITE_NODE_TIMEOUT_S,
    _all_dependencies_met,
    _collect_runnable,
    _command_runs_full_suite,
    _count_in_flight,
    _derive_plan_status,
    _effective_node_timeout,
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
from freetalon.orchestrator.measure import VERDICT_FAIL, VERDICT_PASS
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    UnknownCapabilityError,
    register_default_claws,
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


# ── Scaffold guard (retry #14) ───────────────────────────────────────────────


class TestScaffoldGuardExecutor(unittest.TestCase):
    """Scaffold nodes are measurable; placeholder overwrites are refused (retry #14).

    Retry #14 (run run-20261117T090107Z-c77c7a): the v2 scaffold wrote 39
    bytes and measured UNMEASURABLE ("no exit code reported"), and the v3
    scaffold clobbered freetalon/api.py with a 1-line placeholder comment
    (219 lines -> 1 line) without the loop noticing. Two fixes: the
    scaffold claw returns exit_code 0 on a successful write, so an
    exit_code or file_exists criterion measures PASS; and the claw refuses
    to overwrite an existing code file with placeholder content — the
    refusal surfaces as a measured FAIL and the file is unchanged.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmpdir.name)
        self.registry = ToolRegistry()
        register_default_claws(self.registry)
        # A run store under tmp: without one, Executor._node_workdir
        # falls back to Path.cwd() and a scaffold would write into the
        # repo root. The run dir is where the node writes and measures.
        self.run_store = RunStore(Path(self.tmpdir.name) / "runs")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _run_scaffold_plan(self, plan_id: str, node: PlanNode) -> ExecutionPlan:
        plan = _make_plan(plan_id, [node])
        self.store.save(plan)
        executor = Executor(
            store=self.store,
            registry=self.registry,
            run_store=self.run_store,
            run_id="test-run",
        )
        return _run(executor.run(plan_id))

    def test_scaffold_node_measures_pass(self) -> None:
        node = PlanNode(
            id="s1",
            objective="Scaffold the health module",
            assigned_claw="scaffold",
            inputs={"file_path": "health.py", "content": "STATUS = \"ready\"\n"},
            outputs=["health.py"],
            criteria=[
                {"type": "exit_code", "equals": 0},
                {"type": "file_exists", "path": "health.py"},
            ],
        )
        result = self._run_scaffold_plan("scaffold-pass", node)
        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertEqual(result.nodes[0].measurement["verdict"], VERDICT_PASS)
        written = (
            Path(self.tmpdir.name) / "runs" / "test-run" / "nodes" / "s1" / "health.py"
        )
        self.assertTrue(written.exists())
        self.assertEqual(written.read_text(encoding="utf-8"), "STATUS = \"ready\"\n")

    def test_placeholder_overwrite_refused_at_handler(self) -> None:
        target = Path(self.tmpdir.name) / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        handler = self.registry.resolve("scaffold")
        with self.assertRaises(ValueError):
            _run(handler({"file_path": str(target), "content": "# Placeholder.\n"}))
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 1\n")

    def test_docstring_only_overwrite_refused_at_handler(self) -> None:
        target = Path(self.tmpdir.name) / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        handler = self.registry.resolve("scaffold")
        with self.assertRaises(ValueError):
            _run(
                handler(
                    {"file_path": str(target), "content": '"""Health endpoint."""\n'}
                )
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 1\n")

    def test_real_code_overwrite_succeeds_at_handler(self) -> None:
        target = Path(self.tmpdir.name) / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        handler = self.registry.resolve("scaffold")
        envelope = _run(handler({"file_path": str(target), "content": "x = 2\n"}))
        self.assertEqual(envelope["exit_code"], 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 2\n")

    def test_comment_stub_to_new_file_succeeds_at_handler(self) -> None:
        target = Path(self.tmpdir.name) / "new_mod.py"
        handler = self.registry.resolve("scaffold")
        envelope = _run(handler({"file_path": str(target), "content": "# Stub.\n"}))
        self.assertEqual(envelope["exit_code"], 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "# Stub.\n")

    def test_placeholder_overwrite_fails_measured_at_executor(self) -> None:
        target = Path(self.tmpdir.name) / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        node = PlanNode(
            id="s1",
            objective="Scaffold the health endpoint",
            assigned_claw="scaffold",
            inputs={
                "file_path": str(target),
                "content": "# Placeholder for health endpoint logic.\n",
            },
            outputs=[str(target)],
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        result = self._run_scaffold_plan("scaffold-placeholder", node)
        self.assertEqual(result.nodes[0].status, PlanStatus.FAILED)
        self.assertEqual(result.nodes[0].measurement["verdict"], VERDICT_FAIL)
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 1\n")


# ── Suite-node timeout floor (planner A/B refactor, defect 2) ────────────────


class TestSuiteNodeTimeout(unittest.TestCase):
    """A bash node that runs the full test suite gets a floor under
    its timeout in the executor: the suite takes ~385s and the
    default node timeout is 300s, so retry #33's accepted plan could
    never pass — its suite node was killed at 300.0s. The floor is
    derived where the timeout is enforced, so any plan that runs
    the suite is executable within its timeouts by construction.
    """

    def _node(self, command: str, timeout_s: float = 300.0) -> PlanNode:
        return PlanNode(
            id="n1",
            objective="run tests",
            assigned_claw="bash",
            inputs={"command": command},
            timeout_s=timeout_s,
        )

    def test_full_suite_shapes_detected(self) -> None:
        for command in (
            "cd /repo && .venv/bin/python -m unittest discover -s tests",
            ".venv/bin/python -m unittest discover -s tests -v",
            "python -m unittest",
            "python -m unittest -v",
            "pytest",
            "pytest tests",
            "python -m pytest tests/",
            "make test && python -m unittest discover",
        ):
            self.assertTrue(_command_runs_full_suite(command), command)

    def test_scoped_runs_not_detected(self) -> None:
        for command in (
            "python -m unittest tests.test_api",
            "python -m unittest tests.test_api.TestHealth.test_ok",
            "pytest tests/test_api.py",
            "pytest tests/test_api.py::test_ok",
            "python3 /home/seeker/freetalon-workspace/test_health.py",
            "curl -s http://localhost:8765/health",
        ):
            self.assertFalse(_command_runs_full_suite(command), command)

    def test_suite_node_timeout_floored(self) -> None:
        node = self._node("python -m unittest discover -s tests", timeout_s=30.0)
        self.assertEqual(_effective_node_timeout(node), _SUITE_NODE_TIMEOUT_S)
        self.assertGreater(_SUITE_NODE_TIMEOUT_S, 385.0)

    def test_declared_larger_timeout_kept(self) -> None:
        node = self._node(
            "python -m unittest discover -s tests", timeout_s=1800.0
        )
        self.assertEqual(_effective_node_timeout(node), 1800.0)

    def test_scoped_and_non_bash_nodes_untouched(self) -> None:
        scoped = self._node("python -m unittest tests.test_api", timeout_s=30.0)
        self.assertEqual(_effective_node_timeout(scoped), 30.0)
        other = PlanNode(
            id="n2",
            objective="write",
            assigned_claw="scaffold",
            inputs={"command": "python -m unittest discover -s tests"},
            timeout_s=30.0,
        )
        self.assertEqual(_effective_node_timeout(other), 30.0)

    def test_suite_node_completes_past_its_declared_timeout(self) -> None:
        # Behavioral: the handler runs 1.0s; the node declares 0.2s.
        # A scoped command times out; the suite command completes
        # because the floor, not the declared value, is enforced.
        async def slow_handler(inputs: dict) -> dict:
            await asyncio.sleep(1.0)
            return {"done": True}

        for index, (command, expected) in enumerate(
            (
                ("python -m unittest discover -s tests", PlanStatus.COMPLETED),
                ("python -m unittest tests.test_api", PlanStatus.FAILED),
            )
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                store = _make_store(tmpdir)
                registry = ToolRegistry()
                registry.register("bash", slow_handler)
                plan_id = f"suite-timeout-{index}"
                store.save(_make_plan(plan_id, [self._node(command, timeout_s=0.2)]))
                result = _run(Executor(store=store, registry=registry).run(plan_id))
                self.assertEqual(result.nodes[0].status, expected, command)


# ── Repo-root coherence (cycle 4) ────────────────────────────────────────────


class TestExecutorRepoRootCoherence(unittest.TestCase):
    """A repo-grounded plan executes against the repo it was planned against.

    Cycle 4 (the /health re-run, head ddb3c0d): plan-7885a4ef9d99 was
    accepted and executed in both live runs, nodes 1-2 PASS, but node-3
    failed exit 127 in all 3 plan versions of both runs: the plan runs
    ./.venv/bin/python — the grounded form the planner validates
    against the repo root (_bash_interpreter_errors: the path "must
    exist there") — while the executor ran the bash claw from the
    node's run directory, where ./.venv does not exist. Same split for
    writes: the planner resolves scaffold targets against the repo
    root, the scaffold claw resolved them against the run directory,
    so one test file landed in the workspace and another in the repo.

    The fix is executor-side: the plan carries the repo root it was
    grounded against in metadata["repo_root"] (stamped by
    plan_task_intent), and Executor._node_workdir — the one base that
    feeds the bash cwd, the scaffold handler's relative resolution,
    and measurement — returns that root for a stamped plan. Plans
    with no stamp keep the run-directory base exactly as before.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmpdir.name)
        self.registry = ToolRegistry()
        register_default_claws(self.registry)
        self.run_store = RunStore(Path(self.tmpdir.name) / "runs")
        # A fake repo with its own .venv interpreter (a symlink to the
        # real one) and a tests/ directory.
        self.repo = Path(self.tmpdir.name) / "repo"
        venv_bin = self.repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        (self.repo / "tests").mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _plan(
        self,
        plan_id: str,
        nodes: list[PlanNode],
        *,
        repo_root: str | None = None,
    ) -> ExecutionPlan:
        metadata = {} if repo_root is None else {"repo_root": repo_root}
        return ExecutionPlan(
            plan_id=plan_id,
            intent=TaskIntent(goal="Test plan"),
            nodes=nodes,
            metadata=metadata,
        )

    def _run_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        self.store.save(plan)
        executor = Executor(
            store=self.store,
            registry=self.registry,
            run_store=self.run_store,
            run_id="test-run",
        )
        return _run(executor.run(plan.plan_id))

    def _node_dir(self, node_id: str) -> Path:
        return self.run_store.node_dir("test-run", node_id, 1)

    def _place_tool(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        tool = directory / "tool.sh"
        tool.write_text("#!/bin/sh\nprintf ran > marker.txt\n", encoding="utf-8")
        tool.chmod(0o755)

    def test_relative_interpreter_resolves_against_repo_root(self) -> None:
        # The re-run's node-3 shape: a relative interpreter path in a
        # repo-grounded plan, executed with a run store attached (so
        # the pre-fix base is the node run directory -> exit 127).
        node = PlanNode(
            id="n3",
            objective="Run the health tests with the repo venv",
            assigned_claw="bash",
            inputs={
                "command": (
                    "./.venv/bin/python -c "
                    "'from pathlib import Path; "
                    "Path(\"marker.txt\").write_text(\"ran\")'"
                )
            },
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        plan = self._plan("repo-interp", [node], repo_root=str(self.repo))
        result = self._run_plan(plan)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertEqual(result.nodes[0].exit_code, 0)
        # The marker proves the cwd: written relative, landed at the
        # repo root — not in the node's run directory.
        self.assertEqual((self.repo / "marker.txt").read_text(encoding="utf-8"), "ran")
        self.assertFalse((self._node_dir("n3") / "marker.txt").exists())

    def test_relative_scaffold_target_lands_in_repo(self) -> None:
        # The two-landing-spots tangle: a repo-relative scaffold target
        # in a repo-grounded plan lands in the repo, not the run dir.
        content = (
            "import unittest\n\n\n"
            "class TestHealth(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n"
        )
        node = PlanNode(
            id="s1",
            objective="Write the health tests",
            assigned_claw="scaffold",
            inputs={"file_path": "tests/test_health.py", "content": content},
            outputs=["tests/test_health.py"],
            criteria=[
                {"type": "exit_code", "equals": 0},
                {"type": "file_exists", "path": "tests/test_health.py"},
            ],
        )
        plan = self._plan("repo-scaffold", [node], repo_root=str(self.repo))
        result = self._run_plan(plan)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertEqual(result.nodes[0].measurement["verdict"], VERDICT_PASS)
        written = self.repo / "tests" / "test_health.py"
        self.assertEqual(written.read_text(encoding="utf-8"), content)
        self.assertFalse(
            (self._node_dir("s1") / "tests" / "test_health.py").exists()
        )

    def test_explicit_cd_still_governs(self) -> None:
        # A command that cds explicitly behaves as before: the shell's
        # own cd wins over the injected base (the re-run's node-4 shape).
        elsewhere = Path(self.tmpdir.name) / "elsewhere"
        self._place_tool(elsewhere)
        node = PlanNode(
            id="n4",
            objective="Run the tool from its own directory",
            assigned_claw="bash",
            inputs={"command": f"cd {elsewhere} && ./tool.sh"},
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        plan = self._plan("repo-explicit-cd", [node], repo_root=str(self.repo))
        result = self._run_plan(plan)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertTrue((elsewhere / "marker.txt").exists())
        self.assertFalse((self.repo / "marker.txt").exists())

    def test_absolute_interpreter_path_unchanged(self) -> None:
        marker = Path(self.tmpdir.name) / "abs-marker.txt"
        command = (
            f"{self.repo}/.venv/bin/python -c "
            f"'from pathlib import Path; Path(\"{marker}\").write_text(\"ran\")'"
        )
        node = PlanNode(
            id="n1",
            objective="Run with an absolute interpreter path",
            assigned_claw="bash",
            inputs={"command": command},
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        plan = self._plan("repo-absolute", [node], repo_root=str(self.repo))
        result = self._run_plan(plan)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertEqual(marker.read_text(encoding="utf-8"), "ran")

    def test_plan_without_repo_root_keeps_run_dir_base(self) -> None:
        # No stamp: bash cwd and scaffold targets stay in the node run
        # directories, exactly as before cycle 4.
        self._place_tool(self._node_dir("n1"))
        bash_node = PlanNode(
            id="n1",
            objective="Run the local tool",
            assigned_claw="bash",
            inputs={"command": "./tool.sh"},
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        scaffold_node = PlanNode(
            id="s1",
            objective="Write a scratch module",
            assigned_claw="scaffold",
            inputs={"file_path": "out.py", "content": "X = 1\n"},
            outputs=["out.py"],
            criteria=[
                {"type": "exit_code", "equals": 0},
                {"type": "file_exists", "path": "out.py"},
            ],
        )
        plan = self._plan("no-repo-plan", [bash_node, scaffold_node])
        result = self._run_plan(plan)
        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertTrue((self._node_dir("n1") / "marker.txt").exists())
        self.assertEqual(
            (self._node_dir("s1") / "out.py").read_text(encoding="utf-8"), "X = 1\n"
        )
        self.assertFalse((self.repo / "out.py").exists())

    def test_stale_repo_root_stamp_falls_back_to_run_dir(self) -> None:
        # A stamp naming a directory that no longer exists is ignored:
        # the run falls back to today's base instead of crashing.
        self._place_tool(self._node_dir("n1"))
        node = PlanNode(
            id="n1",
            objective="Run the local tool",
            assigned_claw="bash",
            inputs={"command": "./tool.sh"},
            criteria=[{"type": "exit_code", "equals": 0}],
        )
        plan = self._plan(
            "stale-repo-plan",
            [node],
            repo_root=str(Path(self.tmpdir.name) / "no-such-repo"),
        )
        result = self._run_plan(plan)
        self.assertEqual(result.nodes[0].status, PlanStatus.COMPLETED)
        self.assertTrue((self._node_dir("n1") / "marker.txt").exists())
