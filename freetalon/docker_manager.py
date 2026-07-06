"""freetalon.docker_manager — optional Docker integration layer.

Wraps the root-level ClawOrchestrator and ResourceManager into the hive
package.  All Docker calls are guarded so that the rest of the hive
remains functional when Docker is not installed or not running.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

# Optional docker SDK — graceful when unavailable
_docker: object = None
_NotFound: type = Exception
try:
    import docker as _docker
    from docker.errors import NotFound as _NotFound

    _DOCKER_SDK = True
except ImportError:
    _DOCKER_SDK = False

# Make root-level modules importable from inside the package
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from resource_manager import ResourceManager as _ResourceManager

    _RM_AVAILABLE = True
except ImportError:
    _RM_AVAILABLE = False

_NETWORK_NAME = "freetalon-claw-net"
_TRUSTED_IMAGE = "trusted-python-base:1.0.0"
_CONTAINER_LABEL = "freetalon.managed"
_TASK_LABEL = "freetalon.task_id"


class DockerManager:
    """Thin integration layer between the FreeTalon hive and Docker claws.

    Raises ``RuntimeError`` on construction if the docker SDK is not installed
    or the Docker daemon is unreachable.  Callers should catch this and surface
    a clear ``docker_claw`` task failure rather than crashing the hive.
    """

    def __init__(self) -> None:
        if not _DOCKER_SDK:
            raise RuntimeError(
                "docker SDK not installed; add docker==7.1.0 to requirements and rebuild."
            )
        try:
            self._client = _docker.from_env()
            self._client.ping()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Docker daemon unreachable: {exc}") from exc
        self._rm: Any = _ResourceManager() if _RM_AVAILABLE else None
        self._lock = threading.Lock()
        self._containers: dict[str, Any] = {}

    # ── Container lifecycle ──────────────────────────────────────────────

    def spawn_claw(self, task_id: str, code: str, profile: str = "default") -> str:
        """Spawn a sandboxed container and return its short container ID."""
        run_kwargs: dict[str, Any] = dict(
            name=f"claw-{task_id}",
            network=self._ensure_network(),
            detach=True,
            labels={_CONTAINER_LABEL: "true", _TASK_LABEL: task_id},
        )
        if self._rm is not None:
            limits = self._rm.limits_for(profile)
            run_kwargs.update(
                mem_limit=limits.mem_limit,
                cpu_period=limits.cpu_period,
                cpu_quota=limits.cpu_quota,
                read_only=limits.read_only,
            )
        container = self._client.containers.run(
            _TRUSTED_IMAGE,
            command=["python3", "-c", code],
            **run_kwargs,
        )
        with self._lock:
            self._containers[task_id] = container
        return container.short_id

    def claw_status(self, task_id: str) -> str:
        """Return the Docker status string for the claw container."""
        with self._lock:
            container = self._containers.get(task_id)
        if container is None:
            return "missing"
        container.reload()
        return container.status

    def collect_result(self, task_id: str) -> dict[str, Any]:
        """Collect exit code and log lines from a finished claw."""
        with self._lock:
            container = self._containers.get(task_id)
        if container is None:
            return {"exit_code": -1, "output": []}
        try:
            result = container.wait(timeout=5)
            exit_code = result.get("StatusCode", -1)
        except Exception:  # noqa: BLE001
            exit_code = -1
        raw = container.logs(stdout=True, stderr=True)
        output = raw.decode("utf-8", errors="replace").splitlines()
        return {"exit_code": exit_code, "output": output}

    def kill_claw(self, task_id: str) -> None:
        """Send SIGKILL to the claw container (idempotent)."""
        with self._lock:
            container = self._containers.get(task_id)
        if container is None:
            return
        try:
            container.kill()
        except Exception:  # noqa: BLE001
            pass

    def remove_claw(self, task_id: str) -> None:
        """Remove the claw container and clean up tracking state."""
        with self._lock:
            container = self._containers.pop(task_id, None)
        if container is None:
            return
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001
            pass

    def resources_summary(self) -> dict[str, Any]:
        """Return a JSON-friendly resource summary (does not require Docker)."""
        if self._rm is not None:
            return self._rm.summary()
        return {"docker_sdk": _DOCKER_SDK, "resource_manager": False}

    # ── Helpers ──────────────────────────────────────────────────────────

    def _ensure_network(self) -> str:
        try:
            self._client.networks.get(_NETWORK_NAME)
        except _NotFound:
            self._client.networks.create(_NETWORK_NAME, driver="bridge", internal=True)
        return _NETWORK_NAME


def resource_summary_safe() -> dict[str, Any]:
    """Return a resource summary without requiring Docker to be running."""
    if not _RM_AVAILABLE:
        return {"docker_sdk": _DOCKER_SDK, "resource_manager": False}
    try:
        return _ResourceManager().summary()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
