"""Tests for orchestrator models and state persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from freetalon.orchestrator.state_store import ExecutionPlanStateStore


def _make_plan(plan_id: str = "plan-001") -> ExecutionPlan:
    intent = TaskIntent(goal="Build a trading dashboard")
    node = PlanNode(id="node-1", objective="Scaffold project")
    return ExecutionPlan(plan_id=plan_id, intent=intent, nodes=[node])


class TestModels(unittest.TestCase):
    """Smoke tests for Pydantic model construction and validation."""

    def test_task_intent_defaults(self) -> None:
        intent = TaskIntent(goal="Do something")
        self.assertEqual(intent.project_type, "general")
        self.assertEqual(intent.capabilities, [])
        self.assertEqual(intent.constraints, {})
        self.assertEqual(intent.missing_inputs, [])

    def test_plan_node_defaults(self) -> None:
        node = PlanNode(id="n1", objective="Step one")
        self.assertEqual(node.status, PlanStatus.DRAFT)
        self.assertIsNone(node.error)
        self.assertEqual(node.depends_on, [])

    def test_execution_plan_touch_updates_timestamp(self) -> None:
        plan = _make_plan()
        original = plan.updated_at
        plan.touch()
        # updated_at should be >= original (may be equal in fast systems)
        self.assertGreaterEqual(plan.updated_at, original)

    def test_execution_plan_node_map(self) -> None:
        plan = _make_plan()
        nmap = plan.node_map()
        self.assertIn("node-1", nmap)
        self.assertEqual(nmap["node-1"].objective, "Scaffold project")

    def test_extra_fields_forbidden(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TaskIntent(goal="x", unknown_field="y")  # type: ignore[call-arg]


class TestStateStore(unittest.TestCase):
    """Tests for ExecutionPlanStateStore CRUD operations."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tmpdir.name) / "test_state.db"
        self.store = ExecutionPlanStateStore(db_path=db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmpdir.cleanup()

    def test_save_and_load_round_trip(self) -> None:
        plan = _make_plan("round-trip-plan")
        self.store.save(plan)
        loaded = self.store.load("round-trip-plan")
        self.assertIsNotNone(loaded)
        assert loaded is not None  # appease type checkers
        self.assertEqual(loaded.plan_id, plan.plan_id)
        self.assertEqual(loaded.intent.goal, plan.intent.goal)
        self.assertEqual(len(loaded.nodes), 1)
        self.assertEqual(loaded.nodes[0].id, "node-1")

    def test_load_missing_returns_none(self) -> None:
        result = self.store.load("nonexistent-id")
        self.assertIsNone(result)

    def test_delete_existing_returns_true(self) -> None:
        plan = _make_plan("delete-me")
        self.store.save(plan)
        self.assertTrue(self.store.delete("delete-me"))
        self.assertIsNone(self.store.load("delete-me"))

    def test_delete_nonexistent_returns_false(self) -> None:
        self.assertFalse(self.store.delete("ghost-plan"))

    def test_list_ids_empty(self) -> None:
        self.assertEqual(self.store.list_ids(), [])

    def test_list_ids_multiple_plans(self) -> None:
        for i in range(3):
            self.store.save(_make_plan(f"plan-{i}"))
        ids = self.store.list_ids()
        self.assertEqual(sorted(ids), ["plan-0", "plan-1", "plan-2"])

    def test_save_upserts_on_same_id(self) -> None:
        plan = _make_plan("upsert-plan")
        self.store.save(plan)
        plan.status = PlanStatus.RUNNING
        plan.touch()
        self.store.save(plan)
        loaded = self.store.load("upsert-plan")
        assert loaded is not None
        self.assertEqual(loaded.status, PlanStatus.RUNNING)
        self.assertEqual(len(self.store.list_ids()), 1)

    def test_persistence_across_store_instances(self) -> None:
        """Data written by one store instance is visible to a new instance."""
        db_path = Path(self.tmpdir.name) / "persist_test.db"
        store1 = ExecutionPlanStateStore(db_path=db_path)
        store1.save(_make_plan("persist-plan"))
        store1.close()

        store2 = ExecutionPlanStateStore(db_path=db_path)
        loaded = store2.load("persist-plan")
        store2.close()
        self.assertIsNotNone(loaded)

    def test_full_round_trip_no_data_loss(self) -> None:
        """All fields survive a serialization/deserialization cycle."""
        intent = TaskIntent(
            goal="Comprehensive test",
            project_type="web_app",
            capabilities=["browser", "code"],
            constraints={"budget": 100},
            missing_inputs=["api_key"],
        )
        node = PlanNode(
            id="n1",
            objective="Do work",
            depends_on=["n0"],
            assigned_claw="worker-1",
            status=PlanStatus.RUNNING,
            inputs={"key": "value"},
            outputs=["output.txt"],
            acceptance=["file exists"],
            error=None,
        )
        plan = ExecutionPlan(
            plan_id="full-plan",
            intent=intent,
            nodes=[node],
            status=PlanStatus.RUNNING,
            metadata={"owner": "test"},
        )
        self.store.save(plan)
        loaded = self.store.load("full-plan")
        assert loaded is not None
        self.assertEqual(loaded.model_dump(), plan.model_dump())


if __name__ == "__main__":
    unittest.main()
