"""The autonomous loop runner (design doc Stages 1-6).

M0: one pass — build the plan, execute it with measurement (via the
executor), record everything in the run directory, and evaluate the goal
contract. M1 extends this with the judge and re-plan loop; the runner then
iterates until the goal is met, the budget is spent, or a gate/escalation
stops it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .executor import Executor
from .measure import VERDICT_FAIL, VERDICT_PASS, VERDICT_UNMEASURABLE, _compare
from .models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from .planner import build_execution_plan
from .run_store import RunStore, new_run_id
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry

if True:  # TYPE_CHECKING-friendly import for audit logger
    from freetalon.audit import AuditLogger


@dataclass
class RunResult:
    """Outcome of one loop run."""

    run_id: str
    plan: ExecutionPlan
    contract: GoalContract
    measurements: list[dict[str, Any]] = field(default_factory=list)
    goal_verdict: str = VERDICT_FAIL
    goal_detail: str = ""
    elapsed_s: float = 0.0
    summary_path: str = ""
    plan_versions: int = 1


class LoopRunner:
    """Drives a goal contract through plan → run → measure → (M1: judge)."""

    def __init__(
        self,
        store: ExecutionPlanStateStore,
        registry: ToolRegistry,
        *,
        run_store: RunStore | None = None,
        audit_logger: "AuditLogger | None" = None,
        custom_checkers: dict | None = None,
        max_concurrency: int = 8,
    ) -> None:
        self._store = store
        self._registry = registry
        self._run_store = run_store or RunStore()
        self._audit_logger = audit_logger
        self._custom_checkers = custom_checkers
        self._max_concurrency = max_concurrency

    # ── Public entry points ──────────────────────────────────────────────

    def run_plan(
        self, plan: ExecutionPlan, contract: GoalContract, *, run_id: str | None = None
    ) -> RunResult:
        """Execute an already-built plan under a goal contract, measured."""
        run_id = run_id or new_run_id()
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()

        if len(plan.nodes) > contract.budget.max_nodes:
            raise ValueError(
                f"Plan has {len(plan.nodes)} nodes, over the contract budget "
                f"of {contract.budget.max_nodes}."
            )

        self._run_store.create_run(run_id, contract.model_dump(mode="json"))
        self._run_store.write_plan_version(run_id, 1, plan)
        self._log("loop.run.started", run_id=run_id, plan_id=plan.plan_id)

        executor = Executor(
            self._store,
            self._registry,
            max_concurrency=self._max_concurrency,
            run_store=self._run_store,
            run_id=run_id,
            custom_checkers=self._custom_checkers,
            audit_logger=self._audit_logger,
        )
        plan = asyncio.run(executor.run(plan.plan_id))

        measurements = self._run_store.read_measurements(run_id)
        # Fall back to the measurements recorded on the nodes themselves
        # (e.g. when the executor ran without a run store).
        if not measurements:
            measurements = [n.measurement for n in plan.nodes if n.measurement]

        elapsed_s = time.monotonic() - started
        goal_verdict, goal_detail = self._evaluate_goal(contract, plan, measurements, elapsed_s)
        summary_path = str(self._write_summary(
            run_id, plan, contract, measurements, goal_verdict, goal_detail,
            started_at, elapsed_s, plan_versions=1,
        ))
        self._log(
            "loop.run.finished",
            run_id=run_id,
            plan_id=plan.plan_id,
            goal_verdict=goal_verdict,
            elapsed_s=round(elapsed_s, 3),
        )
        return RunResult(
            run_id=run_id,
            plan=plan,
            contract=contract,
            measurements=measurements,
            goal_verdict=goal_verdict,
            goal_detail=goal_detail,
            elapsed_s=elapsed_s,
            summary_path=summary_path,
            plan_versions=1,
        )

    def run_goal(
        self,
        contract: GoalContract,
        intent: TaskIntent,
        nodes: list[PlanNode] | list[dict[str, Any]] | None = None,
        *,
        run_id: str | None = None,
        plan_id: str | None = None,
    ) -> RunResult:
        """Build a plan for a goal (from explicit nodes) and run it.

        ``nodes`` must be supplied for now; the LLM planning path
        (``plan_task_intent``) is wired in when the loop re-plans (M1).
        """
        if nodes is None:
            raise ValueError(
                "run_goal requires explicit nodes in M0; LLM planning is wired in M1."
            )
        plan = build_execution_plan(intent, nodes, plan_id=plan_id)
        self._store.save(plan)
        return self.run_plan(plan, contract, run_id=run_id)

    # ── Goal evaluation ──────────────────────────────────────────────────

    def _evaluate_goal(
        self,
        contract: GoalContract,
        plan: ExecutionPlan,
        measurements: list[dict[str, Any]],
        elapsed_s: float,
    ) -> tuple[str, str]:
        """Evaluate the contract's success measures against run measurements.

        A run that passes every node but misses the contract's success
        measures is a goal failure — and evidence the criteria need
        strengthening, by a human, between runs.
        """
        if elapsed_s > contract.budget.max_wall_clock_s:
            return VERDICT_FAIL, (
                f"wall-clock budget exceeded: {elapsed_s:.1f}s > "
                f"{contract.budget.max_wall_clock_s}s"
            )
        if plan.status == PlanStatus.FAILED:
            failed = [n.id for n in plan.nodes if n.status == PlanStatus.FAILED]
            return VERDICT_FAIL, f"plan failed; failed nodes: {failed}"
        if plan.status == PlanStatus.NEEDS_TOOL:
            return VERDICT_FAIL, "plan needs a tool that does not exist yet"
        if plan.status == PlanStatus.AWAITING_APPROVAL:
            return VERDICT_UNMEASURABLE, "plan paused awaiting approval"
        if not contract.success_measures:
            return VERDICT_PASS, "plan completed; no success measures declared"

        merged: dict[str, Any] = {}
        for measurement in measurements:
            merged.update(measurement.get("metrics") or {})
        for measure in contract.success_measures:
            if measure.metric not in merged:
                return VERDICT_UNMEASURABLE, (
                    f"success measure {measure.name!r} unmeasurable: "
                    f"metric {measure.metric!r} not reported"
                )
            value = merged[measure.metric]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return VERDICT_FAIL, (
                    f"success measure {measure.name!r}: metric "
                    f"{measure.metric!r} is not numeric: {value!r}"
                )
            if not _compare(measure.op, float(value), float(measure.value)):
                return VERDICT_FAIL, (
                    f"success measure {measure.name!r}: {measure.metric} "
                    f"{value} {measure.op} {measure.value} is false"
                )
        return VERDICT_PASS, "all success measures met"

    # ── Summary ──────────────────────────────────────────────────────────

    def _write_summary(
        self,
        run_id: str,
        plan: ExecutionPlan,
        contract: GoalContract,
        measurements: list[dict[str, Any]],
        goal_verdict: str,
        goal_detail: str,
        started_at: str,
        elapsed_s: float,
        *,
        plan_versions: int,
    ) -> Path:
        lines = [
            f"# Run {run_id}",
            "",
            f"- Started: {started_at}",
            f"- Elapsed: {elapsed_s:.1f}s",
            f"- Goal: {contract.objective}",
            f"- Risk class: {contract.risk_class.value}",
            f"- Plan: {plan.plan_id} (status {plan.status.value}, "
            f"{plan_versions} version(s))",
            f"- Goal verdict: **{goal_verdict}** — {goal_detail}",
            "",
            "## Nodes",
            "",
        ]
        by_id = {m.get("node_id"): m for m in measurements}
        for node in plan.nodes:
            measurement = by_id.get(node.id, node.measurement or {})
            verdict = measurement.get("verdict", "—")
            lines.append(
                f"- `{node.id}` {node.objective} — {node.status.value} "
                f"(verdict {verdict})"
            )
            for result in measurement.get("results", []):
                mark = "✓" if result.get("passed") else (
                    "?" if not result.get("measured") else "✗"
                )
                lines.append(f"  - {mark} {result.get('detail')}")
        lines += ["", "## Measurements", ""]
        for measurement in measurements:
            lines.append(
                f"- `{measurement.get('node_id')}`: {measurement.get('verdict')} "
                f"(exit {measurement.get('exit_code')}, "
                f"{measurement.get('duration_s')}s)"
            )
        lines.append("")
        return self._run_store.write_summary(run_id, "\n".join(lines))

    def _log(self, event: str, **fields: Any) -> None:
        if self._audit_logger is not None:
            self._audit_logger.log(event, **fields)
