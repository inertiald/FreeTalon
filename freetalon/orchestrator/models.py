"""Orchestrator data models for FreeTalon.

These schemas provide stable contracts between intake, planner, router,
executor, and persistence layers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanStatus(str, Enum):
    """Execution status for plans and plan nodes."""

    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_TOOL = "needs_tool"  # Terminal: missing capability; draft scaffold proposed for human review


class TaskIntent(BaseModel):
    """Normalized representation of a user request."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(..., min_length=1, description="Primary user outcome")
    project_type: str = Field(
        default="general",
        min_length=1,
        description="Classifier output, e.g. trading_system, web_app",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Required capabilities/tools to satisfy the request",
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="Budget/runtime/environment constraints",
    )
    missing_inputs: list[str] = Field(
        default_factory=list,
        description="Required fields still needed from the user",
    )


class PlanNode(BaseModel):
    """A single executable unit in a DAG plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Stable node identifier")
    objective: str = Field(..., min_length=1, description="What this node must deliver")
    depends_on: list[str] = Field(default_factory=list, description="Upstream node IDs")
    assigned_claw: str = Field(
        default="",
        description="Worker/agent assigned to execute this node",
    )
    status: PlanStatus = Field(default=PlanStatus.DRAFT)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(
        default_factory=list,
        description="Completion checks/tests for this node",
    )
    error: str | None = Field(default=None, description="Last error, if failed")


class ExecutionPlan(BaseModel):
    """Top-level orchestrator plan object."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(..., min_length=1)
    intent: TaskIntent
    nodes: list[PlanNode] = Field(default_factory=list)
    status: PlanStatus = Field(default=PlanStatus.DRAFT)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form orchestration metadata (owner, tags, etc.)",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        """Bump update timestamp after any mutation."""
        self.updated_at = datetime.now(UTC)

    def node_map(self) -> dict[str, PlanNode]:
        """Convenience map for fast node lookup by id."""
        return {node.id: node for node in self.nodes}
