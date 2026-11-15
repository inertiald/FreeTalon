"""Tool registry mapping capability names to executable handlers.

Handlers can be:
- Synchronous Python callables (wrapped transparently in an executor thread).
- Asynchronous Python callables (awaited directly).
- Docker run command descriptors (executed via async subprocess).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import warnings
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from .intake import call_llm

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


class ToolRegistry:
    def __init__(self, *, strict: bool = True, scaffolder: Any | None = None) -> None:
        self._strict = strict
        self._handlers: dict[str, AsyncHandler] = {}
        self._scaffolder = scaffolder

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
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")

            return {"status": "success", "path": str(target_path), "bytes_written": len(content)}

        @self.register("bash")
        @self.register("shell_exec")
        async def bash_handler(inputs: dict[str, Any]) -> dict[str, Any]:
            cmd = inputs.get("command") or inputs.get("cmd")
            if not cmd:
                raise ValueError("bash claw requires a 'command' input string.")

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
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

            summary = await asyncio.to_thread(call_llm, system_prompt, user_prompt)
            print(f"\n--- SYNTHESIS RESULT ---\n{summary}\n------------------------\n")
            return {"status": "success", "summary": summary}

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
