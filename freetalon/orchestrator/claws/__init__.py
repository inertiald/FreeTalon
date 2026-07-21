"""Claw capability handlers for the FreeTalon orchestrator.

Each module in this package exposes a ``dict -> dict`` handler that can be
registered with :class:`~freetalon.orchestrator.tool_registry.ToolRegistry`.

- ``network`` — ADR 0000 Task 1.3: Netmiko-based switch configuration with a
  mandatory Human-In-The-Loop approval gate and deferred vendored dependency.
"""
