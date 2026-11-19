"""Tests for pure DAG visualization helpers."""

from __future__ import annotations

import unittest

from freetalon.orchestrator.dag import compute_dag_levels
from freetalon.orchestrator.models import ExecutionPlan, PlanNode, TaskIntent


def _make_plan(nodes: list[PlanNode]) -> ExecutionPlan:
    return ExecutionPlan(plan_id="dag-test", intent=TaskIntent(goal="Test DAG"), nodes=nodes)


class TestComputeDagLevels(unittest.TestCase):
    """Unit tests for DAG depth derivation."""

    def test_linear_chain_assigns_increasing_depths(self) -> None:
        plan = _make_plan(
            [
                PlanNode(id="n1", objective="root"),
                PlanNode(id="n2", objective="middle", depends_on=["n1"]),
                PlanNode(id="n3", objective="leaf", depends_on=["n2"]),
            ]
        )

        self.assertEqual(compute_dag_levels(plan), {"n1": 0, "n2": 1, "n3": 2})

    def test_diamond_uses_longest_parent_chain(self) -> None:
        plan = _make_plan(
            [
                PlanNode(id="n1", objective="root"),
                PlanNode(id="n2", objective="left", depends_on=["n1"]),
                PlanNode(id="n3", objective="right", depends_on=["n1"]),
                PlanNode(id="n4", objective="merge", depends_on=["n2", "n3"]),
            ]
        )

        self.assertEqual(compute_dag_levels(plan)["n4"], 2)

    def test_single_root_is_depth_zero(self) -> None:
        plan = _make_plan([PlanNode(id="n1", objective="only node")])

        self.assertEqual(compute_dag_levels(plan), {"n1": 0})

    def test_unknown_dependency_falls_back_safely(self) -> None:
        plan = _make_plan(
            [PlanNode(id="n1", objective="dangling dependency", depends_on=["ghost"])]
        )

        self.assertEqual(compute_dag_levels(plan), {"n1": 0})

    def test_cycle_returns_safe_fallback_without_raising(self) -> None:
        plan = _make_plan(
            [
                PlanNode(id="n1", objective="first", depends_on=["n2"]),
                PlanNode(id="n2", objective="second", depends_on=["n1"]),
            ]
        )

        self.assertEqual(compute_dag_levels(plan), {"n1": 0, "n2": 0})


if __name__ == "__main__":
    unittest.main()
