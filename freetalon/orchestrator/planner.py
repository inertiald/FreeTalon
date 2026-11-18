"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations
import ast
from dataclasses import dataclass, replace as dataclass_replace
import json
import os
import re
import shlex
import hashlib
import heapq
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Collection

from pydantic import ValidationError

from .intake import (
    LLMBackendError,
    LLMResponseError,
    LLMSettings,
    call_llm,
    parse_json_object,
)
from .repo_index import (
    build_repo_index,
    resolve_repo_root,
    duplicate_scaffold_error,
    format_repo_context,
    framework_in_text,
    qualified_def_names,
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
from .prompts import (
    FORMATTER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    REPLAN_SYSTEM_PROMPT,
)
from .tool_registry import (
    CODE_FILE_SUFFIXES,
    KNOWN_CLAW_NAMES,
    is_code_path,
    is_placeholder_content,
    metrics_reported_by,
)


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
    of burning a repair round. Retry #17: the model followed the
    grounded skeleton shape but filled it with placeholder comments,
    and the placeholder rejection spent the last repair round — so a
    rejection whose only errors are placeholder scaffolds is cheap
    too: a comment stub costs the model nothing to replace with real
    code. A subclass of LLMResponseError, so callers that catch
    LLMResponseError are unaffected.
    """


class EmptyPlanError(CheapRejectionError):
    """An empty plan ({"nodes": []}) — a degenerate output, not a plan.

    Retry #16: the re-planner returned empty plans 5 times in a row after
    an informative failure, and the escalation carried no grounded
    guidance. A distinct subclass lets the repair loop tell an empty reply
    from any other cheap rejection, so a run of empties can be answered
    with a concrete grounded skeleton instead of the same rejection again.
    A subclass of CheapRejectionError, so the free re-prompt accounting
    and every caller that catches CheapRejectionError are unaffected.
    """


class StuckRepairError(LLMResponseError):
    """The repair loop stopped: the same rejection kept recurring.

    Cycle 2 (steal #5, OpenHands' StuckDetector): when one planning
    call rejects the model's output with the identical structured
    error signature (same codes on the same nodes) for the third
    time, further repair prompts are not steering — they are the
    loop ping-ponging on a defect it cannot fix by re-asking. The
    repair loop raises this instead of spending the remaining
    attempts, so the failure escalates to the loop's judge / re-plan
    path with the repeated defect named. A subclass of
    LLMResponseError, so every caller that treats a planning failure
    as an escalation is unaffected; the repeated rejection's
    PlanError records ride along on ``plan_errors``.
    """


# --- Composable pipeline stages with structured errors (planner A/B
# refactor, 2026-11-17) ---
#
# parse_planner_response is a pipeline of small stages over the raw
# output: sanitize -> schema (JSON shape, node schema, DAG structure)
# -> interpreter-normalize -> materialize (anchored edits) -> validate
# (the content rules). Every rejection the pipeline raises carries a
# list of PlanError records on the exception's ``plan_errors``
# attribute: a stable machine CODE, the human message (byte-identical
# to the text the numbered rejection has always carried), the node the
# error belongs to, and the stage that produced it. The repair loop
# steers off the CODES — never off substrings of the message. (Both
# historical steering contradictions, retries #28 and #33, came from
# steering blocks pattern-matching rejection wording: the shape block
# blessed a wholesale replacement because the guard's phrase was not
# yet in its marker list, then blessed a dedented delta the same way.
# A code cannot drift out of a list when a message is reworded.)

STAGE_SCHEMA = "schema"
STAGE_MATERIALIZE = "materialize"
STAGE_VALIDATE = "validate"


@dataclass(frozen=True)
class PlanError:
    """One structured planner rejection error."""

    code: str
    message: str
    node_id: str | None = None
    stage: str = STAGE_VALIDATE

    def __str__(self) -> str:
        return self.message


_NODE_ID_PREFIX_RE = re.compile(r"^Planner node '([^']+)'")


def _node_id_from_message(message: str) -> str | None:
    """The node a validator message belongs to, from its own prefix.

    Every per-node validator message opens with "Planner node '<id>'"
    — the convention is ours and total, so the aggregator can attribute
    records without changing any validator's return shape.
    """
    match = _NODE_ID_PREFIX_RE.match(message)
    return match.group(1) if match else None


def _error_records(
    code: str, messages: Collection[str], stage: str = STAGE_VALIDATE
) -> list[PlanError]:
    """Wrap one validator's messages as PlanError records with its code."""
    return [
        PlanError(
            code=code,
            message=message,
            node_id=_node_id_from_message(message),
            stage=stage,
        )
        for message in messages
    ]


def _attach_plan_errors(
    exc: LLMResponseError, records: Collection[PlanError]
) -> LLMResponseError:
    """Attach structured error records to a rejection about to be raised."""
    exc.plan_errors = list(records)  # type: ignore[attr-defined]
    return exc


def _single_error_rejection(code: str, message: str, stage: str) -> LLMResponseError:
    """An LLMResponseError carrying exactly one structured record."""
    return _attach_plan_errors(
        LLMResponseError(message),
        [PlanError(code=code, message=message, stage=stage)],
    )


def _numbered_rejection(
    records: Collection[PlanError], *, cheap: bool = False, suffix: str = ""
) -> LLMResponseError:
    """The house numbered rejection, built from structured records.

    The message format is the one the repair loop has always received
    ("rejected with N errors — fix ALL of them:" + numbered lines +
    an optional shape suffix); the records ride along on the exception
    so the repair loop can steer off their codes.
    """
    records = list(records)
    noun = "error" if len(records) == 1 else "errors"
    lines = "\n".join(
        f"{index}. {record.message}"
        for index, record in enumerate(records, start=1)
    )
    message = (
        f"Planner output was rejected with {len(records)} {noun} — "
        f"fix ALL of them:\n{lines}{suffix}"
    )
    exc: LLMResponseError = (
        CheapRejectionError(message) if cheap else LLMResponseError(message)
    )
    return _attach_plan_errors(exc, records)


class _ModuleSourceCache:
    """Per-parse cache of existing-module sources and definition names.

    The validators each used to re-read and re-parse the same module
    files (api.py was parsed by the repo index, the overwrite guard,
    and the code-append check on every parse_planner_response call).
    One cache is created per call and threaded through the aggregator,
    so each module file is read and parsed at most once per parse;
    validators called directly (tests) pass no cache and get a fresh
    one, with identical results.
    """

    def __init__(self) -> None:
        self._sources: dict[Path, str | None] = {}
        self._def_names: dict[Path, list[str]] = {}

    def source(self, path: Path) -> str | None:
        """The file's text, or None when it cannot be read (missing)."""
        if path not in self._sources:
            try:
                self._sources[path] = path.read_text(encoding="utf-8")
            except OSError:
                self._sources[path] = None
        return self._sources[path]

    def def_names(self, path: Path) -> list[str]:
        """The file's qualified definition names ([] when unreadable)."""
        if path not in self._def_names:
            source = self.source(path)
            self._def_names[path] = (
                qualified_def_names(source) if source is not None else []
            )
        return self._def_names[path]


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
    repo_root: str | Path | None = None,
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
    round on output that was never a plan. The placeholder checks run
    on the lenient views too (retry #21): attempt 2 carried BOTH a
    placeholder heredoc and a typo ("assigned_claws"), and the
    placeholder defect never reached the repair prompt, so the model
    fixed the typo and returned the same placeholder plan. ``repo_index``/
    ``goal_text`` are the repo index and goal for the grounded-move
    sentence; ``repo_root`` is the repo root for the placeholder
    scaffold's exists-on-disk check. All three are optional
    (hand-built plans pass none of them).
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
        views = [
            view
            for view in (
                _lenient_node_view(raw, position)
                for position, raw in enumerate(raw_nodes, start=1)
            )
            if view is not None
        ]
        standin_errors = _static_standin_errors(
            views,
            index=repo_index,
            goal_text=goal_text,
        )
        standin_records = _error_records("static_standin", standin_errors)
        # Retry #21: the same masking as the stand-in check, one fix
        # later — attempt 2 carried BOTH a placeholder heredoc (step-1)
        # and a typo (step-4 "assigned_claws"), and the schema-error path
        # ran only the stand-in check, so the placeholder defect never
        # reached the repair prompt; the model fixed the typo and returned
        # the same placeholder plan, burning another round. Run the
        # placeholder checks on the lenient views too, with the same
        # dedup as _collect_plan_errors (a scaffold placeholder already
        # names the defect; the heredoc one is the same defect, not a new
        # one). The full _collect_plan_errors is deliberately NOT run
        # here: several of its errors duplicate the schema errors (an
        # unknown claw for a missing assigned_claw, a no-criteria error
        # for a missing criteria field), which would change the pinned
        # counts for the retry-#7/#11 invented-schema fixtures — the
        # placeholder/stand-in checks are the fake-naming checks that
        # belong in the schema path.
        placeholder_records = _placeholder_scaffold_error_records(
            views, repo_root, index=repo_index, goal_text=goal_text
        )
        if not placeholder_records:
            placeholder_records = _error_records(
                "placeholder_bash",
                _placeholder_bash_errors(
                    views, repo_root, index=repo_index, goal_text=goal_text
                ),
            )
        all_records = (
            placeholder_records
            + standin_records
            + _error_records("node_schema", errors, stage=STAGE_SCHEMA)
        )
        raise _numbered_rejection(
            all_records,
            cheap=bool(standin_records or placeholder_records),
            suffix=f"\n{_NODE_SHAPE_EXAMPLE}",
        )
    return nodes


def _coerce_nodes(
    nodes: list[PlanNode] | list[dict[str, Any]],
    repo_index: Any | None = None,
    goal_text: str | None = None,
    repo_root: str | Path | None = None,
) -> list[PlanNode]:
    if nodes and isinstance(nodes[0], PlanNode):
        return list(nodes)
    return _validate_raw_nodes(
        list(nodes), repo_index=repo_index, goal_text=goal_text, repo_root=repo_root
    )


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
    repo_root: str | Path | None = None,
    workspace: str | None = None,
    id_labels: dict[str, str] | None = None,
) -> list[PlanNode]:
    """Validate a DAG, topologically order it, and assign deterministic node ids.

    ``repo_index``/``goal_text`` are threaded to the schema check so a
    fake with wrong-typed ids is named as a fake (retry #13); both
    optional (hand-built plans pass neither). ``repo_root``/``workspace``
    are threaded to the dependency check so the unknown-dependency
    rejection can also name the other validation errors (retry #15);
    both optional. ``repo_root`` is also threaded to the schema check so
    a placeholder defect is named alongside a schema error (retry #21). ``id_labels`` is an optional out-parameter: when
    given, it is filled with {normalized_id: original_id} so a caller
    can name the model's own ids in a rejection (retry #17: the
    placeholder error named 'node-1' while the plan's id was
    'step-1').
    """
    nodes = _coerce_nodes(
        nodes, repo_index=repo_index, goal_text=goal_text, repo_root=repo_root
    )
    if not nodes:
        # Cheap rejection (retry #12): an empty plan is a degenerate
        # output, not a plan that needs a repair round to fix.
        raise _attach_plan_errors(
            EmptyPlanError("Planner response must include at least one plan node."),
            [
                PlanError(
                    code="empty_plan",
                    message="Planner response must include at least one plan node.",
                    stage=STAGE_SCHEMA,
                )
            ],
        )

    node_map = {node.id: node for node in nodes}
    if len(node_map) != len(nodes):
        raise _single_error_rejection(
            "duplicate_node_ids",
            "Planner response contained duplicate node ids.",
            STAGE_SCHEMA,
        )

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
                raise _attach_plan_errors(
                    LLMResponseError(
                        f"Planner node '{node.id}' cannot depend on itself."
                    ),
                    [
                        PlanError(
                            code="self_dependency",
                            message=f"Planner node '{node.id}' cannot depend on itself.",
                            node_id=node.id,
                            stage=STAGE_SCHEMA,
                        )
                    ],
                )
            if dependency not in node_map:
                unknown_dep_errors.append(
                    _unknown_dependency_error(node, node_map, dependency)
                )
    if unknown_dep_errors:
        # Retry #15 attempt 2: the dependency error was the ONLY error the
        # model saw, so the repair round fixed the dependency and kept the
        # Flask heredoc to /tmp -- the framework and /tmp signals were lost
        # because normalize raises before _collect_plan_errors runs. Append
        # the other validation errors of the plan (framework, /tmp, prose,
        # stand-in, ...) so the repair round sees the real defects in the
        # same rejection. Duplicate ids, cycles, and self-dependency still
        # raise first, as before.
        extra_records, _cheap = _collect_plan_error_records(
            nodes, repo_root, goal_text, workspace, index=repo_index
        )
        all_records = _error_records(
            "unknown_dependency", unknown_dep_errors, stage=STAGE_SCHEMA
        ) + extra_records
        raise _numbered_rejection(
            all_records, suffix=f"\n{_DEPENDS_ON_SHAPE}"
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
        raise _single_error_rejection(
            "dependency_cycle",
            "Planner response contained a dependency cycle.",
            STAGE_SCHEMA,
        )

    original_to_normalized_id = {
        original_id: f"node-{index}" for index, original_id in enumerate(ordered_ids, start=1)
    }
    if id_labels is not None:
        id_labels.update(
            {
                normalized: original
                for original, normalized in original_to_normalized_id.items()
            }
        )
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
    # Replaces patterns like '[UNK_BYTE_0xe29681▁a]a' with ' a'. The hex in
    # the artifact is the UTF-8 encoding of U+2581 itself, so the space
    # this pass restores is the same space marker the bare-character pass
    # below converts -- one artifact arriving through two decode paths.
    cleaned = re.sub(r'\[UNK_BYTE_[^\]]+▁?([^\]]*)\]', r' \1', raw_text)
    # Convert any leftover lower block characters to the spaces they are.
    # A bare U+2581 IS the SentencePiece space marker, and in longer
    # generations Gemma emits whole indentation runs as bare markers;
    # deleting them (the old behavior) stripped every indented line
    # flush-left and the scaffold failed the plan-time parse check --
    # retry #24 lost all three attempts to exactly that. The mojibake
    # 3-char form is the same marker mangled by a latin-1 decode. Order
    # matters: the UNK_BYTE pass above runs first and consumes the
    # bracketed artifacts whole (the markers inside them included), so
    # this pass only ever converts the bare runs.
    cleaned = cleaned.replace("\xe2\x96\x81", " ").replace("▁", " ")
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


def _duplicate_key_errors(
    raw_text: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Duplicate JSON keys in the raw planner output, as named rejections.

    Retry #15 attempt 2: the three nodes were concatenated into a single
    object (the separators between nodes were missing, and
    outputs/acceptance/criteria were nested inside inputs), so the
    last-wins collapse turned three nodes into one node-3 depending on
    the nonexistent node-2 -- and the only error the model saw was the
    unknown dependency, nothing about the real defect (a Flask heredoc
    to /tmp). Runs on the RAW text before the normal parse collapses it;
    returns ([], []) when the text is not parseable JSON (the normal parse
    raises "not valid JSON" then) or has no duplicates.

    Also returns the split nodes: when a node-shaped object has duplicate
    keys, the merged object is split back into the nodes it was
    concatenated from (a new node starts whenever a key repeats), with
    outputs/acceptance/criteria hoisted out of inputs — so the caller can
    run the per-node validation on what the model meant to write, instead
    of only on the collapsed node.

    Only node-shaped objects count: a flat prose object whose keys are
    repeated words (retry #8 tokenized prose) is a prose re-serialization,
    not a node collapse -- the prose check names it, so duplicate keys
    must not shadow it. Nested fields only count inside an actual inputs
    dict: a node that merely has command and criteria at the same level
    (retry #7 invented schema) is a schema error, not a nesting.
    """
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    attempts = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and start < end:
        attempts.append(stripped[start : end + 1])
    for attempt in attempts:
        found: list[tuple[str, list[str], list[int]]] = []
        nested_fields: set[str] = set()
        split_nodes: list[dict[str, Any]] = []

        def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            obj: dict[str, Any] = {}
            counts: dict[str, int] = {}
            for key, value in pairs:
                counts[key] = counts.get(key, 0) + 1
                obj[key] = value
            dups = [key for key, count in counts.items() if count > 1]
            is_top = "nodes" in obj
            is_node = isinstance(obj.get("id"), str) and any(
                key in obj
                for key in (
                    "objective",
                    "depends_on",
                    "assigned_claw",
                    "inputs",
                    "outputs",
                    "acceptance",
                    "criteria",
                )
            )
            # Nesting is detected BEFORE the split below hoists fields
            # out of the inputs dicts: the hoist pops outputs/acceptance/
            # criteria out of the same dict objects, so a check after it
            # would see nothing nested (retry #15 attempt 2 lost the
            # nesting error this way).
            inputs = obj.get("inputs")
            if isinstance(inputs, dict):
                nested_fields.update(
                    field
                    for field in ("outputs", "acceptance", "criteria")
                    if field in inputs
                )
            if dups and (is_top or is_node):
                if is_top:
                    label = "the top-level object"
                else:
                    label = "node object (id " + repr(obj.get("id")) + ")"
                found.append((label, dups, [counts[key] for key in dups]))
                if is_node:
                    # Split the merged object back into the nodes it was
                    # concatenated from: a new node starts whenever a key
                    # repeats (retry #15 attempt 2). The split nodes let
                    # the caller run the per-node validation on what the
                    # model meant to write, instead of only on the
                    # collapsed node.
                    split: list[dict[str, Any]] = []
                    current: dict[str, Any] = {}
                    for key, value in pairs:
                        if key in current:
                            split.append(current)
                            current = {}
                        current[key] = value
                    if current:
                        split.append(current)
                    for node in split:
                        node_inputs = node.get("inputs")
                        if isinstance(node_inputs, dict):
                            for field in ("outputs", "acceptance", "criteria"):
                                if field in node_inputs:
                                    node[field] = node_inputs.pop(field)
                    split_nodes.extend(split)
            return obj

        try:
            parsed = json.loads(attempt, object_pairs_hook=hook)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if not found and not nested_fields:
            return [], []
        errors: list[str] = []
        sep = "},{"
        for label, dups, dup_counts in found:
            keys = ", ".join(
                f"{key!r} ({count} times)"
                for key, count in zip(dups, dup_counts)
            )
            errors.append(
                f"Planner output has duplicate JSON keys in {label}: {keys} — "
                "the JSON parser kept only the last value of each, so "
                "several nodes collapsed into one. The nodes were probably "
                "concatenated into a single object: separate each node with "
                f"{sep!r} (close the node object, comma, open the next one) so "
                "the \"nodes\" array has one object per node, each with its "
                "own id, objective, depends_on, assigned_claw, inputs, "
                "outputs, acceptance and criteria."
            )
        if nested_fields:
            nested = ", ".join(f"{field!r}" for field in sorted(nested_fields))
            errors.append(
                f"Planner output nests {nested} inside \"inputs\" — they belong "
                "beside \"inputs\" at the node level, not inside it. Move them "
                "out of the \"inputs\" object so each node has its own outputs, "
                "acceptance and criteria."
            )
        return errors, split_nodes
    return [], []


def _relabel_nodes_for_errors(
    nodes: list[PlanNode], id_labels: dict[str, str]
) -> list[PlanNode]:
    """Copies of ``nodes`` with ids relabeled to the model's original ids.

    Retry #17: normalize renames the model's ids to deterministic
    node-1, node-2, ... — but a rejection naming 'node-1' when the model
    wrote 'step-1' points at a node the model cannot find in its own
    output. Error messages name the original ids; depends_on is
    remapped the same way so the copies stay a consistent DAG. The
    returned nodes are unaffected (they keep normalized ids).
    """
    if not id_labels:
        return nodes
    return [
        node.model_copy(
            update={
                "id": id_labels.get(node.id, node.id),
                "depends_on": [id_labels.get(dep, dep) for dep in node.depends_on],
            }
        )
        for node in nodes
    ]


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
    
    dup_errors, split_nodes = _duplicate_key_errors(raw_text)
    if dup_errors:
        # Retry #15 attempt 2: the duplicate-key rejection named only the
        # structural defect (the collapse), so the repair round never saw
        # the Flask heredoc to /tmp — the framework and /tmp signals were
        # lost, the same way they were lost on the unknown-dependency raise
        # before b66c5ac. Run the per-node validation on the split nodes
        # (what the model meant to write) and carry those errors in the
        # same rejection, so the repair round sees the real defects.
        repo_index = build_repo_index(repo_root) if goal_text else None
        views = [
            view
            for view in (
                _lenient_node_view(raw, position)
                for position, raw in enumerate(split_nodes, start=1)
            )
            if view is not None
        ]
        extra_records, _cheap = _collect_plan_error_records(
            views, repo_root, goal_text, workspace, index=repo_index
        )
        all_records = _error_records(
            "duplicate_keys", dup_errors, stage=STAGE_SCHEMA
        ) + extra_records
        raise _numbered_rejection(
            all_records, suffix=f"\n{_NODE_SHAPE_EXAMPLE}"
        )
    payload = parse_json_object(raw_text)
    if not isinstance(payload, dict):
        raise _single_error_rejection(
            "not_json_object",
            "Planner response must be a JSON object.",
            STAGE_SCHEMA,
        )

    nodes_raw = payload.get("nodes")
    if not isinstance(nodes_raw, list):
        # Brief-echo shape (retry #4 replan 1): the model re-serialized the
        # re-plan brief as {"instruction": ..., "delta_plan": {"steps": [...]}}.
        # Name the echo explicitly — the repair prompt carries the rejection
        # verbatim, so the model learns the brief is input, not output.
        brief_keys = {"instruction", "delta_plan", "steps", "failed_nodes", "remaining_budget"}
        if brief_keys & set(payload):
            raise _single_error_rejection(
                "brief_echo",
                "Planner output copied the re-plan brief instead of a plan DAG — "
                "it has an 'instruction'/'delta_plan'/'steps' object and no 'nodes' "
                "array. Output ONLY a JSON object with a NEW 'nodes' array of "
                "PlanNodes (id, objective, depends_on, assigned_claw, inputs, "
                "outputs, acceptance, criteria, error) — never re-serialize the "
                "brief, its instruction, or its failed steps.",
                STAGE_SCHEMA,
            )
        if _looks_like_prose_reserialization(payload):
            raise _single_error_rejection(
                "prose_reserialization",
                "Planner output re-serialized the prompt's instructions as JSON "
                "instead of a plan DAG — it turned guidance prose (for example "
                "the ROUTE GOAL shape) into a JSON object whose keys are words "
                "from the prompt and which has no 'nodes' array. The prompt's "
                "instructions are input, not output: return ONLY a JSON object "
                "with a NEW 'nodes' array of PlanNodes (id, objective, "
                "depends_on, assigned_claw, inputs, outputs, acceptance, "
                "criteria, error) — never turn any sentence from the prompt "
                "into JSON keys or values.",
                STAGE_SCHEMA,
            )
        raise _single_error_rejection(
            "missing_nodes_array",
            "Planner output must contain a 'nodes' array.",
            STAGE_SCHEMA,
        )

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

    # Deterministic interpreter normalization (retry #27): rewrite
    # the model-spelled interpreter tokens in the plan's bash
    # commands to the grounded ./.venv/bin/python before validation
    # -- the model will not copy the string verbatim, and the repair
    # loop cannot steer it. The validator remains as the backstop
    # for every shape this does not cover.
    _normalize_plan_interpreters(nodes_raw)

    # Anchored-edit materialization (retry #30): a scaffold node for
    # an existing module may carry an "anchor" + "position" + only
    # the new code instead of the whole file; splice each edit into
    # the current module here, on the raw dicts, so the schema check,
    # every content check, and the executor see the full-content
    # nodes they see today.
    _materialize_plan_edits(nodes_raw, repo_root)

    # Payload channel (cycle 2): a sole heredoc .py write whose
    # payload fails to parse but is deterministically repairable is
    # rerouted to the scaffold form here, at the same raw-dict seam,
    # so validation judges the repaired content as a file write
    # instead of rejecting the plan on heredoc fidelity (CP2).
    _materialize_heredoc_writes(nodes_raw)

    # Normalize: topologically order the DAG and assign deterministic
    # node-1, node-2, ... ids, remapping depends_on. Without this the raw
    # LLM ids/order leak through and downstream code cannot rely on
    # dependency order. The repo index is built once here and shared by
    # the schema check (which needs it to name a stand-in fake on raw
    # nodes, retry #13) and the collected validation errors below, so a
    # fake with wrong-typed ids is named as a fake instead of only as a
    # schema error — and the rejection is cheap, not repair-consuming.
    index = build_repo_index(repo_root) if goal_text else None
    id_labels: dict[str, str] = {}
    nodes = normalize_plan_nodes(
        nodes_raw,
        repo_index=index,
        goal_text=goal_text,
        repo_root=repo_root,
        workspace=workspace,
        id_labels=id_labels,
    )

    # Planner hardening: an invented claw name or an unmeasurable node is a
    # plan error here, where the repair retry can fix it — not at execution
    # time. Hand-built plans bypass this function (they use
    # build_execution_plan directly), so existing callers are unaffected.
    # All per-node errors are collected and reported in ONE rejection, so a
    # plan with several independent errors can be fixed in a single repair
    # round instead of burning the repair budget one error at a time.
    # The rejection must name the model's own ids ('step-1'), not the
    # normalized node-1/node-2 ids normalize assigned (retry #17: the
    # placeholder error named 'node-1' while the plan's id was
    # 'step-1'). Error collection runs on relabeled copies; the
    # returned nodes keep their normalized ids.
    records, cheap = _collect_plan_error_records(
        _relabel_nodes_for_errors(nodes, id_labels),
        repo_root,
        goal_text,
        workspace,
        index=index,
        modules=_ModuleSourceCache(),
    )
    if records:
        raise _numbered_rejection(records, cheap=cheap)

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


def _usable_criterion_error(
    criterion: Any, node: PlanNode, repo_root: str | Path | None = None
) -> str | None:
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
        # Retry #23: an exit_code criterion with a `command` measures
        # that command (see measure._eval_exit_code) — the command is
        # a bash command too, so its interpreter must exist. A bare
        # criterion (no command) measures the node command's exit code
        # — that is the explicit default, and it stays valid.
        command = criterion.get("command")
        if command is None:
            return None
        if not isinstance(command, str) or not command.strip():
            return "exit_code criterion 'command' must be a non-empty string"
        interpreter_errors = _bash_interpreter_errors(command, repo_root)
        if interpreter_errors:
            return "; ".join(interpreter_errors)
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


# A bash "command" that is prose, not shell (retry #14): the v1 plan's
# node-1 command was a description of the edit — "/home/seeker/...
# /freetalon/api.py:HiveAPIServer, _ThreadingHTTPServer - Add a /health
# branch to the do_GET method ..." — and /bin/sh failed on "(" in 0.001s.
# The model keeps emitting descriptions where an executable edit belongs.
_PROSE_MARKERS = (
    " the ", " a ", " an ", " to ", " of ", " for ", " that ", " which ",
    " and ", " with ", " in ", " on ", " is ", " are ", " returns ",
    " reports ", " verify ", " check ",
)
_CODE_CITATION_RE = re.compile(
    r"^[^:\s]*\.(?:"
    + "|".join(sorted(s.lstrip(".") for s in CODE_FILE_SUFFIXES))
    + r"):"
)
_SEGMENT_SPLIT_RE = re.compile(r"(?:\n|&&|\|\||;|\||>|<| - )")


def _segment_is_prose(segment: str) -> bool:
    """True when one shell segment is a prose description, not a command.

    A segment is prose when it reads as a sentence: at least four words,
    no shell verb, and either a code citation as its first token
    ("freetalon/api.py:Handler," — a file reference, not an executable)
    or a capitalised first word with sentence markers ("Add a /health
    branch to the do_GET method ..."). Real commands start with a
    lowercase command name (curl, python3, nvidia-smi), a path to an
    executable, or a shell keyword — never with a capitalised English
    word. An assignment first token (VAR=1 ...) is not prose.
    """
    words = segment.split()
    if len(words) < 4:
        return False
    first = words[0]
    if _CODE_CITATION_RE.match(first):
        return True
    if not first[0].isupper() or "=" in first:
        return False
    lowered = f" {segment.lower()} "
    return any(marker in lowered for marker in _PROSE_MARKERS)


def _prose_command_error(shell: str) -> str | None:
    """Return an error if a bash command is prose, not shell — else None.

    Two shapes, both general: a command that is only comments (retry #14
    v2 node-2: "# Define a test command to verify the /health
    endpoint." — a comment does nothing), and a command with a prose
    segment (retry #14 v1 node-1: a description of the edit, node-3: a
    real curl with a " - Verify ..." prose tail). The rejection names
    the grounded move — a heredoc or the scaffold claw with real code —
    the way the anti-fake rejection does. Heredoc bodies are already
    stripped from ``shell`` by the caller, so file content is never
    mistaken for prose.
    """
    stripped = shell.strip()
    if not stripped:
        return None
    content_lines = [
        line.strip()
        for line in stripped.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not content_lines:
        return (
            "bash command is only a comment — it does nothing. Write the "
            "edit as a heredoc (cat > <path> <<'EOF' ... EOF) or use the "
            "scaffold claw with real code, and run the result."
        )
    for segment in _SEGMENT_SPLIT_RE.split(shell):
        if _segment_is_prose(segment):
            return (
                f"bash command is prose, not a shell command: "
                f"{segment.strip()!r} — describe nothing; write the edit "
                "as a heredoc (cat > <path> <<'EOF' ... EOF) or use the "
                "scaffold claw with real code, and run the result."
            )
    return None


# Shell builtins and keywords: never an interpreter to validate.
_SHELL_SKIP_TOKENS = frozenset({
    "cd", "echo", "export", "source", ".", "test", "[", "[[", "true",
    "false", "set", "unset", "eval", "exec", "exit", "return", "shift",
    "trap", "umask", "alias", "unalias", "if", "then", "else", "elif",
    "fi", "for", "while", "until", "do", "done", "case", "esac",
    "function", "select", "time", "coproc", "{", "}",
})

# Command separators for interpreter extraction: a new segment starts
# after &&, ||, ;, |, or a newline. Redirections and ` - ` splits are
# NOT separators here — they belong to the segment's command.
_INTERPRETER_SEGMENT_RE = re.compile(r"(?:\n|&&|\|\||;|\|)")


# --- Deterministic interpreter normalization (retry #27) ---
#
# Measured across retries #25-#27: the model will not copy
# './.venv/bin/python' verbatim into a bash command -- zero occurrences
# across every planner output in #26 and #27 -- it writes
# './venv/bin/python' (no dot) instead. _bash_interpreter_errors
# correctly rejects that spelling, and the repair loop, even when handed
# the exact error naming the grounded form, cannot steer the string
# (retry #27 attempt 3 was otherwise fully valid and died on exactly
# this). Prompt-layer grounding (69c0ece) and validation (9ea9a43) have
# done what they can; the correction is deterministic, in code, at parse
# time: rewrite the interpreter token in command position to the
# grounded form BEFORE validation, so the parsed plan the executor
# receives already carries it. The validator stays in place as the
# backstop for every shape this does not cover.
_GROUNDED_INTERPRETER = "./.venv/bin/python"
_INTERPRETER_REWRITES = {
    "./venv/bin/python": _GROUNDED_INTERPRETER,
    "venv/bin/python": _GROUNDED_INTERPRETER,
    "python": _GROUNDED_INTERPRETER,
    "python3": _GROUNDED_INTERPRETER,
}

# _INTERPRETER_SEGMENT_RE with the separators kept, so a command can be
# reassembled byte-identically apart from the rewritten tokens.
_INTERPRETER_SEGMENT_SPLIT_RE = re.compile(r"(\n|&&|\|\||;|\|)")


def _heredoc_body_spans(command: str) -> list[tuple[int, int]]:
    """Character spans of heredoc bodies in a bash command.

    Mirrors _strip_heredoc_bodies' detection (same regex, same line
    walk): the head line and the closing marker line are shell and stay
    unprotected; the lines between them are file content and are never
    rewritten. An unterminated heredoc protects nothing past its head
    line, exactly as the strip leaves the tail in place for validation.
    """
    spans: list[tuple[int, int]] = []
    rest_start = 0
    while True:
        match = _STRIP_HEREDOC_RE.search(command, rest_start)
        if not match:
            return spans
        head_end = command.find("\n", match.end())
        if head_end == -1:
            return spans
        body_start = head_end + 1
        marker = match.group("marker")
        cursor = body_start
        marker_start: int | None = None
        while cursor < len(command):
            line_end = command.find("\n", cursor)
            if line_end == -1:
                line_end = len(command)
            if command[cursor:line_end].strip() == marker:
                marker_start = cursor
                rest_start = line_end
                break
            cursor = line_end + 1
        if marker_start is None:
            return spans
        spans.append((body_start, marker_start))


def _scan_token_span(text: str, start: int) -> tuple[int, int] | None:
    """Raw span of the next shell token at/after ``start``.

    Quote- and escape-aware, mirroring shlex's token boundaries for the
    first tokens of a segment: whitespace separates, a backslash escapes
    the next character, single quotes are literal, double quotes allow
    backslash escapes. Returns None when only whitespace remains.
    """
    index = start
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text):
        return None
    token_start = index
    while index < len(text):
        char = text[index]
        if char in " \t":
            break
        if char == "\\":
            index += 2
            continue
        if char == "'":
            close = text.find("'", index + 1)
            if close == -1:
                return token_start, len(text)
            index = close + 1
            continue
        if char == '"':
            cursor = index + 1
            while cursor < len(text):
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == '"':
                    break
                cursor += 1
            index = cursor + 1 if cursor < len(text) else len(text)
            continue
        index += 1
    return token_start, index


def _normalize_segment_interpreter(segment: str) -> str:
    """Rewrite one shell segment's interpreter token, when covered.

    The interpreter token is identified exactly as _bash_interpreter_errors
    identifies it: shlex tokens, leading env assignments stripped, first
    remaining token. Only an exact match against _INTERPRETER_REWRITES is
    rewritten, at its raw span (a quoted command word loses its quotes --
    the replacement needs none). Anything else -- skip tokens, other
    binaries, paths outside the covered spellings, segments shlex cannot
    tokenize -- is returned byte-identical for the validator to judge.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return segment
    index = 0
    while index < len(tokens) and re.match(
        r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]
    ):
        index += 1
    if index >= len(tokens):
        return segment
    replacement = _INTERPRETER_REWRITES.get(tokens[index])
    if replacement is None:
        return segment
    span: tuple[int, int] | None = None
    position = 0
    for _ in range(index + 1):
        span = _scan_token_span(segment, position)
        if span is None:
            return segment
        position = span[1]
    return segment[: span[0]] + replacement + segment[span[1] :]


def _normalize_bash_interpreters(command: str) -> str:
    """Normalize every covered interpreter token in a bash command.

    Segments are split exactly as the validator splits them
    (_INTERPRETER_SEGMENT_RE); heredoc body lines are file content, not
    shell, and are never rewritten. A command with no covered token is
    returned byte-identical.
    """
    if not command:
        return command
    protected = _heredoc_body_spans(command)
    parts = _INTERPRETER_SEGMENT_SPLIT_RE.split(command)
    out: list[str] = []
    offset = 0
    for position, part in enumerate(parts):
        start = offset
        offset += len(part)
        if position % 2 == 1 or not part:
            out.append(part)
            continue
        if any(start < end and begin < offset for begin, end in protected):
            out.append(part)
            continue
        out.append(_normalize_segment_interpreter(part))
    return "".join(out)


def _normalize_plan_interpreters(nodes_raw: list[Any]) -> None:
    """Apply interpreter normalization to a raw plan's bash commands.

    Walks exactly the strings the validator treats as bash commands:
    the 'command'/'cmd' input of bash/shell_exec nodes (the strings
    _command_errors validates) and the 'command' of exit_code criteria
    (the strings _usable_criterion_error validates). Scaffold contents
    and every other input are file content or data, never commands, and
    are not touched. Runs on the raw node dicts before
    normalize_plan_nodes, so the PlanNodes the executor receives carry
    the normalized commands.
    """
    for node in nodes_raw:
        if not isinstance(node, dict):
            continue
        if node.get("assigned_claw") in ("bash", "shell_exec"):
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                for key in ("command", "cmd"):
                    value = inputs.get(key)
                    if isinstance(value, str):
                        inputs[key] = _normalize_bash_interpreters(value)
        criteria = node.get("criteria")
        if isinstance(criteria, list):
            for criterion in criteria:
                if isinstance(criterion, dict):
                    value = criterion.get("command")
                    if isinstance(value, str):
                        criterion["command"] = _normalize_bash_interpreters(value)


# --- Anchored-edit scaffolds for existing modules (retry #30) ---
#
# Retry #30: shown api.py's full source (the 1b6de40 grounding) and
# told to write the FULL updated module, the model emitted step_1's
# scaffold as a 5-line comment stub copying the steering text into
# comments — zero code — and the placeholder check rejected all
# three (best-formed yet) plans. A 12B model will not retype a
# 219-line module as one node among five, so the grounded source
# now reaches the scaffold BY CONSTRUCTION: for an existing module
# the model may emit an anchored edit instead of the whole file —
# "anchor" (a qualified definition name in the target), "position"
# ("replace" substitutes that definition; "after"/"before" insert
# a sibling next to it), and "content" holding ONLY the new code.
# Materialization runs in parse_planner_response on the raw node
# dicts, before normalize_plan_nodes: it reads the current file,
# splices the delta in at the anchor's exact line span (decorators
# included), and rewrites the node's inputs to the legacy
# full-content form, so the schema check, every content check
# (placeholder, parse, overwrite guard — all unchanged, all judging
# the materialized module), and the executor see exactly the
# full-content nodes they see today. The executor cannot tell the
# difference and is not touched.
#
# Retry #32: the grounded skeleton's step-1 delta blank was
# instruction prose, and retry #31's model copied it into the
# edit's content (its re-wrap dropping the '#' markers, so the
# placeholder checks missed the echo and the indentation gate
# named the wrong defect at full price). Two fixes: the blank is
# now real code (_anchored_delta_sketch — the definition's current
# source with the branch extended in code form; instructions live
# in the steering text around the slot), and materialization
# detects comment/prose-only deltas (_delta_carries_code) at the
# placeholder check, BEFORE the indentation gate, so an echoed
# blank is named placeholder content and stays a cheap rejection.
#
# Retry #33: retry #32's model used the edit form with the
# real-code sketch but DEDENTED its copy — the uniform 12-space
# file indentation stripped (def at column 0, body at 4) — and
# the gate rejected it. The model's copy normalizes whatever it
# carries (#31 dropped the '#' markers re-wrapping prose; #32
# dropped the uniform indent): the same mechanical variance the
# interpreter normalization absorbs for command strings.
# Materialization now re-indents a delta that differs from the
# anchor's indentation ONLY by a uniform shift — dedented by its
# own base indent it is coherent (for "replace", exactly the one
# anchored definition; for "after"/"before", it parses as
# sibling code) — and judges the re-indented materialized module
# exactly as before. Non-uniform or incoherent deltas keep the
# indentation error, both indents stated.

_EDIT_POSITIONS = ("replace", "after", "before")

_EDIT_SHAPE_EXAMPLE = (
    'ANCHORED EDIT — to change ONE definition of an existing module, '
    'a scaffold node\'s inputs may be {"file_path": "<path>", '
    '"anchor": "<qualified definition name, e.g. '
    'ClassName.method_name>", "position": "replace" | "after" | '
    '"before", "content": "<only the new code, written at the '
    'indentation it has in the file>"}: "replace" substitutes the '
    'anchored definition (content is its complete new version, def '
    'line included), "after"/"before" insert the content as a '
    'sibling next to it. The system applies the edit to the current '
    'file, so every other definition is preserved — never retype '
    'the whole module.'
)


def _definition_spans(source: str) -> list[tuple[str, int, int, int]]:
    """(qualified name, first line, last line, indent) per definition.

    The span walk mirrors repo_index.qualified_def_names exactly —
    it descends only through def/class nodes (a definition nested
    under an ``if``/``try`` inside a function is invisible to both),
    so the spans and the qualified names the guard and the
    grounding use always agree. A definition's first line is its
    first decorator line when it has decorators. [] when the
    source does not parse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[str, int, int, int]] = []

    def _visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                qualified = prefix + child.name
                start = child.lineno
                for decorator in child.decorator_list:
                    start = min(start, decorator.lineno)
                spans.append(
                    (
                        qualified,
                        start,
                        child.end_lineno or child.lineno,
                        child.col_offset,
                    )
                )
                _visit(child, qualified + ".")

    _visit(tree, "")
    return spans


def _route_branch_anchor(source: str, route: str) -> str | None:
    """Qualified name of the definition holding a route's branch.

    The innermost definition whose span covers an ``if`` whose test
    compares something == the route literal (the same branch shape
    repo_index._route_branch reads) — for /health in freetalon/api.py,
    HiveAPIServer.start.Handler.do_GET. None when the source does
    not parse or no such branch sits inside a definition.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    spans = _definition_spans(source)
    if not spans:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == route
        ):
            continue
        containing = [
            span for span in spans if span[1] <= node.lineno <= span[2]
        ]
        if containing:
            return min(containing, key=lambda span: span[2] - span[1])[0]
    return None


def _delta_top_level_def_names(content: str) -> list[str] | None:
    """Top-level def names of a dedented edit delta, or None.

    A "replace" delta is ONE definition written at its file
    indentation; dedented, it must parse with exactly that
    definition at its top level. None when the dedented delta does
    not parse at all (a body fragment, a comment sketch, a syntax
    error) — the caller names the defect.
    """
    try:
        tree = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return None
    return [
        node.name
        for node in tree.body
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
    ]


# First-token shapes of a line of Python code, for the code-presence
# test below: a keyword-led statement, a decorator, a name-led
# assignment / call / subscript / operator expression, or a line
# opening a bracket, string, or number. Prose lines — sentences —
# match none of these: they open with a capitalized word followed
# by another word, not by an operator or a bracket.
_CODE_LINE_RE = re.compile(
    r"^(?:"
    r"(?:def|class|if|elif|else|for|while|try|except|finally|with|"
    r"return|import|from|pass|raise|assert|yield|await|async|lambda|"
    r"global|nonlocal|del|break|continue)\b"
    r"|@\w"
    r"|[A-Za-z_][\w.]*\s*(?:\(|\[|[+\-*/%@&|^]?=|//=|\*\*=|<<=|>>=|"
    r"==|!=|<=|>=)"
    r"|[\[{(]"
    r"|[\"']"
    r"|\d"
    r")"
)


def _delta_carries_code(content: str) -> bool:
    """True when an anchored edit's delta contains at least one real code line.

    Retry #32: retry #31's model echoed the grounded skeleton's
    delta blank — instruction prose — into the edit's content, but
    its re-wrap dropped the '#' markers from the prose lines, so
    is_placeholder_content (which strips comment lines and then
    finds prose, not a lone string) said False and
    _is_placeholder_stub (which needs a self-labeling comment)
    said False too: the delta fell through to the indentation
    gate and was rejected for starting at 0 spaces — a true fact
    naming the wrong defect, and a full-price rejection for what
    is a placeholder echo. A delta carries code when its dedented
    text parses with at least one real statement (anything but a
    lone docstring), or — when it does not parse, as with a body
    fragment or a typo'd def line — when at least one non-comment
    line is code-shaped (_CODE_LINE_RE). Comment/prose-only deltas
    carry no code, so the placeholder check in
    _materialize_scaffold_edit names them BEFORE indentation is
    judged and the rejection stays cheap; real code at the wrong
    indentation still reaches the indentation gate untouched.
    """
    try:
        tree = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        tree = None
    if tree is not None:
        return any(
            not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
            for stmt in tree.body
        )
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        if _CODE_LINE_RE.match(stripped):
            return True
    return False


def _dedent_delta_lines(
    delta_lines: list[str], base_indent: int
) -> list[str] | None:
    """The delta with its own base indent removed, or None.

    The dedent a uniform copy survives: every non-blank line
    carries at least ``base_indent`` leading spaces (the first
    line is genuinely the least-indented line — a line dedented
    below the base, like a string-literal content line at
    column 0 under an indented def, breaks uniformity) and no
    line's leading whitespace mixes in a tab. Blank lines pass
    through untouched. None when the delta is not a uniform
    shift of anything — the caller keeps the indentation error.
    """
    dedented: list[str] = []
    for line in delta_lines:
        if not line.strip():
            dedented.append(line)
            continue
        whitespace = line[: len(line) - len(line.lstrip())]
        if "\t" in whitespace or len(whitespace) < base_indent:
            return None
        dedented.append(line[base_indent:])
    return dedented


def _delta_parses_as_code(lines: list[str]) -> bool:
    """True when the joined lines parse as Python code.

    The coherence bar for an "after"/"before" delta on the
    re-indent path: dedented by its own base indent, sibling
    code must parse as module-level code. ("replace" has the
    stricter bar — _delta_top_level_def_names must name exactly
    the anchored definition — applied by the shape check the
    gate already runs.)
    """
    try:
        ast.parse("\n".join(lines))
    except SyntaxError:
        return False
    return True


def _materialize_scaffold_edit(
    label: str,
    inputs: dict[str, Any],
    repo_root: str | Path | None,
    sources: dict[Path, str],
) -> tuple[str, bool] | None:
    """Materialize one node's anchored edit in place.

    On success the node's inputs hold the full materialized module
    as "content" (the "anchor"/"position" keys removed) and None is
    returned. On failure an (error, placeholder_delta) pair is
    returned and the inputs are left untouched: every error names
    the defect and the fix, so the repair round can steer.
    A delta whose indentation differs from the anchor's only by a
    uniform shift is re-indented to the anchor's indentation
    before splicing (retry #33); every other check judges the
    materialized module exactly as before.
    ``sources`` carries the current text per resolved target path
    across the nodes of one plan, seeded from disk: a second edit
    of the same module materializes against the first edit's
    result, not against the stale file.
    """
    anchor = inputs.get("anchor")
    position = inputs.get("position")
    target = inputs.get("file_path") or inputs.get("path")
    content = inputs.get("content")
    if not isinstance(anchor, str) or not anchor.strip():
        return (
            f"Planner node '{label}' carries a 'position' but no usable "
            "'anchor' — an anchored edit names the definition it "
            "changes: 'anchor' is a qualified definition name from the "
            "target module (e.g. 'ClassName.method_name'), 'position' "
            "is 'replace', 'after', or 'before', and 'content' holds "
            "only the new code. Drop both keys to scaffold a full file "
            "instead.",
            False,
        )
    if position not in _EDIT_POSITIONS:
        return (
            f"Planner node '{label}' anchors an edit on {anchor!r} "
            f"with position {position!r} — 'position' must be 'replace' "
            "(the content substitutes the anchored definition), "
            "'after', or 'before' (the content is inserted as a "
            "sibling next to it).",
            False,
        )
    if not isinstance(target, str) or not target:
        return (
            f"Planner node '{label}' anchors an edit on {anchor!r} but "
            "names no 'file_path' — an anchored edit needs the "
            "existing module's path in 'file_path'.",
            False,
        )
    if not isinstance(content, str):
        return (
            f"Planner node '{label}' anchors an edit on {anchor!r} in "
            f"{target!r} but its 'content' is missing or not a string "
            "— the content is the new code for the anchored "
            "definition, and nothing else.",
            False,
        )
    if not is_code_path(target) or Path(target).suffix.lower() != ".py":
        return (
            f"Planner node '{label}' anchors an edit on {target!r}, "
            "which is not a Python module — anchored edits work on "
            "existing Python modules only; scaffold the full content "
            "for anything else, without an 'anchor'.",
            False,
        )
    resolved = _resolve_plan_target(target, resolve_repo_root(repo_root))
    if resolved in sources:
        source = sources[resolved]
    else:
        try:
            source = resolved.read_text(encoding="utf-8")
        except OSError:
            return (
                f"Planner node '{label}' anchors an edit on {target!r}, "
                "which does not exist — an anchored edit changes an "
                "existing module; to create a new file, scaffold its "
                "full content without an 'anchor'.",
                False,
            )
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return (
            f"Planner node '{label}' anchors an edit on {target!r}, "
            "whose current content does not parse as Python "
            f"({_syntax_error_detail(exc)}) — no definition in it can "
            "be anchored. Scaffold the full corrected module instead, "
            "without an 'anchor'.",
            False,
        )
    names = qualified_def_names(source)
    if len(names) < 4:
        return (
            f"Planner node '{label}' anchors an edit on {target!r}, "
            f"which has only {len(names)} definitions — too small for "
            "an anchored edit (the floor is four, the overwrite "
            "guard's). Scaffold the full updated module instead, "
            "without an 'anchor'.",
            False,
        )
    if (
        not content.strip()
        or is_placeholder_content(content)
        or _is_placeholder_stub(content)
        or not _delta_carries_code(content)
    ):
        return (
            f"Planner node '{label}' scaffolds {target!r} with an "
            "anchored edit whose content is placeholder content only "
            "(comments, prose, and/or a docstring — no code) — the "
            f"edit's content must be the real new code for {anchor!r}: "
            "the complete definition (def line included) for "
            "'replace', or the complete new sibling for "
            "'after'/'before', at its indentation in the file — not "
            "a comment about it, and not instructions copied from "
            "the prompt.",
            True,
        )
    spans = _definition_spans(source)
    matches = [span for span in spans if span[0] == anchor]
    if not matches:
        return (
            f"Planner node '{label}' anchors its edit on {anchor!r}, "
            f"which is not a definition of {target!r} — the module's "
            f"definitions are: {', '.join(names)}. Anchor on one of "
            "those names, exactly as written.",
            False,
        )
    if len(matches) > 1:
        return (
            f"Planner node '{label}' anchors its edit on {anchor!r}, "
            f"which names {len(matches)} definitions of {target!r} — "
            "an anchor must name exactly one. Anchor on a unique "
            "definition, or scaffold the full updated module instead.",
            False,
        )
    _name, start, end, indent = matches[0]
    delta_lines = content.splitlines()
    while delta_lines and not delta_lines[0].strip():
        delta_lines.pop(0)
    while delta_lines and not delta_lines[-1].strip():
        delta_lines.pop()
    delta_indent = len(delta_lines[0]) - len(delta_lines[0].lstrip())
    if delta_indent != indent:
        indentation_error = (
            f"Planner node '{label}' anchors an edit on {anchor!r} in "
            f"{target!r}, but the edit's content starts at "
            f"{delta_indent} spaces of indentation while {anchor!r} "
            f"sits at {indent} spaces in the file — write the new code "
            "at exactly the indentation it must have in the file (for "
            "'replace', the complete definition starting with its "
            f"def/class line at {indent} spaces).",
            False,
        )
        # Retry #33: absorb a UNIFORM shift — the model's copy
        # stripped (or added) the same columns on every line, the
        # mechanical variance the interpreter normalization
        # absorbs for command strings. The shift is uniform when
        # the delta dedents by its own base indent (the first
        # line's — the measure this gate has always used) and the
        # dedented form is coherent: for 'replace', exactly the
        # one anchored definition (the shape check below, applied
        # here to the dedented form); for 'after'/'before', it
        # parses as sibling code. Anything else — a line dedented
        # below the base, a tab-led line, a fragment, the wrong
        # definition — is not a uniform copy of the anchored code
        # and keeps the indentation error, both indents stated.
        dedented = _dedent_delta_lines(delta_lines, delta_indent)
        if dedented is None:
            return indentation_error
        if position == "replace":
            leaf = anchor.rsplit(".", 1)[-1]
            if _delta_top_level_def_names("\n".join(dedented)) != [leaf]:
                return indentation_error
        elif not _delta_parses_as_code(dedented):
            return indentation_error
        prefix = " " * indent
        delta_lines = [
            prefix + line if line.strip() else line for line in dedented
        ]
        content = "\n".join(delta_lines)
    if position == "replace":
        leaf = anchor.rsplit(".", 1)[-1]
        top = _delta_top_level_def_names(content)
        if top is None:
            return (
                f"Planner node '{label}' anchors a 'replace' edit on "
                f"{anchor!r} in {target!r}, but the content does not "
                "parse as a definition on its own — for 'replace' the "
                f"content is the complete new version of {anchor!r} "
                "(decorators if any, the def/class line, and the whole "
                "body), at its indentation in the file — not a fragment "
                "of its body and not a comment about it.",
                False,
            )
        if top != [leaf]:
            shown = ", ".join(top) if top else "no definition"
            return (
                f"Planner node '{label}' anchors a 'replace' edit on "
                f"{anchor!r} in {target!r}, but the content defines "
                f"{shown} at its top level — for 'replace' the content "
                f"is the complete new version of {anchor!r} alone (one "
                f"definition, named {leaf!r}), not the whole module. "
                "Put helper code inside the definition, or add it with "
                "a separate 'after' edit anchored on the same "
                "definition.",
                False,
            )
    lines = source.splitlines()
    if position == "replace":
        lines[start - 1 : end] = delta_lines
    elif position == "after":
        lines[end:end] = delta_lines
    else:  # before
        lines[start - 1 : start - 1] = delta_lines
    materialized = "\n".join(lines)
    if source.endswith("\n"):
        materialized += "\n"
    try:
        ast.parse(materialized)
    except SyntaxError as exc:
        return (
            f"Planner node '{label}' anchors an edit on {anchor!r} in "
            f"{target!r}, but the result does not parse as Python "
            f"({_syntax_error_detail(exc)}) — the edit's content must "
            "be complete, valid code for the anchored definition at "
            "its indentation in the file.",
            False,
        )
    lost = [name for name in names if name not in qualified_def_names(materialized)]
    if lost:
        shown = ", ".join(lost[:8])
        if len(lost) > 8:
            shown += f", ... ({len(lost) - 8} more)"
        return (
            f"Planner node '{label}' anchors an edit on {anchor!r} in "
            f"{target!r}, but the result loses existing definitions: "
            f"{shown} — an anchored edit must keep every definition "
            "it does not replace. For 'replace', the content must be "
            f"the complete new version of {anchor!r} itself (same "
            "name, same indentation); inserted code must not swallow "
            "the definitions around the anchor.",
            False,
        )
    inputs["content"] = materialized
    inputs.pop("anchor", None)
    inputs.pop("position", None)
    sources[resolved] = materialized
    return None


def _materialize_plan_edits(
    nodes_raw: list[Any], repo_root: str | Path | None
) -> None:
    """Rewrite anchored-edit scaffold nodes to full-content nodes.

    Runs on the raw node dicts in parse_planner_response, after
    interpreter normalization and before normalize_plan_nodes — the
    same in-place raw-dict seam. A node is an edit-form node when it
    is a scaffold/file_writer node whose inputs carry an "anchor"
    (or a stray "position" — a malformed edit, named as such).
    Legacy full-content nodes are untouched. Errors from every edit
    node are collected into ONE numbered rejection in the house
    format, naming the model's own raw node ids; when every error
    is a placeholder delta, the rejection is cheap (the retry-#17
    placeholder rule: a comment stub costs the model nothing to
    replace with real code, so the re-prompt is free) — and its
    records carry the 'placeholder_delta' code, so the
    repair loop answers it with the grounded skeleton.
    """
    errors: list[tuple[str, bool]] = []
    sources: dict[Path, str] = {}
    for node_position, raw in enumerate(nodes_raw, start=1):
        if not isinstance(raw, dict):
            continue
        if raw.get("assigned_claw") not in ("scaffold", "file_writer"):
            continue
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if "anchor" not in inputs and "position" not in inputs:
            continue
        raw_id = raw.get("id")
        label = raw_id if isinstance(raw_id, str) else f"position {node_position}"
        outcome = _materialize_scaffold_edit(label, inputs, repo_root, sources)
        if outcome is not None:
            errors.append(outcome)
    if not errors:
        return
    # Structured records (A/B refactor): a placeholder delta carries
    # its own code — it is cheap, and its repair round answers with
    # the grounded skeleton, not the materialization steering block.
    records = [
        PlanError(
            code="placeholder_delta" if placeholder else "anchored_edit",
            message=error,
            node_id=_node_id_from_message(error),
            stage=STAGE_MATERIALIZE,
        )
        for error, placeholder in errors
    ]
    raise _numbered_rejection(
        records,
        cheap=all(placeholder for _error, placeholder in errors),
        suffix=f"\n{_EDIT_SHAPE_EXAMPLE}",
    )


def _sole_heredoc_write(command: str) -> tuple[str, str] | None:
    """The (target, payload) of a bash command whose ONLY effect is one
    plain ``cat > target <<marker`` heredoc write, else None.

    The payload channel (cycle 2, steal #1b) may only reroute a write
    it can judge whole: exactly one extracted write, not an append,
    and nothing else in the command — no second write, no command
    chained after the closing marker, no ``cd`` prefix. Tee and echo
    writes are not this shape (their extraction differs) and keep the
    validator path they have today. The stripped shell (heredoc
    bodies removed) of a sole write is exactly two non-blank lines:
    the cat head and the closing marker.
    """
    writes = _bash_code_writes(command)
    if len(writes) != 1:
        return None
    target, content, is_append = writes[0]
    if is_append:
        return None
    shell = _strip_heredoc_bodies(command)
    lines = [line.strip() for line in shell.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    head, marker_line = lines
    match = _HEREDOC_RE.fullmatch(head)
    if match is None or marker_line != match.group("marker"):
        return None
    return target, content


def _indent_block_body(lines: list[str], header_index: int) -> list[str] | None:
    """Indent the body a compound header is missing, or None.

    The repair for the payload defect of CP2 / retry #33: a header
    line (``if __name__ == "__main__":``) whose body statement sits
    at the header's own indent (``unittest.main()`` at column 0), so
    the payload cannot parse at all. The un-indented statement has
    exactly one parseable reading — it is the header's body — so
    indenting it (and the lines that belong to it: the following
    lines indented deeper than it) by one level is a deterministic
    repair, verified by the caller re-parsing the result. None when
    the shape is anything else: no following statement, a body that
    is already indented (the error is elsewhere), or tabs in the
    indentation being shifted.
    """
    if header_index < 0 or header_index >= len(lines):
        return None
    header = lines[header_index]
    if not header.strip():
        return None
    header_indent = len(header) - len(header.lstrip(" "))
    body_index: int | None = None
    for index in range(header_index + 1, len(lines)):
        if lines[index].strip():
            body_index = index
            break
    if body_index is None:
        return None
    body_line = lines[body_index]
    body_whitespace = body_line[: len(body_line) - len(body_line.lstrip())]
    if "\t" in body_whitespace:
        return None
    body_indent = len(body_whitespace)
    if body_indent > header_indent:
        return None
    shift = " " * 4
    repaired = list(lines)
    repaired[body_index] = shift + body_line
    for index in range(body_index + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        whitespace = line[: len(line) - len(line.lstrip())]
        if "\t" in whitespace or len(whitespace) <= body_indent:
            break
        repaired[index] = shift + line
    return repaired


def _repair_indented_blocks(lines: list[str]) -> list[str] | None:
    """The lines with every missing-indented-block defect repaired.

    Iterates the deterministic repair of _indent_block_body: parse,
    and while the failure is an "expected an indented block" error,
    indent that header's body and parse again (a payload can carry
    the defect more than once). Returns the lines once they parse,
    or None when any other syntax error stands in the way — that
    payload is the heredoc_parse validator's to reject, unchanged.
    """
    current = list(lines)
    for _ in range(4):
        try:
            ast.parse("\n".join(current))
            return current
        except SyntaxError as exc:
            message = exc.msg or ""
            if "expected an indented block" not in message:
                return None
            match = re.search(r"on line (\d+)", message)
            if match is None:
                return None
            repaired = _indent_block_body(current, int(match.group(1)) - 1)
            if repaired is None:
                return None
            current = repaired
    try:
        ast.parse("\n".join(current))
    except SyntaxError:
        return None
    return current


def _repair_python_payload(content: str) -> str | None:
    """A parseable repair of a Python payload, or None.

    The two deterministic normalizations the harness owns, in order:
    a uniform dedent (the payload copied with one extra indent
    level throughout — the anchored-edit delta rule, applied to a
    whole payload) and the missing-indented-block repair of
    _repair_indented_blocks. None when the payload already parses
    (nothing to repair) or when neither normalization makes it
    parse (the defect is the model's to fix, and the heredoc_parse
    rejection names it as before).
    """
    try:
        ast.parse(content)
        return None
    except SyntaxError:
        pass
    lines = content.split("\n")
    candidates = [lines]
    indents = [
        len(line) - len(line.lstrip(" ")) for line in lines if line.strip()
    ]
    base = min(indents) if indents else 0
    if base > 0:
        dedented = _dedent_delta_lines(lines, base)
        if dedented is not None:
            candidates.insert(0, dedented)
    for candidate in candidates:
        fixed = _repair_indented_blocks(candidate)
        if fixed is not None:
            return "\n".join(fixed)
    return None


def _materialize_heredoc_writes(nodes_raw: list[Any]) -> None:
    """Reroute repairable sole heredoc .py writes to the scaffold claw.

    Cycle 2 (steal #1b — the two-channel payload fix). The CP2
    binding defect: the model's real plan wrote its test file as a
    bash heredoc whose payload dedented ``unittest.main()`` out of
    its ``__main__`` guard — one indentation slip inside a shell
    string, and the whole plan died at validation while its repair
    regressed in form. The harness's own grounded skeleton teaches
    the heredoc form for test files, so rejecting the form (the
    alternative) would fight the harness's own steering. Instead the
    materialize stage owns the payload: when a bash node's ONLY
    effect is one plain heredoc write of a .py file and the payload
    fails to parse but a deterministic normalization repairs it
    (verified by re-parsing), the node is rewritten in place to the
    scaffold form — file_path + the repaired content — so the file
    travels the payload channel (node data, judged by the scaffold
    validators: parse, placeholder, retention, elision) instead of
    depending on heredoc fidelity at execution time.

    Deliberately narrow, so nothing that works today changes shape:
    a payload that already parses stays a bash heredoc; a payload no
    normalization repairs stays a bash heredoc too, and the
    heredoc_parse validator rejects it with the same teaching error
    as before; appends, tee/echo writes, multi-write commands, and
    non-.py targets are untouched; a hardcoded-answer literal stays
    the stand-in rule's, and a placeholder payload stays the
    placeholder rules'.
    """
    for raw in nodes_raw:
        if not isinstance(raw, dict):
            continue
        if raw.get("assigned_claw") not in ("bash", "shell_exec"):
            continue
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if set(inputs) - {"command", "cmd"}:
            continue
        command = inputs.get("command") or inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        sole = _sole_heredoc_write(command)
        if sole is None:
            continue
        target, content = sole
        target = target.strip().strip("'\"")
        if not target or Path(target).suffix.lower() != ".py":
            continue
        if not is_code_path(target):
            continue
        if _literal_is_answer(content):
            continue
        repaired = _repair_python_payload(content)
        if repaired is None:
            continue
        if is_placeholder_content(repaired):
            continue
        raw["assigned_claw"] = "scaffold"
        raw["inputs"] = {"file_path": target, "content": repaired}


def _bash_interpreter_errors(
    command: str, repo_root: str | Path | None
) -> list[str]:
    """Return errors for a bash command whose interpreter/binary does not exist.

    Retry #23: v1 node-3 ran `./venv/bin/python` (no such path — the
    venv is `.venv`), v2 node-3 ran bare `python` (does not resolve on
    Ubuntu, only python3), and both failed with exit 127 at execution
    time. Validate at plan time, one segment at a time: the first word
    of each shell segment must be a real interpreter/binary. Bare
    `python` is always an error on Ubuntu — the grounded form is
    `./.venv/bin/python` (the repo venv). A path token is resolved
    against the repo root (disk-independent, like the missing-directory
    check) and must exist there; a path outside the repo (workspace,
    /tmp, /usr/bin) is not judged. Other bare names are NOT checked:
    PATH varies by machine, so a which() check would answer
    differently on seeker7 and in CI. Heredoc bodies are stripped by
    the caller — file content is not shell.

    Retry #26: the root is resolved UNCONDITIONALLY. The driver
    calls plan_task_intent with repo_root=None, and this check used
    to keep root=None in that case and skip every path token — so
    the live path accepted ./venv/bin/python commands that every
    test and probe (which pass the root explicitly) rejected, and
    the plan was accepted and executed. resolve_repo_root(None) is
    the checkout holding this package — the same fallback
    build_repo_index uses — so the driver path now validates
    against the same repo the grounding checks read. (Audit: every
    other repo_root consumer in this module already resolves
    unconditionally or reads the index; this was the only
    None-sensitive check.)
    """
    errors: list[str] = []
    root = resolve_repo_root(repo_root)
    for segment in _INTERPRETER_SEGMENT_RE.split(command):
        tokens: list[str] = []
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        # Strip leading env assignments (VAR=1 python ...) — the
        # command is the first token after them.
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        first = tokens[0]
        if first in _SHELL_SKIP_TOKENS:
            continue
        if first == "python":
            errors.append(
                "bash command uses bare `python`, which does not resolve "
                "on Ubuntu (only python3) — use ./.venv/bin/python (the "
                "repo venv) or python3"
            )
            continue
        if "/" not in first:
            continue
        # A path token: resolve against the repo root and require it
        # to exist. Outside the repo it is not judged.
        if first.startswith("/"):
            rel = _repo_relative_target(first, root)
            if rel is None:
                continue
            candidate = root / rel
        else:
            stripped = first[2:] if first.startswith("./") else first
            if stripped.startswith("../") or stripped.startswith("/"):
                continue
            # The repo venv is a machine fact, not a repo-tree fact:
            # seeker7 has .venv, a CI checkout does not, so its
            # presence cannot be judged from the checkout — and the
            # grounded move names ./.venv/bin/python as the correct
            # interpreter, so a plan that follows it must not be
            # rejected for it. (The retry-#23 defect is ./venv, without
            # the dot — that path IS judged, and does not exist.)
            if stripped == ".venv" or stripped.startswith(".venv/"):
                continue
            candidate = root / stripped
        if not candidate.exists():
            if Path(first).name == "python":
                errors.append(
                    f"bash command uses {first!r}, which does not exist in "
                    "the repo — the repo venv is ./.venv/bin/python, not "
                    f"{first!r}"
                )
            else:
                errors.append(
                    f"bash command uses {first!r}, which does not exist in "
                    f"the repo (repo root {root})"
                )
    return errors


def _validate_bash_command(
    node: PlanNode, repo_root: str | Path | None = None
) -> list[str]:
    """Return every error in a bash/shell_exec node's command (fix 2).

    Checks: the command is a non-empty string; it tokenizes
    (shlex.split — unbalanced quotes fail); it is not words glued together
    (the failed /health run produced ``python3-munittestdiscover-s/...``
    and ``curl-shttp://localhost:8000/health``); any localhost port it
    addresses is a known service (FreeTalon API, Ollama); its
    interpreter/binary exists (retry #23: `./venv/bin/python` and bare
    `python` both died with exit 127 at execution time). All problems are
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
    prose_error = _prose_command_error(shell)
    if prose_error is not None:
        errors.append(prose_error)
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
    errors.extend(_bash_interpreter_errors(shell, repo_root))
    return errors


def _sed_scan_delimited(text: str, index: int) -> tuple[str | None, int]:
    """Scan a delimited pattern starting at text[index] (the delimiter).

    Returns (pattern, index just past the closing delimiter), or
    (None, len(text)) when unterminated. Backslash-escaped delimiters
    do not close the pattern.
    """
    delim = text[index]
    start = index + 1
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == delim:
            return text[start:cursor], cursor + 1
        cursor += 1
    return None, len(text)


def _sed_script_patterns(script: str) -> list[str]:
    """The search patterns of a sed script, or [] when it has none to judge.

    Handles `s<delim>pattern<delim>replacement<delim>[flags]` (any
    delimiter, escaped delimiters skipped) and `/pattern/<command>`
    addresses (delete, print, ...), including an address in front of an
    `s` command (`/addr/s/pat/rep/` yields both patterns). Line-number
    addresses (`10s/a/b/`, `1,5d`) carry no pattern of their own. A
    script whose pattern cannot be parsed (unterminated) yields [] —
    the command would fail at runtime, not silently, so there is
    nothing to judge here.
    """
    text = script.strip()
    patterns: list[str] = []
    if text.startswith("/"):
        pattern, after = _sed_scan_delimited(text, 0)
        if pattern is None:
            return []
        patterns.append(pattern)
        text = text[after:]
    else:
        match = re.match(r"\d+(?:,\d+)?", text)
        if match:
            text = text[match.end():]
    if text.startswith("s") and len(text) >= 2:
        pattern, _after = _sed_scan_delimited(text, 1)
        if pattern is not None:
            patterns.append(pattern)
    return patterns


def _sed_script_replacements(script: str) -> list[str]:
    """The replacement texts of a sed script's s/// commands.

    Same script shapes as _sed_script_patterns: an optional address
    (/pattern/ or a line range) in front of one s command, any
    delimiter. Returns [] when the script has no parseable
    substitution — the command would fail at runtime, not silently,
    so there is nothing to judge here.
    """
    text = script.strip()
    if text.startswith("/"):
        _pattern, after = _sed_scan_delimited(text, 0)
        if _pattern is None:
            return []
        text = text[after:]
    else:
        match = re.match(r"\d+(?:,\d+)?", text)
        if match:
            text = text[match.end():]
    if not (text.startswith("s") and len(text) >= 2):
        return []
    pattern, after = _sed_scan_delimited(text, 1)
    if pattern is None:
        return []
    replacement, _end = _sed_scan_delimited(text, after - 1)
    if replacement is None:
        return []
    results = [replacement]
    # The replacement itself may contain the delimiter inside a
    # quoted string (retry #25 attempt 3: "gpu_temp": "N/A" — the /
    # in "N/A" closes the strict scan early, truncating the payload
    # before the fabricated values). The text the model wrote still
    # carries the full payload, so also offer the tolerant remainder:
    # everything after the search pattern, minus a trailing delimiter
    # and flags.
    remainder = text[after:]
    trimmed = re.sub(re.escape(text[1]) + r"[a-zA-Z0-9]*$", "", remainder)
    if trimmed != replacement and trimmed.strip():
        results.append(trimmed)
    return results


def _sed_pattern_matches(pattern: str, text: str) -> bool:
    """True when sed would find the pattern: literal text or a regex match.

    sed's default regex is BRE, where ( ) { } + ? | are literal and
    \\( \\) are groups — the opposite of Python's re. Checking both the
    literal string and the Python-regex match covers both dialects: a
    pattern that matches either way is one sed would act on.
    """
    if pattern in text:
        return True
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


def _sed_in_place_edits(shell: str) -> list[tuple[str, str]]:
    """(script, file) pairs for `sed -i` in-place edits in a shell string.

    Tokenizes with shlex (heredoc bodies already stripped by the
    caller). A `sed` token at a command position (start of the shell, or
    after && / || / ; / | / sudo) starts an invocation; `-i`,
    `-iSUFFIX`, `--in-place[=SUFFIX]`, and bundles containing `i` mark
    an in-place edit; `-e SCRIPT` (attached or separate) collects a
    script; `-f` is skipped. The first non-flag token is the script
    when no `-e` was given; the rest are files, up to a command
    separator or a redirect. Returns [] when the shell does not
    tokenize or the edit is not in place. A `sed` inside a quoted
    argument (echoed, not run) is not a command position and is skipped;
    a `sed` run through xargs/find -exec is not seen either — both
    noted, not covered.
    """
    try:
        tokens = shlex.split(shell)
    except ValueError:
        return []
    edits: list[tuple[str, str]] = []
    index = 0
    at_command_start = True
    while index < len(tokens):
        token = tokens[index]
        if token in ("&&", "||", ";", "|"):
            at_command_start = True
            index += 1
            continue
        if token == "sudo":
            index += 1
            continue
        if not at_command_start or (token != "sed" and not token.endswith("/sed")):
            at_command_start = False
            index += 1
            continue
        index += 1
        in_place = False
        scripts: list[str] = []
        files: list[str] = []
        while index < len(tokens):
            arg = tokens[index]
            if arg in ("&&", "||", ";", "|") or re.fullmatch(
                r"\d*[<>]+", arg
            ):
                break
            if arg.startswith("-") and arg != "-":
                if arg == "-e":
                    if index + 1 < len(tokens):
                        scripts.append(tokens[index + 1])
                        index += 2
                    else:
                        index += 1
                    continue
                if arg.startswith("-e"):
                    scripts.append(arg[2:])
                    index += 1
                    continue
                if arg.startswith("-f"):
                    index += 1 if len(arg) > 2 else 2
                    continue
                if arg == "--in-place" or arg.startswith("--in-place="):
                    in_place = True
                    index += 1
                    continue
                if arg.startswith("--"):
                    index += 1
                    continue
                if "i" in arg[1:]:
                    in_place = True
                index += 1
                continue
            if not scripts:
                scripts.append(arg)
            else:
                files.append(arg)
            index += 1
        if in_place:
            for script in scripts:
                for target in files:
                    edits.append((script, target))
        at_command_start = True
    return edits


def _sed_noop_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> list[str]:
    """Reject sed -i edits whose search pattern never occurs in the file.

    Retry #18 attempt 3 step 1 ran `sed -i 's/def health(self):/.../'`
    on freetalon/api.py, but api.py has no `def health(self):` — sed -i
    with a non-matching pattern is a silent no-op: exit 0, file
    unchanged, and the plan proceeds as if the endpoint were added.
    General: when a bash node uses sed -i (in-place) against a file
    that exists on disk at plan time, the search pattern must occur in
    that file (as literal text or as a regex match); otherwise the edit
    is rejected by name with the grounded move. Scoped to existing
    files whose contents are readable at plan time — an edit to a file
    the plan itself creates is not judged, and a pattern that cannot be
    parsed is skipped (the command would fail at runtime, not
    silently). `perl -pi -e` and other in-place editors are not covered.
    """
    if modules is None:
        modules = _ModuleSourceCache()
    errors: list[str] = []
    root = index.root if index is not None else resolve_repo_root(repo_root)
    grounded = _grounded_move_text(index, goal_text)
    for node in nodes:
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        shell = _strip_heredoc_bodies(command)
        for script, target in _sed_in_place_edits(shell):
            path = _resolve_plan_target(target, root)
            text = modules.source(path)
            if text is None:
                continue
            patterns = _sed_script_patterns(script)
            if not patterns:
                continue
            missing = [p for p in patterns if not _sed_pattern_matches(p, text)]
            if not missing:
                continue
            errors.append(
                f"Planner node '{node.id}' runs sed -i on {target!r}, but "
                f"the search pattern {missing[0]!r} does not occur in the "
                f"file — sed -i with a non-matching pattern changes "
                f"nothing (exit 0, file unchanged). Check the file's real "
                f"contents and edit what is actually there.{grounded}"
            )
            break
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


def _criteria_errors(
    node: PlanNode, repo_root: str | Path | None = None
) -> list[str]:
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
        error = _usable_criterion_error(criterion, node, repo_root)
        if error is not None:
            errors.append(
                f"Planner node '{node.id}' has an unusable criterion: {error}."
            )
    return errors


def _command_errors(
    node: PlanNode, repo_root: str | Path | None = None
) -> list[str]:
    """Return every malformed-command error for a bash/shell_exec node."""
    if node.assigned_claw not in ("bash", "shell_exec"):
        return []
    return [
        f"Planner node '{node.id}' has an invalid bash command: {error}."
        for error in _validate_bash_command(node, repo_root)
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

    Every write counts, not only a command that IS a single literal
    write: retry #18 attempt 2 step 3 ran `curl ... | jq . >
    /tmp/health_response.json` inside a compound command, and the old
    check — which only recognized a command that is one echo/printf
    write or one heredoc — never saw the redirect. The written paths
    come from _node_written_paths (scaffold target, heredoc targets,
    and every redirect in the shell, heredoc bodies and quoted
    segments stripped first), so a redirect in any segment of a piped
    or &&-chained command is flagged. A `tee` write is not caught —
    there is no redirect to scan — noted, not fixed.
    """
    if not goal_text:
        return []
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return []
    modules = ", ".join(sorted({module for _, module in named}))
    errors: list[str] = []
    for node in nodes:
        for target in sorted(_node_written_paths(node)):
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
            # Unterminated heredoc: the shell reads to EOF (with a warning)
            # and writes what it got, so the rest of the command IS the
            # body. (Retry #15 attempt 2: the Flask heredoc had no closing
            # EOF line, so the old code returned no body and the framework
            # rule never saw the Flask app.)
            bodies.append("".join(lines[1:]))
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
    imports OR uses a web framework different from the serving
    module's. Use counts as well as imports: retry #18 attempt 2's
    heredoc wrote @app.route + jsonify with no import statement, so
    the import-only check never saw the Flask app.
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
                        f"Planner node '{node.id}' writes a {found} app — do "
                        f"NOT write a {found} app. {grounded} {route} is "
                        f"served by {module}, which is built on "
                        f"{serving_framework}. Do not rewrite it in a "
                        f"different web framework."
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


def _missing_directory_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
) -> list[str]:
    """Reject writes to a repo-tree directory that does not exist.

    Retry #18 attempts 2-3 both wrote their test file to
    freetalon/tests/test_api.py — a directory that does not exist (repo
    tests live at repo-root tests/), and nothing named it: the
    wrong-location rule only flags paths under the workspace, and the
    code-append parse check only names the file's syntax. A write whose
    parent directory is missing can never land: the heredoc fails at
    execution, or the file is created somewhere the suite never looks.
    General: any write node (scaffold file_path, bash heredoc or
    redirect) targeting a path under a repo tree (freetalon/ or tests/)
    whose parent directory does not exist on disk is rejected by name,
    with the correct location. Genuine generated files at the workspace
    root and paths that actually exist on disk are not flagged.

    Disk-independent: the target is resolved to its repo-relative form
    first (an absolute fixture path outside this checkout reads this
    checkout's copy), so the check answers the same on seeker7 and in
    CI. A directory the plan itself creates (mkdir -p) is not
    credited — noted, not covered.
    """
    root = index.root if index is not None else resolve_repo_root(repo_root)
    errors: list[str] = []
    for node in nodes:
        for target in sorted(_node_written_paths(node)):
            rel = _repo_relative_target(target, root)
            if rel is None:
                continue
            parent = root / Path(rel).parent
            if parent.exists():
                continue
            if rel.startswith("freetalon/tests/"):
                test_name = rel[len("freetalon/tests/"):]
                hint = (
                    f"Repo tests live at {root}/tests/, not freetalon/tests/ — "
                    f"write the test file to {root}/tests/{test_name}."
                )
            else:
                hint = (
                    f"Create the directory first, or write under an existing "
                    f"directory in the repo (repo root {root})."
                )
            errors.append(
                f"Planner node '{node.id}' writes to {target!r}, but its "
                f"directory {str(parent)!r} does not exist in the repo. {hint}"
            )
            break
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


# A comment that labels its own content as a placeholder to be replaced
# ("# This is a placeholder, replace with actual implementation.").
_PLACEHOLDER_MARKER_RE = re.compile(r"placeholder", re.IGNORECASE)
_PLACEHOLDER_REPLACE_RE = re.compile(
    r"replace|todo|implement|fill in|actual implementation", re.IGNORECASE
)


def _is_placeholder_stub(content: str) -> bool:
    """True when content is a placeholder marker plus a trivial code stub.

    Retry #23 replan output 3 step-1 scaffolded api.py with a
    "placeholder, replace with actual implementation" comment plus a
    two-line `def health(): return {...}` stub. The placeholder guard
    only fires on comment/docstring-only content, so a stub the model
    itself labels as a placeholder sailed through. A write that (a)
    carries a comment saying it is a placeholder to be replaced and
    (b) contains at most three lines of real code is the same defect:
    the author admits the implementation is not there. A real
    implementation that merely mentions "placeholder" in a comment is
    not flagged — it has more than three lines of code.
    """
    has_placeholder_word = False
    has_replace_word = False
    code_lines = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//")):
            if _PLACEHOLDER_MARKER_RE.search(stripped):
                has_placeholder_word = True
            if _PLACEHOLDER_REPLACE_RE.search(stripped):
                has_replace_word = True
            continue
        code_lines += 1
    return has_placeholder_word and has_replace_word and code_lines <= 3


def _placeholder_scaffold_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    goal_text: str | None = None,
) -> list[str]:
    """Reject scaffolds that overwrite an existing code file with placeholder content.

    String view of _placeholder_scaffold_error_records (kept for the
    callers that compose message lists); the records carry the codes
    (placeholder_scaffold vs placeholder_stub — the repair loop treats
    them differently, see _call_llm_with_repair).
    """
    return [
        record.message
        for record in _placeholder_scaffold_error_records(
            nodes, repo_root, index=index, goal_text=goal_text
        )
    ]


def _placeholder_scaffold_error_records(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    goal_text: str | None = None,
) -> list[PlanError]:
    """Reject scaffolds that overwrite an existing code file with placeholder content.

    A scaffold whose content is comments and/or a docstring — no code —
    must never overwrite a module that already exists: retry #14's v3
    replan scaffolded freetalon/api.py with "# Placeholder for health
    endpoint logic." (219 lines -> 1 line) and the loop never noticed,
    because scaffold nodes reported no exit code. The error names the
    file and the fact that the content is a placeholder. A scaffold
    creating a NEW file with a comment stub is still allowed — that is
    how scaffolding starts — so the rule fires only when the target
    already exists in the repo the plan targets. Existence is resolved
    repo-relatively (the _resolve_plan_target rule), never from a raw
    absolute path, so the same plan is judged identically on any
    machine. General: any code file, any route goal.
    When the goal names an existing route, the error also names the
    grounded move (extend the serving module, write real tests, curl
    the live API port), like the stand-in rejection does: the repair
    round is pointed TOWARD the real work, not just away from the
    placeholder.
    """
    records: list[PlanError] = []
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
        if node.assigned_claw not in ("scaffold", "file_writer"):
            continue
        target = (
            node.inputs.get("file_path")
            or node.inputs.get("path")
            or (node.outputs[0] if node.outputs else "")
        )
        content = node.inputs.get("content")
        if not isinstance(target, str) or not target:
            continue
        if not isinstance(content, str):
            continue
        if not is_code_path(target):
            continue
        stub = False
        if not is_placeholder_content(content):
            # Retry #23: a stub the model itself labels as a placeholder
            # (marker comment + a trivial function) is the same defect
            # as a comment-only stub — the implementation is not there.
            if not _is_placeholder_stub(content):
                continue
            stub = True
        # Existence is judged against the repo the plan targets, not
        # the raw path: an absolute target naming a repo file resolves
        # repo-relatively (the _resolve_plan_target rule), so the same
        # plan validates identically on any machine. The raw-path check
        # fired on seeker7 but not in CI for the retry-#23 output-3
        # fixture, whose absolute /home/seeker/... target does not
        # exist on a GitHub runner (hosted run 44140283101 rejected
        # output 3 with 2 errors; seeker7 rejected it with 3).
        if not _resolve_plan_target(target, resolve_repo_root(repo_root)).exists():
            continue
        if stub:
            records.append(
                PlanError(
                    code="placeholder_stub",
                    message=(
                        f"Planner node '{node.id}' scaffolds {target!r} with a "
                        "placeholder stub (a 'placeholder' comment saying to "
                        "replace it, plus only a trivial stub function — no real "
                        "implementation) — that would overwrite the existing "
                        "module with a stub instead of implementing it. Write "
                        "real code in the module (scaffold claw with real code, "
                        "or a bash heredoc) and verify it by running the tests "
                        f"and curling the live service.{grounded}"
                    ),
                    node_id=node.id,
                )
            )
            continue
        records.append(
            PlanError(
                code="placeholder_scaffold",
                message=(
                    f"Planner node '{node.id}' scaffolds {target!r} with placeholder "
                    "content only (comments and/or a docstring, no code) — that would "
                    "overwrite the existing module with a comment stub instead of "
                    "implementing it. Write real code in the module (scaffold claw "
                    "with real code, or a bash heredoc) and verify it by running the "
                    f"tests and curling the live service.{grounded}"
                ),
                node_id=node.id,
            )
        )
    return records


_ECHO_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "'": "'",
}

_ECHO_WRITE_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:echo|printf)\s+(?P<body>.*?)\s*(?P<op>>{1,2})\s*(?P<target>[^\s;&|]+)",
    re.DOTALL,
)

_TEST_RUN_RE = re.compile(
    r"python3?\s+(?:-m\s+unittest|\S*test_\w+\.py)|pytest\b"
)


def _interpret_echo_escapes(text: str) -> str:
    """Interpret backslash escapes the way the executor's shell echo does.

    Bash commands run under /bin/sh (dash), whose echo interprets \\n,
    \\t, \\\\, and friends. Retry #16 node-2 echoed 'def health():\\nreturn
    "OK"' and the file received two lines, not one literal backslash-n —
    so plan-time validation must decode the escapes the same way, or it
    validates a different file than execution produces.
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in _ECHO_ESCAPES:
            out.append(_ECHO_ESCAPES[text[index + 1]])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


_TEE_TARGET_RE = re.compile(r"\btee\s+(?P<append>-a\s+)?(?P<target>[^\s;|&<>]+)")
_HEREDOC_START_RE = re.compile(
    r"<<-?\s*['\"]?(?P<marker>[A-Za-z_][A-Za-z0-9_]*)['\"]?"
)


def _heredoc_body_for_marker(command: str, marker: str, start: int) -> str | None:
    """The body of the heredoc whose head ends at ``start``.

    Lines after the head line, up to the line that is exactly the
    marker; an unterminated heredoc's body is the rest of the command
    (the shell reads to EOF — the same rule _heredoc_bodies uses).
    None when there is no line after the head at all.
    """
    head_end = command.find("\n", start)
    if head_end == -1:
        return None
    lines = command[head_end + 1 :].splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.strip() == marker:
            return "".join(lines[:idx])
    return "".join(lines)


def _bash_code_writes(command: str) -> list[tuple[str, str, bool]]:
    """Every (target, content, is_append) code write in a bash command.

    Heredocs (body verbatim — a quoted marker means no escape
    interpretation) and echo/printf redirects (quoted segments only,
    with dash-echo escapes interpreted — the same rule _literal_write
    uses for a single write). Unquoted echo bodies are not extracted:
    they may hold variables or globs, not a literal the model wrote.
    Returns [] when the command writes nothing extractable.

    Tee writes are extracted too (A/B refactor, defect 3): in
    ``tee TARGET <<M`` and ``cat <<M | tee TARGET`` the heredoc body
    lands in TARGET with no ``cat > TARGET`` head for the pairing
    below to see, so the write used to be invisible to every consumer
    of this function — the payload parse check, the placeholder check,
    and the stand-in check all missed it (the /tmp rule noted the same
    blind spot for redirect scans). A tee match inside a cat-heredoc
    body is file content, not a command, and is ignored.
    """
    writes: list[tuple[str, str, bool]] = []
    bodies = _heredoc_bodies(command)
    for position, match in enumerate(_STRIP_HEREDOC_RE.finditer(command)):
        if position >= len(bodies):
            break
        head = match.group(0).split("<<", 1)[0]
        writes.append((match.group("target"), bodies[position], ">>" in head))
    body_spans = _heredoc_body_spans(command)
    for match in _TEE_TARGET_RE.finditer(command):
        if any(start <= match.start() < end for start, end in body_spans):
            continue  # 'tee' inside a heredoc body is file content
        target = match.group("target").strip("'\"")
        is_append = bool(match.group("append"))
        body: str | None = None
        tail = command[match.end() :]
        head_line_end = tail.find("\n")
        head_line = tail if head_line_end == -1 else tail[:head_line_end]
        start_match = _HEREDOC_START_RE.search(head_line)
        if start_match is not None:
            # Shape 1: tee TARGET <<M — tee itself carries the heredoc.
            body = _heredoc_body_for_marker(
                command, start_match.group("marker"), match.end() + start_match.end()
            )
        else:
            # Shape 2: <<M ... | tee TARGET — the heredoc head line
            # ends at this tee; the start is earlier on the same line.
            line_start = command.rfind("\n", 0, match.start()) + 1
            head_line = command[line_start : match.start()]
            start_match = _HEREDOC_START_RE.search(head_line)
            if start_match is not None:
                body = _heredoc_body_for_marker(
                    command,
                    start_match.group("marker"),
                    line_start + start_match.end(),
                )
        if body is not None:
            writes.append((target, body, is_append))
    shell = _strip_heredoc_bodies(command)
    for match in _ECHO_WRITE_RE.finditer(shell):
        segments = _quoted_segments(match.group("body"))
        if not segments:
            continue
        content = _interpret_echo_escapes(" ".join(segments))
        writes.append((match.group("target"), content, match.group("op") == ">>"))
    return writes


def _plan_measures_validity(nodes: list[PlanNode], path: str) -> bool:
    """True when some node in the plan measures that a file still works.

    A tests_pass criterion runs the suite; a test-suite run (unittest /
    pytest / a test file) measures the repo's code; a py_compile on the
    file itself measures that file. The escape hatch for writes the
    planner cannot parse at plan time (a non-Python code file).
    """
    for node in nodes:
        for criterion in node.criteria or []:
            if isinstance(criterion, dict) and criterion.get("type") == CRITERION_TESTS_PASS:
                return True
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str):
            continue
        shell = _strip_heredoc_bodies(command)
        if "py_compile" in shell and path in shell:
            return True
        if _TEST_RUN_RE.search(shell):
            return True
    return False


def _grounded_move_text(index: Any | None, goal_text: str | None) -> str:
    """The grounded-move sentence(s) for a route goal, or "".

    The same sentence _static_standin_errors appends: extend the serving
    module in place, write real tests, curl the live API port. Shared so
    every rejection that steers toward the grounded move names it the
    same way.
    """
    if index is None or not goal_text:
        return ""
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return ""
    return " " + " ".join(
        _grounded_move_sentence(
            route,
            str(index.root / module),
            index.api_port,
            index.route_handlers.get(route),
        )
        for route, module in named
    )


def _resolve_target_path(path: str, repo_root: str | Path | None) -> Path:
    """The on-disk path a plan target names (same resolution as _path_exists_on_disk)."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    rooted = resolve_repo_root(repo_root) / candidate
    return rooted if rooted.exists() else candidate


# First path components that mark a repo tree (code lives here).
_REPO_TREE_ROOTS = ("freetalon", "tests")


def _repo_relative_target(target: str, root: Path) -> str | None:
    """The repo-relative form of a write/edit target, or None.

    A target is a repo-tree path when its first component (after
    resolving to the repo) is freetalon/ or tests/. Relative targets
    are repo-relative already; absolute targets under the checkout root
    resolve against it; absolute targets elsewhere (a fixture's absolute
    seeker7 path read in CI) resolve from the first freetalon/ or
    tests/ component, so a check built on this answers the same on
    seeker7 and in CI. Anything else (workspace files, /tmp, /etc) is
    not a repo-tree path and returns None.
    """
    candidate = Path(target)
    if candidate.is_absolute():
        try:
            rel = str(candidate.relative_to(root))
        except ValueError:
            parts = candidate.parts
            for position, part in enumerate(parts):
                if part in _REPO_TREE_ROOTS:
                    rel = "/".join(parts[position:])
                    break
            else:
                return None
    else:
        rel = target
    first = rel.split("/", 1)[0]
    if first not in _REPO_TREE_ROOTS:
        return None
    return rel


def _resolve_plan_target(target: str, root: Path) -> Path:
    """The on-disk path a plan target names, resolved against the repo root.

    Absolute repo-tree paths resolve via their repo-relative form, so a
    fixture's absolute seeker7 path reads this checkout's copy in CI
    (disk-independent, like _repo_relative_target); other absolute paths
    read as-is; relative paths resolve against the repo root.
    """
    rel = _repo_relative_target(target, root)
    if rel is not None:
        return root / rel
    candidate = Path(target)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _syntax_error_detail(exc: SyntaxError) -> str:
    """One-line syntax-error detail: message + the error line.

    The parser's message may already end with the line where the
    construct starts ("expected an indented block after function
    definition on line 220") while exc.lineno is the line where the
    error was detected (221) — strip the trailing reference so the
    detail reads "… on line 221", not "… on line 220 on line 221".
    """
    msg = re.sub(r" on line \d+$", "", exc.msg)
    if exc.lineno is not None:
        return f"{msg} on line {exc.lineno}"
    return msg


def _heredoc_python_parse_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> list[str]:
    """String view of _heredoc_python_parse_error_records."""
    return [
        record.message
        for record in _heredoc_python_parse_error_records(
            nodes, repo_root, goal_text, index, modules=modules
        )
    ]


def _heredoc_python_parse_error_records(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> list[PlanError]:
    """Parse-check every Python payload a bash node writes, at plan time.

    A/B refactor (defect 3): this check used to live inside
    _code_append_errors as its .py branch — the payload parse was
    incidental to an append-corruption rule, and it only ever saw the
    writes _bash_code_writes could extract. It is now a first-class
    validator (code 'heredoc_parse'): every extracted bash write to a
    .py file — heredoc, echo/printf redirect, or tee — is simulated
    (append to the file on disk, or the content alone for a new file)
    and parsed with ast.parse, so the __main__-guard indentation class
    from retry #33 output 3 (unittest.main() at column 0 under its
    if __name__ guard — the file the node claimed as its test suite
    could not even be imported) is named at validation, not discovered
    when the test node runs. Echo escapes are interpreted the way dash
    echo does, so the simulated file is the file execution produces.

    Skipped when the written literal is already a hardcoded answer: the
    stand-in rule owns that defect (retry #12). Skipped when the
    existing file itself no longer parses — it is already broken, and
    this rule cannot judge the write. A scaffold write is NOT
    parse-checked here — that is _scaffold_parse_errors' job.
    """
    if modules is None:
        modules = _ModuleSourceCache()
    records: list[PlanError] = []
    grounded = _grounded_move_text(index, goal_text)
    for node in nodes:
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        for target, content, is_append in _bash_code_writes(command):
            if not is_code_path(target):
                continue
            if _literal_is_answer(content):
                continue
            if Path(target).suffix.lower() != ".py":
                continue
            exists = _path_exists_on_disk(target, repo_root)
            if exists:
                existing = modules.source(
                    _resolve_target_path(target, repo_root)
                )
                if existing is None:
                    continue
                try:
                    ast.parse(existing)
                except SyntaxError:
                    continue
                if is_append:
                    simulated = existing
                    if not simulated.endswith("\n"):
                        simulated += "\n"
                    simulated += content
                    if not simulated.endswith("\n"):
                        simulated += "\n"
                else:
                    simulated = content
                try:
                    ast.parse(simulated)
                except SyntaxError as exc:
                    records.append(
                        PlanError(
                            code="heredoc_parse",
                            message=(
                                f"Planner node '{node.id}' writes code into "
                                f"{target!r} that would break the module: after "
                                f"this write the file no longer parses as Python "
                                f"({_syntax_error_detail(exc)}). A bash append "
                                "or heredoc must not corrupt an existing code "
                                "file — write the edit with real code (scaffold "
                                "claw, or a bash heredoc) and verify the file "
                                "still parses (python -m py_compile "
                                f"{target}) and the tests pass.{grounded}"
                            ),
                            node_id=node.id,
                        )
                    )
            else:
                try:
                    ast.parse(content)
                except SyntaxError as exc:
                    records.append(
                        PlanError(
                            code="heredoc_parse",
                            message=(
                                f"Planner node '{node.id}' writes {target!r} "
                                "with code that does not parse as Python "
                                f"({_syntax_error_detail(exc)}) — write valid "
                                f"code.{grounded}"
                            ),
                            node_id=node.id,
                        )
                    )
    return records


def _code_append_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
) -> list[str]:
    """Reject bash writes into existing non-Python code files nobody verifies.

    Retry #16: node-1 and node-2 echoed two lines into freetalon/api.py
    (a comment, then 'def health():\\nreturn "OK"') with only exit_code
    criteria — both measured PASS while the module broke (IndentationError
    on line 221; the suite fell to 608 tests, 18 failures + 2 errors).
    The placeholder guard covers scaffold only, and the stand-in rule
    fires only on JSON literals, so an echo-append of broken code was
    unguarded. Two mechanisms, by language:

    - Python (.py): the payload parse check — now its own validator,
      _heredoc_python_parse_errors (A/B refactor, defect 3).
    - Other code files (.js, .sh, ...): no parser is available, so the
      write is rejected unless some node in the plan measures the file
      still working (a tests_pass criterion, a test-suite run, or a
      py_compile on the file). That branch is what remains here.

    Skipped when the written literal is already a hardcoded answer: the
    stand-in rule owns that defect (retry #12: a JSON literal echoed
    over api.py).
    """
    errors: list[str] = []
    grounded = _grounded_move_text(index, goal_text)
    for node in nodes:
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        for target, content, is_append in _bash_code_writes(command):
            if not is_code_path(target):
                continue
            if _literal_is_answer(content):
                continue
            if Path(target).suffix.lower() == ".py":
                continue  # _heredoc_python_parse_errors owns .py payloads
            exists = _path_exists_on_disk(target, repo_root)
            if exists and not _plan_measures_validity(nodes, target):
                errors.append(
                    f"Planner node '{node.id}' writes into {target!r}, an "
                    "existing code file, but no node in the plan measures "
                    "that the file still works — add a node that runs the "
                    f"tests or otherwise verifies {target!r} still parses "
                    f"and works.{grounded}"
                )
    return errors


def _scaffold_parse_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
) -> list[str]:
    """Reject scaffold writes of unparseable Python to a code file.

    Retry #23: v1's accepted plan scaffolded freetalon/api.py with a
    whole new BaseHTTPRequestHandler module whose content does not
    parse (IndentationError — `def handle_health(self):` followed by
    a top-level `try:` with no body), and the loop executed it: the
    module broke, and the framework guard then disarmed itself on the
    clobbered file. The code-append parse check covers bash heredoc
    writes only — a scaffold write was never parse-checked (retry #13
    attempt 4's unparseable Flask scaffold was a known unobserved
    hole). A scaffold overwrites the whole file, so its content must
    parse as Python on its own — for an existing file and for a new
    one. Skipped when the content is already a hardcoded answer: the
    stand-in rule owns that defect. Non-Python code files are not
    parse-checked (no parser available), like the bash rule.
    """
    errors: list[str] = []
    grounded = _grounded_move_text(index, goal_text)
    for node in nodes:
        if (node.assigned_claw or "") not in ("scaffold", "file_writer"):
            continue
        target = (
            node.inputs.get("file_path")
            or node.inputs.get("path")
            or (node.outputs[0] if node.outputs else "")
        )
        content = node.inputs.get("content")
        if not isinstance(target, str) or not target:
            continue
        if not isinstance(content, str):
            continue
        if not is_code_path(target):
            continue
        if Path(target).suffix.lower() != ".py":
            continue
        if _literal_is_answer(content):
            continue
        try:
            ast.parse(content)
        except SyntaxError as exc:
            errors.append(
                f"Planner node '{node.id}' scaffolds {target!r} with code "
                "that does not parse as Python "
                f"({_syntax_error_detail(exc)}) — a scaffold overwrites "
                "the whole file, so unparseable content would break the "
                "module. Write valid code (extend the module in place "
                "with real code) and verify it parses (python -m "
                f"py_compile {target}) and the tests pass.{grounded}"
            )
    return errors


# Elision markers: a comment (or bare statement) that stands in for
# code the model did not write. The pattern mirrors the planner A/B
# replay harness's soundness rule verbatim
# (tools/replay-planner-corpus.py), so the planner and the harness
# judge the same content the same way.
_ELISION_RE = re.compile(
    r"^\s*(?:#+|//+|--)?\s*\.\.\.\s*(?:existing|remaining|rest of|unchanged|original|same as|other)\b"
    r"|^\s*(?:#+|//+|--)\s*\.\.\.\s*$"
    r"|^\s*\.\.\.\s*(?:#.*)?$",
    re.MULTILINE,
)


def _elision_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> list[str]:
    """Reject scaffold writes that elide an existing module's code.

    A/B refactor (defect 1): retry #33's fourth output — the first
    accepted plan in 33 retries — scaffolded freetalon/api.py with a
    five-line fragment whose body was two elision comments
    ('# ... existing code ...', '# ... remaining code ...') around a
    single /health branch. It parsed; it was not a placeholder (it
    carried a real def and a real branch); and the overwrite guard
    exempted it as fragment-scale. It executed and clobbered api.py
    (219 lines -> 5) before the run failed elsewhere. The offline
    corpus replay then showed the same shape accepted in retries
    #28/#29 and the llama3 sanity run: every plan the old planner
    accepted was an unsound fragment.

    An elision marker is the defect stated plainly — the content
    itself says the code is not there — so it is a named rejection
    independent of definition counts. The retention guard cannot see
    this defect class at all: a scaffold can keep every definition
    NAME while eliding the bodies, so the two rules are
    complementary. Fires only when the target is an existing Python
    module (a new file has nothing to elide). Content another rule
    owns is skipped, with the guard's skip list: unparseable content
    (the parse check), placeholder content and stubs (the placeholder
    check), hardcoded literal answers (the stand-in check).
    """
    if modules is None:
        modules = _ModuleSourceCache()
    errors: list[str] = []
    grounded = _grounded_move_text(index, goal_text)
    root = index.root if index is not None else resolve_repo_root(repo_root)
    for node in nodes:
        if (node.assigned_claw or "") not in ("scaffold", "file_writer"):
            continue
        target = (
            node.inputs.get("file_path")
            or node.inputs.get("path")
            or (node.outputs[0] if node.outputs else "")
        )
        content = node.inputs.get("content")
        if not isinstance(target, str) or not target:
            continue
        if not isinstance(content, str):
            continue
        if not is_code_path(target):
            continue
        if Path(target).suffix.lower() != ".py":
            continue
        if _literal_is_answer(content):
            continue
        if is_placeholder_content(content) or _is_placeholder_stub(content):
            continue
        try:
            ast.parse(content)
        except SyntaxError:
            continue  # _scaffold_parse_errors owns unparseable content
        if modules.source(_resolve_plan_target(target, root)) is None:
            continue  # a new file: nothing exists to elide
        if not _ELISION_RE.search(content):
            continue
        errors.append(
            f"Planner node '{node.id}' scaffolds {target!r} with content "
            "that elides the existing module — it carries elision markers "
            "(comments standing in for code the plan does not write, like "
            "'... existing code ...') in place of the module's real code. "
            "A scaffold overwrites the whole file, so the elided code "
            "would be deleted, and a file_exists criterion cannot see the "
            "loss. Scaffold the FULL updated module (every existing "
            "definition with its real body, plus the new code) or extend "
            "the module in place with an anchored edit (anchor + position "
            "+ only the new code), which preserves the untouched code by "
            f"construction.{grounded}"
        )
    return errors


def _scaffold_overwrite_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> list[str]:
    """Reject scaffold writes that delete most of an existing module.

    Retry #26: the accepted plan's node-1 scaffold replaced
    freetalon/api.py wholesale (219 lines -> 37) with a small handler
    module and passed on a file_exists criterion — the real
    HiveAPIServer (start/stop, the Handler class defined inside
    start, every route branch), the auth check, and
    _ThreadingHTTPServer were silently deleted, and only the next
    node's exit 127 revealed the loss; api.py had to be restored from
    git. The parse check (retry #23) rejects content that does not
    parse, but a parseable fragment that omits most of the module was
    unguarded.

    General: a scaffold overwrites the whole file, so when it targets
    an EXISTING Python module, every definition it omits is a
    deletion. Compare the qualified def/class names (every nesting
    depth — a same-named replacement class that drops the original's
    methods is still a gutting; the top-level names of api.py are
    only two) of the current file and the scaffold content: when the
    existing module has at least four definitions and the scaffold
    drops at least half of them (and at least three), the write is
    rejected by name, listing what would be lost. A scaffold that
    keeps the module's definitions (the grounded shape: the full
    updated module) passes untouched; new files are exempt (nothing
    exists to delete); tiny modules are exempt (too little structure
    to judge a rewrite); an existing file that itself does not parse
    is skipped (it is already broken, and this rule cannot judge the
    write — the same stance as _code_append_errors). Content another
    rule owns is skipped: unparseable content (the parse check),
    placeholder content and placeholder stubs (the placeholder
    check), hardcoded literal answers (the stand-in check), and
    elision-carrying content (the elision check, A/B refactor).

    A/B refactor (defect 1): the replacement no longer needs to be
    module-scale before its omissions count as deletions. The old
    floor (the scaffold itself needed >= 4 definitions) exempted
    exactly the fragments that did the damage: the corpus replay
    showed retries #28/#29 scaffolding api.py with a three-line
    branch fragment (zero definitions, dropping all 12), retry #33's
    elision fragment (one definition, dropping all 12), and the
    llama3 run replacing tests/test_api.py with two definitions
    (dropping 27) — all accepted, all unsound. A full-content
    scaffold now faces the same retention judgment a materialized
    anchored edit faces by construction: what it omits, it deletes,
    whatever its own size. The judgment is unchanged otherwise —
    same module floor, same drop thresholds.
    """
    if modules is None:
        modules = _ModuleSourceCache()
    errors: list[str] = []
    grounded = _grounded_move_text(index, goal_text)
    root = index.root if index is not None else resolve_repo_root(repo_root)
    for node in nodes:
        if (node.assigned_claw or "") not in ("scaffold", "file_writer"):
            continue
        target = (
            node.inputs.get("file_path")
            or node.inputs.get("path")
            or (node.outputs[0] if node.outputs else "")
        )
        content = node.inputs.get("content")
        if not isinstance(target, str) or not target:
            continue
        if not isinstance(content, str):
            continue
        if not is_code_path(target):
            continue
        if Path(target).suffix.lower() != ".py":
            continue
        if _literal_is_answer(content):
            continue
        if is_placeholder_content(content) or _is_placeholder_stub(content):
            continue
        if _ELISION_RE.search(content):
            continue  # _elision_errors owns elided content
        try:
            ast.parse(content)
        except SyntaxError:
            continue  # _scaffold_parse_errors owns unparseable content
        path = _resolve_plan_target(target, root)
        existing = modules.source(path)
        if existing is None:
            continue  # a new file: nothing exists to delete
        existing_names = modules.def_names(path)
        if len(existing_names) < 4:
            continue  # tiny module: too little structure to judge
        scaffold_names = qualified_def_names(content)
        kept = set(scaffold_names)
        dropped = [name for name in existing_names if name not in kept]
        if len(dropped) < 3 or len(dropped) * 2 < len(existing_names):
            continue
        shown = ", ".join(dropped[:8])
        if len(dropped) > 8:
            shown += f", ... ({len(dropped) - 8} more)"
        errors.append(
            f"Planner node '{node.id}' scaffolds {target!r} with content "
            f"that drops {len(dropped)} of the existing module's "
            f"{len(existing_names)} definitions ({shown}) — a scaffold "
            "overwrites the whole file, so every definition it omits is "
            "deleted, and a file_exists criterion cannot see the loss. "
            "Scaffold the FULL updated module (every existing definition, "
            "plus the new code) or extend the module in place with a bash "
            f"heredoc instead.{grounded}"
        )
    return errors


def _plan_runs_path_as_test(nodes: list[PlanNode], target: str) -> bool:
    """True when some node runs the target path as a test.

    A bash command that names the target and matches the test-run pattern
    (python3 <test file>, pytest, unittest) executes the file — so a
    comment-only file passed off as a test is a fake the loop would
    measure PASS (a comment-only file exits 0). Heredoc bodies are
    stripped first: a path inside file content being written is data,
    not a file being run.
    """
    for node in nodes:
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        shell = _strip_heredoc_bodies(command)
        if target in shell and _TEST_RUN_RE.search(shell):
            return True
    return False


def _placeholder_bash_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    goal_text: str | None = None,
) -> list[str]:
    """Reject bash heredoc/echo writes of placeholder content that fake a deliverable.

    Retry #20: the accepted v1 plan wrote ``#TODO: replace this comment
    with the real code for /health`` into health.py and test_health.py via
    ``cat>`` heredocs, claimed file_exists on both, and ran the comment-only
    test file — all three nodes measured PASS (a comment-only file exists
    and exits 0) while nothing was implemented. The scaffold placeholder
    guard covers scaffold writes to existing code files only, and the
    code-append check only rejects writes that break parsing — a comment
    parses fine — so the heredoc fake sailed through.

    The rule fires when a bash write (heredoc or echo) of comment-only /
    TODO-only content to a code file is claimed by a file_exists criterion
    anywhere in the plan, or run as a test by some node. A placeholder
    heredoc nobody claims and nobody runs is not a fake deliverable, so
    it is left alone; a heredoc with real code passes untouched. The
    error names the placeholder approach (so the repair prompt forbids
    it) and the grounded move for route goals.
    """
    errors: list[str] = []
    grounded = _grounded_move_text(index, goal_text)
    file_exists_claims = {
        criterion.get("path")
        for node in nodes
        for criterion in node.criteria or []
        if isinstance(criterion, dict)
        and criterion.get("type") == CRITERION_FILE_EXISTS
        and isinstance(criterion.get("path"), str)
    }
    for node in nodes:
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        for target, content, _is_append in _bash_code_writes(command):
            if not is_code_path(target):
                continue
            stub = False
            if not is_placeholder_content(content):
                # Retry #23: the scaffold stub rule applies to heredocs
                # too — a marker comment plus a trivial stub is the same
                # fake deliverable as a comment-only heredoc.
                if not _is_placeholder_stub(content):
                    continue
                stub = True
            claimed = target in file_exists_claims
            run_as_test = _plan_runs_path_as_test(nodes, target)
            if not claimed and not run_as_test:
                continue
            why = (
                "claimed by a file_exists criterion"
                if claimed
                else "run as a test"
            )
            if stub:
                errors.append(
                    f"Planner node '{node.id}' writes {target!r} with a "
                    "placeholder stub (a 'placeholder' comment saying to "
                    "replace it, plus only a trivial stub function — no "
                    f"real implementation) via a bash heredoc/echo, {why} "
                    "— the file has only a stub, so the plan fakes the "
                    "deliverable. Write real code (a heredoc with the "
                    "real implementation, or the scaffold claw with real "
                    "code) and verify it by running the tests and curling "
                    f"the live service.{grounded}"
                )
                continue
            errors.append(
                f"Planner node '{node.id}' writes {target!r} with placeholder "
                "content only (comments and/or a docstring, no code) via a "
                f"bash heredoc/echo, {why} — the file exists but implements "
                "nothing, so the plan fakes the deliverable. Write real code "
                "(a heredoc with the real implementation, or the scaffold "
                "claw with real code) and verify it by running the tests "
                f"and curling the live service.{grounded}"
            )
    return errors


def _command_error_records(
    node: PlanNode, repo_root: str | Path | None = None
) -> list[PlanError]:
    """_command_errors as records: prose commands carry their own code.

    The forbidden-approach steering names prose commands separately
    from other command defects, so the prose error (recomputed with
    the same function _validate_bash_command uses) is tagged
    'prose_command'; every other command error is 'command'.
    """
    messages = _command_errors(node, repo_root)
    if not messages:
        return []
    prose_message: str | None = None
    command = node.inputs.get("command") or node.inputs.get("cmd")
    if isinstance(command, str) and command.strip():
        prose = _prose_command_error(_strip_heredoc_bodies(command))
        if prose is not None:
            prose_message = (
                f"Planner node '{node.id}' has an invalid bash command: {prose}."
            )
    return [
        PlanError(
            code="prose_command" if message == prose_message else "command",
            message=message,
            node_id=node.id,
        )
        for message in messages
    ]


def _collect_plan_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None = None,
    goal_text: str | None = None,
    workspace: str | None = None,
    index: Any | None = None,
) -> tuple[list[str], bool]:
    """String view of _collect_plan_error_records (kept for callers
    that compose message lists); the records carry the error codes."""
    records, cheap = _collect_plan_error_records(
        nodes, repo_root, goal_text, workspace, index=index
    )
    return [record.message for record in records], cheap


def _collect_plan_error_records(
    nodes: list[PlanNode],
    repo_root: str | Path | None = None,
    goal_text: str | None = None,
    workspace: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> tuple[list[PlanError], bool]:
    """Collect EVERY plan validation error as structured records.

    Each validator is an independent function registered here with a
    stable error code; this aggregator is the only place they are
    composed. Adding a validator is one registration line plus the
    function — previously it also meant extending the steering
    blocks' message-marker lists and hoping no other rejection's
    wording contained the new marker.

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

    ``modules`` is the per-parse cache of existing-module sources, so
    the validators that read module files share one read + parse per
    file per parse call; a fresh cache is created when none is given.

    Returns (records, cheap): ``cheap`` is True when every error is a
    stand-in / code-overwrite fake (retry #12) or a placeholder scaffold
    (retry #17) — a rejection the repair loop may re-prompt for free,
    without spending the repair budget.
    """
    if modules is None:
        modules = _ModuleSourceCache()
    records: list[PlanError] = []
    needs_index = any(
        node.assigned_claw in ("scaffold", "file_writer") for node in nodes
    ) or goal_text is not None
    if index is None and needs_index:
        index = build_repo_index(repo_root)
    for node in nodes:
        error = _claw_error(node)
        if error is not None:
            records.append(
                PlanError(code="unknown_claw", message=error, node_id=node.id)
            )
        records.extend(
            _error_records("criteria", _criteria_errors(node, repo_root))
        )
        records.extend(_command_error_records(node, repo_root))
        if index is not None:
            error = _grounding_error(node, index)
            if error is not None:
                records.append(
                    PlanError(
                        code="duplicate_code", message=error, node_id=node.id
                    )
                )
    records.extend(
        _error_records(
            "unwritten_file", _unwritten_file_errors(nodes, repo_root)
        )
    )
    placeholder_records = _placeholder_scaffold_error_records(
        nodes, repo_root, index=index, goal_text=goal_text
    )
    if not placeholder_records:
        # Retry #20: a plan whose only placeholder defect is a bash
        # heredoc (no scaffold placeholder) is the same failed approach,
        # named once. When a scaffold placeholder already names it, the
        # heredoc placeholder is the same defect in the same plan, not a
        # new one — listing it too only inflates the count (retry #17
        # attempt-4 stays 1 error) while the cheap free re-prompt lets
        # the model iterate one placeholder defect at a time.
        placeholder_records = _error_records(
            "placeholder_bash",
            _placeholder_bash_errors(
                nodes, repo_root, index=index, goal_text=goal_text
            ),
        )
    records.extend(placeholder_records)
    records.extend(
        _heredoc_python_parse_error_records(
            nodes, repo_root, goal_text, index, modules=modules
        )
    )
    records.extend(
        _error_records(
            "code_append", _code_append_errors(nodes, repo_root, goal_text, index)
        )
    )
    records.extend(
        _error_records(
            "scaffold_parse",
            _scaffold_parse_errors(nodes, repo_root, goal_text, index),
        )
    )
    records.extend(
        _error_records(
            "scaffold_overwrite",
            _scaffold_overwrite_errors(
                nodes, repo_root, goal_text, index, modules=modules
            ),
        )
    )
    records.extend(
        _error_records(
            "elision",
            _elision_errors(nodes, repo_root, goal_text, index, modules=modules),
        )
    )
    records.extend(
        _error_records(
            "sed_noop",
            _sed_noop_errors(nodes, repo_root, goal_text, index, modules=modules),
        )
    )
    standin_records = _error_records(
        "static_standin",
        _static_standin_errors(nodes, index=index, goal_text=goal_text),
    )
    records.extend(standin_records)
    if index is not None:
        records.extend(
            _error_records(
                "route_engagement",
                _route_engagement_errors(nodes, index, goal_text),
            )
        )
        records.extend(
            _error_records(
                "tmp_write", _tmp_write_errors(nodes, index, goal_text)
            )
        )
        records.extend(
            _error_records(
                "framework", _framework_errors(nodes, index, goal_text)
            )
        )
        records.extend(
            _error_records(
                "wrong_location",
                _wrong_location_errors(nodes, index, workspace),
            )
        )
    records.extend(
        _error_records(
            "missing_directory",
            _missing_directory_errors(nodes, repo_root, index),
        )
    )
    # Cheap rejection (retry #12): the only errors are stand-in /
    # code-overwrite fakes, so re-prompting costs the model nothing to
    # fix and must not spend the repair budget. A rejection whose only
    # errors are placeholder scaffolds is cheap too (retry #17): a
    # comment stub costs the model nothing to replace with real code —
    # and so is a rejection whose only errors are placeholder bash
    # heredocs (retry #20). Any other error mixed in makes it a real
    # rejection.
    cheap = (
        bool(records)
        and len(standin_records) + len(placeholder_records) == len(records)
    )
    return records, cheap


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
        # No closing marker: the shell reads to EOF and writes what it got
        # (retry #15 attempt 2), so the body is everything after the
        # heredoc line either way.
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


# A dict-literal binding of a key to a constant: "key": value with a
# literal value (bool, None, number, or quoted string). A value that is
# a name, a call, or any other expression is NOT matched — a computed
# value is the real implementation; only a constant is a stand-in.
_DICT_BINDING_RE = re.compile(
    r"[\"'](?P<key>[A-Za-z_][A-Za-z0-9_]*)[\"']\s*:\s*"
    r"(?P<value>true|false|null|True|False|None|-?\d+(?:\.\d+)?"
    r"|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
)
# Flat {...} spans — the observed fake shape. A nested dict's inner
# spans are found on their own; the outer span is skipped.
_DICT_SPAN_RE = re.compile(r"\{[^{}]*\}")


def _is_measured_key(key: str) -> bool:
    """True for keys that name a measured value, not static metadata.

    The /health contract reports whether Ollama is reachable and the
    GPU temperature — values that exist only by probing the service /
    reading the sensor. A status or a service name is static metadata
    the code may legitimately state as a constant; reachability and
    temperature cannot be stated, only measured.
    """
    lowered = key.lower()
    return (
        "reachable" in lowered
        or "temperature" in lowered
        or lowered == "temp"
        or lowered.endswith("_temp")
    )


def _hardcoded_measured_bindings(text: str) -> list[str]:
    """Constant bindings of measured keys inside dict literals in text.

    Scans flat {...} spans for key: constant pairs whose key names a
    measured value (see _is_measured_key) and returns them in the
    canonical '"key": value' spelling, so the rejection can name the
    exact fabricated values. A dict whose measured values are computed
    (a variable, a call) yields nothing — only constants are
    stand-ins. This is a heuristic for the observed fake shape
    (retry #25 attempt 3), not a Python parser.
    """
    bindings: list[str] = []
    for span in _DICT_SPAN_RE.findall(text):
        for match in _DICT_BINDING_RE.finditer(span):
            if not _is_measured_key(match.group("key")):
                continue
            rendered = f'"{match.group("key")}": {match.group("value")}'
            if rendered not in bindings:
                bindings.append(rendered)
    return bindings


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
            f"heredoc) and run them; start the API server from the repo "
            f"root with ./.venv/bin/python -m freetalon.cli start (stop a "
            f"stale server first with ./.venv/bin/python -m freetalon.cli "
            f"stop) and wait for it to answer; verify live with "
            f"curl http://localhost:{api_port}{route}."
        )
    return (
        f"The grounded move is: extend {module} (scaffold claw, or a bash "
        f"heredoc) to implement {route}; write the tests (scaffold or "
        f"heredoc) and run them; start the API server from the repo root "
        f"with ./.venv/bin/python -m freetalon.cli start (stop a stale "
        f"server first with ./.venv/bin/python -m freetalon.cli stop) and "
        f"wait for it to answer; verify live with "
        f"curl http://localhost:{api_port}{route}."
    )


def _bash_payload_standin_errors(
    node: PlanNode, command: str, grounded: str
) -> list[str]:
    """Stand-in shape 3: measured values hardcoded inside a bash payload.

    Retry #25 attempt 3 edited api.py with a sed -i whose replacement
    text inserted a status dict binding "ollama_reachable": True and
    "gpu_temp": "N/A" — the endpoint's measured values, asserted as
    constants inside a bash string. Shapes 1-2 never see it: the
    command is not a literal write, and the scaffold checks do not
    scan bash payloads. The payloads that write Python files — heredoc
    bodies and echo/printf contents (_bash_code_writes), and sed -i
    replacement texts (_sed_script_replacements) — are scanned for
    dict literals binding measured keys to constants
    (_hardcoded_measured_bindings). A payload that computes the values
    (a probe call, a variable) is untouched, and a payload that is
    itself a JSON literal is skipped: shapes 1-2 own that defect.
    """
    payloads: list[tuple[str, str]] = []
    for target, content, _is_append in _bash_code_writes(command):
        if Path(target).suffix.lower() != ".py":
            continue
        if _literal_is_answer(content):
            continue
        payloads.append((target, content))
    shell = _strip_heredoc_bodies(command)
    for script, target in _sed_in_place_edits(shell):
        if Path(target).suffix.lower() != ".py":
            continue
        for replacement in _sed_script_replacements(script):
            payloads.append((target, replacement))
    errors: list[str] = []
    seen: set[str] = set()
    for target, payload in payloads:
        if target in seen:
            continue
        bindings = _hardcoded_measured_bindings(payload)
        if not bindings:
            continue
        seen.add(target)
        errors.append(
            f"Planner node '{node.id}' writes a hardcoded answer into "
            f"{target!r}: the payload binds measured fields to "
            f"constants ({', '.join(bindings)}) instead of measuring "
            f"them — whether a service is reachable and what a sensor "
            f"reads exist only by probing (call the service, run "
            f"nvidia-smi), so a constant is a fabricated measurement. "
            f"Write real code that measures the values it reports "
            f"(scaffold claw, or a bash heredoc).{grounded}"
        )
    return errors


# File suffixes that mark a path as a code file. A JSON literal is never
# valid code in any of these languages, so writing one over such a path
# destroys an implementation instead of producing it (retry #12: the
# model echoed a JSON answer over freetalon/api.py). Data files are
# exempt on purpose: writing a JSON config to a new file is legitimate
# file production, so only code paths are guarded.
def _static_standin_errors(
    nodes: list[PlanNode],
    index: Any | None = None,
    goal_text: str | None = None,
) -> list[str]:
    """Flag nodes that manufacture their own evidence.

    Three shapes, all general:
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
    3. A bash payload that writes a Python file — a heredoc body, an
       echo/printf body, or a sed -i replacement text — containing a
       dict literal that binds measured fields (reachability,
       temperature) to constants: the surrounding code is real, but
       the values it reports are fabricated, because those fields
       exist only by probing. See _bash_payload_standin_errors
       (retry #25 attempt 3).
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
            if is_code_path(target):
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
        if claw in ("bash", "shell_exec"):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str):
                errors.extend(
                    _bash_payload_standin_errors(node, command, grounded)
                )
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


def _existing_module_definitions(path: str | Path) -> list[str]:
    """Qualified def names of an existing parseable module, or [].

    The grounding counterpart of _scaffold_overwrite_errors: the guard
    rejects a module-scale scaffold that drops at least half (and at
    least three) of an existing module's definitions, but until retry
    #28 nothing ever TOLD the model what those definitions are — the
    repo context lists only top-level names, so the nested definitions
    (for freetalon/api.py, 10 of its 12) were invisible until the
    guard named the first eight dropped ones in a rejection. Returns
    [] when there is nothing to preserve: the file does not exist (a
    new file), does not parse (already broken — the checks that own
    that state judge writes to it), or has fewer than four qualified
    definitions (the guard's own floor: too little structure to judge
    a rewrite, so no preservation contract is stated for it either).
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    names = qualified_def_names(source)
    if len(names) < 4:
        return []
    return names

# Retry #30: the definition NAMES were not enough — retry #29's model
# had all 12 of api.py's names and still guessed the layout (the
# nested Handler class hoisted to module level twice; once written
# as the dotted class name HiveAPIServer.start.Handler, a syntax
# error). The grounding now also carries the module's STRUCTURE.
# Budget (measured 2026-11-17 with the Gemma 3 tokenizer from
# models/tokenizer.json; audit in freetalon-workspace/logs/
# skeleton-grounding-fix-2026-11-17.log): num_ctx is 12288; the
# initial call for the /health goal is 4,644 tokens; a successful
# full-module plan costs ~2,642 tokens to emit. Full source for
# api.py is 2,194 tokens — the worst realistic round (a repair
# carrying the source in the shape plus the skeleton in the
# extension block, then a full-module reply) totals ~10,850,
# leaving ~1,400 spare. So: full source at or under 10,000 chars
# (~2,350 tokens at api.py's density), the signature skeleton
# above that, and the skeleton is hard-capped at 8,000 chars with
# an explicit truncation marker. The qualified-definition list
# itself is never truncated — only the illustrative structure is
# capped.
_STRUCTURE_FULL_SOURCE_MAX_CHARS = 10_000
_STRUCTURE_SKELETON_MAX_CHARS = 8_000


def _module_skeleton(source: str) -> str:
    """A module's signature skeleton: real lines, bodies elided.

    Every def/class header is reproduced verbatim from the source —
    decorators included, multi-line signatures whole — so the
    indentation and nesting shown are the file's own. A body holding
    no nested definition collapses to a single ``...`` line at the
    body's indentation; a body holding nested definitions keeps its
    statements' first lines around them, so a nested class appears
    exactly where the file defines it. Module-level imports are
    kept verbatim and other module-level statements are reduced to
    their first line. Returns "" when the source does not parse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines = source.splitlines()

    def _segment(start: int, end: int) -> list[str]:
        return lines[start - 1 : end]

    def _first_line(node: ast.stmt) -> list[str]:
        seg = _segment(node.lineno, node.end_lineno or node.lineno)
        if not seg:
            return []
        return [seg[0] + (" ..." if len(seg) > 1 else "")]

    def _emit_def(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    ) -> list[str]:
        start = node.lineno
        for decorator in node.decorator_list:
            start = min(start, decorator.lineno)
        if node.body and node.body[0].lineno > node.lineno:
            header_end = node.body[0].lineno - 1
        else:
            header_end = node.end_lineno or node.lineno
        header = _segment(start, header_end)
        while header and not header[-1].strip():
            header.pop()
        out = list(header)
        if not node.body or node.body[0].lineno == node.lineno:
            return out  # one-liner: the header IS the whole definition
        out.extend(_emit_body(node.body))
        return out

    def _emit_body(body: list[ast.stmt]) -> list[str]:
        has_defs = any(
            isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            for stmt in body
        )
        if not has_defs:
            if body:
                return [" " * body[0].col_offset + "..."]
            return []
        out: list[str] = []
        for stmt in body:
            if isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                out.extend(_emit_def(stmt))
            else:
                out.extend(_first_line(stmt))
        return out

    out: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            out.extend(
                _segment(stmt.lineno, stmt.end_lineno or stmt.lineno)
            )
        elif isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if out:
                out.append("")
            out.extend(_emit_def(stmt))
        else:
            out.extend(_first_line(stmt))
    return "\n".join(out)


def _cap_skeleton(skeleton: str) -> str:
    """Bound a skeleton for the prompt, marking any truncation.

    Only the illustrative structure is capped; the qualified
    definition list the grounding also carries is never truncated.
    """
    if len(skeleton) <= _STRUCTURE_SKELETON_MAX_CHARS:
        return skeleton
    cut = skeleton.rfind("\n", 0, _STRUCTURE_SKELETON_MAX_CHARS)
    if cut <= 0:
        cut = _STRUCTURE_SKELETON_MAX_CHARS
    return (
        skeleton[:cut]
        + "\n# ... (structure truncated here — the definition list "
        "above is complete)"
    )


def _grounded_module_source(path: str | Path) -> str | None:
    """Source of an existing parseable module at the guard's floor.

    The shared gate for the structure grounding: None when the file
    does not exist, does not parse, or has fewer than four qualified
    definitions — the same conditions under which
    _existing_module_definitions returns [].
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if len(qualified_def_names(source)) < 4:
        return None
    return source


def _existing_module_structure(path: str | Path) -> tuple[str, str] | None:
    """(kind, text) structure grounding for an existing module, or None.

    kind is "source" — the module's full current source — when it
    fits the prompt budget (_STRUCTURE_FULL_SOURCE_MAX_CHARS), else
    "skeleton" — the capped signature skeleton. None under the same
    conditions as _existing_module_definitions.
    """
    source = _grounded_module_source(path)
    if source is None:
        return None
    if len(source) <= _STRUCTURE_FULL_SOURCE_MAX_CHARS:
        return ("source", source)
    return ("skeleton", _cap_skeleton(_module_skeleton(source)))


def _existing_module_skeleton(path: str | Path) -> str:
    """The capped signature skeleton of an existing module, or "".

    The extension-steering block always carries the skeleton form,
    even when the shape block carried the full source: the repair
    decision needs the layout beside the dropped list, and the
    skeleton is the bounded form of it. "" under the same
    conditions as _existing_module_definitions.
    """
    source = _grounded_module_source(path)
    if source is None:
        return ""
    return _cap_skeleton(_module_skeleton(source))


def _route_goal_shape(
    goal_text: str | None,
    repo_root: str | Path | None = None,
    *,
    delta: bool = True,
) -> str:
    """A concrete node shape for a goal naming an existing API route.

    When the goal names a route, the plan has a known shape — edit the
    serving module, write tests, start the server, call the live route
    on the API port — so the prompt hands the model a NEW structure to
    produce instead of leaving it to discover the shape by trial and
    error inside a 3-attempt budget (retry #5 burned attempts 1-2 on
    the fake before the engagement error fired) or to re-serialize the
    brief's failed steps as JSON (retry #4 replan 1 echoed the brief's
    delta steps verbatim). ``delta`` says whether the plan is a re-plan
    delta ("the delta plan must follow this shape") or a first attempt
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
            f"heredoc) and runs them; node 3 starts the API server — from "
            f"the repo root run cd {index.root} && ./.venv/bin/python -m "
            f"freetalon.cli start (it daemonizes itself; stop a stale "
            f"server first with ./.venv/bin/python -m freetalon.cli stop) "
            f"and wait until curl -sf http://localhost:{index.api_port}"
            f"{route} answers; node 4 verifies live with "
            f"curl http://localhost:{index.api_port}{route}. The live curl "
            f"cannot pass unless a node starts the server first — nothing "
            f"serves port {index.api_port} until then. Do NOT probe "
            f"another service's {route} path, and do NOT echo a JSON answer. "
            f"Do NOT re-serialize these instructions as JSON — they are "
            f"guidance, not output; your output is a NEW 'nodes' array."
        )
    # Retry #28: attempts 2 and 3 were parse-clean scaffolds that
    # REPLACED the serving module with a new smaller one (dropping 11
    # and 8 of its 12 definitions) and were rejected by the overwrite
    # guard — the model was never told what the module contains. The
    # repo context names only top-level definitions, so the nested
    # ones were invisible until a rejection listed them. Name every
    # qualified definition of each serving module here, with the
    # preservation contract the guard enforces, so the first attempt
    # already knows the full-module shape it must produce.
    # Retry #31: the block now teaches the anchored edit as the
    # PREFERRED shape, with the preservation contract kept as the
    # fallback for a plain full-content scaffold: retry #30's model,
    # told to produce the full-module shape, emitted a comment stub
    # instead — the module now reaches the scaffold by construction
    # (parse_planner_response materializes the edit against the
    # current file), so this block only has to teach the model to
    # name an anchor and write the one definition.
    seen_modules: set[str] = set()
    for _route, module in named:
        if module in seen_modules:
            continue
        seen_modules.add(module)
        module_path = str(index.root / module)
        definitions = _existing_module_definitions(module_path)
        if not definitions:
            continue
        anchor_hint = ""
        module_source = _grounded_module_source(module_path)
        if module_source is not None:
            for route_name, route_module in named:
                if route_module != module:
                    continue
                anchor_name = _route_branch_anchor(module_source, route_name)
                if anchor_name is not None:
                    anchor_hint = (
                        f" For this goal the natural anchor is "
                        f"{anchor_name!r}: 'replace' it with its "
                        f"extended version."
                    )
                    break
        lines.append(
            f"EXISTING MODULE — {module_path} already exists with "
            f"{len(definitions)} definitions: "
            + ", ".join(definitions)
            + ". Node 1 EXTENDS this module — it does not write a new "
            "one. PREFER an anchored edit for node 1: its scaffold "
            "inputs carry \"anchor\" (ONE definition name from the "
            "list above — the definition the change belongs to), "
            "\"position\" (\"replace\" substitutes that definition; "
            "\"after\"/\"before\" insert a new sibling next to it), "
            "and \"content\" holding ONLY the new code — for "
            "\"replace\", the anchored definition's complete new "
            "version, def line included — written at the exact "
            "indentation that definition has in the file. The system "
            "applies the edit to the current file, so every other "
            "definition is preserved by construction — do NOT retype "
            "the whole module."
            + anchor_hint
            + " A plain full-content scaffold is still accepted, but "
            "then its content must be the full updated module: every "
            "definition listed above, kept in the same place with the "
            "same nesting (a name like A.B.C is defined inside B, "
            "which is defined inside A — keep it there), plus the new "
            "code. A scaffold that omits a listed definition deletes "
            "it and is rejected."
        )
        # Retry #30: names alone left the model guessing the layout
        # (retry #29 hoisted the nested Handler class to module
        # level twice and once wrote it as a dotted class name).
        # Show the structure itself — the full source when it fits
        # the prompt budget, else the signature skeleton — with
        # the file's real indentation, so the nesting is copied,
        # not inferred. Retry #31: the structure's job is now to let
        # the model choose the edit's anchor and copy the exact
        # indentation the new code must fit — the edit itself is
        # materialized against this file, so nothing in it needs to
        # be retyped.
        structure = _existing_module_structure(module_path)
        if structure is not None:
            kind, text = structure
            if kind == "source":
                lines.append(
                    "The module's full current source is below. Use "
                    "it to choose the edit's anchor and to copy the "
                    "exact indentation and surrounding code the new "
                    "code must fit — you do NOT retype it: the "
                    "system starts from this file and applies your "
                    "anchored edit to it."
                    f"\n```python\n{text.rstrip()}\n```"
                )
            else:
                lines.append(
                    "The module's structure is below, with its real "
                    "indentation (a line containing only ... stands "
                    "for a definition's existing body — when your "
                    "anchored edit replaces that definition, write "
                    "its real new body; never copy a ... "
                    "line as a body). Definitions nested in the "
                    "file appear nested here — anchor names use "
                    "exactly this nesting (A.B.C is defined inside "
                    "B, which is defined inside A)."
                    f"\n```python\n{text}\n```"
                )
    # Retry #25: attempt 1's scaffold plan was good code and failed
    # ONLY on two interpreter strings the model composed from prose —
    # a bare `python` in a step-2 criterion command and
    # `./venv/bin/python` (missing the dot) in the step-3 start
    # command — even though the route line above and every rejection
    # named ./.venv/bin/python. The model copies shapes reliably and
    # composes strings unreliably, so the exact commands also ride in
    # the shape as quoted, copyable slots: the test run as a bash
    # command, the same string as an exit_code criterion's command,
    # and the server start as a bash command.
    lines.append(
        "COMMANDS — copy these character-for-character; do not retype "
        "the interpreter path. The repo venv interpreter is "
        "./.venv/bin/python (with the dot): bare `python` does not "
        "exist on Ubuntu, and ./venv/bin/python is not a real path — "
        "both are rejected. Run the tests with the bash command "
        f"\"cd {index.root} && ./.venv/bin/python -m unittest "
        "discover -s tests\" — and when an exit_code criterion carries "
        "a \"command\", use that same string as the criterion's "
        "command. Start the server with the bash command "
        f"\"cd {index.root} && ./.venv/bin/python -m freetalon.cli "
        "start\"."
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


# Error codes for defects in a plan's CONTENT (the code itself), as
# opposed to defects in its command/criterion strings. Retry #25:
# attempt 1's scaffold plan failed only on two interpreter strings
# (bare `python` in a criterion command, ./venv/bin/python in a bash
# command) — no content defect — and attempts 2/3 then abandoned the
# scaffold shape for sed one-liners. When a scaffold plan's rejection
# carries no content-defect code, the repair prompt says the shape
# was right, so the model fixes the named strings instead of
# switching mechanism.
#
# A/B refactor: this set replaces _CONTENT_DEFECT_MARKERS, a list of
# message substrings. The substring list caused both historical
# steering contradictions: retry #28 (the overwrite guard's phrase
# was not yet a marker, so the shape block blessed a wholesale
# replacement) and retry #33 (the materialization stems were not
# markers either, so it blessed a dedented delta the same way —
# measured: 489 chars of "none of its code was rejected" on a
# rejection whose only defect was the code). A validator's code is
# assigned where the error is created and cannot drift when a
# message is reworded; a new validator joins this set with one entry
# when its defect is in the code, and no other rejection's wording
# can accidentally match it.
_CONTENT_DEFECT_CODES = frozenset(
    {
        "scaffold_parse",  # scaffold content does not parse
        "heredoc_parse",  # a bash-written Python payload does not parse
        "placeholder_scaffold",
        "placeholder_stub",
        "placeholder_bash",
        "placeholder_delta",  # materialized edit whose delta is placeholder-only
        "static_standin",  # hardcoded literal answers
        "framework",  # Flask / FastAPI over a stdlib module
        "scaffold_overwrite",  # retention guard: drops existing definitions
        "elision",  # elided module content
        "anchored_edit",  # materialization defects (bad anchor/indent/shape)
        "scope_widening",  # repair left the rejected plan's scope (cycle 2)
    }
)

# Codes whose rejection is answered with the grounded skeleton in the
# repair prompt (retry #21): a placeholder reply needs the concrete
# shape again, not just the prohibition. Exactly the three cases the
# old text test ("placeholder content only" in the message) selected:
# the materialization placeholder delta, the comment-only scaffold,
# and the placeholder bash heredoc — NOT the placeholder stub (its
# message never carried the phrase, and its repair round is not
# answered with the skeleton today).
_PLACEHOLDER_SKELETON_CODES = frozenset(
    {"placeholder_delta", "placeholder_scaffold", "placeholder_bash"}
)


def _previous_plan_used_scaffold(response_text: str) -> bool:
    """True when the failed planner output edited a file via the scaffold claw."""
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return False
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(
        isinstance(node, dict)
        and node.get("assigned_claw") in ("scaffold", "file_writer")
        for node in nodes
    )


def _shape_steering_block(
    response_text: str, codes: Collection[str]
) -> str:
    """Steer a repair back to a scaffold shape that was one fix away.

    Returns "" unless the previous output used the scaffold claw AND
    its rejection carries no content-defect code (see
    _CONTENT_DEFECT_CODES): only then was the plan's shape right,
    with its defects confined to command/criterion strings.
    Retry #25: without this, the repair after the interpreter-only
    rejection abandoned the scaffold for sed -i one-liners that
    failed shell quoting and pattern grounding. The decision keys
    off the rejection's structured codes (A/B refactor) — the
    message-substring version of this test produced the retry-#28
    and retry-#33 contradictions.
    """
    if not _previous_plan_used_scaffold(response_text):
        return ""
    if _CONTENT_DEFECT_CODES & set(codes):
        return ""
    return (
        "\nYour previous plan's SHAPE was right: it made the code "
        "edit with the scaffold claw, and none of its code was "
        "rejected — the only defects are the command/criterion "
        "strings named above. Keep the same plan shape and the same "
        "code; fix ONLY the named strings, copying the corrected "
        "commands character-for-character (the interpreter is "
        "./.venv/bin/python). Do NOT abandon the scaffold edit for a "
        "different mechanism (such as a sed -i text substitution) to "
        "work around a command-string defect.\n"
    )


def _extension_steering_block(
    response_text: str,
    codes: Collection[str],
    repo_root: str | Path | None = None,
) -> str:
    """The concrete in-place pattern for a repair after an overwrite rejection.

    Retry #28: attempts 2 and 3 scaffolded parse-clean REPLACEMENTS of
    the serving module (dropping 11 and 8 of its 12 definitions). The
    repair prompt they produced carried only the guard's error text —
    which truncates the dropped list at eight names — and the
    shape-steering block actively blessed the replacement (now fixed:
    the guard's phrase is a content-defect marker, so that block stays
    silent). This block recomputes the failed scaffold against the
    module on disk with the guard's own thresholds and hands the
    repair round the FULL dropped list (no truncation), the module's
    FULL definition list, and the concrete pattern to copy. Retry
    #30: the block also appends the module's signature skeleton
    (real indentation, bodies elided) — the names alone left the
    nesting to guesswork. Retry #31: the pattern is now the anchored
    edit (anchor + position + only the new code, materialized
    against the current file by parse_planner_response, so the
    dropped definitions return by construction) — the full updated
    module remains only as the fallback the guard accepts. Returns
    "" unless the rejection carries the guard's code
    ('scaffold_overwrite' — A/B refactor: the trigger used to be the
    guard's distinctive phrase in the message text) and a
    scaffold node in the failed output recomputes to a guard-scale
    drop.
    """
    if "scaffold_overwrite" not in codes:
        return ""
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return ""
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        return ""
    root = resolve_repo_root(repo_root)
    blocks: list[str] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        if raw.get("assigned_claw") not in ("scaffold", "file_writer"):
            continue
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict):
            continue
        outputs = raw.get("outputs")
        target = (
            inputs.get("file_path")
            or inputs.get("path")
            or (outputs[0] if isinstance(outputs, list) and outputs else None)
        )
        content = inputs.get("content")
        if not isinstance(target, str) or not target:
            continue
        if not isinstance(content, str):
            continue
        if Path(target).suffix.lower() != ".py":
            continue
        try:
            ast.parse(content)
        except SyntaxError:
            continue  # the parse check owns unparseable content
        existing_names = _existing_module_definitions(
            _resolve_plan_target(target, root)
        )
        if not existing_names:
            continue
        scaffold_names = qualified_def_names(content)
        # A/B refactor: no fragment-scale floor here either — the
        # recompute mirrors the guard's own thresholds, and the
        # guard no longer exempts small scaffolds (defect 1).
        kept = set(scaffold_names)
        dropped = [name for name in existing_names if name not in kept]
        if len(dropped) < 3 or len(dropped) * 2 < len(existing_names):
            continue  # below the guard's own firing thresholds
        # Retry #30: the dropped names map onto the layout only if
        # the layout is in the block — append the module's skeleton
        # (always the skeleton form: bounded, and the repair needs
        # the nesting beside the dropped list, not the bodies).
        skeleton = _existing_module_skeleton(
            _resolve_plan_target(target, root)
        )
        structure_part = ""
        if skeleton:
            structure_part = (
                " The module's structure, with its real indentation "
                "(a line containing only ... stands for a "
                "definition's existing body), is:\n"
                f"```python\n{skeleton}\n```"
            )
        blocks.append(
            f"\nEXISTING-MODULE EXTENSION — node '{raw.get('id')}' "
            f"scaffolds {target}, which already exists. Your scaffold "
            f"dropped these {len(dropped)} existing definitions: "
            + ", ".join(dropped)
            + f". The module's full definition list is: "
            + ", ".join(existing_names)
            + ". Rewrite the node as an ANCHORED EDIT of the module "
            "instead of a whole-file scaffold: inputs \"anchor\" (the "
            "ONE definition from that list the change belongs to — "
            "for a route branch, the handler method that serves the "
            "route), \"position\": \"replace\", and \"content\" "
            "holding ONLY that definition's complete new code, at "
            "the indentation it has in the file. The system applies "
            "the edit to the current file, so the dropped definitions "
            "return by construction — do NOT retype the whole module. "
            "If you write a full scaffold again instead, it must be "
            "the FULL updated module: every definition in that list, "
            "kept in the same place with the same nesting (a name "
            "like A.B.C is defined inside B, which is defined inside "
            "A — it stays there; do not move a definition to the top "
            "level, rename it, or drop it), plus your new code added "
            "— a scaffold that omits an existing definition is "
            "rejected again."
            + structure_part
            + "\n"
        )
    return "".join(blocks)


def _materialization_steering_block(
    response_text: str,
    codes: Collection[str],
    repo_root: str | Path | None = None,
) -> str:
    """The concrete edit-form pattern for a repair after a materialization rejection.

    Retry #33: after a materialization rejection (retry #32's
    output 3: the anchored edit's delta dedented to column 0), the
    repair prompt carried only the rejection text — whose tail is
    the generic _EDIT_SHAPE_EXAMPLE — while the concrete steering
    that fires after an overwrite-guard rejection
    (_extension_steering_block: full lists, the skeleton, the
    named pattern) stayed silent, because its trigger is the
    guard's phrase alone; and the shape-steering block actively
    misfired (its markers now include the materialization stems,
    so it stays silent too). What was left was a prohibition-
    shaped prompt, and the repair round abandoned the edit form
    for a full-content Flask scaffold — the outputs 4 of retries
    #30, #31, and #32 all end there. This block mirrors the
    extension block for the edit-form failure: it recomputes the
    failed edit against the module on disk and hands the repair
    round the module's FULL definition list, the anchor's own
    indentation, the module's skeleton (real indentation, bodies
    elided), and the keep-the-edit-form pattern. Returns ""
    unless the rejection is a materialization rejection (it
    carries the 'anchored_edit' code — A/B refactor: the trigger
    used to be the materialization stems in the message text) and an
    edit-form node in the failed output targets a module that
    grounds (existing, parseable, at the guard's four-definition
    floor). The placeholder-delta rejection is excluded: it is
    cheap, and its repair round already answers with the grounded
    skeleton.
    """
    if "placeholder_delta" in codes:
        return ""
    if "anchored_edit" not in codes:
        return ""
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return ""
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        return ""
    root = resolve_repo_root(repo_root)
    blocks: list[str] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        if raw.get("assigned_claw") not in ("scaffold", "file_writer"):
            continue
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if "anchor" not in inputs and "position" not in inputs:
            continue
        outputs = raw.get("outputs")
        target = (
            inputs.get("file_path")
            or inputs.get("path")
            or (outputs[0] if isinstance(outputs, list) and outputs else None)
        )
        if not isinstance(target, str) or not target:
            continue
        if Path(target).suffix.lower() != ".py":
            continue
        resolved = _resolve_plan_target(target, root)
        existing_names = _existing_module_definitions(resolved)
        if not existing_names:
            continue
        anchor = inputs.get("anchor")
        indent_part = ""
        if isinstance(anchor, str) and anchor.strip():
            try:
                source_text = resolved.read_text(encoding="utf-8")
            except OSError:
                source_text = ""
            for span_name, _start, _end, span_indent in _definition_spans(
                source_text
            ):
                if span_name == anchor:
                    indent_part = (
                        f" — {span_indent} spaces for {anchor!r}, "
                        "the anchor you used"
                    )
                    break
        skeleton = _existing_module_skeleton(resolved)
        structure_part = ""
        if skeleton:
            structure_part = (
                " The module's structure, with its real indentation "
                "(a line containing only ... stands for a "
                "definition's existing body), is:\n"
                f"```python\n{skeleton}\n```"
            )
        blocks.append(
            f"\nANCHORED-EDIT REPAIR — node '{raw.get('id')}' "
            f"anchors an edit on {target}, which already exists, "
            "and the edit was rejected for the defect named above. "
            "KEEP the anchored-edit form and fix only that defect "
            "in the edit's delta — do NOT replace the edit with a "
            "whole-file scaffold, and do NOT rewrite the module "
            "in a different framework. The edit's inputs are "
            "\"anchor\" (the ONE definition from the module's "
            "list the change belongs to), \"position\", and "
            "\"content\" holding ONLY the new code for that "
            "definition, at the indentation it has in the file"
            f"{indent_part}. The module's full definition list "
            f"is: {', '.join(existing_names)}. The system applies "
            "the edit to the current file, so every other "
            "definition is preserved by construction."
            + structure_part
            + "\n"
        )
    return "".join(blocks)


def _grounded_move_for_goal(goal_text: str | None, repo_root: str | Path | None) -> str:
    """The grounded-move sentence(s) for a route goal, or "".

    Used when the repair loop gives up on empty plans (retry #16): the
    escalation names the valid move instead of failing silently on an
    empty reply. Builds the index from repo_root (the checkout holding
    this package when None).
    """
    if not goal_text:
        return ""
    try:
        index = build_repo_index(repo_root)
    except OSError:
        return ""
    return _grounded_move_text(index, goal_text)


# The response fields the grounded skeleton works with for a route,
# as (field name, isinstance types for the step-2 assertion or
# None): for /health the goal's three reported items (service
# status, Ollama reachable, GPU temperature) under the established
# HEREDOC_TEST fixture's key names; any other route has none, so
# step-2 keeps only its generic ok/status assertions and step-1's
# sketch keeps the branch as it exists. ONE table for both steps
# (retry #32): step-1's delta sketch extends the branch with
# exactly the fields step-2's test asserts, so the two can never
# drift apart.
_SKELETON_ROUTE_FIELDS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "/health": (
        ("service_status", None),
        ("ollama_reachable", "bool"),
        ("gpu_temperature", "(int, float)"),
    ),
}


def _route_branch_if(source: str, route: str) -> ast.If | None:
    """The ast.If dispatching on ``route`` in a module, or None.

    The branch shape _route_branch_anchor and repo_index._route_branch
    both read: a Compare whose single Eq comparator is the route
    literal. Assumes the source parses (callers check); returns the
    first match in walk order, like _route_branch_anchor.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == route
        ):
            return node
    return None


def _anchored_delta_sketch(
    module_source: str, anchor: str, route: str
) -> str | None:
    """The grounded skeleton's step-1 delta: real code, never prose.

    Retry #32: the delta blank used to be instruction prose ("#
    Write the COMPLETE new version of ... here ... Do NOT write a
    TODO comment") with the branch riding as commented lines, and
    retry #31's model copied it into the edit's content verbatim —
    the second consecutive echo of whatever prose occupied an
    example's content slot (retry #30 echoed the old step-1 the
    same way). The blank is now the anchored definition's REAL
    current source — signature and full body sliced verbatim from
    the module at the anchor's span (decorators included), at its
    true file indentation — with the route branch extended in code
    form: when the branch's body is a single ``return <call>(...,
    {dict})`` and the route has derived response fields
    (_SKELETON_ROUTE_FIELDS), the branch is rebuilt as

        payload = {<the branch's real dict>}
        payload["<field>"] = ...      (per field not already a key)
        return <the real call, its dict argument now ``payload``>

    at the branch body's real indentation, every expression taken
    from the source segments, so nothing is invented but the field
    names the shape code already derives. The ``...`` values are
    the sketch's open slots — the steering text AROUND the slot
    (the skeleton's step-1 guidance) tells the model to replace
    them with real computations; no instruction text lives inside
    the content slot anymore. Any other branch shape, no derived
    fields, or every field already present leaves the definition
    source unchanged: still real code at true indentation. Returns
    None when the anchor has no span in the source.
    """
    span: tuple[int, int] | None = None
    for span_name, span_start, span_end, _span_indent in _definition_spans(
        module_source
    ):
        if span_name == anchor:
            span = (span_start, span_end)
            break
    if span is None:
        return None
    start, end = span
    lines = module_source.splitlines()
    definition_lines = lines[start - 1 : end]
    fields = [
        field for field, _types in _SKELETON_ROUTE_FIELDS.get(route, ())
    ]
    if not fields:
        return "\n".join(definition_lines)
    branch = _route_branch_if(module_source, route)
    if branch is None or not (start <= branch.lineno <= end):
        return "\n".join(definition_lines)
    if len(branch.body) != 1 or not isinstance(branch.body[0], ast.Return):
        return "\n".join(definition_lines)
    ret = branch.body[0]
    if not isinstance(ret.value, ast.Call):
        return "\n".join(definition_lines)
    dict_node = next(
        (arg for arg in ret.value.args if isinstance(arg, ast.Dict)), None
    )
    if dict_node is None:
        return "\n".join(definition_lines)
    existing_keys = {
        key.value
        for key in dict_node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    new_fields = [field for field in fields if field not in existing_keys]
    if not new_fields:
        return "\n".join(definition_lines)
    dict_segment = ast.get_source_segment(module_source, dict_node)
    call_segment = ast.get_source_segment(module_source, ret.value)
    if dict_segment is None or call_segment is None:
        return "\n".join(definition_lines)
    definition_text = "\n".join(definition_lines)
    payload_name = next(
        (
            candidate
            for candidate in ("payload", "response_payload", "route_payload")
            if not re.search(rf"\b{candidate}\b", definition_text)
        ),
        None,
    )
    if payload_name is None:
        return "\n".join(definition_lines)
    body_indent = " " * ret.col_offset
    rebuilt = [f"{body_indent}{payload_name} = {dict_segment}"]
    rebuilt += [
        f'{body_indent}{payload_name}["{field}"] = ...'
        for field in new_fields
    ]
    rebuilt.append(
        f"{body_indent}return "
        + call_segment.replace(dict_segment, payload_name, 1)
    )
    definition_lines[ret.lineno - start : ret.end_lineno - start + 1] = rebuilt
    return "\n".join(definition_lines)


def _grounded_skeleton(
    goal_text: str | None,
    repo_root: str | Path | None = None,
    workspace: str | None = None,
    *,
    after: str = "empty",
) -> str:
    """A concrete fill-in-the-blanks plan skeleton for a route goal, or "".

    Retry #16: after an informative failure the re-planner returned
    {"nodes": []} five times in a row — the ROUTE GOAL shape in prose was
    not enough to get it writing again. A skeleton lowers the generation
    burden to filling in the code: the node ids, claws, file paths, and
    criteria are already there, mirroring the grounded plan shape (edit
    the serving module, write + run the tests, start the server, curl
    the live route).
    The skeleton is a SHAPE to complete, not a plan to copy verbatim:
    every "# TODO" line must be replaced with real code, and a TODO
    comment copied into a scaffold is rejected by the placeholder guard
    (it would overwrite the existing module with a comment stub), so a
    verbatim copy cannot slip through as a plan. Step-1 shows the
    existing branch as a commented sketch — the route literal and the
    response fields named, read from the real module — so the model has
    real code to extend instead of a blank to fill (retry #17: the
    model followed the skeleton shape but filled it with placeholder
    comments). A verbatim copy of the sketch is still placeholder-only
    (all comment lines), so the guard rejects it, cheaply. Retry #30:
    the model copied the sketch verbatim anyway — completing the old
    step-1 meant retyping the whole module under a "write the FULL
    module" instruction, and the copied sketch WAS its answer — so
    step-1 is now an anchored edit (retry #31): the blank is the ONE
    handler definition the route lives in, named by its qualified
    anchor, with the sketch riding as the delta's comment blank; a
    verbatim copy is a comment-only delta, which materialization
    rejects as placeholder content. Retry #31: the model copied
    THAT blank too — it used the anchored edit correctly (the named
    anchor, position "replace") but filled the content with the
    blank's instruction prose, its re-wrap dropping the '#'
    markers from the prose lines so the placeholder checks missed
    it and materialization rejected it at the indentation gate,
    naming the wrong defect at full price. Retry #32: the delta is
    no longer prose at all — it is the anchored definition's real
    current code with the route branch extended in code form
    (_anchored_delta_sketch), and the instructions live in the
    step-1 guidance AROUND the slot. A verbatim copy now copies
    plausible code at the right indentation, which materialization
    judges on content like any other delta.
    Step-2's test is runnable real code, not a stub (retry #21): the old
    skeleton showed a "# TODO: replace this comment with the real test
    code" heredoc, and the model copied the TODO verbatim into its plan
    — twice in one call. The test starts the API server in-process on an
    ephemeral port (the pattern in tests/test_api.py), so it runs with
    plain `python3 <file>` and no live server, and it asserts the fields
    the goal asks the endpoint to report, so it fails on the unextended
    endpoint and passes once the endpoint is extended. ``after`` names
    what the previous reply was ("empty" or "placeholder") so the
    lead-in never tells the model its reply was empty when it was a
    placeholder stub, or vice versa.
    Returns "" when the goal names no existing route.
    """
    if not goal_text:
        return ""
    try:
        index = build_repo_index(repo_root)
    except OSError:
        return ""
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return ""
    route, module = named[0]
    module_path = str(index.root / module)
    handler = index.route_handlers.get(route)
    if handler is not None:
        edit = (
            f"Extend {module_path} in place — add a {route} branch to {handler}"
        )
    else:
        edit = f"Extend {module_path} in place to implement {route}"
    test_path = (
        f"{str(workspace).rstrip('/')}/test_health.py"
        if workspace
        else str(index.root / "tests" / "test_health.py")
    )
    branch = index.route_branches.get(route)
    anchor: str | None = None
    if branch is not None:
        # The existing branch, as a commented sketch the model extends:
        # the route literal and the response fields are named, so the
        # model has real code to complete instead of a blank TODO — and
        # a verbatim copy is still placeholder-only (all comment lines),
        # so the guard rejects it instead of letting it through.
        sketch = "\n".join(f"#     {line}" for line in branch.splitlines())
        # Retry #31: step-1 is an anchored edit, not a full-module
        # scaffold. Retry #30's model copied this skeleton's old
        # step-1 verbatim — the commented sketch under a "Write the
        # FULL module" instruction — as its scaffold content (a
        # comment stub, rejected as a placeholder): completing the
        # old blank meant retyping a 219-line module. The blank is
        # now the ONE definition the change belongs to — the route
        # handler's qualified name, derived from the branch's
        # position in the real module. Retry #32: the blank itself
        # is no longer instruction prose with the sketch riding as
        # comments — retry #31's model copied that prose into the
        # edit's content — it is the definition's real code with
        # the branch extended in code form
        # (_anchored_delta_sketch); the instructions live in the
        # step-1 guidance below, around the slot, never in it.
        # When no anchor is derivable (module unreadable or
        # unparseable, branch outside any definition), step-1
        # keeps the legacy full-content form below.
        anchor_indent = 0
        delta_sketch: str | None = None
        try:
            module_source: str | None = Path(module_path).read_text(
                encoding="utf-8"
            )
        except OSError:
            module_source = None
        if module_source is not None:
            anchor = _route_branch_anchor(module_source, route)
            if anchor is not None:
                for span_name, _span_start, _span_end, span_indent in (
                    _definition_spans(module_source)
                ):
                    if span_name == anchor:
                        anchor_indent = span_indent
                        break
                delta_sketch = _anchored_delta_sketch(
                    module_source, anchor, route
                )
                if delta_sketch is None:
                    anchor = None
        if anchor is not None and delta_sketch is not None:
            content = delta_sketch
        else:
            anchor = None
            content = (
                f"# Extend {module_path} in place — add the fields the goal asks "
                f"for to the existing {route} branch in {handler}:\n"
                f"{sketch}\n"
                "# Write the FULL module with the extended branch — the scaffold "
                "replaces the whole file, so a fragment alone is not a plan.\n"
                "# Do NOT write a TODO comment — a comment-only scaffold is rejected."
            )
    else:
        content = f"# TODO: replace this comment with the real code — {edit.lower()}"
    if anchor is not None:
        step1_inputs: dict[str, Any] = {
            "file_path": module_path,
            "anchor": anchor,
            "position": "replace",
            "content": content,
        }
        step1_guidance = (
            "Step-1 is an anchored edit of the serving module: its "
            "\"content\" is the anchored definition's current code, "
            f"with the {route} branch already extended in code form "
            "— the fields the goal asks the endpoint to report are "
            "present with \"...\" sketch values. Return the complete "
            "new version of the anchored definition (def line "
            "included, at its indentation in the file — "
            f"{anchor_indent} spaces) with every \"...\" value "
            "replaced by the real computation of what the goal asks "
            "the endpoint to report, keeping the rest of the "
            "definition's code as it is; the system inserts it into "
            "the current module, so do not write the whole module. "
            "The content is code to complete, not text to copy back "
            "unchanged — a definition still carrying \"...\" sketch "
            "values does not implement the goal."
        )
    else:
        step1_inputs = {"file_path": module_path, "content": content}
        step1_guidance = (
            "Step-1 shows the existing branch as a commented sketch "
            "— extend it with the fields the goal asks for and write the FULL "
            "module; do not copy the sketch's comment lines into the scaffold."
        )
    # Step-2's test is runnable real code (see the docstring): the old
    # skeleton's TODO heredoc was copied verbatim into the model's plan
    # twice in retry #21. The field assertions follow the route's
    # derived response fields (_SKELETON_ROUTE_FIELDS — for /health
    # the goal's three reported items under the established
    # HEREDOC_TEST fixture's key names); any other route gets the
    # generic ok/status assertions, so the test stays correct for
    # routes whose fields the goal does not name.
    field_assertions = "".join(
        f'        self.assertIn("{field}", data)\n'
        + (
            f'        self.assertIsInstance(data["{field}"], {types})\n'
            if types is not None
            else ""
        )
        for field, types in _SKELETON_ROUTE_FIELDS.get(route, ())
    )
    test_code = (
        "import json\n"
        "import sys\n"
        "import tempfile\n"
        "import unittest\n"
        "import urllib.request\n"
        "from pathlib import Path\n"
        "\n"
        f'sys.path.insert(0, "{index.root}")\n'
        "\n"
        "from freetalon.api import HiveAPIServer\n"
        "from freetalon.config import HiveConfig\n"
        "from freetalon.hive import HiveController\n"
        "\n"
        "\n"
        "class RouteEndpointTests(unittest.TestCase):\n"
        "    def setUp(self):\n"
        "        self.tmpdir = tempfile.TemporaryDirectory()\n"
        "        base = Path(self.tmpdir.name)\n"
        "        config = HiveConfig(\n"
        "            workspace=base,\n"
        '            state_path=base / "state.json",\n'
        '            audit_log_path=base / "audit.log",\n'
        "            worker_cap=2,\n"
        "            queue_multiplier=3,\n"
        "            poll_interval_seconds=0.02,\n"
        "        )\n"
        "        self.controller = HiveController(config)\n"
        "        self.controller.start()\n"
        '        self.server = HiveAPIServer(self.controller, "test-token")\n'
        '        self.server.start("127.0.0.1", 0)\n'
        "        port = self.server._httpd.server_address[1]\n"
        '        self.base = f"http://127.0.0.1:{port}"\n'
        "\n"
        "    def tearDown(self):\n"
        "        self.server.stop()\n"
        "        self.controller.stop()\n"
        "        self.tmpdir.cleanup()\n"
        "\n"
        "    def test_route_answers(self):\n"
        f'        with urllib.request.urlopen(f"{{self.base}}{route}", timeout=3) as resp:\n'
        "            status = resp.status\n"
        "            data = json.loads(resp.read())\n"
        "        self.assertEqual(status, 200)\n"
        '        self.assertTrue(data["ok"])\n'
        '        self.assertIn("status", data)\n'
        + field_assertions
        + "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    unittest.main()\n"
    )
    nodes: list[dict[str, Any]] = [
        {
            "id": "step-1",
            "objective": edit,
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": step1_inputs,
            "outputs": [module_path],
            "acceptance": [f"The {route} endpoint is implemented in {module_path}."],
            "criteria": [{"type": "file_exists", "path": module_path}],
        },
        {
            "id": "step-2",
            "objective": f"Write the tests for {route}",
            "depends_on": ["step-1"],
            "assigned_claw": "bash",
            "inputs": {
                "command": f"cat > {test_path} <<'EOF'\n{test_code}EOF"
            },
            "outputs": [test_path],
            "acceptance": [f"The tests for {route} exist."],
            "criteria": [{"type": "file_exists", "path": test_path}],
        },
        {
            "id": "step-3",
            "objective": f"Run the tests for {route}",
            "depends_on": ["step-2"],
            "assigned_claw": "bash",
            "inputs": {"command": f"python3 {test_path}"},
            "outputs": ["test results"],
            "acceptance": [f"The tests for {route} pass."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
        {
            "id": "step-4",
            "objective": f"Start the API server for {route}",
            "depends_on": ["step-3"],
            "assigned_claw": "bash",
            "inputs": {
                "command": (
                    f"cd {index.root} && ./.venv/bin/python -m freetalon.cli "
                    f"stop; ./.venv/bin/python -m freetalon.cli start && "
                    f"for i in $(seq 1 30); do curl -sf "
                    f"http://localhost:{index.api_port}{route} && break; "
                    f"sleep 1; done; curl -sf "
                    f"http://localhost:{index.api_port}{route}"
                )
            },
            "outputs": ["API server running"],
            "acceptance": [f"The API server answers {route}."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
        {
            "id": "step-5",
            "objective": f"Verify {route} live",
            "depends_on": ["step-4"],
            "assigned_claw": "bash",
            "inputs": {"command": f"curl -s http://localhost:{index.api_port}{route}"},
            "outputs": ["verification"],
            "acceptance": [f"{route} returns its JSON live."],
            "criteria": [{"type": "exit_code", "equals": 0}],
        },
    ]
    skeleton = json.dumps({"nodes": nodes}, indent=2)
    if after == "placeholder":
        lead = (
            "CONCRETE PLAN SKELETON — your previous reply was rejected as a "
            "placeholder stub (comment-only code that implements nothing). Do "
            "not return another placeholder. Complete the skeleton below with "
            "REAL code and REAL tests, then return the completed JSON as your "
            "output."
        )
    else:
        lead = (
            "CONCRETE PLAN SKELETON — your previous reply was an empty plan "
            "({\"nodes\": []}). Do not return an empty plan again. Complete the "
            "skeleton below with REAL code and REAL tests, then return the "
            "completed JSON as your output."
        )
    return (
        f"{lead} The skeleton is the SHAPE of your output, not instruction "
        "text to re-serialize: every \"# TODO\" line must be replaced with "
        f"real code. {step1_guidance} "
        "Step-2's test is runnable real code, not a stub — extend its "
        "assertions with the fields the goal asks the endpoint to report; "
        "do not replace it with a TODO comment. Copying a TODO comment "
        "into a scaffold will be rejected — the scaffold must contain the "
        f"real implementation.\n{skeleton}"
    )


# Rejection-message markers for approaches the model has already tried
# and had rejected in this planning call. Retry #17: the Flask heredoc
# came back verbatim after a rejection that named the fix — the model
# never learns across attempts within one call, so the repair prompt
# names the rejected approaches explicitly. Approach names only, never
# quoted model content: the markers match the rejection's own wording
# (the framework error names Flask, the /tmp error names /tmp, and so
# on), so nothing the model wrote is echoed back.
# Retry #19 proved a bare prohibition does not steer the live model:
# the attempt-3 repair prompt named Flask as forbidden and the model
# wrote a Flask scaffold anyway. Each label now pairs the prohibition
# with the concrete replacement to use instead — a temperature-0 model
# follows a positive instruction ("do this") more reliably than a
# negative one ("do not do that") — and the repair prompt states the
# consequence (a repeat is rejected again and spends a repair).
_APPROACH_MARKERS: tuple[tuple[str, str], ...] = (
    (
        "Flask",
        "Do NOT write a Flask app — the serving module is built on stdlib "
        "http.server, not Flask. Instead, extend it in place: add a branch "
        "to the handler method that already serves the route, write the "
        "tests, and verify live with curl on the API port.",
    ),
    (
        "/tmp",
        "Do NOT repeat writing files under /tmp — code lives in the repo, "
        "not /tmp. Instead, write the module in the repo and the tests in "
        "the repo tests directory.",
    ),
    (
        "placeholder",
        "Do NOT scaffold a placeholder/comment-only stub over an existing "
        "module — write real code. Instead, extend the existing handler "
        "branch with the real implementation; a comment-only scaffold "
        "will be rejected again.",
    ),
    (
        "prose, not a shell command",
        "Do NOT write prose commands — write shell commands, not "
        "sentences. Instead, write a real shell command (a heredoc that "
        "writes real code, or a scaffold with real code) that performs "
        "the step.",
    ),
    (
        "hardcoded answer",
        "Do NOT echo a hardcoded JSON answer — run the real code or "
        "service and check ITS output. Instead, run the service and curl "
        "its live endpoint, and check the real response.",
    ),
    (
        # Retry #25: after the scaffold plan fell only on interpreter
        # paths, attempts 2/3 abandoned the scaffold for sed -i
        # one-liners editing api.py — the sed attractor. The marker is
        # the sed no-op rejection's own wording ("runs sed -i on ..."),
        # so a sed rejection anywhere in a call forbids the approach in
        # later repair prompts, and _repo_forbidden_approaches seeds it
        # for route goals from the start.
        "sed -i",
        "Do NOT edit a Python file with sed -i (or another in-place "
        "text substitution such as perl -pi) — a one-line substitution "
        "cannot write a real endpoint, and a pattern that does not "
        "match changes nothing while reporting success. Instead, write "
        "the edit with the scaffold claw (the full file with the real "
        "code) or a bash heredoc containing the real code.",
    ),
    (
        # Retry #28: attempts 2 and 3 answered the goal with a new,
        # smaller module in place of the existing one (dropping 11 and
        # 8 of its 12 definitions). The marker is the overwrite guard's
        # own wording ("drops N of the existing module's M
        # definitions"), so a guard rejection forbids the approach in
        # later repair prompts — and, via the returned
        # rejected_approaches metadata, in the replan that follows.
        "of the existing module's",
        "Do NOT replace an existing module with a new, smaller module "
        "— a scaffold overwrites the whole file, so every definition "
        "it omits is deleted. Instead, extend it with an anchored "
        "edit (anchor + position + only the new code — the system "
        "applies the edit to the current file), or scaffold the FULL "
        "updated module: every existing definition, kept in the same "
        "place with the same nesting, plus the new code.",
    ),
    (
        # Cycle 2: the scope lock's own key. A repair rejected for
        # leaving the rejected plan's file/dependency scope forbids
        # the approach in later repair prompts of the same call, the
        # same way the guard's rejection forbids module replacement.
        "scope",
        "Do NOT widen the plan's scope in a repair — no new files, "
        "no new dependencies. Instead, keep the rejected plan's "
        "file set and approach and fix the named defect; a wider "
        "plan must come from a re-plan, not a repair.",
    ),
)


# Which structured error codes evidence which forbidden approach.
# A/B refactor: _rejected_approaches used to substring-match the
# rejection message for the _APPROACH_MARKERS keys (each key is the
# owning rejection's own wording). The codes are assigned where the
# errors are created, so this mapping cannot drift when a message is
# reworded — and a code from an unrelated rejection can never
# accidentally contain another rule's key the way message text could.
_CODE_TO_APPROACH_KEY = {
    "framework": "Flask",
    "tmp_write": "/tmp",
    "placeholder_scaffold": "placeholder",
    "placeholder_stub": "placeholder",
    "placeholder_bash": "placeholder",
    "placeholder_delta": "placeholder",
    "prose_command": "prose, not a shell command",
    "static_standin": "hardcoded answer",
    "sed_noop": "sed -i",
    "scaffold_overwrite": "of the existing module's",
    "scope_widening": "scope",
}


def _rejected_approaches(codes: Collection[str]) -> list[str]:
    """Approach names a rejection's error codes show the model already tried."""
    keys = {
        _CODE_TO_APPROACH_KEY[code]
        for code in codes
        if code in _CODE_TO_APPROACH_KEY
    }
    return [label for key, label in _APPROACH_MARKERS if key in keys]


def _forbidden_block(
    rejected_approaches: list[str],
    goal_text: str | None,
    repo_root: str | Path | None,
    repairs_used: int,
    max_repairs: int,
    spent_repair: bool,
) -> str:
    """The DO NOT repeat block for the repair prompt, with replacements.

    Retry #19: the attempt-3 repair prompt named Flask as forbidden and
    the live model wrote a Flask scaffold anyway. A bare prohibition
    does not steer a temperature-0 model whose prior for "implement a
    web endpoint" is "write a Flask app" — so each forbidden approach
    carries its concrete replacement (the labels in _APPROACH_MARKERS),
    the Flask entry is followed by the goal's grounded-move sentence
    when one exists, and the block states the consequence: a repeat is
    rejected again, and it spends a repair.
    """
    if not rejected_approaches:
        return ""
    items = "\n".join(f"- {approach}" for approach in rejected_approaches)
    grounded = ""
    if any("Flask app" in approach for approach in rejected_approaches):
        move = _grounded_move_for_goal(goal_text, repo_root).strip()
        if move:
            grounded = f"\n{move}\n"
    consequence = (
        "A plan that repeats a forbidden approach will be rejected again."
    )
    if spent_repair:
        consequence += (
            f" This is repair {repairs_used} of {max_repairs} — do not "
            "spend it on an approach that has already failed."
        )
    return (
        "\nDO NOT repeat approaches you have already tried and had "
        "rejected in this planning call:\n"
        f"{items}\n"
        f"{grounded}"
        f"{consequence}\n"
    )


def _repo_forbidden_approaches(
    goal_text: str | None, index: Any | None
) -> list[str]:
    """Approaches known-bad for this repo, regardless of this call's history.

    Retry #23: the replan branch ran twice (first time since the
    seeded-replan fix) but the seed was EMPTY both times — initial
    planning had zero rejections, so nothing was carried in, and the
    replan's first attempt was the Flask plan. The Flask prohibition
    and the placeholder prohibition are not call history: they are
    repo facts. When the goal names an existing route, the serving
    module is built on stdlib http.server, so writing a Flask app over
    it is always wrong, and a placeholder stub over an existing module
    is always a fake deliverable. Those two labels are seeded into
    every replan prompt for a route goal from the start, seeded or not.

    Retry #25 adds a third: editing the serving module with sed -i.
    When the scaffold plan fell only on interpreter paths, the repairs
    abandoned the scaffold for sed one-liners that failed shell quoting
    and pattern grounding — the sed attractor. A route goal's edit has
    a known-good mechanism (the scaffold claw with the full module, or
    a heredoc), so the in-place text substitution is seeded as
    known-bad for route goals too.
    """
    if not goal_text or index is None:
        return []
    if not _routes_named_in_goal(goal_text, index):
        return []
    markers = {"Flask", "placeholder", "sed -i"}
    return [label for marker, label in _APPROACH_MARKERS if marker in markers]


def _seed_forbidden_block(
    seed: list[str], goal_text: str | None, repo_root: str | Path | None
) -> str:
    """The DO NOT use block for a replan's FIRST prompt, with replacements.

    The seed previously only reached repair prompts (inside
    _call_llm_with_repair): a replan whose first attempt was the Flask
    plan was ACCEPTED before the seed was ever shown. Retry #23's
    replan 1 was exactly that — Flask, accepted, executed, failed at
    runtime. The seed block goes into the initial prompt too, with
    first-attempt wording: these approaches were already rejected for
    this goal (or are known-bad for this repo), and a plan that uses
    one will be rejected again. The labels carry their concrete
    replacements, like the repair-prompt block, and the Flask entry is
    followed by the goal's grounded move when one exists.
    """
    if not seed:
        return ""
    items = "\n".join(f"- {approach}" for approach in seed)
    grounded = ""
    if any("Flask app" in approach for approach in seed):
        move = _grounded_move_for_goal(goal_text, repo_root).strip()
        if move:
            grounded = f"\n{move}\n"
    return (
        "\nDO NOT use these approaches — they were already rejected for "
        "this goal (or are known-bad for this repo):\n"
        f"{items}\n"
        f"{grounded}"
        "A plan that uses a forbidden approach will be rejected again.\n"
    )


# Context window for planner/replan LLM calls (Ollama num_ctx).
# Retry #22: the planner prompts do not fit in the 4096-token
# default, so the model never received the skeleton and could not
# emit a full plan — five byte-identical {"nodes":[]} replies.
# Measured 2026-11-17 with the Gemma 3 tokenizer from
# models/tokenizer.json: initial call 4,255 tokens (system 3,030 +
# user 1,225), first repair 6,415, grounded-plan output 1,687 — a
# full repair round needs 8,102 tokens of context. 12288 covers
# that with ~4,000 tokens spare for longer goals and replan briefs.
# The intake path keeps the 4096 default (its prompts are small);
# only planner/replan calls get the wider window. KV cost at 12288
# is ~4.6 GB vs ~1.5 GB at 4096 (0.375 MB/token for this 12B
# model) — Ollama fits it by offloading a few more layers to CPU
# if VRAM runs short, which is a speed cost, not a failure.
_PLANNER_NUM_CTX = 12288


# --- Cycle 2 (2026-11-17): constrained envelope, repair discipline,
# formatter plumbing ---
#
# Built from the prior-art survey's steal list (aider / OpenHands /
# SWE-agent / Goose), as solutions fitted to FreeTalon's purpose —
# not as a redesign: the staged loop, the validators, and the gates
# are unchanged in shape. Three mechanisms land here:
#
# 1. The plan ENVELOPE is grammar-constrained at decode time
#    (steal #1): planner/replan calls to Ollama carry a JSON Schema
#    in the `format` parameter, so the envelope's shape — a 'nodes'
#    array of node objects with typed fields and the claw name as an
#    enum — is guaranteed by the decoder instead of being repaired
#    after the fact. Code payloads stay inside node inputs as they
#    do today (the survey's warning is about escaping corrupting
#    code in JSON; the envelope constraint does not change payload
#    handling, and the payload channel of _materialize_heredoc_writes
#    is where payload fidelity is owned). The semantic validators
#    remain the second layer: schema-stage codes that the grammar
#    makes unreachable (missing_nodes_array, duplicate keys) simply
#    stop occurring on the live path; the validators still guard
#    every other entry point and every semantic rule.
#
# 2. The repair loop gains discipline (steal #5): stuck detection
#    (the same structured error signature three times in one call
#    stops repairs and escalates), an explicit task restatement in
#    every repair prompt (re-grounding), and a scope lock (a repair
#    that writes files the rejected plan did not write — and the
#    repair prompt did not name — or that installs dependencies the
#    rejected plan did not, is rejected as 'scope_widening' instead
#    of being accepted; widening belongs to a re-plan). No new human
#    gate appears anywhere: escalation goes to the loop's judge /
#    re-plan path, exactly where planning failures already go.
#
# 3. Formatter plumbing (steal #7): an optional second pass that
#    re-emits a planner response under the envelope schema before
#    parsing — the architect/editor split, as two prompts over one
#    local model by default. The endpoint/model are configurable
#    (FREETALON_FORMATTER_OLLAMA_BASE_URL / _MODEL) so the pass can
#    later run on another node (e.g. seeker88's Ollama) with no code
#    change. Off unless configured: with no formatter env set, the
#    planning path is byte-for-byte the one the suite pins today.


def plan_envelope_schema() -> dict[str, Any]:
    """The plan envelope as a JSON Schema for Ollama's ``format``.

    Derived from the PlanNode contract, kept shallow on purpose
    (the survey's caveat: deeply nested schemas degrade on small
    models): the top level requires only the 'nodes' array; each
    node requires 'id' and 'objective' (PlanNode's two required
    fields) and types the remaining model-facing fields, with the
    claw name as an enum over the registry (plus "" — PlanNode's
    default, whose absence the claw validator names). 'inputs' and
    each criterion stay free-form objects: their shape is semantic,
    and the validators own it. Extra keys are not forbidden here —
    the schema guarantees the envelope, the validators judge the
    rest, and an over-tight grammar would only trade repairable
    rejections for degenerate compliance.
    """
    node_properties: dict[str, Any] = {
        "id": {"type": "string"},
        "objective": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "assigned_claw": {
            "type": "string",
            "enum": sorted(KNOWN_CLAW_NAMES) + [""],
        },
        "inputs": {"type": "object"},
        "outputs": {"type": "array", "items": {"type": "string"}},
        "acceptance": {"type": "array", "items": {"type": "string"}},
        "criteria": {"type": "array", "items": {"type": "object"}},
        "status": {"type": "string"},
        "reversible": {"type": "boolean"},
        "timeout_s": {"type": "number"},
        "max_retries": {"type": "integer"},
        "hypothesis": {"type": "string"},
        "responds_to": {"type": "string"},
        "differs_by": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": node_properties,
                    "required": ["id", "objective"],
                },
            },
            "metadata": {"type": "object"},
        },
        "required": ["nodes"],
    }


@dataclass(frozen=True)
class FormatterSettings:
    """Endpoint/model override for the optional formatter pass.

    None fields fall back to the planner call's own settings, so the
    default formatter is the same local Ollama/Gemma the planner
    uses — a local two-pass, per the steal-#7 plumbing brief.
    """

    base_url: str | None = None
    model: str | None = None


def formatter_settings_from_env() -> FormatterSettings | None:
    """The configured formatter pass, or None when not configured.

    The pass is enabled by FREETALON_FORMATTER_ENABLED (truthy) or
    by setting either endpoint variable; FREETALON_FORMATTER_OLLAMA_
    BASE_URL / FREETALON_FORMATTER_OLLAMA_MODEL then select where
    the formatter call goes (another node's Ollama needs no code
    change). With none of the three set, planning is single-pass,
    exactly as before this plumbing existed.
    """
    enabled = os.environ.get(
        "FREETALON_FORMATTER_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    base_url = (
        os.environ.get("FREETALON_FORMATTER_OLLAMA_BASE_URL", "").strip()
        or None
    )
    model = (
        os.environ.get("FREETALON_FORMATTER_OLLAMA_MODEL", "").strip()
        or None
    )
    if not enabled and base_url is None and model is None:
        return None
    return FormatterSettings(base_url=base_url, model=model)


def reformat_plan_output(
    response_text: str,
    settings: LLMSettings,
    formatter: FormatterSettings,
) -> str:
    """Re-emit a planner response under the envelope schema (steal #7).

    The formatter call's only job is form: same plan, same content,
    exact envelope shape — enforced by passing the envelope schema
    as the call's format constraint. A formatter backend failure
    degrades to the planner's original text: the plumbing must never
    make planning more fragile than the single pass it assists, and
    the original text's own parse verdict is the honest fallback.
    """
    formatter_settings = dataclass_replace(
        settings,
        base_url=formatter.base_url or settings.base_url,
        model=formatter.model or settings.model,
        format_schema=plan_envelope_schema(),
    )
    prompt = (
        "Re-emit the plan below as ONE JSON object with a \"nodes\" "
        "array. Keep every node's id, objective, depends_on, "
        "assigned_claw, inputs, outputs, acceptance and criteria "
        "exactly as given — change only the form, never the content "
        "or the meaning.\n\n"
        f"NODE SHAPE — {_NODE_SHAPE_EXAMPLE}\n\n"
        f"Plan to re-emit:\n{response_text}\n\n"
        "JSON Output:"
    )
    try:
        return call_llm(
            FORMATTER_SYSTEM_PROMPT, prompt, settings=formatter_settings
        )
    except LLMBackendError:
        return response_text


# A shell command that installs dependencies: a scope change, never
# a repair of the plan that was rejected (its commands are pinned by
# the human-owned shape, per the survey's scope-lock rule).
_INSTALL_COMMAND_RE = re.compile(
    r"\b(?:pip3?|apt-get|apt|npm|yarn|pnpm)\s+install\b"
)


def _scope_key(path: str, repo_root: str | Path | None) -> str:
    """A write path's comparison key: resolved, so the same file
    spelled absolute in one attempt and relative in another is one
    scope entry."""
    return str(_resolve_target_path(path, repo_root))


def _response_write_scope(
    response_text: str,
) -> tuple[set[str], bool]:
    """The (write paths, installs-dependencies) scope of a raw output.

    Read leniently (the output may be too malformed for PlanNode
    validation — that is why it was rejected): the lenient node view
    feeds the same written-path extraction the validators use. An
    unparseable output has no scope: it defines no baseline, and its
    own schema rejection is the signal that matters.
    """
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except LLMResponseError:
        return set(), False
    nodes_raw = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes_raw, list):
        return set(), False
    paths: set[str] = set()
    installs = False
    for position, raw in enumerate(nodes_raw, start=1):
        view = _lenient_node_view(raw, position)
        if view is None:
            continue
        paths.update(_node_written_paths(view))
        if (view.assigned_claw or "") in ("bash", "shell_exec"):
            command = view.inputs.get("command") or view.inputs.get("cmd")
            if isinstance(command, str) and _INSTALL_COMMAND_RE.search(
                _strip_heredoc_bodies(command)
            ):
                installs = True
    return paths, installs


def _prompt_named_paths(prompt: str) -> set[str]:
    """Absolute paths a prompt names — the harness's own steering is
    an endorsed scope: a repair that writes a file the repair prompt
    itself put in front of the model (the grounded skeleton's test
    file, the extension block's module) is following guidance, not
    widening the plan."""
    return {
        match.group(0).rstrip("),.")
        for match in _ABS_PATH_RE.finditer(prompt)
    }


def _is_test_file_path(path: str) -> bool:
    """True for a Python test file: a test_*.py / *_test.py /
    conftest.py basename, or any .py file under a tests/ directory."""
    parts = [part for part in re.split(r"[/\\]", str(path)) if part]
    if not parts or not parts[-1].endswith(".py"):
        return False
    name = parts[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "tests" in parts[:-1]
    )


def _scope_widening_error(
    nodes: list[PlanNode],
    baseline_paths: set[str],
    baseline_installs: bool,
    exempt_paths: set[str],
    repo_root: str | Path | None,
    workspace: str | Path | None = None,
) -> LLMResponseError | None:
    """A 'scope_widening' rejection when a repair leaves the scope of
    the plan it repairs, else None.

    Cycle 2 (steal #5, scope lock): a repair round exists to fix the
    rejected plan, not to redesign it. A would-be-accepted repair
    that writes a file outside the endorsed scope — or that installs
    dependencies the rejected plan did not — is rejected with the
    named code and the original scope spelled out, so the model can
    either return to the plan's files or let the call escalate to a
    re-plan, where widening is legitimate. (The framework instance
    of this rule — a repair swapping the stack — is older: the
    'framework' validator owns it. This is the generalization to
    the plan's file set and dependency set.)

    The endorsed scope is the task's own territory rather than the
    model's invention: the rejected plan's files (the baseline);
    any path the harness's prompts have named in this call (its
    steering is guidance the repair is supposed to follow — the
    grounded move names the serving module and the repo test file);
    the task's workspace subtree (the scratch area the goal itself
    assigns); and test files anywhere — the canonical grounded
    shape always writes a test file for the goal (the skeleton puts
    it in the workspace, or the repo's tests/ when no workspace is
    known, as in a re-plan whose brief carries none), so which test
    file the rejected plan happened to pick does not bind the
    repair. The historical sequences pin the last two: retry #19's
    grounded repairs — in the initial call and in the re-plan —
    write the workspace test file the rejected plans never wrote.
    A test file's content is judged by the full validator stack
    like any other write; only its novelty is not held against it.
    """
    baseline_keys = {_scope_key(path, repo_root) for path in baseline_paths}
    exempt_keys = {_scope_key(path, repo_root) for path in exempt_paths}
    workspace_key = (
        _scope_key(str(workspace), repo_root) if workspace else None
    )
    records: list[PlanError] = []
    for node in nodes:
        for path in sorted(_node_written_paths(node)):
            if _is_test_file_path(path):
                continue
            key = _scope_key(path, repo_root)
            if key in baseline_keys | exempt_keys:
                continue
            if workspace_key and (
                key == workspace_key or key.startswith(workspace_key + "/")
            ):
                continue
            records.append(
                PlanError(
                    code="scope_widening",
                    message=(
                        f"Planner node '{node.id}' widens the plan's "
                        f"scope: it writes {path!r}, which the rejected "
                        "plan did not write and the repair guidance did "
                        "not name. A repair must stay within the "
                        "rejected plan's files "
                        f"({', '.join(sorted(baseline_paths))}) — "
                        "changing the plan's file set is a re-plan, not "
                        "a repair. Return a corrected plan that writes "
                        "only the original files."
                    ),
                    node_id=node.id,
                )
            )
        if (
            (node.assigned_claw or "") in ("bash", "shell_exec")
            and not baseline_installs
        ):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str) and _INSTALL_COMMAND_RE.search(
                _strip_heredoc_bodies(command)
            ):
                records.append(
                    PlanError(
                        code="scope_widening",
                        message=(
                            f"Planner node '{node.id}' widens the "
                            "plan's scope: it installs dependencies, "
                            "which the rejected plan did not do. A "
                            "repair must not add dependencies — that is "
                            "a re-plan, not a repair. Return a corrected "
                            "plan that stays within the rejected plan's "
                            "approach."
                        ),
                        node_id=node.id,
                    )
                )
    if not records:
        return None
    return _numbered_rejection(records)


# OpenHands' StuckDetector fires on the same error three times;
# three identical structured signatures in one planning call is the
# same signal here (the default budget — 1 + 2 free + 2 repairs —
# makes three the largest count a stuck loop can reach before the
# budget ends anyway, so the detector converts a silent budget burn
# into a named escalation whenever it can fire at all).
_STUCK_REPEAT_LIMIT = 3


def _stuck_repair_error(
    exc: LLMResponseError,
    signature: frozenset,
    count: int,
) -> StuckRepairError:
    """The escalation raised when a repair loop is stuck (steal #5)."""
    codes = sorted({code for code, _node_id in signature})
    stuck = StuckRepairError(
        "Repair loop stuck: the same rejection "
        f"({', '.join(codes)}) has now occurred {count} times in "
        "a row in this planning call — the model is repeating the "
        "same defect instead of fixing it, so repairs stop here "
        "and the failure escalates to re-planning. "
        f"Last rejection: {exc}"
    )
    return _attach_plan_errors(
        stuck, getattr(exc, "plan_errors", None) or []
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
    repo_root: str | Path | None = None,
    seed_forbidden: list[str] | None = None,
    formatter: FormatterSettings | None = None,
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
    code-overwrite fakes — or placeholder scaffolds (retry #17) — is
    re-prompted for free, up to ``max_free_reprompts`` times. The budget
    is not raised — junk that keeps coming eventually spends repairs and
    the loop still ends (at most 1 + max_free_reprompts + max_repairs
    calls). A cheap rejection past the free cap falls through to the
    repair budget.

    The repair prompt also names the approaches the model has already
    tried and had rejected in this planning call (retry #17: the Flask
    heredoc came back verbatim after a rejection that named the fix),
    so the model is told not to repeat them instead of re-discovering
    the same rejection. ``seed_forbidden`` carries approaches rejected
    in an EARLIER planning call into this one (retry #20: the replan
    phase starts with a fresh forbidden list, so Flask — rejected in
    initial planning — came back as the replan's third attempt). Seeded
    approaches are named in the first repair prompt of this call, with
    their replacements and the consequence, exactly like approaches
    rejected inside this call. The accumulated list is returned in the
    metadata under "rejected_approaches" so the caller can carry it
    into the next planning call.

    A run of empty plans is answered with a concrete grounded skeleton
    (retry #16): after any empty reply the next repair prompt carries a
    fill-in-the-blanks plan skeleton for the route goal, so the model has
    structure to complete instead of giving up again. And when the budget
    is exhausted on empty plans, the raised error names the grounded move
    instead of failing silently on an empty reply. A second empty plan in
    the same call gets the skeleton again but with the count and the
    consequence named (retry #21): the model returned an empty plan twice
    despite the skeleton, so the repeat names how many empties there have
    been and that the free re-prompts are used up — the budget itself is
    unchanged. A placeholder rejection gets the skeleton too (retry #21):
    the repair round after a placeholder stub carries the fill-in-the-
    blanks with the actual branch code and a runnable test, not just the
    prohibition.

    The repair prompt also steers back to the scaffold shape when the
    previous plan used it and failed only on command/criterion strings
    (retry #25): attempt 1's scaffold plan was good code rejected for
    two interpreter paths, and attempts 2/3 abandoned the shape for
    sed -i one-liners. When the rejection names no content defect, the
    prompt says the shape was right and to fix only the named strings
    (_shape_steering_block).

    After an overwrite-guard rejection the repair prompt instead
    carries the extension block (retry #28): the failed scaffold's
    FULL dropped-definition list recomputed against the module on
    disk, the module's full definition list, and the concrete pattern
    (the full updated module, definitions in place, plus the new
    code) — and the shape-steering block stays silent, because the
    guard's code is a content-defect code
    (_extension_steering_block).

    After a materialization rejection the repair prompt carries the
    materialization block (retry #33): the failed edit recomputed
    against the module on disk, the module's full definition list,
    the anchor's own indentation, the module's skeleton, and the
    keep-the-edit-form pattern — the extension block's trigger is
    the guard's phrase alone, so before this the repair prompt
    carried only the rejection text and the generic shape example,
    and the repair round abandoned the edit form for a full-content
    scaffold (_materialization_steering_block).

    The repair prompt only steers if the model receives it (retry
    #22): the prompts exceed Ollama's 4096-token context window,
    so planner/replan calls use _PLANNER_NUM_CTX instead. The
    wider window is applied here, not in the driver or intake, so
    the intake path keeps the 4096 default. An explicit num_ctx on
    the caller's settings is respected, never overridden.

    Cycle 2: every call on the Ollama path carries the plan
    envelope schema as its format constraint (steal #1 — a
    caller-supplied schema is respected, like num_ctx); every
    repair prompt restates the task verbatim (steal #5
    re-grounding — the full original prompt already rides along,
    and the restatement puts the unchanged goal directly over the
    rejection, where a drifting repair cannot miss it); a repair
    that widens the first rejected plan's write/dependency scope
    is rejected as 'scope_widening' (steal #5 scope lock); the
    identical rejection signature three times in a row raises
    StuckRepairError and escalates (steal #5 stuck detection);
    and when ``formatter`` is configured, each response first
    passes through the formatter re-emission (steal #7 plumbing).
    """
    # Retry #22: derive the effective settings once, before the loop,
    # so every call in this planning call (initial + repairs) gets
    # the planner context window. A caller that already set num_ctx
    # keeps its value.
    if settings is None:
        effective_settings = LLMSettings.from_env()
    else:
        effective_settings = settings
    if effective_settings.num_ctx is None:
        effective_settings = dataclass_replace(
            effective_settings, num_ctx=_PLANNER_NUM_CTX
        )
    # Cycle 2 (steal #1): the plan envelope is grammar-constrained at
    # decode time on the Ollama path, so the envelope's shape is
    # guaranteed by the decoder instead of repaired after the fact.
    # A caller-supplied schema is respected, never overridden — the
    # same rule as num_ctx above.
    if (
        effective_settings.format_schema is None
        and effective_settings.backend == "ollama"
    ):
        effective_settings = dataclass_replace(
            effective_settings, format_schema=plan_envelope_schema()
        )
    prompt = user_prompt
    last_error: LLMResponseError | None = None
    prev_response_text: str | None = None
    repairs_used = 0
    free_used = 0
    empty_streak = 0
    empty_count = 0
    # Cycle 2 (steal #5): repair-discipline state — the write scope
    # of the first rejected plan in this call (the scope lock's
    # baseline) and the current run of identical rejection
    # signatures (the stuck detector's input).
    baseline_paths: set[str] | None = None
    baseline_installs = False
    # Paths any prompt in this call has named — accumulated, since
    # the harness's steering across rounds is all endorsed scope for
    # the scope lock (see _scope_widening_error).
    endorsed_paths: set[str] = set()
    last_signature: frozenset | None = None
    signature_streak = 0
    # Seed with approaches rejected in an earlier planning call, in
    # order, deduplicated — the first repair prompt of this call already
    # names them (retry #20).
    rejected_approaches: list[str] = []
    for seeded in seed_forbidden or []:
        if isinstance(seeded, str) and seeded and seeded not in rejected_approaches:
            rejected_approaches.append(seeded)
    spent_repair = False
    while True:
        endorsed_paths |= _prompt_named_paths(prompt)
        response_text = call_llm(
            system_prompt, prompt, settings=effective_settings
        )
        if formatter is not None:
            # Steal #7 plumbing: the configured formatter pass
            # re-emits the response under the envelope schema before
            # parsing. What is parsed — and described back to the
            # model on a rejection — is the formatted text: it is the
            # plan being judged.
            response_text = reformat_plan_output(
                response_text, effective_settings, formatter
            )
        try:
            nodes, metadata = parse_planner_response(
                response_text,
                goal_text=goal_text,
                workspace=workspace,
                repo_root=repo_root,
            )
            if baseline_paths is not None:
                # Steal #5 scope lock: a repair that parses is still
                # a rejection when it leaves the endorsed scope —
                # the rejected plan's files, the paths the harness's
                # prompts have named in this call, and the task's
                # workspace (see _scope_widening_error). The widening
                # plan is converted into a named rejection and
                # repaired like any other; when the budget ends it
                # escalates like any other.
                scope_error = _scope_widening_error(
                    nodes,
                    baseline_paths,
                    baseline_installs,
                    endorsed_paths,
                    repo_root,
                    workspace=workspace,
                )
                if scope_error is not None:
                    raise scope_error
            metadata = dict(metadata)
            metadata["rejected_approaches"] = list(rejected_approaches)
            return nodes, metadata
        except LLMResponseError as exc:
            last_error = exc
            # Steering keys off the rejection's structured codes
            # (A/B refactor) — never off substrings of its message.
            error_codes = {
                record.code
                for record in getattr(exc, "plan_errors", None) or []
            }
            for approach in _rejected_approaches(error_codes):
                if approach not in rejected_approaches:
                    rejected_approaches.append(approach)
            # Cycle 2 (steal #5): the first rejected output that
            # writes anything sets this call's scope baseline; later
            # would-be-accepted repairs are locked to it (above).
            if baseline_paths is None:
                found_paths, found_installs = _response_write_scope(
                    response_text
                )
                if found_paths:
                    baseline_paths = found_paths
                    baseline_installs = found_installs
            # Cycle 2 (steal #5): stuck detection — the identical
            # structured signature three times in a row stops the
            # loop and escalates to re-planning, instead of spending
            # the remaining attempts re-asking for a fix the model
            # is not producing. Empty plans are exempt: they carry
            # their own grounded escalation below.
            if not isinstance(exc, EmptyPlanError):
                signature = frozenset(
                    (record.code, record.node_id)
                    for record in getattr(exc, "plan_errors", None)
                    or []
                ) or frozenset({(type(exc).__name__, None)})
                if signature == last_signature:
                    signature_streak += 1
                else:
                    last_signature = signature
                    signature_streak = 1
                if signature_streak >= _STUCK_REPEAT_LIMIT:
                    raise _stuck_repair_error(
                        exc, signature, signature_streak
                    ) from exc
            if isinstance(exc, EmptyPlanError):
                empty_streak += 1
                empty_count += 1
            else:
                empty_streak = 0
            if isinstance(exc, CheapRejectionError) and free_used < max_free_reprompts:
                free_used += 1
                spent_repair = False
            elif repairs_used < max_repairs:
                repairs_used += 1
                spent_repair = True
            else:
                break
            skeleton_block = ""
            if empty_streak >= 1:
                skeleton = _grounded_skeleton(goal_text, repo_root, workspace)
                if skeleton:
                    skeleton_block = f"\n{skeleton}\n"
                    if empty_count >= 2:
                        # Retry #21: the model returned an empty plan twice
                        # in one call despite the skeleton being in the
                        # prompt after the first one — the same skeleton,
                        # shown unchanged, is easy to ignore. Name the
                        # count and the consequence (free re-prompts are
                        # capped, so the next empty spends a repair)
                        # instead of showing the same block again. The
                        # budget itself is NOT changed: a second empty
                        # still gets its free re-prompt.
                        forceful = (
                            f"\nYou have already returned an empty plan "
                            f"{empty_count} times in this planning call. Do "
                            "NOT return another empty plan — complete the "
                            "skeleton below with real code and return it as "
                            "your output.\n"
                        )
                        if free_used >= max_free_reprompts:
                            forceful += (
                                "Your free re-prompts are used up — another "
                                "empty plan will spend one of your remaining "
                                "repairs.\n"
                            )
                        skeleton_block = f"{forceful}{skeleton_block}"
            elif error_codes & _PLACEHOLDER_SKELETON_CODES:
                # Retry #21: after a placeholder rejection the model needs
                # the concrete shape again, not just the prohibition — the
                # skeleton is the fill-in-the-blanks with the actual branch
                # code and a runnable test, so the repair round has
                # something real to complete instead of another TODO to
                # copy. The lead-in names the placeholder rejection, so
                # the model is never told its reply was empty when it was
                # a stub. The code set is exactly the two cases the old
                # message-phrase test selected (the placeholder delta and
                # the comment-only scaffold — not the placeholder stub).
                skeleton = _grounded_skeleton(
                    goal_text, repo_root, workspace, after="placeholder"
                )
                if skeleton:
                    skeleton_block = f"\n{skeleton}\n"
            forbidden_block = _forbidden_block(
                rejected_approaches,
                goal_text,
                repo_root,
                repairs_used,
                max_repairs,
                spent_repair,
            )
            steering_block = _shape_steering_block(response_text, error_codes)
            extension_block = _extension_steering_block(
                response_text, error_codes, repo_root
            )
            materialization_block = _materialization_steering_block(
                response_text, error_codes, repo_root
            )
            # Cycle 2 (steal #5 re-grounding): the repair prompt
            # re-injects the original task verbatim, directly over
            # the rejection — the full original prompt rides along
            # above, but a repair that is drifting (a new stack, a
            # new file set) needs the unchanged goal stated where
            # the failure is named, not only at the top of the call.
            task_block = (
                f"THE TASK IS UNCHANGED: {goal_text}\n" if goal_text else ""
            )
            prompt = (
                f"{user_prompt}\n\n"
                "Your previous output was REJECTED. Do not repeat it.\n"
                f"{task_block}"
                f"Rejection reason: {exc}\n"
                f"Previous output: {_describe_failed_output(response_text)}\n"
                f"{_dropped_fields_note(prev_response_text, response_text)}"
                f"{forbidden_block}"
                f"{steering_block}"
                f"{extension_block}"
                f"{materialization_block}"
                f"\nNODE SHAPE — {_NODE_SHAPE_EXAMPLE}\n"
                f"{skeleton_block}\n"
                "Return ONLY a corrected JSON object with a \"nodes\" array that "
                "fixes ALL of the rejection reasons listed above. Every node "
                "must match the NODE SHAPE above — do not drop a field from it "
                "and do not invent a field outside it.\n"
                "JSON Output:"
            )
            prev_response_text = response_text
    assert last_error is not None
    if isinstance(last_error, EmptyPlanError):
        # Retry #16: the re-planner returned empty plans five times in a
        # row, and the escalation said only "must include at least one
        # plan node" — no grounded guidance for the next attempt or the
        # operator. Name the grounded move in the escalation itself.
        grounded = _grounded_move_for_goal(goal_text, repo_root)
        if grounded:
            escalation = EmptyPlanError(
                f"{last_error}{grounded} The model returned empty plans "
                "repeatedly instead of a plan DAG — produce the grounded "
                "plan above, do not return another empty plan."
            )
            _attach_plan_errors(
                escalation, getattr(last_error, "plan_errors", None) or []
            )
            raise escalation from last_error
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
    # Retry #22: the shape sat at ~70% of the initial prompt (offset
    # 2845 of 4093 chars), ahead of the instruction paragraph. The
    # shape is the decision the model has to make, so it goes last —
    # immediately before "JSON Output:" — instead of ahead of the
    # instruction. Only ~13 chars ("\n\nJSON Output:") follow it.
    user_prompt = (
        f"{context}\n\n"
        f"{_format_intent_for_prompt(intent)}\n\n"
        "Produce a NEW execution plan DAG for the goal above. Your output "
        "must be a JSON object with a \"nodes\" array — do NOT repeat the "
        "goal text or any JSON from this prompt.\n"
        f"{shape_block}\n\n"
        "JSON Output:"
    )
    nodes, metadata = _call_llm_with_repair(
        PLANNER_SYSTEM_PROMPT,
        user_prompt,
        settings,
        max_repairs=max_repairs,
        goal_text=intent.goal,
        workspace=workspace,
        repo_root=repo_root,
        formatter=formatter_settings_from_env(),
    )
    return build_execution_plan(intent, nodes, metadata, plan_id=plan_id)


def replan_from_brief(
    brief: dict[str, Any],
    settings: LLMSettings | None = None,
    *,
    max_repairs: int = 2,
    repo_root: str | Path | None = None,
    seed_forbidden: list[str] | None = None,
) -> list[PlanNode]:
    """Produce delta-plan nodes from a judge's re-plan brief (M1).

    The brief carries the unchanged goal contract, the failed measurements,
    relevant memory, and the remaining budget. The LLM's response is parsed
    and normalized exactly like an initial plan; the caller (the loop) runs
    the judge's duplicate-approach validation before accepting it.

    ``seed_forbidden`` (or the brief's "seed_forbidden" key, which the loop
    fills from the initial plan's metadata) carries the approaches rejected
    in initial planning into this replan call — retry #20: without it the
    replan starts with a fresh forbidden list and Flask, rejected in
    initial planning, came back as the replan's third attempt. The
    accumulated rejected-approaches list is written back into the brief
    under "rejected_approaches" so the loop can carry it into the next
    replan's seed.
    """

    index = build_repo_index(repo_root)
    context = format_repo_context(index, workspace=brief.get("workspace"))
    shape = _route_goal_shape(_goal_text_from_brief(brief), repo_root)
    shape_block = f"\n\n{shape}" if shape else ""
    # The replan needs the node shape in front of it, not just as a
    # rejection after the fact: retry #9's replan 1 invented a
    # node/action schema and burned the budget before the shape was ever
    # named. The ROUTE GOAL shape is route-scoped; the node shape is
    # general, so it rides along on every replan.
    node_shape_block = f"\n\nNODE SHAPE — {_NODE_SHAPE_EXAMPLE}"
    seed = seed_forbidden if seed_forbidden is not None else brief.get("seed_forbidden")
    if not isinstance(seed, list):
        seed = None
    # Retry #23: the seed was empty in both replan calls because initial
    # planning had zero rejections — so the replan's first attempt was
    # the Flask plan, accepted before the seed was ever shown. Merge the
    # call-history seed with the repo-level known-bad approaches (Flask
    # + placeholder for a route goal), and put the merged seed in the
    # FIRST replan prompt too, not just in repair prompts.
    merged_seed = list(
        dict.fromkeys(
            list(seed or [])
            + _repo_forbidden_approaches(_goal_text_from_brief(brief), index)
        )
    )
    seed_block = _seed_forbidden_block(
        merged_seed, _goal_text_from_brief(brief), repo_root
    )
    # Retry #22: as in plan_task_intent, the ROUTE GOAL shape goes
    # last (after the node shape and the instruction), immediately
    # before "JSON Output:" — it is the decision, not preamble. The
    # seed block goes BEFORE the shape, so the shape stays last.
    user_prompt = (
        f"{context}\n\n{_format_brief_for_prompt(brief)}{node_shape_block}\n\n"
        "Produce a NEW delta plan DAG for the brief above. Do NOT repeat, "
        "quote, or echo the brief — it is your input, not your output.\n"
        f"{seed_block}"
        f"{shape_block}\n\n"
        "JSON Output:"
    )
    nodes, metadata = _call_llm_with_repair(
        REPLAN_SYSTEM_PROMPT,
        user_prompt,
        settings,
        max_repairs=max_repairs,
        goal_text=_goal_text_from_brief(brief),
        workspace=brief.get("workspace"),
        repo_root=repo_root,
        seed_forbidden=merged_seed or None,
        formatter=formatter_settings_from_env(),
    )
    brief["rejected_approaches"] = metadata.get("rejected_approaches", seed or [])
    return nodes
