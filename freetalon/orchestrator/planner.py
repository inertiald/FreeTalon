"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations
import re
import shlex
import hashlib
import heapq
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .intake import LLMResponseError, LLMSettings, call_llm, parse_json_object
from .repo_index import (
    build_repo_index,
    duplicate_scaffold_error,
    format_repo_context,
)
from .measure import (
    CRITERION_CUSTOM_CHECK,
    CRITERION_EXIT_CODE,
    CRITERION_FILE_EXISTS,
    CRITERION_FILE_MATCHES,
    CRITERION_METRIC,
    CRITERION_TESTS_PASS,
    custom_checkers,
)
from .models import ExecutionPlan, PlanNode, PlanStatus, TaskIntent
from .prompts import PLANNER_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT
from .tool_registry import KNOWN_CLAW_NAMES, metrics_reported_by


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

def parse_planner_response(
    raw_text: str, *, repo_root: str | Path | None = None
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Validate raw planner output into normalized PlanNodes plus metadata.

    ``repo_root`` overrides the repo the grounding check reads; by default
    it is the checkout holding this package.
    """
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

    # Planner hardening: an invented claw name or an unmeasurable node is a
    # plan error here, where the repair retry can fix it — not at execution
    # time. Hand-built plans bypass this function (they use
    # build_execution_plan directly), so existing callers are unaffected.
    _validate_node_claws(nodes)
    _validate_node_criteria(nodes)
    _validate_node_commands(nodes)
    _validate_node_grounding(nodes, repo_root)

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {"estimated_steps": len(nodes)}

    return nodes, metadata

_CRITERION_TYPES = frozenset(
    {
        CRITERION_EXIT_CODE,
        CRITERION_FILE_EXISTS,
        CRITERION_FILE_MATCHES,
        CRITERION_METRIC,
        CRITERION_TESTS_PASS,
        CRITERION_CUSTOM_CHECK,
    }
)
_COMPARISON_OPS = frozenset({"<", "<=", "==", ">=", ">"})
_REPAIR_TRUNCATION = 4000


def _node_path_strings(node: PlanNode) -> list[str]:
    """All path-like strings a node declares: its inputs and outputs."""
    found: list[str] = []

    def _collect(value: Any) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _collect(item)

    _collect(node.inputs)
    found.extend(str(item) for item in node.outputs)
    return found


def _criterion_path_is_node_path(criterion_path: str, node: PlanNode) -> bool:
    """True if the criterion path is a file this node itself writes or runs.

    Accepts an exact match, a path appearing inside a declared string
    (e.g. a bash command), or a relative path matching the tail of an
    absolute declared path.
    """
    if not criterion_path:
        return False
    for candidate in _node_path_strings(node):
        if not candidate:
            continue
        if criterion_path == candidate:
            return True
        if criterion_path in candidate:
            return True
        if candidate.endswith("/" + criterion_path):
            return True
    return False


def _usable_criterion_error(criterion: Any, node: PlanNode) -> str | None:
    """Return an error string if the criterion is unusable, else None."""
    if not isinstance(criterion, dict):
        return f"criterion is not an object: {criterion!r}"
    ctype = criterion.get("type")
    if ctype not in _CRITERION_TYPES:
        return f"unknown criterion type: {ctype!r}"
    if ctype == CRITERION_EXIT_CODE:
        return None
    if ctype == CRITERION_FILE_EXISTS:
        path = criterion.get("path")
        if not isinstance(path, str) or not path:
            return "file_exists criterion needs a non-empty 'path'"
        if not _criterion_path_is_node_path(path, node):
            return f"file_exists path {path!r} is not a file this node writes or runs"
        return None
    if ctype == CRITERION_FILE_MATCHES:
        path = criterion.get("path")
        if not isinstance(path, str) or not path:
            return "file_matches criterion needs a non-empty 'path'"
        if "sha256" not in criterion and "json_keys" not in criterion:
            return "file_matches criterion needs 'sha256' or 'json_keys'"
        if not _criterion_path_is_node_path(path, node):
            return f"file_matches path {path!r} is not a file this node writes or runs"
        return None
    if ctype == CRITERION_METRIC:
        name = criterion.get("name")
        if not isinstance(name, str) or not name:
            return "metric criterion needs a non-empty 'name'"
        if criterion.get("op") not in _COMPARISON_OPS:
            return f"metric criterion has unknown op: {criterion.get('op')!r}"
        value = criterion.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"metric criterion {name!r} needs a numeric 'value'"
        # Fix 3: the metric must be one the node's claw actually reports.
        # A bash node can never report test_count — that is a plan error,
        # not a measurement to record as "not reported" at execution time.
        claw = node.assigned_claw or ""
        reported = metrics_reported_by(claw)
        if name not in reported:
            if reported:
                return (
                    f"metric criterion {name!r} is not reported by claw {claw!r} "
                    f"(it reports: {', '.join(sorted(reported))})"
                )
            return (
                f"metric criterion {name!r} is not reported by claw {claw!r} "
                "(that claw reports no metrics — use exit_code or file_exists)"
            )
        return None
    if ctype == CRITERION_TESTS_PASS:
        suite = criterion.get("suite")
        if not isinstance(suite, str) or not suite:
            return "tests_pass criterion needs a non-empty 'suite'"
        if not _criterion_path_is_node_path(suite, node):
            return f"tests_pass suite {suite!r} is not a file this node writes or runs"
        return None
    # CRITERION_CUSTOM_CHECK: usable only when the checker is registered.
    name = criterion.get("name")
    if not isinstance(name, str) or not name:
        return "custom_check criterion needs a non-empty 'name'"
    if name not in custom_checkers():
        return f"custom_check {name!r} is not a registered checker"
    return None


# Services a plan may address by port. The API port is HiveConfig.port
# (freetalon/config.py, default 8765); 11434 is Ollama. Port 8000 is the
# OpenAI-compatible LLM backend base URL (intake._OPENAI_BASE_URL) — not a
# service a plan should curl or verify, so it is deliberately absent here.
# A test asserts these match config.py / intake.py so drift is caught.
_KNOWN_SERVICE_PORTS: dict[int, str] = {
    8765: "FreeTalon API",
    11434: "Ollama",
}
_API_ROUTE_NAMES = frozenset(
    {"health", "status", "metrics", "tasks", "resources", "gates", "shutdown"}
)
_PORT_RE = re.compile(r"(?:localhost|127\.0\.0\.1):(\d+)(/[^\s\"']*)?")
_FLAG_PATH_RE = re.compile(r"^-[A-Za-z][^ ]*/")


def _validate_bash_command(node: PlanNode) -> str | None:
    """Return an error if a bash/shell_exec node's command is malformed.

    Checks, in order: the command is a non-empty string; it tokenizes
    (shlex.split — unbalanced quotes fail); it is not words glued together
    (the failed /health run produced ``python3-munittestdiscover-s/...``
    and ``curl-shttp://localhost:8000/health``); any localhost port it
    addresses is a known service (FreeTalon API, Ollama). The glued-word
    checks are heuristics for the observed failure shapes, not a shell
    parser: a single token jamming a program, flags, and a path/URL
    together cannot be a valid invocation.
    """
    cmd = node.inputs.get("command") or node.inputs.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return "bash node needs a non-empty 'command' string"
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        return f"bash command does not parse as a shell command: {exc}"
    if not tokens:
        return "bash node needs a non-empty 'command' string"
    first = tokens[0]
    if not first.startswith(("/", "./", "../")):
        if "://" in first and not first.startswith(("http://", "https://")):
            return (
                f"bash command looks like words glued together (missing spaces): {cmd!r}"
            )
        if "/" in first and first.split("/", 1)[0].count("-") >= 2:
            return (
                f"bash command looks like words glued together (missing spaces): {cmd!r}"
            )
    for token in tokens[1:]:
        if _FLAG_PATH_RE.match(token):
            return (
                f"bash command looks like a flag and a path glued together: "
                f"{token!r} in {cmd!r}"
            )
        if "://" in token and not token.startswith(("http://", "https://")):
            return (
                f"bash command looks like words glued together (missing spaces): {cmd!r}"
            )
    for match in _PORT_RE.finditer(cmd):
        port = int(match.group(1))
        if port in _KNOWN_SERVICE_PORTS:
            continue
        path = match.group(2) or ""
        segment = path.strip("/").split("/", 1)[0] if path else ""
        if segment in _API_ROUTE_NAMES:
            return (
                f"bash command addresses localhost:{port}, but the FreeTalon API "
                f"runs on port 8765, not {port} — use http://localhost:8765{path}"
            )
        return (
            f"bash command addresses unknown service port localhost:{port} — "
            "known services are the FreeTalon API on 8765 and Ollama on 11434"
        )
    return None


def _validate_node_commands(nodes: list[PlanNode]) -> None:
    """Reject bash/shell_exec nodes whose command is malformed (fix 2)."""
    for node in nodes:
        if node.assigned_claw not in ("bash", "shell_exec"):
            continue
        error = _validate_bash_command(node)
        if error is not None:
            raise LLMResponseError(
                f"Planner node '{node.id}' has an invalid bash command: {error}."
            )


def _validate_node_grounding(
    nodes: list[PlanNode], repo_root: str | Path | None = None
) -> None:
    """Reject scaffold nodes that duplicate an existing endpoint/module (fix 1)."""
    index = None
    for node in nodes:
        if node.assigned_claw not in ("scaffold", "file_writer"):
            continue
        target = (
            node.inputs.get("file_path")
            or node.inputs.get("path")
            or (node.outputs[0] if node.outputs else "")
        )
        if not isinstance(target, str) or not target:
            continue
        if index is None:
            index = build_repo_index(repo_root)
        error = duplicate_scaffold_error(target, index)
        if error is not None:
            raise LLMResponseError(
                f"Planner node '{node.id}' duplicates existing code: {error}."
            )


def _validate_node_claws(nodes: list[PlanNode]) -> None:
    """Reject any node whose assigned_claw is not a registered claw."""
    for node in nodes:
        claw = node.assigned_claw or ""
        if claw not in KNOWN_CLAW_NAMES:
            allowed = ", ".join(sorted(KNOWN_CLAW_NAMES))
            raise LLMResponseError(
                f"Planner node '{node.id}' uses unknown claw {claw!r}. "
                f"assigned_claw must be one of: {allowed}."
            )


def _validate_node_criteria(nodes: list[PlanNode]) -> None:
    """Require at least one usable structured criterion per node."""
    for node in nodes:
        criteria = list(node.criteria or [])
        if not criteria:
            raise LLMResponseError(
                f"Planner node '{node.id}' has no 'criteria': every node needs "
                "at least one measurable criterion (exit_code, file_exists, "
                "file_matches, metric, or tests_pass)."
            )
        for criterion in criteria:
            error = _usable_criterion_error(criterion, node)
            if error is not None:
                raise LLMResponseError(
                    f"Planner node '{node.id}' has an unusable criterion: {error}."
                )


def _call_llm_with_repair(
    system_prompt: str,
    user_prompt: str,
    settings: LLMSettings | None,
    *,
    max_repairs: int = 2,
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Call the LLM and parse the plan, repairing rejected output.

    On LLMResponseError the model is re-prompted with the rejection reason
    and its failed output, and asked for a corrected DAG only. Bounded:
    at most ``max_repairs`` repair attempts, then the last error is raised.
    Nothing is silently substituted — a plan the model cannot fix is an
    honest failure.
    """
    prompt = user_prompt
    last_error: LLMResponseError | None = None
    for attempt in range(max_repairs + 1):
        response_text = call_llm(system_prompt, prompt, settings=settings)
        try:
            return parse_planner_response(response_text)
        except LLMResponseError as exc:
            last_error = exc
            if attempt >= max_repairs:
                break
            prompt = (
                f"{user_prompt}\n\n"
                "Your previous output was REJECTED. Do not repeat it.\n"
                f"Rejection reason: {exc}\n"
                "Failed output (do not echo it back, fix it):\n"
                f"{response_text[:_REPAIR_TRUNCATION]}\n\n"
                "Return ONLY a corrected JSON object that fixes the rejection reason."
            )
    assert last_error is not None
    raise last_error


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
    max_repairs: int = 2,
    repo_root: str | Path | None = None,
) -> ExecutionPlan:
    """Generate a validated execution plan DAG for a TaskIntent.

    Rejected output (bad JSON, unknown claw, unmeasurable node) is sent
    back to the model for correction, up to ``max_repairs`` times. The
    prompt carries the repo context (fix 1) so the model plans against
    the code that actually exists.
    """
    context = format_repo_context(build_repo_index(repo_root))
    prompt_input = intent.model_dump_json(indent=2)
    nodes, metadata = _call_llm_with_repair(
        PLANNER_SYSTEM_PROMPT,
        f"{context}\n\nTaskIntent JSON:\n{prompt_input}",
        settings,
        max_repairs=max_repairs,
    )
    return build_execution_plan(intent, nodes, metadata, plan_id=plan_id)


def replan_from_brief(
    brief: dict[str, Any],
    settings: LLMSettings | None = None,
    *,
    max_repairs: int = 2,
    repo_root: str | Path | None = None,
) -> list[PlanNode]:
    """Produce delta-plan nodes from a judge's re-plan brief (M1).

    The brief carries the unchanged goal contract, the failed measurements,
    relevant memory, and the remaining budget. The LLM's response is parsed
    and normalized exactly like an initial plan; the caller (the loop) runs
    the judge's duplicate-approach validation before accepting it.
    """
    import json as _json

    context = format_repo_context(build_repo_index(repo_root))
    nodes, _metadata = _call_llm_with_repair(
        REPLAN_SYSTEM_PROMPT,
        f"{context}\n\nRe-plan brief JSON:\n{_json.dumps(brief, indent=2, default=str)}",
        settings,
        max_repairs=max_repairs,
    )
    return nodes
