"""Orchestrator data models for FreeTalon.

These schemas provide stable contracts between intake, planner, router,
executor, and persistence layers.

Loop extensions (M0, 2026-11-14): testable nodes carry structured
``criteria`` evaluated by ``freetalon.orchestrator.measure`` after execution;
``GoalContract`` / ``RunBudget`` capture the design-doc Stage 1 contract
(objective, success measures, budgets, risk class, stop conditions).
A node is COMPLETED only when its handler finished AND its measurement
passed; the measurement record is stored on the node itself.
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
    AWAITING_APPROVAL = "awaiting_approval"  # Paused: a gate packet needs a human decision (M4)


class RiskClass(str, Enum):
    """What a run may touch. Sets which gates apply (design doc Stage 1)."""

    READ_ONLY = "read_only"
    SANDBOX_WRITE = "sandbox_write"
    BENCH_HARDWARE = "bench_hardware"
    PAPER_TRADING = "paper_trading"
    LIVE_SYSTEMS = "live_systems"


class RunBudget(BaseModel):
    """Conservative default budgets for one run (design doc Stage 1).

    Enforced by the loop runner and judge — never estimated by the planner.
    """

    model_config = ConfigDict(extra="forbid")

    max_wall_clock_s: float = Field(
        default=3600.0, gt=0, description="Max wall-clock seconds for the whole run"
    )
    max_attempts_per_hypothesis: int = Field(
        default=3, ge=1, description="Max attempts (retries + re-plans) per hypothesis"
    )
    max_replans: int = Field(default=3, ge=0, description="Max re-plans per run")
    max_nodes: int = Field(default=50, ge=1, description="Max nodes across all plan versions")
    max_money: float = Field(
        default=0.0, ge=0, description="Max real money at risk (0 = paper/dry-run only)"
    )


class SuccessMeasure(BaseModel):
    """One named, measurable outcome in a goal contract."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    metric: str = Field(..., min_length=1, description="Metric key in run measurements")
    op: str = Field(..., description='Comparison: "<", "<=", "==", ">=", ">"')
    value: float


class GoalContract(BaseModel):
    """Stage 1 goal contract: what 'done' looks like, before any planning.

    Written when the goal is accepted, never edited mid-run — changing the
    contract is a human act, done between runs.
    """

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(..., min_length=1, description="One sentence: what 'done' looks like")
    success_measures: list[SuccessMeasure] = Field(default_factory=list)
    budget: RunBudget = Field(default_factory=RunBudget)
    risk_class: RiskClass = Field(default=RiskClass.READ_ONLY)
    stop_conditions: list[str] = Field(
        default_factory=list, description="Requester-defined aborts"
    )
    irreversible_actions: list[str] = Field(
        default_factory=list,
        description="Named one-way actions in this goal; each needs a gate packet",
    )


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
    """A single executable unit in a DAG plan.

    A node is not schedulable as 'testable' until its ``criteria`` are
    present. ``acceptance`` keeps the legacy free-text checks for plans
    written before structured criteria existed; they are informational —
    only ``criteria`` are machine-evaluated.
    """

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
        description="Completion checks/tests for this node (legacy free text)",
    )
    error: str | None = Field(default=None, description="Last error, if failed")
    # --- Loop fields (M0) ---
    criteria: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured acceptance criteria, evaluated by measure.py",
    )
    reversible: bool = Field(
        default=True,
        description="False routes the node through the approval gate (M4)",
    )
    timeout_s: float = Field(default=300.0, gt=0, description="Hard per-node timeout")
    max_retries: int = Field(default=0, ge=0, description="Retries on transient failure")
    hypothesis: str = Field(
        default="",
        description="What this node tests; recorded in experiment memory",
    )
    responds_to: str = Field(
        default="",
        description="Failed node id this node answers (delta plans, M1)",
    )
    differs_by: str = Field(
        default="",
        description="What this node does differently from the failed attempt (M1)",
    )
    measurement: dict[str, Any] = Field(
        default_factory=dict,
        description="Last measurement record (verdict + per-criterion results)",
    )
    duration_s: float | None = Field(default=None, description="Last execution duration")
    exit_code: int | None = Field(default=None, description="Last reported exit code")


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
