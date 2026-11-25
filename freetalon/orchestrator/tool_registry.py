"""Tool registry mapping capability names to executable handlers.

Handlers can be:
- Synchronous Python callables (wrapped transparently in an executor thread).
- Asynchronous Python callables (awaited directly).
- Docker run command descriptors (executed via async subprocess).
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import signal
import subprocess
import tempfile
import time
import warnings
from collections.abc import Callable, Coroutine
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from .intake import LLMSettings, call_llm

AsyncHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class UnknownCapabilityError(KeyError):
    """Raised when a capability has no registered handler."""

    def __init__(self, capability: str) -> None:
        super().__init__(f"No handler registered for capability: {capability!r}")
        self.capability = capability


CODE_FILE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c",
    ".cc", ".cpp", ".h", ".hpp", ".rb", ".sh", ".pl", ".php", ".swift",
    ".kt", ".scala", ".cs",
})


def is_code_path(path: str | Path) -> bool:
    """True when a path names a code file — by suffix."""
    return Path(path).suffix.lower() in CODE_FILE_SUFFIXES


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->", re.DOTALL)


def is_placeholder_content(content: str) -> bool:
    """True when file content is placeholder-only: comments and/or a docstring, no code.

    Retry #14: the v3 replan scaffolded freetalon/api.py with the single
    comment "# Placeholder for health endpoint logic." — 219 lines of the
    real module replaced by one comment, and the loop never noticed,
    because scaffold nodes reported no exit code. A comment-only or
    docstring-only body is not an implementation: it may only ever create
    a new file, never overwrite an existing code file. Block comments
    (/* ... */, <!-- ... -->) and line comments (#, //) are stripped
    first; what is left must be real code. A lone string literal is a
    docstring, not code.
    """
    text = _BLOCK_COMMENT_RE.sub("", content)
    remaining: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//")):
            continue
        remaining.append(stripped)
    if not remaining:
        return True
    joined = "\n".join(remaining).strip()
    try:
        return isinstance(ast.literal_eval(joined), str)
    except (ValueError, SyntaxError):
        return False


def _wrap_sync(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> AsyncHandler:
    """Return an async wrapper that runs *fn* in the default thread-pool executor."""

    async def _wrapper(inputs: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, inputs)

    _wrapper.__name__ = getattr(fn, "__name__", "sync_handler")
    return _wrapper


def _make_docker_handler(
    image: str,
    extra_args: list[str] | None = None,
    timeout: float = 300.0,
) -> AsyncHandler:
    """Return an async handler that runs ``docker run`` with *image*."""
    extra_args = extra_args or []

    async def _docker_handler(inputs: dict[str, Any]) -> dict[str, Any]:
        env_flags: list[str] = []
        for key, value in inputs.items():
            env_flags.extend(["--env", f"{key}={value}"])

        cmd = ["docker", "run", "--rm"] + extra_args + env_flags + [image]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(
                f"Docker handler for image {image!r} timed out after {timeout}s"
            )

        return {
            "exit_code": proc.returncode,
            "stdout": stdout_bytes.decode(errors="replace"),
            "stderr": stderr_bytes.decode(errors="replace"),
        }

    _docker_handler.__name__ = f"docker_handler[{image}]"
    return _docker_handler


# Canonical claw allowlist (planner hardening): every assigned_claw an
# LLM plan may use, including aliases. The planner rejects any other name
# so an invented claw ("script", "write_file", ...) fails at plan time,
# where the repair retry can fix it, instead of at execution time.
# Keep in sync with the register_* functions:
#   scaffold/file_writer, bash/shell_exec, llm_synthesis/summarize:
#       _register_default_claws below
#   research/web_search, fetch_dataset/data_fetch:
#       research.register_research_claws
#   sandbox_analyze: sandbox.register_sandbox_claws
#   backtest/train_model/paper_trade/invest/engage_kill_switch:
#       finance.register_finance_claws
#   can_capture/verify_backup/tune_surface/flash_ecu: ecu.register_ecu_claws
KNOWN_CLAW_NAMES: frozenset[str] = frozenset(
    {
        "scaffold",
        "file_writer",
        "bash",
        "shell_exec",
        "llm_synthesis",
        "summarize",
        "research",
        "web_search",
        "fetch_dataset",
        "data_fetch",
        "sandbox_analyze",
        "backtest",
        "train_model",
        "paper_trade",
        "invest",
        "engage_kill_switch",
        "can_capture",
        "verify_backup",
        "tune_surface",
        "flash_ecu",
    }
)


def is_known_claw(name: str) -> bool:
    """True if ``name`` is a registered claw the planner may assign."""
    return name in KNOWN_CLAW_NAMES


# Metrics each claw's handler reports in its result envelope ("metrics"
# dict), keyed by claw name including aliases. The planner rejects a
# metric criterion naming a metric the node's claw cannot report, so a
# plan fails at plan time instead of measuring "not reported" at
# execution time (the failed /health run put test_count on a bash node).
# Keep in sync with the handlers:
#   llm_synthesis: summary_chars (tool_registry._register_default_claws)
#   research: source_count, fetched_count; fetch_dataset: bytes
#   sandbox_analyze: size_bytes, strings_count
#   backtest: total_return_pct, max_drawdown_pct, trade_count, win_rate,
#       max_position_pct; train_model: r2, mae
#   paper_trade: final_equity, trade_count, max_position_pct
#   can_capture: frames, unique_ids; verify_backup: backup_verified
#   tune_surface: maps_found, risk_terms
#   scaffold, bash, invest, engage_kill_switch, flash_ecu: no metrics —
#       invest and flash_ecu are refusing stubs, the others return none.
CLA_METRICS: dict[str, frozenset[str]] = {
    "scaffold": frozenset(),
    "file_writer": frozenset(),
    "bash": frozenset(),
    "shell_exec": frozenset(),
    "llm_synthesis": frozenset({"summary_chars"}),
    "summarize": frozenset({"summary_chars"}),
    "research": frozenset({"source_count", "fetched_count"}),
    "web_search": frozenset({"source_count", "fetched_count"}),
    "fetch_dataset": frozenset({"bytes"}),
    "data_fetch": frozenset({"bytes"}),
    "sandbox_analyze": frozenset({"size_bytes", "strings_count"}),
    "backtest": frozenset(
        {
            "total_return_pct",
            "max_drawdown_pct",
            "trade_count",
            "win_rate",
            "max_position_pct",
        }
    ),
    "train_model": frozenset({"r2", "mae"}),
    "paper_trade": frozenset({"final_equity", "trade_count", "max_position_pct"}),
    "invest": frozenset(),
    "engage_kill_switch": frozenset(),
    "can_capture": frozenset({"frames", "unique_ids"}),
    "verify_backup": frozenset({"backup_verified"}),
    "tune_surface": frozenset({"maps_found", "risk_terms"}),
    "flash_ecu": frozenset(),
}


def metrics_reported_by(claw: str) -> frozenset[str]:
    """Metric names the named claw's handler reports (empty if none)."""
    return CLA_METRICS.get(claw, frozenset())


def _process_group_members(pgid: int) -> dict[int, int]:
    """Live members of a process group: {pid: starttime} read from /proc.

    The starttime (field 22 of /proc/<pid>/stat) identifies a process
    incarnation: a pid recycled by an unrelated process never matches the
    starttime recorded for the original, which is what makes the
    detached-group sweep below safe against pid/group-id reuse.
    """
    members: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            # comm (field 2) may contain spaces/parens; fields after the
            # last ')' start at field 3, so field N is rest[N - 3]:
            # pgrp is field 5 (rest[2]), starttime field 22 (rest[19]).
            rest = stat.rsplit(")", 1)[1].split()
            if int(rest[2]) == pgid:
                members[int(entry)] = int(rest[19])
        except (OSError, ValueError, IndexError):
            continue
    return members


class _DetachedProcessGroups:
    """Process groups that outlived the bash node that spawned them.

    Cycle 15: a bash node runs in its own session
    (``start_new_session=True``), so its process-group id is the shell's
    pid. A process the node backgrounds (``server &``) stays in that
    group after the shell exits — reparented to init, group id unchanged
    (verified against /proc in the cycle-15 reproduction). The bash
    handler records groups with surviving members here; the executor
    sweeps them when the run ends, so a server a plan started for a
    later node to talk to never leaks past the run.

    A process that daemonizes into its OWN session (``setsid``, or
    ``freetalon.cli start``'s Popen) has deliberately left the group and
    is NOT tracked: its lifecycle belongs to the plan/app that detached
    it (e.g. ``freetalon.cli stop``), exactly as in an interactive shell.
    """

    def __init__(self) -> None:
        self._groups: dict[int, dict[int, int]] = {}

    def record(self, pgid: int) -> None:
        """Snapshot the surviving members of *pgid* (called at node end)."""
        members = _process_group_members(pgid)
        # Never track a group this process belongs to: in the
        # pathological case where a dead group leader's id was recycled
        # as a node shell's pid, the executor's own group could carry
        # the same number, and sweeping it would signal ourselves.
        if members and os.getpid() not in members:
            self._groups.setdefault(pgid, {}).update(members)

    def pending(self) -> dict[int, dict[int, int]]:
        """The recorded groups: {pgid: {pid: starttime}} (a copy)."""
        return {pgid: dict(members) for pgid, members in self._groups.items()}

    def sweep(self, *, grace_s: float = 2.0) -> int:
        """Terminate every recorded group; return the count swept.

        A group is signalled only while at least one recorded member —
        matched by pid AND starttime, so a recycled pid never counts —
        is verified live in it immediately beforehand. SIGTERM first,
        then SIGKILL for whatever is still alive after the grace.
        """
        groups, self._groups = self._groups, {}
        swept = 0
        for pgid, recorded in groups.items():
            live = self._verified_live(pgid, recorded)
            if not live:
                continue
            swept += 1
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                continue
            deadline = time.monotonic() + grace_s
            while time.monotonic() < deadline:
                if not self._verified_live(pgid, recorded):
                    break
                time.sleep(0.05)
            if self._verified_live(pgid, recorded):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
        return swept

    @staticmethod
    def _verified_live(pgid: int, recorded: dict[int, int]) -> dict[int, int]:
        current = _process_group_members(pgid)
        return {
            pid: start
            for pid, start in current.items()
            if recorded.get(pid) == start
        }


class ToolRegistry:
    def __init__(self, *, strict: bool = True, scaffolder: Any | None = None) -> None:
        self._strict = strict
        self._handlers: dict[str, AsyncHandler] = {}
        self._scaffolder = scaffolder
        # Cycle 15: process groups backgrounded by bash nodes and still
        # alive at node end; swept by the executor when the run ends.
        self._detached_groups = _DetachedProcessGroups()

        # Default claws are NOT auto-registered: callers that want them
        # (bootstrap.py) call register_default_claws(registry) explicitly.
        # An empty registry must stay empty so unknown capabilities fail
        # loudly instead of silently running a default or noop handler.

    def register(
        self,
        capability: str,
        handler: Callable[..., Any] | None = None,
    ) -> Callable[..., Any]:
        """Register *handler* for *capability*."""
        if handler is None:
            def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                self.register(capability, fn)
                return fn

            return _decorator

        if asyncio.iscoroutinefunction(handler):
            self._handlers[capability] = handler  # type: ignore[assignment]
        else:
            self._handlers[capability] = _wrap_sync(handler)  # type: ignore[arg-type]
        return handler

    def register_docker(
        self,
        capability: str,
        image: str,
        extra_args: list[str] | None = None,
        timeout: float = 300.0,
    ) -> None:
        """Register a Docker-run handler for *capability*."""
        self._handlers[capability] = _make_docker_handler(image, extra_args, timeout)

    def _register_default_claws(self) -> None:
        """Register essential built-in capabilities."""

        @self.register("scaffold")
        @self.register("file_writer")
        async def scaffold_handler(inputs: dict[str, Any]) -> dict[str, Any]:
            path_str = (
                inputs.get("file_path")
                or inputs.get("path")
                or (inputs.get("outputs") and inputs["outputs"][0])
            )
            content = inputs.get("content") or inputs.get("file_content") or ""

            if not path_str:
                raise ValueError("scaffold claw requires 'file_path' or 'path' in inputs.")

            target_path = Path(path_str).expanduser()
            workdir = inputs.get("workdir")
            if workdir and not target_path.is_absolute():
                # Loop (M0): relative paths resolve under the node's run dir.
                target_path = Path(workdir) / target_path
            # Retry #14: the v3 replan scaffolded freetalon/api.py with a
            # one-line placeholder comment (219 lines -> 1 line) and the
            # loop never noticed, because scaffold nodes reported no exit
            # code. A placeholder may only ever create a new file — it must
            # never truncate an existing code file. Refuse loudly so the
            # executor records a measured FAIL instead of a silent success.
            if (
                isinstance(content, str)
                and target_path.exists()
                and is_code_path(target_path)
                and is_placeholder_content(content)
            ):
                raise ValueError(
                    f"scaffold claw refuses to overwrite existing code file "
                    f"{target_path} with placeholder content (comments and/or "
                    "a docstring, no code) — write real code instead."
                )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")

            # Retry #14: scaffold nodes were UNMEASURABLE — the handler
            # returned no exit code, so an exit_code criterion could only
            # ever report "no exit code reported" and the loop escalated
            # on "unmeasurable twice in a row". A successful write is
            # exit 0; a failed write raises and the executor records a
            # measured FAIL.
            return {
                "status": "success",
                "path": str(target_path),
                "bytes_written": len(content),
                "exit_code": 0,
            }

        @self.register("bash")
        @self.register("shell_exec")
        async def bash_handler(inputs: dict[str, Any]) -> dict[str, Any]:
            cmd = inputs.get("command") or inputs.get("cmd")
            if not cmd:
                raise ValueError("bash claw requires a 'command' input string.")

            # Loop (M0): hard timeout with process-group kill, and an
            # optional workdir (the node's run directory). The process runs
            # in its own session so a timeout kills the whole group, not
            # just the shell.
            #
            # Cycle 15: output goes to temp files, NOT pipes. With pipes,
            # communicate() returns only at pipe EOF, and EOF waits on
            # EVERY process that inherited the pipe write ends — a node
            # that backgrounds a server (`python -m http.server &`, the
            # natural start-a-server step) could never complete: the
            # server held the pipes open, the node burned its whole
            # timeout, and the timeout group-kill then destroyed the
            # server it had started (cycle-15 reproduction,
            # logs/cycle15-repro.out: server alive and answering at the
            # probe instant, node still FAILED as a timeout). With
            # files, node completion depends only on the shell's own
            # exit; a backgrounded process keeps running for later
            # nodes, and its process group is recorded so the executor
            # can reap it when the run ends.
            timeout = float(inputs.get("timeout_s", 300))
            workdir = inputs.get("workdir")
            with (
                tempfile.TemporaryFile() as out_file,
                tempfile.TemporaryFile() as err_file,
            ):
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=out_file,
                    stderr=err_file,
                    cwd=workdir or None,
                    start_new_session=True,
                )
                timed_out = False
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    timed_out = True
                    _kill_process_group(proc)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except Exception:  # noqa: BLE001 - best effort reap
                        pass
                if not timed_out:
                    # The shell exited on its own; anything it
                    # backgrounded is still alive in its process group
                    # (the shell's pid). Record survivors so the
                    # executor reaps them at run end.
                    self._detached_groups.record(proc.pid)
                out_file.seek(0)
                err_file.seek(0)
                stdout = out_file.read()
                stderr = err_file.read()
            if timed_out:
                return {
                    "exit_code": None,
                    "status": "timeout",
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                }
            return {
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }

        @self.register("llm_synthesis")
        @self.register("summarize")
        async def llm_synthesis_handler(inputs: dict[str, Any]) -> dict[str, Any]:
            """Runs an LLM pass over input text or parent node outputs to synthesize findings."""
            context_data = inputs.get("context") or inputs.get("text") or inputs.get("raw_data") or ""
            instruction = inputs.get("instruction") or "Summarize the provided context clearly and concisely."

            if not context_data:
                return {"status": "skipped", "summary": "No context provided to synthesize."}

            system_prompt = "You are an analytical assistant. Process the provided context data according to the instructions."
            user_prompt = f"INSTRUCTION: {instruction}\n\nCONTEXT DATA:\n{context_data}"

            # Build #14: the summary is free-form prose used raw —
            # the call states unconstrained explicitly instead of
            # inheriting the transport's default JSON directive.
            synthesis_settings = dataclass_replace(
                LLMSettings.from_env(), format_mode="unconstrained"
            )
            summary = await asyncio.to_thread(
                call_llm, system_prompt, user_prompt, synthesis_settings
            )
            print(f"\n--- SYNTHESIS RESULT ---\n{summary}\n------------------------\n")
            # Report a measurable metric: a plan can assert summary_chars >= 1
            # to prove text was produced (content quality stays the LLM's job).
            return {
                "status": "success",
                "summary": summary,
                "metrics": {"summary_chars": len(summary)},
            }

    def cleanup_detached_processes(self) -> int:
        """Terminate process groups that outlived their bash node.

        Cycle 15: called by the executor when a run ends (success or
        failure). Returns how many groups still had live members and
        were terminated; 0 when nothing was backgrounded or everything
        already exited on its own.
        """
        return self._detached_groups.sweep()

    def resolve(self, capability: str, node_inputs: dict[str, Any] | None = None) -> AsyncHandler:
        """Return the handler for capability, scaffolding dynamically if missing."""
        handler = self._handlers.get(capability)
        if handler is not None:
            return handler

        if self._scaffolder is not None:
            try:
                proposal = self._scaffolder.propose(capability, node_inputs or {})
                if hasattr(proposal, "handler") and callable(proposal.handler):
                    self.register(capability, proposal.handler)
                    return self._handlers[capability]
            except Exception as exc:
                if self._strict:
                    raise
                warnings.warn(f"Failed to scaffold missing capability '{capability}': {exc}")

        if self._strict:
            raise UnknownCapabilityError(capability)

        return _make_noop_handler(capability)

    def capabilities(self) -> list[str]:
        """Return a sorted list of all registered capability names."""
        return sorted(self._handlers)

    def __contains__(self, capability: str) -> bool:
        return capability in self._handlers

    def __repr__(self) -> str:  # pragma: no cover
        caps = ", ".join(self.capabilities())
        return f"ToolRegistry(capabilities=[{caps}])"


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess's whole process group (best effort)."""
    import os
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - best effort
            pass


def register_default_claws(registry: ToolRegistry) -> None:
    """Module-level alias for registering default claws on a registry instance."""
    registry._register_default_claws()


def _make_noop_handler(capability: str) -> AsyncHandler:
    """Return a handler that does nothing and warns about the missing capability."""
    async def _noop(inputs: dict[str, Any]) -> dict[str, Any]:
        warnings.warn(
            f"No handler registered for capability {capability!r}; skipping.",
            stacklevel=2,
        )
        return {}

    _noop.__name__ = f"noop_handler[{capability}]"
    return _noop
