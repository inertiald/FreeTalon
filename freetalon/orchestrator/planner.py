"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations
import json
import re
import shlex
import hashlib
import heapq
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from .intake import LLMResponseError, LLMSettings, call_llm, parse_json_object
from .repo_index import (
    build_repo_index,
    resolve_repo_root,
    duplicate_scaffold_error,
    format_repo_context,
    framework_in_text,
)
from .measure import (
    CRITERION_CUSTOM_CHECK,
    CRITERION_EXIT_CODE,
    CRITERION_FILE_EXISTS,
    CRITERION_FILE_MATCHES,
    CRITERION_METRIC,
    CRITERION_TESTS_PASS,
    custom_checkers,
    measurement_failed_detail,
)
from .models import ExecutionPlan, PlanNode, PlanStatus, TaskIntent
from .prompts import PLANNER_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT
from .tool_registry import KNOWN_CLAW_NAMES, metrics_reported_by


def _plan_id_for_intent(intent: TaskIntent) -> str:
    digest = hashlib.sha256(intent.model_dump_json().encode("utf-8")).hexdigest()
    return f"plan-{digest[:12]}"


def _normalized_node_sort_key(node_id: str) -> int:
    return int(node_id.split("-")[1])


# The PlanNode shape, spelled out wherever a node-schema error is
# reported. Retry #7: the model invented write_file/execute_command
# node types with no objective and no assigned_claw, and the structural
# raise never showed it the shape it was supposed to use. Short on
# purpose: it rides along in every schema rejection and in the planner
# prompt's schema rules.
_NODE_SHAPE_EXAMPLE = (
    'every node MUST be a PlanNode object: {"id": "step_1", "objective": '
    '"<what the node delivers>", "depends_on": [], "assigned_claw": '
    '"scaffold" | "bash", "inputs": {"file_path": "<path>", "content": '
    '"<file contents>"} | {"command": "<shell command>"}, "outputs": '
    '["<path or result>"], "acceptance": ["<human-readable check>"], '
    '"criteria": [{"type": "exit_code", "equals": 0}]}. There is NO '
    '"type" field and NO "write_file" / "execute_command" node kind — '
    'to write a file use the "scaffold" claw, to run a command use the '
    '"bash" claw. "depends_on" lists node ids only (e.g. "step_1") — '
    'never an output name, a file path, or a description.'
)


# The depends_on shape, spelled out wherever a dependency error is
# reported. Retry #10: the model put output names ("endpoint_implemented",
# "tests_written") in depends_on, and the bare "depends on unknown node"
# raise never showed it the shape or the available node ids, so the
# repair budget died on the same error. Short on purpose: it rides along
# in every unknown-dependency rejection.
_DEPENDS_ON_SHAPE = (
    '"depends_on" lists NODE IDS only — the "id" of an earlier node in the '
    'same plan (e.g. "step_1"), never an output name (like '
    '"endpoint_implemented"), never a file path, never a description. '
    'A node that needs another node\'s output depends on that node\'s id. '
    'Root nodes use "depends_on": [].'
)


def _schema_error_text(label: str, err: dict[str, Any]) -> str:
    """One pydantic validation error as a plain sentence naming the field."""
    loc = err.get("loc", ())
    field = ".".join(str(part) for part in loc) if loc else ""
    err_type = err.get("type", "")
    if err_type == "missing":
        return f"Planner node {label} is missing required field '{field}'."
    if err_type == "extra_forbidden":
        return (
            f"Planner node {label} has an unknown field '{field}' — "
            f"PlanNodes have no '{field}' field."
        )
    if field:
        return (
            f"Planner node {label} has an invalid value for '{field}': "
            f"{err.get('msg', '')}."
        )
    return (
        f"Planner node {label} is not a valid node object: "
        f"{err.get('msg', '')}."
    )


class CheapRejectionError(LLMResponseError):
    """A planner rejection cheap enough to re-prompt without spending the repair budget.

    Retry #12: the first two planner attempts were an empty plan
    (``{"nodes": []}``) and an echo fake, and they spent the whole
    repair budget (max 2) before the third attempt's single-error,
    repairable Flask rejection ever arrived — so the model never got a
    repair round for it. A rejection is cheap when the output is
    degenerate (no plan at all) or its only errors are stand-in /
    code-overwrite fakes: re-prompting costs the model nothing to fix,
    so the repair loop re-prompts for free, up to a small cap, instead
    of burning a repair round. A subclass of LLMResponseError, so
    callers that catch LLMResponseError are unaffected.
    """


def _lenient_node_view(raw: Any, position: int) -> SimpleNamespace | None:
    """A duck-typed view of a raw node dict for checks that only read
    claw/inputs/criteria/id — usable before PlanNode validation passes.

    Retry #13 attempt 2: the echo fake had integer ids, so
    _validate_raw_nodes raised a schema error before the stand-in check
    in _collect_plan_errors ever ran, and the fake spent a repair round
    instead of a free re-prompt. The view exposes exactly what
    _static_standin_errors reads (assigned_claw, inputs, criteria, id)
    with defensive defaults, so the stand-in check can run on raw nodes
    even when their shape is wrong. Non-dict entries have no view.
    """
    if not isinstance(raw, dict):
        return None
    raw_id = raw.get("id")
    if isinstance(raw_id, str):
        label = raw_id
    elif raw_id is not None:
        label = str(raw_id)
    else:
        label = f"position {position}"
    inputs = raw.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    criteria = raw.get("criteria")
    if isinstance(criteria, dict):
        criteria = [criteria]
    elif not isinstance(criteria, list):
        criteria = []
    outputs = raw.get("outputs")
    if isinstance(outputs, str):
        outputs = [outputs]
    elif not isinstance(outputs, list):
        outputs = []
    return SimpleNamespace(
        id=label,
        assigned_claw=raw.get("assigned_claw") or "",
        inputs=inputs,
        criteria=criteria,
        outputs=outputs,
    )


def _validate_raw_nodes(
    raw_nodes: list[Any],
    repo_index: Any | None = None,
    goal_text: str | None = None,
) -> list[PlanNode]:
    """Validate raw node dicts, collecting per-node schema errors.

    Per-node schema errors (a missing required field, an unknown
    extra-forbidden field, a wrong field type) are collected into ONE
    numbered LLMResponseError in the same format as the collected
    validation errors in _collect_plan_errors, so the repair loop can
    fix them in one round. (Retry #7: the model invented write_file /
    execute_command node types with no objective and no assigned_claw;
    the old code raised a structural "did not match PlanNode schema"
    error that burned the whole repair budget before validation could
    name the problem.) Genuinely structural problems — bad JSON,
    duplicate ids, dependency cycles — still raise first in
    normalize_plan_nodes, as today.

    When schema errors exist, the stand-in check also runs on lenient
    views of the raw nodes (retry #13): a fake whose ids are the wrong
    type must still be named as a fake, and the rejection is cheap —
    the repair loop re-prompts for free instead of spending a repair
    round on output that was never a plan. ``repo_index``/``goal_text``
    are the repo index and goal for the grounded-move sentence; both
    optional (hand-built plans pass neither).
    """
    nodes: list[PlanNode] = []
    errors: list[str] = []
    for position, raw in enumerate(raw_nodes, start=1):
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            label = f"'{raw['id']}'"
        else:
            label = f"at position {position}"
        try:
            nodes.append(PlanNode.model_validate(raw))
        except ValidationError as exc:
            errors.extend(_schema_error_text(label, err) for err in exc.errors())
    if errors:
        standin_errors = _static_standin_errors(
            [
                view
                for view in (
                    _lenient_node_view(raw, position)
                    for position, raw in enumerate(raw_nodes, start=1)
                )
                if view is not None
            ],
            index=repo_index,
            goal_text=goal_text,
        )
        all_errors = standin_errors + errors
        noun = "error" if len(all_errors) == 1 else "errors"
        lines = "\n".join(
            f"{index}. {error}" for index, error in enumerate(all_errors, start=1)
        )
        message = (
            f"Planner output was rejected with {len(all_errors)} {noun} — "
            f"fix ALL of them:\n{lines}\n{_NODE_SHAPE_EXAMPLE}"
        )
        if standin_errors:
            raise CheapRejectionError(message)
        raise LLMResponseError(message)
    return nodes


def _coerce_nodes(
    nodes: list[PlanNode] | list[dict[str, Any]],
    repo_index: Any | None = None,
    goal_text: str | None = None,
) -> list[PlanNode]:
    if nodes and isinstance(nodes[0], PlanNode):
        return list(nodes)
    return _validate_raw_nodes(list(nodes), repo_index=repo_index, goal_text=goal_text)


def _unknown_dependency_error(
    node: PlanNode, node_map: dict[str, PlanNode], dependency: str
) -> str:
    """One unknown depends_on entry as a plain sentence naming the fix.

    The error names the bad value, the node ids that exist in this plan,
    and -- when the value is an output name of some node -- which node
    produces it and the id to depend on instead. Retry #10 attempt 3
    depended on "endpoint_implemented" (an output of step_1) and
    "tests_written" (an output of step_2); the bare raise said only
    "unknown node", so the model never learned the fix.
    """
    producers = [
        other.id
        for other in node_map.values()
        if dependency in (other.outputs or [])
    ]
    hint = ""
    if producers:
        hint = (
            f" '{dependency}' is an output of node '{producers[0]}' — "
            f"depend on '{producers[0]}' (the node id), not on its output name."
        )
    return (
        f"Planner node '{node.id}' depends on unknown node '{dependency}' — "
        f"'depends_on' lists node ids only, never output names."
        f"{hint} The nodes in this plan are: "
        f"{', '.join(sorted(node_map))}."
    )


def normalize_plan_nodes(
    nodes: list[PlanNode] | list[dict[str, Any]],
    repo_index: Any | None = None,
    goal_text: str | None = None,
) -> list[PlanNode]:
    """Validate a DAG, topologically order it, and assign deterministic node ids.

    ``repo_index``/``goal_text`` are threaded to the schema check so a
    fake with wrong-typed ids is named as a fake (retry #13); both
    optional (hand-built plans pass neither).
    """
    nodes = _coerce_nodes(nodes, repo_index=repo_index, goal_text=goal_text)
    if not nodes:
        # Cheap rejection (retry #12): an empty plan is a degenerate
        # output, not a plan that needs a repair round to fix.
        raise CheapRejectionError("Planner response must include at least one plan node.")

    node_map = {node.id: node for node in nodes}
    if len(node_map) != len(nodes):
        raise LLMResponseError("Planner response contained duplicate node ids.")

    # Unknown dependencies are a repairable rejection, not a structural
    # raise: retry #10 attempt 3 put output names ("endpoint_implemented",
    # "tests_written") in depends_on, and the bare raise never showed the
    # model the shape or the available node ids, so the repair budget died
    # on the same error. Collect every unknown dependency into ONE
    # numbered rejection in the same format as _validate_raw_nodes, so the
    # repair loop gets a round to fix them. Duplicate ids, dependency
    # cycles, and self-dependency still raise first, as before.
    unknown_dep_errors: list[str] = []
    for node in nodes:
        for dependency in node.depends_on:
            if dependency == node.id:
                raise LLMResponseError(f"Planner node '{node.id}' cannot depend on itself.")
            if dependency not in node_map:
                unknown_dep_errors.append(
                    _unknown_dependency_error(node, node_map, dependency)
                )
    if unknown_dep_errors:
        noun = "error" if len(unknown_dep_errors) == 1 else "errors"
        lines = "\n".join(
            f"{index}. {error}" for index, error in enumerate(unknown_dep_errors, start=1)
        )
        raise LLMResponseError(
            f"Planner output was rejected with {len(unknown_dep_errors)} {noun} — "
            f"fix ALL of them:\n{lines}\n{_DEPENDS_ON_SHAPE}"
        )

    original_order = {node.id: index for index, node in enumerate(nodes)}
    adjacency: dict[str, list[str]] = {node.id: [] for node in nodes}
    indegree = {node.id: 0 for node in nodes}

    for node in nodes:
        unique_dependencies: list[str] = []
        seen_dependencies: set[str] = set()
        for dependency in node.depends_on:
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

# Top-level keys a real planner response may carry. Anything else with
# no "nodes" array is not a plan shape.
_PLAN_PAYLOAD_KEYS = frozenset(
    {"nodes", "metadata", "plan_id", "intent", "status", "created_at", "updated_at"}
)


def _looks_like_prose_reserialization(payload: dict[str, Any]) -> bool:
    """True when a JSON object with no 'nodes' array reads like prompt prose turned into JSON.

    Retry #8 replan 1 re-serialized the ROUTE GOAL shape as a tokenized
    object ({"health": true, "served": false, "freetalon": "/api.py",
    "on": "port", "8765": true, "node": "1", ...}) — keys are single
    words or fragments of the prompt's sentences, values are booleans or
    sentence fragments, and there is no "nodes" array. A real plan
    payload carries recognized top-level keys (nodes, metadata, plan_id,
    intent); prose carries none of those, and its keys are mostly short
    tokens. This is general: any prompt prose, not just the ROUTE GOAL
    block, tokenizes the same way.
    """
    keys = [str(key) for key in payload]
    if len(keys) < 3:
        return False
    if _PLAN_PAYLOAD_KEYS & set(keys):
        return False
    prose_like = 0
    for key in keys:
        if re.fullmatch(r"[A-Za-z0-9]{1,16}", key):
            prose_like += 1
        elif any(ch in key for ch in " ()\"'.,;:"):
            prose_like += 1
    return prose_like * 2 >= len(keys)


def parse_planner_response(
    raw_text: str,
    *,
    repo_root: str | Path | None = None,
    goal_text: str | None = None,
    workspace: str | None = None,
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Validate raw planner output into normalized PlanNodes plus metadata.

    ``repo_root`` overrides the repo the grounding check reads; by default
    it is the checkout holding this package. ``goal_text`` is the goal the
    plan serves; when it names an existing API route, the plan must engage
    that route (anti-fake rule). ``workspace`` is the run-outputs
    directory; when known, a repo-tree path named under it is rejected
    as a wrong-location hit.
    """
    raw_text = sanitize_llm_output(raw_text)
    print(f"\n--- RAW PLANNER OUTPUT ---\n{raw_text}\n--------------------------\n")
    
    payload = parse_json_object(raw_text)
    if not isinstance(payload, dict):
        raise LLMResponseError("Planner response must be a JSON object.")

    nodes_raw = payload.get("nodes")
    if not isinstance(nodes_raw, list):
        # Brief-echo shape (retry #4 replan 1): the model re-serialized the
        # re-plan brief as {"instruction": ..., "delta_plan": {"steps": [...]}}.
        # Name the echo explicitly — the repair prompt carries the rejection
        # verbatim, so the model learns the brief is input, not output.
        brief_keys = {"instruction", "delta_plan", "steps", "failed_nodes", "remaining_budget"}
        if brief_keys & set(payload):
            raise LLMResponseError(
                "Planner output copied the re-plan brief instead of a plan DAG — "
                "it has an 'instruction'/'delta_plan'/'steps' object and no 'nodes' "
                "array. Output ONLY a JSON object with a NEW 'nodes' array of "
                "PlanNodes (id, objective, depends_on, assigned_claw, inputs, "
                "outputs, acceptance, criteria, error) — never re-serialize the "
                "brief, its instruction, or its failed steps."
            )
        if _looks_like_prose_reserialization(payload):
            raise LLMResponseError(
                "Planner output re-serialized the prompt's instructions as JSON "
                "instead of a plan DAG — it turned guidance prose (for example "
                "the ROUTE GOAL shape) into a JSON object whose keys are words "
                "from the prompt and which has no 'nodes' array. The prompt's "
                "instructions are input, not output: return ONLY a JSON object "
                "with a NEW 'nodes' array of PlanNodes (id, objective, "
                "depends_on, assigned_claw, inputs, outputs, acceptance, "
                "criteria, error) — never turn any sentence from the prompt "
                "into JSON keys or values."
            )
        raise LLMResponseError("Planner output must contain a 'nodes' array.")

    # Defensive normalization: coerce plain strings into single-element
    # lists, and a bare-dict "criteria" into a one-element list (retry #6).
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

            if isinstance(node.get("criteria"), dict):
                # Bare-dict criteria (retry #6: {"tests_pass": true}).
                # Coerce to a one-element list so the shape guess becomes
                # a validation error the repair loop can fix, instead of
                # a structural PlanNode schema error that burns the whole
                # repair budget. The coerced object still has no "type",
                # so _usable_criterion_error rejects it by name below.
                node["criteria"] = [node["criteria"]]

    # Normalize: topologically order the DAG and assign deterministic
    # node-1, node-2, ... ids, remapping depends_on. Without this the raw
    # LLM ids/order leak through and downstream code cannot rely on
    # dependency order. The repo index is built once here and shared by
    # the schema check (which needs it to name a stand-in fake on raw
    # nodes, retry #13) and the collected validation errors below, so a
    # fake with wrong-typed ids is named as a fake instead of only as a
    # schema error — and the rejection is cheap, not repair-consuming.
    index = build_repo_index(repo_root) if goal_text else None
    nodes = normalize_plan_nodes(nodes_raw, repo_index=index, goal_text=goal_text)

    # Planner hardening: an invented claw name or an unmeasurable node is a
    # plan error here, where the repair retry can fix it — not at execution
    # time. Hand-built plans bypass this function (they use
    # build_execution_plan directly), so existing callers are unaffected.
    # All per-node errors are collected and reported in ONE rejection, so a
    # plan with several independent errors can be fixed in a single repair
    # round instead of burning the repair budget one error at a time.
    errors, cheap = _collect_plan_errors(
        nodes, repo_root, goal_text, workspace, index=index
    )
    if errors:
        noun = "error" if len(errors) == 1 else "errors"
        lines = "\n".join(
            f"{index}. {error}" for index, error in enumerate(errors, start=1)
        )
        message = (
            f"Planner output was rejected with {len(errors)} {noun} — "
            f"fix ALL of them:\n{lines}"
        )
        if cheap:
            raise CheapRejectionError(message)
        raise LLMResponseError(message)

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


# The criterion shape, spelled out wherever a criteria error is reported.
# Retry #6: the model guessed criteria as a bare dict {"tests_pass": true}
# because the rejection said "every node needs at least one measurable
# criterion" but never showed the shape. Short on purpose: it rides along
# in every criteria rejection and in the planner schema rules.
_CRITERION_SHAPE_EXAMPLE = (
    'criteria MUST be a JSON array (a LIST) of criterion objects, e.g. '
    '[{"type": "exit_code", "equals": 0}]. A node that writes a file uses '
    '[{"type": "file_exists", "path": "<the file the node writes>"}]. '
    'Never a single object like {"tests_pass": true}'
)


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
        if ctype is None:
            # A criterion object with no "type" at all — the usual shape
            # of a coerced bare-dict guess (retry #6). Name the shape
            # instead of just the missing key, so the repair round can
            # fix it in one go.
            return f"criterion has no 'type': {_CRITERION_SHAPE_EXAMPLE}"
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


_GLUED_WORD_GUIDANCE = (
    " To write a multi-line file, use a heredoc "
    "(cat > <path> <<'EOF' ... EOF) or the scaffold claw \u2014 do not use "
    "single-line echo for multi-line content"
)


def _validate_bash_command(node: PlanNode) -> list[str]:
    """Return every error in a bash/shell_exec node's command (fix 2).

    Checks: the command is a non-empty string; it tokenizes
    (shlex.split — unbalanced quotes fail); it is not words glued together
    (the failed /health run produced ``python3-munittestdiscover-s/...``
    and ``curl-shttp://localhost:8000/health``); any localhost port it
    addresses is a known service (FreeTalon API, Ollama). All problems are
    collected, not just the first, so one repair round can fix them all.
    The glued-word checks are heuristics for the observed failure shapes,
    not a shell parser: a single token jamming a program, flags, and a
    path/URL together cannot be a valid invocation.
    """
    errors: list[str] = []
    cmd = node.inputs.get("command") or node.inputs.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return ["bash node needs a non-empty 'command' string"]
    # A heredoc body is file content, not shell: strip it before the
    # shlex / port checks, or a URL inside the file is flagged as glued
    # words (retry #3). The shell around the heredoc is still validated.
    shell = _strip_heredoc_bodies(cmd)
    tokens: list[str] = []
    try:
        tokens = shlex.split(shell)
    except ValueError as exc:
        errors.append(f"bash command does not parse as a shell command: {exc}")
    if tokens:
        first = tokens[0]
        if not first.startswith(("/", "./", "../")):
            if "://" in first and not first.startswith(("http://", "https://")):
                errors.append(
                    f"bash command looks like words glued together (missing spaces): {shell!r}.{_GLUED_WORD_GUIDANCE}"
                )
            elif "/" in first and first.split("/", 1)[0].count("-") >= 2:
                errors.append(
                    f"bash command looks like words glued together (missing spaces): {shell!r}.{_GLUED_WORD_GUIDANCE}"
                )
        for token in tokens[1:]:
            if _FLAG_PATH_RE.match(token):
                errors.append(
                    f"bash command looks like a flag and a path glued together: "
                    f"{token!r} in {shell!r}"
                )
            if "://" in token and not token.startswith(("http://", "https://")):
                errors.append(
                    f"bash command looks like words glued together (missing spaces): {shell!r}.{_GLUED_WORD_GUIDANCE}"
                )
    elif not errors:
        errors.append("bash node needs a non-empty 'command' string")
    for match in _PORT_RE.finditer(shell):
        port = int(match.group(1))
        if port in _KNOWN_SERVICE_PORTS:
            continue
        path = match.group(2) or ""
        segment = path.strip("/").split("/", 1)[0] if path else ""
        if segment in _API_ROUTE_NAMES:
            errors.append(
                f"bash command addresses localhost:{port}, but the FreeTalon API "
                f"runs on port 8765, not {port} — use http://localhost:8765{path}"
            )
        else:
            errors.append(
                f"bash command addresses unknown service port localhost:{port} — "
                "known services are the FreeTalon API on 8765 and Ollama on 11434"
            )
    return errors


def _claw_error(node: PlanNode) -> str | None:
    """Return an error if the node's assigned_claw is not a registered claw."""
    claw = node.assigned_claw or ""
    if claw not in KNOWN_CLAW_NAMES:
        allowed = ", ".join(sorted(KNOWN_CLAW_NAMES))
        return (
            f"Planner node '{node.id}' uses unknown claw {claw!r}. "
            f"assigned_claw must be one of: {allowed}."
        )
    return None


def _criteria_errors(node: PlanNode) -> list[str]:
    """Return every unusable-criterion error for one node."""
    errors: list[str] = []
    criteria = list(node.criteria or [])
    if not criteria:
        return [
            f"Planner node '{node.id}' has no 'criteria': every node needs "
            "at least one measurable criterion (exit_code, file_exists, "
            f"file_matches, metric, or tests_pass). {_CRITERION_SHAPE_EXAMPLE}."
        ]
    for criterion in criteria:
        error = _usable_criterion_error(criterion, node)
        if error is not None:
            errors.append(
                f"Planner node '{node.id}' has an unusable criterion: {error}."
            )
    return errors


def _command_errors(node: PlanNode) -> list[str]:
    """Return every malformed-command error for a bash/shell_exec node."""
    if node.assigned_claw not in ("bash", "shell_exec"):
        return []
    return [
        f"Planner node '{node.id}' has an invalid bash command: {error}."
        for error in _validate_bash_command(node)
    ]


def _grounding_error(node: PlanNode, index: Any) -> str | None:
    """Return an error if a scaffold node duplicates existing code (fix 1)."""
    if node.assigned_claw not in ("scaffold", "file_writer"):
        return None
    target = (
        node.inputs.get("file_path")
        or node.inputs.get("path")
        or (node.outputs[0] if node.outputs else "")
    )
    if not isinstance(target, str) or not target:
        return None
    error = duplicate_scaffold_error(target, index)
    if error is not None:
        return f"Planner node '{node.id}' duplicates existing code: {error}."
    return None


def _tmp_write_errors(
    nodes: list[PlanNode], index: Any, goal_text: str | None
) -> list[str]:
    """Reject route-goal plans that write their deliverable to /tmp.

    When the goal names an existing API route, the work belongs in the
    serving module or the workspace — a /tmp scratch file is a stand-in,
    not the deliverable. (Retry #6 attempt 2 wrote a Flask app to
    /tmp/freetalon/api.py instead of extending freetalon/api.py, and it
    passed route engagement because the /tmp path contains the module
    name as a substring.) General: any write node targeting /tmp on a
    route goal is flagged, naming the serving module(s).
    """
    if not goal_text:
        return []
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return []
    modules = ", ".join(sorted({module for _, module in named}))
    errors: list[str] = []
    for node in nodes:
        targets: list[str] = []
        if node.assigned_claw in ("bash", "shell_exec"):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str):
                written = _literal_write(command)
                if written is not None:
                    targets.append(written[0])
        elif node.assigned_claw in ("scaffold", "file_writer"):
            target = (
                node.inputs.get("file_path")
                or node.inputs.get("path")
                or (node.outputs[0] if node.outputs else "")
            )
            if isinstance(target, str) and target:
                targets.append(target)
        for target in targets:
            if target == "/tmp" or target.startswith("/tmp/"):
                errors.append(
                    f"Planner node '{node.id}' writes to {target!r} — for a "
                    f"route goal the work belongs in the serving module "
                    f"({modules}) or the workspace, not a /tmp scratch file. "
                    f"Extend {modules} instead."
                )
                break
    return errors


def _heredoc_bodies(command: str) -> list[str]:
    """The bodies of every heredoc in a bash command (file content, not shell)."""
    bodies: list[str] = []
    rest = command
    while True:
        match = _STRIP_HEREDOC_RE.search(rest)
        if not match:
            return bodies
        tail = rest[match.end() :]
        lines = tail.splitlines(keepends=True)
        marker = match.group("marker")
        marker_idx = next(
            (i for i, line in enumerate(lines) if line.strip() == marker), None
        )
        if marker_idx is None:
            return bodies
        bodies.append("".join(lines[1:marker_idx]))
        rest = "".join(lines[marker_idx + 1 :])


def _framework_errors(
    nodes: list[PlanNode], index: Any, goal_text: str | None
) -> list[str]:
    """Reject route-goal plans that write a different web framework.

    When the goal names an existing API route, the serving module's
    framework is the stack: a node that writes a Flask app for a route
    served by an http.server module is inventing a stack the repo does
    not use (retry #7 attempts 2-3 wrote a Flask app for /health,
    which freetalon/api.py serves with stdlib http.server). General:
    any written content (scaffold content, or a heredoc body) that
    imports a web framework different from the serving module's.
    """
    if not goal_text:
        return []
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return []
    serving: dict[str, str] = {}
    for route, module in named:
        serving.setdefault(module, route)
    errors: list[str] = []
    for module, route in sorted(serving.items()):
        serving_framework = index.frameworks.get(module)
        if not serving_framework:
            continue
        # Retry #13: the rejection named http.server but the model
        # treated "write a Flask app" as the grounded move and elaborated
        # it across repair rounds. Name the grounded move concretely --
        # extend the serving module in place, adding a branch to the
        # handler that already serves the route -- the way the anti-fake
        # rejection names the grounded move (retry #5).
        grounded = _grounded_move_sentence(
            route,
            str(index.root / module),
            index.api_port,
            index.route_handlers.get(route),
        )
        for node in nodes:
            written: list[str] = []
            if node.assigned_claw in ("scaffold", "file_writer"):
                content = node.inputs.get("content")
                if isinstance(content, str):
                    written.append(content)
            elif node.assigned_claw in ("bash", "shell_exec"):
                command = node.inputs.get("command") or node.inputs.get("cmd")
                if isinstance(command, str):
                    written.extend(_heredoc_bodies(command))
            for content in written:
                found = framework_in_text(content)
                if found is not None and found != serving_framework:
                    errors.append(
                        f"Planner node '{node.id}' writes a {found} app — "
                        f"{route} is served by {module}, which is built on "
                        f"{serving_framework}. {grounded} Do not rewrite it "
                        f"in a different web framework."
                    )
                    break
    return errors


# An absolute path inside a node string: used to spot workspace-prefixed
# module paths. Stops at whitespace and quote/semicolon/pipe characters,
# so a path followed by `|`, `&&`, or a quote ends cleanly.
_ABS_PATH_RE = re.compile(r"/[^\s\"';&|]+")


def _wrong_location_errors(
    nodes: list[PlanNode], index: Any, workspace: str | None
) -> list[str]:
    """Reject repo module/test paths named under the workspace root.

    The planner prompt names the workspace (from the TaskIntent
    constraints), and the model sometimes uses it as the repo root:
    retry #9 node-1 ran head/grep against
    /home/seeker/freetalon-workspace/freetalon/api.py — a path that does
    not exist — because the workspace was the only directory it had been
    told. The workspace holds run outputs and generated files, not code:
    a path under the workspace that runs inside a repo tree
    (``freetalon/...`` or ``tests/...``) is a wrong-location hit,
    rejected by name with the repo root as the correct location.
    Genuine generated files (test_health.py, health.json, squares.py)
    are not flagged — they sit at the workspace root, not inside a repo
    tree — and a workspace path that actually exists on disk is not
    flagged either: only a nonexistent workspace copy of a repo-tree
    path counts. Heredoc bodies are stripped before scanning a bash
    command: a path inside file content the node is writing is data,
    not a file the node reads. (Retry #9 node-2 named
    freetalon/tests/test_api.py under the workspace; the repo has no
    freetalon/tests/ — its tests live at repo-root tests/ — but the
    path is still a repo-tree path under the workspace, so it is
    flagged the same way.)
    """
    if not workspace:
        return []
    ws = str(workspace).rstrip("/")
    errors: list[str] = []
    for node in nodes:
        candidates: list[str] = []
        claw = node.assigned_claw or ""
        if claw in ("scaffold", "file_writer"):
            target = (
                node.inputs.get("file_path")
                or node.inputs.get("path")
                or (node.outputs[0] if node.outputs else "")
            )
            if isinstance(target, str) and target:
                candidates.append(target)
        elif claw in ("bash", "shell_exec"):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str) and command:
                candidates.append(_strip_heredoc_bodies(command))
        candidates.extend(str(item) for item in node.outputs)
        for criterion in node.criteria or []:
            if isinstance(criterion, dict) and isinstance(
                criterion.get("path"), str
            ):
                candidates.append(criterion["path"])
        seen: set[str] = set()
        for text in candidates:
            for match in _ABS_PATH_RE.finditer(text):
                path = match.group(0).rstrip("),.")
                if path in seen:
                    continue
                seen.add(path)
                if not (path == ws or path.startswith(ws + "/")):
                    continue
                rel = path[len(ws) + 1 :] if path != ws else ""
                first = rel.split("/", 1)[0] if rel else ""
                if first not in ("freetalon", "tests"):
                    continue
                if Path(path).exists():
                    continue
                counterpart = index.root / rel
                errors.append(
                    f"Planner node '{node.id}' names {path!r} — a repo "
                    f"module/test path under the workspace. The workspace "
                    f"({ws}) holds run outputs and generated files, not "
                    f"code: the repo root is {index.root}, so the code path "
                    f"is {counterpart}. Use the repo path, not the "
                    f"workspace path."
                )
    return errors


# A shell redirect that writes a file: `> path`, `>> path`, `2> path`.
# Heredoc targets are caught by _STRIP_HEREDOC_RE separately; a `>` inside
# a quoted string is not a redirect, so callers strip quoted segments first.
_REDIRECT_RE = re.compile(r"(?:^|[\s;&|])(?:\d+)?>>?\s*(?P<target>[^\s;&|]+)")


def _node_written_paths(node: PlanNode) -> set[str]:
    """Paths a node writes: scaffold target, bash redirect/heredoc targets.

    A node's "outputs" list is NOT evidence — it is a claim about what
    the node produces, and a fake plan lists a file there without ever
    creating it (retry #8: node-1 listed health.json in outputs while
    echoing its answer to stdout). Only a real write counts: the scaffold
    claw's file_path, a redirect, or a heredoc target.
    """
    written: set[str] = set()
    claw = node.assigned_claw or ""
    if claw in ("scaffold", "file_writer"):
        target = (
            node.inputs.get("file_path")
            or node.inputs.get("path")
            or (node.outputs[0] if node.outputs else "")
        )
        if isinstance(target, str) and target:
            written.add(target)
    elif claw in ("bash", "shell_exec"):
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if isinstance(command, str) and command:
            for match in _STRIP_HEREDOC_RE.finditer(command):
                written.add(match.group("target"))
            stripped = _strip_heredoc_bodies(command)
            unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", stripped)
            for match in _REDIRECT_RE.finditer(unquoted):
                target = match.group("target")
                if target and not target.startswith("/dev/"):
                    written.add(target)
    return written


def _node_command_mentions_path(node: PlanNode, path: str) -> bool:
    """True if a bash node's command names the path — it runs or reads the file.

    Heredoc bodies are stripped first: a path inside file content the node
    is writing is data, not a file the node runs.
    """
    if node.assigned_claw not in ("bash", "shell_exec"):
        return False
    command = node.inputs.get("command") or node.inputs.get("cmd")
    if not isinstance(command, str):
        return False
    shell = _strip_heredoc_bodies(command)
    return path in shell or shell.endswith("/" + path)


def _path_exists_on_disk(path: str, repo_root: str | Path | None) -> bool:
    """True if the path already exists on disk at plan time.

    Absolute paths are checked as-is; relative paths resolve against the
    repo root (the checkout holding this package when repo_root is None).
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.exists()
    return (resolve_repo_root(repo_root) / candidate).exists() or candidate.exists()


def _unwritten_file_errors(
    nodes: list[PlanNode], repo_root: str | Path | None
) -> list[str]:
    """Reject file_exists criteria on paths the plan never produces.

    A file_exists criterion on a path no node writes is unmeasurable by
    construction — the measurement can only ever report "does not exist".
    (Retry #8: node-1 echoed a JSON answer to STDOUT and claimed
    file_exists on health.json, a file the command never writes; the
    static stand-in rule did not fire because there was no redirect, and
    only execution caught it as UNMEASURABLE.) The criterion path must be
    written by some node in the plan (scaffold file_path, bash redirect,
    or heredoc target — the check looks at the whole plan's writes, so a
    later node may check a file an earlier node wrote), named in a node's
    bash command (the node runs or reads the file), or already exist on
    disk at plan time. Listing the path in a node's "outputs" alone does
    not count — outputs is a claim, not a write. Nodes with an unknown
    claw are skipped: the claw error already rejects them, and they
    cannot write anything.
    """
    written: set[str] = set()
    for node in nodes:
        written.update(_node_written_paths(node))
    errors: list[str] = []
    for node in nodes:
        if (node.assigned_claw or "") not in KNOWN_CLAW_NAMES:
            continue
        for criterion in node.criteria or []:
            if not isinstance(criterion, dict):
                continue
            if criterion.get("type") != CRITERION_FILE_EXISTS:
                continue
            path = criterion.get("path")
            if not isinstance(path, str) or not path:
                continue  # _usable_criterion_error already flags this
            if any(_same_path(path, target) for target in written):
                continue
            if any(_node_command_mentions_path(other, path) for other in nodes):
                continue
            if _path_exists_on_disk(path, repo_root):
                continue
            errors.append(
                f"Planner node '{node.id}' has a file_exists criterion on "
                f"{path!r}, but no node in the plan writes that file and it "
                "does not exist on disk — the file would be unmeasurable. A "
                "file_exists criterion must point at a file the plan writes "
                "(scaffold file_path, a bash redirect, or a heredoc target), "
                "a file a node runs or reads in its command, or a file that "
                "already exists. Listing the path in 'outputs' does not "
                "count: echoing an answer to stdout does not create the file."
            )
    return errors


def _collect_plan_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None = None,
    goal_text: str | None = None,
    workspace: str | None = None,
    index: Any | None = None,
) -> tuple[list[str], bool]:
    """Collect EVERY plan validation error, grouped by node, plus plan-level rules.

    Each node is checked against every rule (claw allowlist, structured
    criteria, command/port validation, repo grounding) and every failure is
    reported, so a single rejection can drive one repair round that fixes
    them all. Plan-level rules run after the per-node checks: static
    stand-in files (a node that echo-writes a hardcoded answer and then
    claims it exists), unwritten files (a file_exists criterion on a
    path no node writes and that does not exist on disk), wrong location
    (a repo-tree path named under the workspace root), and route
    engagement (a goal naming an existing API route must have a node
    that reads, modifies, or calls it). Per-node
    schema errors (missing fields, invented fields) are collected the
    same way in _validate_raw_nodes, before this runs. DAG-level
    structural errors (bad JSON, duplicate ids, dependency cycles,
    self-dependency) are not collected — they are raised first by
    normalize_plan_nodes, since the node list cannot be interpreted at
    all until the DAG is structurally valid. Unknown dependencies are
    the exception: normalize_plan_nodes collects them as a numbered
    rejection (retry #10), so the repair loop can fix them instead of
    the driver dying on a bare raise.

    Returns (errors, cheap): ``cheap`` is True when every error is a
    stand-in / code-overwrite fake (retry #12) — a rejection the repair
    loop may re-prompt for free, without spending the repair budget.
    """
    errors: list[str] = []
    needs_index = any(
        node.assigned_claw in ("scaffold", "file_writer") for node in nodes
    ) or goal_text is not None
    if index is None and needs_index:
        index = build_repo_index(repo_root)
    for node in nodes:
        error = _claw_error(node)
        if error is not None:
            errors.append(error)
        errors.extend(_criteria_errors(node))
        errors.extend(_command_errors(node))
        if index is not None:
            error = _grounding_error(node, index)
            if error is not None:
                errors.append(error)
    errors.extend(_unwritten_file_errors(nodes, repo_root))
    standin_errors = _static_standin_errors(nodes, index=index, goal_text=goal_text)
    errors.extend(standin_errors)
    if index is not None:
        errors.extend(_route_engagement_errors(nodes, index, goal_text))
        errors.extend(_tmp_write_errors(nodes, index, goal_text))
        errors.extend(_framework_errors(nodes, index, goal_text))
        errors.extend(_wrong_location_errors(nodes, index, workspace))
    # Cheap rejection (retry #12): the only errors are stand-in /
    # code-overwrite fakes, so re-prompting costs the model nothing to
    # fix and must not spend the repair budget. Any other error mixed
    # in makes it a real rejection.
    cheap = bool(errors) and len(standin_errors) == len(errors)
    return errors, cheap


# --- Anti-fake validation (retry #2, run-20261117T054715Z-698173) ---

_LITERAL_WRITE_RE = re.compile(
    r"^\s*(?:echo|printf)\s+(?P<body>.*?)\s*(?:>>|>)\s*(?P<target>\S+)",
    re.DOTALL,
)
_HEREDOC_RE = re.compile(
    r"^\s*cat\s*>>?\s*(?P<target>\S+)\s*<<-?\s*['\"]?(?P<marker>[A-Za-z_][A-Za-z0-9_]*)['\"]?",
    re.MULTILINE,
)


# Same heredoc shape without the line-start anchor, for stripping
# bodies from commands where the heredoc follows `&&` or `;`.
_STRIP_HEREDOC_RE = re.compile(
    r"cat\s*>>?\s*(?P<target>\S+)\s*<<-?\s*['\"]?(?P<marker>[A-Za-z_][A-Za-z0-9_]*)['\"]?"
)


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies from a bash command, leaving the shell around them.

    A heredoc body is file content, not shell: its lines must not be
    tokenized by the glued-word checks or scanned for localhost ports.
    (Retry #3: a heredoc test file containing
    ``requests.get("http://localhost:8765/health")`` was rejected as
    glued words, because shlex turns the body line into a token
    containing "://" that does not start with "http://".) The head line
    (``cat > path <<'MARKER'``), the closing marker line, and any
    commands after the marker are kept and still validated.
    """
    out: list[str] = []
    rest = command
    while True:
        match = _STRIP_HEREDOC_RE.search(rest)
        if not match:
            out.append(rest)
            break
        out.append(rest[: match.end()])
        tail = rest[match.end() :]
        lines = tail.splitlines(keepends=True)
        marker = match.group("marker")
        marker_idx = next(
            (i for i, line in enumerate(lines) if line.strip() == marker), None
        )
        if marker_idx is None:
            # Unterminated heredoc: leave the rest as-is for validation.
            out.append(tail)
            break
        out.append(lines[0] if lines else "")
        out.append(lines[marker_idx])
        rest = "".join(lines[marker_idx + 1 :])
    return "".join(out)


def _quoted_segments(body: str) -> list[str]:
    """Quoted segments of a shell fragment, in order.

    Only quoted text can be a hardcoded answer: an unquoted word is a
    variable, a substitution, or a plain word, not a literal the model
    wrote. Single quotes nest double quotes and vice versa, so the scan
    pairs each opening quote with the next matching one.
    """
    segments: list[str] = []
    index = 0
    while index < len(body):
        quote = body[index]
        if quote in ("'", '"'):
            close = body.find(quote, index + 1)
            if close == -1:
                break
            segments.append(body[index + 1 : close])
            index = close + 1
        else:
            index += 1
    return segments


def _literal_write(command: str) -> tuple[str, str] | None:
    """If a bash command only writes a quoted literal to a file, return (path, literal).

    Matches ``echo '...' > path``, ``printf '...' > path`` (and ``>>``),
    and ``cat > path <<'MARKER'`` heredocs. Returns None when the command
    does anything else. This is a heuristic for the observed fake shapes,
    not a shell parser.
    """
    heredoc = _HEREDOC_RE.search(command)
    if heredoc:
        marker = heredoc.group("marker")
        lines = command[heredoc.end() :].splitlines()
        body: list[str] = []
        for line in lines:
            if line.strip() == marker:
                break
            body.append(line)
        else:
            return None
        return heredoc.group("target"), "\n".join(body)
    match = _LITERAL_WRITE_RE.match(command)
    if not match:
        return None
    segments = _quoted_segments(match.group("body"))
    if not segments:
        return None
    return match.group("target"), " ".join(segments)


def _literal_is_answer(literal: str) -> bool:
    """True when a written literal is a hardcoded answer, not data.

    A JSON object/array literal (``{"health": "OK", ...}``) or a
    single-line string containing ``assert`` is the expected answer
    written out by hand.
    Plain text (``echo hello > out.txt``) is ordinary scratch output and
    is not flagged.
    """
    text = literal.strip()
    # A single-line assert sentence is the expected answer written out
    # by hand (retry #2: an assert sentence echoed into test.sh). A
    # multi-line literal containing "assert" is real code (a test file),
    # not an answer sentence - the JSON check below still catches a
    # multi-line fake JSON answer.
    if "assert" in text.lower() and "\n" not in text:
        return True
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, (dict, list))


def _same_path(left: str, right: str) -> bool:
    """True when two path spellings name the same file."""
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def _grounded_move_sentence(
    route: str, module: str, api_port: int, handler: str | None = None
) -> str:
    """One sentence naming the valid move for a route goal.

    The sentence is the concrete form of the ROUTE GOAL shape: extend
    the serving module, write real tests, verify against the live API
    port. It is appended to the anti-fake rejection so the repair round
    is pointed TOWARD the real work, not just away from the fake.
    ``module`` is the absolute repo path of the serving module, so the
    model is never left guessing which directory the code lives in.
    ``handler`` is the repo index's description of the request handler
    that already serves the route ("the do_GET method of the Handler
    class (defined inside HiveAPIServer.start)"), when known: the move
    is then named as a branch on that handler, not just "extend the
    module" — the model otherwise reads "implement the endpoint" as
    "write a new app" and elaborates the wrong framework instead
    (retry #13: two Flask scaffolds, the second more elaborate than
    the first, both rejected by the framework rule).
    """
    if handler is not None:
        return (
            f"The grounded move is: extend {module} in place — add a "
            f"{route} branch to {handler}; write the tests (scaffold or "
            f"heredoc) and run them; verify live with "
            f"curl http://localhost:{api_port}{route}."
        )
    return (
        f"The grounded move is: extend {module} (scaffold claw, or a bash "
        f"heredoc) to implement {route}; write the tests (scaffold or "
        f"heredoc) and run them; verify live with "
        f"curl http://localhost:{api_port}{route}."
    )


# File suffixes that mark a path as a code file. A JSON literal is never
# valid code in any of these languages, so writing one over such a path
# destroys an implementation instead of producing it (retry #12: the
# model echoed a JSON answer over freetalon/api.py). Data files are
# exempt on purpose: writing a JSON config to a new file is legitimate
# file production, so only code paths are guarded.
_CODE_FILE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c",
    ".cc", ".cpp", ".h", ".hpp", ".rb", ".sh", ".pl", ".php", ".swift",
    ".kt", ".scala", ".cs",
})


def _is_code_path(path: str) -> bool:
    """True when a path names a code file — by suffix."""
    return Path(path).suffix.lower() in _CODE_FILE_SUFFIXES


def _static_standin_errors(
    nodes: list[PlanNode],
    index: Any | None = None,
    goal_text: str | None = None,
) -> list[str]:
    """Flag nodes that manufacture their own evidence.

    Two shapes, both general:
    1. A bash node that echo-writes a hardcoded answer (a JSON literal
       or an assert string) into a file and then claims file_exists on
       that same file never ran any code or service: the file's contents
       were asserted, not produced. (Retry #2: node-1 echoed a hardcoded
       api.json; node-2 echoed an assert sentence into test.sh.)
    2. A node that writes a hardcoded answer into a CODE file — a bash
       redirect/heredoc, or a scaffold write whose content is a JSON
       literal. A JSON literal is not code, so this overwrites the
       implementation instead of producing it. (Retry #12 attempt 2
       echoed a JSON answer over freetalon/api.py with an exit_code
       criterion, so shape 1 never fired — only the int-id type error
       caught it, and string ids would have let the plan through and
       destroyed api.py.) Shape 2 fires regardless of the node's
       criteria: the criterion type must not be the escape hatch.
       Data/generated files are exempt: writing a JSON config to a new
       file is legitimate file production, so only code paths (.py and
       friends) are guarded. The scaffold claw's normal pattern —
       writing real code and claiming file_exists on it — is untouched:
       real code is not a JSON literal.
    The escape hatch is the scaffold claw for real files, or a node that
    runs the real code and checks ITS output. When the goal names an
    existing route, the rejection also names the grounded move (extend
    the serving module, write real tests, curl the live API port): the
    rule used to steer AWAY from the fake without pointing TOWARD the
    real work, so the model burned repair rounds re-discovering it
    (retry #5: two fakes, then a wrong-service probe, budget spent
    before the engagement error fired).
    """
    errors: list[str] = []
    grounded = ""
    if index is not None and goal_text:
        named = _routes_named_in_goal(goal_text, index)
        if named:
            grounded = " " + " ".join(
                _grounded_move_sentence(
                    route,
                    str(index.root / module),
                    index.api_port,
                    index.route_handlers.get(route),
                )
                for route, module in named
            )
    for node in nodes:
        claw = node.assigned_claw or ""
        writes: list[tuple[str, str]] = []
        if claw in ("bash", "shell_exec"):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str):
                written = _literal_write(command)
                if written is not None:
                    writes.append(written)
        elif claw in ("scaffold", "file_writer"):
            target = node.inputs.get("file_path") or node.inputs.get("path")
            content = node.inputs.get("content")
            if isinstance(target, str) and target and isinstance(content, str):
                writes.append((target, content))
        for target, literal in writes:
            if not _literal_is_answer(literal):
                continue
            if _is_code_path(target):
                errors.append(
                    f"Planner node '{node.id}' writes a hardcoded answer into "
                    f"{target!r} — a JSON literal is not code, and {target!r} "
                    f"is a code file. That overwrites code with a hardcoded "
                    f"answer instead of implementing it. Write real code in "
                    f"the module (scaffold claw, or a bash heredoc) and verify "
                    f"it by running the tests and curling the live service.{grounded}"
                )
                continue
            if claw not in ("bash", "shell_exec"):
                continue
            for criterion in node.criteria or []:
                if (
                    isinstance(criterion, dict)
                    and criterion.get("type") == CRITERION_FILE_EXISTS
                    and isinstance(criterion.get("path"), str)
                    and _same_path(criterion["path"], target)
                ):
                    errors.append(
                        f"Planner node '{node.id}' writes a static file {target!r} "
                        "containing a hardcoded answer and then claims it exists — "
                        "that manufactures the deliverable instead of producing it. "
                        "Run the real code or service and check ITS output instead "
                        "(implement the endpoint in its module, run the tests, "
                        f"curl the live service).{grounded}"
                    )
                    break
    return errors


def _routes_named_in_goal(goal_text: str, index: Any) -> list[tuple[str, str]]:
    """Existing routes the goal names, as (route, serving module) pairs.

    A route is named when the goal contains its path literal (``/health``)
    or the route name with ``endpoint``/``route`` (``health endpoint``).
    Bare words like ``status`` or ``metrics`` do not count on their own —
    they are too common to mean the API route.
    """
    text = (goal_text or "").lower()
    named: list[tuple[str, str]] = []
    for route in sorted(index.routes):
        name = route.strip("/").split("/", 1)[0]
        if not name:
            continue
        path_hit = re.search(rf"{re.escape('/' + name)}(?![A-Za-z0-9_.\-])", text)
        word_hit = re.search(rf"\b{re.escape(name)}\s+(endpoint|route)\b", text)
        if path_hit or word_hit:
            named.append((route, index.routes[route]))
    return named


def _route_engagement_errors(
    nodes: list[PlanNode], index: Any, goal_text: str | None
) -> list[str]:
    """Reject a plan that claims route work without touching the route.

    When the goal names an existing API route, at least one node must
    engage it: mention the module that serves it, or call the live route
    on the API's own port. A route literal on any other host/port does
    NOT count — it is a different service. (Retry #2: the goal asked for
    GET /health in freetalon/api.py, but no node read, modified, or called
    anything in freetalon/api.py — the plan wrote a static api.json
    instead. Retry #4: node-1 curled http://localhost:11434/health — the
    route literal on Ollama's port — and the plan passed while never
    touching freetalon/api.py and curling a service with no /health
    endpoint.)
    """
    if not goal_text:
        return []
    errors: list[str] = []
    api_port = index.api_port
    for route, module in _routes_named_in_goal(goal_text, index):
        engaged = False
        wrong_service: list[str] = []
        # A call to the route on the API's own port, e.g.
        # curl http://localhost:8765/health or
        # requests.get("http://localhost:8765/health").
        live_re = re.compile(
            rf":{api_port}[^\s\"']*{re.escape(route)}(?![A-Za-z0-9_.\-])"
        )
        # The same route literal after any other port is a different
        # service (Ollama on 11434 has no /health endpoint).
        other_port_re = re.compile(
            rf":(\d+)[^\s\"']*{re.escape(route)}(?![A-Za-z0-9_.\-])"
        )
        for node in nodes:
            for text in _node_path_strings(node):
                if module in text or live_re.search(text):
                    engaged = True
                    break
                for match in other_port_re.finditer(text):
                    if int(match.group(1)) != api_port:
                        wrong_service.append(f"localhost:{match.group(1)}")
            if engaged:
                break
        if not engaged:
            if wrong_service:
                ports = ", ".join(sorted(set(wrong_service)))
                errors.append(
                    f"Planner goal asks for the {route} endpoint, served by "
                    f"{module} on port {api_port}, but no node reads, modifies, "
                    f"or calls it — a node calls {route} on {ports} instead, "
                    f"which is a different service, not the FreeTalon API. "
                    f"Call {route} on port {api_port} or extend {module}."
                )
            else:
                errors.append(
                    f"Planner goal asks for the {route} endpoint, served by "
                    f"{module}, but no node reads, modifies, or calls it — "
                    f"extend {module} instead of writing a stand-in file."
                )
    return errors


def _describe_param_value(key: str, value: Any) -> str:
    """One prose phrase for a past parameter — its kind, never its text.

    A failed command or file path handed to the model verbatim is a node
    waiting to be copied: at temperature 0 the model pastes what it last
    saw. Retry #4's replan 1 copied the failed curl command out of the
    memory prose, and the retry-#2 test.sh / glued-unittest commands the
    same way (all under 80 chars, so truncation did not save them).
    Short plain tokens — a model name, a flag, a number — carry no command
    and are safe to name; anything shaped like a command, a path, or code
    is described by kind only.
    """
    if isinstance(value, bool):
        return f"{key}={value}"
    if isinstance(value, (int, float)):
        return f"{key}={value}"
    text = str(value)
    if len(text) <= 40 and not any(ch in text for ch in " /|<>$\"'{}()[]"):
        return f"{key}={text}"
    if key == "command":
        return f"{key}=a shell command"
    if key in ("file_path", "path"):
        return f"{key}=a file path"
    return f"{key}=a longer value"


def _describe_measurement(measurement: Any) -> str:
    """One prose line for a measurement dict; numbers stay exact."""
    if not isinstance(measurement, dict):
        return str(measurement)
    bits: list[str] = []
    if measurement.get("node_id"):
        bits.append(f"node {measurement['node_id']}")
    if measurement.get("verdict"):
        bits.append(f"verdict {measurement['verdict']}")
    if measurement.get("exit_code") is not None:
        bits.append(f"exit {measurement['exit_code']}")
    if measurement.get("duration_s") is not None:
        bits.append(f"{measurement['duration_s']}s")
    if measurement.get("verdict") in ("FAIL", "UNMEASURABLE"):
        bits.append(measurement_failed_detail(measurement))
    return "; ".join(bits) if bits else "no measurement recorded"


def _describe_memory_record(record: Any) -> str:
    """One prose line for an experiment-memory record — never its JSON.

    The record's keys (run_id, goal_key, node_id, approach, ...) are a
    copyable JSON object at temperature 0: in the /health retry #2 the
    replan returned one verbatim as the nodes array. Prose carries the
    same facts (what was tried, what happened, the lesson) with nothing
    to copy. Parameter values are described by kind, never quoted: a
    failed command handed over verbatim is copied back as a node (retry #4).
    """
    if not isinstance(record, dict):
        return str(record)
    approach = record.get("approach") or {}
    if not isinstance(approach, dict):
        approach = {}
    capability = approach.get("capability", "")
    params = approach.get("key_params") or {}
    param_text = ""
    if isinstance(params, dict) and params:
        param_text = "; ".join(
            _describe_param_value(key, value) for key, value in params.items()
        )
    data_window = approach.get("data_window", "")
    bits = [
        f"Past experiment in run {record.get('run_id', '')} "
        f"({record.get('ts', '')}): node {record.get('node_id', '')} "
        f"tried {capability}"
    ]
    if param_text:
        bits.append(f"with {param_text}")
    if data_window:
        bits.append(f"data window {data_window}")
    bits.append(f"— outcome: {_describe_measurement(record.get('measurement') or {})}")
    if record.get("decision"):
        bits.append(f"Decision: {record['decision']}.")
    if record.get("hypothesis"):
        bits.append(f"Hypothesis: {record['hypothesis']}.")
    if record.get("lesson"):
        bits.append(f"Lesson: {record['lesson']}")
    return " ".join(bits)


def _format_brief_for_prompt(brief: dict[str, Any]) -> str:
    """Render a re-plan brief as prose lines, never as JSON.

    The brief used to be pasted verbatim (``Re-plan brief JSON: {...}``),
    including the memory records as JSON objects. At temperature 0 the
    model copies the JSON it last saw: in the /health retry #2 the replan
    returned an experiment-memory record verbatim as the nodes array.
    Prose carries the same facts — the unchanged goal and its exact
    thresholds, the failed nodes with their exact measurements, the
    relevant memory as lessons, the remaining budget — with nothing
    copyable. (Same fix family as the intent prose rendering in 04b76e1.)
    """
    lines = ["Re-plan brief:"]
    goal = brief.get("goal", {})
    if isinstance(goal, dict):
        if goal.get("objective"):
            lines.append(f"Goal: {goal['objective']}")
        for measure in goal.get("success_measures") or []:
            if isinstance(measure, dict):
                lines.append(
                    f"Success measure: {measure.get('name', '')} — "
                    f"{measure.get('metric', '')} {measure.get('op', '')} "
                    f"{measure.get('value', '')}"
                )
        budget = goal.get("budget") or {}
        if isinstance(budget, dict) and budget:
            lines.append(
                "Goal budget: "
                + "; ".join(f"{key}={value}" for key, value in budget.items())
            )
        if goal.get("risk_class"):
            lines.append(f"Risk class: {goal['risk_class']}")
        for condition in goal.get("stop_conditions") or []:
            lines.append(f"Stop condition: {condition}")
        for action in goal.get("irreversible_actions") or []:
            lines.append(f"Irreversible action (needs a gate): {action}")
    elif isinstance(goal, str) and goal:
        lines.append(f"Goal: {goal}")
    lines.append(
        f"Plan: {brief.get('plan_id', '')} (version {brief.get('plan_version', '')})"
    )
    for node in brief.get("failed_nodes") or []:
        if not isinstance(node, dict):
            continue
        lines.append(
            f"Failed node {node.get('id', '')}: {node.get('objective', '')} "
            f"(claw: {node.get('assigned_claw', '')}). "
            f"Hypothesis: {node.get('hypothesis', '')}. "
            f"Measurement: {_describe_measurement(node.get('measurement') or {})}. "
            f"Error: {node.get('error', '') or 'none'}."
        )
    measurements = brief.get("measurements") or []
    if measurements:
        lines.append("Measurements so far:")
        for measurement in measurements:
            lines.append(f"- {_describe_measurement(measurement)}")
    memory = brief.get("memory") or []
    if memory:
        lines.append("Relevant past experiments (do not repeat a recorded failure):")
        for record in memory:
            lines.append(f"- {_describe_memory_record(record)}")
    remaining = brief.get("remaining_budget") or {}
    if isinstance(remaining, dict) and remaining:
        lines.append(
            "Remaining budget: "
            + "; ".join(f"{key}={value}" for key, value in remaining.items())
        )
    if brief.get("instruction"):
        lines.append(f"Instruction: {brief['instruction']}")
    return "\n".join(lines)


def _goal_text_from_brief(brief: dict[str, Any]) -> str:
    """The goal's objective text from a re-plan brief, for route matching."""
    goal = brief.get("goal", {})
    if isinstance(goal, dict):
        return str(goal.get("objective", "") or "")
    if isinstance(goal, str):
        return goal
    return ""


def _route_goal_shape(
    goal_text: str | None,
    repo_root: str | Path | None = None,
    *,
    delta: bool = True,
) -> str:
    """A concrete node shape for a goal naming an existing API route.

    When the goal names a route, the plan has a known shape — edit the
    serving module, write tests, call the live route on the API port —
    so the prompt hands the model a NEW structure to produce instead of
    leaving it to discover the shape by trial and error inside a
    3-attempt budget (retry #5 burned attempts 1-2 on the fake before
    the engagement error fired) or to re-serialize the brief's failed
    steps as JSON (retry #4 replan 1 echoed the brief's delta steps
    verbatim). ``delta`` says whether the plan is a re-plan delta
    ("the delta plan must follow this shape") or a first attempt
    ("the plan must follow this shape"). Returns "" when the goal
    names no route.
    """
    if not goal_text:
        return ""
    index = build_repo_index(repo_root)
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return ""
    plan_word = "delta plan" if delta else "plan"
    lines = [f"ROUTE GOAL — the {plan_word} must follow this shape:"]
    for route, module in named:
        # Absolute repo path, not the relative module name: the model
        # must not have to guess which directory the code lives in
        # (retry #9 named the module under the workspace path instead).
        module_path = str(index.root / module)
        edit = (
            f"node 1 edits {module_path} (scaffold claw, or a bash heredoc) to "
            f"implement the endpoint"
        )
        handler = index.route_handlers.get(route)
        if handler is not None:
            # Retry #13: the shape said "implement the endpoint" and the
            # model read that as "write a new app" -- twice, in Flask.
            # Name the edit as a branch on the handler that already serves
            # the route, so the valid move is concrete.
            edit += f" — add a {route} branch to {handler}"
        lines.append(
            f"- {route} is served by {module_path} on port {index.api_port}: "
            f"{edit}; node 2 writes the tests (scaffold or "
            f"heredoc) and runs them; node 3 verifies live with "
            f"curl http://localhost:{index.api_port}{route}. Do NOT probe "
            f"another service's {route} path, and do NOT echo a JSON answer. "
            f"Do NOT re-serialize these instructions as JSON — they are "
            f"guidance, not output; your output is a NEW 'nodes' array."
        )
    return "\n".join(lines)

def _format_intent_for_prompt(intent: TaskIntent) -> str:
    """Render a TaskIntent as plain prose lines for the planner prompt.

    The planner prompt must never end with a copyable JSON object: with
    temperature 0 the model echoes whatever JSON it last saw in the prompt.
    That is what happened in the /health retry — Gemma returned the
    TaskIntent JSON verbatim 3/3 times instead of a plan DAG. Prose lines
    carry the same facts with nothing to copy. (Same fix family as the
    Nov 14 intake echo, where the few-shot JSON example was removed and
    the request was reframed as a "User Request:" line followed by
    "JSON Output:".)
    """
    lines = [
        f"Goal: {intent.goal}",
        f"Project type: {intent.project_type}",
    ]
    if intent.capabilities:
        lines.append(f"Capabilities to use: {', '.join(intent.capabilities)}")
    constraints = dict(intent.constraints or {})
    workspace = constraints.pop("workspace", None)
    if workspace:
        lines.append(f"Workspace: {workspace}")
    if constraints:
        pairs = "; ".join(f"{key}={value}" for key, value in constraints.items())
        lines.append(f"Other constraints: {pairs}")
    if intent.missing_inputs:
        lines.append(f"Missing inputs: {', '.join(intent.missing_inputs)}")
    else:
        lines.append("Missing inputs: none")
    return "\n".join(lines)


def _describe_failed_output(response_text: str) -> str:
    """Summarize a rejected planner output WITHOUT quoting it back.

    Pasting the failed output into the repair prompt reinforces the echo:
    at temperature 0 the model copies the JSON it last saw, so a repair
    round that re-presents the echoed TaskIntent JSON just produces the
    same echo again. The repair prompt gets a classification instead —
    enough to tell the model what went wrong, with nothing copyable.
    """
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return "text that is not valid JSON."
    keys = ", ".join(sorted(str(key) for key in payload))
    if "nodes" in payload:
        return f"a JSON object with keys {keys} whose 'nodes' failed validation."
    return f"a JSON object with keys {keys} and no 'nodes' array."


# PlanNode fields the planner shape demands on every node. Retry #11:
# attempt 2 (the stdout-echo fake) had objective/assigned_claw/inputs on
# its nodes and attempt 3 dropped all three while inventing
# description/output fields, so the shape error was named only after the
# repair budget was spent. The repair prompt names any such regression.
_REQUIRED_NODE_FIELDS = ("objective", "assigned_claw", "inputs", "criteria")


def _node_field_names(response_text: str) -> set[str]:
    """Field names present on the nodes of a planner reply, best-effort."""
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return set()
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return set()
    fields: set[str] = set()
    for node in nodes:
        if isinstance(node, dict):
            fields.update(str(key) for key in node)
    return fields


def _dropped_fields_note(prev_text: str | None, cur_text: str) -> str:
    """Name required fields the previous attempt had and this one dropped.

    Returns "" when there is no previous attempt, when either reply is
    not parseable JSON, or when nothing was dropped. Field names only --
    no quoted content -- so the note cannot feed an echo the way pasting
    the failed output would.
    """
    if not prev_text:
        return ""
    dropped = [
        field
        for field in _REQUIRED_NODE_FIELDS
        if field in _node_field_names(prev_text)
        and field not in _node_field_names(cur_text)
    ]
    if not dropped:
        return ""
    quoted = ", ".join(f"'{field}'" for field in dropped)
    return (
        f"Your previous attempt had {quoted} on its nodes; do not drop "
        f"them — every node needs all of: id, objective, depends_on, "
        f"assigned_claw, inputs, outputs, acceptance, criteria.\n"
    )


def _call_llm_with_repair(
    system_prompt: str,
    user_prompt: str,
    settings: LLMSettings | None,
    *,
    max_repairs: int = 2,
    max_free_reprompts: int = 2,
    goal_text: str | None = None,
    workspace: str | None = None,
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Call the LLM and parse the plan, repairing rejected output.

    On LLMResponseError the model is re-prompted with the rejection reason
    and a short description of what was wrong with the failed output —
    never the output itself, which at temperature 0 would just be copied
    again. Bounded: at most ``max_repairs`` repair attempts, then the last
    error is raised. Nothing is silently substituted — a plan the model
    cannot fix is an honest failure.

    The repair prompt also carries the full NODE SHAPE block, so the
    model has the valid node in front of it while fixing the named
    error instead of having to remember it from the system prompt
    (retry #11: the model fixed depends_on and invented
    description/output nodes instead). When the new output drops a
    required field the previous attempt had, the prompt names the
    dropped fields.

    Cheap rejections do not spend the repair budget (retry #12): an
    empty plan, or a rejection whose only errors are stand-in /
    code-overwrite fakes, is re-prompted for free, up to
    ``max_free_reprompts`` times. The budget is not raised — junk that
    keeps coming eventually spends repairs and the loop still ends
    (at most 1 + max_free_reprompts + max_repairs calls). A cheap
    rejection past the free cap falls through to the repair budget.
    """
    prompt = user_prompt
    last_error: LLMResponseError | None = None
    prev_response_text: str | None = None
    repairs_used = 0
    free_used = 0
    while True:
        response_text = call_llm(system_prompt, prompt, settings=settings)
        try:
            return parse_planner_response(
                response_text, goal_text=goal_text, workspace=workspace
            )
        except LLMResponseError as exc:
            last_error = exc
            if isinstance(exc, CheapRejectionError) and free_used < max_free_reprompts:
                free_used += 1
            elif repairs_used < max_repairs:
                repairs_used += 1
            else:
                break
            prompt = (
                f"{user_prompt}\n\n"
                "Your previous output was REJECTED. Do not repeat it.\n"
                f"Rejection reason: {exc}\n"
                f"Previous output: {_describe_failed_output(response_text)}\n"
                f"{_dropped_fields_note(prev_response_text, response_text)}"
                f"\nNODE SHAPE — {_NODE_SHAPE_EXAMPLE}\n\n"
                "Return ONLY a corrected JSON object with a \"nodes\" array that "
                "fixes ALL of the rejection reasons listed above. Every node "
                "must match the NODE SHAPE above — do not drop a field from it "
                "and do not invent a field outside it.\n"
                "JSON Output:"
            )
            prev_response_text = response_text
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
    the code that actually exists. When the goal names an existing route,
    the prompt also carries the grounded ROUTE GOAL shape, so the first
    attempt already has the valid move in front of it instead of having
    to discover it by trial and error.
    """
    workspace = (intent.constraints or {}).get("workspace")
    context = format_repo_context(
        build_repo_index(repo_root), workspace=workspace
    )
    shape = _route_goal_shape(intent.goal, repo_root, delta=False)
    shape_block = f"\n\n{shape}" if shape else ""
    nodes, metadata = _call_llm_with_repair(
        PLANNER_SYSTEM_PROMPT,
        f"{context}\n\n"
        f"{_format_intent_for_prompt(intent)}{shape_block}\n\n"
        "Produce a NEW execution plan DAG for the goal above. Your output "
        "must be a JSON object with a \"nodes\" array — do NOT repeat the "
        "goal text or any JSON from this prompt.\n"
        "JSON Output:",
        settings,
        max_repairs=max_repairs,
        goal_text=intent.goal,
        workspace=workspace,
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

    context = format_repo_context(
        build_repo_index(repo_root), workspace=brief.get("workspace")
    )
    shape = _route_goal_shape(_goal_text_from_brief(brief), repo_root)
    shape_block = f"\n\n{shape}" if shape else ""
    # The replan needs the node shape in front of it, not just as a
    # rejection after the fact: retry #9's replan 1 invented a
    # node/action schema and burned the budget before the shape was ever
    # named. The ROUTE GOAL shape is route-scoped; the node shape is
    # general, so it rides along on every replan.
    node_shape_block = f"\n\nNODE SHAPE — {_NODE_SHAPE_EXAMPLE}"
    nodes, _metadata = _call_llm_with_repair(
        REPLAN_SYSTEM_PROMPT,
        f"{context}\n\n{_format_brief_for_prompt(brief)}{shape_block}{node_shape_block}\n\n"
        "Produce a NEW delta plan DAG for the brief above. Do NOT repeat, "
        "quote, or echo the brief — it is your input, not your output.\n"
        "JSON Output:",
        settings,
        max_repairs=max_repairs,
        goal_text=_goal_text_from_brief(brief),
        workspace=brief.get("workspace"),
    )
    return nodes
