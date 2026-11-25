"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations
import ast
from contextlib import contextmanager
from dataclasses import dataclass, replace as dataclass_replace
from collections import Counter
import json
import os
import re
import shlex
import hashlib
import heapq
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Collection

from pydantic import ValidationError

from .contract_clauses import (
    CLAUSES_METADATA_KEY,
    clause_coverage_errors,
    clause_delta_errors,
    clauses_for_specs,
    delta_clause_coverage,
    format_clauses_block,
    format_replan_clauses_block,
    node_clause_declaration,
    probe_suspect_map,
)
from .executor import _command_runs_full_suite
from .grounding import (
    attribute_grounding_violations,
    edit_grounding_violations,
)
from .scoped_edits import (
    ScopedEdit,
    ScopedEditError,
    apply_scoped_edits,
    parse_scoped_edits,
    python_syntax_error,
)
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
    existing_file_inventory,
    format_existing_file_inventory,
    format_repo_context,
    format_spec_context,
    framework_in_text,
    goal_named_specs,
    qualified_def_names,
)
from .measure import (
    CRITERION_COMMAND_TIMEOUT_S,
    CRITERION_CUSTOM_CHECK,
    CRITERION_EXIT_CODE,
    CRITERION_FILE_EXISTS,
    CRITERION_FILE_MATCHES,
    CRITERION_METRIC,
    CRITERION_TESTS_PASS,
    VERDICT_FAIL,
    custom_checkers,
    measurement_failed_detail,
)
from .memory import node_payload, normalize_payload
from .models import ExecutionPlan, PlanNode, PlanStatus, TaskIntent
from .prompts import (
    FORMATTER_SYSTEM_PROMPT,
    LOCAL_REPAIR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    REPLAN_SYSTEM_PROMPT,
    planner_system_prompt,
    replan_system_prompt,
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


def _attach_intent_corrections(
    exc: LLMResponseError, corrections: Collection[dict[str, Any]]
) -> LLMResponseError:
    """Attach this attempt's skeleton-intent corrections to a rejection.

    Campaign build #11: parse corrects a skeleton write node's
    declared content intent deterministically when it contradicts
    the tree (see _correct_skeleton_intents); when the attempt is
    still rejected for other defects, the corrections ride the
    exception — the _attach_plan_errors channel — so the repair
    loop can persist them in the attempt record (build #9) and
    name them in the repair brief's target-facts block instead of
    the correction vanishing with the discarded candidate.
    """
    exc.intent_corrections = list(corrections)  # type: ignore[attr-defined]
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

    The nodes that DID validate also contribute their own per-node
    validation records (gap closure, 2026-11-20): a valid sibling's
    real defect — a banned command, an unknown claw, a placeholder
    value — was invisible until the schema round was fixed, spending
    a second repair round to surface what the first rejection
    already knew. The merge is scoped (per-node codes only,
    deduplicated, subset artifacts filtered); see
    _schema_path_merged_records.
    """
    nodes: list[PlanNode] = []
    errors: list[str] = []
    failed_positions: set[int] = set()
    for position, raw in enumerate(raw_nodes, start=1):
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            label = f"'{raw['id']}'"
        else:
            label = f"at position {position}"
        try:
            nodes.append(PlanNode.model_validate(raw))
        except ValidationError as exc:
            failed_positions.add(position)
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
        # belong in the schema path. The collector DOES now run on the
        # nodes that validated (gap closure, 2026-11-20) and their
        # per-node records merge in below — scoped so the duplication
        # and count objections above do not apply; see
        # _schema_path_merged_records.
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
        all_records = all_records + _schema_path_merged_records(
            nodes,
            raw_nodes,
            failed_positions,
            repo_index,
            goal_text,
            repo_root,
            {record.message for record in all_records},
        )
        raise _numbered_rejection(
            all_records,
            cheap=bool(standin_records or placeholder_records),
            suffix=f"\n{_NODE_SHAPE_EXAMPLE}",
        )
    return nodes


# Per-node codes that may merge into a schema-error rejection. The
# plan-level codes (unwritten_file, route_engagement,
# missing_directory, contract_*) are computed against the whole plan
# and would misfire on the valid subset, so they never merge.
_SCHEMA_PATH_MERGED_CODES = frozenset(
    {
        "unknown_claw",
        "criteria",
        "command",
        "prose_command",
        "duplicate_code",
        "heredoc_parse",
        "code_append",
        "scaffold_parse",
        "static_standin",
        "scaffold_overwrite",
        "elision",
        "placeholder_value",
        "stub_only_module",
        "sed_noop",
        "sed_ban",
        "placeholder_scaffold",
        "placeholder_stub",
        "placeholder_bash",
        "tmp_write",
        "framework",
        "wrong_location",
        "missing_target",
        "suite_criterion_command",
        "field_too_long",
        "repetition_loop",
    }
)

_CRITERION_UNWRITTEN_PATH_RE = re.compile(
    r"file_(?:exists|matches) path (.+?) is not a file this node "
    r"writes or runs"
)


def _schema_path_merged_records(
    nodes: list[PlanNode],
    raw_nodes: list[Any],
    failed_positions: set[int],
    repo_index: Any | None,
    goal_text: str | None,
    repo_root: str | Path | None,
    seen_messages: set[str],
) -> list[PlanError]:
    """The valid nodes' own defects, collected on the schema-error path.

    When one node failed schema validation, _validate_raw_nodes named
    the schema errors plus the stand-in/placeholder fakes (lenient
    views) — and nothing else, so a sibling that DID validate but
    carried a real defect was invisible until the schema round was
    fixed. The full collector now runs on the nodes that validated
    and their per-node records merge into the schema rejection.

    The merge is scoped, on the recorded retry-#21 evidence (a naive
    merge duplicates the schema errors and moves the pinned
    retry-#7/#11 counts): (a) only per-node codes merge — plan-level
    records are computed against the whole plan and misfire on a
    subset; (b) a record whose message already appears (the
    lenient-view stand-in/placeholder records) is dropped; (c) a
    criteria record blaming a valid node for a file-criterion path a
    schema-BROKEN sibling demonstrably writes is an artifact of the
    subset view and is dropped — fixing the broken node's schema
    restores the credit. When no node validated there is nothing to
    merge, so the pinned all-broken fixtures are untouched. A
    collector failure falls back to no merge: enrichment must never
    mask the schema rejection. The cheap flag is the caller's and is
    unchanged — it is computed from the stand-in/placeholder records
    exactly as before.
    """
    if not nodes:
        return []
    try:
        records, _cheap = _collect_plan_error_records(
            nodes,
            repo_root,
            goal_text,
            None,
            index=repo_index,
            plan_context="enrichment",
        )
    except Exception:
        return []
    broken_written: set[str] = set()
    for position, raw in enumerate(raw_nodes, start=1):
        if position not in failed_positions:
            continue
        view = _lenient_node_view(raw, position)
        if view is not None:
            broken_written.update(_node_written_paths(view))
    merged: list[PlanError] = []
    for record in records:
        if record.code not in _SCHEMA_PATH_MERGED_CODES:
            continue
        if record.message in seen_messages:
            continue
        if record.code == "criteria" and broken_written:
            match = _CRITERION_UNWRITTEN_PATH_RE.search(record.message)
            if match is not None:
                try:
                    claimed = ast.literal_eval(match.group(1))
                except (SyntaxError, ValueError):
                    claimed = None
                if isinstance(claimed, str) and any(
                    _same_path(claimed, target) for target in broken_written
                ):
                    continue
        seen_messages.add(record.message)
        merged.append(record)
    return merged


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


def _structural_rejection(
    structural: PlanError,
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None,
    workspace: str | None,
    repo_index: Any | None,
) -> LLMResponseError:
    """A structural rejection, enriched with the plan's content errors.

    Duplicate ids, a self-dependency, and a dependency cycle raise in
    normalize_plan_nodes before _collect_plan_errors can run, so a
    plan carrying BOTH a structural defect and a stand-in/placeholder
    fake was rejected for the structure alone — the repair round
    fixed the structure and returned the same fake (the masking
    family; retry #15 fixed exactly this for unknown dependencies in
    the same function, and this helper gives the other three raises
    the same treatment). When the collected content records are
    non-empty, the rejection is the numbered form with the structural
    record first; when they are empty, the bare structural rejection
    is returned unchanged. A failure inside the enrichment falls
    back to the bare rejection — enrichment must never mask the
    structural error with a crash. (_ancestor_node_ids is
    visited-set based, so the collector is safe on the cyclic plans
    this helper exists for.)
    """
    try:
        records, _cheap = _collect_plan_error_records(
            nodes,
            repo_root,
            goal_text,
            workspace,
            index=repo_index,
            plan_context="enrichment",
        )
    except Exception:
        records = []
    if not records:
        return _attach_plan_errors(
            LLMResponseError(structural.message), [structural]
        )
    return _numbered_rejection([structural] + records)


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
        raise _structural_rejection(
            PlanError(
                code="duplicate_node_ids",
                message="Planner response contained duplicate node ids.",
                stage=STAGE_SCHEMA,
            ),
            nodes,
            repo_root,
            goal_text,
            workspace,
            repo_index,
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
                raise _structural_rejection(
                    PlanError(
                        code="self_dependency",
                        message=f"Planner node '{node.id}' cannot depend on itself.",
                        node_id=node.id,
                        stage=STAGE_SCHEMA,
                    ),
                    nodes,
                    repo_root,
                    goal_text,
                    workspace,
                    repo_index,
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
            nodes,
            repo_root,
            goal_text,
            workspace,
            index=repo_index,
            plan_context="enrichment",
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
        raise _structural_rejection(
            PlanError(
                code="dependency_cycle",
                message="Planner response contained a dependency cycle.",
                stage=STAGE_SCHEMA,
            ),
            nodes,
            repo_root,
            goal_text,
            workspace,
            repo_index,
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


# Node fields a model may nest inside "inputs" (cycle 9, Track A):
# gemma3:27b's complete plans put outputs/acceptance/criteria inside
# the large scaffold node's "inputs" object — the values shape-perfect,
# one level too deep — and planning repair never fixed the nesting
# across two rounds. The envelope schema cannot prevent it ("inputs"
# is a free-form object by design; a grammar over it cannot express
# "these keys belong one level up"), so the parse seam repairs it
# deterministically, the way the payload channel repairs heredocs.
_RESERVED_NODE_FIELDS = ("outputs", "acceptance", "criteria")


def _reserved_field_shape_ok(field: str, value: Any) -> bool:
    """True when ``value`` has the shape ``field`` carries at node level.

    outputs/acceptance are string lists (a bare string is the shape the
    parse seam's own coercions already accept for them at top level);
    criteria is a list of criterion objects — dicts carrying a string
    "type" — or a single such dict (the bare-dict shape the same
    coercions accept). Anything else is not the field's shape: a claw
    input that merely shares the name is left alone.
    """
    if field in ("outputs", "acceptance"):
        if isinstance(value, str):
            return True
        return isinstance(value, list) and all(
            isinstance(item, str) for item in value
        )
    if isinstance(value, dict):
        return isinstance(value.get("type"), str)
    return isinstance(value, list) and all(
        isinstance(item, dict) and isinstance(item.get("type"), str)
        for item in value
    )


def _nested_field_hoistable(node: dict[str, Any], field: str) -> bool:
    """True when ``field`` sits in node's inputs in hoistable form.

    All-or-nothing per field: the field is present in "inputs", the
    node does NOT already carry it at its own level, and the nested
    value has the field's shape. Both levels present, or a value that
    is not the field's shape, is not hoistable — the node is left
    exactly as written and the nesting rejection proceeds as before.
    """
    inputs = node.get("inputs")
    if not isinstance(inputs, dict) or field not in inputs:
        return False
    if field in node:
        return False
    return _reserved_field_shape_ok(field, inputs[field])


def _hoist_nested_node_fields(nodes_raw: list[Any]) -> None:
    """Move hoistable reserved fields out of "inputs" to the node level.

    Runs on the raw node dicts in parse_planner_response, before the
    coercions, schema validation, and error collection, so every later
    stage sees the node the model meant to write. _duplicate_key_errors
    applies the same gate when deciding whether nesting is an error at
    all, so a hoisted field is never also named as a nesting defect.
    """
    for node in nodes_raw:
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field in _RESERVED_NODE_FIELDS:
            if _nested_field_hoistable(node, field):
                node[field] = inputs.pop(field)


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

    Cycle 9: nesting that the deterministic hoist can repair (right
    shape, no top-level twin — see _hoist_nested_node_fields) is not
    reported as an error here at all; only a nesting the hoist must
    leave alone is named.

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
                # Cycle 9: a nesting the deterministic hoist can repair
                # (right shape, no top-level twin — see
                # _nested_field_hoistable) is not an error: the parse
                # seam hoists it before validation. Only a nesting the
                # hoist must leave alone is named here — and an object
                # with duplicate keys is always left alone: it is not
                # one node but several collapsed into one (retry #15),
                # the parse raises on the collapse regardless, and its
                # nesting is part of that wreckage, not a hoistable
                # single-node defect.
                nested_fields.update(
                    field
                    for field in ("outputs", "acceptance", "criteria")
                    if field in inputs
                    and not (not dups and _nested_field_hoistable(obj, field))
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
    pinned_contract: list[dict[str, Any]] | None = None,
    clauses: list[dict[str, Any]] | None = None,
    skeleton: bool = False,
    clause_mode: str = "full",
    clause_required: Collection[str] | None = None,
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Validate raw planner output into normalized PlanNodes plus metadata.

    ``clause_mode`` (campaign build #7) selects the clause rule
    the collector applies when ``clauses`` is supplied: "full"
    (the initial planner's rule — every clause implemented and
    verified across the plan) or "delta" (the re-plan rule —
    every node declares a mapping, and the clauses named in
    ``clause_required``, the brief's open set, are implemented
    and verified within the delta). The default is "full", so
    every existing caller is judged exactly as before.

    ``repo_root`` overrides the repo the grounding check reads; by default
    it is the checkout holding this package. ``goal_text`` is the goal the
    plan serves; when it names an existing API route, the plan must engage
    that route (anti-fake rule). ``workspace`` is the run-outputs
    directory; when known, a repo-tree path named under it is rejected
    as a wrong-location hit. ``pinned_contract`` is the module
    contract pinned by an earlier accepted plan version (the re-plan
    path): when given, it governs — the payload's contents are
    validated against it, a payload contract that differs from it is
    rejected (contract_changed), and the returned metadata carries
    the pin. Without it, the payload's own top-level "contract" (if
    any) is validated and written into the metadata under
    "module_contract"; that key is system-owned either way — a
    model-supplied metadata value under it never survives.
    When no pin governs and the payload declares no contract
    (or omits an entry), the missing entries are derived from
    the plan's own coupling (_derive_contract_entries) and
    pinned the same way. ``clauses`` (campaign build #3) is the
    frozen contract-clause list the callers extracted from the
    goal-named spec: when supplied, the clause-coverage rule
    judges the plan's per-node inputs.clauses declarations
    against it; when omitted, that rule does not run at all.
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
            views,
            repo_root,
            goal_text,
            workspace,
            index=repo_index,
            plan_context="enrichment",
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

    # Deterministic nesting hoist (cycle 9): a reserved node field
    # (outputs/acceptance/criteria) nested inside "inputs" in the
    # field's own shape, with no top-level twin, is moved to the node
    # level here — before the coercions below and every later stage —
    # so validation judges the node the model meant to write.
    # _duplicate_key_errors applied the same gate above, so a hoisted
    # field was never named as a nesting error either.
    _hoist_nested_node_fields(nodes_raw)

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

    # System-owned delta fields (planner schema redesign,
    # 2026-11-19): "responds_to" / "differs_by" are dropped from the
    # raw nodes here, at the same raw-dict seam as the
    # normalizations above. They are the repair path's own
    # bookkeeping — it writes them onto materialized PlanNodes when
    # it splices a repair — and the two repeat checks
    # (judge.validate_delta_plan, loop's _find_repeated_approach)
    # read them as the system's note. Letting model text land in
    # them made both checks pass on any prose the model cared to
    # write, and the fields were the pairing run's degeneration
    # site (one value: 29,358 chars of negation loop, zero complete
    # nodes). The envelope schema no longer declares them; a model
    # that emits them anyway has them stripped, not judged.
    for node in nodes_raw:
        if isinstance(node, dict):
            node.pop("responds_to", None)
            node.pop("differs_by", None)
            # "_materialized_from" is the same class of system-owned
            # bookkeeping, one level down: materialization stamps it
            # on a write node's inputs when the node's content was
            # DERIVED from an edit form (anchored / scoped edits), and
            # the modify-path rule (campaign build #2) exempts
            # edit-derived content. A model that emits the key itself
            # could exempt its own whole-file rewrite, so the raw
            # value is stripped here — only materialization may set
            # it, later in this same function.
            raw_inputs = node.get("inputs")
            if isinstance(raw_inputs, dict):
                raw_inputs.pop("_materialized_from", None)

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
    contract_records, effective_contract = _contract_parse_stage(
        payload, pinned_contract
    )
    if pinned_contract is None and not contract_records:
        # Contract derivation (2026-11-23): no pin governs and
        # the payload's declaration (if any) was shape-clean —
        # fill the entries it omitted from the plan's own
        # coupling before validation, so the coherence checks
        # judge a contract on every coupled plan instead of
        # planning dying on a declaration the models
        # measurably do not emit.
        effective_contract = _derive_contract_entries(
            nodes, effective_contract, repo_root
        )
    module_cache = _ModuleSourceCache()
    intent_corrections: list[dict[str, Any]] = []
    if skeleton:
        # Campaign build #11: deterministic skeleton-intent
        # correction — a declared content intent that contradicts
        # the tree on disk is corrected mechanically HERE, before
        # the remaining validation judges the attempt, so the
        # model never spends a repair round re-deriving a fact
        # the system already knows (arm #20: four of five
        # skeleton attempts died on "create" declared for files
        # the standing tree already held). The corrections are
        # recorded, never silent: they ride the returned metadata
        # on acceptance and the rejection itself (the
        # _attach_intent_corrections channel) otherwise. The one
        # module cache serves both the correction and the
        # validators, so each module file is still read at most
        # once per parse.
        intent_corrections = _correct_skeleton_intents(
            nodes, repo_root, index=index, modules=module_cache
        )
    relabeled_nodes = _relabel_nodes_for_errors(nodes, id_labels)
    records, cheap = _collect_plan_error_records(
        relabeled_nodes,
        repo_root,
        goal_text,
        workspace,
        index=index,
        modules=module_cache,
        contract=effective_contract,
        clauses=clauses if clauses is not None else _CLAUSES_UNSUPPLIED,
        skeleton=skeleton,
        clause_mode=clause_mode,
        clause_required=clause_required,
    )
    records = contract_records + records
    if skeleton:
        # Campaign build #5: a content-less write node must declare
        # how its content will be filled (the skeleton's content
        # intent). A write node that already carries content or an
        # edit form is pre-filled and needs no intent.
        records = records + _skeleton_intent_basic_records(relabeled_nodes)
    if records:
        rejection = _numbered_rejection(records, cheap=cheap)
        if intent_corrections:
            _attach_intent_corrections(rejection, intent_corrections)
        raise rejection

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {"estimated_steps": len(nodes)}
    # The module contract key is system-owned (the status /
    # differs_by rule): the validated contract — or the pin — is
    # the only value that may sit under it; a model-supplied one is
    # discarded, never trusted as a pin.
    if effective_contract:
        metadata[CONTRACT_METADATA_KEY] = effective_contract
    else:
        metadata.pop(CONTRACT_METADATA_KEY, None)
    # The intent-corrections key is system-owned like the module
    # contract key (campaign build #11): stamped from this parse's
    # own corrections — a model-supplied value under it never
    # survives, and a parse that corrected nothing carries none.
    metadata.pop(INTENT_CORRECTIONS_METADATA_KEY, None)
    if intent_corrections:
        metadata[INTENT_CORRECTIONS_METADATA_KEY] = intent_corrections

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


def _criterion_path_is_credited(
    criterion_path: str,
    node: PlanNode,
    plan_written: set[str] | None = None,
) -> bool:
    """True if a file criterion path is credited to this node or plan.

    The crediting rule (cycle 10): the path is one of the node's own
    declared strings (the historical per-node rule), OR it names —
    under _same_path's spelling-insensitive comparison — a file the
    node itself demonstrably writes (_node_written_paths: a relative
    redirect/download target in its command credits the absolute
    criterion path naming the same file), OR, when the caller threads
    the plan's written set in, a file ANY node in the plan
    demonstrably writes. Declared outputs alone are not a write and
    earn nothing here beyond the historical substring rule: a bare
    stdout command (curl with no redirect and no -o) writes no file,
    however the outputs list describes it.
    """
    if _criterion_path_is_node_path(criterion_path, node):
        return True
    for target in _node_written_paths(node):
        if _same_path(criterion_path, target):
            return True
    if plan_written:
        for target in plan_written:
            if _same_path(criterion_path, target):
                return True
    return False


def _custom_checker_registry_text() -> str:
    """The registered custom checkers, as error text (cycle 9).

    Derived from the live registry at call time: the names verbatim
    when any are registered; an explicit unavailability statement when
    none are — which is every production run today (register_checker
    has no production caller, and the planner prompt already bans the
    type without saying why or what to use instead — qwen2.5-coder
    reached for it in 4 nodes across 2 outputs, twice with a "command"
    it expected to be run). When the registry is empty the text names
    the alternatives, including exit_code with its own "command" —
    the capability the model was actually reaching for.
    """
    names = sorted(custom_checkers())
    if names:
        return "registered checkers: " + ", ".join(repr(n) for n in names)
    return (
        "no custom checkers are registered, so custom_check is "
        "unavailable — use exit_code (a criterion may carry its own "
        "'command'), file_exists, file_matches, metric, or tests_pass "
        "instead"
    )


# The constructive pointer for file criteria (cycle 11), shared by the
# per-node criterion error and the plan-level unwritten_file errors.
# The write forms named are exactly the cycle-10 crediting set
# (_node_written_paths): the forms the scaffold claw and the bash
# claw's real shell demonstrably perform. The live-endpoint half
# answers re-run #7's Qwen shape (a bare curl naming a response
# file): the response is on stdout, no file exists to check, and the
# measurable verification is the curl node's own exit code.
_FILE_CRITERION_GUIDANCE = (
    "name a file some node in the plan demonstrably writes (a "
    "scaffold/file_writer target, a heredoc target, a bash redirect, "
    "a download target such as curl -o or wget -O, a tee target, a "
    "dd of= target, a cp/mv destination, or a touch target), a file a "
    "node runs or reads in its command, or a file that already "
    "exists — to verify a live endpoint response, use an exit_code "
    "criterion on the node whose command curls the endpoint instead: "
    "stdout is the response and a bare curl writes no file"
)


def _usable_criterion_error(
    criterion: Any,
    node: PlanNode,
    repo_root: str | Path | None = None,
    plan_written: set[str] | None = None,
) -> str | None:
    """Return an error string if the criterion is unusable, else None.

    ``plan_written`` (cycle 10) is the plan's demonstrable written set
    (the union of _node_written_paths over every node), threaded in by
    _collect_plan_error_records: a file_exists/file_matches path is
    also usable when some node in the plan demonstrably writes that
    file (an earlier node's redirect, download target, tee target, or
    heredoc target), even when the checking node never names it. None
    (direct calls) keeps the per-node rule exactly as before.
    """
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
        return (
            f"unknown criterion type: {ctype!r} — the criterion types "
            f"are: {', '.join(sorted(_CRITERION_TYPES))}"
        )
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
        if not _criterion_path_is_credited(path, node, plan_written):
            return (
                f"file_exists path {path!r} is not a file this node "
                f"writes or runs — {_FILE_CRITERION_GUIDANCE}"
            )
        return None
    if ctype == CRITERION_FILE_MATCHES:
        path = criterion.get("path")
        if not isinstance(path, str) or not path:
            return "file_matches criterion needs a non-empty 'path'"
        if "sha256" not in criterion and "json_keys" not in criterion:
            return "file_matches criterion needs 'sha256' or 'json_keys'"
        if not _criterion_path_is_credited(path, node, plan_written):
            return (
                f"file_matches path {path!r} is not a file this node "
                f"writes or runs — {_FILE_CRITERION_GUIDANCE}"
            )
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
                "(that claw reports no metrics, so a metric criterion can "
                "never be measured on it — use exit_code (for a live "
                "response check, an exit_code criterion on the node whose "
                "command produces the response, or one carrying its own "
                "'command') or file_exists instead)"
            )
        return None
    if ctype == CRITERION_TESTS_PASS:
        suite = criterion.get("suite")
        if not isinstance(suite, str) or not suite:
            return "tests_pass criterion needs a non-empty 'suite'"
        if not _criterion_path_is_node_path(suite, node):
            # Cycle 9: state the rule in the validator's own terms —
            # qwen2.5-coder named the suite as an absolute directory
            # its command never mentions ("... -s tests"), and the old
            # text named no acceptable form, so the repair round had
            # nothing to steer by.
            return (
                f"tests_pass suite {suite!r} is not a file this node writes or runs"
                " — the suite must be named by this node itself: a test "
                "file this node writes (its file_path, a redirect or "
                "heredoc target) or a path that appears in this node's "
                "command (for example the exact directory its unittest "
                "command runs against); a path only another node "
                "touches does not count"
            )
        return None
    # CRITERION_CUSTOM_CHECK: usable only when the checker is registered.
    name = criterion.get("name")
    if not isinstance(name, str) or not name:
        return (
            "custom_check criterion needs a non-empty 'name' naming a "
            f"registered checker — {_custom_checker_registry_text()}"
        )
    if name not in custom_checkers():
        return (
            f"custom_check {name!r} is not a registered checker — "
            f"{_custom_checker_registry_text()}"
        )
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

# Campaign build #2 (step 1.5): the second edit form. Where the
# anchored form names ONE Python definition, the scoped form names
# exact text spans — the repair seam's primitive (f4f9e35) promoted
# to a plan form, for any file type: a write node for an EXISTING
# file carries "edits" (a list of search/replace pairs) instead of
# "content", and materialization applies them to the file's current
# text with the same deterministic machinery (exact match, exactly
# once, all-or-nothing), so the executor again sees an ordinary
# full-content node and everything outside the spans is preserved
# by construction.
_SCOPED_EDIT_SHAPE_EXAMPLE = (
    'SCOPED EDITS — to change an existing file without retyping '
    'it, a scaffold node\'s inputs may be {"file_path": "<path>", '
    '"edits": [{"search": "<text copied exactly from the current '
    'file>", "replace": "<the new text>"}, ...]} with NO "content": '
    'each search text must occur exactly once in the file as it '
    'stands, the system applies every edit mechanically, and '
    'everything outside the searched spans is preserved exactly. '
    'Use this (or an anchored edit for one Python definition) '
    'whenever the target file already exists; reserve whole-file '
    '"content" for creating a NEW file.'
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


def _delta_syntax_detail(content: str) -> str:
    """The dedented delta's syntax error, or "" when it parses.

    Form: "<parser message> on line <n>: '<the line's text>'" — the
    line number and text are the content's own (dedent preserves
    both), so the model can find the exact line it wrote. The
    materialize rejection for an unparseable 'replace' delta names
    this defect: without it the rejection can only say "not a
    definition", which misdiagnoses a complete definition carrying
    a typo as a fragment (re-run #10 arm 3: gemma4's bracket
    mismatch, repeated verbatim 3x under the fragment text).
    """
    try:
        ast.parse(textwrap.dedent(content))
    except SyntaxError as exc:
        detail = _syntax_error_detail(exc)
        lines = content.splitlines()
        if exc.lineno is not None and 1 <= exc.lineno <= len(lines):
            return f"{detail}: {lines[exc.lineno - 1].strip()!r}"
        return detail
    return ""


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
            # Cycle 14: the dedented content does not parse at all,
            # and the old text named only the fragment defect — but
            # the recorded failure (re-run #10 arm 3) was a COMPLETE
            # definition carrying a bracket mismatch: told to fix a
            # defect it did not have, the model repeated the content
            # verbatim 3x. Name the parser's own defect (detail +
            # the content line it lands on) first; the complete-
            # definition rule stays, as the frame the fix lands in.
            detail = _delta_syntax_detail(content)
            detail_part = f" ({detail})" if detail else ""
            return (
                f"Planner node '{label}' anchors a 'replace' edit on "
                f"{anchor!r} in {target!r}, but the content does not "
                f"parse as Python{detail_part} — for 'replace' the "
                f"content is the complete new version of {anchor!r} "
                "(decorators if any, the def/class line, and the whole "
                "body), at its indentation in the file, parsing exactly "
                "as written: find the defect named above (a bracket, "
                "quote, or indentation slip is the usual cause) and "
                "fix that line — not a fragment of its body and not a "
                "comment about it.",
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
    if position == "replace":
        # Cycle 12: an elision marker inside the replacement body —
        # a comment standing in for the rest of the definition —
        # passes every check above (the content IS one complete
        # definition by shape) and the name-retention check below
        # (the definition survives; its branches do not). The delta
        # is judged here, before the splice, where the body is still
        # visible as a body. See _anchored_elision_marker.
        marker = _anchored_elision_marker("\n".join(delta_lines))
        if marker is not None:
            return (
                f"Planner node '{label}' anchors a 'replace' edit on "
                f"{anchor!r} in {target!r}, but the replacement body "
                f"elides the rest of {anchor!r} — it carries the "
                f"elision marker {marker!r}, a comment standing in "
                "for code the plan does not write. A 'replace' content "
                "substitutes the WHOLE definition: the splice keeps "
                "only what the content contains, so the code the "
                "marker stands in for is deleted (the definition "
                "keeps its name and loses its branches — a retention "
                "check on definition names cannot see the loss, and "
                "a file_exists criterion cannot either). Return the "
                f"complete new version of {anchor!r}: the current "
                "body carried over in full, with the goal's change "
                "applied — every branch the goal does not change "
                "keeps its real code, never a comment promising it.",
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
    # Provenance for the modify-path rule (campaign build #2): this
    # content was derived from an edit form, not authored whole —
    # the validator exempts it. Set only here (parse strips the key
    # from raw model nodes), never by the model.
    inputs["_materialized_from"] = "anchored_edit"
    sources[resolved] = materialized
    return None


def _materialize_scoped_edit(
    label: str,
    inputs: dict[str, Any],
    repo_root: str | Path | None,
    sources: dict[Path, str],
) -> tuple[str, bool] | None:
    """Materialize one node's scoped-edit form in place.

    Mirrors _materialize_scaffold_edit's contract: on success the
    node's inputs hold the full materialized file as "content"
    (the "edits" key removed, "_materialized_from" stamped) and
    None is returned; on failure an (error, False) pair — a
    scoped-edit failure is never the placeholder class. The base
    text is the target's current text from the shared ``sources``
    chain (an earlier node's write in this same plan wins over the
    file on disk), application is the repair seam's deterministic
    machinery (exact match, exactly once, all-or-nothing), and the
    result feeds the chain for later nodes targeting the same file.
    """
    if "anchor" in inputs or "position" in inputs:
        return (
            f"Planner node '{label}' carries both an anchored edit "
            "('anchor'/'position') and scoped edits ('edits') — the "
            "two edit forms are alternatives: an anchored edit "
            "rewrites ONE Python definition by name, scoped edits "
            "rewrite exact text spans in any file. Keep one form.",
            False,
        )
    if "content" in inputs:
        return (
            f"Planner node '{label}' carries both 'content' and "
            "'edits' — an edit-form node carries only 'file_path' "
            "and 'edits' (the system produces the content by "
            "applying the edits to the current file); a create node "
            "carries only 'file_path' and 'content'. Drop one.",
            False,
        )
    target = inputs.get("file_path") or inputs.get("path")
    if not isinstance(target, str) or not target:
        return (
            f"Planner node '{label}' carries 'edits' but names no "
            "'file_path' — scoped edits change the existing file "
            "named in 'file_path'.",
            False,
        )
    edits_raw = inputs.get("edits")
    if not isinstance(edits_raw, list) or not edits_raw:
        return (
            f"Planner node '{label}' carries 'edits' that is not a "
            "non-empty list — 'edits' is a list of "
            '{"search": "<text copied exactly from the current '
            'file>", "replace": "<the new text>"} objects, one per '
            "change. To create a new file instead, scaffold its "
            "full 'content' and drop 'edits'.",
            False,
        )
    edits: list[ScopedEdit] = []
    for position, entry in enumerate(edits_raw, start=1):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("search"), str)
            or not entry.get("search")
            or not isinstance(entry.get("replace"), str)
        ):
            return (
                f"Planner node '{label}' has an 'edits' entry "
                f"(#{position}) that is not a usable edit: every "
                'edit is {"search": "<non-empty text copied exactly '
                'from the current file>", "replace": "<the new '
                'text>"} — the search text must be non-empty (an '
                "empty search is not a create form; scaffold new "
                "files with 'content').",
                False,
            )
        edits.append(
            ScopedEdit(search=entry["search"], replace=entry["replace"])
        )
    resolved = _resolve_plan_target(target, resolve_repo_root(repo_root))
    if resolved in sources:
        source = sources[resolved]
    else:
        try:
            source = resolved.read_text(encoding="utf-8")
        except OSError:
            return (
                f"Planner node '{label}' has scoped edits for "
                f"{target!r}, which does not exist — scoped edits "
                "change an existing file; to create a new file, "
                "scaffold its full 'content' instead, without "
                "'edits'.",
                False,
            )
    try:
        materialized = apply_scoped_edits(source, edits)
    except ScopedEditError as exc:
        return (
            f"Planner node '{label}' has scoped edits for {target!r} "
            f"that do not apply ({exc.kind}): {exc} — every search "
            "text must be copied character-for-character from the "
            "file as it stands and occur exactly once in it; widen "
            "a search text with more surrounding lines until it is "
            "unique, and never overlap two edits.",
            False,
        )
    inputs["content"] = materialized
    inputs.pop("edits", None)
    # Provenance for the modify-path rule, as in the anchored form:
    # this content was derived from edits, not authored whole.
    inputs["_materialized_from"] = "scoped_edits"
    sources[resolved] = materialized
    return None


def _materialize_plan_edits(
    nodes_raw: list[Any], repo_root: str | Path | None
) -> None:
    """Rewrite edit-form scaffold nodes to full-content nodes.

    Runs on the raw node dicts in parse_planner_response, after
    interpreter normalization and before normalize_plan_nodes — the
    same in-place raw-dict seam. A node is an edit-form node when it
    is a scaffold/file_writer node whose inputs carry an "anchor"
    (or a stray "position" — a malformed edit, named as such) or
    "edits" (the scoped form, campaign build #2). Legacy full-content
    nodes are untouched. Errors from every edit node are collected
    into ONE numbered rejection in the house format, naming the
    model's own raw node ids; when every error is a placeholder
    delta, the rejection is cheap (the retry-#17 placeholder rule:
    a comment stub costs the model nothing to replace with real
    code, so the re-prompt is free) — and its records carry the
    'placeholder_delta' code, so the repair loop answers it with
    the grounded skeleton. Scoped-edit errors carry 'scoped_edit'.
    """
    errors: list[tuple[str, str]] = []
    sources: dict[Path, str] = {}
    for node_position, raw in enumerate(nodes_raw, start=1):
        if not isinstance(raw, dict):
            continue
        if raw.get("assigned_claw") not in ("scaffold", "file_writer"):
            continue
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if (
            "anchor" not in inputs
            and "position" not in inputs
            and "edits" not in inputs
        ):
            continue
        raw_id = raw.get("id")
        label = raw_id if isinstance(raw_id, str) else f"position {node_position}"
        # Campaign build #3: the target's text BEFORE this node's
        # edit materializes (an earlier node's write in this plan
        # wins over disk, exactly as in the materializers), kept
        # for the grounding check below.
        target_value = inputs.get("file_path") or inputs.get("path")
        prior_source: str | None = None
        if isinstance(target_value, str) and target_value.endswith(".py"):
            resolved_prior = _resolve_plan_target(
                target_value, resolve_repo_root(repo_root)
            )
            if resolved_prior in sources:
                prior_source = sources[resolved_prior]
            else:
                try:
                    prior_source = resolved_prior.read_text(encoding="utf-8")
                except OSError:
                    prior_source = None
        if "edits" in inputs:
            outcome = _materialize_scoped_edit(
                label, inputs, repo_root, sources
            )
            if outcome is not None:
                errors.append((outcome[0], "scoped_edit"))
                continue
        else:
            outcome = _materialize_scaffold_edit(label, inputs, repo_root, sources)
            if outcome is not None:
                error, placeholder = outcome
                errors.append(
                    (error, "placeholder_delta" if placeholder else "anchored_edit")
                )
                continue
        # Campaign build #3: edit grounding. The edit applied; now
        # its content must answer to the file it lands in. Arm #11's
        # anchored edits applied cleanly and then referenced
        # self.store for self.server.store — form success, content
        # failure, and the server hung. A reference the edit
        # INTRODUCES that the target cannot resolve (an attribute
        # the class never assigns, a call to a name the module does
        # not define or import) is a plan error here, in the same
        # rejection, with the same authored gating as every other
        # materialization defect.
        materialized_content = inputs.get("content")
        if prior_source is not None and isinstance(materialized_content, str):
            violations = edit_grounding_violations(
                prior_source, materialized_content
            )
            if violations:
                errors.append(
                    (
                        f"Planner node '{label}' has an edit for "
                        f"{target_value!r} whose new code introduces "
                        "ungrounded references: "
                        + "; ".join(violations)
                        + " — reference only attributes the class "
                        "assigns (in any of its methods, or through a "
                        "base class) and names the module defines or "
                        "imports; if the reference is the right one, "
                        "assign the attribute in the class as part of "
                        "the edit.",
                        "ungrounded_reference",
                    )
                )
    if not errors:
        return
    # Structured records (A/B refactor): a placeholder delta carries
    # its own code — it is cheap, and its repair round answers with
    # the grounded skeleton, not the materialization steering block.
    records = [
        PlanError(
            code=code,
            message=error,
            node_id=_node_id_from_message(error),
            stage=STAGE_MATERIALIZE,
        )
        for error, code in errors
    ]
    suffix_parts: list[str] = []
    if any(code in ("anchored_edit", "placeholder_delta") for _error, code in errors):
        suffix_parts.append(_EDIT_SHAPE_EXAMPLE)
    if any(code == "scoped_edit" for _error, code in errors):
        suffix_parts.append(_SCOPED_EDIT_SHAPE_EXAMPLE)
    raise _numbered_rejection(
        records,
        cheap=all(code == "placeholder_delta" for _error, code in errors),
        suffix="\n" + "\n".join(suffix_parts),
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

def _sed_ban_errors(
    nodes: list[PlanNode],
    goal_text: str | None = None,
    index: Any | None = None,
) -> list[str]:
    """Hard-reject sed -i edits of Python files, whatever the pattern.

    Reversal of the retry-#18 pin (approved by Pat 2026-11-19).
    Retry #18 judged sed -i edits case by case: a search pattern
    that occurs in the file passed, and _sed_noop_errors still
    rejects the ones that do not. But a judged-and-passed sed -i is
    still an ungrounded rewrite — it bypasses the anchored-edit
    machinery, its result is never checked against the file's real
    contents before it lands, and sed-shaped edits have clobbered
    modules in the campaign record. So the rule is now categorical:
    any in-place sed (an -i flag, a bundle containing i, -iSUFFIX,
    or --in-place) whose target path ends in .py is rejected by
    name. The ban does not consult the disk — a .py the plan itself
    creates is covered — and it does not reach sed without -i or
    sed -i on non-.py files; those keep their previous behavior,
    the no-op judgment included.
    """
    errors: list[str] = []
    grounded = _grounded_move_text(index, goal_text)
    for node in nodes:
        if (node.assigned_claw or "") not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        shell = _strip_heredoc_bodies(command)
        for _script, target in _sed_in_place_edits(shell):
            if Path(target).suffix.lower() != ".py":
                continue
            errors.append(
                f"Planner node '{node.id}' runs sed -i on {target!r}: "
                f"in-place sed edits of Python files are banned — the "
                f"edit bypasses the anchored-edit machinery, so nothing "
                f"checks it against the file's real contents before it "
                f"lands. Write the code with the scaffold claw, or a "
                f"bash heredoc carrying the full intended content."
                f"{grounded}"
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
    node: PlanNode,
    repo_root: str | Path | None = None,
    plan_written: set[str] | None = None,
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
        error = _usable_criterion_error(
            criterion, node, repo_root, plan_written=plan_written
        )
        if error is not None:
            errors.append(
                f"Planner node '{node.id}' has an unusable criterion: {error}."
            )
    return errors


def _suite_criterion_command_errors(nodes: list[PlanNode]) -> list[str]:
    """Reject a test-suite run attached to an exit_code criterion's command.

    Cycle 12 (re-run #8): qwen2.5-coder:14b's accepted plans attached
    the full suite (~477s) as an exit_code criterion's own "command"
    in both arms (on the test-writing node in Arm 1, on the edit node
    in Arm 3). A criterion command is run by the measurement step
    under CRITERION_COMMAND_TIMEOUT_S (measure._eval_exit_code), so a
    suite run there can only time out — and a measurement timeout is
    judged RETRY (the timeout path never re-plans), so both runs
    repeated the timeout and escalated with the suite never
    completing a run anywhere. The suite has two sound placements
    and the error names both: as the node's OWN command (where the
    executor's suite floor gives it room), measured by an exit_code
    criterion carrying no command of its own; or as a tests_pass
    criterion, the type built for suites.

    Detection is the executor's own suite definition,
    _command_runs_full_suite — the same judgment that raises a suite
    node's timeout floor — so "suite run" means one thing in the
    codebase: unittest discovery, a bare unittest run, or an
    unscoped pytest run. A scoped run (one test module, one test
    file, one node id) is a quick probe and is untouched, as are the
    recorded probe commands (curl, grep, nvidia-smi). The rejection
    is cheap (registered in _collect_plan_error_records): moving the
    suite from the criterion onto the node's own command is a
    mechanical relocation, the placeholder_value rationale.
    """
    budget = (
        f"{int(CRITERION_COMMAND_TIMEOUT_S)}s"
        if float(CRITERION_COMMAND_TIMEOUT_S).is_integer()
        else f"{CRITERION_COMMAND_TIMEOUT_S}s"
    )
    errors: list[str] = []
    for node in nodes:
        for criterion in node.criteria or []:
            if not isinstance(criterion, dict):
                continue
            if criterion.get("type") != CRITERION_EXIT_CODE:
                continue
            command = criterion.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            if not _command_runs_full_suite(command):
                continue
            errors.append(
                f"Planner node '{node.id}' has an unusable criterion: "
                f"its exit_code criterion command {command!r} runs the "
                "full test suite — a criterion command is run by the "
                f"measurement step under a {budget} timeout, so a suite "
                "run attached to a criterion can only time out, and a "
                "measurement timeout is retried rather than re-planned. "
                "Run the suite as the node's own command instead and "
                "measure it with an exit_code criterion carrying NO "
                "command of its own, or use a tests_pass criterion "
                "naming the suite this node runs."
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
    or &&-chained command is flagged. (Cycle 10: tee targets are in
    the written set too, so a tee write to /tmp is flagged like a
    redirect.)
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


# A mkdir invocation inside a bash command: `mkdir -p <dir>` (also
# `mkdir <dir>`). Args run to the next shell operator or newline.
_MKDIR_ARGS_RE = re.compile(r"\bmkdir\b(?P<args>[^;&|\n]*)")


def _mkdir_created_targets(command: str) -> set[str]:
    """Literal directory paths a bash command creates with mkdir.

    Heredoc bodies and quoted strings are stripped first: a mkdir
    inside file content or an echo string is data, not a command.
    Only literal targets count — a path carrying shell expansion
    ($, backtick, glob) expands to a name the validator cannot
    know, so it is not credited (the same rule _node_written_paths
    applies to write targets).
    """
    shell = _strip_heredoc_bodies(command)
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", shell)
    created: set[str] = set()
    for match in _MKDIR_ARGS_RE.finditer(unquoted):
        for token in match.group("args").split():
            if token.startswith("-"):
                continue
            if any(char in token for char in "$`*?[]"):
                continue
            created.add(token)
    return created


def _plan_created_directories(
    nodes: list[PlanNode], root: Path
) -> dict[str, set[str]]:
    """Map node id -> repo-relative directories that node creates.

    Only bash/shell_exec nodes can create a directory (a mkdir in
    their command). Directories outside the repo tree are irrelevant
    to the missing-directory check and are dropped.
    """
    created: dict[str, set[str]] = {}
    for node in nodes:
        if getattr(node, "assigned_claw", "") not in ("bash", "shell_exec"):
            continue
        inputs = getattr(node, "inputs", None) or {}
        command = inputs.get("command") or inputs.get("cmd")
        if not isinstance(command, str) or not command:
            continue
        dirs = {
            rel
            for target in _mkdir_created_targets(command)
            if (rel := _repo_relative_target(target, root)) is not None
        }
        if dirs:
            created[node.id] = dirs
    return created


def _ancestor_node_ids(nodes: list[PlanNode]) -> dict[str, set[str]]:
    """Map node id -> the ids it transitively depends on."""
    by_id = {node.id: node for node in nodes}
    ancestors: dict[str, set[str]] = {}

    def collect(node_id: str, seen: set[str]) -> set[str]:
        found: set[str] = set()
        node = by_id.get(node_id)
        if node is None:
            return found
        # Lenient node views (the duplicate-keys path) carry no
        # depends_on: they get no ancestors, hence no mkdir credit —
        # that path only enriches an already-certain rejection.
        for dep in getattr(node, "depends_on", None) or []:
            if dep in by_id and dep not in seen:
                found.add(dep)
                found |= collect(dep, seen | {dep})
        return found

    for node in nodes:
        ancestors[node.id] = collect(node.id, {node.id})
    return ancestors


def _directory_is_created(parent_rel: str, created: set[str]) -> bool:
    """True if a credited mkdir creates ``parent_rel``.

    ``mkdir -p D`` creates D and every ancestor of D (but not D's
    children), so a write is covered when its parent directory is
    a created directory or an ancestor of one.
    """
    return any(
        parent_rel == directory or directory.startswith(parent_rel + "/")
        for directory in created
    )


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
    CI.

    Executor alignment (2026-11-19, the pairing proof arm): the two
    write claws differ, so the check does too. The scaffold claw
    creates parent directories itself (tool_registry's scaffold
    handler runs parent.mkdir(parents=True, exist_ok=True) before
    writing), so a scaffold write into a new directory LANDS — it is
    not rejected for a missing parent, except freetalon/tests/ below,
    which is not merely missing but the wrong location. The bash claw
    runs a real shell, where a heredoc/redirect into a missing
    directory fails outright — those writes stay rejected UNLESS the
    plan itself creates the directory: a mkdir in a step the write
    transitively depends on (or earlier in the write's own command)
    is credited, because the executor runs dependencies first. The
    proof arm's model did exactly that (step_1 mkdir -p, every write
    depending on it) and was still rejected three times running —
    the check's own hint said "create the directory first" but the
    directory the plan created was never credited.
    """
    root = index.root if index is not None else resolve_repo_root(repo_root)
    created_by = _plan_created_directories(nodes, root)
    ancestors = _ancestor_node_ids(nodes)
    errors: list[str] = []
    for node in nodes:
        credited: set[str] = set()
        for ancestor in ancestors.get(node.id, set()):
            credited |= created_by.get(ancestor, set())
        credited |= created_by.get(node.id, set())
        for target in sorted(_node_written_paths(node)):
            rel = _repo_relative_target(target, root)
            if rel is None:
                continue
            parent = root / Path(rel).parent
            if parent.exists():
                continue
            if _directory_is_created(Path(rel).parent.as_posix(), credited):
                continue
            if rel.startswith("freetalon/tests/"):
                test_name = rel[len("freetalon/tests/"):]
                hint = (
                    f"Repo tests live at {root}/tests/, not freetalon/tests/ — "
                    f"write the test file to {root}/tests/{test_name}."
                )
            elif node.assigned_claw in ("scaffold", "file_writer"):
                # The scaffold claw creates the parent directory at
                # execution (see docstring), so this write lands.
                continue
            else:
                hint = (
                    f"Create the directory first — a bash step running "
                    f"mkdir -p that this step depends on — or write under "
                    f"an existing directory in the repo (repo root {root})."
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


# The bash claw runs its command through a real shell
# (tool_registry.bash_handler -> create_subprocess_shell), so every
# deterministic write form the shell performs is a write the executor
# will actually make: redirects and heredoc targets (above), plus —
# cycle 10 — download targets, tee targets, dd of=, and cp/mv/touch
# destinations. memory.py's _bash_write_targets extracts the same set
# for approach signatures; the two constructions agree on what a
# write is. Targets carrying shell expansion ($, backtick, globs) are
# NOT credited for these forms: the shell would expand them to a name
# the validator cannot know, and crediting a guessed name is how an
# unmeasurable criterion gets blessed.
_DOWNLOAD_TARGET_RE = re.compile(
    r"(?:-o|-O|--output|--output-document)[=\s]+"
    r"(\"[^\"]+\"|'[^']+'|[^\s;|&]+)"
)
_DD_TARGET_RE = re.compile(r"\bof=(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
_TEE_WRITTEN_RE = re.compile(r"\btee\s+(?:-\w+\s+)*(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
_WRITE_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
_EXPANSION_CHARS = "$`"


def _unquote_shell_target(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _bash_extra_write_targets(command: str) -> set[str]:
    """Write targets of a bash command beyond redirects/heredocs.

    Download targets are credited only in a segment whose program is
    curl or wget — a bare -o on some other program takes no file
    argument (unzip -o names no output), so crediting it would invent
    a write. Tee targets are scanned on the heredoc-stripped shell,
    so a 'tee' inside file content is not a command. cp/mv credit
    only the destination (the last token of the segment); touch
    credits its file argument.
    """
    targets: set[str] = set()

    def _add(token: str) -> None:
        target = _unquote_shell_target(token.strip())
        if (
            target
            and not target.startswith("/dev/")
            and not any(char in target for char in _EXPANSION_CHARS)
            and "*" not in target
            and "?" not in target
        ):
            targets.add(target)

    stripped = _strip_heredoc_bodies(command)
    for match in _TEE_WRITTEN_RE.finditer(stripped):
        _add(match.group(1))
    for match in _DD_TARGET_RE.finditer(stripped):
        _add(match.group(1))
    for segment in _WRITE_SEGMENT_SPLIT_RE.split(stripped):
        tokens = segment.split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        program = tokens[0].rsplit("/", 1)[-1]
        if program in ("curl", "wget"):
            for match in _DOWNLOAD_TARGET_RE.finditer(segment):
                _add(match.group(1))
        elif program in ("cp", "mv") and len(tokens) >= 3:
            _add(tokens[-1])
        elif program == "touch" and len(tokens) >= 2:
            for token in tokens[1:]:
                if not token.startswith("-"):
                    _add(token)
    return targets


def _node_written_paths(node: PlanNode) -> set[str]:
    """Paths a node writes: scaffold target, bash redirect/heredoc targets.

    A node's "outputs" list is NOT evidence — it is a claim about what
    the node produces, and a fake plan lists a file there without ever
    creating it (retry #8: node-1 listed health.json in outputs while
    echoing its answer to stdout). Only a real write counts: the scaffold
    claw's file_path, a redirect, or a heredoc target. Cycle 10: the
    bash branch also credits the shell's other deterministic write
    forms (download targets, tee, dd of=, cp/mv, touch — see
    _bash_extra_write_targets), mirroring what the executor's shell
    actually performs.
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
            written.update(_bash_extra_write_targets(command))
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
                f"file_exists criterion must {_FILE_CRITERION_GUIDANCE}. "
                "Listing the path in 'outputs' does not "
                "count: echoing an answer to stdout does not create the file."
            )
    return errors


def _unwritten_file_matches_errors(
    nodes: list[PlanNode], repo_root: str | Path | None
) -> list[str]:
    """Reject file_matches criteria on paths the plan never produces.

    The file_matches twin of _unwritten_file_errors (cycle 10).
    file_exists has had the plan-level check since retry #8;
    file_matches never did, so a file_matches path carried only by a
    node's "outputs" claim passed validation although nothing in the
    plan writes the file and the measurement could only ever report
    "does not exist" (the outputs-only hole: re-run #7's Qwen verify
    nodes named a workspace health_response.json that no node's
    command writes — a bare `curl` to stdout creates no file). The
    crediting rule is the same as file_exists': written by some node
    in the plan (the extended _node_written_paths set), named in a
    node's bash command, or already on disk. An outputs listing alone
    does not count.
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
            if criterion.get("type") != CRITERION_FILE_MATCHES:
                continue
            path = criterion.get("path")
            if not isinstance(path, str) or not path:
                continue  # _usable_criterion_error already flags this
            if "sha256" not in criterion and "json_keys" not in criterion:
                continue  # _usable_criterion_error already flags this
            if any(_same_path(path, target) for target in written):
                continue
            if any(_node_command_mentions_path(other, path) for other in nodes):
                continue
            if _path_exists_on_disk(path, repo_root):
                continue
            errors.append(
                f"Planner node '{node.id}' has a file_matches criterion on "
                f"{path!r}, but no node in the plan writes that file and it "
                "does not exist on disk — the file would be unmeasurable. A "
                f"file_matches criterion must {_FILE_CRITERION_GUIDANCE}. "
                "Listing the path in 'outputs' does not "
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

# The body of an echo/printf write, as a pattern fragment shared by
# the two echo-write detectors (_ECHO_WRITE_RE here, _LITERAL_WRITE_RE
# at the stand-in check): quoted segments are consumed whole — a ">"
# inside quotes is content, not the redirect; the old ".*?" body
# stopped at the first ">", truncating the body mid-quote so the
# quoted-segment scan found no closed quote and the write escaped
# detection entirely (or produced a garbage target). Unquoted runs
# stop at the redirect and at quote characters, which start a segment.
_ECHO_BODY_PATTERN = r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|[^>\"'])*?"

_ECHO_WRITE_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:echo|printf)\s+(?P<body>" + _ECHO_BODY_PATTERN + r")\s*(?P<op>>{1,2})\s*(?P<target>[^\s;&|]+)",
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


def _scaffold_unparseable_binding_errors(
    nodes: list[PlanNode],
    goal_text: str | None = None,
    index: Any | None = None,
) -> list[str]:
    """Name fabricated measurements inside scaffold content that does not parse.

    Retry #24 attempt 2: the scaffold content carried hardcoded
    stand-in values (gpu_temperature 25, ollama_reachable True) inside
    code that did not parse, and the rejection named only the parse
    error — the parse failure masked the stand-in detection, so the
    repair round fixed the syntax and kept the fabrication. The
    bindings scan (_hardcoded_measured_bindings) is textual, so it
    runs on unparseable content exactly as on parseable text; this
    check applies it to scaffold writes whose content fails the
    Python parse (the parse error itself is _scaffold_parse_errors'
    to name). Parseable content is untouched: a constant inside
    working code is the execution layer's judgment (the critic), and
    expected-value fixtures bind measured keys legitimately. A
    whole-literal content is skipped — shape 2 owns that defect.
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
            continue
        except SyntaxError:
            pass
        bindings = _hardcoded_measured_bindings(content)
        if not bindings:
            continue
        errors.append(
            f"Planner node '{node.id}' scaffolds {target!r} with code "
            f"that binds measured fields to constants "
            f"({', '.join(bindings)}) instead of measuring them — "
            f"whether a service is reachable and what a sensor reads "
            f"exist only by probing (call the service, run "
            f"nvidia-smi), so a constant is a fabricated measurement, "
            f"and the parse error on the same content does not hide "
            f"it. Write real code that measures the values it "
            f"reports.{grounded}"
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


# Elision keywords for the anchored-replacement phrase check below:
# the words a comment uses to point at code it stands in for. The set
# is _ELISION_RE's keyword group minus the bare "other" — too weak on
# its own, since it needs no elision company to appear in real prose.
_ANCHORED_ELISION_KEYWORDS = (
    "existing",
    "remaining",
    "rest of",
    "unchanged",
    "original",
    "same as",
)


def _anchored_elision_marker(content: str) -> str | None:
    """The elision-marker line in an anchored 'replace' body, or None.

    Cycle 12 (re-run #8 Arm 1): the accepted plan's anchored do_GET
    replacement ended with the standalone comment '# Existing
    code...' — the /health branch written out, every sibling GET
    route promised to a comment (executed diff +14/-70). Definition-
    level retention cannot see the loss (do_GET survives by name;
    its branches do not), and _ELISION_RE cannot either: its
    alternatives are dots-first ('... existing code ...'), a bare
    '...', or '# ...' alone, so the keyword-first form ('# Existing
    code...') evades it, as does a parenthetical after the dots
    ('# ... (rest of the original method) ...', the 27B's scaffold
    form in the same run). This check runs on the DELTA at the
    materialize seam — the only place the replacement body is
    visible as a body, before the splice dissolves it into the
    module — and fires on either shape: a line matching _ELISION_RE,
    or a standalone comment line carrying '...' together with an
    elision keyword. Both recorded must-accepts are clean under it:
    Arm 3's complete delta, and the canonical grounded delta (the
    verbatim current do_GET; its one comment names the M4 gates
    section — no dots, no keyword). A comment naming existing code
    WITHOUT the ellipsis does not fire: the dots are the marker's
    signature — the comment claims the code is elsewhere, not shown.
    """
    for line in content.splitlines():
        if _ELISION_RE.search(line):
            return line.strip()
        stripped = line.strip()
        if not stripped.startswith(("#", "//")):
            continue
        text = stripped.lstrip("#/ ").lower()
        if "..." in text and any(
            keyword in text for keyword in _ANCHORED_ELISION_KEYWORDS
        ):
            return stripped
    return None


_ELLIPSIS_STUB_DECORATORS = frozenset({"overload", "abstractmethod"})


def _dotted_name_tail(node: ast.AST) -> str:
    """The last component of a dotted name (``typing.Protocol`` -> ``Protocol``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _ellipsis_value_assignments(source: str) -> list[tuple[str, str]]:
    """(scope, target) pairs for bare Ellipsis assigned as a whole value.

    The AST walk behind _placeholder_value_errors: an assignment
    (plain, annotated, or augmented) or a return whose ENTIRE value
    is the bare ``...`` literal. Scope is the qualified definition
    path ("" at module level; nested definitions — a handler class
    defined inside a method — produce dotted scopes like
    ``Server.start.Handler.do_GET``), and the target is the
    assignment target's own source text (``ast.unparse``), so
    ``payload["ollama_reachable"] = ...`` is recorded as
    (``...do_GET``, ``payload['ollama_reachable']``) and a bare
    ``return ...`` as (scope, ``return``). Pairs are de-duplicated,
    first occurrence wins.

    Only the bare whole-value form is recorded — the value must BE
    the Ellipsis, not merely contain it:

    - a signature default (``def f(x=...)``) is an argument default,
      not an assignment, and never appears here;
    - ``...`` inside a string or a comment is not an Ellipsis value
      (a string constant is a str; comments never reach the AST);
    - a ``...`` expression statement (a stub body — the form
      freetalon/mesh/recon.py uses) is not an assignment;
    - a container literal holding an Ellipsis (``{"a": ...}``)
      assigns the container, not the Ellipsis.

    Stub-shaped scopes are exempt: a function decorated @overload or
    @abstractmethod, and any member of a Protocol class — their
    bodies are declarations, not implementations. Unparseable source
    yields no pairs (the parse checks own that defect).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[tuple[str, str]] = []

    def is_ellipsis(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Constant) and node.value is Ellipsis

    def visit(node: ast.AST, scope: str, exempt: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child_scope = f"{scope}.{node.name}" if scope else node.name
            child_exempt = exempt or any(
                _dotted_name_tail(decorator) in _ELLIPSIS_STUB_DECORATORS
                for decorator in node.decorator_list
            )
            for child in node.body:
                visit(child, child_scope, child_exempt)
            return
        if isinstance(node, ast.ClassDef):
            child_scope = f"{scope}.{node.name}" if scope else node.name
            child_exempt = exempt or any(
                _dotted_name_tail(base) == "Protocol" for base in node.bases
            )
            for child in node.body:
                visit(child, child_scope, child_exempt)
            return
        if not exempt:
            if isinstance(node, ast.Return):
                if is_ellipsis(node.value):
                    found.append((scope, "return"))
            else:
                value: ast.AST | None = None
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    value, targets = node.value, list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    value, targets = node.value, [node.target]
                elif isinstance(node, ast.AugAssign):
                    value, targets = node.value, [node.target]
                if is_ellipsis(value):
                    for target in targets:
                        found.append((scope, ast.unparse(target)))
        for child in ast.iter_child_nodes(node):
            visit(child, scope, exempt)

    for statement in tree.body:
        visit(statement, "", False)
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for pair in found:
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _placeholder_value_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
    modules: _ModuleSourceCache | None = None,
) -> list[str]:
    """Reject scaffold writes that assign the bare Ellipsis as a value.

    Cycle 6: across five live runs the accepted /health plan's step-1
    assigned literal ... to the response fields (payload[...] = ...
    inside the extended do_GET). The write is structurally complete
    — a real module, a real branch, every definition retained — so
    every existing content rule passed it: it is not placeholder
    content (the file is full of code), not a placeholder stub (no
    self-labeling comment), not an elision (no markers), not a
    stand-in (no hardcoded literal answer). Only the plan's own test
    caught it, at execution: "AssertionError: 'Ellipsis' is not an
    instance of bool". Cycle 5 put that assertion in the re-plan
    brief verbatim, and the model re-emitted the same delta anyway —
    so the defect becomes a planning-stage rejection the repair loop
    iterates on before execution, like every layer before it.

    The shape has a teacher: the harness's own grounded skeleton
    (_anchored_delta_sketch) sketches a route branch with
    payload["<field>"] = ... lines whose "..." values are open slots
    the surrounding steering tells the model to replace with real
    computations, and the model copies the sketch verbatim, slots
    and all. The error therefore says plainly that a "..." shown in
    a skeleton or example marks the slot a value belongs in — it is
    not code to copy.

    Scope (the boundary is the point; _ellipsis_value_assignments
    carries the AST half): scaffold/file_writer content for a
    NON-test Python implementation file. .pyi stubs never reach the
    check (their suffix is not .py — "x: int = ..." is the stub
    idiom), and test files are exempt via _is_test_file_path: a test
    may use Ellipsis as a sentinel or expected value, while the
    implementation is what must be real. An assignment the target
    file ALREADY carries (same scope, same target, judged from the
    module's current source through the shared cache) is not an
    offense — the rule names what the write INTRODUCES, so a
    faithful full-module scaffold is never rejected for a
    placeholder the module already had, and an anchored edit (whose
    content reaches this stage materialized into the full module)
    is judged on its own contribution. Content another rule owns is
    skipped, with the neighbors' skip list: a hardcoded literal
    answer (the stand-in check), placeholder content and placeholder
    stubs (the placeholder check), unparseable content (the parse
    check).
    """
    if modules is None:
        modules = _ModuleSourceCache()
    errors: list[str] = []
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
        if _is_test_file_path(target):
            continue
        if _literal_is_answer(content):
            continue
        if is_placeholder_content(content) or _is_placeholder_stub(content):
            continue
        try:
            ast.parse(content)
        except SyntaxError:
            continue  # _scaffold_parse_errors owns unparseable content
        existing = modules.source(_resolve_plan_target(target, root))
        existing_keys = (
            set(_ellipsis_value_assignments(existing))
            if existing is not None
            else set()
        )
        introduced = [
            pair
            for pair in _ellipsis_value_assignments(content)
            if pair not in existing_keys
        ]
        if not introduced:
            continue
        by_scope: dict[str, list[str]] = {}
        for scope, target_name in introduced:
            by_scope.setdefault(scope, []).append(target_name)
        groups = []
        for scope, target_names in by_scope.items():
            shown = ", ".join(target_names[:8])
            if len(target_names) > 8:
                shown += f", ... ({len(target_names) - 8} more)"
            where = f"in {scope}" if scope else "at module level"
            groups.append(f"{where}: {shown}")
        errors.append(
            f"Planner node '{node.id}' scaffolds {target!r} with "
            "placeholder values — the bare Ellipsis '...' assigned as "
            f"a value ({'; '.join(groups)}). '...' is not a value the "
            "code can use: at runtime the target holds the Ellipsis "
            "object itself, not the data the assignment must produce, "
            "so the write is a placeholder wearing a real assignment's "
            "shape. Assign each named target the real expression that "
            "computes its value (a call, a lookup, a parse of command "
            "output, or a literal of the right type). Where a plan "
            "skeleton or example shows '...' as a value, it marks the "
            "slot the value belongs in — replace it with the "
            "computation, never copy it as code."
        )
    return errors


def _is_not_implemented_stub_body(body: list[ast.stmt]) -> bool:
    """True when a definition body is a docstring plus exactly one
    ``raise NotImplementedError`` statement — the stub shape."""
    statements = list(body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    if len(statements) != 1:
        return False
    statement = statements[0]
    if not isinstance(statement, ast.Raise) or statement.exc is None:
        return False
    exc = statement.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


def _is_constant_expr(node: ast.AST) -> bool:
    """A literal or a container of literals (``__all__ = [...]`` is
    declarative module furniture, not implementation)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_constant_expr(elt) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (key is None or _is_constant_expr(key))
            and _is_constant_expr(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def _stub_only_module(source: str) -> bool:
    """True when a module's every definition is a NotImplementedError stub.

    The exam re-arm #4 shape (2026-11-23): a new module whose
    classes and functions carry real-looking signatures over bodies
    that only ``raise NotImplementedError("Implement per
    CONTRACT.md")`` — a scaffold deferring the implementation to a
    synthesis step that does not exist in the plan. Declarations
    are exempt, mirroring _ellipsis_value_assignments: @overload /
    @abstractmethod definitions and Protocol members are interface
    declarations, not implementations, and a module of only those
    is a typing surface, not a stub scaffold. The rule is strict
    about "solely": one real definition, or module-level code
    beyond docstring / imports / constant assignments, disqualifies
    — partial scaffolds are the critic's judgment call, not this
    rule's. Unparseable source yields False (the parse checks own
    that defect).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    saw_impl_def = False
    saw_real = False

    def visit_def(
        node: ast.FunctionDef | ast.AsyncFunctionDef, exempt: bool
    ) -> None:
        nonlocal saw_impl_def, saw_real
        if exempt or any(
            _dotted_name_tail(decorator) in _ELLIPSIS_STUB_DECORATORS
            for decorator in node.decorator_list
        ):
            return
        saw_impl_def = True
        if not _is_not_implemented_stub_body(node.body):
            saw_real = True

    def visit_class(node: ast.ClassDef, exempt: bool) -> None:
        nonlocal saw_real
        inner_exempt = exempt or any(
            _dotted_name_tail(base) == "Protocol" for base in node.bases
        )
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_def(child, inner_exempt)
            elif isinstance(child, ast.ClassDef):
                visit_class(child, inner_exempt)
            elif (
                isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Constant)
                and isinstance(child.value.value, str)
            ):
                continue  # class docstring
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                if child.value is not None and not _is_constant_expr(
                    child.value
                ):
                    saw_real = True
            else:
                saw_real = True

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit_def(statement, False)
        elif isinstance(statement, ast.ClassDef):
            visit_class(statement, False)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        elif (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue  # module docstring
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            if statement.value is not None and not _is_constant_expr(
                statement.value
            ):
                saw_real = True
        else:
            saw_real = True
    return saw_impl_def and not saw_real


def _stub_only_module_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    goal_text: str | None = None,
    index: Any | None = None,
) -> list[str]:
    """Reject a NEW module whose every definition is a stub.

    Exam re-arm #4 (2026-11-23): the accepted plan scaffolded the
    pairing package as NotImplementedError stubs ("Implement per
    CONTRACT.md") and deferred the implementation to a later
    synthesis step; parse passed it, execution spent three nodes
    writing it, and only the critic's hollow review stopped the
    writes. The shape is deterministic — an AST fact about the
    content, like placeholder_value — so it becomes a planning-stage
    rejection the repair loop iterates on before execution.

    Scope (the boundary is the point, as in _placeholder_value_errors):
    scaffold/file_writer content for a NON-test Python implementation
    file that does NOT exist yet. An existing file's rewrite is the
    overwrite / anchored-edit checks' territory (and a faithful
    rewrite of a stub module that already exists introduces nothing
    this rule owns); package scaffolding files (__init__.py,
    __main__.py) are exempt — a new package's dunder files are
    legitimately near-empty; content another rule owns is skipped
    (placeholder content and labeled placeholder stubs — the
    placeholder check; unparseable content — the parse check).
    """
    errors: list[str] = []
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
        if _is_test_file_path(target):
            continue
        if Path(target).name in ("__init__.py", "__main__.py"):
            continue
        if is_placeholder_content(content) or _is_placeholder_stub(content):
            continue
        if _resolve_plan_target(target, root).exists():
            continue
        if _stub_only_module(content):
            errors.append(
                f"Planner node '{node.id}' scaffolds {target!r} as a "
                "module whose every definition only raises "
                "NotImplementedError — a scaffold that defers the "
                "implementation to a step that does not exist in the "
                "plan. Write the real implementation the goal "
                "requires, in this node: where the goal names a spec "
                "file, its text is in this prompt — implement what "
                "it pins, with real bodies that compute and return "
                "the specified values."
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
        if _is_test_file_path(target):
            # Cycle 11: for an existing test module the generic advice
            # (retype the full module) is the wrong steer — the moves
            # that serve a tests goal are a NEW test file, or an
            # anchored insert into this module (the move Qwen's Arm 3
            # repair made on its own after this rejection in re-run
            # #7). Name both in the rejection itself.
            advice = (
                "This is an existing test module: do not scaffold a "
                "replacement for it. Put the new tests in a NEW test "
                "file (for example tests/test_health.py), or add the "
                "new cases to this module with an anchored insert "
                "(inputs \"anchor\" naming an existing definition in "
                "the module, \"position\": \"after\", and \"content\" "
                "holding only the new test code) instead."
            )
        else:
            advice = (
                "Scaffold the FULL updated module (every existing "
                "definition, plus the new code) or extend the module "
                "in place with a bash heredoc instead."
            )
        errors.append(
            f"Planner node '{node.id}' scaffolds {target!r} with content "
            f"that drops {len(dropped)} of the existing module's "
            f"{len(existing_names)} definitions ({shown}) — a scaffold "
            "overwrites the whole file, so every definition it omits is "
            "deleted, and a file_exists criterion cannot see the loss. "
            f"{advice}{grounded}"
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


def _missing_target_errors(nodes: list[PlanNode]) -> list[str]:
    """A scaffold/file_writer node whose inputs name no target file.

    Cycle 7 — the narrow floor of the required-inputs question
    cycle 6 deferred. The scaffold claw determines its target in
    exactly one place (tool_registry's scaffold handler): inputs
    "file_path", else "path", else the first inputs "outputs"
    entry — and raises when none of them names one. A node with
    no determinable target can therefore only fail at execution,
    after its plan was accepted (live re-run #4, run 2: a scaffold
    node with empty inputs {} was accepted, and only experiment
    memory stopped it). Mirroring the claw's one expression here
    names the defect at planning instead. This is NOT the full
    required-inputs table cycle 6 deferred: every other input
    keeps the behavior it has today — a node WITH a target and no
    content writes an empty file, which is the claw's defined
    behavior, not this floor's business. The outputs access is
    guarded to list/tuple: the claw itself would crash on an
    exotic outputs value at execution, and a planning rejection
    is the honest name for that too.
    """
    errors: list[str] = []
    for node in nodes:
        if node.assigned_claw not in ("scaffold", "file_writer"):
            continue
        inputs = node.inputs or {}
        target = inputs.get("file_path") or inputs.get("path")
        if not target:
            outputs = inputs.get("outputs")
            if isinstance(outputs, (list, tuple)) and outputs:
                target = outputs[0]
        if isinstance(target, str) and target:
            continue
        errors.append(
            f"Planner node '{node.id}' uses the scaffold claw but "
            "names no target file: its inputs carry no 'file_path', "
            "'path', or 'outputs' entry, so the claw has nothing "
            "to write. Name the file the node writes in "
            "'file_path' and give its full new content in "
            "'content'."
        )
    return errors



# --- Planner schema redesign (2026-11-19): prose bounds ---------------
#
# The assessment (planner-assessment-2026-11-19.md) measured the
# model-facing schema's free-text fields as the planner's one
# unbounded cost: a single differs_by value ran to 87,415 chars
# (~21,850 tokens, ~260 s of generation) and died in a repetition
# loop, and across all 41 curated outputs degeneration occurred ONLY
# inside free-text justification fields. differs_by / responds_to are
# system-owned now (parse strips them; see parse_planner_response),
# and the two validators registered in _collect_plan_error_records
# bound what remains: hard length caps on the prose fields, and a
# deterministic repetition-loop detector over the same fields.
# Payload (inputs: commands, code) is deliberately NOT scanned —
# repetition there can be legitimate, and payload hollowness is the
# payload validators' and the critic's job, not this guard's.

#: Hard caps (chars) for the model-emitted scalar prose fields,
#: enforced at parse time. The healthy objective distribution tops
#: out at 312 chars across the curated corpora (209 in the replay
#: sets); 400 leaves headroom without leaving a field unbounded.
#: hypothesis is capped at the same bound — it feeds approach
#: signatures and is a sentence by contract, not an essay.
_PROSE_FIELD_CAPS: dict[str, int] = {
    "objective": 400,
    "hypothesis": 400,
}

#: Cap for each free-text acceptance entry: human-readable one-line
#: checks by contract (the legacy channel; criteria carry the
#: machine-evaluated substance).
_ACCEPTANCE_ENTRY_CAP = 400

#: A prose string at or under this length cannot be the runaway the
#: detector exists for; longer strings are judged (the assessment's
#: measurement gate was the same 200 chars).
_LOOP_MIN_CHARS = 200

#: A clause (semicolon split) or word 6-gram repeated at least this
#: many times in one prose string is a degeneration loop, not
#: emphasis. The pairing field's top clause repeated 898 times and
#: its top 6-gram 896 times; across the 41 curated outputs no
#: legitimate prose field trips this threshold.
_LOOP_MIN_REPEATS = 5
_LOOP_NGRAM = 6


def _repetition_loop_detail(text: str) -> str | None:
    """Describe the repetition loop in ``text``, or None if sound.

    Deterministic and cheap: clause frequency over ';' splits plus
    word n-gram frequency — the assessment's two detectors, which
    between them flagged every observed degeneration and nothing
    else. Returns a short description of the worst offender for the
    rejection text.
    """
    if len(text) <= _LOOP_MIN_CHARS:
        return None
    clauses = [part.strip() for part in text.split(";")]
    clauses = [part for part in clauses if part]
    if clauses:
        clause, count = Counter(clauses).most_common(1)[0]
        if count >= _LOOP_MIN_REPEATS and len(clause) <= 60:
            return f"clause {clause!r} repeats {count} times"
    words = text.split()
    if len(words) >= _LOOP_NGRAM * _LOOP_MIN_REPEATS:
        grams = Counter(
            " ".join(words[i : i + _LOOP_NGRAM])
            for i in range(len(words) - _LOOP_NGRAM + 1)
        )
        gram, count = grams.most_common(1)[0]
        if count >= _LOOP_MIN_REPEATS:
            return f"word sequence {gram!r} repeats {count} times"
    return None


def _node_prose_fields(node: PlanNode) -> list[tuple[str, str, int]]:
    """A node's model-emitted prose as (field label, value, cap)."""
    fields: list[tuple[str, str, int]] = [
        (
            "objective",
            getattr(node, "objective", "") or "",
            _PROSE_FIELD_CAPS["objective"],
        ),
        (
            "hypothesis",
            getattr(node, "hypothesis", "") or "",
            _PROSE_FIELD_CAPS["hypothesis"],
        ),
    ]
    fields.extend(
        (f"acceptance[{index}]", entry, _ACCEPTANCE_ENTRY_CAP)
        for index, entry in enumerate(getattr(node, "acceptance", None) or [])
        if isinstance(entry, str)
    )
    return fields


def _prose_length_errors(nodes: list[PlanNode]) -> list[str]:
    """Prose fields over their hard caps, one message per defect."""
    errors: list[str] = []
    for node in nodes:
        for field, value, cap in _node_prose_fields(node):
            if len(value) > cap:
                errors.append(
                    f"Planner node '{node.id}' field '{field}' is "
                    f"{len(value)} chars, over the {cap}-char cap for "
                    f"planner prose — state the work in one short "
                    f"passage; extended justification is not part of "
                    f"the plan contract."
                )
    return errors


def _repetition_loop_errors(nodes: list[PlanNode]) -> list[str]:
    """Prose fields degenerated into repetition loops, per node."""
    errors: list[str] = []
    for node in nodes:
        for field, value, _cap in _node_prose_fields(node):
            detail = _repetition_loop_detail(value)
            if detail is not None:
                errors.append(
                    f"Planner node '{node.id}' field '{field}' "
                    f"degenerates into a repetition loop ({detail}) "
                    f"— the generation repeated itself instead of "
                    f"stating the work; write the field once, plainly."
                )
    return errors


# ── Module contract pinning ──────────────────────────────────────
#
# A plan that writes two or more NEW Python modules coupled by
# imports (a package's modules, a module and its test file) used to
# invent each file's interface independently inside one generation:
# run-20261120T083931Z-6c84e7 wrote a store whose poll returns
# {"state": "not-found"} and tests that read a .state attribute,
# expected the goal's word "gone", and demanded a CodeNotFound raise
# the store never raises — 6 of its 7 tests failed against its own
# code, and the re-plans, which never see the previous contents,
# regenerated the same incoherence. The fix is the discipline the
# hand-built reference (freetalon/pairing/CONTRACT.md) used: ONE
# contract, written first, that every file answers to.
#
# The plan envelope gains an optional top-level "contract": one
# entry per coupled new module — {"module": repo-relative path,
# "defines": [public names], "statuses": [status strings]}. Parse
# shape-validates it and writes the validated form into the plan
# metadata under "module_contract" (a system-owned key, carried on
# the same channel as rejected_approaches); the coherence validator
# below then checks the plan's contents against it deterministically
# (AST): declared names must be defined, imported names must be
# declared and defined, and state/status literals must come from
# the declared vocabulary. A plan-written file importing a
# plan-written NEW module with no contract entry is rejected
# (contract_missing) — that import is what makes the contract
# required, so single-module plans and plans touching only existing
# modules (the corpus shape) are entirely unaffected.
#
# Once a plan is accepted, its contract is PINNED for the run: the
# loop seeds it into the re-plan brief (plan metadata -> brief, the
# seed_forbidden flow), the brief renders it as binding, and
# replan_from_brief passes it back into parse, where a delta plan
# that declares a different contract is rejected (contract_changed)
# and its contents are validated against the pin. The pin is what
# makes the failure feedback binding: a re-plan can no longer
# re-invent the interface it just failed against.
#
# Derivation (2026-11-23): the declaration itself measured as a
# capability failure — prompt section, worked example, schema
# key and exact-shape repair feedback were all already in
# place, and two models (the 7B in arm 88C, qwen3.8:27b in exam
# re-arm #2) emitted the array zero times across every
# candidate. Per the differs_by precedent, the skeleton is now
# system-derived at parse (_derive_contract_entries): a
# declared contract still governs where present, derivation
# fills only omitted entries, and the result pins exactly like
# a declared one.

CONTRACT_METADATA_KEY = "module_contract"

# The L1 re-ask's outcome record: written by _call_llm_with_repair
# into the accepted plan's metadata whenever the re-ask fired, so
# a non-adoption is never silent again (exam re-arm #3: the re-ask
# produced a richer declared contract, the adoption gates rejected
# it at parse, and the reason was recorded nowhere). System-owned
# like module_contract — the planner is its only writer.
CONTRACT_REASK_METADATA_KEY = "contract_reask"

# The goal-named spec record (spec grounding, 2026-11-23): the
# specs the harness loaded verbatim into this plan's prompts —
# path, sha256, and byte count per file — stamped by
# plan_task_intent and mirrored into the re-plan brief by
# replan_from_brief, so a post-mortem can prove which
# authoritative text reached the calls instead of inferring it.
# System-owned like contract_reask: set from the loader's own
# result, cleared when the goal named no spec.
SPEC_CONTEXT_METADATA_KEY = "spec_context"

# Sentinel for _collect_plan_error_records' contract parameter: the
# caller supplied no contract context, so the contract checks do
# not run at all. parse_planner_response always supplies a context
# (the effective contract, or None for "no contract declared — the
# contract_missing trigger still applies"); the other callers (the
# duplicate-key path's lenient views, the node-local repair's
# candidate check when the plan predates pinning) keep the
# pre-contract behavior unless they pass a pin of their own.
_CONTRACT_UNSUPPLIED: Any = object()

# Sentinel for the clauses parameter, same discipline (campaign
# build #3): the caller supplied no frozen contract-clause list,
# so the clause-coverage rule does not run at all. Only
# plan_task_intent and replan_from_brief supply one — extracted
# deterministically from the goal-named specs they already load —
# so corpus replay and every other parse caller keep the exact
# pre-build-#3 behavior.
_CLAUSES_UNSUPPLIED: Any = object()

_CONTRACT_MODULE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py$")
_CONTRACT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Dict keys / attribute names that carry a record's state. The
# failed pairing store used "state"; the reference contract's
# public view uses "status". Both are state carriers for the
# vocabulary check — the contract's "statuses" list is the one
# vocabulary either spelling must draw from.
_STATE_KEYS = ("state", "status")


def _normalize_contract(
    raw: Any,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Validate and normalize a raw module contract.

    Returns (entries, errors). ``entries`` is None when no usable
    contract exists — either the value was absent (errors empty:
    no contract declared) or it was malformed (errors names why).
    Entries are normalized — repo-relative module paths, defines /
    statuses deduplicated in declared order, entries sorted by
    module — so two spellings of the same contract compare equal.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, list):
        return None, ['the plan\'s "contract" must be an array of module entries']
    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            errors.append(f"contract entry {position} is not an object")
            continue
        module = item.get("module")
        if isinstance(module, str):
            module = module.strip().replace("\\", "/")
            if module.startswith("./"):
                module = module[2:]
        if not isinstance(module, str) or not _CONTRACT_MODULE_RE.match(module):
            errors.append(
                f"contract entry {position}: \"module\" must be a "
                "repo-relative .py path (e.g. freetalon/pairing/store.py)"
            )
            continue
        if module in seen:
            errors.append(f"contract declares module '{module}' twice")
            continue
        seen.add(module)
        defines = item.get("defines")
        if (
            not isinstance(defines, list)
            or not defines
            or any(
                not isinstance(name, str) or not _CONTRACT_NAME_RE.match(name)
                for name in defines
            )
        ):
            errors.append(
                f"contract entry for '{module}': \"defines\" must be a "
                "non-empty array of public names (classes, functions, "
                "exceptions, and the methods other files rely on)"
            )
            continue
        statuses = item.get("statuses") or []
        if not isinstance(statuses, list) or any(
            not isinstance(value, str) or not value for value in statuses
        ):
            errors.append(
                f"contract entry for '{module}': \"statuses\" must be "
                "an array of status strings"
            )
            continue
        entries.append(
            {
                "module": module,
                "defines": list(dict.fromkeys(defines)),
                "statuses": list(dict.fromkeys(statuses)),
            }
        )
    if errors:
        return None, errors
    entries.sort(key=lambda entry: entry["module"])
    return entries, []


def _contract_from_metadata(
    metadata: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """The pinned contract in a plan's metadata, or None.

    Parse wrote the value (system-owned key, already normalized);
    normalize again defensively — a hand-built plan's metadata is
    caller input, and an unusable value means no pin, never a crash.
    """
    if not isinstance(metadata, dict):
        return None
    entries, errors = _normalize_contract(metadata.get(CONTRACT_METADATA_KEY))
    if errors or not entries:
        return None
    return entries


def _contracts_equal(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> bool:
    """Set-wise equality of two normalized contracts."""

    def key(
        entries: list[dict[str, Any]],
    ) -> dict[str, tuple[set[str], set[str]]]:
        return {
            entry["module"]: (set(entry["defines"]), set(entry["statuses"]))
            for entry in entries
        }

    return key(left) == key(right)


def _contract_diff_summary(
    declared: list[dict[str, Any]], pinned: list[dict[str, Any]]
) -> str:
    """One line naming how a declared contract differs from the pin."""
    mine = {entry["module"]: entry for entry in declared}
    theirs = {entry["module"]: entry for entry in pinned}
    parts: list[str] = []
    for module in sorted(set(mine) | set(theirs)):
        if module not in theirs:
            parts.append(f"{module} is not in the pinned contract")
        elif module not in mine:
            parts.append(f"{module} is missing")
        else:
            if set(mine[module]["defines"]) != set(theirs[module]["defines"]):
                parts.append(f"{module} declares different defines")
            if set(mine[module]["statuses"]) != set(theirs[module]["statuses"]):
                parts.append(f"{module} declares different statuses")
    return "; ".join(parts) or "the entries differ"


def _render_contract_block(
    contract: list[dict[str, Any]], *, pinned: bool
) -> str:
    """The contract as prompt prose — one line per module.

    Rendered into the re-plan brief and the repair prompts. Prose,
    never JSON, for the brief-echo reason _format_brief_for_prompt
    documents: at temperature 0 the model copies JSON it last saw.
    """
    if pinned:
        lines = [
            "MODULE CONTRACT — PINNED from the accepted plan version. "
            "These interfaces are fixed for the rest of this run: conform "
            "every module and test you write to them EXACTLY — same public "
            "names, same status strings, same return shapes. Do not rename "
            "a name, do not invent a status string, and do not emit a "
            "different contract."
        ]
    else:
        lines = ["MODULE CONTRACT (binding):"]
    for entry in contract:
        line = f"- {entry['module']}: defines {', '.join(entry['defines'])}"
        if entry["statuses"]:
            line += f"; statuses: {', '.join(entry['statuses'])}"
        lines.append(line)
    return "\n".join(lines)


def _content_defined_names(source: str) -> set[str]:
    """Every name a module's content binds.

    Defs and classes at any depth, top-level assignment targets, and
    imported names (an __init__ that re-exports a name binds it for
    its importers). Empty for unparseable content — the syntax
    validators own that failure; this check judges names, not syntax.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def _dotted_module_candidates(dotted: str) -> list[str]:
    """Repo-relative .py paths a dotted module name may mean."""
    base = dotted.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _content_imports(
    source: str, importer_rel: str, known_modules: set[str]
) -> list[tuple[str, list[str]]]:
    """Imports in a content that resolve to a known module.

    Returns (module repo-relative path, imported names) pairs; an
    empty names list is a bare module import (``import a.b``, or
    ``from a import b`` where b is itself a module). Relative
    imports resolve against the importing file's package. Imports
    resolving outside known_modules (the stdlib, existing repo
    modules) are not returned — existing grounding owns those.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    importer_parts = importer_rel.split("/")[:-1]
    results: list[tuple[str, list[str]]] = []

    def resolve(dotted: str) -> str | None:
        for candidate in _dotted_module_candidates(dotted):
            if candidate in known_modules:
                return candidate
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = resolve(alias.name)
                if target is not None:
                    results.append((target, []))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(importer_parts) - (node.level - 1)
                if keep < 0:
                    continue
                base_parts = importer_parts[:keep]
                tail = node.module.split(".") if node.module else []
                dotted = ".".join(base_parts + tail)
            else:
                dotted = node.module or ""
            if not dotted:
                continue
            names: list[str] = []
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub = resolve(f"{dotted}.{alias.name}")
                if sub is not None:
                    results.append((sub, []))
                else:
                    names.append(alias.name)
            if names:
                target = resolve(dotted)
                if target is not None:
                    results.append((target, names))
    return results


def _is_state_expr(node: ast.expr) -> bool:
    """A subscript or attribute that reads a record's state."""
    if isinstance(node, ast.Subscript):
        return (
            isinstance(node.slice, ast.Constant)
            and node.slice.value in _STATE_KEYS
        )
    if isinstance(node, ast.Attribute):
        return node.attr in _STATE_KEYS
    return False


def _status_literals(source: str) -> list[str]:
    """The status strings a content uses as state values.

    Two shapes: dict-literal values under a "state"/"status" key
    ({"state": "pending", ...} — the shape the failed pairing store
    returned), and string literals compared ==/!= against a
    state/status subscript or attribute (result["status"] == "gone"
    — the shape its tests asserted). Assignments and bare literals
    are not state uses and are not collected.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in _STATE_KEYS
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.append(value.value)
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for position, op in enumerate(node.ops):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                left, right = operands[position], operands[position + 1]
                for expr, other in ((left, right), (right, left)):
                    if (
                        isinstance(other, ast.Constant)
                        and isinstance(other.value, str)
                        and _is_state_expr(expr)
                    ):
                        found.append(other.value)
    return found


def _plan_written_python_contents(
    nodes: list[PlanNode], root: Path
) -> dict[str, tuple[str, str]]:
    """Repo-relative path -> (content, node id) for the Python files
    this plan authors in full: scaffold/file_writer contents and
    bash heredoc/echo code writes (full writes only — an append
    authors a fragment, not a module's interface). Only .py targets
    under the repo tree; anything else is not a module this check
    can judge."""
    contents: dict[str, tuple[str, str]] = {}
    for node in nodes:
        claw = node.assigned_claw or ""
        if claw in ("scaffold", "file_writer"):
            target = node.inputs.get("file_path") or node.inputs.get("path")
            content = node.inputs.get("content")
            if isinstance(target, str) and isinstance(content, str):
                rel = _repo_relative_target(target, root)
                if rel and rel.endswith(".py"):
                    contents[rel] = (content, node.id)
        elif claw in ("bash", "shell_exec"):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str) and command:
                for target, content, is_append in _bash_code_writes(command):
                    if is_append:
                        continue
                    rel = _repo_relative_target(target, root)
                    if rel and rel.endswith(".py"):
                        contents[rel] = (content, node.id)
    return contents


def _contract_module_content(
    module: str,
    contents: dict[str, tuple[str, str]],
    root: Path,
) -> str | None:
    """A contracted module's effective content: the plan's own write
    when the plan writes it, else the file on disk (a delta plan
    that leaves a module alone is judged against what is actually
    there), else None."""
    if module in contents:
        return contents[module][0]
    path = _resolve_plan_target(module, root)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _contract_error_records(
    nodes: list[PlanNode],
    contract: list[dict[str, Any]] | None,
    repo_root: str | Path | None,
) -> list[PlanError]:
    """Cross-module coherence against the plan's module contract.

    Three checks, all deterministic (AST over the plan's own
    contents — no model judgment):

    - contract_missing: a plan-written file imports from a
      plan-written NEW module (target not on disk) that has no
      contract entry. The import is the coupling that makes the
      contract required; a plan with no coupled new modules never
      sees this check fire.
    - declared-but-undefined: every name in an entry's "defines"
      must be defined by the module's effective content.
    - undeclared imports: every name a plan-written file imports
      from a contracted module must be in that module's "defines"
      (being defined in the content is checked once, at the module
      level, above).
    - status vocabulary: when any entry declares "statuses", the
      state/status literals used by the coupled files (contracted
      modules the plan writes, and files importing one) must come
      from the declared union — one vocabulary for the whole plan.
    """
    root = resolve_repo_root(repo_root)
    contents = _plan_written_python_contents(nodes, root)
    if not contents:
        return []
    known = set(contents)
    imports_by_file = {
        rel: _content_imports(content, rel, known)
        for rel, (content, _node_id) in contents.items()
    }
    imported_targets = {
        target for pairs in imports_by_file.values() for target, _names in pairs
    }
    new_modules = {
        rel for rel in contents if not _path_exists_on_disk(rel, root)
    }
    entries = {entry["module"]: entry for entry in contract or []}
    records: list[PlanError] = []

    for target in sorted(imported_targets & new_modules):
        if target in entries:
            continue
        importers = sorted(
            rel
            for rel, pairs in imports_by_file.items()
            if any(candidate == target for candidate, _names in pairs)
        )
        first = importers[0] if importers else target
        records.append(
            PlanError(
                code="contract_missing",
                message=(
                    f"Planner node '{contents[first][1]}': this plan "
                    f"writes new module '{target}' and other files in "
                    "the plan import from it, but the plan declares "
                    "no \"contract\" entry for it. Declare the module "
                    "contract FIRST — a top-level \"contract\" array "
                    "in the plan object, before \"nodes\": one entry "
                    "per new module another file imports from, "
                    "{\"module\": \"<repo-relative path>\", \"defines\": "
                    "[<every public name another file relies on>], "
                    "\"statuses\": [<the exact status strings>]}. The "
                    "contract is what keeps the implementation and "
                    "its tests describing the same interface."
                ),
                node_id=contents[first][1],
            )
        )
    if not entries:
        return records

    defined_by_module: dict[str, set[str]] = {}
    for module in sorted(entries):
        entry = entries[module]
        content = _contract_module_content(module, contents, root)
        if content is None:
            records.append(
                PlanError(
                    code="contract_incoherence",
                    message=(
                        f"The module contract declares '{module}', but "
                        "the plan does not write it and it does not "
                        "exist in the repo — a contract entry must name "
                        "a module this plan writes (or one already on "
                        "disk)."
                    ),
                )
            )
            continue
        defined = _content_defined_names(content)
        defined_by_module[module] = defined
        missing = [name for name in entry["defines"] if name not in defined]
        if missing:
            if module in contents:
                prefix = f"Planner node '{contents[module][1]}'"
                node_id: str | None = contents[module][1]
            else:
                prefix = f"Module '{module}'"
                node_id = None
            records.append(
                PlanError(
                    code="contract_incoherence",
                    message=(
                        f"{prefix}: the module contract for '{module}' "
                        f"declares defines {missing}, but the module's "
                        "content does not define them. The contract is "
                        "binding: define every name it promises, or "
                        "correct the contract — never let the two "
                        "describe different interfaces."
                    ),
                    node_id=node_id,
                )
            )

    for rel in sorted(imports_by_file):
        for target, names in imports_by_file[rel]:
            entry = entries.get(target)
            if entry is None:
                continue
            for name in names:
                if name not in entry["defines"]:
                    records.append(
                        PlanError(
                            code="contract_incoherence",
                            message=(
                                f"Planner node '{contents[rel][1]}': "
                                f"this file imports '{name}' from "
                                f"'{target}', but the module contract "
                                "for that module does not declare it "
                                f"(declares: {', '.join(entry['defines'])}). "
                                "Every name another file relies on must "
                                "be in the contract's defines — and "
                                "defined by the module — before any "
                                "file may import it."
                            ),
                            node_id=contents[rel][1],
                        )
                    )

    vocabulary: set[str] = set()
    for entry in entries.values():
        vocabulary.update(entry["statuses"])
    if vocabulary:
        coupled = {module for module in entries if module in contents}
        for rel, pairs in imports_by_file.items():
            if any(target in entries for target, _names in pairs):
                coupled.add(rel)
        for rel in sorted(coupled):
            content, node_id = contents[rel]
            for literal in sorted(set(_status_literals(content))):
                if literal not in vocabulary:
                    records.append(
                        PlanError(
                            code="contract_incoherence",
                            message=(
                                f"Planner node '{node_id}': this file "
                                f"uses status \"{literal}\", which is "
                                "not in the module contract's status "
                                f"vocabulary ({', '.join(sorted(vocabulary))}). "
                                "One vocabulary for the whole plan: use "
                                "only the status strings the contract "
                                "declares — they come from the goal's "
                                "own words."
                            ),
                            node_id=node_id,
                        )
                    )
    return records


def _contract_parse_stage(
    payload: dict[str, Any],
    pinned_contract: list[dict[str, Any]] | None,
) -> tuple[list[PlanError], list[dict[str, Any]] | None]:
    """The contract half of one parse: (records, effective contract).

    The effective contract is the pin when the caller supplies one
    (a re-plan: the pin governs, and a payload contract that differs
    from it is a contract_changed rejection); otherwise the
    payload's own declared contract, shape-validated. Shape errors
    are contract_shape records and leave no effective contract —
    the rejection speaks for itself.
    """
    records: list[PlanError] = []
    declared, shape_errors = _normalize_contract(payload.get("contract"))
    for message in shape_errors:
        records.append(PlanError(code="contract_shape", message=message))
    if pinned_contract is not None:
        if declared is not None and not _contracts_equal(declared, pinned_contract):
            records.append(
                PlanError(
                    code="contract_changed",
                    message=(
                        "The plan declares a module contract that "
                        "differs from this run's PINNED contract ("
                        + _contract_diff_summary(declared, pinned_contract)
                        + "). The pinned contract is binding — conform "
                        "to it exactly; do not re-invent it.\n"
                        + _render_contract_block(pinned_contract, pinned=True)
                    ),
                )
            )
        return records, pinned_contract
    if shape_errors:
        return records, None
    return records, declared


def _derive_contract_entries(
    nodes: list[PlanNode],
    declared: list[dict[str, Any]] | None,
    repo_root: str | Path | None,
) -> list[dict[str, Any]] | None:
    """System-derived contract entries for coupled NEW modules.

    Contract derivation (2026-11-23): the declaration requirement
    measured as a capability failure, not a prompt failure — the
    MODULE CONTRACT section and a worked example were already in
    the system prompt, the key was already in the envelope schema,
    and the repair feedback already spelled out the exact shape,
    yet qwen3.8:27b emitted the array zero times across three
    candidate plans (exam re-arm #2, 2026-11-22) and the 7B did
    the same in arm 88C. The differs_by precedent (2026-11-19)
    takes a field the model emits unreliably out of the model's
    hands; the contract's skeleton is the same case, because
    every part of it is already determined by the plan itself:

    - module: the contract_missing trigger set — plan-written NEW
      modules another plan file imports from;
    - defines: the names plan files actually import from the
      module (the reliance set the undeclared-import check
      judges). A name in it that the module never defines still
      fails, as contract_incoherence naming the name — the
      proof-arm-3 failure class, now reachable instead of masked
      behind an unfixable contract_missing. When a module is
      only ever bare-imported, its content's public defined
      names stand in.
    - statuses: the module's OWN status literals, so the
      vocabulary check keeps its direction — implementation
      modules set the vocabulary, importing files (the tests)
      must draw from it.

    A model-declared contract still governs: derivation fills
    only the entries the declaration omitted, and a declared
    entry is never rewritten (it can promise names imports do
    not show — methods relied on by attribute access). Returns
    the declared contract unchanged when nothing needed filling.
    """
    root = resolve_repo_root(repo_root)
    contents = _plan_written_python_contents(nodes, root)
    if not contents:
        return declared
    known = set(contents)
    imports_by_file = {
        rel: _content_imports(content, rel, known)
        for rel, (content, _node_id) in contents.items()
    }
    imported_targets = {
        target
        for pairs in imports_by_file.values()
        for target, _names in pairs
    }
    new_modules = {
        rel for rel in contents if not _path_exists_on_disk(rel, root)
    }
    entries = {entry["module"]: entry for entry in declared or []}
    filled = False
    for module in sorted(imported_targets & new_modules):
        if module in entries:
            continue
        defines = {
            name
            for pairs in imports_by_file.values()
            for target, names in pairs
            if target == module
            for name in names
        }
        if not defines:
            defines = {
                name
                for name in _content_defined_names(contents[module][0])
                if not name.startswith("_")
            }
        if not defines:
            # Nothing importable can be named for this module —
            # leave it entry-less; contract_missing remains the
            # honest rejection for it.
            continue
        entries[module] = {
            "module": module,
            "defines": sorted(defines),
            "statuses": sorted(
                set(_status_literals(contents[module][0]))
            ),
        }
        filled = True
    if not filled:
        return declared
    merged, errors = _normalize_contract(
        [entries[module] for module in sorted(entries)]
    )
    if errors or merged is None:
        return declared
    return merged


def _contract_omitted_modules(
    nodes: list[PlanNode],
    response_text: str,
    repo_root: str | Path | None,
) -> list[str]:
    """Coupled NEW modules the payload's own contract omitted.

    The L1 trigger (2026-11-23): the set derivation can fill —
    every coupled new module, computed with no declared entries
    at all — minus the modules the payload actually declared.
    Empty when the plan couples no new modules or the
    declaration covered the coupling. Best-effort by design:
    any failure to re-read the payload reads as no omission,
    because the accepted plan already carries the derived
    contract — the re-ask this feeds is an improvement attempt,
    never a gate on planning.
    """
    try:
        derivable = _derive_contract_entries(nodes, None, repo_root)
        if not derivable:
            return []
        payload = parse_json_object(sanitize_llm_output(response_text))
        declared_entries, _errors = _normalize_contract(
            payload.get("contract") if isinstance(payload, dict) else None
        )
        declared = {entry["module"] for entry in declared_entries or []}
        return sorted(
            entry["module"]
            for entry in derivable
            if entry["module"] not in declared
        )
    except Exception:
        return []


# --- Campaign build #5: split plan emission -------------------------
#
# Exam arm #14 (2026-11-24): build #4's anchoring converged the
# repair loop, but compliant plans died truncated mid-JSON at the
# generation fuse — a compliant plan carries full contents for
# every file plus per-node clause declarations in ONE response
# (A5 cut at 39,034 chars; the largest completed response was
# 34.1K). Raising the fuse is an arms race; the structural fix is
# that no single response carries a whole multi-file plan.
# plan_task_intent therefore emits in two phases: a SKELETON (the
# DAG without file contents — per write node a content_intent:
# "create", "edit", or "whole_file") judged by every structural
# validator, then a CONTENT FILL (one call per write node, only
# that node's payload) judged per node, then ASSEMBLY through the
# ordinary full parse so the accepted plan is indistinguishable
# from a single-shot plan. replan_from_brief originally kept the
# single-shot emission (delta plans were assumed small); campaign
# build #9 mirrors the split there — see the build #9 section at
# replan_from_brief. Arm #18 measured why: a repair delta for the
# standing server failure rewrites server.py wholesale, which is
# not a small response, and the re-plan died at raw JSON validity
# with no v2 produced.

#: Content intents a skeleton write node may declare.
SKELETON_CONTENT_INTENTS = ("create", "edit", "whole_file")

#: Plan metadata key for the two-phase emission record.
PLAN_EMISSION_METADATA_KEY = "plan_emission"

#: Plan metadata key for the deterministic skeleton-intent
#: corrections (campaign build #11): one entry per write node
#: whose declared content intent parse corrected against the
#: tree — {node, target, from, to, reason}. System-owned: parse
#: stamps it from its own corrections and clears any value a
#: model payload carried under it.
INTENT_CORRECTIONS_METADATA_KEY = "intent_corrections"

#: Validator codes that judge a write node's payload. At the
#: skeleton phase their records against a content-less write node
#: are dropped (see _collect_plan_error_records); at the fill
#: phase they run per node and bind.
_SKELETON_CONTENT_CODES = frozenset(
    {
        "scaffold_overwrite",
        "scaffold_parse",
        "elision",
        "placeholder_value",
        "placeholder_scaffold",
        "stub_only_module",
        "code_append",
        "static_standin",
        "heredoc_parse",
    }
)

#: Per-node content codes the fill phase enforces on one node's
#: filled payload (the plan-level codes — unwritten_file,
#: route_engagement, missing_directory — judge the assembled plan
#: at assembly, not a single node in isolation).
_CONTENT_FILL_CODES = frozenset(
    _SKELETON_CONTENT_CODES
    | {
        "duplicate_code",
        "criteria",
        "unknown_claw",
        "command",
        "prose_command",
        "field_too_long",
        "repetition_loop",
        "placeholder_bash",
        "sed_noop",
        "sed_ban",
        "missing_target",
    }
)


def _skeleton_intent_basic_records(
    nodes: list[PlanNode],
) -> list[PlanError]:
    """A content-less write node must declare a valid content intent.

    The basic shape rule (fires on every skeleton, like a schema
    error): a scaffold/file_writer node carrying neither "content"
    nor an edit form must carry inputs["content_intent"] naming
    one of SKELETON_CONTENT_INTENTS. Pre-filled nodes (content or
    edits present after materialization) need no intent.
    """
    records: list[PlanError] = []
    for node in nodes:
        if (node.assigned_claw or "") not in ("scaffold", "file_writer"):
            continue
        inputs = node.inputs or {}
        if isinstance(inputs.get("content"), str):
            continue
        intent = inputs.get("content_intent")
        if intent not in SKELETON_CONTENT_INTENTS:
            records.append(
                PlanError(
                    code="skeleton_intent",
                    message=(
                        f"Planner node '{node.id}' is a skeleton write "
                        "node with no content: its inputs must declare "
                        '"content_intent" as one of "create" (a new '
                        'file, filled with full content), "edit" (an '
                        "existing file, filled with scoped/anchored "
                        'edits), or "whole_file" (an existing file, '
                        "filled with a justified whole-file rewrite) "
                        f"— got {intent!r}. The skeleton carries no "
                        "file contents; each write node's payload is "
                        "emitted separately after the skeleton is "
                        "accepted."
                    ),
                    node_id=node.id,
                )
            )
    return records


# ── Campaign build #11: skeleton-intent correction + anchoring ──
#
# Exam arm #20 (2026-11-24): the initial skeleton exhausted all
# five attempts, four of them on skeleton_intent — the model kept
# declaring content_intent "create" for freetalon/pairing/store.py
# and server.py, which the standing exam tree already held. The
# rejection named the defect exactly and the model still never
# settled on a legal intent: whether a file exists is not a
# judgment the model should spend repair rounds re-deriving,
# because the system already knows it (build #4's inventory
# states it in the first prompt). Arm #19's skeleton declared
# "whole_file" for the same files and passed on attempt 2 — pure
# model variance on a deterministic fact.
#
# Two mechanisms, both deterministic:
#
# - INTENT CORRECTION (_correct_skeleton_intents): at skeleton
#   parse, before the remaining validation judges the attempt, a
#   declared intent that contradicts the tree is corrected
#   mechanically — "create" for a target that exists on disk
#   becomes "edit"; "edit" / "whole_file" for a target that
#   exists neither on disk nor earlier in the same plan becomes
#   "create". If the intent was the node's only defect the
#   attempt proceeds; if other errors coexist, validation judges
#   the corrected plan, so the repair round works from a
#   rejection that no longer contains the corrected defect.
#   Every correction is recorded — plan metadata on acceptance,
#   the rejection and the attempt record (build #9) otherwise —
#   visible, never silent. NOT correctable, by design:
#   "whole_file" against an existing target without the
#   justification + impact-check pair (a justification is the
#   model's claim to make, not a fact the system can fabricate);
#   "create" against a target an earlier node of the same plan
#   creates (a plan-internal contradiction, not a tree fact);
#   and a missing or invalid intent (the basic shape rule's
#   rejection, unchanged).
#
# - EXISTENCE ANCHORING (_skeleton_target_facts_block): every
#   skeleton repair brief gains the deterministic facts for the
#   rejected skeleton's write targets — exists on disk (with the
#   existing-file inventory's definition count when the
#   inventory has it), created earlier in this same plan, or
#   absent — plus the legal intents for each, the intent the
#   attempt declared, and the correction the system already
#   applied. Build #4's anchoring pattern (measured facts + a
#   standing instruction) for the defect class its facts never
#   covered: clause coverage and write scope, not existence.
#
# Both mechanisms read existence from ONE computation
# (_skeleton_write_target_facts — the validator's own: on disk
# through the parse's module-source cache, or written by an
# earlier node of the same plan; only in-tree targets judged),
# so the correction, the validator, and the brief can never
# disagree about what exists. The re-plan path shares the seam:
# replan_from_brief's skeleton phase parses through the same
# parse_planner_response(skeleton=True), so delta skeletons are
# corrected and anchored identically; their corrections write
# back through the brief (the build #7 channel) and the loop
# stamps them into the new version's metadata.


@dataclass(frozen=True)
class _SkeletonTargetFact:
    """One skeleton write target's existence facts (build #11)."""

    node: Any  # a PlanNode, or a lenient view of one (repair briefs)
    target: str  # the target exactly as the node declares it
    resolved: str  # the resolved on-disk path, as a string
    on_disk: bool  # the target exists in the tree now
    plan_prior: bool  # an earlier node of this same plan writes it

    @property
    def prior(self) -> bool:
        """The validator's existence verdict: on disk, or in-plan."""
        return self.on_disk or self.plan_prior


def _skeleton_write_target_facts(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    modules: Any | None = None,
) -> list[_SkeletonTargetFact]:
    """Existence facts for every judged skeleton write target.

    The one existence computation for skeleton intents (campaign
    build #11): a target is judged only when it resolves inside
    the repo root (the validator's own scope); it exists when it
    is on disk (read through the parse's module-source cache when
    one is given, so each module file is still read at most once
    per parse) or when an earlier node of this same plan writes
    it. Every judged target registers for the nodes after it —
    pre-filled nodes included — exactly as the validator's
    `existing` set always accumulated them.
    """
    root = index.root if index is not None else resolve_repo_root(repo_root)
    root_resolved = Path(root).resolve()
    existing: set[str] = set()
    facts: list[_SkeletonTargetFact] = []
    for node in nodes:
        target = _node_write_target(node)
        if target is None:
            continue
        resolved_path = _resolve_plan_target(target, root).resolve()
        if not resolved_path.is_relative_to(root_resolved):
            continue
        resolved = str(resolved_path)
        if modules is not None:
            on_disk = modules.source(resolved_path) is not None
        else:
            on_disk = resolved_path.is_file()
        plan_prior = resolved in existing
        existing.add(resolved)
        facts.append(
            _SkeletonTargetFact(
                node=node,
                target=target,
                resolved=resolved,
                on_disk=on_disk,
                plan_prior=plan_prior,
            )
        )
    return facts


def _correct_skeleton_intents(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    modules: Any | None = None,
) -> list[dict[str, Any]]:
    """Correct skeleton content intents that contradict the tree.

    The build #11 correction (see the section above for the rule
    and its deliberate exclusions). Mutates the corrected nodes'
    inputs in place — the fill phase reads the intent from the
    node, so the corrected intent is the one the payload is
    requested under — and returns one record per correction:
    {node, target, from, to, reason}. Pre-filled nodes are exempt,
    exactly as in the validator: their form is the model's own
    payload, judged by the ordinary rules.
    """
    corrections: list[dict[str, Any]] = []
    for fact in _skeleton_write_target_facts(
        nodes, repo_root, index, modules
    ):
        node = fact.node
        inputs = node.inputs or {}
        if not isinstance(inputs, dict):
            continue
        if isinstance(inputs.get("content"), str):
            continue
        intent = inputs.get("content_intent")
        corrected: str | None = None
        reason = ""
        if intent == "create" and fact.on_disk:
            corrected = "edit"
            reason = "the target already exists on disk"
        elif intent in ("edit", "whole_file") and not fact.prior:
            corrected = "create"
            reason = (
                "the target does not exist on disk and no earlier "
                "node of this plan creates it"
            )
        if corrected is None:
            continue
        inputs["content_intent"] = corrected
        corrections.append(
            {
                "node": node.id,
                "target": fact.target,
                "from": intent,
                "to": corrected,
                "reason": reason,
            }
        )
    return corrections


def _skeleton_target_facts_block(
    response_text: str,
    repo_root: str | Path | None,
    corrections: Collection[dict[str, Any]] = (),
) -> str:
    """The SKELETON TARGET FACTS block for a skeleton repair brief.

    Deterministic existence facts for the rejected skeleton's
    write targets (campaign build #11): for each target — whether
    it exists on disk (with the existing-file inventory's
    definition count when the inventory has it), whether an
    earlier node of this same skeleton creates it, or that it
    exists nowhere yet; the content intents that are legal for
    it; the intent the attempt declared; and the correction the
    system already applied to that declaration, when it applied
    one. Read leniently from the raw output (the
    _plan_attempt_facts pattern — the output may be too malformed
    for PlanNode validation) and fail-soft: any extraction
    failure returns "", because a facts failure must never mask
    the rejection it describes. "" when the output names no
    in-tree write target.
    """
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return ""
    nodes_raw = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes_raw, list):
        return ""
    try:
        root = resolve_repo_root(repo_root)
        root_resolved = Path(root).resolve()
        views = [
            view
            for view in (
                _lenient_node_view(raw, position)
                for position, raw in enumerate(nodes_raw, start=1)
            )
            if view is not None
        ]
        facts = _skeleton_write_target_facts(views, root)
        if not facts:
            return ""
        focus_dirs: set[str] = set()
        for fact in facts:
            try:
                parent = (
                    Path(fact.resolved)
                    .relative_to(root_resolved)
                    .parent.as_posix()
                )
            except ValueError:
                continue
            if parent not in ("", "."):
                focus_dirs.add(parent)
        inventory = dict(existing_file_inventory(root, sorted(focus_dirs)))
        corrected_by_target = {
            entry.get("target"): entry
            for entry in corrections
            if isinstance(entry, dict)
        }
        whole_file_pair = (
            '"whole_file" with a non-empty overwrite_justification '
            "AND an impact-check node"
        )
        lines = [
            "SKELETON TARGET FACTS (system — measured from the "
            "tree on disk and this skeleton's own node order, "
            "not from memory):"
        ]
        for fact in facts:
            inputs = fact.node.inputs or {}
            declared = inputs.get("content_intent")
            try:
                rel = Path(fact.resolved).relative_to(root_resolved).as_posix()
            except ValueError:
                rel = fact.target
            count = inventory.get(rel)
            count_text = (
                f" ({count} definitions)" if count is not None else ""
            )
            if fact.on_disk:
                state = f"EXISTS on disk{count_text}"
                legal = f'"edit", or {whole_file_pair}'
            elif fact.plan_prior:
                state = (
                    "does not exist on disk, but an earlier node "
                    "of this skeleton writes it, so it exists by "
                    "the time it is filled"
                )
                legal = f'"edit", or {whole_file_pair}'
            else:
                state = "does not exist yet"
                legal = '"create"'
            line = (
                f"- {fact.target}: {state}. Legal content "
                f"intents: {legal}."
            )
            if isinstance(inputs.get("content"), str):
                line += (
                    " This node carries its content already, so "
                    "no content intent applies to it."
                )
            elif declared is not None:
                line += f' Declared: "{declared}".'
                correction = corrected_by_target.get(fact.target)
                if (
                    correction is not None
                    and correction.get("from") == declared
                ):
                    line += (
                        " The system corrected that declaration "
                        f'to "{correction.get("to")}" for this '
                        "attempt's validation."
                    )
            lines.append(line)
        lines.append(
            "Declare every write node's content intent from "
            "these facts: whether a file exists is a measured "
            "fact, not a choice — an intent that contradicts it "
            "is corrected mechanically where the correction is "
            "unambiguous, and rejected where it is not (a "
            "whole-file rewrite of an existing file still needs "
            "YOUR overwrite_justification and impact-check node)."
        )
        return "\n" + "\n".join(lines) + "\n"
    except Exception:
        return ""


def _skeleton_intent_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    modules: Any | None = None,
) -> list[str]:
    """Judge skeleton content intents against the tree (gated rule).

    The skeleton-phase analogue of _modify_path_errors, in its
    gated slot: existence is judged exactly as there (on disk, or
    written by an earlier node of this same plan; only in-tree
    targets are judged). "create" against an existing file and
    "edit" against a missing file are contradictions the fill
    phase could only fail on; "whole_file" against an existing
    file must carry the modify rule's gated override (a non-empty
    overwrite_justification AND a companion impact-check node);
    "whole_file" against a missing file is a create mislabeled.
    Pre-filled nodes are exempt — their form was judged by the
    ordinary rules.

    Campaign build #11: existence comes from
    _skeleton_write_target_facts, the one computation the intent
    correction and the repair brief's target facts also read —
    and parse has already corrected the mechanically correctable
    contradictions before this runs, so what remains here is
    exactly the non-correctable residue (the unjustified
    whole-file rewrite, the plan-internal contradiction).
    """
    root = index.root if index is not None else resolve_repo_root(repo_root)
    errors: list[str] = []
    for fact in _skeleton_write_target_facts(
        nodes, repo_root, index, modules
    ):
        node = fact.node
        target = fact.target
        resolved = fact.resolved
        inputs = node.inputs or {}
        prior = fact.prior
        if isinstance(inputs.get("content"), str):
            continue
        intent = inputs.get("content_intent")
        if intent == "create" and prior:
            errors.append(
                f"Planner node '{node.id}' declares content_intent "
                f'"create" for {target!r}, which already exists — a '
                "create fills the node with a NEW file's full "
                "content and would replace the existing file whole. "
                'Use "edit" (scoped/anchored edits against the '
                'current file) or "whole_file" with the '
                "overwrite justification + impact-check pair."
            )
        elif intent == "edit" and not prior:
            errors.append(
                f"Planner node '{node.id}' declares content_intent "
                f'"edit" for {target!r}, which does not exist — '
                "there is no current file for the fill phase to "
                'edit. Use "create" so the node is filled with the '
                "new file's full content."
            )
        elif intent == "whole_file":
            if not prior:
                errors.append(
                    f"Planner node '{node.id}' declares content_intent "
                    f'"whole_file" for {target!r}, which does not '
                    'exist — a whole-file rewrite replaces an '
                    'existing file; use "create" for a new file.'
                )
            else:
                justification = inputs.get("overwrite_justification")
                has_justification = isinstance(
                    justification, str
                ) and bool(justification.strip())
                has_impact_check = False
                for other in nodes:
                    if other is node:
                        continue
                    if node.id not in (other.depends_on or []):
                        continue
                    named = (other.inputs or {}).get("impact_check_for")
                    if not isinstance(named, str) or not named:
                        continue
                    if (
                        str(_resolve_plan_target(named, root).resolve())
                        == resolved
                    ):
                        has_impact_check = True
                        break
                if not (has_justification and has_impact_check):
                    missing: list[str] = []
                    if not has_justification:
                        missing.append(
                            'a non-empty "overwrite_justification" '
                            "explaining why the change cannot be "
                            "expressed as edits"
                        )
                    if not has_impact_check:
                        missing.append(
                            "an impact-check node that depends on "
                            "this node and carries inputs "
                            '{"impact_check_for": "'
                            + target
                            + '"}'
                        )
                    errors.append(
                        f"Planner node '{node.id}' declares "
                        f'content_intent "whole_file" for {target!r} '
                        "without the whole-file override: the node "
                        "must also carry " + " AND ".join(missing) + "."
                    )
    return errors


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
    contract: Any = _CONTRACT_UNSUPPLIED,
    clauses: Any = _CLAUSES_UNSUPPLIED,
    plan_context: str = "authored",
    skeleton: bool = False,
    clause_mode: str = "full",
    clause_required: Collection[str] | None = None,
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

    ``plan_context`` (campaign build #2) names whose plan this is:
    "authored" (a model's fresh plan, the default), "derivative"
    (a repair seam's candidate — a mechanical derivative of an
    already-accepted plan), or "enrichment" (records being gathered
    to enrich a rejection that already exists). The two gated rules
    (whole_file_modify, suite_placement) judge how a plan's author
    CHOSE to express work, and each fires only on an authored plan
    that no other rule has rejected: a derivative candidate's nodes
    were judged at acceptance (its culprit's content is an edit
    product by construction), and an enrichment call serves a plan
    already under rejection — in both cases the plan is not the
    otherwise-acceptable plan these rules exist to steer. The
    clause-coverage rule (clause_coverage) is gated differently
    (campaign build #10): authored plan + a supplied clause list
    is its whole gate, and it contributes its records alongside
    any others — see its block below for the arm #19 evidence.

    Returns (records, cheap): ``cheap`` is True when every error is a
    stand-in / code-overwrite fake (retry #12) or a placeholder scaffold
    (retry #17), or placeholder values (cycle 6), or a suite run attached
    to a criterion command (cycle 12), or a stub-only module (2026-11-23)
    — a rejection the repair
    loop may re-prompt for free, without spending the repair budget.
    """
    if modules is None:
        modules = _ModuleSourceCache()
    records: list[PlanError] = []
    needs_index = any(
        node.assigned_claw in ("scaffold", "file_writer") for node in nodes
    ) or goal_text is not None
    if index is None and needs_index:
        index = build_repo_index(repo_root)
    plan_written: set[str] = set()
    for node in nodes:
        plan_written.update(_node_written_paths(node))
    for node in nodes:
        error = _claw_error(node)
        if error is not None:
            records.append(
                PlanError(code="unknown_claw", message=error, node_id=node.id)
            )
        records.extend(
            _error_records(
                "criteria",
                _criteria_errors(node, repo_root, plan_written=plan_written),
            )
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
    records.extend(
        _error_records(
            "unwritten_file",
            _unwritten_file_matches_errors(nodes, repo_root),
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
            "static_standin",
            _scaffold_unparseable_binding_errors(nodes, goal_text, index),
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
    placeholder_value_records = _error_records(
        "placeholder_value",
        _placeholder_value_errors(
            nodes, repo_root, goal_text, index, modules=modules
        ),
    )
    records.extend(placeholder_value_records)
    stub_only_records = _error_records(
        "stub_only_module",
        _stub_only_module_errors(nodes, repo_root, goal_text, index),
    )
    records.extend(stub_only_records)
    records.extend(
        _error_records(
            "sed_noop",
            _sed_noop_errors(nodes, repo_root, goal_text, index, modules=modules),
        )
    )
    records.extend(
        _error_records(
            "sed_ban",
            _sed_ban_errors(nodes, goal_text, index),
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
    records.extend(
        _error_records("missing_target", _missing_target_errors(nodes))
    )
    suite_criterion_records = _error_records(
        "suite_criterion_command",
        _suite_criterion_command_errors(nodes),
    )
    records.extend(suite_criterion_records)
    # Planner schema redesign (2026-11-19): the prose bounds. Not
    # cheap — a degenerate or bloated generation is a real defect
    # the repair round must regenerate against, not a mechanical
    # relocation it can re-prompt for free.
    records.extend(
        _error_records("field_too_long", _prose_length_errors(nodes))
    )
    records.extend(
        _error_records("repetition_loop", _repetition_loop_errors(nodes))
    )
    # Module contract coherence: runs only when the caller supplies
    # a contract context (parse_planner_response always does — the
    # effective contract, or None when none was declared, which
    # still arms the contract_missing trigger; the node-local
    # repair passes the plan's pin when it has one). Every other
    # caller keeps the pre-contract behavior exactly.
    if contract is not _CONTRACT_UNSUPPLIED:
        records.extend(_contract_error_records(nodes, contract, repo_root))
    # Campaign build #2: the gated authorship rules — the modify-
    # path rule (an existing file is changed with scoped edits, not
    # a whole-file rewrite) and the tiered criteria rule (a full-
    # suite run belongs only on the plan's final gate node). Each
    # fires ONLY when the plan is authored (plan_context) and every
    # other rule has passed: a plan rejected for substantive
    # defects gets those fixed first (the one-defect-class rhythm
    # of retry #20's placeholder suppression), and the rules bite
    # with full force on exactly the plans that would otherwise be
    # accepted — the only plans whose expression choices can do
    # harm, since a rejected plan never executes. Neither is cheap:
    # re-expressing a write as edits, or re-tiering a suite run, is
    # a real regeneration, not a mechanical relocation. The modify
    # rule runs first; when it fires, the suite rule waits for the
    # next round, for the same one-class-at-a-time reason. Campaign
    # build #3 added a third rule behind both — clause coverage —
    # under the identical gate; campaign build #10 moved it out
    # (see its own block below): the gate starved it — arm #19's
    # re-plan attempts each carried an unrelated 'command' record,
    # so the clause slot never ran at all.
    if skeleton:
        # Campaign build #5: content-dependent validators judge a
        # write node's payload; a skeleton write node has none yet
        # (its content arrives in the fill phase, where the same
        # validators run per node). Their records against a
        # content-less write node are artifacts of the missing
        # payload, not defects of the skeleton — drop exactly those.
        contentless = {
            node.id
            for node in nodes
            if (node.assigned_claw or "") in ("scaffold", "file_writer")
            and not isinstance((node.inputs or {}).get("content"), str)
        }
        if contentless:
            records = [
                record
                for record in records
                if not (
                    record.node_id in contentless
                    and record.code in _SKELETON_CONTENT_CODES
                )
            ]
    if plan_context == "authored" and not records:
        if skeleton:
            # Campaign build #5: at the skeleton phase the modify-
            # path rule's analogue is the content-intent legality
            # rule — a "create" intent against a file that exists,
            # an "edit" intent against a file that does not, or a
            # "whole_file" intent without the justification +
            # impact-check pair the modify rule demands. It takes
            # the modify rule's slot in the gated sequence; the
            # suite-placement rule behind it, and the independent
            # clause-coverage slot below, run on the skeleton
            # exactly as on a full plan.
            modify_records = _error_records(
                "skeleton_intent",
                _skeleton_intent_errors(nodes, repo_root, index, modules),
            )
        else:
            modify_records = _error_records(
                "whole_file_modify",
                _modify_path_errors(nodes, repo_root, index, modules),
            )
        records.extend(modify_records)
        if not modify_records:
            suite_records = _error_records(
                "suite_placement",
                _suite_placement_errors(nodes, repo_root, index),
            )
            records.extend(suite_records)
    if plan_context == "authored" and clauses is not _CLAUSES_UNSUPPLIED:
        # Campaign build #3: the clause-coverage rule — every
        # frozen contract clause must be implemented by at least
        # one node and verified by at least one node (the panel's
        # requirement-to-node binding). It fires only on an
        # authored plan, and only when the caller supplied the
        # clause list extracted from the goal-named spec (the
        # sentinel keeps corpus and other clause-less callers
        # byte-identical). Not cheap: completing a coverage map
        # is a real regeneration of the plan's declarations.
        #
        # Campaign build #7: a re-plan parse is marked
        # clause_mode="delta" and judged by the delta rule in the
        # same slot, under the same code — every emitted node
        # must declare its mapping, and the brief's open clauses
        # (clause_required) must be implemented and verified
        # within the delta. Full coverage of the frozen list is
        # the initial planner's burden, not a repair delta's
        # (arm #16: the full rule rejected the re-plan wholesale
        # and no v2 ever landed). The delta branch requires a
        # non-empty frozen list: with no contract there is
        # nothing to declare against, and the empty-list full
        # rule below is exactly the pre-build-#7 behavior for
        # contract-less plans.
        #
        # Campaign build #10: the clause slot is INDEPENDENT of
        # the gated sequence above. Through build #9 it sat
        # behind both authorship rules AND behind an empty
        # record set, so any other rejection — however unrelated
        # — suppressed it entirely: in arm #19 all three re-plan
        # attempts carried a 'command' record, the slot never
        # ran, and the delta rule went a third arm without ever
        # judging a live delta (one attempt's dropped
        # declarations went unflagged for the same reason).
        # Clause coverage is a judgment about the contract, not
        # about how the author expressed the work, so it does
        # not belong to the one-defect-class rhythm: it now runs
        # whenever its own gate (authored plan, clauses
        # supplied) holds and contributes its records alongside
        # whatever else the plan is rejected for, so one repair
        # round can fix every defect at once. The modify/suite
        # pair keeps the gated rhythm between themselves,
        # unchanged.
        if clause_mode == "delta" and clauses:
            clause_errors = clause_delta_errors(
                nodes, clauses, clause_required or ()
            )
        else:
            clause_errors = clause_coverage_errors(nodes, clauses)
        records.extend(
            _error_records(
                "clause_coverage",
                clause_errors,
            )
        )
    # Cheap rejection (retry #12): the only errors are stand-in /
    # code-overwrite fakes, so re-prompting costs the model nothing to
    # fix and must not spend the repair budget. A rejection whose only
    # errors are placeholder scaffolds is cheap too (retry #17): a
    # comment stub costs the model nothing to replace with real code —
    # and so is a rejection whose only errors are placeholder bash
    # heredocs (retry #20), and so is one whose only errors are
    # placeholder values (cycle 6): a bare '...' costs the model nothing
    # to replace with the real expression. A missing-target rejection
    # (cycle 7) is NOT cheap: the node is structurally incomplete — a
    # schema-class defect, like the raw-node schema errors. Any other
    # error mixed in makes it a real rejection. A suite-criterion
    # rejection (cycle 12) is cheap by the same rationale as
    # placeholder values: the plan is executable as written except
    # that the suite run sits on a criterion instead of the node's
    # own command — a mechanical relocation, fixed in one re-prompt.
    # A stub-only module rejection (2026-11-23) is cheap by the
    # placeholder rationale too: the module's shape is complete
    # except that every definition defers to a step that does not
    # exist — real code is one re-prompt away.
    cheap = (
        bool(records)
        and len(standin_records)
        + len(placeholder_records)
        + len(placeholder_value_records)
        + len(stub_only_records)
        + len(suite_criterion_records)
        == len(records)
    )
    return records, cheap


# --- Campaign build #2: the gated authorship rules -------------------
#
# Registered in _collect_plan_error_records under the gate described
# there (authored plans only, and only when every other rule has
# passed). Both rules judge how a plan's author expressed the work,
# never what the work is.


def _modify_path_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
    modules: Any | None = None,
) -> list[str]:
    """Reject authored whole-file writes to files that already exist.

    Campaign build #2 (step 1.5), from live proof-2: the accepted
    plan's first node rewrote the EXISTING freetalon/api.py
    whole-file and broke 19 sibling endpoint tests — a whole-file
    write replaces every line it does not mention, so retyping a
    file from memory silently reverts whatever the retype omits or
    misremembers. Prior art is unanimous (Aider / SWE-agent /
    OpenHands, and Gemini's consultation answer): create and
    modify are different operations, and no modify path is a
    wholesale rewrite. The def-retention guard
    (_scaffold_overwrite_errors) catches a rewrite that DROPS
    definitions; this rule is the structural one — it judges the
    form, whatever the content happens to keep.

    A write node violates when its target exists — on disk, or
    written by an earlier node of this same plan (by execution
    time the file is there either way) — and its content is an
    authored whole-file payload. Edit-derived content is exempt:
    materialization stamps inputs["_materialized_from"] on nodes
    whose content the system produced from an anchored or scoped
    edit, and parse strips the key from raw model nodes, so only
    materialization can set it. The gated override: the node
    carries a non-empty "overwrite_justification" AND a companion
    impact-check node exists — another node that depends on the
    write node and carries inputs["impact_check_for"] naming the
    same target (path-keyed because normalization remaps node ids,
    never paths). The justification is for the cases edits cannot
    carry (an edit set that repeatedly failed to apply, or a
    rewrite of more than about two-thirds of the file); the impact
    check is the symbol/test evidence the rewrite is safe.

    Two scoping decisions, both from the rule's own wording ("a
    target path that EXISTS in the tree"): only targets resolving
    INSIDE the repo root are judged — a plan's scratch outputs
    outside the tree (workspace scripts, /tmp artifacts) are not
    the tree the planner is modifying — and existence is read
    through the validation pass's module-source cache when the
    collector threaded one in, so this rule judges the same tree
    presentation as every other validator (the recorded-fixture
    pins that present a file as absent apply to it uniformly).
    """
    root = index.root if index is not None else resolve_repo_root(repo_root)
    root_resolved = Path(root).resolve()
    errors: list[str] = []
    existing: set[str] = set()
    for node in nodes:
        target = _node_write_target(node)
        if target is None:
            continue
        resolved_path = _resolve_plan_target(target, root).resolve()
        if not resolved_path.is_relative_to(root_resolved):
            continue
        resolved = str(resolved_path)
        content = (node.inputs or {}).get("content")
        if modules is not None:
            on_disk = modules.source(resolved_path) is not None
        else:
            on_disk = resolved_path.is_file()
        prior = resolved in existing or on_disk
        if isinstance(content, str) and prior:
            inputs = node.inputs or {}
            if inputs.get("_materialized_from") not in (
                "anchored_edit",
                "scoped_edits",
            ):
                justification = inputs.get("overwrite_justification")
                has_justification = isinstance(
                    justification, str
                ) and bool(justification.strip())
                has_impact_check = False
                for other in nodes:
                    if other is node:
                        continue
                    if node.id not in (other.depends_on or []):
                        continue
                    named = (other.inputs or {}).get("impact_check_for")
                    if not isinstance(named, str) or not named:
                        continue
                    if (
                        str(_resolve_plan_target(named, root).resolve())
                        == resolved
                    ):
                        has_impact_check = True
                        break
                if not (has_justification and has_impact_check):
                    missing: list[str] = []
                    if not has_justification:
                        missing.append(
                            'a non-empty "overwrite_justification" '
                            "explaining why the change cannot be "
                            "expressed as edits"
                        )
                    if not has_impact_check:
                        missing.append(
                            "an impact-check node that depends on "
                            "this node and carries inputs "
                            '{"impact_check_for": "'
                            + target
                            + '"} — a node running the touched '
                            "module's tests or a symbol-level diff "
                            "of the rewrite"
                        )
                    errors.append(
                        "Planner node '"
                        + node.id
                        + "' writes the whole file "
                        + repr(target)
                        + ", which already exists — a whole-file "
                        "write replaces every line it does not "
                        "mention, so retyping an existing file from "
                        "memory silently reverts whatever the "
                        "retype omits or misremembers. Change the "
                        "file with scoped edits instead: inputs "
                        '{"file_path": "'
                        + target
                        + '", "edits": [{"search": "<text copied '
                        "exactly from the current file>\", "
                        '"replace": "<the new text>"}]} with NO '
                        '"content" — or, for one Python definition, '
                        "an anchored edit. Reserve whole-file "
                        '"content" for creating a NEW file. To keep '
                        "a whole-file rewrite of an existing file, "
                        "the node must also carry "
                        + " AND ".join(missing)
                        + "."
                    )
        existing.add(resolved)
    return errors


def _transitive_ancestors(
    node: PlanNode, by_id: dict[str, PlanNode]
) -> set[str]:
    ancestors: set[str] = set()
    stack = list(node.depends_on or [])
    while stack:
        current = stack.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        parent = by_id.get(current)
        if parent is not None:
            stack.extend(parent.depends_on or [])
    return ancestors


def _is_gate_node(node: PlanNode, nodes: list[PlanNode]) -> bool:
    """True when ``node`` is a plan's final verification gate.

    A gate node transitively depends on every node that writes a
    file, and no write node transitively depends on it — so a
    suite run placed on it measures the plan's complete product,
    and nothing written later escapes its verdict. Trailing
    non-write steps (a live probe, a summary) do not disqualify
    it: the barrier's purpose is that the suite is the last word
    over the plan's WRITES, not that it is the plan's last node.
    When the plan writes nothing, the ancestry condition is
    vacuous: a pure verification plan's suite node is its gate.
    """
    by_id = {item.id: item for item in nodes}
    ancestors = _transitive_ancestors(node, by_id)
    writes = [
        other
        for other in nodes
        if other.id != node.id and _node_written_paths(other)
    ]
    if any(other.id not in ancestors for other in writes):
        return False
    return all(
        node.id not in _transitive_ancestors(other, by_id)
        for other in writes
    )


def _suite_placement_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None,
    index: Any | None = None,
) -> list[str]:
    """Reject full-suite runs placed anywhere but the final gate.

    Campaign build #2 (tiered criteria): proof-2's plan carried
    ~13-minute full-suite runs as mid-plan node criteria — two of
    them burned ~26 minutes, and a suite run attached to a timed
    criterion is what manufactured the false FAIL record behind
    the judge-gate defect. Prior art agrees (Aider lints only
    touched files; SWE-agent's in-loop check is a seconds-long
    reproduction script; FAIL_TO_PASS lives in the grading layer):
    the full suite is a boundary artifact, never a per-step
    assertion. Detection is the executor's own definition,
    _command_runs_full_suite, applied to a node's own command and
    to criterion commands — the command forms are where the
    measured harm lives (proof-2's ~13-minute node criteria; the
    60-second criterion-command timeout that manufactured the
    judge-gate defect's false FAIL). A tests_pass criterion over
    the whole tests directory is deliberately NOT this rule's
    concern: it is the canonical cycle-9 verification shape,
    measured through measure's own budget rather than the
    criterion-command cap, and its failure is an ordinary
    measured FAIL the judge can re-plan on. The prompts still
    steer criteria toward targeted tests; this rule hard-stops
    only the command forms. The exit_code-criterion-command
    placement is cycle 12's cheap error already — when it fires,
    this rule is gated off with the rest, so the two never
    double-name. A node carrying a non-empty
    "suite_justification" is exempt.
    """
    root = index.root if index is not None else resolve_repo_root(repo_root)
    errors: list[str] = []
    for node in nodes:
        inputs = node.inputs or {}
        placement: str | None = None
        if (node.assigned_claw or "") in ("bash", "shell_exec"):
            command = inputs.get("command") or inputs.get("cmd")
            if isinstance(command, str) and _command_runs_full_suite(command):
                placement = "its own command"
        if placement is None:
            for criterion in node.criteria or []:
                if not isinstance(criterion, dict):
                    continue
                command = criterion.get("command")
                if isinstance(command, str) and _command_runs_full_suite(
                    command
                ):
                    placement = "a criterion command"
                    break

        if placement is None:
            continue
        justification = inputs.get("suite_justification")
        if isinstance(justification, str) and justification.strip():
            continue
        if _is_gate_node(node, nodes):
            continue
        errors.append(
            "Planner node '"
            + node.id
            + "' runs the full test suite as "
            + placement
            + " — the full suite takes ~13 minutes on this repo "
            "and is a synchronization barrier, not a per-step "
            "assertion: placed mid-plan it makes every earlier step "
            "wait behind a run that can only report the accumulated "
            "state, and a suite run under a criterion's timeout "
            "manufactures failures that belong to no step. Default "
            "to TARGETED tests for the module the node touches "
            '(for example "python -m unittest tests.test_<module>" '
            "— seconds). Reserve the full suite for ONE final gate "
            "node: a node that depends (directly or transitively) "
            "on every write node in the plan and that no write "
            "node follows. Or "
            'carry a non-empty "suite_justification" on this node '
            "explaining why the suite must run here."
        )
    return errors


# --- Anti-fake validation (retry #2, run-20261117T054715Z-698173) ---

_LITERAL_WRITE_RE = re.compile(
    r"^\s*(?P<verb>echo|printf)\s+(?P<body>" + _ECHO_BODY_PATTERN + r")\s*(?:>>|>)\s*(?P<target>\S+)",
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
        if quote == "'":
            # Single quotes have no escapes in shell: the next single
            # quote closes the segment.
            close = body.find(quote, index + 1)
            if close == -1:
                break
            segments.append(body[index + 1 : close])
            index = close + 1
        elif quote == '"':
            # Double quotes: a \" is an escaped quote, not the end of
            # the segment (and \\ escapes the backslash) — the old
            # next-quote pairing split a body like
            # "{\"status\": \"ok\"}" at the first escaped quote and
            # lost the write. The shell consumes those two escapes
            # itself, so the segment carries the unescaped character
            # (a JSON literal written this way becomes valid JSON);
            # every other backslash is the shell's literal text and
            # stays for the echo-escape interpreter downstream.
            chars: list[str] = []
            cursor = index + 1
            while cursor < len(body):
                ch = body[cursor]
                if (
                    ch == "\\"
                    and cursor + 1 < len(body)
                    and body[cursor + 1] in ('"', "\\")
                ):
                    chars.append(body[cursor + 1])
                    cursor += 2
                    continue
                if ch == '"':
                    break
                chars.append(ch)
                cursor += 1
            if cursor >= len(body):
                break
            segments.append("".join(chars))
            index = cursor + 1
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
    if (
        match.group("verb") == "printf"
        and len(segments) > 1
        and "%" in segments[0]
    ):
        # printf's first argument is the FORMAT, not written content:
        # printf '%s' '<literal>' writes the argument. (A printf whose
        # format carries no directives, or that has no argument
        # segments, writes the format itself and keeps it.)
        segments = segments[1:]
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
    4. A literal write (bash or scaffold) whose JSON binds measured
       fields to constants, into a non-code file shapes 1-2 do not
       claim: a fabricated measurement is manufactured evidence in a
       data file too, under any criterion — shape 3's principle
       applied to the literal writes shapes 1-2 own (gap closure,
       2026-11-20). Ordinary literal data stays legitimate file
       production under the data-file exemption (a JSON config states
       settings; it does not report probe results): only the measured
       bindings fire.
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
            claimed = False
            if claw in ("bash", "shell_exec"):
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
                        claimed = True
                        break
            if claimed:
                continue
            bindings = _hardcoded_measured_bindings(literal)
            if bindings:
                errors.append(
                    f"Planner node '{node.id}' writes a hardcoded answer into "
                    f"{target!r}: the literal binds measured fields to "
                    f"constants ({', '.join(bindings)}) instead of measuring "
                    f"them — whether a service is reachable and what a sensor "
                    f"reads exist only by probing (call the service, run "
                    f"nvidia-smi), so a constant is a fabricated measurement. "
                    f"Produce the file from a real probe instead (run the "
                    f"check and write ITS output).{grounded}"
                )
        if claw in ("bash", "shell_exec"):
            command = node.inputs.get("command") or node.inputs.get("cmd")
            if isinstance(command, str):
                errors.extend(
                    _bash_payload_standin_errors(node, command, grounded)
                )
    return errors


# Verbs that, aimed at a path literal from earlier in the same sentence,
# make that literal the goal's subject (endpoint work the goal asks for).
_ROUTE_SUBJECT_VERBS = (
    "add",
    "build",
    "change",
    "create",
    "delete",
    "extend",
    "fix",
    "implement",
    "introduce",
    "modify",
    "remove",
    "update",
)


def _path_literal_is_route_subject(text: str, match: re.Match[str]) -> bool:
    """Whether a path literal in a goal is the goal's subject.

    The check is local to the sentence (or line) enclosing the literal:
    the sentence must frame the literal as endpoint work — call it an
    endpoint/route, or direct a build/change verb at it from earlier in
    the same sentence. Spec prose that merely contains the literal (a
    contract's ``- POST /x: 201`` listing, ``method/path/status``) frames
    nothing, so the literal is not a route subject.
    """
    start = max(
        text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start())
    ) + 1
    ends = [
        pos
        for pos in (text.find(".", match.end()), text.find("\n", match.end()))
        if pos != -1
    ]
    end = min(ends) if ends else len(text)
    sentence = text[start:end]
    if re.search(r"\b(endpoint|route)\b", sentence):
        return True
    before = sentence[: match.start() - start]
    return any(
        re.search(rf"\b{verb}\b", before) for verb in _ROUTE_SUBJECT_VERBS
    )


def _routes_named_in_goal(goal_text: str, index: Any) -> list[tuple[str, str]]:
    """Existing routes the goal makes its subject, as (route, module) pairs.

    A route is captured only when the goal makes it the subject: the
    route name with ``endpoint``/``route`` (``health endpoint``), or a
    standalone path literal (``/health``) in a sentence that frames it
    as endpoint work (see _path_literal_is_route_subject). A path-like
    token that merely appears inside spec prose — ``method/path/status``
    in a contract description, a ``- POST /pairing/register: 201``
    contract listing, a file path like ``freetalon/pairing/server.py`` —
    captures nothing. Bare words like ``status`` or ``metrics`` do not
    count on their own — they are too common to mean the API route.
    """
    text = (goal_text or "").lower()
    named: list[tuple[str, str]] = []
    for route in sorted(index.routes):
        name = route.strip("/").split("/", 1)[0]
        if not name:
            continue
        word_hit = re.search(rf"\b{re.escape(name)}\s+(endpoint|route)\b", text)
        path_hit = any(
            _path_literal_is_route_subject(text, match)
            for match in re.finditer(
                rf"(?<![A-Za-z]){re.escape('/' + name)}(?![A-Za-z0-9_.\-])",
                text,
            )
        )
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
    Cycle 5: a failed node's entry also carries "diagnostics" — the
    bounded tail of the node's own captured output, extracted by the
    judge — rendered as its own block under the node's line. It is
    output the node produced in this run, not a command or a memory
    record, and the failing assertion it names is exactly what the
    planner needs to change the substance instead of re-emitting it.
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
    pinned_contract, _pin_errors = _normalize_contract(
        brief.get("module_contract")
    )
    if pinned_contract:
        lines.append("")
        lines.append(_render_contract_block(pinned_contract, pinned=True))
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
        diagnostics = node.get("diagnostics")
        if isinstance(diagnostics, str) and diagnostics:
            lines.append(
                f"Captured output from failed node {node.get('id', '')} "
                f"(tail):\n{diagnostics}"
            )
        repeated = node.get("repeated_diagnostic")
        if isinstance(repeated, str) and repeated:
            lines.append(
                f"Repeated failure: node {node.get('id', '')} produced "
                f"the same failure already briefed for plan version "
                f"{node.get('repeated_from_version', '')}: {repeated}. "
                "The previous delta plan did not remove it; change what "
                "produces this result, not just the steps around it."
            )
    # Re-plan anchoring (2026-11-23): the judge's computed record —
    # the failing tests by name, the stagnation verdict when that
    # set is identical to the previous brief's, and the produced-file
    # lists. Rendered only when the judge stamped the fields and
    # they are non-empty: a brief without them (every brief shape
    # before this change) renders byte-identically to before.
    failing_tests = brief.get("failing_tests") or []
    if failing_tests:
        lines.append(
            "Failing tests (named by the system from the failed "
            "nodes' captured output): "
            + ", ".join(str(test) for test in failing_tests)
        )
    stagnation = brief.get("stagnation")
    if isinstance(stagnation, dict) and stagnation.get("failing_tests"):
        last_change = stagnation.get("last_change_files") or []
        lines.append(
            "Stagnation (system): the failing tests above are "
            "IDENTICAL to the failing tests briefed for plan version "
            f"{stagnation.get('unchanged_since_version', '')} — they "
            "failed before the last change and they failed after it. "
            "The last change modified: "
            + (", ".join(str(target) for target in last_change) or "(no files)")
            + ". A further change confined to those same files is "
            "unlikely to move them. Widen the suspect set — the "
            "modules the failing tests exercise, the tests "
            "themselves, and the files listed below — instead of "
            "rewriting the same files again."
        )
    never_revised = brief.get("never_revised_files") or []
    if never_revised:
        lines.append(
            "Files produced by earlier plan versions that the most "
            "recent version did not revise: "
            + ", ".join(str(target) for target in never_revised)
        )
    missing_declared = brief.get("missing_declared_files") or []
    if missing_declared:
        lines.append(
            "Files declared as write targets by an earlier plan "
            "version but missing on disk (declared, never produced): "
            + ", ".join(str(target) for target in missing_declared)
        )
    # Contract clauses (campaign build #3): the judge's computed
    # list of clauses the frozen contract list still shows as
    # unverified — obligations no failing test may ever have
    # exercised. Rendered only when the judge stamped the field
    # and it is non-empty; a brief without it renders as before.
    uncovered_clauses = brief.get("uncovered_clauses") or []
    if uncovered_clauses:
        lines.append(
            "Uncovered contract clauses (system): the contract's "
            "frozen clause list is not yet verified for the clauses "
            "below. They stay open until a node implements them, a "
            "verification node proves them, and the system's probes "
            "pass — whether or not any failing test names them. "
            "Target them in the delta plan and keep every node's "
            "inputs.clauses declarations accurate:"
        )
        for entry in uncovered_clauses:
            if not isinstance(entry, dict):
                continue
            reasons = "; ".join(
                str(reason) for reason in entry.get("reasons") or []
            )
            lines.append(
                f"- {entry.get('id', '')} ({entry.get('heading', '')}): "
                f"{reasons}"
            )
    # Escalation conversion (campaign build #6): when the judge's
    # escalation was converted into this re-plan, the brief says so,
    # with the timeout diagnostics (the wall the last version hit)
    # and the clause-probe failures verbatim (the system's own
    # checks, outside the plan's tests). Rendered only when the
    # loop stamped the fields and they are non-empty; a brief
    # without them renders byte-identically to before.
    conversion = brief.get("escalation_conversion")
    if isinstance(conversion, dict) and conversion:
        lines.append(
            "Escalation converted (system): the judge escalated "
            f"({conversion.get('original_reason', '')}) but re-plan "
            "budget remained, so the run was NOT ended \u2014 this "
            "re-plan replaces the escalation. The failure is "
            "informative: address its evidence below, or the delta "
            "plan meets the same wall."
        )
    timeout_records = brief.get("timeout_diagnostics") or []
    if timeout_records:
        lines.append(
            "Timeout diagnostics (system): these nodes hit their "
            "hard timeout on every attempt \u2014 what they ran "
            "against did not finish (a hang, not a slow pass):"
        )
        for entry in timeout_records:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"- {entry.get('node_id', '')}: command "
                f"{entry.get('command')!r} timed out after "
                f"{entry.get('timeout_s', '')}s "
                f"(attempts: {entry.get('attempts', '')})"
            )
    probe_failure_records = brief.get("contract_probe_failures") or []
    if probe_failure_records:
        lines.append(
            "Contract probe failures (system, verbatim): the "
            "system's deterministic probes against the produced "
            "tree failed \u2014 defects in the produced code, "
            "independent of the plan's own tests:"
        )
        for record in probe_failure_records:
            if not isinstance(record, dict):
                continue
            lines.append(
                f"- {record.get('clause_id', '')} "
                f"[{record.get('probe', '')}]: {record.get('detail', '')}"
            )
    # Campaign build #12: the derived suspect — when the failed
    # flows all exercised one produced file, that file and its
    # failed flows are the delta's target list, named as such so
    # the planner aims at the flows even when the repair seams
    # declined. Rendered only when the loop stamped the map;
    # a brief without it renders byte-identically to before.
    probe_suspect = brief.get("probe_suspect")
    if isinstance(probe_suspect, dict) and probe_suspect:
        lines.append(
            "Probe suspect (system): every failed flow above "
            f"exercised one produced file — "
            f"{probe_suspect.get('file', '')}. Treat that file "
            "and its failed flows as this delta's target list:"
        )
        for entry in probe_suspect.get("flows") or []:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"- {entry.get('flow', '')}: {entry.get('detail', '')}"
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


def _sibling_test_module(index: Any, module: str) -> Path | None:
    """The repo's existing test module for a module, or None.

    The repo's own convention: freetalon/api.py is tested by
    tests/test_api.py — test_<stem>.py in a tests/ directory at
    the repo root. The repo index walks only the package, so the
    sibling is resolved on disk: the conventional location first,
    then a bounded walk for the same name elsewhere in the tree
    (the package itself, VCS internals, the venv, and the model
    store pruned). Existence is all that is claimed here; the
    caller reads and parses the file.
    """
    wanted = f"test_{Path(module).stem}.py"
    root = Path(index.root)
    conventional = root / "tests" / wanted
    if conventional.is_file():
        return conventional
    pruned = {".git", ".venv", "models", "__pycache__", Path(module).parts[0]}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in pruned)
        if wanted in filenames:
            found.append(Path(dirpath) / wanted)
    if not found:
        return None
    found.sort(key=lambda p: (len(p.parts), p.as_posix()))
    return found[0]


def _test_module_construction(source: str) -> str:
    """The construction excerpt of a test module too big to show whole.

    Imports, module-level statements and helpers, and every test
    class's setUp/tearDown, verbatim — the parts that show how the
    subject is built and reached. The test methods themselves are
    omitted: the pattern being taught is construction, not
    assertion. Capped like the structure skeleton.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    pieces: list[str] = []
    for node in tree.body:
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Assign,
                ast.AnnAssign,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
            ),
        ):
            segment = ast.get_source_segment(source, node)
            if segment:
                pieces.append(segment)
        elif isinstance(node, ast.ClassDef):
            methods = [
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name
                in ("setUp", "tearDown", "setUpClass", "tearDownClass")
            ]
            segments = [
                segment
                for segment in (
                    ast.get_source_segment(source, method) for method in methods
                )
                if segment
            ]
            if segments:
                pieces.append(
                    f"class {node.name}:\n" + "\n\n".join(segments)
                )
    excerpt = "\n\n".join(pieces)
    if len(excerpt) <= _STRUCTURE_SKELETON_MAX_CHARS:
        return excerpt
    cut = excerpt.rfind("\n", 0, _STRUCTURE_SKELETON_MAX_CHARS)
    if cut <= 0:
        cut = _STRUCTURE_SKELETON_MAX_CHARS
    return excerpt[:cut] + "\n# ... (construction excerpt truncated here)"


def _sibling_test_grounding(index: Any, module: str) -> str:
    """Grounding: how the repo's own tests construct the module's subject.

    The route grounding shows the model the serving module's
    source but never its tests, so the model invents the subject's
    construction — and invents it wrong: across re-runs #9-#10 the
    models produced four distinct broken constructions of the API
    handler (a bare RequestHandlerClass(), a MagicMock spec,
    _httpd.Handler(), RequestHandlerClass missing its server
    argument) while tests/test_api.py, never shown, constructs the
    server correctly. The sibling test module rides along in the
    shape: whole, when it fits the same full-source budget as the
    serving module itself; as its construction excerpt when it
    does not. "" when the module has no sibling test module.
    """
    path = _sibling_test_module(index, module)
    if path is None:
        return ""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        ast.parse(source)
    except SyntaxError:
        return ""
    if len(source) <= _STRUCTURE_FULL_SOURCE_MAX_CHARS:
        body, form = source.rstrip(), "full current source"
    else:
        body, form = _test_module_construction(source), "construction excerpt"
    if not body:
        return ""
    return (
        f"EXISTING TESTS — {path} already tests this module. Node "
        "2's tests must construct the subject the way these tests "
        "do: copy their construction — what they build, in what "
        "order, and how they reach the running subject — instead "
        "of inventing a construction of your own (a construction "
        "these tests do not use, like instantiating an inner "
        "class of the subject directly, does not run). The test "
        f"module's {form} is below."
        f"\n```python\n{body}\n```"
    )


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
        # Cycle 14: the module's tests ride along with its source —
        # the model has always been shown how the endpoint is
        # served, never how the repo constructs it in a test, and
        # the invented constructions are the campaign's terminal
        # defect (see _sibling_test_grounding).
        tests_block = _sibling_test_grounding(index, module)
        if tests_block:
            lines.append(tests_block)
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
        "placeholder_value",  # bare Ellipsis assigned as a value (cycle 6)
        "missing_target",  # scaffold node names no target file (cycle 7)
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
# drift apart. Cycle 7: the sketch no longer extends the branch —
# the table's step-1 role is now naming the fields in the step-1
# GUIDANCE, so the extension is the model's own code to write; the
# two steps still read the one table, so they still cannot drift.
_SKELETON_ROUTE_FIELDS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "/health": (
        ("service_status", None),
        ("ollama_reachable", "bool"),
        ("gpu_temperature", "(int, float)"),
    ),
}


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
    same way). The blank became the anchored definition's REAL
    current source — signature and full body sliced verbatim from
    the module at the anchor's span (decorators included), at its
    true file indentation.

    Retry #32 also rebuilt the route branch in code form when its
    body was a single ``return <call>(..., {dict})``: the real
    dict bound to a payload name, one ``payload["<field>"] = ...``
    line per derived field (_SKELETON_ROUTE_FIELDS), the real
    call returning the payload — the ``...`` values as open slots
    the step-1 guidance told the model to replace with real
    computations. Cycle 7 removes the rebuild: across five live
    runs the model copied the sketch verbatim, slots and all,
    every time — a worked form the model can copy is a form it
    WILL copy, and the slot lines taught the placeholder shape
    the rest of the harness then had to reject (cycle 6's
    placeholder_value names it). The sketch is now the definition
    exactly as it stands, for every branch shape; the extension
    is the model's own code to write — the step-1 guidance names
    the fields and demands the computations. A verbatim copy of
    the sketch materializes as the module unchanged: the
    anchored-edit contract accepts any complete version of the
    definition, and whether the goal's change is in it is judged
    by the plan's own tests at execution, not by a planning gate
    (cycle 7 built that gate and reverted it — the suite's
    grounded fixtures encode the replace contract across ~98
    pins, and the cycle's rule is to change the ask, not add a
    gate). ``route`` no longer shapes the sketch (the guidance
    reads the fields table); it stays in the signature for the
    call site. Returns None when the anchor has no span in the
    source.
    """
    for span_name, span_start, span_end, _span_indent in _definition_spans(
        module_source
    ):
        if span_name == anchor:
            lines = module_source.splitlines()
            return "\n".join(lines[span_start - 1 : span_end])
    return None


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
    judges on content like any other delta. Cycle 7: five live runs
    copied the slot sketch verbatim anyway — the guidance said
    "replace the slots" and the model returned the slots — so the
    sketch no longer extends the branch at all: step-1's content is
    the definition exactly as it stands, and the guidance names the
    fields and demands the computations as the model's own code
    (see _anchored_delta_sketch).
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
        # Cycle 7: the sketch is the definition exactly as it
        # stands — the retry-#32 branch rebuild (the payload
        # slots) is gone, because the model copied it verbatim in
        # five live runs; the extension is the model's own code
        # to write, and the step-1 guidance below carries the
        # field names and the computation demand instead.
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
        # Cycle 7: the fields the extended branch must report are
        # named here, in the guidance — the sketch itself no longer
        # carries them as "..." slots (the model copied the slots
        # verbatim in five live runs). The demand is for the model's
        # own computations, in the anti-fabrication voice the
        # stand-in checks already use.
        fields = [
            field for field, _types in _SKELETON_ROUTE_FIELDS.get(route, ())
        ]
        fields_guidance = ""
        if fields:
            fields_guidance = (
                " The extended branch must also report "
                + ", ".join(fields)
                + ": write the real code that computes each one "
                "inside the definition — probe the service or run "
                "the command the goal names and parse its output; "
                "where a probe can fail, attempt it and report "
                "None (False for a yes/no field) when it fails. "
                "Never assign an invented constant for a value "
                "that exists only by measuring it — a fabricated "
                "reading is a wrong answer, not a shortcut — and "
                "never assign \"...\" (a placeholder, rejected by "
                "name at planning)."
            )
        step1_guidance = (
            "Step-1 is an anchored edit of the serving module: its "
            "\"content\" is the anchored definition's current code, "
            f"exactly as it stands in the file today — the {route} "
            "branch is NOT extended yet. Return the complete new "
            "version of the anchored definition (def line included, "
            "at its indentation in the file — "
            f"{anchor_indent} spaces) with the {route} branch "
            "extended to report what the goal asks the endpoint to "
            "report, keeping the rest of the definition's code as "
            "it is; the system inserts it into the current module, "
            "so do not write the whole module. The content is code "
            "to extend, not text to copy back unchanged — a "
            "definition returned unchanged changes nothing, and "
            "the plan's own tests then fail the endpoint's new "
            "behavior at execution."
            + fields_guidance
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
    "sed_ban": "sed -i",
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

# Generation ceiling for planner/replan calls (Ollama num_predict).
# Planner schema redesign (2026-11-19): the pairing run's planner
# spent ~260 s and ~21,850 tokens generating a single degenerate
# schema field before the stream died at the context wall — the
# failure the parse-time loop detector now rejects after the fact,
# but only the ceiling bounds the generation itself. Originally
# sized from the recorded evidence base: the largest healthy
# planner output on record was 14,300 chars (~3,600 tokens,
# model-A/B corpus), so 8192 left ~2.3x headroom over any
# legitimate plan while cutting a runaway at ~37% of the pairing
# output's length.
# Raised to 16384 (2026-11-23, exam re-arm #6): plans that embed
# full file contents inline outgrew 8192 once spec grounding put
# the completed CONTRACT.md in front of the planner — all three
# responses truncated mid-JSON at 31,760 / 31,763 / 33,519 bytes,
# inside the final node's contents, and no plan ever parsed
# (StuckRepairError). The truncated contents were substantively
# correct; the envelope legitimately needed more room. 16384
# leaves ~2x headroom over that near-complete ~33.5 KB envelope
# while still cutting a runaway at ~75% of the pairing output's
# length, and the repetition-loop detector plus the per-field
# prose caps remain the degeneration guards — the ceiling only
# bounds how long a bad generation runs before they judge it,
# and the client timeout bounds the wall-clock regardless. A
# caller that set num_predict (or FREETALON_LLM_NUM_PREDICT)
# keeps their value — the same rule as num_ctx above.
_PLANNER_NUM_PREDICT = 16384


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


def plan_envelope_schema(*, require_contract: bool = False) -> dict[str, Any]:
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

    ``status``, ``responds_to`` and ``differs_by`` are deliberately
    absent: all three are system-owned. Status is forced to DRAFT at
    materialization; responds_to / differs_by are written only by
    the repair path (the node-local repair in this module) as the
    system's own repair note, and the repeat checks read them as
    exactly that. The 2026-11-19 assessment measured the
    model-emitted versions at 93.6% of one run's entire planner
    output — one value degenerating into a 29,358-char repetition
    loop — with zero code consumers ever reading their content, so
    the model is no longer asked to produce them; parse strips them
    if they appear anyway (parse_planner_response).

    The optional top-level ``contract`` (the module contract: one
    entry per new module another file in the plan imports from) is
    the inverse case — the model IS asked to declare it, parse
    validates its shape and the plan's contents against it, and the
    validated form is pinned into the plan metadata under
    ``module_contract`` for the run's re-plans (see the module
    contract pinning section). It stays out of "required": most
    plans write no coupled new modules and need no contract.

    ``require_contract`` is the constrained re-ask variant (L1,
    2026-11-23): the same schema with ``contract`` promoted into
    "required" at minItems 1, each entry requiring ``module``
    plus a non-empty ``defines``. It exists for the single
    re-ask _call_llm_with_repair makes after an accepted plan
    couples new modules its declaration omitted: under the
    variant the decoder cannot emit the envelope without the
    array. It is never the default schema, so plans that couple
    nothing are not forced to invent hollow contract entries.

    The contract entry shape is defined once, below, and shared
    by both variants. Its ``defines`` items carry the
    bare-identifier pattern — ``_CONTRACT_NAME_RE.pattern``,
    referenced rather than copied, so the schema cannot drift
    from the pattern _normalize_contract enforces: contract
    names are bare identifiers end to end, and a dotted
    "Class.method" spelling is un-emittable under the
    constraint instead of merely rejected after the fact (exam
    re-arm #3's re-ask was discarded for exactly that spelling).
    ``module`` carries no pattern on purpose: _normalize_contract
    normalizes the spelling (whitespace, backslashes, a leading
    "./") before applying its own path check, so a schema
    pattern would forbid spellings the validator accepts.
    ``statuses`` items stay plain strings — the validator
    demands only non-empty strings there.
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
        "reversible": {"type": "boolean"},
        "timeout_s": {"type": "number"},
        "max_retries": {"type": "integer"},
        "hypothesis": {"type": "string"},
    }
    schema: dict[str, Any] = {
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
            "contract": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "module": {"type": "string"},
                        "defines": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "pattern": _CONTRACT_NAME_RE.pattern,
                            },
                        },
                        "statuses": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "metadata": {"type": "object"},
        },
        "required": ["nodes"],
    }
    if require_contract:
        schema["required"] = ["nodes", "contract"]
        contract_schema = schema["properties"]["contract"]
        contract_schema["minItems"] = 1
        contract_schema["items"]["required"] = ["module", "defines"]
        contract_schema["items"]["properties"]["defines"]["minItems"] = 1
    return schema


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
        format_mode="schema",
    )
    prompt = (
        "Re-emit the plan below as ONE JSON object with a \"nodes\" "
        "array. Keep every node's id, objective, depends_on, "
        "assigned_claw, inputs, outputs, acceptance and criteria, plus any "
        "top-level \"contract\" array, "
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


def _rejection_signature(
    response_text: str,
    records: Collection[PlanError],
) -> frozenset:
    """A rejection's stuck-detector identity: (code, node, payload).

    Campaign build #10: the signature keys on payload identity,
    not on the error code alone — the principle the judge's
    repeat-approach gate established (92448dc): a signature match
    escalates to a substance check, and only an identical payload
    convicts. Each record contributes its code, its node, and the
    whitespace-normalized payload that node carried in THIS
    response (memory.node_payload / normalize_payload — a write
    node's content, a bash node's command), read leniently from
    the raw output, since a rejected output may not validate.
    Same code on a materially different payload is iteration —
    the model is working the defect, however unsuccessfully —
    and must not accumulate toward the stuck threshold; the same
    code on the same payload is the repetition the detector
    exists to name. A record whose node's payload cannot be
    extracted (the node is absent from the output, its claw
    carries no payload, the output does not parse) contributes
    None: no payload comparison is possible, and the record keys
    on code + node exactly as before this build.
    """
    payloads: dict[str, str] = {}
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        payload = None
    nodes_raw = payload.get("nodes") if isinstance(payload, dict) else None
    if isinstance(nodes_raw, list):
        for position, raw in enumerate(nodes_raw, start=1):
            view = _lenient_node_view(raw, position)
            if view is None:
                continue
            text = node_payload(view)
            if text is not None:
                payloads[view.id] = normalize_payload(text)
    return frozenset(
        (
            record.code,
            record.node_id,
            payloads.get(record.node_id) if record.node_id else None,
        )
        for record in records
    )


def _stuck_repair_error(
    exc: LLMResponseError,
    signature: frozenset,
    count: int,
) -> StuckRepairError:
    """The escalation raised when a repair loop is stuck (steal #5)."""
    codes = sorted({code for code, _node_id, _payload in signature})
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


def _rejection_error_dicts(exc: BaseException) -> list[dict[str, Any]]:
    """A rejection exception as plain record dicts for the trail.

    Structured PlanError records keep their code / node / stage /
    message; an exception carrying no records (a backend failure,
    a formatter crash) becomes one record naming its type, so the
    recorded reason always says something concrete.
    """
    records = getattr(exc, "plan_errors", None) or []
    if records:
        return [
            {
                "code": record.code,
                "node_id": record.node_id,
                "stage": record.stage,
                "message": record.message,
            }
            for record in records
        ]
    return [
        {
            "code": type(exc).__name__,
            "node_id": None,
            "stage": None,
            "message": str(exc),
        }
    ]


def _contract_constrained_reask(
    system_prompt: str,
    user_prompt: str,
    settings: LLMSettings,
    *,
    omitted: list[str],
    goal_text: str | None,
    workspace: str | None,
    repo_root: str | Path | None,
    formatter: FormatterSettings | None,
    baseline_paths: set[str] | None,
    baseline_installs: bool,
    endorsed_paths: set[str],
    clauses: list[dict[str, Any]] | None = None,
    clause_mode: str = "full",
    clause_required: Collection[str] | None = None,
) -> tuple[tuple[list[PlanNode], dict[str, Any]] | None, dict[str, Any]]:
    """One contract-required re-ask (L1, 2026-11-23).

    The cross-model consultation's top mechanism, in the shape
    this stack can enforce: the same planning prompt plus a
    declaration demand, under the envelope schema variant that
    makes "contract" a required key (minItems 1), so Ollama's
    constrained decoding cannot produce the envelope without
    the array. The prompt names the modules the accepted plan
    coupled without declaring — system-computed facts about the
    plan's own coupling, not the model's rejected text (the
    repair loop's no-echo rule stands: the failed output itself
    is never re-shown).

    The re-asked plan is adopted ONLY when it parses clean,
    declares every coupled module itself (no derivation fill),
    and passes the same scope lock the accepted plan passed.
    Anything else returns None and the accepted plan — its
    contract completed by derivation — stands. Constraint
    guarantees presence, not quality: the declaration is judged
    by the same validators as any other, and a hollow or
    incoherent one fails parse like any other plan.

    Returns (adopted, record): ``adopted`` is the (nodes,
    metadata) pair when the re-asked plan qualifies, else None.
    ``record`` is the outcome either way — {"outcome":
    "adopted"} or {"outcome": "rejected", "gate": <the gate
    that stopped it>, ...} carrying the concrete failing detail
    (the parse rejection's structured errors, the still-omitted
    module list, or the scope lock's records). The caller writes
    the record into the accepted plan's metadata under
    CONTRACT_REASK_METADATA_KEY, which the plan state store
    persists. Before the record existed, every rejection path
    below returned a bare None and exam re-arm #3's non-adoption
    was recoverable only by replaying the raw output against a
    reconstructed tree.
    """
    module_list = ", ".join(omitted)
    reask_prompt = (
        f"{user_prompt}\n\n"
        "CONTRACT DECLARATION REQUIRED: a previous attempt at "
        "this plan coupled NEW Python modules without declaring "
        f"them ({module_list}). Your plan object MUST include a "
        "top-level \"contract\" array, placed BEFORE \"nodes\": "
        "one entry per NEW module another file in the plan "
        "imports from, each with \"module\", \"defines\" (every "
        "public name other files rely on, as BARE names only: a "
        "method another file calls is listed by its bare method "
        "name — \"register\", never \"PairingStore.register\") "
        "and \"statuses\" (the exact status strings the modules "
        "use). Declare the contract for the modules "
        "YOUR plan couples, then make every file match it "
        "exactly.\n\n"
        "JSON Output:"
    )
    try:
        reask_settings = dataclass_replace(
            settings,
            format_schema=plan_envelope_schema(require_contract=True),
            format_mode="schema",
        )
        reask_text = call_llm(
            system_prompt, reask_prompt, settings=reask_settings
        )
        if formatter is not None:
            reask_text = reformat_plan_output(
                reask_text, settings, formatter
            )
    except Exception as exc:
        return None, {
            "outcome": "rejected",
            "gate": "call",
            "errors": _rejection_error_dicts(exc),
        }
    try:
        new_nodes, new_metadata = parse_planner_response(
            reask_text,
            goal_text=goal_text,
            workspace=workspace,
            repo_root=repo_root,
            pinned_contract=None,
            clauses=clauses,
            clause_mode=clause_mode,
            clause_required=clause_required,
        )
    except Exception as exc:
        return None, {
            "outcome": "rejected",
            "gate": "parse",
            "errors": _rejection_error_dicts(exc),
        }
    still_omitted = _contract_omitted_modules(
        new_nodes, reask_text, repo_root
    )
    if still_omitted:
        return None, {
            "outcome": "rejected",
            "gate": "omitted_modules",
            "modules": still_omitted,
        }
    if baseline_paths is not None:
        scope_error = _scope_widening_error(
            new_nodes,
            baseline_paths,
            baseline_installs,
            endorsed_paths,
            repo_root,
            workspace=workspace,
        )
        if scope_error is not None:
            return None, {
                "outcome": "rejected",
                "gate": "scope_lock",
                "errors": _rejection_error_dicts(scope_error),
            }
    return (new_nodes, new_metadata), {"outcome": "adopted"}


# ── Plan-attempt anchoring (campaign build #4) ─────────────────────
#
# Exam arm #13 (2026-11-23) isolated the repair loop's failure mode:
# with the clause layer live, plan_task_intent spent its three
# attempts fixing each rejection by regressing a different property.
# Attempt 1 scaffolded with 0/12 clause mappings; attempt 2 added a
# NEW test file but wrote no implementation and still mapped 0/12;
# attempt 3 carried the full implementation AND 12/12 mappings — and
# scaffolded the existing tests/test_pairing.py, dropping 61 of its
# 65 definitions, so the preservation guard rejected it and the
# budget was gone. The repair prompt named only the CURRENT
# rejection; nothing carried forward what earlier attempts had
# already gotten right, so no single attempt ever held every
# validated property at once. Arm #12 showed the same shape with
# two scaffold errors; #13 narrowed it to one. The blocker is
# convergence, not clause compliance.
#
# The anchoring below is the deterministic counterpart: after every
# rejected attempt the loop records simple plan-shape facts about
# the attempt (its validator error codes; how many frozen clauses
# its own declarations implemented / verified; which files it wrote,
# split into implementation writes, NEW test files, and writes to
# EXISTING test files; which existing files it targeted with a
# full-content scaffold). The next repair prompt carries the facts
# for every prior attempt plus DO-NOT-REGRESS constraints computed
# comparatively — a property some earlier attempt satisfied and the
# latest attempt lost — and the standing instruction that the next
# plan must satisfy all validators simultaneously. Everything is
# computed from the rejected output and the tree on disk, never
# from a model's account; extraction is fail-soft, because a facts
# failure must never mask the rejection it describes. On success
# the history is stamped into the plan metadata under
# ATTEMPT_ANCHORING_METADATA_KEY (system-owned trail, like
# rejected_approaches), so an exam arm can show what the briefs
# carried.

ATTEMPT_ANCHORING_METADATA_KEY = "attempt_anchoring"

# File lists inside one attempt's fact line / one constraint are
# capped so a sprawling plan cannot flood the repair prompt; the
# counts always ride along, so the cap hides names, never facts.
_ANCHOR_FACT_FILE_CAP = 4


def _fact_file_list(paths: list[str]) -> str:
    """A bounded rendering of a fact's file list ("a, b, +2 more")."""
    if not paths:
        return "none"
    shown = ", ".join(paths[:_ANCHOR_FACT_FILE_CAP])
    if len(paths) > _ANCHOR_FACT_FILE_CAP:
        shown += f", +{len(paths) - _ANCHOR_FACT_FILE_CAP} more"
    return shown


def _plan_attempt_facts(
    response_text: str,
    error_records: Collection[PlanError],
    repo_root: str | Path | None,
    clauses: list[dict[str, Any]] | None,
    clause_required: Collection[str] | None = None,
) -> dict[str, Any]:
    """Deterministic shape facts about one rejected plan attempt.

    Read leniently from the raw output (the _response_write_scope
    pattern — the output may be too malformed for PlanNode
    validation; that is why it was rejected): the error codes come
    from the rejection's structured records; the clause counts from
    the nodes' own inputs.clauses declarations, counting only IDs
    in the frozen list (an invented ID is not coverage); the write
    split from _node_written_paths, classified by _is_test_file_path
    and by existence on disk; a full-content scaffold is a scaffold /
    file_writer node carrying "content" with neither "anchor" nor
    "edits" (the two edit forms are modifications, not scaffolds).
    Never raises: any extraction failure returns the error codes
    alone, so the repair loop's bookkeeping cannot mask the
    rejection it describes.

    Campaign build #7: when the call is a re-plan (``clause_required``
    given — the brief's open-clause set), the facts also carry the
    delta measures the delta rule judges: how many of the required
    clauses the attempt implemented / verified, and how many of its
    nodes declared a clause mapping at all (nodes_declared of
    nodes_total). Without them the anchoring could only speak in
    full-coverage terms a delta is not required to meet.
    """
    facts: dict[str, Any] = {
        "errors": sorted({record.code for record in error_records}),
        "clauses_implemented": None,
        "clauses_verified": None,
        "clauses_total": 0,
        "implementation_writes": [],
        "test_writes_new": [],
        "test_writes_existing": [],
        "scaffolded_existing": [],
    }
    try:
        payload = parse_json_object(sanitize_llm_output(response_text))
    except Exception:
        return facts
    nodes_raw = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes_raw, list):
        return facts
    root = resolve_repo_root(repo_root)
    known_clauses: set[str] | None = None
    implemented: set[str] = set()
    verified: set[str] = set()
    if clauses:
        known_clauses = {str(clause.get("id")) for clause in clauses}
        facts["clauses_total"] = len(known_clauses)
    # Campaign build #7: delta-attempt tracking (re-plan calls).
    required_ids: set[str] = set()
    if clause_required:
        required_ids = {str(clause_id) for clause_id in clause_required}
    nodes_total = 0
    nodes_declared = 0
    required_implemented: set[str] = set()
    required_verified: set[str] = set()
    implementation_writes: set[str] = set()
    test_new: set[str] = set()
    test_existing: set[str] = set()
    scaffolded_existing: set[str] = set()
    for position, raw in enumerate(nodes_raw, start=1):
        view = _lenient_node_view(raw, position)
        if view is None:
            continue
        nodes_total += 1
        if clause_required is not None and (
            view.inputs or {}
        ).get("clauses") is not None:
            nodes_declared += 1
        if known_clauses is not None:
            declares_impl, declares_verify, decl_error = (
                node_clause_declaration(view)
            )
            if decl_error is None:
                implemented.update(
                    clause_id
                    for clause_id in declares_impl
                    if clause_id in known_clauses
                )
                verified.update(
                    clause_id
                    for clause_id in declares_verify
                    if clause_id in known_clauses
                )
                required_implemented.update(
                    clause_id
                    for clause_id in declares_impl
                    if clause_id in required_ids
                )
                required_verified.update(
                    clause_id
                    for clause_id in declares_verify
                    if clause_id in required_ids
                )
        for path in _node_written_paths(view):
            if _is_test_file_path(path):
                if _path_exists_on_disk(path, repo_root):
                    test_existing.add(path)
                else:
                    test_new.add(path)
            else:
                implementation_writes.add(path)
        if (view.assigned_claw or "") in ("scaffold", "file_writer"):
            inputs = view.inputs or {}
            if (
                isinstance(inputs.get("content"), str)
                and "anchor" not in inputs
                and "edits" not in inputs
            ):
                target = (
                    inputs.get("file_path")
                    or inputs.get("path")
                    or (view.outputs[0] if view.outputs else "")
                )
                if (
                    isinstance(target, str)
                    and target
                    and _resolve_plan_target(target, root).is_file()
                ):
                    scaffolded_existing.add(target)
    if known_clauses is not None:
        facts["clauses_implemented"] = len(implemented)
        facts["clauses_verified"] = len(verified)
    if clause_required is not None:
        facts["required_total"] = (
            len(required_ids & known_clauses)
            if known_clauses is not None
            else 0
        )
        facts["required_implemented"] = len(required_implemented)
        facts["required_verified"] = len(required_verified)
        facts["nodes_total"] = nodes_total
        facts["nodes_declared"] = nodes_declared
    facts["implementation_writes"] = sorted(implementation_writes)
    facts["test_writes_new"] = sorted(test_new)
    facts["test_writes_existing"] = sorted(test_existing)
    facts["scaffolded_existing"] = sorted(scaffolded_existing)
    return facts


def _attempt_anchoring_constraints(
    attempts: list[dict[str, Any]],
) -> list[str]:
    """DO-NOT-REGRESS constraints: properties an earlier attempt
    satisfied and the latest attempt lost.

    Comparative by construction — each constraint names the attempt
    that established the property, the latest attempt's regression,
    and the move that restores it. A monotonically improving history
    yields no constraints: the anchoring never invents a regression
    to warn about. Error-code novelty is a constraint too: a code
    the latest attempt produced that NO earlier attempt produced is
    the signature of fixing one rejection by creating another.
    """
    if len(attempts) < 2:
        return []
    earlier, latest = attempts[:-1], attempts[-1]
    constraints: list[str] = []
    total = latest.get("clauses_total") or 0
    for key, label in (
        ("clauses_implemented", "implementation"),
        ("clauses_verified", "verification"),
    ):
        best = max(
            (attempt.get(key) for attempt in earlier),
            key=lambda value: -1 if value is None else value,
        )
        current = latest.get(key)
        if best is None or current is None or current >= best:
            continue
        peak = next(
            index + 1
            for index, attempt in enumerate(earlier)
            if attempt.get(key) == best
        )
        constraints.append(
            f"Clause {label} coverage peaked at {best}/{total} in "
            f"attempt {peak}; the latest attempt declared "
            f"{current}/{total}. Keep every clause declaration the "
            f"earlier attempts had and add the missing ones — the "
            f"next plan must declare at least {best}/{total} "
            f"{label} coverage."
        )
    # Campaign build #7: the delta measures, for re-plan calls
    # whose attempts carry them. The full-coverage constraints
    # above judge a delta by a rule it is not under; these judge
    # it by its own: coverage of the brief's open clauses, and
    # every node declaring a mapping at all.
    if latest.get("required_total") is not None:
        required_total = latest.get("required_total") or 0
        for key, label in (
            ("required_implemented", "implementation"),
            ("required_verified", "verification"),
        ):
            best = max(
                (attempt.get(key) for attempt in earlier),
                key=lambda value: -1 if value is None else value,
            )
            current = latest.get(key)
            if best is None or current is None or current >= best:
                continue
            peak = next(
                index + 1
                for index, attempt in enumerate(earlier)
                if attempt.get(key) == best
            )
            constraints.append(
                f"Open-clause {label} coverage peaked at "
                f"{best}/{required_total} in attempt {peak}; the "
                f"latest attempt declared {current}/"
                f"{required_total}. The clauses the brief names "
                f"as open are this re-plan's assignment — the next "
                f"plan must declare at least {best}/"
                f"{required_total} {label} coverage of them."
            )
        full_declaration_seen = any(
            attempt.get("nodes_total")
            and attempt.get("nodes_declared") == attempt.get("nodes_total")
            for attempt in earlier
        )
        latest_declared = latest.get("nodes_declared")
        latest_nodes = latest.get("nodes_total") or 0
        if (
            full_declaration_seen
            and latest_nodes > 0
            and (latest_declared or 0) < latest_nodes
        ):
            constraints.append(
                "An earlier attempt in this call declared a "
                "clause mapping on every node it emitted; the "
                f"latest attempt declared mappings on "
                f"{latest_declared} of its {latest_nodes} nodes. "
                "Every node in a re-plan must carry inputs.clauses "
                "— restore a declaration on every node."
            )
    if not latest.get("implementation_writes"):
        richest = max(
            earlier, key=lambda attempt: len(attempt["implementation_writes"])
        )
        if richest["implementation_writes"]:
            peak = earlier.index(richest) + 1
            constraints.append(
                f"Attempt {peak} included implementation writes "
                f"({_fact_file_list(richest['implementation_writes'])}); "
                "the latest attempt wrote no implementation files at "
                "all. A plan of tests and reads alone implements "
                "nothing — the next plan must include the "
                "implementation writes."
            )
    if latest.get("test_writes_existing") and not latest.get(
        "test_writes_new"
    ):
        for index, attempt in enumerate(earlier):
            if attempt.get("test_writes_new") and not attempt.get(
                "test_writes_existing"
            ):
                constraints.append(
                    f"Attempt {index + 1} put its tests in NEW "
                    f"file(s) ({_fact_file_list(attempt['test_writes_new'])}); "
                    "the latest attempt wrote to existing test "
                    f"file(s) ({_fact_file_list(latest['test_writes_existing'])}). "
                    "Do not scaffold or rewrite an existing test "
                    "file — put new tests in a new file, or extend "
                    "the existing module with an anchored insert."
                )
                break
    prior_codes: set[str] = set()
    for attempt in earlier:
        prior_codes.update(attempt.get("errors") or [])
    new_codes = sorted(set(latest.get("errors") or []) - prior_codes)
    if new_codes:
        constraints.append(
            "The latest attempt introduced rejection(s) no earlier "
            f"attempt in this call had: {', '.join(new_codes)}. "
            "Fixing one rejection must not create another — the "
            "next plan must avoid these as well as the rejection "
            "above."
        )
    return constraints


def _attempt_anchoring_block(attempts: list[dict[str, Any]]) -> str:
    """The PLAN-ATTEMPT ANCHORING block for a repair prompt.

    "" until a second attempt exists (one attempt has nothing to
    regress against — the rejection itself is the whole signal).
    With two or more: one measured line per prior attempt, the
    DO-NOT-REGRESS constraints (when any fired), and the standing
    instruction that the next plan must satisfy all validators at
    once.
    """
    if len(attempts) < 2:
        return ""
    lines = [
        "\nPLAN-ATTEMPT ANCHORING (system — what your earlier "
        "attempts in this planning call got right, measured from "
        "their outputs and the tree on disk, not from memory):"
    ]
    for index, attempt in enumerate(attempts, start=1):
        errors = ", ".join(attempt.get("errors") or []) or "unstructured"
        line = f"- Attempt {index}: rejected with {errors}."
        if attempt.get("clauses_total"):
            line += (
                f" Clause declarations: implements "
                f"{attempt.get('clauses_implemented')}/"
                f"{attempt['clauses_total']}, verifies "
                f"{attempt.get('clauses_verified')}/"
                f"{attempt['clauses_total']}."
            )
        if attempt.get("required_total") is not None:
            # Campaign build #7: the delta measures, for re-plan
            # attempts — coverage of the brief's open clauses and
            # the declared-node count the delta rule judges.
            line += (
                f" Open-clause declarations: implements "
                f"{attempt.get('required_implemented')}/"
                f"{attempt['required_total']}, verifies "
                f"{attempt.get('required_verified')}/"
                f"{attempt['required_total']}; nodes declaring a "
                f"clause mapping: {attempt.get('nodes_declared')}/"
                f"{attempt.get('nodes_total')}."
            )
        line += (
            f" Implementation writes: "
            f"{_fact_file_list(attempt.get('implementation_writes') or [])}."
            f" Test writes: new "
            f"{_fact_file_list(attempt.get('test_writes_new') or [])}, "
            f"existing "
            f"{_fact_file_list(attempt.get('test_writes_existing') or [])}."
            f" Existing files scaffolded whole: "
            f"{_fact_file_list(attempt.get('scaffolded_existing') or [])}."
        )
        lines.append(line)
    constraints = _attempt_anchoring_constraints(attempts)
    if constraints:
        lines.append("DO NOT REGRESS:")
        lines.extend(f"- {constraint}" for constraint in constraints)
    lines.append(
        "The next plan must satisfy ALL the validators at once: "
        "fix the rejection above AND keep every property listed "
        "here. Trading one rejection for another spends the "
        "remaining budget without progress."
    )
    return "\n" + "\n".join(lines) + "\n"


def _existing_files_section(
    specs: list[Any], repo_root: str | Path | None
) -> str:
    """The EXISTING FILES prompt section for a planning call.

    Campaign build #4: the deterministic inventory of the tree the
    plan will write into — the goal-named specs' directories (the
    package the goal is about), the package root's modules, and the
    tests tree — each file with its definition count, under the
    standing rule that existing files are never scaffolded. The
    repair prompts embed the original prompt, so the section rides
    into every repair round without a second channel. "" when the
    tree holds none of the inventoried scopes.
    """
    root = resolve_repo_root(repo_root)
    focus_dirs = sorted(
        {
            Path(spec.path).parent.as_posix()
            for spec in specs
            if getattr(spec, "path", None)
            and Path(spec.path).parent.as_posix() not in ("", ".")
        }
    )
    block = format_existing_file_inventory(
        existing_file_inventory(root, focus_dirs)
    )
    return f"\n\n{block}" if block else ""


# ── Campaign build #9: planner attempt persistence ──────────────────
#
# Arm #17's fill payloads were never persisted (build #8's
# diagnosis had to reproduce the run byte-identically to see
# them), and arm #18's terminal re-plan failure — "LLM response
# was not valid JSON" — could not be attributed to an attempt:
# the re-plan path persisted no per-attempt outputs at all. Parse
# prints every plan response to stdout (the driver-log pattern
# the corpora were built from), but unlabeled: no phase, no
# attempt number, no outcome, and fill payloads never printed.
#
# Every planner attempt now records one labeled record — phase,
# attempt number within the phase, validation outcome, the error
# codes / validator errors on a rejection, and the raw response
# text (post-formatter for plan phases, post-sanitize for fill:
# the text validation actually judged) — to two destinations:
#
# - stdout, as a labeled block in the same spirit as parse's
#   RAW PLANNER OUTPUT print, so the driver-visible log carries
#   attributed attempts for every planning call, in or out of a
#   run; and
# - the attempt sink, when one is installed: a callable taking
#   the record dict. LoopRunner.run_plan installs a sink bound
#   to its run store for the run's duration, so a run's attempts
#   land in <run_dir>/planner_attempts.jsonl next to the
#   measurements and memory records; plan_task_intent and
#   replan_from_brief also accept an attempt_sink argument for
#   direct capture outside a run.
#
# Persistence is observability, never a gate: a sink failure is
# swallowed (noted on stdout), never raised into planning. The
# volume is bounded by the attempt budgets themselves (plan-sized
# texts, a handful per planning call); the recorded error text is
# capped for the same reason.
#
# Campaign build #10: the sink's installation ordering left the
# INITIAL planning phases (plan_skeleton, content_fill) out of
# the run's jsonl — the driver flow plans first and only then
# calls run_plan, which is what installs the sink (arm #19's
# jsonl held the 3 replan_skeleton records and none of the 8
# initial-planning attempts; those stood in the driver log's
# stdout blocks only). Records from the initial phases made
# while NO sink is installed now wait in a bounded pending
# buffer, and LoopRunner.run_plan drains the buffer into its
# run sink on entry (drain_pending_planner_attempts), so a
# loop-driven run's trail covers the planning that produced
# its plan, ahead of the run's own re-plan records, in the
# order the attempts were made. Only the initial phases
# buffer: re-plan attempts are made inside a run (a sink is
# installed) or by a direct caller with its own capture —
# buffering them would misattribute one caller's attempts to
# whatever run happened to start next. Direct callers without
# a sink keep the stdout record exactly as before.

_ATTEMPT_ERROR_CAP = 4000

#: The installed attempt sink, if any (see planner_attempt_sink).
_ATTEMPT_SINK: Callable[[dict[str, Any]], None] | None = None

#: The phases whose records buffer when no sink is installed —
#: the initial planning phases (campaign build #10, above).
_INITIAL_PLANNING_PHASES = frozenset({"plan_skeleton", "content_fill"})

#: Initial-planning records awaiting a run's sink, oldest first.
#: Bounded like everything else in this section: a planning
#: call's attempts are a handful of plan-sized texts, and the
#: cap holds the most recent ones when no run ever claims them.
_PENDING_ATTEMPTS: list[dict[str, Any]] = []
_PENDING_ATTEMPT_LIMIT = 128


@contextmanager
def planner_attempt_sink(
    sink: Callable[[dict[str, Any]], None] | None,
):
    """Install ``sink`` as the planner attempt sink for a block.

    Records go to the innermost installed sink; nesting restores
    the outer sink on exit. Passing None installs a blackout (the
    stdout record still prints). LoopRunner.run_plan is the
    production installer; tests and drivers may install their own.
    """
    global _ATTEMPT_SINK
    previous = _ATTEMPT_SINK
    _ATTEMPT_SINK = sink
    try:
        yield
    finally:
        _ATTEMPT_SINK = previous


def drain_pending_planner_attempts(
    sink: Callable[[dict[str, Any]], None],
) -> int:
    """Hand the buffered initial-planning records to ``sink``.

    Campaign build #10: LoopRunner.run_plan calls this with its
    run sink on entry — the initial planning that produced the
    run's plan happened before that sink existed (the driver
    flow plans first, then runs), and its records waited in the
    pending buffer (see the section above). Returns the number
    of records delivered. The buffer is cleared either way: the
    records were offered to the run that claimed them, and a
    later run must not inherit them. A sink failure on one
    record is noted on stdout and the drain continues —
    persistence is observability, never a gate.
    """
    global _PENDING_ATTEMPTS
    pending = _PENDING_ATTEMPTS
    _PENDING_ATTEMPTS = []
    delivered = 0
    for record in pending:
        try:
            sink(record)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                "planner attempt drain failed "
                f"({type(exc).__name__}: {exc}); the attempt record "
                "stands in the log only."
            )
    return delivered


def _record_planner_attempt(
    *,
    phase: str,
    attempt: int,
    outcome: str,
    response_text: str,
    error_codes: Collection[str] = (),
    errors: Collection[str] = (),
    node_id: str | None = None,
    target: str | None = None,
    intent: str | None = None,
    intent_corrections: Collection[dict[str, Any]] = (),
) -> None:
    """Persist one planner attempt (see the section above).

    ``outcome`` is "accepted" or "rejected". The record's error
    text is the rejection's own rendering, capped at
    _ATTEMPT_ERROR_CAP characters — the raw response is NOT
    capped: it is the artifact the record exists to preserve.

    Campaign build #11: ``intent_corrections`` carries the
    skeleton-intent corrections parse applied to this attempt
    (accepted or rejected alike); the key is present only when
    a correction actually happened, so a record from an
    uncorrected attempt is byte-identical to the pre-build-#11
    shape.
    """
    error_text = "\n".join(str(error) for error in errors)
    if len(error_text) > _ATTEMPT_ERROR_CAP:
        error_text = (
            error_text[:_ATTEMPT_ERROR_CAP]
            + "\n... [error text capped]"
        )
    record: dict[str, Any] = {
        "phase": phase,
        "attempt": attempt,
        "outcome": outcome,
        "error_codes": sorted(str(code) for code in error_codes),
        "errors": error_text,
        "node_id": node_id,
        "target": target,
        "intent": intent,
        "response_text": response_text,
    }
    if intent_corrections:
        record["intent_corrections"] = [
            dict(entry) for entry in intent_corrections
        ]
    header = (
        f"phase={phase} attempt={attempt} outcome={outcome}"
        + (f" node={node_id}" if node_id else "")
        + (f" target={target}" if target else "")
        + (f" intent={intent}" if intent else "")
        + (
            f" error_codes={','.join(record['error_codes'])}"
            if record["error_codes"]
            else ""
        )
    )
    print(
        f"\n--- PLANNER ATTEMPT [{header}] ---\n"
        f"{response_text}\n"
        "--- END PLANNER ATTEMPT ---\n"
    )
    if _ATTEMPT_SINK is not None:
        try:
            _ATTEMPT_SINK(record)
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                "planner attempt sink failed "
                f"({type(exc).__name__}: {exc}); the attempt record "
                "above stands in the log only."
            )
    elif phase in _INITIAL_PLANNING_PHASES:
        # Campaign build #10: no sink is installed — in the
        # driver flow this is the initial planning, which runs
        # before LoopRunner.run_plan installs the run's sink.
        # Hold the record for that sink (see the section above);
        # the stdout block above already stands either way.
        _PENDING_ATTEMPTS.append(record)
        del _PENDING_ATTEMPTS[:-_PENDING_ATTEMPT_LIMIT]


# ── Seam attempt persistence (campaign build #13) ─────────────────
#
# The repair seams (cycle 8's local repair, the scoped-edit seam)
# make model calls too, and until this build those calls were the
# last unrecorded response class in the loop. Exam arm #22 is the
# cost: build #12's suspect-map wiring worked — the scoped-edit
# seam reached its model call in all three cycles with the
# suspect file and the probe's flow diagnostics — and declined
# 3/3 with the new reason no_edit_blocks, "the model's response
# carries no edit blocks". What the model actually returned (a
# prose explanation? a whole-file rewrite? a JSON envelope?) was
# recorded NOWHERE — not the driver log, not the run store — so
# the decline was a verdict without evidence, the same failure
# that cost arm #17 (fill payloads, answered by build #8's
# capture) and arm #18 (re-plan JSON, answered by build #9's
# planner persistence). The seams now get the build #9 treatment:
#
# - every seam model call records one labeled record — seam name,
#   attempt number within the invocation, the response's
#   disposition (accepted = in the seam's required format and
#   judged as a candidate; rejected = the response itself was
#   unusable, with the decline reason it produced), cycle context
#   (plan id, the brief's plan version, the target), and the FULL
#   raw response text — to the same two channels: stdout, as a
#   labeled block, and the attempt sink when one is installed.
#   LoopRunner.run_plan's sink routes seam records (they carry
#   kind="seam_attempt") to the run's seam_attempts.jsonl, so
#   planner_attempts.jsonl keeps its build #9 shape exactly.
# - seam records never enter the pending buffer: a seam only runs
#   inside a run (a sink is installed) or under a direct caller,
#   whose capture is the stdout block. Buffering them would
#   misattribute one caller's seam calls to whatever run started
#   next — the build #10 reasoning, applied unchanged.
# - persistence is observability, never a gate: a sink failure is
#   swallowed (noted on stdout), never raised into the seam.


def _record_seam_attempt(
    *,
    seam: str,
    attempt: int,
    outcome: str,
    response_text: str,
    decline_reason: str = "",
    shape_repair: bool = False,
    plan_id: str | None = None,
    plan_version: Any = None,
    target: str | None = None,
) -> None:
    """Persist one repair-seam model call (see the section above).

    ``outcome`` is the RESPONSE's disposition: "accepted" when it
    arrived in the seam's required format (edit blocks parsed /
    the definition extracted) and went on to candidate judgment,
    "rejected" when the response itself was unusable — with
    ``decline_reason`` naming the decline it produced. Candidate-
    level declines after an accepted response (an edit set that
    fails to apply, a splice that changes nothing) ride the
    build #2 decline records, as before; this record exists so
    the raw response behind ANY seam outcome is reconstructable.
    """
    record: dict[str, Any] = {
        "kind": "seam_attempt",
        "seam": seam,
        "attempt": attempt,
        "outcome": outcome,
        "decline_reason": decline_reason,
        "shape_repair": shape_repair,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "target": target,
        "response_text": response_text,
    }
    header = (
        f"seam={seam} attempt={attempt} outcome={outcome}"
        + (f" reason={decline_reason}" if decline_reason else "")
        + (" shape_repair=true" if shape_repair else "")
        + (f" target={target}" if target else "")
    )
    print(
        f"\n--- SEAM ATTEMPT [{header}] ---\n"
        f"{response_text}\n"
        "--- END SEAM ATTEMPT ---\n"
    )
    if _ATTEMPT_SINK is not None:
        try:
            _ATTEMPT_SINK(record)
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                "seam attempt sink failed "
                f"({type(exc).__name__}: {exc}); the attempt record "
                "above stands in the log only."
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
    pinned_contract: list[dict[str, Any]] | None = None,
    clauses: list[dict[str, Any]] | None = None,
    formatter: FormatterSettings | None = None,
    skeleton: bool = False,
    clause_mode: str = "full",
    clause_required: Collection[str] | None = None,
    attempt_phase: str = "plan",
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

    ``pinned_contract`` is the module contract an earlier accepted
    plan version pinned (module contract pinning): it is rendered
    into every repair prompt as binding and passed to parse, which
    validates each candidate against it and rejects a candidate
    that declares a different contract.

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

    L1 (2026-11-23): when an accepted plan couples NEW modules
    its own "contract" declaration omitted — the omission the
    definitive exam measured at zero emissions across every
    candidate on two models — one extra call re-asks for the
    plan under the contract-required schema variant, so the
    decoder cannot emit the envelope without the array. The
    re-asked plan replaces the accepted one only if it parses
    clean, declares every coupled module itself, and passes
    the same scope lock; otherwise the accepted plan stands,
    its contract completed by derivation
    (_contract_constrained_reask). Either outcome is recorded
    in the returned metadata under CONTRACT_REASK_METADATA_KEY
    — a non-adoption names its gate and the concrete failing
    detail, never a bare None. The re-ask fires at most
    once per planning call, only when no contract pin governs,
    and only under the planner-owned schema — a caller-supplied
    schema is respected, never replaced.

    Campaign build #4: every rejected attempt's plan-shape facts
    are recorded (_plan_attempt_facts) and, from the second
    repair on, the repair prompt carries the PLAN-ATTEMPT
    ANCHORING block — the prior attempts' measured facts plus
    DO-NOT-REGRESS constraints for properties an earlier attempt
    satisfied and the latest lost (exam arm #13: each attempt
    fixed the named rejection and regressed a different property,
    so no attempt ever held every validated property at once).
    On success the history rides the returned metadata under
    ATTEMPT_ANCHORING_METADATA_KEY.

    Campaign build #7: ``clause_mode`` / ``clause_required`` are
    threaded to parse (the delta clause rule for re-plan calls)
    and to the attempt facts, so a re-plan's anchoring measures
    the delta properties — open-clause coverage and declared-node
    counts — alongside the full-list counts. The repair budget
    semantics are unchanged: clause rejections are not cheap and
    spend repairs like any other real rejection.

    Campaign build #9: every attempt in this cycle is persisted
    via _record_planner_attempt under ``attempt_phase`` (the
    caller's phase name — "plan_skeleton" for the initial
    planner, "replan_skeleton" for a re-plan), with its outcome
    and, on a rejection, the structured error codes. Recording
    changes no decision this function makes.

    Campaign build #11: at the skeleton phases, parse corrects
    content intents that contradict the tree before judging the
    attempt (arm #20's five-attempt death on a deterministic
    fact); the corrections ride the attempt records kept here —
    from the accepted plan's metadata, or from the rejection
    itself — and every skeleton repair prompt carries the
    SKELETON TARGET FACTS block: the rejected skeleton's write
    targets with their measured existence and legal intents.
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
    # Planner schema redesign (2026-11-19): the generation ceiling,
    # same rule as num_ctx — a caller that already set num_predict
    # keeps their value. The fuse exists because an unbounded
    # generation is the one planner failure parse-time checks
    # cannot bound: they judge the output only after it exists.
    if effective_settings.num_predict is None:
        effective_settings = dataclass_replace(
            effective_settings, num_predict=_PLANNER_NUM_PREDICT
        )
    # Cycle 2 (steal #1): the plan envelope is grammar-constrained at
    # decode time on the Ollama path, so the envelope's shape is
    # guaranteed by the decoder instead of repaired after the fact.
    # A caller-supplied schema is respected, never overridden — the
    # same rule as num_ctx above.
    planner_schema_owned = False
    if (
        effective_settings.format_schema is None
        and effective_settings.format_mode is None
        and effective_settings.backend == "ollama"
    ):
        # Build #14: an explicit format_mode is the caller's stated
        # expectation and is never overridden here — the same rule
        # as a caller-supplied schema above.
        effective_settings = dataclass_replace(
            effective_settings,
            format_schema=plan_envelope_schema(),
            format_mode="schema",
        )
        planner_schema_owned = True
    # Campaign build #11: the skeleton-phase flag, captured before
    # the loop — the empty-plan branch inside it rebinds the local
    # name ``skeleton`` to the grounded-skeleton TEXT (the
    # long-standing shadowing), so the phase is read here.
    is_skeleton_phase = skeleton
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
    contract_reask_done = False
    # Campaign build #4: per-attempt plan-shape facts for the
    # anchoring block (see the section above this function). One
    # entry per rejected attempt, in order; the block is rebuilt
    # from the whole history each round, so a property established
    # by ANY earlier attempt anchors every later repair.
    attempt_history: list[dict[str, Any]] = []
    attempt_no = 0
    while True:
        endorsed_paths |= _prompt_named_paths(prompt)
        response_text = call_llm(
            system_prompt, prompt, settings=effective_settings
        )
        attempt_no += 1
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
                pinned_contract=pinned_contract,
                clauses=clauses,
                skeleton=skeleton,
                clause_mode=clause_mode,
                clause_required=clause_required,
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
            reask_record: dict[str, Any] | None = None
            if (
                not contract_reask_done
                and pinned_contract is None
                and planner_schema_owned
                and not skeleton
            ):
                # Campaign build #5: the re-ask derives omitted
                # contract entries from node CONTENTS (the plan's
                # coupling); a skeleton has none, so the re-ask
                # cannot fire at this phase. Contract derivation
                # and pinning run on the ASSEMBLED plan instead
                # (plan_task_intent's assembly parse), where the
                # contents exist.
                contract_reask_done = True
                omitted = _contract_omitted_modules(
                    nodes, response_text, repo_root
                )
                if omitted:
                    upgraded, reask_record = _contract_constrained_reask(
                        system_prompt,
                        prompt,
                        effective_settings,
                        omitted=omitted,
                        goal_text=goal_text,
                        workspace=workspace,
                        repo_root=repo_root,
                        formatter=formatter,
                        baseline_paths=baseline_paths,
                        baseline_installs=baseline_installs,
                        endorsed_paths=endorsed_paths,
                        clauses=clauses,
                        clause_mode=clause_mode,
                        clause_required=clause_required,
                    )
                    if upgraded is not None:
                        nodes, metadata = upgraded
            metadata = dict(metadata)
            # The re-ask record is system-owned trail, set from
            # this call's own outcome like rejected_approaches
            # below — never carried in from the payload: a call
            # in which the re-ask did not fire clears any value
            # a model-supplied metadata carried.
            if reask_record is not None:
                metadata[CONTRACT_REASK_METADATA_KEY] = reask_record
            else:
                metadata.pop(CONTRACT_REASK_METADATA_KEY, None)
            metadata["rejected_approaches"] = list(rejected_approaches)
            # Campaign build #4: the anchoring trail is system-owned
            # like rejected_approaches — stamped from this call's own
            # attempt history when any repair happened, cleared
            # otherwise (a value a model payload carried under the
            # key never survives, and a first-attempt acceptance
            # carries no anchoring record at all).
            metadata.pop(ATTEMPT_ANCHORING_METADATA_KEY, None)
            if attempt_history:
                metadata[ATTEMPT_ANCHORING_METADATA_KEY] = {
                    "attempts": attempt_history,
                    "do_not_regress": _attempt_anchoring_constraints(
                        attempt_history
                    ),
                }
            # Campaign build #9: the accepted attempt is part of
            # the same record as the rejected ones — the phase's
            # full attempt sequence is recoverable, not only its
            # failures.
            _record_planner_attempt(
                phase=attempt_phase,
                attempt=attempt_no,
                outcome="accepted",
                response_text=response_text,
                intent_corrections=metadata.get(
                    INTENT_CORRECTIONS_METADATA_KEY
                )
                or (),
            )
            return nodes, metadata
        except LLMResponseError as exc:
            last_error = exc
            # Steering keys off the rejection's structured codes
            # (A/B refactor) — never off substrings of its message.
            error_codes = {
                record.code
                for record in getattr(exc, "plan_errors", None) or []
            }
            # Campaign build #9: record the rejected attempt
            # before any budget decision, so an attempt that ends
            # the call (budget exhausted, stuck detection) is
            # persisted exactly like one that earns a repair.
            _record_planner_attempt(
                phase=attempt_phase,
                attempt=attempt_no,
                outcome="rejected",
                response_text=response_text,
                error_codes=error_codes,
                errors=[str(exc)],
                intent_corrections=getattr(
                    exc, "intent_corrections", None
                )
                or (),
            )
            # Campaign build #4: record this attempt's plan-shape
            # facts BEFORE the budget decision, so the anchoring
            # block built below describes every attempt the model
            # has made in this call, including the one just rejected.
            attempt_history.append(
                _plan_attempt_facts(
                    response_text,
                    getattr(exc, "plan_errors", None) or [],
                    repo_root,
                    clauses,
                    clause_required,
                )
            )
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
            #
            # Campaign build #10: the signature carries payload
            # identity (see _rejection_signature). Arm #19's
            # re-plan died here on three 'command' rejections of
            # step_5 whose commands were materially different
            # attempts (a multi-line inline script, then two
            # semicolon one-liners): same code, different payload
            # is iteration, and the repair budget — not the stuck
            # detector — is what ends it.
            if not isinstance(exc, EmptyPlanError):
                signature = _rejection_signature(
                    response_text,
                    getattr(exc, "plan_errors", None) or [],
                ) or frozenset({(type(exc).__name__, None, None)})
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
            # Campaign build #11: the skeleton target-facts block
            # — deterministic existence facts for the rejected
            # skeleton's write targets (exists on disk / created
            # earlier in this plan / absent), the legal intents
            # for each, and the corrections this attempt already
            # received. Skeleton phases only; a non-skeleton
            # repair brief is byte-unchanged.
            existence_block = (
                _skeleton_target_facts_block(
                    response_text,
                    repo_root,
                    getattr(exc, "intent_corrections", None) or (),
                )
                if is_skeleton_phase
                else ""
            )
            # Campaign build #4: the anchoring block — prior
            # attempts' measured facts + DO-NOT-REGRESS constraints.
            # Empty until a second attempt exists.
            anchoring_block = _attempt_anchoring_block(attempt_history)
            # Cycle 2 (steal #5 re-grounding): the repair prompt
            # re-injects the original task verbatim, directly over
            # the rejection — the full original prompt rides along
            # above, but a repair that is drifting (a new stack, a
            # new file set) needs the unchanged goal stated where
            # the failure is named, not only at the top of the call.
            task_block = (
                f"THE TASK IS UNCHANGED: {goal_text}\n" if goal_text else ""
            )
            contract_block = (
                _render_contract_block(pinned_contract, pinned=True) + "\n"
                if pinned_contract
                else ""
            )
            prompt = (
                f"{user_prompt}\n\n"
                "Your previous output was REJECTED. Do not repeat it.\n"
                f"{task_block}"
                f"{contract_block}"
                f"Rejection reason: {exc}\n"
                f"Previous output: {_describe_failed_output(response_text)}\n"
                f"{_dropped_fields_note(prev_response_text, response_text)}"
                f"{forbidden_block}"
                f"{steering_block}"
                f"{extension_block}"
                f"{materialization_block}"
                f"{existence_block}"
                f"{anchoring_block}"
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


# ── Campaign build #5: content fill (phase 2) + assembly ─────────────

_CONTENT_FILL_SYSTEM_PROMPT = (
    "You fill ONE write node of an already-accepted plan skeleton. "
    "The skeleton is fixed: do not propose other nodes, do not "
    "restate the plan, and do not explain. Return ONLY a JSON "
    "object carrying this node's payload, in the exact form the "
    "prompt names."
)

#: Attempts per write node in the fill phase: the initial call plus
#: two bounded repairs with the validator errors in the prompt.
_CONTENT_FILL_MAX_ATTEMPTS = 3


def _content_fill_settings(
    settings: LLMSettings | None,
) -> LLMSettings:
    """Effective settings for a fill call (planner window/fuse).

    Same derivation as _call_llm_with_repair (a caller-set num_ctx /
    num_predict is respected), except the format directive: the plan
    envelope schema constrains the SKELETON phase only — a fill
    response is one node's payload object, not a plan envelope, so
    any envelope schema is dropped here and the mode is stated
    explicitly as "json" (build #14): the payload IS parsed as a
    JSON object downstream, so the bare JSON directive the
    transport used to substitute for a dropped schema is now the
    call's declared expectation, not an accident of None.
    """
    if settings is None:
        effective = LLMSettings.from_env()
    else:
        effective = settings
    if effective.num_ctx is None:
        effective = dataclass_replace(effective, num_ctx=_PLANNER_NUM_CTX)
    if effective.num_predict is None:
        effective = dataclass_replace(
            effective, num_predict=_PLANNER_NUM_PREDICT
        )
    effective = dataclass_replace(
        effective, format_schema=None, format_mode="json"
    )
    return effective


def _node_clause_obligations(
    node: PlanNode, clauses: list[dict[str, Any]] | None
) -> str:
    """The node's own clause obligations, with clause text."""
    if not clauses:
        return ""
    implements, verifies, _error = node_clause_declaration(node)
    wanted = {"implements": implements, "verifies": verifies}
    if not wanted["implements"] and not wanted["verifies"]:
        return ""
    by_id = {str(clause.get("id")): clause for clause in clauses}
    lines = ["This node's clause obligations (from the frozen contract):"]
    for kind in ("implements", "verifies"):
        for clause_id in wanted[kind]:
            clause = by_id.get(clause_id) or {}
            text = clause.get("text") or clause.get("heading") or ""
            lines.append(f"  {kind} {clause_id}: {text}".rstrip())
    return "\n".join(lines)


def _filled_candidate_errors(
    candidate: dict[str, Any],
    prior_dicts: list[dict[str, Any]],
    repo_root: str | Path | None,
    goal_text: str | None,
) -> list[str]:
    """Per-node validators over one filled candidate node.

    The candidate (with every earlier filled node, so an edit
    against a file an earlier node creates materializes against
    that content) passes through the ordinary materialization seam
    — scoped/anchored edit application plus the build #3 edit
    grounding check — and then the per-node content validators
    (scaffold preservation, duplicate_code grounding, parse,
    placeholder, elision, stub-only, ...; _CONTENT_FILL_CODES).
    A whole-file/create payload for an existing Python file also
    faces the edit grounding check against the file's prior text:
    a rewrite may not introduce references the file cannot resolve
    any more than an edit may. Returns error strings; empty = the
    payload is valid.
    """
    import copy as _copy

    dicts = [_copy.deepcopy(item) for item in prior_dicts]
    candidate_copy = _copy.deepcopy(candidate)
    dicts.append(candidate_copy)
    try:
        _materialize_plan_edits(dicts, repo_root)
    except LLMResponseError as exc:
        return [str(exc)]
    try:
        node = PlanNode.model_validate(candidate_copy)
    except ValidationError as exc:
        return [f"filled node failed schema validation: {exc}"]
    records, _cheap = _collect_plan_error_records(
        [node],
        repo_root,
        goal_text,
        None,
        plan_context="enrichment",
    )
    errors = [
        record.message
        for record in records
        if record.code in _CONTENT_FILL_CODES
    ]
    inputs = candidate_copy.get("inputs") or {}
    content = inputs.get("content")
    target = _node_write_target(node)
    if (
        isinstance(content, str)
        and target is not None
        and target.endswith(".py")
        and inputs.get("_materialized_from")
        not in ("anchored_edit", "scoped_edits")
    ):
        prior_source: str | None = None
        for prior in prior_dicts:
            prior_inputs = prior.get("inputs") or {}
            if _node_write_target(
                PlanNode.model_validate(prior)
            ) == target and isinstance(prior_inputs.get("content"), str):
                prior_source = prior_inputs["content"]
        if prior_source is None:
            try:
                prior_source = _resolve_plan_target(
                    target, resolve_repo_root(repo_root)
                ).read_text(encoding="utf-8")
            except OSError:
                prior_source = None
        if prior_source is not None:
            violations = edit_grounding_violations(prior_source, content)
            if violations:
                errors.append(
                    "the filled content introduces ungrounded "
                    "references: "
                    + "; ".join(violations)
                    + " — reference only attributes the class assigns "
                    "and names the module defines or imports."
                )
    return errors


# ── Campaign build #8: schema-explicit repair for fill shape failures ──
#
# Arm #17 died in content fill, and its fill payloads were never
# persisted — the diagnosis here rests on a byte-identical
# reproduction run during this build (same greedy settings, same
# worktree state, every raw response captured): every edit fill
# opened with {} — the model's "no change needed" answer — and for
# __init__.py, whose current content already satisfied the node's
# objective, all three attempts were that same claim in different
# dialects ({}, then a whole-file no-op edit rejected by content
# validation as no_op_edit, then {"edits": []}). The repair prompt
# only ever repeated the bare validator sentence; it never spelled
# the schema. So: a payload that fails on SHAPE (not a JSON object,
# an empty object, an empty edits list, an anchor without content,
# a missing/empty/wrong-typed content) earns a schema-explicit
# repair — the previous payload's defect named in plain words, the
# exact required schema for the node's intent, and a minimal worked
# example of the envelope. Content-validation failures keep the
# plain errors block, and first attempts are unchanged. Envelope
# recovery was considered and NOT built: the observed payloads
# carried no usable content under any alternate envelope ({} and
# {"edits": []} are empty by construction), so there was nothing
# to recover.


def _json_type_name(value: Any) -> str:
    """The JSON type name of a parsed value, for defect sentences."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return "a number"


def _fill_payload_shape(
    payload: dict[str, Any], intent: str
) -> tuple[dict[str, Any], list[str]]:
    """Split one fill payload into (candidate input fields, defects).

    The shape half of fill validation: is the payload one of the
    envelopes the node's intent accepts? Returns the inputs the
    payload contributes when the shape is usable, else a single
    defect sentence naming — in plain words — what the payload
    actually was, for the schema-explicit repair block. Content
    validation (materialization, scaffold preservation, grounding,
    ...) judges the fields afterwards, exactly as before.
    """
    if intent in ("create", "whole_file"):
        if "content" not in payload:
            if not payload:
                return {}, [
                    "your previous payload was an empty JSON object "
                    '({}) — it carried no "content" at all'
                ]
            keys = ", ".join(sorted(str(key) for key in payload))
            return {}, [
                'your previous payload carried no "content" string '
                f"(keys present: {keys}) — the complete file "
                'content must ride in "content"'
            ]
        content = payload["content"]
        if not isinstance(content, str):
            return {}, [
                'your previous payload\'s "content" was '
                f"{_json_type_name(content)}, not a string"
            ]
        if not content:
            return {}, [
                'your previous payload\'s "content" was an empty '
                "string — it must carry the complete file content"
            ]
        return {"content": content}, []
    edits = payload.get("edits")
    if isinstance(edits, list) and edits:
        return {"edits": edits}, []
    anchor = payload.get("anchor")
    if isinstance(anchor, str) and anchor:
        snippet = payload.get("content")
        if isinstance(snippet, str):
            return {
                "anchor": anchor,
                "position": payload.get("position", "after"),
                "content": snippet,
            }, []
        if "content" not in payload:
            return {}, [
                'your previous payload carried an "anchor" but no '
                '"content" — an anchored edit must also carry the '
                'new code in "content"'
            ]
        return {}, [
            'your previous payload\'s "content" was '
            f"{_json_type_name(snippet)}, not a string — an "
            "anchored edit must carry the new code as a string "
            'in "content"'
        ]
    if not payload:
        return {}, [
            "your previous payload was an empty JSON object "
            '({}) — it carried neither an "edits" list nor '
            'an "anchor"'
        ]
    if "edits" in payload:
        if isinstance(edits, list):
            return {}, [
                'your previous payload carried an "edits" list, '
                "but it was empty — an empty list makes no change "
                "and is not a valid payload"
            ]
        return {}, [
            'your previous payload\'s "edits" was '
            f"{_json_type_name(edits)}, not a list of edits"
        ]
    if "anchor" in payload:
        if isinstance(anchor, str):
            return {}, [
                'your previous payload\'s "anchor" was an empty '
                "string — it must be a line copied exactly from "
                "the current file"
            ]
        return {}, [
            'your previous payload\'s "anchor" was '
            f"{_json_type_name(anchor)}, not a string"
        ]
    keys = ", ".join(sorted(str(key) for key in payload))
    return {}, [
        "your previous payload carried none of the required keys "
        f"(keys present: {keys}) — it had neither a non-empty "
        '"edits" list nor an "anchor" with "content"'
    ]


def _fill_shape_repair_block(intent: str, defects: list[str]) -> str:
    """The schema-explicit repair block after a shape failure.

    Names the previous payload's defect, spells the exact required
    schema for the node's intent, and shows a minimal worked
    example of the envelope — the three things the pre-build-#8
    repair prompt (the bare validator sentence, repeated) never
    gave the model.
    """
    lines = [
        "Your previous payload for this node was REJECTED — it "
        "did not match the required payload shape:",
        *(f"  - {defect}" for defect in defects),
        "",
    ]
    if intent in ("create", "whole_file"):
        lines += [
            f"The required payload shape for this node "
            f"(content_intent: {intent}) is EXACTLY this JSON "
            "object — nothing else is a valid payload:",
            '  {"content": "<the complete file content>"}',
            '  Worked example: {"content": "VALUE = 1\\n"}',
            "",
            'An empty object ({}), a missing "content", and an '
            'empty "content" string are NOT valid answers: '
            '"content" must carry the whole file, every '
            "definition, no elisions, no placeholders.",
            "Return a corrected payload only.",
        ]
    else:
        lines += [
            "The required payload shape for this node "
            "(content_intent: edit) is EXACTLY ONE of these two "
            "JSON objects — nothing else is a valid payload:",
            '  1. Scoped edits: {"edits": [{"search": "<text '
            'copied exactly from the current file>", "replace": '
            '"<the new text>"}]}',
            '     Worked example: {"edits": [{"search": '
            '"import os\\n", "replace": "import os\\nimport '
            'sys\\n"}]}',
            '  2. Anchored edit: {"anchor": "<a line copied '
            'exactly from the current file>", "position": '
            '"after" or "before", "content": "<the new code>"}',
            '     Worked example: {"anchor": "import os", '
            '"position": "after", "content": "import sys"}',
            "",
            'An empty object ({}) and an empty "edits" list are '
            'NOT "no change" answers — this contract has no '
            "no-change payload: the payload must carry at least "
            "one real edit or one anchored insertion. Do NOT "
            "return the whole file.",
            "Return a corrected payload only.",
        ]
    return "\n".join(lines)


def _fill_node_payload(
    node: PlanNode,
    intent: str,
    *,
    settings: LLMSettings,
    skeleton_json: str,
    obligations: str,
    spec_section: str,
    inventory_section: str,
    existing_source: str | None,
    prior_dicts: list[dict[str, Any]],
    repo_root: str | Path | None,
    goal_text: str | None,
    attempt_phase: str = "content_fill",
) -> tuple[dict[str, Any], int]:
    """Fill one skeleton write node via its own LLM call(s).

    Returns (candidate node dict in unmaterialized form, attempts
    used). Bounded repair: up to _CONTENT_FILL_MAX_ATTEMPTS calls,
    each retry carrying the validator errors of the last payload.
    A node that still fails raises LLMResponseError naming the
    node, its target, its intent, and the last errors — for a
    shape failure those errors are the final payload's defect
    sentences (campaign build #8) — the plan fails with that
    precise error; nothing is silently accepted.

    Campaign build #8: a retry after a SHAPE failure carries the
    schema-explicit repair block (_fill_shape_repair_block) in
    place of the plain errors block; a retry after a
    content-validation failure keeps the plain block unchanged.

    Campaign build #9: every attempt is persisted via
    _record_planner_attempt under ``attempt_phase`` (the caller's
    fill-phase name — "content_fill" for the initial planner,
    "replan_content_fill" for a re-plan), with the node, target,
    and intent attached. The recorded text is the sanitized
    response — the payload validation actually judged. Arm #17's
    unpersisted fill payloads are the failure this removes.
    """
    target = _node_write_target(node) or ""
    if intent in ("create", "whole_file"):
        form = (
            'Return {"content": "<the complete file content>"} — '
            "the whole file, every definition, no elisions, no "
            "placeholders."
        )
    else:
        form = (
            'Return EITHER {"edits": [{"search": "<text copied '
            'exactly from the current file>", "replace": "<the new '
            'text>"}]} (scoped edits) OR {"anchor": "<a line copied '
            'exactly from the current file>", "position": "after" '
            'or "before", "content": "<the new code>"} (an anchored '
            "edit). Do NOT return the whole file."
        )
    existing_block = ""
    if existing_source is not None:
        existing_block = (
            f"\n\nCurrent content of {target} (the file as it "
            f"stands before this node):\n{existing_source}"
        )
    errors_block = ""
    last_errors: list[str] = []
    last_defects: list[str] = []
    for attempt in range(1, _CONTENT_FILL_MAX_ATTEMPTS + 1):
        prompt = (
            f"CONTENT FILL — node '{node.id}' writes {target} "
            f"(content_intent: {intent}).\n\n"
            f"Goal: {goal_text or ''}\n\n"
            f"Accepted plan skeleton (compact):\n{skeleton_json}\n\n"
            f"This node's objective: {node.objective}\n"
            + (f"{obligations}\n" if obligations else "")
            + f"{spec_section}"
            + f"{inventory_section}"
            + f"{existing_block}"
            + f"{errors_block}\n\n"
            f"{form}\n"
            "JSON Output:"
        )
        response_text = call_llm(
            _CONTENT_FILL_SYSTEM_PROMPT, prompt, settings=settings
        )
        response_text = sanitize_llm_output(response_text)
        candidate = node.model_dump(mode="json")
        candidate_inputs = dict(candidate.get("inputs") or {})
        candidate_inputs.pop("content_intent", None)
        try:
            payload = parse_json_object(response_text)
        except Exception as exc:  # noqa: BLE001 — named to the repair
            payload = None
            last_defects = [
                f"your previous payload was not a JSON object: {exc}"
            ]
        fields: dict[str, Any] = {}
        if payload is not None:
            fields, last_defects = _fill_payload_shape(payload, intent)
        candidate_inputs.update(fields)
        candidate["inputs"] = candidate_inputs
        if last_defects:
            last_errors = last_defects
        else:
            last_errors = _filled_candidate_errors(
                candidate, prior_dicts, repo_root, goal_text
            )
        if not last_errors:
            _record_planner_attempt(
                phase=attempt_phase,
                attempt=attempt,
                outcome="accepted",
                response_text=response_text,
                node_id=node.id,
                target=target,
                intent=intent,
            )
            return candidate, attempt
        _record_planner_attempt(
            phase=attempt_phase,
            attempt=attempt,
            outcome="rejected",
            response_text=response_text,
            errors=last_errors,
            node_id=node.id,
            target=target,
            intent=intent,
        )
        if last_defects:
            errors_block = "\n\n" + _fill_shape_repair_block(
                intent, last_defects
            )
        else:
            errors_block = (
                "\n\nYour previous payload for this node was REJECTED:\n"
                + "\n".join(f"  - {error}" for error in last_errors)
                + "\nReturn a corrected payload only."
            )
    raise LLMResponseError(
        f"Content fill for planner node '{node.id}' (target "
        f"{target!r}, intent {intent!r}) failed after "
        f"{_CONTENT_FILL_MAX_ATTEMPTS} attempts. Last errors: "
        + "; ".join(last_errors)
    )


def _fill_plan_contents(
    nodes: list[PlanNode],
    *,
    settings: LLMSettings | None,
    goal_text: str | None,
    repo_root: str | Path | None,
    clauses: list[dict[str, Any]] | None,
    spec_section: str,
    inventory_section: str,
    attempt_phase: str = "content_fill",
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]] | None:
    """Phase 2: fill every content-less write node of a skeleton.

    Returns None when no node needs filling (the plan was emitted
    pre-filled, the legacy single-shot shape — assembly is then
    unnecessary and the skeleton nodes stand as accepted). Otherwise
    returns (filled raw node dicts for assembly, per-node content
    attempt counts, pre-filled node ids). Nodes fill in skeleton
    (topological) order, so an edit node sees the filled content of
    every earlier write to its target.

    Campaign build #9: this helper is the ONE fill implementation
    for both planning calls — plan_task_intent (initial plans) and
    replan_from_brief (repair deltas) — parameterized only by
    ``attempt_phase``, the persistence label its per-node attempts
    are recorded under. There is no second fill path for the
    re-plan to drift from.
    """
    pending = [
        node
        for node in nodes
        if (node.assigned_claw or "") in ("scaffold", "file_writer")
        and not isinstance((node.inputs or {}).get("content"), str)
    ]
    if not pending:
        return None
    effective = _content_fill_settings(settings)
    skeleton_json = json.dumps(
        [
            {
                "id": node.id,
                "objective": node.objective,
                "depends_on": list(node.depends_on or []),
                "assigned_claw": node.assigned_claw,
                "inputs": {
                    key: value
                    for key, value in (node.inputs or {}).items()
                    if key != "content"
                },
            }
            for node in nodes
        ]
    )
    filled_dicts: list[dict[str, Any]] = []
    attempts: dict[str, int] = {}
    prefilled: list[str] = []
    root = resolve_repo_root(repo_root)
    for node in nodes:
        raw = node.model_dump(mode="json")
        if (node.assigned_claw or "") not in ("scaffold", "file_writer"):
            filled_dicts.append(raw)
            continue
        inputs = node.inputs or {}
        if isinstance(inputs.get("content"), str):
            raw_inputs = dict(raw.get("inputs") or {})
            raw_inputs.pop("content_intent", None)
            raw["inputs"] = raw_inputs
            prefilled.append(node.id)
            filled_dicts.append(raw)
            continue
        intent = str(inputs.get("content_intent"))
        target = _node_write_target(node) or ""
        existing_source: str | None = None
        if intent in ("edit", "whole_file"):
            for prior in filled_dicts:
                prior_inputs = prior.get("inputs") or {}
                prior_target = prior_inputs.get(
                    "file_path"
                ) or prior_inputs.get("path")
                if prior_target == target and isinstance(
                    prior_inputs.get("content"), str
                ):
                    existing_source = prior_inputs["content"]
            if existing_source is None:
                try:
                    existing_source = _resolve_plan_target(
                        target, root
                    ).read_text(encoding="utf-8")
                except OSError:
                    existing_source = None
        candidate, used = _fill_node_payload(
            node,
            intent,
            settings=effective,
            skeleton_json=skeleton_json,
            obligations=_node_clause_obligations(node, clauses),
            spec_section=spec_section,
            inventory_section=inventory_section,
            existing_source=existing_source,
            prior_dicts=filled_dicts,
            repo_root=repo_root,
            goal_text=goal_text,
            attempt_phase=attempt_phase,
        )
        attempts[node.id] = used
        filled_dicts.append(candidate)
    return filled_dicts, attempts, prefilled


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


# Campaign build #4: plan_task_intent's repair budget, as a named
# constant. Total attempts = 1 initial + this many repairs. Raised
# from 2 repairs (3 attempts) to 4 (5 attempts) — deliberately
# paired with plan-attempt anchoring (above): arm #13's three
# attempts each fixed the named rejection and regressed a different
# property, so the anchoring is the fix and this budget is the room
# it needs to converge. Arm #8's lesson stands for EXECUTION
# re-plans (budget alone was not binding there), so
# replan_from_brief's budget is unchanged; this raise is scoped to
# initial planning, where the measured exhaustion happened.
PLAN_TASK_INTENT_MAX_REPAIRS = 4


def plan_task_intent(
    intent: TaskIntent,
    settings: LLMSettings | None = None,
    *,
    plan_id: str | None = None,
    max_repairs: int = PLAN_TASK_INTENT_MAX_REPAIRS,
    repo_root: str | Path | None = None,
    attempt_sink: Callable[[dict[str, Any]], None] | None = None,
) -> ExecutionPlan:
    """Generate a validated execution plan DAG for a TaskIntent.

    Rejected output (bad JSON, unknown claw, unmeasurable node) is sent
    back to the model for correction, up to ``max_repairs`` times. The
    prompt carries the repo context (fix 1) so the model plans against
    the code that actually exists. When the goal names an existing route,
    the prompt also carries the grounded ROUTE GOAL shape, so the first
    attempt already has the valid move in front of it instead of having
    to discover it by trial and error.

    Campaign build #9: ``attempt_sink``, when given, is installed
    as the planner attempt sink for this call (see
    planner_attempt_sink) — every skeleton and content-fill
    attempt is recorded to it as well as to stdout. A sink already
    installed by an outer scope (a run's) is restored on exit.
    """
    if attempt_sink is None:
        return _plan_task_intent(
            intent,
            settings,
            plan_id=plan_id,
            max_repairs=max_repairs,
            repo_root=repo_root,
        )
    with planner_attempt_sink(attempt_sink):
        return _plan_task_intent(
            intent,
            settings,
            plan_id=plan_id,
            max_repairs=max_repairs,
            repo_root=repo_root,
        )


def _plan_task_intent(
    intent: TaskIntent,
    settings: LLMSettings | None = None,
    *,
    plan_id: str | None = None,
    max_repairs: int = PLAN_TASK_INTENT_MAX_REPAIRS,
    repo_root: str | Path | None = None,
) -> ExecutionPlan:
    """plan_task_intent's body (build #9 split it out so the
    public entry can scope the attempt sink)."""
    workspace = (intent.constraints or {}).get("workspace")
    index = build_repo_index(repo_root)
    context = format_repo_context(index, workspace=workspace)
    # Spec grounding (2026-11-23): a spec file the goal names is
    # loaded verbatim into the prompt (goal_named_specs) — the
    # goal's prose alone first carried a restated summary the loop
    # implemented instead of the spec (exam re-arm #3), then,
    # scrubbed, nothing at all (re-arm #4). An oversized or
    # unreadable named spec raises here, before any call: planning
    # fails loudly rather than running ungrounded.
    specs = goal_named_specs(
        intent.goal, resolve_repo_root(repo_root), index
    )
    spec_section = ""
    if specs:
        spec_section = f"\n\n{format_spec_context(specs)}"
    # Contract clauses (campaign build #3): the same specs compile
    # deterministically into the frozen clause list — the prompt
    # carries the IDs the plan must cover, parse validates the
    # coverage declarations against the list, and the accepted
    # plan's metadata carries the list itself (system-owned, below)
    # for the loop's probes, briefs, and verdict gate.
    clauses = clauses_for_specs(specs)
    clauses_section = ""
    if clauses:
        clauses_section = f"\n\n{format_clauses_block(clauses)}"
    # Existing-file inventory (campaign build #4): the tree's
    # existing files with their definition counts, under the
    # standing rule that existing files are never scaffolded. Arm
    # #13's final attempt scaffolded a 47-test leftover module it
    # was never told existed; the preservation guard caught the
    # loss, but only by spending the last repair.
    inventory_section = _existing_files_section(specs, repo_root)

    shape = _route_goal_shape(intent.goal, repo_root, delta=False)
    shape_block = f"\n\n{shape}" if shape else ""
    # Retry #22: the shape sat at ~70% of the initial prompt (offset
    # 2845 of 4093 chars), ahead of the instruction paragraph. The
    # shape is the decision the model has to make, so it goes last —
    # immediately before "JSON Output:" — instead of ahead of the
    # instruction. Only ~13 chars ("\n\nJSON Output:") follow it.
    user_prompt = (
        f"{context}{spec_section}{clauses_section}{inventory_section}\n\n"
        f"{_format_intent_for_prompt(intent)}\n\n"
        "Produce a NEW execution plan DAG for the goal above. Your output "
        "must be a JSON object with a \"nodes\" array — do NOT repeat the "
        "goal text or any JSON from this prompt.\n"
        "\nSPLIT EMISSION (campaign build #5): emit the plan SKELETON "
        "only. Do NOT include any file \"content\", \"edits\", or "
        "\"anchor\" in any node — file payloads are requested "
        "separately, one node at a time, after the skeleton is "
        "accepted. For each scaffold/file_writer node, declare "
        "inputs \"content_intent\": \"create\" for a NEW file (filled "
        "with its full content), \"edit\" for an EXISTING file "
        "(filled with scoped/anchored edits), or \"whole_file\" for "
        "an existing file that must be rewritten whole (which "
        "requires the overwrite_justification + impact-check pair "
        "the modify rules demand). Keep each node's file_path, "
        "depends_on, criteria, and inputs.clauses declarations "
        "exactly as a full plan would carry them. Bash/command "
        "nodes keep their command in the skeleton as usual.\n"
        f"{shape_block}\n\n"
        "JSON Output:"
    )
    nodes, metadata = _call_llm_with_repair(
        planner_system_prompt(),
        user_prompt,
        settings,
        max_repairs=max_repairs,
        goal_text=intent.goal,
        workspace=workspace,
        repo_root=repo_root,
        clauses=clauses,
        formatter=formatter_settings_from_env(),
        skeleton=True,
        attempt_phase="plan_skeleton",
    )
    # Campaign build #5, phase 2 + assembly: fill each content-less
    # write node with its own call, then run the filled plan
    # through the ordinary full parse — the assembled plan is
    # indistinguishable in shape from a single-shot plan, and the
    # contract derivation/pinning the skeleton phase could not
    # judge (it reads node contents) completes there. A plan the
    # model emitted pre-filled needs neither step.
    filled = _fill_plan_contents(
        nodes,
        settings=settings,
        goal_text=intent.goal,
        repo_root=repo_root,
        clauses=clauses,
        spec_section=spec_section,
        inventory_section=inventory_section,
        attempt_phase="content_fill",
    )
    if filled is not None:
        filled_dicts, fill_attempts, prefilled_ids = filled
        assembled_nodes, assembled_metadata = parse_planner_response(
            json.dumps({"nodes": filled_dicts, "metadata": {}}),
            goal_text=intent.goal,
            workspace=workspace,
            repo_root=repo_root,
            clauses=clauses,
        )
        nodes = assembled_nodes
        metadata = dict(metadata or {})
        if assembled_metadata.get(CONTRACT_METADATA_KEY) is not None:
            metadata[CONTRACT_METADATA_KEY] = assembled_metadata[
                CONTRACT_METADATA_KEY
            ]
        metadata[PLAN_EMISSION_METADATA_KEY] = {
            "phases": ["skeleton", "content_fill"],
            "content_attempts": fill_attempts,
            "prefilled": prefilled_ids,
        }
    # Cycle 4: record the repo this plan was grounded against in its
    # metadata, so the executor resolves the plan's relative
    # references (./.venv/bin/python, scaffold targets, criterion
    # paths) against the same base the validators above judged them
    # by. Plan metadata is the established channel for planning facts
    # execution needs (cf. rejected_approaches, seeded the same way);
    # the loop's re-plan rebuild copies the metadata dict whole, so
    # the stamp survives into delta plans. This records a fact only —
    # no validation or steering behavior changes.
    metadata = dict(metadata or {})
    metadata.setdefault("repo_root", str(resolve_repo_root(repo_root)))
    # The spec record is system-owned trail, stamped from the
    # loader's result like the contract_reask record in
    # _call_llm_with_repair: cleared first, so a value a model
    # payload carried under the key never survives.
    metadata.pop(SPEC_CONTEXT_METADATA_KEY, None)
    if specs:
        metadata[SPEC_CONTEXT_METADATA_KEY] = [
            spec.record() for spec in specs
        ]
    # The frozen clause list is system-owned trail like the spec
    # record: cleared first (a model payload value under the key
    # never survives), then set from this call's own extraction.
    metadata.pop(CLAUSES_METADATA_KEY, None)
    if clauses:
        metadata[CLAUSES_METADATA_KEY] = clauses
    return build_execution_plan(intent, nodes, metadata, plan_id=plan_id)


# ── Re-plan diagnostics: suspect map + precedence (2026-11-23) ─────
#
# Exam re-arm #8: with a failure named only as a node-level
# criterion miss, the planner attributed every failing test to the
# module it had most recently rewritten — three re-plans rewrote
# store.py while the defects sat in server.py (produced in v1,
# never revised) and in a produced test that contradicted the
# contract. The block built here is the mechanical counterpart to
# the judge's stagnation record: for each failing test the system
# locates the test's own source in the file on disk and names the
# produced modules that file exercises, then states the precedence
# rule — a produced test is a derived artifact; the spec (or,
# without one, the goal) outranks it. Everything is computed from
# the brief's system-stamped fields and the files on disk, never
# from model output. The suspect map is returned alongside the
# block so the caller can stamp it back into the brief (the
# rejected_approaches write-back channel), where the judge's
# stagnation gate reads it for its repair note.

_SUSPECT_TEST_CAP = 10
_SUSPECT_METHOD_LINES = 40
_SUSPECT_METHOD_CHARS = 1600
_SUSPECT_SEARCH_FILE_CAP = 400
_SUSPECT_SEARCH_SKIP_DIRS = frozenset(
    {".git", ".venv", "__pycache__", "node_modules", "models"}
)


def _root_relative(path_text: str, root: Path) -> str:
    """A path as a repo-relative posix string, when it is under root."""
    path = Path(path_text)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _is_test_file_name(path: Path) -> bool:
    name = path.name
    return name.endswith(".py") and (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in path.parts
    )


def _test_id_candidates(test_id: str, root: Path) -> list[tuple[Path, str]]:
    """(file, method) candidates for a failing test identifier.

    pytest nodeids name the file outright ("tests/test_x.py::C::test_m");
    unittest identifiers are dotted ("pkg.mod.Class.test_m") — the
    method is the last component and the module is a prefix of the
    rest, tried longest-first (the class name is not a module).
    Existence and content are the caller's check.
    """
    if "::" in test_id:
        file_part, _, rest = test_id.partition("::")
        method = rest.split("::")[-1]
        return [(root / file_part, method)] if method else []
    parts = test_id.split(".")
    if len(parts) < 2:
        return []
    method = parts[-1]
    candidates: list[tuple[Path, str]] = []
    for cut in range(len(parts) - 1, 0, -1):
        base = root.joinpath(*parts[:cut])
        candidates.append((base.with_suffix(".py"), method))
        candidates.append((base / "__init__.py", method))
    return candidates


def _test_method_source(path: Path, method: str) -> str | None:
    """The source lines of one test method in a file, bounded.

    AST-located (module level or inside a class), sliced by line
    numbers, then capped by lines and by characters like every
    other excerpt the brief carries. None when the file does not
    parse or defines no such method.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method
            and node.end_lineno is not None
        ):
            lines = source.splitlines()[node.lineno - 1 : node.end_lineno]
            text = "\n".join(lines[:_SUSPECT_METHOD_LINES])
            if len(text) > _SUSPECT_METHOD_CHARS:
                text = text[: _SUSPECT_METHOD_CHARS - 3] + "..."
            return text
    return None


def _locate_failing_test(
    test_id: str, root: Path
) -> tuple[Path, str] | None:
    """Locate a failing test's (file, method) on disk, or None.

    Direct candidates first (nodeid path / dotted module path); the
    fallback is a bounded search of the repo's test files for a
    definition of the method name, in sorted path order so the
    result is deterministic.
    """
    for candidate, method in _test_id_candidates(test_id, root):
        if candidate.is_file() and _test_method_source(candidate, method):
            return candidate, method
    method = test_id.replace("::", ".").split(".")[-1]
    if not method.startswith("test"):
        return None
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if any(part in _SUSPECT_SEARCH_SKIP_DIRS for part in path.parts):
            continue
        if not _is_test_file_name(path):
            continue
        scanned += 1
        if scanned > _SUSPECT_SEARCH_FILE_CAP:
            break
        if _test_method_source(path, method):
            return path, method
    return None


def _produced_module_form(rel_path: str) -> str:
    """The dotted module form of a produced .py path."""
    form = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    if form.endswith("/__init__"):
        form = form[: -len("/__init__")]
    return form.replace("/", ".")


def _test_file_exercised_modules(
    test_path: Path, root: Path, produced_files: Collection[str]
) -> list[str]:
    """Produced non-test modules a test file imports or names.

    Import statements are the primary evidence: absolute imports
    match by dotted prefix in either direction, and relative imports
    are resolved against the test file's own package. A produced
    module whose name appears as a word in the source also counts —
    plans name modules in strings (importlib, subprocess commands)
    as well as imports. Produced test files are never exercised
    modules: the suspect is the code under test, not another test.
    """
    try:
        source = test_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return []
    try:
        package_parts = list(test_path.parent.relative_to(root).parts)
    except ValueError:
        package_parts = []
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package_parts) - (node.level - 1)
                base = package_parts[:keep] if keep > 0 else []
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if module:
                imported.add(module)
                for alias in node.names:
                    imported.add(f"{module}.{alias.name}")
    exercised: list[str] = []
    for produced in produced_files:
        rel = _root_relative(str(produced), root)
        if not rel.endswith(".py") or _is_test_file_name(Path(rel)):
            continue
        if rel == _root_relative(str(test_path), root):
            continue
        form = _produced_module_form(rel)
        stem = Path(rel).stem
        # An import names the module itself, or something under it
        # ("from pkg.store import CodeConflict"). Importing a parent
        # package alone does not mark every produced submodule as
        # exercised — that access shows up as a name in the source,
        # which by_name below catches.
        by_import = any(
            name == form or name.startswith(form + ".") for name in imported
        )
        by_name = len(stem) >= 3 and (
            re.search(rf"\b{re.escape(stem)}\b", source) is not None
        )
        if by_import or by_name:
            exercised.append(rel)
    return sorted(exercised)


def _replan_diagnostics(
    brief: dict[str, Any],
    root: Path,
    *,
    has_spec: bool,
    spec_paths: Collection[str] = (),
) -> tuple[str, dict[str, dict[str, Any]]]:
    """The re-plan prompt's diagnostics block + the suspect map.

    ("", {}) when the brief names no failing tests — a re-plan
    whose failure was not a test run prompts exactly as before. The
    suspect map (per failing test: its file on disk, the produced
    modules it exercises) is returned for the caller to stamp back
    into the brief; the block renders the same facts plus the
    test's bounded source and the precedence rule.
    """
    failing = [str(test) for test in brief.get("failing_tests") or []]
    if not failing:
        return "", {}
    produced = brief.get("produced_files") or []
    suspect_map: dict[str, dict[str, Any]] = {}
    lines = [
        "Re-plan diagnostics (system — computed from this run's "
        "measurements and the files on disk, not from a model's "
        "account of them):"
    ]
    for test_id in failing[:_SUSPECT_TEST_CAP]:
        entry: dict[str, Any] = {"file": None, "modules": []}
        located = _locate_failing_test(test_id, root)
        lines.append(f"Failing test {test_id}:")
        if located is not None:
            path, method = located
            entry["file"] = _root_relative(str(path), root)
            entry["modules"] = _test_file_exercised_modules(
                path, root, produced
            )
            lines.append(f"  defined in {entry['file']}:")
            excerpt = _test_method_source(path, method)
            if excerpt:
                lines.extend(
                    f"    {line}" for line in excerpt.splitlines()
                )
            modules = entry["modules"]
            lines.append(
                "  exercises produced modules: "
                + (", ".join(modules) if modules else "(none identified)")
            )
        else:
            lines.append(
                "  (its source was not located in this repo — it may "
                "live outside the produced files)"
            )
        suspect_map[test_id] = entry
    if has_spec:
        names = ", ".join(spec_paths) or "the goal-named spec"
        lines.append(
            f"Precedence (system): {names} is the authoritative "
            "specification for this goal. Tests produced during this "
            "run are derived artifacts: where a produced test "
            "contradicts the spec, the test is wrong — fix the test, "
            "not the code. Where a failing test exercises a module, "
            "the module AND the test are both suspects."
        )
    else:
        lines.append(
            "Precedence (system): no external spec governs this run; "
            "the goal text governs. Tests produced during this run "
            "are derived artifacts and can themselves be wrong: where "
            "a produced test contradicts the goal, fix the test, not "
            "the code. Where a failing test exercises a module, the "
            "module AND the test are both suspects."
        )
    return "\n".join(lines), suspect_map


def _probe_repair_diagnostics(
    brief: dict[str, Any],
    *,
    has_spec: bool,
    spec_paths: Collection[str] = (),
) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    """Probe-derived diagnostics + suspect map for the seams (#12).

    The counterpart of _replan_diagnostics for a brief whose
    failure named no failing tests — the timeout class: the plan's
    own test node hung, so there are no test IDs to locate, and
    both repair seams declined in every arm-#21 cycle for exactly
    that absence. The loop's contract probes, though, recorded
    precise per-flow failures against the produced file they
    exercised. When those failures name exactly one file
    (contract_clauses.probe_suspect_map, over the loop-stamped
    brief["probe_suspect"] when present, else derived here from
    the brief's verbatim probe failures), the failed flows stand
    in for failing tests: each flow becomes one synthetic failing
    entry ("probe:<flow>") whose exercised module is the suspect
    file, and the block below is the diagnostics the seam's model
    reads. Returns ("", {}, []) when no single suspect exists —
    the seams then decline exactly as before this build.
    """
    suspect = brief.get("probe_suspect")
    if not isinstance(suspect, dict) or not suspect:
        suspect = probe_suspect_map(brief.get("contract_probe_failures") or [])
    if not suspect:
        return "", {}, []
    target = suspect.get("file")
    flows = suspect.get("flows") or []
    if not isinstance(target, str) or not target or not flows:
        return "", {}, []
    lines = [
        "Probe diagnostics (system — the loop's own contract "
        "probes exercised the produced file below against the "
        "contract and recorded these flow failures; computed from "
        "the run's probe records, not from a model's account of "
        "them):"
    ]
    failing: list[str] = []
    suspect_map: dict[str, dict[str, Any]] = {}
    for entry in flows:
        if not isinstance(entry, dict):
            continue
        flow = str(entry.get("flow") or "flow")
        probe_id = f"probe:{flow}"
        if probe_id in suspect_map:
            continue
        failing.append(probe_id)
        suspect_map[probe_id] = {"file": None, "modules": [target]}
        lines.append(f"Failing probe flow {probe_id}:")
        lines.append(f"  exercised produced file: {target}")
        lines.append(f"  {entry.get('detail') or ''}")
    if not failing:
        return "", {}, []
    if has_spec:
        names = ", ".join(spec_paths) or "the goal-named spec"
        lines.append(
            f"Precedence (system): {names} is the authoritative "
            "specification for this goal, and these records are "
            "the system's own probes against it — the suspect is "
            "the produced file, not the probe. Fix the file so "
            "the recorded flows behave as the contract pins."
        )
    else:
        lines.append(
            "Precedence (system): no external spec governs this "
            "run; the goal text governs, and these records are "
            "the system's own probes — the suspect is the "
            "produced file, not the probe."
        )
    return "\n".join(lines), suspect_map, failing


def _replan_required_clause_ids(
    brief: dict[str, Any], clauses: list[dict[str, Any]]
) -> list[str]:
    """The brief's open-clause set, in frozen-list order (build #7).

    The union of the judge's uncovered clauses (every clause the
    current plan version leaves unverified or failed) and the
    clause IDs in the loop-stamped probe failures — both computed
    by the system from the frozen list, never from model output.
    Intersected with the frozen list: an ID the list does not
    carry cannot be required of the delta. This set is the delta
    rule's assignment: each of these clauses must be implemented
    and verified somewhere in the re-plan.
    """
    named: set[str] = set()
    for entry in brief.get("uncovered_clauses") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            named.add(entry["id"])
    for record in brief.get("contract_probe_failures") or []:
        if isinstance(record, dict) and isinstance(
            record.get("clause_id"), str
        ):
            named.add(record["clause_id"])
    return [
        str(clause["id"])
        for clause in clauses
        if str(clause["id"]) in named
    ]


def replan_from_brief(
    brief: dict[str, Any],
    settings: LLMSettings | None = None,
    *,
    max_repairs: int = 2,
    repo_root: str | Path | None = None,
    seed_forbidden: list[str] | None = None,
    attempt_sink: Callable[[dict[str, Any]], None] | None = None,
) -> list[PlanNode]:
    """Produce delta-plan nodes from a judge's re-plan brief (M1).

    Campaign build #9: ``attempt_sink``, when given, is installed
    as the planner attempt sink for this call (see
    planner_attempt_sink) — every re-plan skeleton and
    content-fill attempt is recorded to it as well as to stdout.
    A sink already installed by an outer scope (a run's) is
    restored on exit. The rest of this docstring is the body's.
    """
    if attempt_sink is None:
        return _replan_from_brief(
            brief,
            settings,
            max_repairs=max_repairs,
            repo_root=repo_root,
            seed_forbidden=seed_forbidden,
        )
    with planner_attempt_sink(attempt_sink):
        return _replan_from_brief(
            brief,
            settings,
            max_repairs=max_repairs,
            repo_root=repo_root,
            seed_forbidden=seed_forbidden,
        )


def _replan_from_brief(
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

    Campaign build #7: when the goal named a contract, the parse
    runs the DELTA clause rule (clause_mode="delta"), not the
    initial planner's full-coverage rule: every emitted node must
    declare inputs.clauses, and the brief's open clauses
    (_replan_required_clause_ids) must be implemented and verified
    within the delta. The rule runs inside this call's repair
    cycle, so a clause-blind output is re-asked with the coverage
    errors named — arm #16's wholesale terminal rejection (24
    full-coverage errors, no v2) is the failure this removes. On
    success the delta's declared coverage is written back into
    the brief under "replan_clause_coverage" (the
    rejected_approaches channel), for the loop to stamp into the
    new plan version's metadata.

    ``seed_forbidden`` (or the brief's "seed_forbidden" key, which the loop
    fills from the initial plan's metadata) carries the approaches rejected
    in initial planning into this replan call — retry #20: without it the
    replan starts with a fresh forbidden list and Flask, rejected in
    initial planning, came back as the replan's third attempt. The
    accumulated rejected-approaches list is written back into the brief
    under "rejected_approaches" so the loop can carry it into the next
    replan's seed.

    Campaign build #9: emission is two-phase, mirroring build
    #5's split in plan_task_intent. Phase 1 emits the delta
    SKELETON (structure, targets, deps, criteria, content
    intents, clause declarations — no file contents) through the
    same repair cycle, judged by build #7's delta rule at this
    phase (the rule, the anchoring facts, and the re-ask
    machinery all live in that cycle; the repair budget is
    unchanged at 2). Phase 2 fills each content-less write node
    through the shared _fill_plan_contents — the same per-node
    validators and build #8 shape-repair blocks as the initial
    planner — and the filled delta is assembled through the
    ordinary full parse under the delta rule and the contract
    pin, so the result is indistinguishable in shape from a
    single-shot re-plan output. Arm #18 is why: its re-plan had
    to rewrite server.py wholesale — not a small response — and
    the single-shot emission died at raw JSON validity before
    the delta rule could judge anything (the second arm running
    build #7 went unmeasured). The accepted emission is written
    back into the brief under "plan_emission", on the same
    write-back channel as "replan_clause_coverage".
    """

    index = build_repo_index(repo_root)
    context = format_repo_context(index, workspace=brief.get("workspace"))
    # Spec grounding (2026-11-23), as in plan_task_intent: a fresh
    # planning call gets the goal-named spec verbatim again — the
    # initial call's grounding does not transfer through the brief.
    specs = goal_named_specs(
        _goal_text_from_brief(brief), resolve_repo_root(repo_root), index
    )
    spec_section = ""
    if specs:
        spec_section = f"\n\n{format_spec_context(specs)}"
    # Contract clauses (campaign build #3; delta rule, build #7):
    # the frozen list is re-extracted from the same spec text (the
    # contract does not change mid-run, so the list is identical
    # to the one stamped into the accepted plan's metadata). The
    # prompt carries the re-plan block — the frozen list, the
    # per-node declaration rule, and the brief's open clauses as
    # the delta's assigned targets — and parse validates the
    # delta's declarations under the delta rule inside the repair
    # cycle (see the docstring). Build #3 judged the delta by the
    # full-coverage rule in this same slot; arm #16 measured what
    # that costs (a wholesale rejection no repair round could
    # answer, because the demand itself was the wrong shape for a
    # delta). The backstop for coverage a delta drops is
    # per-version and unchanged: the verdict gate and the next
    # brief recompute every clause's state from the new version's
    # own nodes, so a dropped clause re-opens instead of passing.
    clauses = clauses_for_specs(specs)
    required_clause_ids = _replan_required_clause_ids(brief, clauses)
    clauses_section = ""
    if clauses:
        clauses_section = (
            f"\n\n{format_replan_clauses_block(clauses, required_clause_ids)}"
        )
    # Existing-file inventory (campaign build #4), as in
    # plan_task_intent: a re-plan writes into the same tree, and a
    # delta that scaffolds an existing file the brief never named
    # is the same loss in a later version.
    inventory_section = _existing_files_section(specs, repo_root)
    # Re-plan diagnostics (2026-11-23): the suspect map + precedence
    # block, computed from the brief's judge-stamped failing tests
    # and the files on disk. The map is stamped back into the brief
    # on the same write-back channel as rejected_approaches, so the
    # loop's delta validation can name the exercised modules in a
    # stagnation rejection. Empty for a brief without failing tests:
    # the prompt then renders exactly as before.
    diagnostics_text, suspect_map = _replan_diagnostics(
        brief,
        resolve_repo_root(repo_root),
        has_spec=bool(specs),
        spec_paths=[spec.path for spec in specs],
    )
    if suspect_map:
        brief["suspect_map"] = suspect_map
    diagnostics_block = f"\n\n{diagnostics_text}" if diagnostics_text else ""

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
    # Contract pinning: the loop seeds the accepted plan's module
    # contract into the brief (plan metadata -> brief, the
    # seed_forbidden channel). A malformed value cannot occur from
    # that channel (parse wrote it normalized); if one arrives
    # anyway it is treated as no pin rather than crashing the
    # re-plan.
    pinned_contract, _pin_errors = _normalize_contract(
        brief.get("module_contract")
    )
    # Retry #22: as in plan_task_intent, the ROUTE GOAL shape goes
    # last (after the node shape and the instruction), immediately
    # before "JSON Output:" — it is the decision, not preamble. The
    # seed block goes BEFORE the shape, so the shape stays last.
    user_prompt = (
        f"{context}{spec_section}{clauses_section}{inventory_section}\n\n{_format_brief_for_prompt(brief)}{diagnostics_block}{node_shape_block}\n\n"
        "Produce a NEW delta plan DAG for the brief above. Do NOT repeat, "
        "quote, or echo the brief — it is your input, not your output.\n"
        "\nSPLIT EMISSION (campaign build #9): emit the delta SKELETON "
        "only. Do NOT include any file \"content\", \"edits\", or "
        "\"anchor\" in any node — file payloads are requested "
        "separately, one node at a time, after the skeleton is "
        "accepted. For each scaffold/file_writer node, declare "
        "inputs \"content_intent\": \"create\" for a NEW file (filled "
        "with its full content), \"edit\" for an EXISTING file "
        "(filled with scoped/anchored edits), or \"whole_file\" for "
        "an existing file that must be rewritten whole (which "
        "requires the overwrite_justification + impact-check pair "
        "the modify rules demand). Keep each node's file_path, "
        "depends_on, criteria, and inputs.clauses declarations "
        "exactly as a full delta plan would carry them. Bash/command "
        "nodes keep their command in the skeleton as usual.\n"
        f"{seed_block}"
        f"{shape_block}\n\n"
        "JSON Output:"
    )
    nodes, metadata = _call_llm_with_repair(
        replan_system_prompt(),
        user_prompt,
        settings,
        max_repairs=max_repairs,
        goal_text=_goal_text_from_brief(brief),
        workspace=brief.get("workspace"),
        repo_root=repo_root,
        seed_forbidden=merged_seed or None,
        pinned_contract=pinned_contract,
        clauses=clauses,
        formatter=formatter_settings_from_env(),
        skeleton=True,
        clause_mode="delta",
        clause_required=required_clause_ids if clauses else None,
        attempt_phase="replan_skeleton",
    )
    # Campaign build #9, phase 2 + assembly: fill each content-less
    # write node of the accepted delta skeleton with its own call
    # (the shared fill machinery — identical per-node validation
    # and shape repair to the initial planner's), then run the
    # filled delta through the ordinary full parse under the same
    # delta clause rule and the same contract pin, so the
    # assembled delta carries exactly the shape and judgments a
    # single-shot re-plan output carried. The contract pin's
    # content-level coherence checks bind here, at assembly, as
    # they do for the initial planner (build #5): a skeleton has
    # no contents for them to judge. A delta the model emitted
    # pre-filled needs neither step — its nodes were judged whole
    # at the skeleton phase.
    filled = _fill_plan_contents(
        nodes,
        settings=settings,
        goal_text=_goal_text_from_brief(brief),
        repo_root=repo_root,
        clauses=clauses,
        spec_section=spec_section,
        inventory_section=inventory_section,
        attempt_phase="replan_content_fill",
    )
    if filled is not None:
        filled_dicts, fill_attempts, prefilled_ids = filled
        assembled_nodes, _assembled_metadata = parse_planner_response(
            json.dumps({"nodes": filled_dicts, "metadata": {}}),
            goal_text=_goal_text_from_brief(brief),
            workspace=brief.get("workspace"),
            repo_root=repo_root,
            pinned_contract=pinned_contract,
            clauses=clauses,
            clause_mode="delta",
            clause_required=required_clause_ids if clauses else None,
        )
        nodes = assembled_nodes
        brief["plan_emission"] = {
            "phases": ["replan_skeleton", "replan_content_fill"],
            "content_attempts": fill_attempts,
            "prefilled": prefilled_ids,
        }
    brief["rejected_approaches"] = metadata.get("rejected_approaches", seed or [])
    if clauses:
        # Campaign build #7: the accepted delta's declared
        # coverage, written back on the rejected_approaches
        # channel — the loop stamps it into the new plan
        # version's metadata, so which clauses the delta claimed
        # to address is part of the run record.
        brief["replan_clause_coverage"] = {
            "required": required_clause_ids,
            **delta_clause_coverage(nodes, clauses),
        }
    if metadata.get(CONTRACT_REASK_METADATA_KEY) is not None:
        brief[CONTRACT_REASK_METADATA_KEY] = metadata[
            CONTRACT_REASK_METADATA_KEY
        ]
    if metadata.get(INTENT_CORRECTIONS_METADATA_KEY):
        # Campaign build #11: the delta skeleton's deterministic
        # intent corrections (the skeleton phase parses through
        # the same parse_planner_response as the initial planner,
        # so the correction applies here identically) write back
        # on the same channel — the loop stamps them into the new
        # plan version's metadata, so a delta whose declared
        # intents the system had to correct says so in the run
        # record.
        brief["intent_corrections"] = metadata[
            INTENT_CORRECTIONS_METADATA_KEY
        ]
    if specs:
        brief[SPEC_CONTEXT_METADATA_KEY] = [
            spec.record() for spec in specs
        ]
    return nodes


# ── Node-local repair (cycle 8) ──────────────────────────────────────
#
# When an executed measurement fails, the loop's re-plan re-asks the
# whole task. Live re-run #5 showed the shape that cannot survive
# that ask: the planner can copy a definition exactly OR compute its
# fields freely, not both in one plan-shaped output — accepted v1
# plans returned the definition unextended, and whole-task re-plans
# lost the vessel (a clobber module carrying real probe code, a
# complete do_GET at the wrong indentation, a stand-in plan). The
# node-local repair is the seam the loop lacked: before a whole-task
# re-plan, the harness identifies the edit node whose write produced
# the code the failed measurement exercised, takes the definition
# exactly as it stands in the file on disk (the harness holds the
# copy), and asks for ONLY that definition's replacement — the model
# supplies only the substance, in the smallest possible window.
#
# The mapping is conservative by construction: every step must hold
# with no ambiguity, and any doubt returns None so the caller runs
# today's whole-task re-plan unchanged. A produced candidate is
# "same plan, one node replaced" — the culprit's content spliced,
# every other node preserved, execution state reset as for any new
# version — and it faces the FULL validator stack (every code,
# placeholder_value included) before the loop may use it; a
# candidate that fails validation is discarded, never repaired
# further. The attempt rides the caller's re-plan decision, so it
# consumes exactly one unit of the existing re-plan budget.

_WRITE_CLAWS = ("scaffold", "file_writer")

# The input keys the executor merges into a node after it runs
# (memory._OUTPUT_INPUT_KEYS / _VOLATILE_INPUT_KEYS): a new plan
# version's nodes must not carry the previous pass's residue.
_EXECUTOR_RESIDUE_INPUT_KEYS = (
    "exit_code",
    "stdout",
    "stderr",
    "duration_s",
    "workdir",
    "timeout_s",
)

_TRACEBACK_FRAME_RE = re.compile(r'File "(?P<path>[^"]+\.py)", line (?P<line>\d+)')
_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class _LocalRepairTarget:
    """One confidently identified node-local repair (cycle 8)."""

    failed_id: str
    culprit_id: str
    target_path: str  # resolved on-disk path of the measured module
    qualified_name: str  # the definition to rewrite
    span: tuple[int, int]  # (first line, last line) inside the culprit content
    indent: int  # the definition's indentation in the file
    window: str  # the definition's text, exactly as it stands
    diagnostics: str  # the failed node's bounded diagnostics excerpt
    criterion_detail: str  # the failed criterion's one-line detail


def _node_write_target(node: PlanNode) -> str | None:
    """The file a write node writes, by the claw's own expression.

    Mirrors _missing_target_errors (cycle 7): inputs "file_path",
    else "path", else the first inputs "outputs" entry. None when
    the node is not a write node or names no target.
    """
    if node.assigned_claw not in _WRITE_CLAWS:
        return None
    inputs = node.inputs or {}
    target = inputs.get("file_path") or inputs.get("path")
    if not target:
        outputs = inputs.get("outputs")
        if isinstance(outputs, (list, tuple)) and outputs:
            target = outputs[0]
    if isinstance(target, str) and target:
        return target
    return None


def _command_script_paths(command: str) -> list[str]:
    """The .py script paths a command runs (never the interpreter).

    Plain token scan of the failed node's command: the script a test
    node executed is a token in its own command (`./.venv/bin/python
    /abs/test_health.py`), while the interpreter token never ends in
    .py. Redirect targets are writes, not runs, and are skipped.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    scripts: list[str] = []
    previous = ""
    for token in tokens:
        if (
            token.endswith(".py")
            and previous not in (">", ">>")
            and not token.startswith("-")
            and token not in scripts
        ):
            scripts.append(token)
        previous = token
    return scripts


def _script_source(plan: ExecutionPlan, script: str, root: Path) -> str | None:
    """The source of a script a plan node ran.

    The payload of the node that wrote it when the plan carries it
    (a heredoc write or a scaffold content), else the file on disk —
    the run may have written it outside the repo root, where only
    the disk copy exists.
    """
    for node in plan.nodes:
        inputs = node.inputs or {}
        command = inputs.get("command")
        if node.assigned_claw in ("bash", "shell_exec") and isinstance(
            command, str
        ):
            sole = _sole_heredoc_write(command)
            writes = [sole] if sole is not None else [
                (target, content)
                for target, content, is_append in _bash_code_writes(command)
                if not is_append
            ]
            for target, payload in writes:
                if _same_path(target, script):
                    return payload
        target = _node_write_target(node)
        if target is not None and _same_path(target, script):
            content = inputs.get("content")
            if isinstance(content, str):
                return content
    try:
        return _resolve_plan_target(script, root).read_text(encoding="utf-8")
    except OSError:
        return None


def _imported_module_files(source: str, root: Path) -> list[Path]:
    """Files under the repo root that a script's imports name.

    `from freetalon.api import X` names freetalon/api.py; a longer
    dotted name is shortened until a file matches, so a submodule
    import still lands on the module file that defines the code
    under test.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.append(node.module)
    files: list[Path] = []
    for module in modules:
        parts = module.split(".")
        for cut in range(len(parts), 0, -1):
            candidate = root.joinpath(*parts[:cut]).with_suffix(".py")
            if candidate.is_file():
                files.append(candidate)
                break
            package = root.joinpath(*parts[:cut]) / "__init__.py"
            if package.is_file():
                files.append(package)
                break
    return files


def _route_literals(source: str) -> list[str]:
    """Route-shaped string constants ("/health") a script names."""
    routes: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return routes
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if (
                value.startswith("/")
                and len(value) > 1
                and " " not in value
                and value not in routes
            ):
                routes.append(value)
    return routes


def _local_repair_target(
    plan: ExecutionPlan, brief: dict[str, Any], root: Path
) -> tuple[_LocalRepairTarget | None, tuple[str, str] | None]:
    """Map a brief's first mappable failure to its culprit edit node.

    Confidence is all-or-nothing; every requirement is mandatory and
    any failure returns None (the caller falls back to whole-task
    re-plan):

    - the failed node has an executed FAIL measurement (at least one
      measured, failing result) and a non-empty diagnostics excerpt;
    - the failed node is not itself a write node (a failed write is
      a different failure class — the seam repairs measured code,
      not failed writes);
    - exactly ONE file is linked to the failure: the test script the
      failed node ran imports it, or a traceback frame in the
      diagnostics names it — and exactly ONE completed write node in
      this plan version wrote that file;
    - exactly one window definition is identified inside the file:
      the innermost definition containing a diagnostics frame line,
      or the route-branch anchor of a route literal the test names;
    - the window text in the culprit node's content is identical to
      the text on disk, so the culprit is the file's current author
      for that definition (no later writer drifted it).

    Campaign build #2: on failure the second element is the decline
    (reason, detail) of the entry that got FURTHEST through the
    checks — the binding constraint on the seam, not the first
    entry's incidental one (an early write-node entry must not mask
    a later entry's suspect ambiguity).
    """
    nodes_by_id = {node.id: node for node in plan.nodes}
    writers: dict[str, list[PlanNode]] = {}
    for node in plan.nodes:
        target = _node_write_target(node)
        if target is None or node.status != PlanStatus.COMPLETED:
            continue
        resolved = str(_resolve_plan_target(target, root).resolve())
        writers.setdefault(resolved, []).append(node)
    if not writers:
        return None, (
            "no_writers",
            "no completed write node in this plan version",
        )
    entries = [
        entry
        for entry in brief.get("failed_nodes") or []
        if isinstance(entry, dict)
    ]
    if not entries:
        return None, ("no_failed_nodes", "the brief names no failed nodes")
    best: tuple[str, str] | None = None
    best_depth = -1
    for entry in entries:
        target, decline, depth = _local_repair_target_for_entry(
            plan, entry, nodes_by_id, writers, root
        )
        if target is not None:
            return target, None
        if decline is not None and depth > best_depth:
            best, best_depth = decline, depth
    return None, best


def _local_repair_target_for_entry(
    plan: ExecutionPlan,
    entry: dict[str, Any],
    nodes_by_id: dict[str, PlanNode],
    writers: dict[str, list[PlanNode]],
    root: Path,
) -> tuple[_LocalRepairTarget | None, tuple[str, str] | None, int]:
    """One entry's target search: (target, decline, depth).

    Depth is the ordinal of the check that failed — the caller
    keeps the deepest entry's decline as the seam's reason
    (campaign build #2 decline observability).
    """
    failed_id = entry.get("id")
    failed_node = nodes_by_id.get(failed_id) if isinstance(failed_id, str) else None
    if failed_node is None:
        return None, (
            "failed_node_unknown",
            f"failed node {failed_id!r} is not in this plan version",
        ), 0
    if _node_write_target(failed_node) is not None:
        return None, (
            "failed_node_is_write",
            f"failed node {failed_id} is itself a write node",
        ), 1
    measurement = entry.get("measurement") or failed_node.measurement or {}
    if not isinstance(measurement, dict) or measurement.get("verdict") != VERDICT_FAIL:
        return None, (
            "no_fail_measurement",
            f"failed node {failed_id} has no FAIL measurement",
        ), 2
    results = measurement.get("results") or []
    if not any(
        isinstance(result, dict) and result.get("measured") and not result.get("passed")
        for result in results
    ):
        return None, (
            "no_measured_failure",
            f"failed node {failed_id} has no measured failing result",
        ), 3
    diagnostics = entry.get("diagnostics")
    if not isinstance(diagnostics, str) or not diagnostics.strip():
        return None, (
            "no_diagnostics",
            f"failed node {failed_id} carries no diagnostics excerpt",
        ), 4
    inputs = entry.get("inputs") or failed_node.inputs or {}
    command = inputs.get("command")
    if not isinstance(command, str):
        command = (failed_node.inputs or {}).get("command") or ""

    candidate_files: list[str] = []
    frame_lines: dict[str, int] = {}
    for match in _TRACEBACK_FRAME_RE.finditer(diagnostics):
        resolved = str(_resolve_plan_target(match.group("path"), root).resolve())
        if resolved in writers and resolved not in candidate_files:
            candidate_files.append(resolved)
            frame_lines[resolved] = int(match.group("line"))
    script_sources: list[str] = []
    for script in _command_script_paths(command):
        source = _script_source(plan, script, root)
        if source is None:
            continue
        script_sources.append(source)
        for module_file in _imported_module_files(source, root):
            resolved = str(module_file.resolve())
            if resolved in writers and resolved not in candidate_files:
                candidate_files.append(resolved)
    if len(candidate_files) != 1:
        return None, (
            "suspect_not_unique",
            "the failure links to "
            f"{len(candidate_files)} produced files, not exactly one",
        ), 5
    resolved = candidate_files[0]
    culprits = writers[resolved]
    if len(culprits) != 1:
        if len(culprits) > 1:
            return None, (
                "multiple_writers",
                f"{len(culprits)} completed write nodes wrote {resolved}",
            ), 6
        return None, (
            "no_completed_writer",
            f"no completed write node wrote {resolved}",
        ), 6
    culprit = culprits[0]
    content = (culprit.inputs or {}).get("content")
    if not isinstance(content, str) or not content:
        return None, (
            "content_missing",
            f"culprit node {culprit.id} carries no content",
        ), 7
    try:
        disk_source = Path(resolved).read_text(encoding="utf-8")
    except OSError:
        return None, (
            "disk_unreadable",
            f"the suspect file {resolved} cannot be read from disk",
        ), 8
    spans = _definition_spans(content)
    if not spans:
        return None, (
            "no_definitions",
            f"no definition spans in {resolved}",
        ), 9
    qualified: str | None = None
    span: tuple[int, int] | None = None
    indent = 0
    if resolved in frame_lines:
        line = frame_lines[resolved]
        containing = [item for item in spans if item[1] <= line <= item[2]]
        if containing:
            chosen = min(containing, key=lambda item: item[2] - item[1])
            qualified, span, indent = chosen[0], (chosen[1], chosen[2]), chosen[3]
    if qualified is None:
        anchors: set[str] = set()
        for source in script_sources:
            for route in _route_literals(source):
                name = _route_branch_anchor(content, route)
                if name is not None:
                    anchors.add(name)
        if len(anchors) != 1:
            return None, (
                "window_not_identified",
                f"{len(anchors)} route-branch anchors in {resolved}, "
                "not exactly one, and no frame line names a window",
            ), 10
        qualified = anchors.pop()
        for item in spans:
            if item[0] == qualified:
                span, indent = (item[1], item[2]), item[3]
                break
        if span is None:
            return None, (
                "window_not_identified",
                f"anchor {qualified} has no span in {resolved}",
            ), 10
    lines = content.splitlines()
    window = "\n".join(lines[span[0] - 1 : span[1]])
    disk_lines = disk_source.splitlines()
    disk_span = {item[0]: item for item in _definition_spans(disk_source)}.get(
        qualified
    )
    if disk_span is None:
        return None, (
            "disk_window_missing",
            f"definition {qualified} is not present in {resolved} on disk",
        ), 11
    disk_window = "\n".join(disk_lines[disk_span[1] - 1 : disk_span[2]])
    if disk_window != window:
        return None, (
            "disk_drift",
            f"the on-disk text of {qualified} in {resolved} differs "
            "from the culprit node's content",
        ), 12
    return _LocalRepairTarget(
        failed_id=failed_id,
        culprit_id=culprit.id,
        target_path=resolved,
        qualified_name=qualified,
        span=span,
        indent=indent,
        window=window,
        diagnostics=diagnostics,
        criterion_detail=measurement_failed_detail(measurement),
    ), None, 13



def _local_repair_prompts(
    goal_text: str, target: _LocalRepairTarget, spec_block: str = ""
) -> tuple[str, str]:
    """The focused repair ask: the window as it stands, the failure,
    and cycle 7's demand — complete real code, computed values,
    attempt-and-fallback, no invented constants, no placeholders.
    The window is shown unnumbered, as codebase grounding shows code.

    When the goal names a spec, ``spec_block`` carries its verbatim
    text (spec grounding, 2026-11-23): the repair is a code-writing
    call for code the spec governs, so it gets the text directly —
    the planner's understanding does not transfer through the plan.
    """
    spec_section = f"{spec_block}\n\n" if spec_block else ""
    user_prompt = (
        f"THE TASK IS UNCHANGED: {goal_text}\n\n"
        f"{spec_section}"
        f"One step of the current plan (node {target.culprit_id}) wrote "
        f"{target.target_path}, and a later step (node {target.failed_id}) "
        f"measured the result and failed: {target.criterion_detail}.\n\n"
        f"Captured output from failed node {target.failed_id} (tail):\n"
        f"{target.diagnostics}\n\n"
        f"The definition below is {target.qualified_name} in "
        f"{target.target_path}, exactly as it stands in the file now:\n\n"
        f"{target.window}\n\n"
        "Return ONLY the complete new version of this one definition "
        "(def line included, at its indentation in the file — "
        f"{target.indent} spaces) with the failing behavior fixed, "
        "keeping the rest of the definition's code as it is; the system "
        "splices it into the current file, so do not write the whole "
        "module and do not add other definitions. Write the real code "
        "that computes each value the definition reports — probe the "
        "service or run the command the goal names and parse its "
        "output; where a probe can fail, attempt it and report None "
        "(False for a yes/no field) when it fails. Never assign an "
        "invented constant for a value that exists only by measuring "
        'it, and never assign "..." — both are rejected.'
    )
    return LOCAL_REPAIR_SYSTEM_PROMPT, user_prompt


def _extract_repaired_definition(
    text: str, qualified_name: str
) -> tuple[list[str], int] | None:
    """The repaired definition's lines from an LLM response.

    Deterministic extraction only (the materialization spirit): the
    first fenced block that holds the definition when fences are
    present, else the raw text; dedented; the span whose qualified
    name matches the window's — exactly, or as a trailing part of it
    (the model may return just the method, or the method inside its
    class). The deepest unique match wins. Returns (lines, the
    definition's indentation in the dedented text), or None when no
    span matches or the deepest match is ambiguous.
    """
    blocks = [match.group(1) for match in _CODE_FENCE_RE.finditer(text)]
    for candidate in blocks or [text]:
        dedented = textwrap.dedent(candidate).strip("\n")
        if not dedented.strip():
            continue
        matches = [
            item
            for item in _definition_spans(dedented)
            if item[0] == qualified_name
            or qualified_name.endswith("." + item[0])
        ]
        if not matches:
            continue
        deepest = max(item[0].count(".") for item in matches)
        best = [item for item in matches if item[0].count(".") == deepest]
        if len(best) != 1:
            return None
        _name, start, end, indent = best[0]
        return dedented.splitlines()[start - 1 : end], indent
    return None


def _shift_lines(lines: list[str], shift: int) -> list[str] | None:
    """Uniformly re-indent lines; None when a dedent would cross
    column 0 (the response's shape cannot be placed in the window).
    """
    if shift == 0:
        return list(lines)
    shifted: list[str] = []
    for line in lines:
        if not line.strip():
            shifted.append(line)
            continue
        if shift > 0:
            shifted.append(" " * shift + line)
        else:
            if not line.startswith(" " * (-shift)):
                return None
            shifted.append(line[-shift:])
    return shifted


def _local_repair_candidate_nodes(
    plan: ExecutionPlan, target: _LocalRepairTarget, new_content: str
) -> list[PlanNode]:
    """Same plan, one node replaced (cycle 8).

    Every node is deep-copied with its execution state reset — the
    state a freshly built version carries (status DRAFT, no
    measurement, no error/exit/duration, inputs shed of executor
    residue) — because build_execution_plan re-executes the version
    wholesale. Only the culprit's content changes; it also records
    what it answers (responds_to / differs_by) so the judge's delta
    validation sees a genuine, stated difference from the failed
    attempt rather than a repeat of it.
    """
    nodes: list[PlanNode] = []
    for node in plan.nodes:
        copied = node.model_copy(deep=True)
        copied.status = PlanStatus.DRAFT
        copied.measurement = {}
        copied.error = None
        copied.exit_code = None
        copied.duration_s = None
        inputs = dict(copied.inputs or {})
        for key in _EXECUTOR_RESIDUE_INPUT_KEYS:
            inputs.pop(key, None)
        copied.inputs = inputs
        if copied.id == target.culprit_id:
            copied.inputs = {**inputs, "content": new_content}
            note = (
                f"node-local repair: {target.qualified_name} rewritten in "
                f"response to the measured failure at {target.failed_id} "
                f"({target.criterion_detail})"
            )
            copied.differs_by = (
                f"{copied.differs_by}; {note}" if copied.differs_by else note
            )
            copied.responds_to = target.failed_id
        nodes.append(copied)
    return nodes


def _local_repair_shape_repair_block(target: _LocalRepairTarget) -> str:
    """The format re-ask after an unextractable answer (build #13).

    Build #8's shape-repair pattern for cycle 8's seam: the
    defect named in plain words, the exact required form
    restated (the one definition, its def line included, at the
    window's indentation — nothing else), and one minimal worked
    example of the form against a generic definition.
    """
    lines = [
        "Your previous response was REJECTED — it did not answer "
        "in the required format:",
        f"  - your previous response carried no definition "
        f"matching {target.qualified_name} that the system "
        "could extract from it",
        "",
        "The ONLY acceptable answer is the complete new version "
        f"of the one definition {target.qualified_name} — its "
        f"def line included, at the indentation the task shows "
        f"({target.indent} spaces) — and nothing else: no JSON, "
        "no prose before or after, no other definitions, and "
        "not the whole module. (A single code fence holding only "
        "the definition is accepted; prose around it is not.)",
        "",
        "Worked example of the form — a response repairing a "
        "definition named compute_total is exactly:",
        "",
        "def compute_total(items):",
        "    return sum(items)",
        "",
        "— the definition alone, at its own indentation.",
        "Return the repaired definition only.",
    ]
    return "\n".join(lines)


def _is_fabricated_error_body(response_text: str) -> bool:
    """True when a seam response is a fabricated API-error body.

    Campaign build #14: arm #23's scoped-edit seam received, in
    all six recorded responses, a JSON object shaped like an API
    error — {"error": {"message": ..., "type":
    "invalid_request_error", "code": ...}} — naming a model that
    exists nowhere on the box. Ordered by the prompt to emit edit
    blocks and by the transport to emit JSON, the model invented
    the error that explained the missing blocks. The transport
    contradiction is fixed at the settings layer; this classifier
    names the shape if it ever recurs, so a decline records
    error_body_response instead of masquerading as an ordinary
    no-blocks / no-definition answer.
    """
    text = response_text.strip()
    if not text.startswith("{"):
        return False
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return isinstance(error, (dict, str)) and bool(error)


def _scoped_edit_decline_reason(problem: str | None) -> str:
    """The decline reason a scoped-edit problem class records."""
    if problem == "malformed":
        return "malformed_edit_blocks"
    if problem == "error_body":
        return "error_body_response"
    return "no_edit_blocks"


def try_local_repair(
    plan: ExecutionPlan,
    brief: dict[str, Any],
    settings: LLMSettings | None = None,
    declines: list[dict[str, Any]] | None = None,
) -> list[PlanNode] | None:
    """Attempt a node-local repair of a failed plan version (cycle 8).

    Returns the replacement node list for a new version of the same
    plan — the culprit edit node's content spliced with the model's
    repaired definition, every other node preserved — or None when
    the culprit cannot be identified with confidence, the model's
    answer cannot be spliced, the splice changes nothing, or the
    candidate fails the full validator stack. None always means the
    caller proceeds with the whole-task re-plan exactly as before;
    this function never raises.

    Campaign build #2: when ``declines`` is supplied, a None return
    appends exactly one structured record to it —
    {"seam": "local_repair", "reason": <code>, "detail": <short
    string>} — including the blanket-exception path below
    (reason "internal_error"), so a decline is never again
    observable only as the absence of a success.

    Campaign build #13: every model call this seam makes is
    recorded with its full raw response (_record_seam_attempt),
    and an answer no definition can be extracted from earns one
    format re-ask inside the same invocation — arm #22's
    unrecorded seam responses are the defect this ends.
    """
    before = len(declines) if declines is not None else 0
    try:
        return _try_local_repair(plan, brief, settings, declines)
    except Exception as exc:  # noqa: BLE001 - any doubt falls back to whole-task re-plan
        if declines is not None and len(declines) == before:
            declines.append(
                {
                    "seam": "local_repair",
                    "reason": "internal_error",
                    "detail": str(exc)[:200],
                }
            )
        return None


def _try_local_repair(
    plan: ExecutionPlan,
    brief: dict[str, Any],
    settings: LLMSettings | None,
    declines: list[dict[str, Any]] | None = None,
) -> list[PlanNode] | None:
    def decline(reason: str, detail: str = "") -> None:
        if declines is not None:
            declines.append(
                {"seam": "local_repair", "reason": reason, "detail": detail}
            )
        return None

    metadata = plan.metadata or {}
    pinned_contract = _contract_from_metadata(metadata)
    stamped = metadata.get("repo_root")
    root = (
        Path(stamped)
        if isinstance(stamped, str) and stamped
        else resolve_repo_root(None)
    )
    target, target_decline = _local_repair_target(plan, brief, root)
    if target is None:
        reason, detail = target_decline or ("no_target", "")
        return decline(reason, detail)
    culprit = next(node for node in plan.nodes if node.id == target.culprit_id)
    content = (culprit.inputs or {})["content"]
    goal = brief.get("goal")
    goal_text = plan.intent.goal
    if isinstance(goal, dict) and isinstance(goal.get("objective"), str):
        goal_text = goal["objective"]
    # Spec grounding (2026-11-23): a goal-named spec reaches the
    # repair call verbatim too (see _local_repair_prompts). An
    # unloadable named spec raises SpecContextError, which
    # try_local_repair's blanket fallback turns into a whole-task
    # re-plan — where the same loader raises it loudly at planning.
    specs = goal_named_specs(goal_text, root)
    system_prompt, user_prompt = _local_repair_prompts(
        goal_text, target, format_spec_context(specs)
    )
    effective = settings or LLMSettings.from_env()
    if effective.num_ctx is None:
        effective = dataclass_replace(effective, num_ctx=_PLANNER_NUM_CTX)
    # The repair returns one definition, not a plan envelope —
    # the envelope grammar would corrupt the code payload (the
    # cycle-2 two-channel finding). Build #14: the mode is stated
    # explicitly as unconstrained — dropping the schema alone
    # used to leave the transport substituting a "json" directive
    # the prompt never ordered.
    effective = dataclass_replace(
        effective, format_schema=None, format_mode="unconstrained"
    )
    # Campaign build #13: the call is recorded (raw response
    # included), and an answer from which no matching definition
    # can be extracted earns ONE format re-ask — the scoped-edit
    # seam's treatment, and symmetric by construction: extraction
    # failure is this seam's no-blocks class, a response whose
    # SHAPE is wrong (prose, JSON, a different definition, the
    # whole module without the one definition) rather than a
    # repair that failed on substance. The re-ask restates this
    # seam's own format (build #8's shape-repair pattern); it is
    # INSIDE this invocation, so the seam still rides the
    # caller's one re-plan budget unit, and a second extraction
    # failure declines no_definition_extracted exactly as
    # before — now with both responses on record.
    seam_target = f"{target.target_path}::{target.qualified_name}"
    response = call_llm(system_prompt, user_prompt, settings=effective)
    extracted = _extract_repaired_definition(response, target.qualified_name)
    _record_seam_attempt(
        seam="local_repair",
        attempt=1,
        outcome="accepted" if extracted is not None else "rejected",
        response_text=response,
        decline_reason=(
            ""
            if extracted is not None
            else (
                "error_body_response"
                if _is_fabricated_error_body(response)
                else "no_definition_extracted"
            )
        ),
        plan_id=plan.plan_id,
        plan_version=brief.get("plan_version"),
        target=seam_target,
    )
    if extracted is None:
        response = call_llm(
            system_prompt,
            user_prompt
            + "\n\n"
            + _local_repair_shape_repair_block(target),
            settings=effective,
        )
        extracted = _extract_repaired_definition(
            response, target.qualified_name
        )
        _record_seam_attempt(
            seam="local_repair",
            attempt=2,
            outcome="accepted" if extracted is not None else "rejected",
            response_text=response,
            decline_reason=(
                ""
                if extracted is not None
                else (
                    "error_body_response"
                    if _is_fabricated_error_body(response)
                    else "no_definition_extracted"
                )
            ),
            shape_repair=True,
            plan_id=plan.plan_id,
            plan_version=brief.get("plan_version"),
            target=seam_target,
        )
    if extracted is None:
        if _is_fabricated_error_body(response):
            return decline(
                "error_body_response",
                "the model's answer was a fabricated API-error "
                "body (an invented {\"error\": ...} object), not "
                "a definition",
            )
        return decline(
            "no_definition_extracted",
            "the model's answer holds no definition matching "
            f"{target.qualified_name}",
        )
    repaired_lines, response_indent = extracted
    shifted = _shift_lines(repaired_lines, target.indent - response_indent)
    if shifted is None:
        return decline(
            "splice_indent_failed",
            "the repaired definition cannot be re-indented to the "
            "window's indentation",
        )
    lines = content.splitlines()
    new_content = "\n".join(
        lines[: target.span[0] - 1] + shifted + lines[target.span[1] :]
    )
    if content.endswith("\n"):
        new_content += "\n"
    if new_content == content:
        return decline("no_change", "the splice reproduces the file exactly")
    new_spans = {item[0]: item for item in _definition_spans(new_content)}
    new_span = new_spans.get(target.qualified_name)
    if new_span is None:
        return decline(
            "definition_vanished",
            f"the spliced file no longer defines {target.qualified_name}",
        )
    new_window = "\n".join(
        new_content.splitlines()[new_span[1] - 1 : new_span[2]]
    )
    if new_window == target.window:
        return decline(
            "window_unchanged",
            f"the repaired {target.qualified_name} is identical to "
            "the window as it stood",
        )
    # Campaign build #3: edit grounding — the spliced definition
    # must answer to the file it lands in. A reference the repair
    # introduces that the file cannot resolve (self.<attr> the
    # class never assigns, a call to an undefined name) declines
    # the candidate like any other doubt in this seam.
    # Campaign build #15: attribute chains decline under their
    # own reason — a chain the file cannot resolve (arm #24's
    # self.server.pairing_store in a file whose handler reaches
    # the store as self.store) teaches the re-plan a different
    # lesson than a bare ungrounded name, and the decline text
    # carries the file's own convention for the role.
    chain_violations = attribute_grounding_violations(content, new_content)
    if chain_violations:
        return decline("ungrounded_attribute", "; ".join(chain_violations))
    grounding = edit_grounding_violations(content, new_content)
    if grounding:
        return decline("ungrounded_reference", "; ".join(grounding))
    nodes = _local_repair_candidate_nodes(plan, target, new_content)
    workspace = (plan.intent.constraints or {}).get("workspace")
    records, _cheap = _collect_plan_error_records(
        nodes,
        root,
        goal_text,
        workspace,
        index=build_repo_index(root),
        modules=_ModuleSourceCache(),
        contract=(
            pinned_contract
            if pinned_contract is not None
            else _CONTRACT_UNSUPPLIED
        ),
        plan_context="derivative",
    )
    if records:
        return decline(
            "validator_records",
            "candidate rejected: "
            + ", ".join(sorted({record.code for record in records})),
        )
    return nodes


# ── Scoped-edit repair (architecture campaign step 1, 2026-11-23) ────
#
# The second node-local seam, tried after cycle 8's single-definition
# repair and before the whole-task re-plan. Exam re-arms #7-#9 named
# the failure this answers: a whole-task re-plan's write nodes carry
# whole file contents, so every version regenerates every file it
# touches — v2's server.py passed a test that v3's wholesale rewrite
# broke again, and the score oscillated at 30-31/35. Here the model
# never re-emits a file: it answers with Aider-style search/replace
# blocks for ONE suspect file, and the harness applies them
# mechanically (scoped_edits: exact match, exactly-once, no
# overlaps, all-or-nothing). Everything outside the blocks is
# preserved by construction, so a repair cannot regress code it did
# not touch.
#
# Targeting rides the judge's own signals, with cycle 8's
# all-or-nothing confidence: the brief's failing tests feed
# _replan_diagnostics, whose suspect map names the produced modules
# each test exercises. The seam fires only when those modules are
# EXACTLY ONE file (the failing tests' own files are evidence in
# the prompt, never the edit target — a seam must not edit the
# measurement it is judged by; stale tests are the oracle-test
# step's problem, and the precedence rule in the diagnostics block
# tells the model when a test, not the code, is wrong), exactly one
# completed write node in this plan version wrote that file, and
# the node's content is byte-identical to the file on disk. A
# suspect map computed over a capped prefix of the failing tests
# (more tests than _SUSPECT_TEST_CAP) is partial evidence and also
# declines. Any doubt — ambiguity, drift, oversize, an unparseable
# or non-applying edit set, a syntax break, any validator record —
# returns None and the caller runs the whole-task re-plan exactly
# as before; this seam never raises and never writes anything
# itself. The attempt rides the caller's re-plan decision, so it
# consumes exactly one unit of the existing re-plan budget, the
# same as cycle 8's repair and the whole-task re-plan.
#
# Campaign build #2 (2026-11-23): two changes. (1) Decline
# observability — every None path in this seam (and, for symmetry,
# cycle 8's) records a structured reason through the optional
# ``declines`` out-param; arm #10's silent decline at its only
# repair opportunity is the defect this ends. (2) Pair support —
# the target is the suspect map's module UNION, and a union of two
# files is now a target (per-file all-or-nothing, candidate-level
# atomicity): arm #10's span was exactly two files (store.poll +
# server.do_GET), the shape the single-file seam could not take.
#
# Campaign build #12 (2026-11-24): a second targeting source.
# Arm #21's failure named no failing tests at all — the plan's
# test node TIMED OUT against a hanging server — so this seam
# declined no_diagnostics in all three cycles while the loop's
# own smoke probe held precise per-flow diagnostics against
# exactly one produced file. _probe_repair_diagnostics now
# derives the same (diagnostics block, suspect map) pair from
# the brief's probe evidence when the failing-tests source is
# empty: the failed flows stand in for failing tests, judged
# against the same cap, the same writer/disk/size preconditions,
# and the same grounding checks. Cycle 8's seam is unchanged:
# its targeting needs a definition window (a traceback frame or
# a route anchor in an executed failure's diagnostics), which a
# flow record does not carry — its preconditions do not fit
# this evidence class, so it keeps declining on it as before.

from .prompts import SCOPED_EDIT_REPAIR_SYSTEM_PROMPT as _SCOPED_EDIT_PROMPT

# (The scoped_edits names — ScopedEdit, ScopedEditError,
# apply_scoped_edits, parse_scoped_edits, python_syntax_error —
# are imported at the top of this module since campaign build #2,
# which also uses them in plan materialization.)

# The model must hold the suspect file verbatim to write exact
# SEARCH text; beyond this size the seam declines rather than
# truncating the file it would ask the model to match against.
_SCOPED_EDIT_MAX_FILE_CHARS = 24_000

# Campaign build #2: pair support (below) lets the seam target a
# minimal PAIR of suspect files in one candidate. The model must
# hold BOTH files verbatim, so the pair's combined size is capped
# tighter than twice the single-file cap.
_SCOPED_EDIT_MAX_PAIR_CHARS = 36_000


@dataclass(frozen=True)
class _ScopedEditTarget:
    """One confidently identified scoped-edit repair."""

    failed_id: str | None  # first failed node, for responds_to
    culprit_id: str
    target_path: str  # resolved on-disk path of the suspect file
    relative_path: str  # repo-root-relative, as the brief names it
    content: str  # the file exactly as it stands (node == disk)
    failing_tests: list[str]  # the brief's failing tests, in order
    criterion_detail: str  # one-line summary for the differs_by note


def _scoped_edit_targets(
    plan: ExecutionPlan,
    brief: dict[str, Any],
    root: Path,
    suspect_map: dict[str, dict[str, Any]],
    declines: list[dict[str, Any]] | None = None,
) -> list[_ScopedEditTarget] | None:
    """The seam's targets: ONE suspect file, or a minimal PAIR.

    The suspect map (from _replan_diagnostics) names, per failing
    test, the produced modules it exercises; the seam's field is
    the UNION of those modules. Campaign step 1 fired only on a
    union of exactly one; arm #10's only repair opportunity had a
    union of two (the failing poll behavior spanned store.poll and
    server.do_GET) and declined silently. Campaign build #2: a
    union of one or two files is a target — per-file requirements
    unchanged (exactly one completed writer, node content
    byte-identical to disk, the file is never a failing test's own
    file, <= the per-file size cap) plus a combined cap for the
    pair. A union of zero or of three-plus still declines: beyond
    a pair the suspect span is a re-plan's work, not a seam's.

    Every decline appends its record to ``declines`` when supplied
    (campaign build #2 observability), naming the binding reason.
    """

    def decline(reason: str, detail: str = "") -> None:
        if declines is not None:
            declines.append(
                {
                    "seam": "scoped_edit_repair",
                    "reason": reason,
                    "detail": detail,
                }
            )
        return None

    failing = [str(test) for test in brief.get("failing_tests") or []]
    if not failing:
        return decline(
            "no_failing_tests", "the brief names no failing tests"
        )
    if len(failing) > _SUSPECT_TEST_CAP:
        return decline(
            "suspect_cap_exceeded",
            f"{len(failing)} failing tests exceed the suspect cap "
            f"of {_SUSPECT_TEST_CAP}; the suspect map would be "
            "partial evidence",
        )
    modules: set[str] = set()
    test_files: set[str] = set()
    for entry in suspect_map.values():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("file"), str):
            test_files.add(entry["file"])
        for module in entry.get("modules") or []:
            if isinstance(module, str):
                modules.add(module)
    if not modules:
        return decline(
            "no_suspect_module",
            "the failing tests exercise no produced module",
        )
    if len(modules) > 2:
        return decline(
            "suspect_not_unique",
            "the failing tests exercise "
            f"{len(modules)} produced modules ("
            + ", ".join(sorted(modules))
            + "); the seam targets one file or a minimal pair",
        )
    writers: dict[str, list[PlanNode]] = {}
    for node in plan.nodes:
        target = _node_write_target(node)
        if target is None or node.status != PlanStatus.COMPLETED:
            continue
        resolved = str(_resolve_plan_target(target, root).resolve())
        writers.setdefault(resolved, []).append(node)
    failed_id: str | None = None
    for entry in brief.get("failed_nodes") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            failed_id = entry["id"]
            break
    detail = f"{len(failing)} failing test(s): {failing[0]}"
    if len(failing) > 1:
        detail += f" (+{len(failing) - 1} more)"
    targets: list[_ScopedEditTarget] = []
    for relative in sorted(modules):
        if relative in test_files:
            # An exercised "module" that is a failing test's own
            # file: editing the measurement is outside this seam.
            return decline(
                "test_file_target",
                f"suspect {relative} is a failing test's own file",
            )
        resolved_target = str(_resolve_plan_target(relative, root).resolve())
        culprits = writers.get(resolved_target) or []
        if len(culprits) != 1:
            if len(culprits) > 1:
                return decline(
                    "multiple_writers",
                    f"{len(culprits)} completed write nodes wrote "
                    f"{relative}",
                )
            return decline(
                "no_completed_writer",
                f"no completed write node wrote {relative}",
            )
        culprit = culprits[0]
        content = (culprit.inputs or {}).get("content")
        if not isinstance(content, str) or not content:
            return decline(
                "content_missing",
                f"culprit node {culprit.id} carries no content",
            )
        if len(content) > _SCOPED_EDIT_MAX_FILE_CHARS:
            return decline(
                "file_too_large",
                f"{relative} is {len(content)} chars, over the "
                f"{_SCOPED_EDIT_MAX_FILE_CHARS}-char seam cap",
            )
        try:
            disk_source = Path(resolved_target).read_text(encoding="utf-8")
        except OSError:
            return decline(
                "disk_unreadable",
                f"the suspect file {relative} cannot be read from disk",
            )
        if disk_source != content:
            return decline(
                "disk_drift",
                f"the on-disk text of {relative} differs from the "
                "culprit node's content",
            )
        targets.append(
            _ScopedEditTarget(
                failed_id=failed_id,
                culprit_id=culprit.id,
                target_path=resolved_target,
                relative_path=relative,
                content=content,
                failing_tests=failing,
                criterion_detail=detail,
            )
        )
    if len(targets) == 2:
        combined = sum(len(target.content) for target in targets)
        if combined > _SCOPED_EDIT_MAX_PAIR_CHARS:
            return decline(
                "pair_too_large",
                f"the pair is {combined} chars combined, over the "
                f"{_SCOPED_EDIT_MAX_PAIR_CHARS}-char pair cap",
            )
    return targets


def _scoped_edit_prompts(
    goal_text: str,
    targets: list[_ScopedEditTarget],
    diagnostics_block: str,
    spec_block: str = "",
) -> tuple[str, str]:
    """The scoped-edit ask: the diagnostics, the file(s) as they
    stand, and the demand — edit blocks only, copied exactly. For
    a pair (campaign build #2), both files are shown under FILE:
    headers and the answer must group its blocks under the same
    headers, one section per file. When the goal names a spec,
    ``spec_block`` carries its verbatim text (spec grounding, as
    in the other repair paths).
    """
    spec_section = f"{spec_block}\n\n" if spec_block else ""
    if len(targets) == 1:
        target = targets[0]
        user_prompt = (
            f"THE TASK IS UNCHANGED: {goal_text}\n\n"
            f"{spec_section}"
            f"One step of the current plan (node {target.culprit_id}) wrote "
            f"{target.relative_path}, and later steps measured the result: "
            f"{target.criterion_detail}.\n\n"
            f"{diagnostics_block}\n\n"
            f"The file below is {target.relative_path}, exactly as it "
            f"stands in the file now:\n\n"
            f"{target.content}\n\n"
            "Return ONLY search/replace edit blocks for this one file, "
            "in the format the system prompt shows — one block per "
            "change, every SEARCH text copied character-for-character "
            "from the file above and appearing exactly once in it. Do "
            "not write the whole file and do not touch any other file."
        )
        return _SCOPED_EDIT_PROMPT, user_prompt
    files_section = "\n\n".join(
        f"FILE: {target.relative_path}\n\n{target.content}"
        for target in targets
    )
    names = " and ".join(target.relative_path for target in targets)
    user_prompt = (
        f"THE TASK IS UNCHANGED: {goal_text}\n\n"
        f"{spec_section}"
        f"Steps of the current plan wrote {names}, and later steps "
        f"measured the result: {targets[0].criterion_detail}. The "
        "failure spans both files; each is shown below exactly as "
        f"it stands now, under its FILE: header.\n\n"
        f"{diagnostics_block}\n\n"
        f"{files_section}\n\n"
        "Return search/replace edit blocks for these two files "
        "ONLY, grouped under a line 'FILE: <path>' naming each "
        "file exactly as shown — one section per file, every "
        "SEARCH text copied character-for-character from its own "
        "file's section and appearing exactly once in that file. "
        "Do not write either whole file and do not touch any "
        "other file."
    )
    pair_addendum = (
        "\n\nPAIR FORM: this repair spans TWO files. Group the "
        "blocks under a line 'FILE: <path>' per file, exactly as "
        "the task names them; every SEARCH text belongs to the "
        "file of its own section."
    )
    return _SCOPED_EDIT_PROMPT + pair_addendum, user_prompt


def _scoped_edit_sections(
    response: str, targets: list[_ScopedEditTarget]
) -> tuple[dict[str, list[ScopedEdit]] | None, str | None]:
    """Split a scoped-edit response into per-file edit lists.

    Returns (sections, None) on success — sections maps each
    target's relative path to its edits — or (None, problem) with
    problem "malformed" (a block or FILE section that does not
    parse), "empty" (a file's section carries no blocks), or
    "missing_section" (a pair target has no section). The single-
    file form is the step-1 format unchanged: bare blocks, no
    header. The pair form requires one 'FILE: <path>' section per
    target; a block marker before the first header, an unknown or
    repeated header, is malformed — a block in the wrong section
    then fails application against that section's file, which is
    the atomicity the candidate relies on.
    """
    if len(targets) == 1:
        edits = parse_scoped_edits(response)
        if edits is None:
            return None, "malformed"
        if not edits:
            return None, "empty"
        return {targets[0].relative_path: edits}, None
    wanted = {target.relative_path for target in targets}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("FILE:"):
            name = stripped[len("FILE:") :].strip()
            if name not in wanted or name in sections:
                return None, "malformed"
            sections[name] = []
            current = name
            continue
        if current is None:
            if stripped.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                return None, "malformed"
            continue
        sections[current].append(line)
    if len(sections) != len(targets):
        return None, "missing_section"
    parsed: dict[str, list[ScopedEdit]] = {}
    for target in targets:
        edits = parse_scoped_edits("\n".join(sections[target.relative_path]))
        if edits is None:
            return None, "malformed"
        if not edits:
            return None, "empty"
        parsed[target.relative_path] = edits
    return parsed, None


def _scoped_edit_candidate_nodes(
    plan: ExecutionPlan,
    edited: list[tuple[_ScopedEditTarget, str]],
) -> list[PlanNode]:
    """Same plan, the edited nodes' contents replaced.

    Mirrors _local_repair_candidate_nodes (cycle 8): every node is
    deep-copied with its execution state reset, only the culprits'
    contents change, and each culprit records what it answers so
    the judge's delta validation sees a genuine, stated
    difference. ``edited`` carries one (target, new content) pair
    per edited file — one for a single-file repair, two for a pair
    (campaign build #2); each file's edit set was applied
    all-or-nothing before this builder runs, so the candidate is
    never half-applied.
    """
    replacements = {
        target.culprit_id: (target, new_content)
        for target, new_content in edited
    }
    pair = len(edited) > 1
    nodes: list[PlanNode] = []
    for node in plan.nodes:
        copied = node.model_copy(deep=True)
        copied.status = PlanStatus.DRAFT
        copied.measurement = {}
        copied.error = None
        copied.exit_code = None
        copied.duration_s = None
        inputs = dict(copied.inputs or {})
        for key in _EXECUTOR_RESIDUE_INPUT_KEYS:
            inputs.pop(key, None)
        copied.inputs = inputs
        if copied.id in replacements:
            target, new_content = replacements[copied.id]
            copied.inputs = {**inputs, "content": new_content}
            note = (
                f"scoped-edit repair"
                + (" (pair)" if pair else "")
                + f": {target.relative_path} edited in "
                f"response to the measured failure "
                f"({target.criterion_detail}); search/replace blocks "
                f"applied mechanically to the file as it stood, the "
                f"rest of the file preserved by construction"
            )
            copied.differs_by = (
                f"{copied.differs_by}; {note}" if copied.differs_by else note
            )
            if target.failed_id is not None:
                copied.responds_to = target.failed_id
        nodes.append(copied)
    return nodes


def _scoped_edit_shape_repair_block(
    problem: str | None, targets: list[_ScopedEditTarget]
) -> str:
    """The format re-ask after a no-blocks response (build #13).

    Build #8's shape-repair pattern for the seam: the previous
    response's defect named in plain words, the exact required
    block format restated, one minimal worked example against a
    generic snippet, and the contract's boundaries restated —
    prose and whole-file returns are not answers; the only
    non-block answer is the explicitly empty return the seam's
    contract already allows when the file is already correct.
    """
    if problem == "missing_section":
        defect = (
            "your previous response did not group its blocks "
            "under one 'FILE: <path>' section per file — a "
            "repair spanning two files must carry both sections, "
            "one per file, or it cannot be applied"
        )
    else:
        defect = (
            "your previous response carried no SEARCH/REPLACE "
            "edit blocks at all — no block in the required format "
            "was found in it"
        )
    lines = [
        "Your previous response was REJECTED — it did not answer "
        "in the required format:",
        f"  - {defect}",
        "",
        "The ONLY acceptable answer is search/replace edit "
        "blocks in EXACTLY this format, one block per change:",
        "",
        "<<<<<<< SEARCH",
        "<existing text, copied character-for-character from "
        "the file>",
        "=======",
        "<the new text>",
        ">>>>>>> REPLACE",
        "",
        "Worked example — changing one line of a file:",
        "",
        "<<<<<<< SEARCH",
        "LIMIT = 10",
        "=======",
        "LIMIT = 20",
        ">>>>>>> REPLACE",
        "",
        "Prose explanations, JSON, and whole-file returns are "
        "NOT acceptable answers, and neither is a summary of the "
        "change you intend: return the blocks themselves.",
    ]
    if len(targets) > 1:
        headers = " and ".join(
            f"'FILE: {target.relative_path}'" for target in targets
        )
        lines += [
            f"This repair spans TWO files: group the blocks "
            f"under the lines {headers} — one section per file, "
            "every SEARCH text copied from its own file's "
            "section above.",
            "",
        ]
    lines += [
        "One exception, from the repair contract itself: if — "
        "and only if — the file is already correct against the "
        "goal and the diagnostics, return an empty response (no "
        "text at all) rather than inventing an edit. Any other "
        "answer must be edit blocks.",
        "Return the edit blocks only.",
    ]
    return "\n".join(lines)


def try_scoped_edit_repair(
    plan: ExecutionPlan,
    brief: dict[str, Any],
    settings: LLMSettings | None = None,
    declines: list[dict[str, Any]] | None = None,
) -> list[PlanNode] | None:
    """Attempt a scoped-edit repair of a failed plan version.

    Returns the replacement node list for a new version of the same
    plan — the suspect file's (or pair's) content with the model's
    search/replace blocks applied, every other node preserved — or
    None when the suspect cannot be identified with confidence, the
    model's answer carries no applicable edit set, an edited file
    fails syntax or the full validator stack, or the edit changes
    nothing. None always means the caller proceeds with the
    whole-task re-plan exactly as before; this function never
    raises.

    Campaign build #2: when ``declines`` is supplied, a None return
    appends exactly one structured record to it —
    {"seam": "scoped_edit_repair", "reason": <code>, "detail":
    <short string>} — including the blanket-exception path below
    (reason "internal_error"). Arm #10 declined its only repair
    opportunity with no trace anywhere; that is the defect this
    record exists to end.

    Campaign build #13: every model call this seam makes is
    recorded with its full raw response (_record_seam_attempt),
    and a response carrying no parseable edit blocks earns one
    format re-ask inside the same invocation — arm #22 declined
    3/3 no_edit_blocks with the responses recorded nowhere; both
    halves of that are the defect this ends.
    """
    before = len(declines) if declines is not None else 0
    try:
        return _try_scoped_edit_repair(plan, brief, settings, declines)
    except Exception as exc:  # noqa: BLE001 - any doubt falls back to whole-task re-plan
        if declines is not None and len(declines) == before:
            declines.append(
                {
                    "seam": "scoped_edit_repair",
                    "reason": "internal_error",
                    "detail": str(exc)[:200],
                }
            )
        return None


def _try_scoped_edit_repair(
    plan: ExecutionPlan,
    brief: dict[str, Any],
    settings: LLMSettings | None,
    declines: list[dict[str, Any]] | None = None,
) -> list[PlanNode] | None:
    def decline(reason: str, detail: str = "") -> None:
        if declines is not None:
            declines.append(
                {
                    "seam": "scoped_edit_repair",
                    "reason": reason,
                    "detail": detail,
                }
            )
        return None

    metadata = plan.metadata or {}
    pinned_contract = _contract_from_metadata(metadata)
    stamped = metadata.get("repo_root")
    root = (
        Path(stamped)
        if isinstance(stamped, str) and stamped
        else resolve_repo_root(None)
    )
    goal = brief.get("goal")
    goal_text = plan.intent.goal
    if isinstance(goal, dict) and isinstance(goal.get("objective"), str):
        goal_text = goal["objective"]
    # Spec grounding (2026-11-23): a goal-named spec reaches the
    # repair call verbatim too. An unloadable named spec raises
    # SpecContextError, which try_scoped_edit_repair's blanket
    # fallback turns into a whole-task re-plan — where the same
    # loader raises it loudly at planning.
    specs = goal_named_specs(goal_text, root)
    diagnostics_block, suspect_map = _replan_diagnostics(
        brief,
        root,
        has_spec=bool(specs),
        spec_paths=[spec.path for spec in specs],
    )
    targets_brief = brief
    if not diagnostics_block:
        # Campaign build #12: a brief with no failing tests can
        # still carry the loop's probe evidence — the timeout
        # class, where the plan's own test node hung and named
        # nothing. When the failed probe flows name exactly one
        # produced file, they stand in for the failing tests:
        # the seam targets that file with the flows as its
        # diagnostics (the failed-flow count is judged against
        # the same suspect cap in _scoped_edit_targets). Only
        # when neither source names a suspect does the seam
        # decline no_diagnostics, exactly as before.
        probe_block, probe_map, probe_failing = _probe_repair_diagnostics(
            brief,
            has_spec=bool(specs),
            spec_paths=[spec.path for spec in specs],
        )
        if not probe_block:
            return decline(
                "no_diagnostics",
                "the brief names no failing tests, so no diagnostics "
                "block (and no suspect map) was computed",
            )
        diagnostics_block, suspect_map = probe_block, probe_map
        targets_brief = {**brief, "failing_tests": probe_failing}
    targets = _scoped_edit_targets(
        plan, targets_brief, root, suspect_map, declines
    )
    if targets is None:
        # _scoped_edit_targets recorded the reason itself.
        return None
    system_prompt, user_prompt = _scoped_edit_prompts(
        goal_text, targets, diagnostics_block, format_spec_context(specs)
    )
    effective = settings or LLMSettings.from_env()
    if effective.num_ctx is None:
        effective = dataclass_replace(effective, num_ctx=_PLANNER_NUM_CTX)
    # The repair returns edit blocks, not a plan envelope — the
    # envelope grammar would corrupt the payload (the cycle-2
    # two-channel finding). Build #14: the mode is stated
    # explicitly as unconstrained — dropping the schema alone
    # used to leave the transport substituting a "json" directive,
    # and arm #23 proved what that contradiction produces: six
    # fabricated API-error bodies, zero edit blocks.
    effective = dataclass_replace(
        effective, format_schema=None, format_mode="unconstrained"
    )
    # Campaign build #13: every seam call is recorded (raw
    # response included), and a response carrying no parseable
    # edit blocks earns ONE format re-ask — build #8's
    # shape-repair pattern, which content fill proved on exactly
    # this failure class: the first prompt's bare format demand
    # repeated is not steering, so the re-ask names the defect in
    # plain words, restates the exact block format with a worked
    # example, and restates the contract's one non-block answer
    # (an explicitly empty return when the file is already
    # correct). The re-ask is INSIDE this invocation: the seam
    # still rides the caller's one re-plan budget unit, and a
    # second no-blocks response declines no_edit_blocks exactly
    # as before — now with both responses on record. A MALFORMED
    # response (block markers present but broken) is not
    # re-asked: it declines malformed_edit_blocks on the first
    # response, as before — the model attempted the format and
    # broke its mechanics, a different defect from never
    # attempting it.
    seam_target = ", ".join(target.relative_path for target in targets)
    response = call_llm(system_prompt, user_prompt, settings=effective)
    sections, problem = _scoped_edit_sections(response, targets)
    if sections is None and _is_fabricated_error_body(response):
        problem = "error_body"
    _record_seam_attempt(
        seam="scoped_edit",
        attempt=1,
        outcome="accepted" if sections is not None else "rejected",
        response_text=response,
        decline_reason=(
            ""
            if sections is not None
            else _scoped_edit_decline_reason(problem)
        ),
        plan_id=plan.plan_id,
        plan_version=brief.get("plan_version"),
        target=seam_target,
    )
    if sections is None and problem in ("empty", "missing_section", "error_body"):
        response = call_llm(
            system_prompt,
            user_prompt
            + "\n\n"
            + _scoped_edit_shape_repair_block(problem, targets),
            settings=effective,
        )
        sections, problem = _scoped_edit_sections(response, targets)
        if sections is None and _is_fabricated_error_body(response):
            problem = "error_body"
        _record_seam_attempt(
            seam="scoped_edit",
            attempt=2,
            outcome="accepted" if sections is not None else "rejected",
            response_text=response,
            decline_reason=(
                ""
                if sections is not None
                else _scoped_edit_decline_reason(problem)
            ),
            shape_repair=True,
            plan_id=plan.plan_id,
            plan_version=brief.get("plan_version"),
            target=seam_target,
        )
    if sections is None:
        if problem == "malformed":
            return decline(
                "malformed_edit_blocks",
                "the model's response carries a malformed edit "
                "block or FILE section",
            )
        if problem == "error_body":
            return decline(
                "error_body_response",
                "the model's response was a fabricated API-error "
                "body (an invented {\"error\": ...} object), not "
                "edit blocks",
            )
        if problem == "missing_section":
            return decline(
                "no_edit_blocks",
                "the model's response has no FILE section for one "
                "of the pair's files",
            )
        return decline(
            "no_edit_blocks",
            "the model's response carries no edit blocks",
        )
    edited: list[tuple[_ScopedEditTarget, str]] = []
    for target in targets:
        try:
            new_content = apply_scoped_edits(
                target.content, sections[target.relative_path]
            )
        except ScopedEditError as exc:
            # Candidate-level atomicity: one file's failing edit
            # set declines the whole candidate — a half-applied
            # pair is never produced.
            return decline(
                "blocks_failed_to_apply",
                f"{target.relative_path}: {exc.kind}: {exc}",
            )
        if new_content == target.content:
            return decline(
                "no_change",
                f"the edit set for {target.relative_path} changes "
                "nothing",
            )
        if target.relative_path.endswith(".py"):
            syntax = python_syntax_error(new_content)
            if syntax is not None:
                return decline(
                    "syntax_error", f"{target.relative_path}: {syntax}"
                )
            # Campaign build #3: edit grounding, as in cycle 8's
            # seam — the edited content must answer to the file it
            # lands in (arm #11: edits that applied cleanly and
            # referenced self.store for self.server.store).
            # Campaign build #15: attribute chains decline under
            # their own reason, as in cycle 8's seam above.
            chain_violations = attribute_grounding_violations(
                target.content, new_content
            )
            if chain_violations:
                return decline(
                    "ungrounded_attribute",
                    f"{target.relative_path}: "
                    + "; ".join(chain_violations),
                )
            grounding = edit_grounding_violations(target.content, new_content)
            if grounding:
                return decline(
                    "ungrounded_reference",
                    f"{target.relative_path}: " + "; ".join(grounding),
                )
        edited.append((target, new_content))
    nodes = _scoped_edit_candidate_nodes(plan, edited)
    workspace = (plan.intent.constraints or {}).get("workspace")
    records, _cheap = _collect_plan_error_records(
        nodes,
        root,
        goal_text,
        workspace,
        index=build_repo_index(root),
        modules=_ModuleSourceCache(),
        contract=(
            pinned_contract
            if pinned_contract is not None
            else _CONTRACT_UNSUPPLIED
        ),
        plan_context="derivative",
    )
    if records:
        return decline(
            "validator_records",
            "candidate rejected: "
            + ", ".join(sorted({record.code for record in records})),
        )
    return nodes
