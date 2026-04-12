#!/usr/bin/env python3
"""FreeTalon Hive Manager — multi-agent orchestration TUI.

Launches N Docker 'claw' containers in parallel, each assigned a coding task,
and renders a live Rich dashboard showing per-claw status updates.

Usage::

    python hive_manager.py

Requirements: ``docker>=7.0.0``, ``rich>=13.0.0``  (see ``requirements.txt``)
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum

import docker
from docker.errors import APIError, DockerException, ImageNotFound
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Docker image used for every claw container.
CLAW_IMAGE: str = "python:3.12-slim"

#: Number of parallel claw containers to launch.
CLAW_COUNT: int = 5

#: Hard memory ceiling per container (Docker ``mem_limit`` format).
MEM_LIMIT: str = "2g"

#: Name of the internal Docker bridge network shared with the LLM-Host.
LLM_HOST_NETWORK: str = "llm-host"

#: Container name prefix — final name is ``{prefix}-{index}``.
CONTAINER_PREFIX: str = "freetalon-claw"

#: Seconds a claw container spends in the THINKING phase before EXECUTING.
THINKING_DURATION: float = 4.0

#: Seconds a claw container spends in the EXECUTING phase before completing.
EXECUTING_DURATION: float = 6.0

#: The five coding tasks distributed across claws.
CODING_TASKS: list[str] = [
    "Write a recursive Fibonacci function with memoization in Python.",
    "Implement a binary search algorithm and verify it with unit tests.",
    "Create a thread-safe LRU cache class using Python's collections module.",
    "Build a simple REST API health-check endpoint using Python's http.server.",
    "Write a CSV parser that handles quoted fields and newlines within fields.",
]

# Shell command executed inside each claw container.
# The STATUS: lines are parsed by the poller to drive phase transitions.
_CLAW_CMD_TEMPLATE = (
    "python3 -u -c \""
    "import time, sys; "
    "print('TASK: {task}', flush=True); "
    "print('STATUS: thinking', flush=True); "
    "time.sleep({think}); "
    "print('STATUS: executing', flush=True); "
    "time.sleep({execute}); "
    "print('STATUS: complete', flush=True)"
    "\""
)


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


class ClawPhase(str, Enum):
    """Ordered lifecycle phases for a single claw container."""

    PENDING = "Pending"
    LAUNCHING = "Launching"
    THINKING = "Thinking"
    EXECUTING = "Executing Test"
    COMPLETE = "Complete"
    FAILED = "Failed"


_PHASE_STYLE: dict[ClawPhase, str] = {
    ClawPhase.PENDING: "dim",
    ClawPhase.LAUNCHING: "bold yellow",
    ClawPhase.THINKING: "bold cyan",
    ClawPhase.EXECUTING: "bold blue",
    ClawPhase.COMPLETE: "bold green",
    ClawPhase.FAILED: "bold red",
}


@dataclass
class ClawState:
    """Thread-safe state record for one claw container."""

    index: int
    task: str
    phase: ClawPhase = ClawPhase.PENDING
    container_id: str = ""
    log_tail: str = ""
    _lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, repr=False
    )

    def update(
        self,
        phase: ClawPhase,
        container_id: str = "",
        log_tail: str = "",
    ) -> None:
        """Atomically update mutable fields."""
        with self._lock:
            self.phase = phase
            if container_id:
                self.container_id = container_id
            if log_tail:
                self.log_tail = log_tail


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


class HiveLogger:
    """High-level hive status logger that emits styled messages to a Console."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        """Print a timestamped hive-level status message."""
        ts = time.strftime("%H:%M:%S")
        line = (
            f"[dim]{ts}[/dim] "
            f"[bold bright_blue]▶ HIVE[/bold bright_blue] {message}"
        )
        with self._lock:
            self._console.log(line)


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def _phase_cell(phase: ClawPhase) -> Text:
    """Return a styled Rich Text object for a phase label."""
    return Text(phase.value, style=_PHASE_STYLE.get(phase, ""))


def build_status_table(states: list[ClawState]) -> Table:
    """Construct a Rich Table from the current snapshot of all claw states."""
    table = Table(
        title="[bold cyan]FreeTalon Hive — Claw Status[/bold cyan]",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )
    table.add_column("Claw", style="bold", justify="center", width=8)
    table.add_column("Status", justify="left", width=18)
    table.add_column("Task", justify="left")
    table.add_column("Container ID", style="dim", justify="left", width=16)

    for state in states:
        short_id = state.container_id[:12] if state.container_id else "—"
        table.add_row(
            f"Claw {state.index}",
            _phase_cell(state.phase),
            state.task,
            short_id,
        )
    return table


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def _ensure_network(client: docker.DockerClient, network_name: str) -> None:
    """Create the named Docker network if it does not already exist.

    The network is created as an *internal* bridge so containers can reach each
    other (and the LLM-Host container) but have no outbound access to the host
    network or the public internet.
    """
    existing = {n.name for n in client.networks.list()}
    if network_name not in existing:
        client.networks.create(
            name=network_name,
            driver="bridge",
            internal=True,  # no external / host internet access
            check_duplicate=True,
        )


def _pull_image_if_needed(
    client: docker.DockerClient,
    image: str,
    logger: HiveLogger,
) -> None:
    """Pull *image* if it is not already present in the local Docker cache."""
    try:
        client.images.get(image)
    except ImageNotFound:
        logger.log(
            f"Pulling image [bold]{image}[/bold] — this may take a moment…"
        )
        client.images.pull(image)
        logger.log(f"Image [bold]{image}[/bold] ready.")


def _remove_stale_container(client: docker.DockerClient, name: str) -> None:
    """Force-remove a container with *name* if it already exists."""
    try:
        old = client.containers.get(name)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass


# ---------------------------------------------------------------------------
# Claw lifecycle
# ---------------------------------------------------------------------------


def launch_claw(
    client: docker.DockerClient,
    state: ClawState,
    logger: HiveLogger,
    think_secs: float = THINKING_DURATION,
    exec_secs: float = EXECUTING_DURATION,
) -> None:
    """Launch a single claw container and poll it until completion.

    Phase transitions::

        PENDING → LAUNCHING → THINKING → EXECUTING → COMPLETE | FAILED

    The container is started with:

    * ``mem_limit`` / ``memswap_limit`` both set to :data:`MEM_LIMIT` (2 GB)
      to prevent memory run-away.
    * ``network=LLM_HOST_NETWORK`` — the internal bridge that gives access to
      the LLM-Host container but **no** host or internet access.
    * ``cpu_quota=100_000`` — limits each claw to one logical CPU core.
    """
    name = f"{CONTAINER_PREFIX}-{state.index}"

    # ── LAUNCHING ─────────────────────────────────────────────────────────
    state.update(ClawPhase.LAUNCHING)
    _remove_stale_container(client, name)

    cmd = _CLAW_CMD_TEMPLATE.format(
        task=state.task.replace('"', '\\"').replace("'", "\\'"),
        think=think_secs,
        execute=exec_secs,
    )

    try:
        container = client.containers.run(
            image=CLAW_IMAGE,
            command=["sh", "-c", cmd],
            name=name,
            detach=True,
            mem_limit=MEM_LIMIT,
            memswap_limit=MEM_LIMIT,  # swap == mem_limit → effectively no swap
            network=LLM_HOST_NETWORK,
            cpu_quota=100_000,  # 100 000 µs per 100 ms period == 1 vCPU
            remove=False,
            labels={"freetalon.claw": str(state.index)},
        )
    except (ImageNotFound, APIError) as exc:
        logger.log(f"[red]Claw {state.index} failed to launch:[/red] {exc}")
        state.update(ClawPhase.FAILED)
        return

    state.update(ClawPhase.THINKING, container_id=container.id)
    logger.log(
        f"Claw {state.index} container up — [dim]{container.id[:12]}[/dim]"
    )

    # ── Poll container until it exits ─────────────────────────────────────
    start = time.monotonic()
    while True:
        try:
            container.reload()
        except docker.errors.NotFound:
            state.update(ClawPhase.FAILED)
            logger.log(
                f"[red]Claw {state.index} container vanished unexpectedly.[/red]"
            )
            return

        elapsed = time.monotonic() - start
        status = container.status  # "created" | "running" | "exited" | ...

        if status == "running":
            if elapsed >= think_secs and state.phase == ClawPhase.THINKING:
                state.update(ClawPhase.EXECUTING)

        elif status == "exited":
            exit_code: int = container.attrs["State"]["ExitCode"]
            tail = (
                container.logs(tail=3)
                .decode("utf-8", errors="replace")
                .strip()
            )
            if exit_code == 0:
                state.update(ClawPhase.COMPLETE, log_tail=tail)
                logger.log(f"[green]Claw {state.index} complete.[/green]")
            else:
                state.update(ClawPhase.FAILED, log_tail=tail)
                logger.log(
                    f"[red]Claw {state.index} failed "
                    f"(exit {exit_code}):[/red] {tail}"
                )
            try:
                container.remove()
            except APIError:
                pass
            return

        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Orchestration entry-point
# ---------------------------------------------------------------------------


def run_hive(tasks: list[str] | None = None) -> None:
    """Main orchestration routine.

    Steps:

    1. Connect to the Docker daemon.
    2. Ensure the :data:`LLM_HOST_NETWORK` internal bridge network exists.
    3. Pull :data:`CLAW_IMAGE` if it is not cached locally.
    4. Launch all claws in parallel via a :class:`~concurrent.futures.ThreadPoolExecutor`.
    5. Render a live Rich dashboard until every claw has finished.
    6. Print a summary panel.
    """
    if tasks is None:
        tasks = CODING_TASKS

    console = Console()
    logger = HiveLogger(console)

    console.print(
        Panel.fit(
            "[bold cyan]FreeTalon Hive Manager[/bold cyan]\n"
            "Multi-agent claw orchestration — security hardened & "
            "network isolated.",
            border_style="bright_blue",
        )
    )

    # ── Docker client ─────────────────────────────────────────────────────
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as exc:
        console.print(
            f"[red]✖ Cannot connect to Docker daemon:[/red] {exc}"
        )
        raise SystemExit(1) from exc

    logger.log("Docker daemon reachable.")

    # ── Ensure LLM-Host network ───────────────────────────────────────────
    _ensure_network(client, LLM_HOST_NETWORK)
    logger.log(
        f"Network [bold]{LLM_HOST_NETWORK}[/bold] ready (internal bridge)."
    )

    # ── Pull image once ───────────────────────────────────────────────────
    _pull_image_if_needed(client, CLAW_IMAGE, logger)

    # ── Initialise claw states ────────────────────────────────────────────
    states: list[ClawState] = [
        ClawState(index=i, task=tasks[i]) for i in range(len(tasks))
    ]

    logger.log(f"Launching open claws 0–{len(states) - 1}…")

    # ── Live dashboard + parallel launcher ────────────────────────────────
    with Live(
        build_status_table(states),
        console=console,
        refresh_per_second=4,
        screen=False,
    ) as live:

        def _refresh() -> None:
            live.update(build_status_table(states))

        with ThreadPoolExecutor(max_workers=len(states)) as pool:
            futures = {
                pool.submit(launch_claw, client, s, logger): s
                for s in states
            }

            # Refresh the table while workers run.
            while not all(f.done() for f in futures):
                _refresh()
                time.sleep(0.25)

            # Collect any exceptions so they surface to the caller.
            for fut in as_completed(futures):
                fut.result()

        _refresh()

    # ── Summary ───────────────────────────────────────────────────────────
    complete = sum(1 for s in states if s.phase == ClawPhase.COMPLETE)
    failed = sum(1 for s in states if s.phase == ClawPhase.FAILED)
    console.print(
        Panel.fit(
            f"[bold green]Hive run complete.[/bold green]\n"
            f"  Completed: [green]{complete}[/green] / {len(states)}\n"
            f"  Failed:    [red]{failed}[/red] / {len(states)}",
            border_style="bright_green",
        )
    )


if __name__ == "__main__":
    run_hive()
