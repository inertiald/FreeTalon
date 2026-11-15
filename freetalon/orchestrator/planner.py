"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations
import re
import hashlib
import heapq
from typing import Any

from pydantic import ValidationError

from .intake import LLMResponseError, LLMSettings, call_llm, parse_json_object
from .models import ExecutionPlan, PlanNode, PlanStatus, TaskIntent
from .prompts import PLANNER_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT


def _plan_id_for_intent(intent: TaskIntent) -> str:
    digest = hashlib.sha256(intent.model_dump_json().encode("utf-8")).hexdigest()
    return f"plan-{digest[:12]}"


def _normalized_node_sort_key(node_id: str) -> int:
    return int(node_id.split("-")[1])


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

    ready = [(original_order[node.id], node.id) for node in nodes if indegree[node.id] == 0]
    heapq.heapify(ready)
    ordered_ids: list[str] = []
    while ready:
        _, current = heapq.heappop(ready)
        ordered_ids.append(current)
        for downstream in sorted(adjacency[current], key=original_order.__getitem__):
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                heapq.heappush(ready, (original_order[downstream], downstream))

    if len(ordered_ids) != len(nodes):
        raise LLMResponseError("Planner response contained a dependency cycle.")

    original_to_normalized_id = {
        original_id: f"node-{index}" for index, original_id in enumerate(ordered_ids, start=1)
    }
    normalized_nodes: list[PlanNode] = []
    for original_id in ordered_ids:
        node = node_map[original_id]
        normalized_dependencies = sorted(
            {original_to_normalized_id[dependency] for dependency in node.depends_on},
            key=_normalized_node_sort_key,
        )
        normalized_nodes.append(
            node.model_copy(
                update={
                    "id": original_to_normalized_id[original_id],
                    "depends_on": normalized_dependencies,
                    "status": PlanStatus.DRAFT,
                    "error": None,
                }
            )
        )
    return normalized_nodes

def sanitize_llm_output(raw_text: str) -> str:
    """Clean raw SentencePiece UNK_BYTE token artifacts output by Gemma models."""
    # Replaces patterns like '[UNK_BYTE_0xe29681▁a]a' with ' a'
    cleaned = re.sub(r'\[UNK_BYTE_[^\]]+▁?([^\]]*)\]', r' \1', raw_text)
    # Clean up any leftover lower block characters or non-breaking spaces
    cleaned = cleaned.replace("\xe2\x96\x81", " ").replace("▁", "")
    return cleaned

def parse_planner_response(raw_text: str) -> tuple[list[PlanNode], dict[str, Any]]:
    """Validate raw planner output into normalized PlanNodes plus metadata."""
    raw_text = sanitize_llm_output(raw_text)
    print(f"\n--- RAW PLANNER OUTPUT ---\n{raw_text}\n--------------------------\n")
    
    payload = parse_json_object(raw_text)
    if not isinstance(payload, dict):
        raise LLMResponseError("Planner response must be a JSON object.")

    nodes_raw = payload.get("nodes")
    if not isinstance(nodes_raw, list):
        raise LLMResponseError("Planner output must contain a 'nodes' array.")

    # Defensive normalization: coerce plain strings into single-element lists
    for node in nodes_raw:
        if isinstance(node, dict):
            if isinstance(node.get("acceptance"), str):
                node["acceptance"] = [node["acceptance"]]
            elif node.get("acceptance") is None:
                node["acceptance"] = []

            if isinstance(node.get("outputs"), str):
                node["outputs"] = [node["outputs"]]
            elif node.get("outputs") is None:
                node["outputs"] = []

            if isinstance(node.get("depends_on"), str):
                node["depends_on"] = [node["depends_on"]]
            elif node.get("depends_on") is None:
                node["depends_on"] = []

    # Normalize: topologically order the DAG and assign deterministic
    # node-1, node-2, ... ids, remapping depends_on. Without this the raw
    # LLM ids/order leak through and downstream code cannot rely on
    # dependency order.
    nodes = normalize_plan_nodes(nodes_raw)

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {"estimated_steps": len(nodes)}

    return nodes, metadata

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


def replan_from_brief(
    brief: dict[str, Any],
    settings: LLMSettings | None = None,
) -> list[PlanNode]:
    """Produce delta-plan nodes from a judge's re-plan brief (M1).

    The brief carries the unchanged goal contract, the failed measurements,
    relevant memory, and the remaining budget. The LLM's response is parsed
    and normalized exactly like an initial plan; the caller (the loop) runs
    the judge's duplicate-approach validation before accepting it.
    """
    import json as _json

    response_text = call_llm(
        REPLAN_SYSTEM_PROMPT,
        f"Re-plan brief JSON:\n{_json.dumps(brief, indent=2, default=str)}",
        settings=settings,
    )
    nodes, _metadata = parse_planner_response(response_text)
    return nodes
