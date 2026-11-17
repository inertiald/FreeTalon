"""LLM-backed planner for producing validated execution DAGs."""

from __future__ import annotations
import json
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
    raw_text: str,
    *,
    repo_root: str | Path | None = None,
    goal_text: str | None = None,
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Validate raw planner output into normalized PlanNodes plus metadata.

    ``repo_root`` overrides the repo the grounding check reads; by default
    it is the checkout holding this package. ``goal_text`` is the goal the
    plan serves; when it names an existing API route, the plan must engage
    that route (anti-fake rule).
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
    # All per-node errors are collected and reported in ONE rejection, so a
    # plan with several independent errors can be fixed in a single repair
    # round instead of burning the repair budget one error at a time.
    errors = _collect_plan_errors(nodes, repo_root, goal_text)
    if errors:
        noun = "error" if len(errors) == 1 else "errors"
        lines = "\n".join(
            f"{index}. {error}" for index, error in enumerate(errors, start=1)
        )
        raise LLMResponseError(
            f"Planner output was rejected with {len(errors)} {noun} — "
            f"fix ALL of them:\n{lines}"
        )

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
            "file_matches, metric, or tests_pass)."
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


def _collect_plan_errors(
    nodes: list[PlanNode],
    repo_root: str | Path | None = None,
    goal_text: str | None = None,
) -> list[str]:
    """Collect EVERY plan validation error, grouped by node, plus plan-level rules.

    Each node is checked against every rule (claw allowlist, structured
    criteria, command/port validation, repo grounding) and every failure is
    reported, so a single rejection can drive one repair round that fixes
    them all. Two plan-level rules run after the per-node checks: static
    stand-in files (a node that echo-writes a hardcoded answer and then
    claims it exists) and route engagement (a goal naming an existing API
    route must have a node that reads, modifies, or calls it). Structural
    errors (bad JSON, duplicate ids, dependency cycles) are not collected
    here — they are raised first by normalize_plan_nodes, since the node
    list cannot be interpreted at all until the DAG is structurally valid.
    """
    errors: list[str] = []
    needs_index = any(
        node.assigned_claw in ("scaffold", "file_writer") for node in nodes
    ) or goal_text is not None
    index = build_repo_index(repo_root) if needs_index else None
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
    errors.extend(_static_standin_errors(nodes))
    if index is not None:
        errors.extend(_route_engagement_errors(nodes, index, goal_text))
    return errors


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


def _static_standin_errors(nodes: list[PlanNode]) -> list[str]:
    """Flag nodes that manufacture their own evidence.

    A bash node that echo-writes a hardcoded answer (a JSON literal or an
    assert string) into a file and then claims file_exists on that same
    file never ran any code or service: the file's contents were asserted,
    not produced. (Retry #2: node-1 echoed a hardcoded api.json; node-2
    echoed an assert sentence into test.sh.) The escape hatch is the
    scaffold claw for real files, or a node that runs the real code and
    checks ITS output.
    """
    errors: list[str] = []
    for node in nodes:
        if node.assigned_claw not in ("bash", "shell_exec"):
            continue
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if not isinstance(command, str):
            continue
        written = _literal_write(command)
        if written is None:
            continue
        target, literal = written
        if not _literal_is_answer(literal):
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
                    "curl the live service)."
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


def _route_goal_shape(goal_text: str | None, repo_root: str | Path | None = None) -> str:
    """A concrete node shape for a goal naming an existing API route.

    When the goal names a route, the delta plan has a known shape — edit
    the serving module, write tests, call the live route on the API port —
    so the replan prompt hands the model a NEW structure to produce
    instead of leaving it to re-serialize the brief's failed steps as
    JSON (retry #4 replan 1 echoed the brief's delta steps verbatim).
    Returns "" when the goal names no route.
    """
    if not goal_text:
        return ""
    index = build_repo_index(repo_root)
    named = _routes_named_in_goal(goal_text, index)
    if not named:
        return ""
    lines = ["ROUTE GOAL — the delta plan must follow this shape:"]
    for route, module in named:
        lines.append(
            f"- {route} is served by {module} on port {index.api_port}: "
            f"node 1 edits {module} (scaffold claw, or a bash heredoc) to "
            f"implement the endpoint; node 2 writes the tests (scaffold or "
            f"heredoc) and runs them; node 3 verifies live with "
            f"curl http://localhost:{index.api_port}{route}. Do NOT probe "
            f"another service's {route} path, and do NOT echo a JSON answer."
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


def _call_llm_with_repair(
    system_prompt: str,
    user_prompt: str,
    settings: LLMSettings | None,
    *,
    max_repairs: int = 2,
    goal_text: str | None = None,
) -> tuple[list[PlanNode], dict[str, Any]]:
    """Call the LLM and parse the plan, repairing rejected output.

    On LLMResponseError the model is re-prompted with the rejection reason
    and a short description of what was wrong with the failed output —
    never the output itself, which at temperature 0 would just be copied
    again. Bounded: at most ``max_repairs`` repair attempts, then the last
    error is raised. Nothing is silently substituted — a plan the model
    cannot fix is an honest failure.
    """
    prompt = user_prompt
    last_error: LLMResponseError | None = None
    for attempt in range(max_repairs + 1):
        response_text = call_llm(system_prompt, prompt, settings=settings)
        try:
            return parse_planner_response(response_text, goal_text=goal_text)
        except LLMResponseError as exc:
            last_error = exc
            if attempt >= max_repairs:
                break
            prompt = (
                f"{user_prompt}\n\n"
                "Your previous output was REJECTED. Do not repeat it.\n"
                f"Rejection reason: {exc}\n"
                f"Previous output: {_describe_failed_output(response_text)}\n\n"
                "Return ONLY a corrected JSON object with a \"nodes\" array that "
                "fixes ALL of the rejection reasons listed above.\n"
                "JSON Output:"
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
    nodes, metadata = _call_llm_with_repair(
        PLANNER_SYSTEM_PROMPT,
        f"{context}\n\n"
        f"{_format_intent_for_prompt(intent)}\n\n"
        "Produce a NEW execution plan DAG for the goal above. Your output "
        "must be a JSON object with a \"nodes\" array — do NOT repeat the "
        "goal text or any JSON from this prompt.\n"
        "JSON Output:",
        settings,
        max_repairs=max_repairs,
        goal_text=intent.goal,
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

    context = format_repo_context(build_repo_index(repo_root))
    shape = _route_goal_shape(_goal_text_from_brief(brief), repo_root)
    shape_block = f"\n\n{shape}" if shape else ""
    nodes, _metadata = _call_llm_with_repair(
        REPLAN_SYSTEM_PROMPT,
        f"{context}\n\n{_format_brief_for_prompt(brief)}{shape_block}\n\n"
        "Produce a NEW delta plan DAG for the brief above. Do NOT repeat, "
        "quote, or echo the brief — it is your input, not your output.\n"
        "JSON Output:",
        settings,
        max_repairs=max_repairs,
        goal_text=_goal_text_from_brief(brief),
    )
    return nodes
