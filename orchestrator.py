#!/usr/bin/env python3
"""FreeTalon Claw Orchestrator — Docker-based sandboxed task execution.

Manages isolated Docker containers ('claws') that execute tasks in a
security-hardened environment.  Each container runs on an **internal**
bridge network with no route to the public internet.  The only peer
reachable from a claw is the LLM service container on the same bridge.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from queue import Empty, Queue

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.models.networks import Network

from resource_manager import ResourceManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NETWORK_NAME = "freetalon-claw-net"
TRUSTED_IMAGE = "trusted-python-base:1.0.0"

# Browser Claw image and its dedicated (internet-accessible) network.
BROWSER_CLAW_IMAGE = "freetalon-claw-browser:1.0.0"
BROWSER_CLAW_PORT = 8080
_BROWSER_NETWORK_NAME = "freetalon-browser-net"

# Upload network — non-internal bridge used by YouTube-upload claws.
_UPLOAD_NETWORK_NAME = "freetalon-upload-net"

_CONTAINER_LABEL_KEY = "freetalon.managed"
_TASK_LABEL_KEY = "freetalon.task_id"

# How long to wait for a browser claw HTTP server to become reachable.
_BROWSER_READY_TIMEOUT = 30  # seconds
_BROWSER_READY_INTERVAL = 0.5  # seconds between probes

# Timeout for a single HTTP command forwarded to a browser claw (seconds).
# Must be longer than _BROWSER_READY_TIMEOUT and Playwright's navigation
# timeout (30 s) so that slow page loads do not cause spurious failures.
_BROWSER_COMMAND_TIMEOUT = 65


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
        self._resource_mgr = ResourceManager()
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

    def _ensure_browser_network(self) -> Network:
        """Return the browser-accessible bridge network, creating it when absent.

        Unlike the internal claw network, this network allows outbound internet
        access so browser claws can navigate to external URLs.  It is kept
        separate from the default Docker bridge to make the intent explicit.
        """
        try:
            return self._client.networks.get(_BROWSER_NETWORK_NAME)
        except NotFound:
            return self._client.networks.create(
                _BROWSER_NETWORK_NAME,
                driver="bridge",
                # internal=False (default) — browser claws need internet access.
            )

    def _ensure_upload_network(self) -> Network:
        """Return the upload-accessible bridge network, creating it when absent.

        Used exclusively by YouTube-upload claws that need outbound internet
        access to reach the YouTube API.  The network is non-internal so
        containers attached to it can route to the public internet.
        """
        try:
            return self._client.networks.get(_UPLOAD_NETWORK_NAME)
        except NotFound:
            return self._client.networks.create(
                _UPLOAD_NETWORK_NAME,
                driver="bridge",
                # internal=False (default) — upload claws need internet access.
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
        limits = self._resource_mgr.limits_for("default")
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
            mem_limit=limits.mem_limit,
            cpu_period=limits.cpu_period,
            cpu_quota=limits.cpu_quota,
            read_only=limits.read_only,
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

    # ── Browser Claw ──────────────────────────────────────────────────────

    def spawn_browser_claw(
        self, task_id: str, screenshots_host_path: str
    ) -> str:
        """Start a headless-Chromium browser claw for *task_id*.

        The container runs the ``freetalon-claw-browser`` image and exposes
        an HTTP command server on ``BROWSER_CLAW_PORT``.  Screenshots are
        saved inside the container at ``/screenshots`` which is bind-mounted
        to *screenshots_host_path* on the host.

        Unlike standard claws, browser claws run on a non-internal bridge
        so they can reach external URLs.  They are still memory- and
        CPU-capped, and they do **not** get a read-only filesystem because
        they must write screenshots.

        Parameters
        ----------
        task_id:
            Unique identifier (used in the container name ``browser-claw-<id>``
            and in orchestrator bookkeeping).
        screenshots_host_path:
            Absolute path on the *host* that will be bind-mounted as
            ``/screenshots`` inside the container.  Created automatically if
            it does not exist.

        Returns
        -------
        str
            The short container ID assigned by Docker.

        Raises
        ------
        docker.errors.ImageNotFound
            If the *freetalon-claw-browser* image has not been built.
        docker.errors.APIError
            On any other Docker daemon error.
        RuntimeError
            If the browser claw HTTP server does not become reachable within
            ``_BROWSER_READY_TIMEOUT`` seconds.
        """
        host_path = Path(screenshots_host_path).resolve()
        host_path.mkdir(parents=True, exist_ok=True)

        container_name = f"browser-claw-{task_id}"
        browser_net = self._ensure_browser_network()
        limits = self._resource_mgr.limits_for("default")

        container: Container = self._client.containers.run(
            BROWSER_CLAW_IMAGE,
            name=container_name,
            network=browser_net.name,
            detach=True,
            labels={
                _CONTAINER_LABEL_KEY: "true",
                _TASK_LABEL_KEY: task_id,
            },
            mem_limit=limits.mem_limit,
            cpu_period=limits.cpu_period,
            cpu_quota=limits.cpu_quota,
            # read_only=False — browser claw must write screenshots to the volume.
            volumes={str(host_path): {"bind": "/screenshots", "mode": "rw"}},
            environment={
                "SCREENSHOTS_DIR": "/screenshots",
                "CLAW_PORT": str(BROWSER_CLAW_PORT),
            },
        )

        with self._lock:
            self._containers[task_id] = container

        thread = threading.Thread(
            target=self._stream_logs,
            args=(task_id, container),
            daemon=True,
            name=f"browser-claw-log-{task_id}",
        )
        thread.start()
        with self._lock:
            self._log_threads[task_id] = thread

        logger.info(
            "Spawned browser claw %s (container %s)", task_id, container.short_id
        )

        # Wait until the HTTP server inside the container is reachable.
        self._wait_for_browser_ready(task_id, container)

        return container.short_id

    def get_browser_claw_url(self, task_id: str) -> str | None:
        """Return the base HTTP URL for the browser claw command server.

        Returns *None* if *task_id* is not a known container or if the
        container's IP cannot be determined.
        """
        with self._lock:
            container = self._containers.get(task_id)
        if container is None:
            return None
        try:
            container.reload()
            nets = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            ip = nets.get(_BROWSER_NETWORK_NAME, {}).get("IPAddress", "")
            if not ip:
                return None
            return f"http://{ip}:{BROWSER_CLAW_PORT}"
        except (NotFound, APIError):
            return None

    def send_browser_command(self, task_id: str, cmd: dict) -> dict:
        """Send a JSON command to the browser claw and return the response.

        Parameters
        ----------
        task_id:
            The task ID of a running browser claw.
        cmd:
            A command dict, e.g. ``{"cmd": "navigate", "url": "https://…"}``.

        Returns
        -------
        dict
            The JSON response from the claw.  Always contains an ``"ok"`` key.

        Raises
        ------
        RuntimeError
            If the container URL cannot be determined or the HTTP request
            fails.
        """
        base_url = self.get_browser_claw_url(task_id)
        if base_url is None:
            raise RuntimeError(
                f"Browser claw {task_id!r} is not running or its IP is unknown."
            )
        body = json.dumps(cmd).encode()
        req = urllib.request.Request(
            f"{base_url}/command",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_BROWSER_COMMAND_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Command to browser claw {task_id!r} failed: {exc}"
            ) from exc

    def _wait_for_browser_ready(self, task_id: str, container: Container) -> None:
        """Block until the browser claw HTTP server responds to /health."""
        deadline = time.monotonic() + _BROWSER_READY_TIMEOUT
        base_url: str | None = None

        while time.monotonic() < deadline:
            if base_url is None:
                base_url = self.get_browser_claw_url(task_id)
            if base_url:
                try:
                    with urllib.request.urlopen(
                        f"{base_url}/health", timeout=2
                    ) as resp:
                        if resp.status == 200:
                            logger.info(
                                "Browser claw %s is ready at %s", task_id, base_url
                            )
                            return
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Browser claw %s not yet ready at %s: %s",
                        task_id,
                        base_url,
                        exc,
                    )
            time.sleep(_BROWSER_READY_INTERVAL)

        raise RuntimeError(
            f"Browser claw {task_id!r} did not become ready within "
            f"{_BROWSER_READY_TIMEOUT} s.  Check 'docker logs {container.name}'."
        )

    # ── Media Claw (video tasks) ──────────────────────────────────────────

    def spawn_media_claw(
        self, task_id: str, task_description: str, output_host_path: str
    ) -> str:
        """Start a resource-heavy container for video / media tasks.

        The container receives up to 8 GiB RAM and 400 % CPU (4 cores),
        clamped to the host's actual capacity by the :class:`ResourceManager`.
        The root filesystem is **not** read-only, and ``/workspace/output``
        is bind-mounted in read-write mode so that the claw can persist
        rendered media files back to the host.

        Parameters
        ----------
        task_id:
            Unique task identifier.
        task_description:
            Python source code to execute inside the container.
        output_host_path:
            Absolute path on the host that is bind-mounted as
            ``/workspace/output`` inside the container.

        Returns
        -------
        str
            The short container ID assigned by Docker.
        """
        limits = self._resource_mgr.limits_for("video")
        container_name = f"media-claw-{task_id}"

        host_path = _validate_host_path(output_host_path)
        host_path.mkdir(parents=True, exist_ok=True)

        container: Container = self._client.containers.run(
            TRUSTED_IMAGE,
            command=["python3", "-c", task_description],
            name=container_name,
            network=NETWORK_NAME,
            detach=True,
            labels={
                _CONTAINER_LABEL_KEY: "true",
                _TASK_LABEL_KEY: task_id,
            },
            mem_limit=limits.mem_limit,
            cpu_period=limits.cpu_period,
            cpu_quota=limits.cpu_quota,
            read_only=limits.read_only,  # False for video profile
            volumes={str(host_path): {"bind": "/workspace/output", "mode": "rw"}},
        )

        with self._lock:
            self._containers[task_id] = container

        thread = threading.Thread(
            target=self._stream_logs,
            args=(task_id, container),
            daemon=True,
            name=f"media-claw-log-{task_id}",
        )
        thread.start()
        with self._lock:
            self._log_threads[task_id] = thread

        logger.info(
            "Spawned media claw %s (container %s) — mem=%s cpu_quota=%d",
            task_id,
            container.short_id,
            limits.mem_limit,
            limits.cpu_quota,
        )
        return container.short_id

    # ── Upload Claw (YouTube upload tasks) ────────────────────────────────

    def spawn_upload_claw(
        self, task_id: str, task_description: str, output_host_path: str
    ) -> str:
        """Start an internet-enabled container for YouTube upload tasks.

        The container uses the ``youtube_upload`` resource profile which
        mirrors the *video* profile's compute budget but is attached to a
        **non-internal** bridge network (``freetalon-upload-net``) so that
        it can reach the YouTube API over the public internet.

        Parameters
        ----------
        task_id:
            Unique task identifier.
        task_description:
            Python source code to execute inside the container.
        output_host_path:
            Absolute path on the host that is bind-mounted as
            ``/workspace/output`` inside the container.

        Returns
        -------
        str
            The short container ID assigned by Docker.
        """
        limits = self._resource_mgr.limits_for("youtube_upload")
        container_name = f"upload-claw-{task_id}"
        upload_net = self._ensure_upload_network()

        host_path = _validate_host_path(output_host_path)
        host_path.mkdir(parents=True, exist_ok=True)

        container: Container = self._client.containers.run(
            TRUSTED_IMAGE,
            command=["python3", "-c", task_description],
            name=container_name,
            # Non-internal bridge — allows outbound internet access for
            # YouTube API uploads while still being isolated from the host
            # network namespace.
            network=upload_net.name,
            detach=True,
            labels={
                _CONTAINER_LABEL_KEY: "true",
                _TASK_LABEL_KEY: task_id,
            },
            mem_limit=limits.mem_limit,
            cpu_period=limits.cpu_period,
            cpu_quota=limits.cpu_quota,
            read_only=limits.read_only,  # False for upload profile
            volumes={str(host_path): {"bind": "/workspace/output", "mode": "rw"}},
        )

        with self._lock:
            self._containers[task_id] = container

        thread = threading.Thread(
            target=self._stream_logs,
            args=(task_id, container),
            daemon=True,
            name=f"upload-claw-log-{task_id}",
        )
        thread.start()
        with self._lock:
            self._log_threads[task_id] = thread

        logger.info(
            "Spawned upload claw %s (container %s) — network=%s mem=%s cpu_quota=%d",
            task_id,
            container.short_id,
            upload_net.name,
            limits.mem_limit,
            limits.cpu_quota,
        )
        return container.short_id

    # ── Resource introspection ────────────────────────────────────────────

    def resource_summary(self) -> dict[str, object]:
        """Return host resources and per-profile limits (JSON-friendly)."""
        return self._resource_mgr.summary()

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
        """Remove the internal bridge, browser, and upload networks (best-effort)."""
        for net_name in (NETWORK_NAME, _BROWSER_NETWORK_NAME, _UPLOAD_NETWORK_NAME):
            try:
                net = self._client.networks.get(net_name)
                net.remove()
            except (NotFound, APIError):
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_host_path(raw_path: str) -> Path:
    """Resolve *raw_path* and verify it stays within the workspace.

    Raises :class:`ValueError` if the resolved path escapes outside the
    user's home directory, preventing directory-traversal attacks.
    """
    if not raw_path or not raw_path.strip():
        raise ValueError("Path must not be empty.")
    resolved = Path(raw_path).resolve()
    # Ensure the resolved path lives under the user's home directory.
    # This is the broadest safe boundary — callers typically pass a
    # workspace sub-path (e.g. ~/freetalon-workspace/output).
    home = Path.home().resolve()
    if not str(resolved).startswith(str(home) + os.sep) and resolved != home:
        raise ValueError(
            f"Path {raw_path!r} resolves to {resolved} which is outside "
            f"the user's home directory ({home})."
        )
    return resolved


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
