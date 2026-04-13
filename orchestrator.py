#!/usr/bin/env python3
"""FreeTalon Claw Orchestrator — Docker-based sandboxed task execution.

Manages isolated Docker containers ('claws') that execute tasks in a
security-hardened environment.  Each container runs on an **internal**
bridge network with no route to the public internet.  The only peer
reachable from a claw is the LLM service container on the same bridge.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from queue import Empty, Queue

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.models.networks import Network

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NETWORK_NAME = "freetalon-claw-net"
TRUSTED_IMAGE = "trusted-python-base"

_CONTAINER_LABEL_KEY = "freetalon.managed"
_TASK_LABEL_KEY = "freetalon.task_id"

# Resource caps — keep claws from starving the host.
_MEM_LIMIT = "512m"
_CPU_PERIOD = 100_000
_CPU_QUOTA = 50_000  # 50 % of one CPU core


# ---------------------------------------------------------------------------
# ClawOrchestrator
# ---------------------------------------------------------------------------


class ClawOrchestrator:
    """Manage sandboxed Docker containers for FreeTalon task execution.

    Each container ('claw') is launched on an *internal* Docker bridge
    network that can reach the local LLM container but has **no** route
    to the public internet.  The file-system is mounted read-only and
    memory / CPU usage is capped to prevent runaway processes.

    Usage::

        orch = ClawOrchestrator()
        cid  = orch.spawn_claw("abc123", "print('hello from the claw')")
        lines = orch.drain_logs("abc123")
        orch.kill_all()
    """

    # ── Initialisation ────────────────────────────────────────────────────

    def __init__(self) -> None:
        self._client: docker.DockerClient = docker.from_env()
        self._containers: dict[str, Container] = {}
        self._log_queues: dict[str, Queue[str]] = defaultdict(Queue)
        self._log_threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._network: Network = self._ensure_network()

    # ── Network management ────────────────────────────────────────────────

    def _ensure_network(self) -> Network:
        """Return the internal bridge network, creating it when absent.

        The network uses ``internal=True`` so containers attached to it
        can communicate with each other (e.g. claw → LLM) but **cannot**
        reach any external network.  This is the Docker-level equivalent
        of ``network_mode='none'`` with a private bridge exception.
        """
        try:
            return self._client.networks.get(NETWORK_NAME)
        except NotFound:
            return self._client.networks.create(
                NETWORK_NAME,
                driver="bridge",
                internal=True,
            )

    # ── Container lifecycle ───────────────────────────────────────────────

    def spawn_claw(self, task_id: str, task_description: str) -> str:
        """Start a sandboxed container for *task_id*.

        Parameters
        ----------
        task_id:
            Unique identifier for the task.  Used as part of the
            container name (``claw-<task_id>``).
        task_description:
            Python source code to execute inside the container.

        Returns
        -------
        str
            The short container ID assigned by Docker.

        Raises
        ------
        docker.errors.ImageNotFound
            If the *trusted-python-base* image has not been built.
        docker.errors.APIError
            On any other Docker daemon error.
        """
        container_name = f"claw-{task_id}"

        container: Container = self._client.containers.run(
            TRUSTED_IMAGE,
            command=["python3", "-c", task_description],
            name=container_name,
            # Attach *only* to the internal bridge — no default bridge,
            # no host networking.  Equivalent to ``network_mode='none'``
            # plus a single private bridge for the LLM container.
            network=NETWORK_NAME,
            detach=True,
            labels={
                _CONTAINER_LABEL_KEY: "true",
                _TASK_LABEL_KEY: task_id,
            },
            mem_limit=_MEM_LIMIT,
            cpu_period=_CPU_PERIOD,
            cpu_quota=_CPU_QUOTA,
            read_only=True,
        )

        with self._lock:
            self._containers[task_id] = container

        # Background thread to stream container stdout/stderr.
        thread = threading.Thread(
            target=self._stream_logs,
            args=(task_id, container),
            daemon=True,
            name=f"claw-log-{task_id}",
        )
        thread.start()
        with self._lock:
            self._log_threads[task_id] = thread

        logger.info("Spawned claw %s (container %s)", task_id, container.short_id)
        return container.short_id

    def stop_claw(self, task_id: str) -> bool:
        """Stop and remove a single container.  Returns *True* on success."""
        with self._lock:
            container = self._containers.pop(task_id, None)
            self._log_threads.pop(task_id, None)
            self._log_queues.pop(task_id, None)

        if container is None:
            return False

        _force_remove(container)
        logger.info("Stopped claw %s", task_id)
        return True

    def kill_all(self) -> int:
        """Kill and remove **all** tracked containers (the Kill Switch).

        Returns the number of containers that were successfully stopped.
        """
        with self._lock:
            snapshot = list(self._containers.items())
            self._containers.clear()
            self._log_threads.clear()
            self._log_queues.clear()

        stopped = 0
        for _task_id, container in snapshot:
            if _force_remove(container):
                stopped += 1

        logger.info("Kill switch activated — stopped %d container(s)", stopped)
        return stopped

    # ── Log streaming ─────────────────────────────────────────────────────

    def _stream_logs(self, task_id: str, container: Container) -> None:
        """Read container stdout/stderr and enqueue lines for the UI."""
        queue = self._log_queues[task_id]
        try:
            for chunk in container.logs(stream=True, follow=True):
                line = chunk.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    queue.put(line)
        except Exception as exc:  # noqa: BLE001 – catch-all for daemon errors
            queue.put(f"[log stream ended: {exc}]")

    def drain_logs(self, task_id: str, max_lines: int = 50) -> list[str]:
        """Return up to *max_lines* pending log lines for *task_id*."""
        queue = self._log_queues.get(task_id)
        if queue is None:
            return []
        lines: list[str] = []
        while len(lines) < max_lines:
            try:
                lines.append(queue.get_nowait())
            except Empty:
                break
        return lines

    # ── Introspection ─────────────────────────────────────────────────────

    def list_claws(self) -> list[dict[str, str]]:
        """Return a snapshot of every tracked container and its status."""
        result: list[dict[str, str]] = []
        stale: list[str] = []

        with self._lock:
            items = list(self._containers.items())

        for task_id, container in items:
            try:
                container.reload()
                result.append({
                    "task_id": task_id,
                    "status": container.status,
                    "id": container.short_id,
                    "name": container.name,
                })
            except (NotFound, APIError):
                result.append({
                    "task_id": task_id,
                    "status": "removed",
                    "id": "n/a",
                    "name": f"claw-{task_id}",
                })
                stale.append(task_id)

        if stale:
            with self._lock:
                for tid in stale:
                    self._containers.pop(tid, None)
                    self._log_threads.pop(tid, None)

        return result

    def cleanup_network(self) -> None:
        """Remove the internal bridge network (best-effort)."""
        try:
            net = self._client.networks.get(NETWORK_NAME)
            net.remove()
        except (NotFound, APIError):
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _force_remove(container: Container) -> bool:
    """Kill *and* remove a container, swallowing 'already gone' errors.

    Returns *True* when the container was successfully killed.
    """
    killed = False
    try:
        container.kill()
        killed = True
    except (NotFound, APIError):
        pass
    try:
        container.remove(force=True)
    except (NotFound, APIError):
        pass
    return killed
