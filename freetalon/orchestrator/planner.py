"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import ValidationError

from .intake import LLMResponseError, LLMSettings, call_llm, parse_json_object
from .models import ExecutionPlan, PlanNode, PlanStatus, TaskIntent
from .prompts import PLANNER_SYSTEM_PROMPT


def _plan_id_for_intent(intent: TaskIntent) -> str:
    digest = hashlib.sha256(intent.model_dump_json().encode("utf-8")).hexdigest()
    return f"plan-{digest[:12]}"


def _validate_raw_nodes(raw_nodes: list[Any]) -> list[PlanNode]:
    try:
        return [PlanNode.model_validate(node) for node in raw_nodes]
    except ValidationError as exc:
        raise LLMResponseError(f"Planner response did not match PlanNode schema: {exc}") from exc


def _coerce_nodes(nodes: list[PlanNode] | list[dict[str, Any]]) -> list[PlanNode]:
    if nodes and isinstance(nodes[0], PlanNode):
        return list(nodes)
    return _validate_raw_nodes(list(nodes))


def normalize_plan_nodes(nodes: list[PlanNode] | list[dict[str, Any]]) -> list[PlanNode]:
    """Validate a DAG, topologically order it, and assign deterministic node ids."""
    nodes = _coerce_nodes(nodes)
    if not nodes:
        raise LLMResponseError("Planner response must include at least one plan node.")

    node_map = {node.id: node for node in nodes}
    if len(node_map) != len(nodes):
        raise LLMResponseError("Planner response contained duplicate node ids.")

    original_order = {node.id: index for index, node in enumerate(nodes)}
    adjacency: dict[str, list[str]] = {node.id: [] for node in nodes}
    indegree = {node.id: 0 for node in nodes}

    for node in nodes:
        unique_dependencies: list[str] = []
        seen_dependencies: set[str] = set()
        for dependency in node.depends_on:
            if dependency == node.id:
                raise LLMResponseError(f"Planner node '{node.id}' cannot depend on itself.")
            if dependency not in node_map:
                raise LLMResponseError(
                    f"Planner node '{node.id}' depends on unknown node '{dependency}'."
                )
            if dependency not in seen_dependencies:
                seen_dependencies.add(dependency)
                unique_dependencies.append(dependency)
        indegree[node.id] = len(unique_dependencies)
        for dependency in unique_dependencies:
            adjacency[dependency].append(node.id)

    ready = [node.id for node in nodes if indegree[node.id] == 0]
    ordered_ids: list[str] = []
    while ready:
        ready.sort(key=original_order.__getitem__)
        current = ready.pop(0)
        ordered_ids.append(current)
        for downstream in sorted(adjacency[current], key=original_order.__getitem__):
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                ready.append(downstream)

    if len(ordered_ids) != len(nodes):
        raise LLMResponseError("Planner response contained a dependency cycle.")

    id_map = {original_id: f"node-{index}" for index, original_id in enumerate(ordered_ids, start=1)}
    normalized_nodes: list[PlanNode] = []
    for original_id in ordered_ids:
        node = node_map[original_id]
        normalized_dependencies = sorted(
            {id_map[dependency] for dependency in node.depends_on},
            key=lambda dependency_id: int(dependency_id.split("-")[1]),
        )
        normalized_nodes.append(
            node.model_copy(
                update={
                    "id": id_map[original_id],
                    "depends_on": normalized_dependencies,
                    "status": PlanStatus.DRAFT,
                    "error": None,
                }
            )
        )
    return normalized_nodes


def parse_planner_response(raw_text: str) -> tuple[list[PlanNode], dict[str, Any]]:
    """Validate raw planner output into normalized PlanNodes plus metadata."""
    payload = parse_json_object(raw_text)
    allowed_keys = {"nodes", "metadata"}
    unexpected_keys = sorted(set(payload) - allowed_keys)
    if unexpected_keys:
        raise LLMResponseError(
            f"Planner response contained unexpected top-level fields: {unexpected_keys}"
        )

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise LLMResponseError("Planner response must include a 'nodes' array.")

    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise LLMResponseError("Planner response 'metadata' field must be a JSON object.")

    return normalize_plan_nodes(_validate_raw_nodes(raw_nodes)), metadata


def build_execution_plan(
    intent: TaskIntent,
    nodes: list[PlanNode] | list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
    *,
    plan_id: str | None = None,
) -> ExecutionPlan:
    """Build a validated ExecutionPlan from an intent and node list."""
    return ExecutionPlan(
        plan_id=plan_id or _plan_id_for_intent(intent),
        intent=intent,
        nodes=normalize_plan_nodes(nodes),
        status=PlanStatus.DRAFT,
        metadata=metadata or {},
    )


def plan_task_intent(
    intent: TaskIntent,
    settings: LLMSettings | None = None,
    *,
    plan_id: str | None = None,
) -> ExecutionPlan:
    """Generate a validated execution plan DAG for a TaskIntent."""
    prompt_input = intent.model_dump_json(indent=2)
    response_text = call_llm(
        PLANNER_SYSTEM_PROMPT,
        f"TaskIntent JSON:\n{prompt_input}",
        settings=settings,
    )
    nodes, metadata = parse_planner_response(response_text)
    return build_execution_plan(intent, nodes, metadata, plan_id=plan_id)
