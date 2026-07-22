"""Pure DAG helpers for orchestration plan visualization."""

from __future__ import annotations

from collections import deque

from .models import ExecutionPlan


def compute_dag_levels(plan: ExecutionPlan) -> dict[str, int]:
    """Return node depths keyed by node id using the longest known dependency chain.

    Roots (nodes with no known dependencies) are depth 0. Missing dependency ids are
    ignored for depth calculation. Cycles are tolerated by leaving unresolved nodes at
    the safest known fallback depth accumulated from any acyclic parents, or 0.
    """

    nodes = list(plan.nodes)
    depths = {node.id: 0 for node in nodes}
    known_ids = {node.id for node in nodes}
    parents_by_id = {
        node.id: [dep_id for dep_id in node.depends_on if dep_id in known_ids]
        for node in nodes
    }
    children_by_id = {node.id: [] for node in nodes}
    indegree = {node.id: len(parents_by_id[node.id]) for node in nodes}

    for node in nodes:
        for dep_id in parents_by_id[node.id]:
            children_by_id[dep_id].append(node.id)

    ready = deque(node.id for node in nodes if indegree[node.id] == 0)
    while ready:
        node_id = ready.popleft()
        next_depth = depths[node_id] + 1
        for child_id in children_by_id[node_id]:
            if next_depth > depths[child_id]:
                depths[child_id] = next_depth
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)

    return depths
