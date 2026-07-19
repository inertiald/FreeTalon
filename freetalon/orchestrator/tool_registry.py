"""Tool registry mapping capability names to executable handlers.

Handlers can be:
- Synchronous Python callables (wrapped transparently in an executor thread).
- Asynchronous Python callables (awaited directly).
- Docker run command descriptors (executed via async subprocess).

Usage::

    registry = ToolRegistry()

    # Register a plain Python function
    @registry.register("lint")
    def run_lint(node_inputs: dict) -> dict:
        ...
        return {"exit_code": 0}

    # Register a Docker command handler
    registry.register_docker("browser", image="ghcr.io/freetalon/claw-browser:latest")

    # Resolve and invoke
    handler = registry.resolve("lint")
    result = await handler({"file": "main.py"})
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Coroutine
from typing import Any

# Type alias for an async handler: receives node inputs dict, returns result dict.
AsyncHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class UnknownCapabilityError(KeyError):
    """Raised when a capability has no registered handler."""

    def __init__(self, capability: str) -> None:
        super().__init__(f"No handler registered for capability: {capability!r}")
        self.capability = capability


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
    """Return an async handler that runs ``docker run`` with *image*.

    The handler passes node inputs as ``--env KEY=VALUE`` flags and returns
    stdout, stderr, and the exit code.

    Parameters
    ----------
    image:
        Docker image to run (e.g. ``"ghcr.io/freetalon/claw-browser:latest"``).
    extra_args:
        Additional ``docker run`` arguments inserted before the image name.
    timeout:
        Maximum seconds to wait for the container to finish.
    """
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


class ToolRegistry:
    """Registry mapping capability names to async handlers.

    Parameters
    ----------
    strict:
        When ``True`` (default), :meth:`resolve` raises
        :class:`UnknownCapabilityError` for unregistered capabilities.
        When ``False``, it returns a no-op handler that logs a warning.
    """

    def __init__(self, *, strict: bool = True) -> None:
        self._strict = strict
        self._handlers: dict[str, AsyncHandler] = {}

    # ── Registration ──────────────────────────────────────────────────────

    def register(
        self,
        capability: str,
        handler: Callable[..., Any] | None = None,
    ) -> Callable[..., Any]:
        """Register *handler* for *capability*.

        Can be used as a decorator or called directly::

            registry.register("lint", my_async_fn)

            @registry.register("lint")
            async def my_handler(inputs): ...

        If *handler* is a regular (sync) callable it is automatically
        wrapped to run in a thread-pool executor.

        Parameters
        ----------
        capability:
            Capability name string (case-sensitive).
        handler:
            Callable to register; if omitted the method returns a
            decorator.

        Returns
        -------
        The original callable (enabling decorator usage).
        """
        if handler is None:
            # Decorator mode: @registry.register("cap")
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
        """Register a Docker-run handler for *capability*.

        Parameters
        ----------
        capability:
            Capability name.
        image:
            Docker image to execute.
        extra_args:
            Additional ``docker run`` arguments (e.g. volume mounts).
        timeout:
            Container timeout in seconds.
        """
        self._handlers[capability] = _make_docker_handler(image, extra_args, timeout)

    # ── Resolution ────────────────────────────────────────────────────────

    def resolve(self, capability: str) -> AsyncHandler:
        """Return the async handler registered for *capability*.

        Raises
        ------
        UnknownCapabilityError
            If *capability* is not registered and ``strict=True``.
        """
        handler = self._handlers.get(capability)
        if handler is not None:
            return handler
        if self._strict:
            raise UnknownCapabilityError(capability)
        # Non-strict: return a no-op handler.
        return _make_noop_handler(capability)

    def capabilities(self) -> list[str]:
        """Return a sorted list of all registered capability names."""
        return sorted(self._handlers)

    def __contains__(self, capability: str) -> bool:
        return capability in self._handlers

    def __repr__(self) -> str:  # pragma: no cover
        caps = ", ".join(self.capabilities())
        return f"ToolRegistry(capabilities=[{caps}])"


def _make_noop_handler(capability: str) -> AsyncHandler:
    """Return a handler that does nothing and warns about the missing capability."""
    import warnings

    async def _noop(inputs: dict[str, Any]) -> dict[str, Any]:
        warnings.warn(
            f"No handler registered for capability {capability!r}; skipping.",
            stacklevel=2,
        )
        return {}

    _noop.__name__ = f"noop_handler[{capability}]"
    return _noop
