"""Asyncio-driven DAG execution engine for FreeTalon."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING, TypedDict

from .measure import (
    VERDICT_PASS,
    build_envelope,
    measure_node,
    measurement_failed_detail,
)
from .models import ExecutionPlan, PlanNode, PlanStatus
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry, UnknownCapabilityError

if TYPE_CHECKING:
    from freetalon.audit import AuditLogger
    from .run_store import RunStore
    from .tool_scaffolder import ToolScaffolder

logger = logging.getLogger(__name__)

_NO_RESULT: dict[str, Any] = {}
DEPENDENCY_MISSING_KEY = "dependency_missing"
MAX_INJECTION_ATTEMPTS = 3


class _DependencyRequestRequired(TypedDict):
    objectives: list[str]


class DependencyRequest(_DependencyRequestRequired, total=False):
    inputs: dict[str, Any]


def _is_dependency_missing(result: Any) -> bool:
    return isinstance(result, dict) and DEPENDENCY_MISSING_KEY in result


# Floor under the timeout of a node that runs the full test suite
# (planner A/B refactor, defect 2). The suite takes ~385s end to end;
# the default per-node timeout is 300s, so a suite node at the
# default can never pass — retry #33's accepted plan died exactly
# here, its suite node killed at 300.0s with the suite still running.
_SUITE_NODE_TIMEOUT_S = 900.0

_UNITTEST_VALUE_FLAGS = frozenset(
    {"-s", "-p", "-t", "--start-directory", "--pattern", "--top-level-directory"}
)
_PYTEST_VALUE_FLAGS = frozenset(
    {
        "-k",
        "-m",
        "-p",
        "-o",
        "--maxfail",
        "--timeout",
        "--rootdir",
        "--basetemp",
        "--deselect",
        "--ignore",
        "--override-ini",
        "--log-level",
    }
)
_PYTEST_SUITE_TARGETS = frozenset({"tests", "tests/", "./tests", ".", "./"})


def _positional_args(args: list[str], value_flags: frozenset) -> list[str]:
    """Non-flag tokens in ``args``, skipping the values of known flags."""
    positional: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positional.append(token)
    return positional


def _command_runs_full_suite(command: str) -> bool:
    """True when a bash command runs the repo's whole test suite in one go.

    Recognizes the shapes plans actually use: unittest discovery
    (``python -m unittest discover ...``), a bare unittest module run
    (``python -m unittest`` naming no test module — discovery by
    another name), and pytest with no scoped target (or only the
    tests directory). A scoped run — one test module, one test file,
    one node id — is fast and keeps the node's own timeout. The scan
    is per shell segment (``&&``/``||``/``;``), token-based, and only
    ever RAISES a timeout ceiling, so a false positive costs a longer
    leash on one node, never a wrong verdict.
    """
    for segment in re.split(r"&&|\|\||;", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        for position, token in enumerate(tokens):
            if token == "unittest" and position >= 1 and tokens[position - 1] == "-m":
                args = tokens[position + 1 :]
                if "discover" in args or not _positional_args(
                    args, _UNITTEST_VALUE_FLAGS
                ):
                    return True
            if token.rsplit("/", 1)[-1] == "pytest":
                positional = _positional_args(
                    tokens[position + 1 :], _PYTEST_VALUE_FLAGS
                )
                if not positional or all(
                    target in _PYTEST_SUITE_TARGETS for target in positional
                ):
                    return True
    return False


def _effective_node_timeout(node: PlanNode) -> float:
    """The timeout a node actually runs under.

    A bash node that runs the full test suite gets a floor of
    _SUITE_NODE_TIMEOUT_S under its timeout (defect 2): the
    derivation lives here, not in the planner, because validator
    steering cannot create executability — it would spend repair
    budget making the model split a run the goal explicitly demands
    whole ("keep the full test suite green"), and each fragment's
    cost is unknowable at plan time. The executor-side floor
    guarantees the invariant — a plan that runs the suite is
    executable within its timeout — by construction, for LLM plans
    and hand-built plans alike. A node that declares a larger
    timeout keeps it; every other node keeps exactly its own.
    """
    if (node.assigned_claw or "") in ("bash", "shell_exec"):
        command = node.inputs.get("command") or node.inputs.get("cmd")
        if isinstance(command, str) and _command_runs_full_suite(command):
            return max(node.timeout_s, _SUITE_NODE_TIMEOUT_S)
    return node.timeout_s


def _plan_repo_root(plan: ExecutionPlan) -> Path | None:
    """The repo root a plan was grounded against, from its metadata.

    Cycle 4: plan_task_intent stamps metadata["repo_root"] with the
    root its validators judged the plan's relative references against
    (the interpreter check requires ./.venv/bin/python to exist under
    that root; scaffold targets and criterion paths resolve there).
    Execution must resolve the same references against the same base
    or a validated plan fails at run time — the /health re-run's
    node-3 died with exit 127 in every plan version because the bash
    claw ran from the node's run directory instead. A stamp that does
    not name an existing directory is stale (the checkout moved):
    ignore it, with a warning, and keep the default base rather than
    crashing a run on metadata. Plans with no stamp — hand-built
    plans, run_goal, every non-repo vertical — return None and keep
    today's behavior exactly.
    """
    raw = (plan.metadata or {}).get("repo_root")
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return None
    root = Path(raw).expanduser()
    if not root.is_dir():
        logger.warning(
            "Plan %s carries repo_root %r, which is not an existing "
            "directory; ignoring the stamp.",
            plan.plan_id,
            raw,
        )
        return None
    return root.resolve()


# --- Execution-side interpreter resolution (campaign build #21) ---
#
# The planner grounds every python invocation in a plan to
# ./.venv/bin/python (planner._normalize_plan_interpreters rewrites
# even bare `python`/`python3` to it), and the validator exempts
# that spelling from its existence check on the reasoning that the
# venv is a machine fact — the executing machine has it. That held
# for the main repo and the long-lived pairing exam worktree (both
# carry a .venv), and broke on the held-out screening set: the
# driver clones a FRESH worktree per run and never installs a venv,
# so every verification node died with exit 127
# ("/bin/sh: 1: ./.venv/bin/python: not found") and the judge spent
# the re-plan budget on a phantom code failure while the shipped
# work passed every hidden test (10 such measurements across the
# four task-B runs, all with the identical stderr). Resolution
# therefore also happens here, at execution, against the same repo
# root validation judged the plan against: when the grounded
# interpreter does not exist under that root, the token is
# rewritten to the interpreter this process is running under
# (sys.executable) — present by construction, and able to run the
# project's code because it is running the project. When the venv
# exists, commands pass through byte-identical, so the main repo
# and the pairing arms are untouched.

_VENV_INTERPRETER_SPELLINGS = ("./.venv/bin/python", ".venv/bin/python")

# Segment separators, kept on split so a command reassembles
# byte-identically apart from rewritten tokens. Mirrors
# planner._INTERPRETER_SEGMENT_SPLIT_RE (the planner cannot be
# imported here — it imports this module).
_RESOLUTION_SEGMENT_SPLIT_RE = re.compile(r"(\n|&&|\|\||;|\|)")

# Heredoc detection for span protection: same `cat > target <<MARKER`
# shape the planner's normalization protects
# (planner._STRIP_HEREDOC_RE) — a heredoc body is file content, not
# shell, and is never rewritten.
_RESOLUTION_HEREDOC_RE = re.compile(
    r"cat\s*>>?\s*(?P<target>\S+)\s*<<-?\s*['\"]?(?P<marker>[A-Za-z_][A-Za-z0-9_]*)['\"]?"
)

# Shell builtins and keywords: never an interpreter token. Mirrors
# planner._SHELL_SKIP_TOKENS.
_RESOLUTION_SKIP_TOKENS = frozenset({
    "cd", "echo", "export", "source", ".", "test", "[", "[[", "true",
    "false", "set", "unset", "eval", "exec", "exit", "return", "shift",
    "trap", "umask", "alias", "unalias", "if", "then", "else", "elif",
    "fi", "for", "while", "until", "do", "done", "case", "esac",
    "function", "select", "time", "coproc", "{", "}",
})


def _resolution_heredoc_spans(command: str) -> list[tuple[int, int]]:
    """Character spans of heredoc bodies in a bash command.

    Mirrors planner._heredoc_body_spans: the head line and the
    closing marker line are shell and stay unprotected; the lines
    between them are file content. An unterminated heredoc protects
    nothing past its head line.
    """
    spans: list[tuple[int, int]] = []
    rest_start = 0
    while True:
        match = _RESOLUTION_HEREDOC_RE.search(command, rest_start)
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


def _resolution_token_span(text: str, start: int) -> tuple[int, int] | None:
    """Raw span of the next shell token at/after ``start``.

    Quote- and escape-aware, mirroring planner._scan_token_span.
    Returns None when only whitespace remains.
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


def _resolve_segment_interpreter(
    segment: str, fallback: str
) -> tuple[str, tuple[str, str] | None]:
    """Rewrite one shell segment's grounded interpreter token.

    The interpreter token is identified exactly as the validator
    identifies it: shlex tokens, leading env assignments stripped,
    first remaining token. Only an exact venv spelling is rewritten,
    at its raw span; anything else — skip tokens, other binaries,
    segments shlex cannot tokenize — is returned byte-identical.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return segment, None
    index = 0
    while index < len(tokens) and re.match(
        r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]
    ):
        index += 1
    if index >= len(tokens):
        return segment, None
    token = tokens[index]
    if token in _RESOLUTION_SKIP_TOKENS or token not in _VENV_INTERPRETER_SPELLINGS:
        return segment, None
    span: tuple[int, int] | None = None
    position = 0
    for _ in range(index + 1):
        span = _resolution_token_span(segment, position)
        if span is None:
            return segment, None
        position = span[1]
    return segment[: span[0]] + fallback + segment[span[1] :], (token, fallback)


def _resolve_grounded_interpreter(
    command: str, repo_root: Path | None
) -> tuple[str, list[tuple[str, str]]]:
    """Resolve a bash command's grounded venv interpreter for execution.

    Returns (command, substitutions): the possibly rewritten command
    and the (original, replacement) pairs applied. A no-op —
    byte-identical command, no substitutions — when there is no
    repo root, when <root>/.venv/bin/python exists (the main repo,
    the pairing exam worktree), or when no covered token sits in
    command position. Heredoc bodies are never rewritten.
    """
    if repo_root is None or not command:
        return command, []
    if (repo_root / ".venv" / "bin" / "python").exists():
        return command, []
    fallback = sys.executable
    if not fallback:
        return command, []
    protected = _resolution_heredoc_spans(command)
    parts = _RESOLUTION_SEGMENT_SPLIT_RE.split(command)
    out: list[str] = []
    substitutions: list[tuple[str, str]] = []
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
        resolved, substitution = _resolve_segment_interpreter(part, fallback)
        out.append(resolved)
        if substitution is not None:
            substitutions.append(substitution)
    return "".join(out), substitutions


class ExecutorError(RuntimeError):
    """Raised when the executor cannot make forward progress."""


class Executor:
    def __init__(
        self,
        store: ExecutionPlanStateStore,
        registry: ToolRegistry,
        max_concurrency: int = 8,
        subdag_planner: Callable[[DependencyRequest], list[PlanNode]] | None = None,
        scaffolder: ToolScaffolder | None = None,
        audit_logger: AuditLogger | None = None,
        run_store: "RunStore | None" = None,
        run_id: str | None = None,
        custom_checkers: dict | None = None,
        plan_version: int = 1,
        gate_callback: "Callable[[PlanNode], bool] | None" = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._subdag_planner = subdag_planner
        self._injection_counts: dict[str, int] = {}
        self._scaffolder = scaffolder
        self._audit_logger = audit_logger
        # Loop (M0): when a run store is attached, node outputs and
        # measurements are recorded in the run directory.
        self._run_store = run_store
        self._run_id = run_id
        self._custom_checkers = custom_checkers
        self._plan_version = plan_version
        # M4: called before an irreversible node (reversible is False)
        # runs. Returns True when a human has approved the action; False
        # pauses the node at AWAITING_APPROVAL. None means no gate is
        # configured — an irreversible node then fails rather than running.
        self._gate_callback = gate_callback
        # Cycle 4: the repo root of the plan being executed, read from
        # the plan itself in run() (see _plan_repo_root). None until a
        # plan is loaded, and None for plans with no repo context.
        self._plan_repo_root: Path | None = None

    async def run(self, plan_id: str) -> ExecutionPlan:
        try:
            return await self._run_plan(plan_id)
        finally:
            self._sweep_detached_processes(plan_id)

    def _sweep_detached_processes(self, plan_id: str) -> None:
        """Reap process groups that outlived their bash node (cycle 15).

        A node may background a server so a later node can talk to it;
        those processes belong to the run, so the run ending — success,
        failure, or error — terminates them (the registry tracks the
        surviving members of each node shell's process group). A
        cleanup failure is logged, never raised: it must not mask the
        run's own outcome.
        """
        sweep = getattr(self._registry, "cleanup_detached_processes", None)
        if not callable(sweep):
            return
        try:
            swept = sweep()
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            logger.exception(
                "Detached-process cleanup failed for plan %s.", plan_id
            )
            return
        if swept:
            logger.info(
                "Plan %s: reaped %d detached process group(s) left by "
                "backgrounded node processes.",
                plan_id,
                swept,
            )

    async def _run_plan(self, plan_id: str) -> ExecutionPlan:
        plan = self._store.load(plan_id)
        if plan is None:
            raise ExecutorError(f"Plan {plan_id!r} not found in store.")
        self._plan_repo_root = _plan_repo_root(plan)

        logger.info("Executor starting plan %s (%d nodes).", plan_id, len(plan.nodes))
        plan.status = PlanStatus.RUNNING
        plan.touch()
        self._store.save(plan)

        while True:
            plan = self._load_or_raise(plan_id)

            if _is_terminal(plan.status):
                break

            runnable = _collect_runnable(plan)
            in_flight = _count_in_flight(plan)

            if not runnable and in_flight == 0:
                break

            if runnable:
                tasks = [
                    asyncio.create_task(
                        self._execute_node(plan_id, node),
                        name=f"node-{node.id}",
                    )
                    for node in runnable
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                continue

            if in_flight > 0:
                await asyncio.sleep(0.05)
                continue

        plan = self._load_or_raise(plan_id)
        final_status = _derive_plan_status(plan)
        if plan.status != final_status:
            plan.status = final_status
            plan.touch()
            self._store.save(plan)

        logger.info("Executor finished plan %s — status: %s.", plan_id, plan.status)
        return plan

    async def _execute_node(self, plan_id: str, node: PlanNode) -> None:
        async with self._semaphore:
            plan = self._load_or_raise(plan_id)
            target = _find_node(plan, node.id)
            if target is None or _is_terminal(target.status):
                return
            # M4: an irreversible node never runs without a human approval.
            if target.reversible is False:
                if self._gate_callback is None:
                    self._record_failure(
                        plan_id,
                        node.id,
                        "irreversible node requires an approval gate"
                        " (no gate callback configured).",
                    )
                    return
                if not self._gate_callback(target):
                    target.status = PlanStatus.AWAITING_APPROVAL
                    plan.touch()
                    self._store.save(plan)
                    return
            target.status = PlanStatus.RUNNING
            plan.touch()
            self._store.save(plan)

            capability = target.assigned_claw or node.id
            logger.debug("Dispatching node %s (capability=%r).", node.id, capability)
            started = time.monotonic()

            try:
                handler = self._registry.resolve(capability)

                # Resolve context piping placeholders (e.g. {{step_1.stdout}})
                completed_results = {
                    n.id: n.inputs
                    for n in plan.nodes
                    if n.status == PlanStatus.COMPLETED
                }
                resolved_inputs = _resolve_input_variables(dict(target.inputs), completed_results)
                # Loop (M0): handlers run in the node's run directory when a
                # run store is attached, with the node's hard timeout.
                workdir = self._node_workdir(node.id)
                resolved_inputs.setdefault("workdir", str(workdir))
                # M3: the run directory is shared by every node in the run,
                # so a dataset fetched by one node can be analysed by the
                # next (research artifacts, sandbox quarantine live here).
                if self._run_store is not None and self._run_id is not None:
                    resolved_inputs.setdefault(
                        "run_dir", str(self._run_store.run_dir(self._run_id))
                    )
                effective_timeout = _effective_node_timeout(target)
                if effective_timeout != target.timeout_s:
                    logger.info(
                        "Node %s runs the full test suite: timeout floor "
                        "%.0fs applies (node declares %.0fs).",
                        node.id,
                        effective_timeout,
                        target.timeout_s,
                    )
                resolved_inputs.setdefault("timeout_s", effective_timeout)

                # Build #21: resolve the command's grounded
                # interpreter against the plan's repo root (see
                # _resolve_grounded_interpreter). Only the resolved
                # copy the handler receives is rewritten — the plan's
                # own inputs keep the grounded command.
                if capability in ("bash", "shell_exec"):
                    for key in ("command", "cmd"):
                        value = resolved_inputs.get(key)
                        if isinstance(value, str):
                            resolved_command, substitutions = (
                                _resolve_grounded_interpreter(
                                    value, self._plan_repo_root
                                )
                            )
                            if substitutions:
                                logger.warning(
                                    "Node %s: grounded interpreter %s "
                                    "does not exist under %s; running "
                                    "with %s instead.",
                                    node.id,
                                    substitutions[0][0],
                                    self._plan_repo_root,
                                    substitutions[0][1],
                                )
                                resolved_inputs[key] = resolved_command

                try:
                    result = await asyncio.wait_for(
                        handler(resolved_inputs), timeout=effective_timeout
                    )
                except asyncio.TimeoutError:
                    duration_s = time.monotonic() - started
                    envelope = build_envelope(
                        node.id, None, duration_s=duration_s, status="timeout"
                    )
                    measurement = self._measure_and_record(target, envelope)
                    self._record_failure(
                        plan_id,
                        node.id,
                        f"Node timed out after {effective_timeout}s.",
                        measurement,
                    )
                    return
                duration_s = time.monotonic() - started

                if _is_dependency_missing(result):
                    dep_request: DependencyRequest = result[DEPENDENCY_MISSING_KEY]

                    if self._subdag_planner is None:
                        self._record_failure(
                            plan_id,
                            node.id,
                            "DependencyMissing returned but no subdag_planner is configured.",
                        )
                        return

                    count = self._injection_counts.get(node.id, 0)
                    if count >= MAX_INJECTION_ATTEMPTS:
                        self._record_failure(
                            plan_id,
                            node.id,
                            f"Injection loop guard triggered for node {node.id!r}.",
                        )
                        return

                    self._injection_counts[node.id] = count + 1
                    try:
                        sub_nodes = self._subdag_planner(dep_request)
                        plan = self._load_or_raise(plan_id)
                        plan = _inject_subdag(plan, node.id, sub_nodes)
                        plan.touch()
                        self._store.save(plan)
                    except Exception as planner_exc:
                        self._record_failure(
                            plan_id,
                            node.id,
                            f"sub-DAG planner raised an error: {planner_exc}",
                        )
                        return
                    return

                # Loop (M0): the handler returning is only an input to
                # measurement. The node is COMPLETED only when its
                # measurement passes; a non-zero exit code or a failed
                # criterion makes it FAILED with the reason attached.
                # A handler may report its own execution status (e.g. the
                # bash handler reports "timeout" when it kills the process
                # group). Only execution statuses count here — handlers also
                # return business statuses like "success", which mean the
                # handler completed normally.
                handler_status = "completed"
                if isinstance(result, dict) and result.get("status") in {
                    "timeout",
                    "killed",
                    "failed",
                }:
                    handler_status = result["status"]
                envelope = build_envelope(
                    node.id,
                    result if isinstance(result, dict) else {},
                    duration_s=duration_s,
                    status=handler_status,
                )
                measurement = self._measure_and_record(target, envelope)
                plan = self._load_or_raise(plan_id)
                target = _find_node(plan, node.id)
                if target is not None:
                    # The plan was reloaded after execution, so attach the
                    # measurement to this fresh node object (the pre-run
                    # object measured above is a different instance).
                    target.measurement = measurement
                    target.exit_code = envelope.get("exit_code")
                    target.duration_s = envelope.get("duration_s")
                    if isinstance(result, dict):
                        target.inputs.update(result)
                    if measurement["verdict"] == VERDICT_PASS:
                        target.status = PlanStatus.COMPLETED
                        target.error = None
                        logger.info("Node %s completed (measurement PASS).", node.id)
                    else:
                        target.status = PlanStatus.FAILED
                        target.error = measurement_failed_detail(measurement)
                        logger.info(
                            "Node %s failed measurement: %s", node.id, target.error
                        )
                    plan.touch()
                    self._store.save(plan)
            except UnknownCapabilityError as exc:
                if self._scaffolder is not None:
                    self._handle_missing_capability(plan_id, node.id, capability, exc)
                else:
                    self._record_failure(plan_id, node.id, str(exc))
            except Exception as exc:
                duration_s = time.monotonic() - started
                envelope = build_envelope(
                    node.id,
                    None,
                    duration_s=duration_s,
                    status="failed",
                    error={"kind": type(exc).__name__, "message": str(exc)},
                )
                measurement = self._measure_and_record(target, envelope)
                self._record_failure(plan_id, node.id, str(exc), measurement)

    def _handle_missing_capability(
        self,
        plan_id: str,
        node_id: str,
        capability: str,
        exc: UnknownCapabilityError,
    ) -> None:
        from .tool_scaffolder import CapabilityNameError

        assert self._scaffolder is not None
        try:
            proposal = self._scaffolder.propose(capability)
            error_msg = (
                f"Missing capability {capability!r}: draft scaffold proposed at {proposal.path!s}."
            )
            if self._audit_logger is not None:
                self._audit_logger.log(
                    "tool.scaffold.proposed",
                    capability=capability,
                    path=str(proposal.path),
                    node_id=node_id,
                    plan_id=plan_id,
                )
        except CapabilityNameError as val_exc:
            error_msg = f"Missing capability {capability!r} and scaffold rejected: {val_exc}"

        try:
            plan = self._load_or_raise(plan_id)
            node = _find_node(plan, node_id)
            if node is not None:
                node.status = PlanStatus.NEEDS_TOOL
                node.error = error_msg
                plan.touch()
                self._store.save(plan)
                if self._audit_logger is not None:
                    self._audit_logger.log(
                        "node.needs_tool",
                        capability=capability,
                        node_id=node_id,
                        plan_id=plan_id,
                    )
        except ExecutorError:
            logger.error("Could not record NEEDS_TOOL for node %s.", node_id)

    def _record_failure(
        self,
        plan_id: str,
        node_id: str,
        error: str,
        measurement: dict[str, Any] | None = None,
    ) -> None:
        try:
            plan = self._load_or_raise(plan_id)
            node = _find_node(plan, node_id)
            if node is not None:
                node.status = PlanStatus.FAILED
                node.error = error
                if measurement is not None:
                    node.measurement = measurement
                    node.exit_code = measurement.get("exit_code")
                    node.duration_s = measurement.get("duration_s")
                plan.touch()
                self._store.save(plan)
        except ExecutorError:
            logger.error("Could not record failure for node %s.", node_id)

    def _node_workdir(self, node_id: str) -> Path:
        """Working directory for a node: its run directory, else the cwd.

        Cycle 4: for a repo-grounded plan (see _plan_repo_root) the
        base is the plan's repo root instead. This one method feeds
        every relative reference at execution — the workdir input the
        bash claw uses as its cwd, the base the scaffold claw resolves
        relative targets against, and the base measure_node resolves
        relative criterion paths against — so all three move together
        onto the base the planner validated against. Commands that cd
        explicitly or use absolute paths are unaffected (the shell
        resolves those itself), a node-specified workdir input still
        wins over the injected one, and the separate run_dir input
        (artifact sharing) is unchanged.
        """
        if self._plan_repo_root is not None:
            return self._plan_repo_root
        if self._run_store is not None and self._run_id is not None:
            node_dir = self._run_store.node_dir(
                self._run_id, node_id, self._plan_version
            )
            node_dir.mkdir(parents=True, exist_ok=True)
            return node_dir
        return Path.cwd()

    def _node_with_resolved_criteria(self, node: PlanNode) -> PlanNode:
        """A copy of ``node`` with criterion commands interpreter-resolved.

        Build #21: an exit_code criterion may carry its own command,
        which measure_node runs in the node workdir — the plan-time
        normalization grounds those commands to ./.venv/bin/python
        too, so they hit the same fresh-checkout 127 as bash node
        commands. The copy exists only for this measurement; the
        plan's own node (and its persisted criteria) keep the
        grounded command byte-identical.
        """
        if self._plan_repo_root is None or not node.criteria:
            return node
        criteria: list[Any] = []
        changed = False
        for criterion in node.criteria:
            if isinstance(criterion, dict) and isinstance(
                criterion.get("command"), str
            ):
                resolved, substitutions = _resolve_grounded_interpreter(
                    criterion["command"], self._plan_repo_root
                )
                if substitutions:
                    criterion = {**criterion, "command": resolved}
                    changed = True
            criteria.append(criterion)
        if not changed:
            return node
        return node.model_copy(update={"criteria": criteria})

    def _measure_and_record(
        self, node: PlanNode, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Measure a finished node and persist the verdict (M0)."""
        workdir = self._node_workdir(node.id)
        measurement = measure_node(
            self._node_with_resolved_criteria(node),
            envelope,
            workdir,
            self._custom_checkers,
        )
        node.measurement = measurement
        node.exit_code = envelope.get("exit_code")
        node.duration_s = envelope.get("duration_s")
        # Stamp the plan version on the measurement so attempt history
        # stays attributable across re-plans.
        measurement["plan_version"] = self._plan_version
        if self._run_store is not None and self._run_id is not None:
            paths = self._run_store.write_node_output(
                self._run_id,
                node.id,
                stdout=envelope.get("stdout", ""),
                stderr=envelope.get("stderr", ""),
                version=self._plan_version,
            )
            envelope["stdout_path"] = paths["stdout_path"]
            envelope["stderr_path"] = paths["stderr_path"]
            self._run_store.append_measurement(self._run_id, measurement)
        if self._audit_logger is not None:
            self._audit_logger.log(
                "node.measured",
                node_id=node.id,
                verdict=measurement["verdict"],
                exit_code=envelope.get("exit_code"),
            )
        return measurement

    def _load_or_raise(self, plan_id: str) -> ExecutionPlan:
        plan = self._store.load(plan_id)
        if plan is None:
            raise ExecutorError(f"Plan {plan_id!r} disappeared from store mid-run.")
        return plan


def _resolve_input_variables(inputs: dict[str, Any], completed_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    resolved = {}
    for key, value in inputs.items():
        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
            target_ref = value[2:-2].strip()
            if "." in target_ref:
                step_id, field = target_ref.split(".", 1)
                parent_result = completed_results.get(step_id, {})
                resolved[key] = parent_result.get(field, value)
            else:
                resolved[key] = value
        else:
            resolved[key] = value
    return resolved


def _is_terminal(status: PlanStatus) -> bool:
    return status in {
        PlanStatus.COMPLETED,
        PlanStatus.FAILED,
        PlanStatus.CANCELLED,
        PlanStatus.NEEDS_TOOL,
    }


def _all_dependencies_met(node: PlanNode, node_map: dict[str, PlanNode]) -> bool:
    for dep_id in node.depends_on:
        dep = node_map.get(dep_id)
        if dep is None or dep.status != PlanStatus.COMPLETED:
            return False
    return True


def _collect_runnable(plan: ExecutionPlan) -> list[PlanNode]:
    node_map = plan.node_map()
    return [
        node
        for node in plan.nodes
        if node.status in {PlanStatus.DRAFT, PlanStatus.READY}
        and _all_dependencies_met(node, node_map)
    ]


def _count_in_flight(plan: ExecutionPlan) -> int:
    return sum(1 for node in plan.nodes if node.status == PlanStatus.RUNNING)


def _find_node(plan: ExecutionPlan, node_id: str) -> PlanNode | None:
    return plan.node_map().get(node_id)


def _derive_plan_status(plan: ExecutionPlan) -> PlanStatus:
    statuses = {node.status for node in plan.nodes}
    if PlanStatus.FAILED in statuses or PlanStatus.NEEDS_TOOL in statuses:
        return PlanStatus.FAILED
    if PlanStatus.CANCELLED in statuses:
        return PlanStatus.CANCELLED
    if PlanStatus.AWAITING_APPROVAL in statuses:
        return PlanStatus.AWAITING_APPROVAL
    if all(_is_terminal(s) for s in statuses):
        return PlanStatus.COMPLETED
    return PlanStatus.RUNNING


def _inject_subdag(
    plan: ExecutionPlan,
    orig_node_id: str,
    sub_nodes: list[PlanNode],
) -> ExecutionPlan:
    if not sub_nodes:
        orig = _find_node(plan, orig_node_id)
        if orig is not None:
            orig.status = PlanStatus.DRAFT
            orig.error = None
        return plan

    existing_ids = {n.id for n in plan.nodes}
    id_map: dict[str, str] = {}
    for k, sub_node in enumerate(sub_nodes):
        base = f"injected-{orig_node_id}-{k}"
        candidate = base
        suffix = 0
        while candidate in existing_ids:
            suffix += 1
            candidate = f"{base}-{suffix}"
        id_map[sub_node.id] = candidate
        existing_ids.add(candidate)

    remapped: list[PlanNode] = []
    for sub_node in sub_nodes:
        new_deps = [id_map.get(d, d) for d in sub_node.depends_on]
        remapped.append(
            PlanNode(
                id=id_map[sub_node.id],
                objective=sub_node.objective,
                depends_on=new_deps,
                assigned_claw=sub_node.assigned_claw,
                status=PlanStatus.DRAFT,
                inputs=dict(sub_node.inputs),
                outputs=list(sub_node.outputs),
                acceptance=list(sub_node.acceptance),
            )
        )

    all_sub_deps = {d for n in remapped for d in n.depends_on}
    terminal_ids = [n.id for n in remapped if n.id not in all_sub_deps]

    plan.nodes.extend(remapped)

    orig = _find_node(plan, orig_node_id)
    if orig is not None:
        for tid in terminal_ids:
            if tid not in orig.depends_on:
                orig.depends_on.append(tid)
        orig.status = PlanStatus.DRAFT
        orig.error = None

    return plan
