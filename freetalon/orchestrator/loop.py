"""The autonomous loop runner (design doc Stages 1-6).

M0: one pass — build the plan, execute it with measurement (via the
executor), record everything in the run directory, and evaluate the goal
contract. M1: the judge loop — after each executor pass the judge decides
CONTINUE / RETRY / REPLAN / ESCALATE from the measurements; re-plans are
delta plans from a structured brief, numbered and kept in the run
directory; the run ends honestly when the budget is spent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .executor import Executor
from .gates import GateStatus, GateStore, build_packet, compute_packet_hash
from .judge import Decision, Judge
from .measure import VERDICT_FAIL, VERDICT_PASS, VERDICT_UNMEASURABLE, _compare
from .memory import (
    ExperimentStore,
    approach_signature,
    describe_signature,
    goal_key_for,
)
from .models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from .planner import build_execution_plan, replan_from_brief
from .run_store import RunStore, new_run_id
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry

if True:  # TYPE_CHECKING-friendly import for audit logger
    from freetalon.audit import AuditLogger

# A re-plan function takes a brief dict and returns delta-plan nodes.
ReplanFn = Callable[[dict[str, Any]], list[PlanNode]]


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
    attempts: list[dict[str, Any]] = field(default_factory=list)
    escalation: str | None = None
    decision_history: list[dict[str, Any]] = field(default_factory=list)
    gate_packet_id: str | None = None


class LoopRunner:
    """Drives a goal contract through plan → run → measure → judge."""

    def __init__(
        self,
        store: ExecutionPlanStateStore,
        registry: ToolRegistry,
        *,
        run_store: RunStore | None = None,
        audit_logger: "AuditLogger | None" = None,
        custom_checkers: dict | None = None,
        max_concurrency: int = 8,
        memory_store: ExperimentStore | None = None,
        gate_store: GateStore | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._run_store = run_store or RunStore()
        self._audit_logger = audit_logger
        self._custom_checkers = custom_checkers
        self._max_concurrency = max_concurrency
        # M2: experiment memory shares the plan store's SQLite file, so the
        # experiments table sits next to execution_plans.
        if memory_store is not None:
            self._memory_store = memory_store
        else:
            db_path = getattr(store, "db_path", None)
            self._memory_store = (
                ExperimentStore(db_path) if db_path is not None else ExperimentStore()
            )
        # M4: gate packets share the same SQLite file (gates table).
        if gate_store is not None:
            self._gate_store = gate_store
        else:
            db_path = getattr(store, "db_path", None)
            self._gate_store = (
                GateStore(db_path, audit_logger=audit_logger)
                if db_path is not None
                else GateStore(audit_logger=audit_logger)
            )
        self._gate_packets: dict[str, str] = {}

    # ── Public entry points ──────────────────────────────────────────────

    def run_plan(
        self,
        plan: ExecutionPlan,
        contract: GoalContract,
        *,
        run_id: str | None = None,
        replan_fn: ReplanFn | None = None,
    ) -> RunResult:
        """Execute a plan under a goal contract, with judge + re-plan (M1)."""
        run_id = run_id or new_run_id()
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        judge = Judge(contract.budget)

        if len(plan.nodes) > contract.budget.max_nodes:
            raise ValueError(
                f"Plan has {len(plan.nodes)} nodes, over the contract budget "
                f"of {contract.budget.max_nodes}."
            )

        self._run_store.create_run(run_id, contract.model_dump(mode="json"))
        self._log("loop.run.started", run_id=run_id, plan_id=plan.plan_id)

        version = 1
        attempts: list[dict[str, Any]] = []
        decision_history: list[dict[str, Any]] = []
        escalation: str | None = None

        # M2: before executing, the planner's retrieval — a second run of
        # the same goal must not repeat a measured-failed approach. A node
        # that repeats one without a stated differs_by escalates before
        # anything runs; the earlier failure is cited, not repeated.
        goal_key = goal_key_for(contract.objective)
        domain = plan.intent.project_type
        repeat = self._find_repeated_approach(plan, goal_key, domain)
        skip_execution = False
        if repeat is not None:
            escalation = (
                "plan repeats a measured-failed approach for this goal: "
                f"{repeat}; the approach was already tried and failed "
                "(see experiment memory)"
            )
            self._log(
                "loop.memory.duplicate",
                run_id=run_id,
                plan_id=plan.plan_id,
                repeat=repeat,
            )
            skip_execution = True

        while not skip_execution:
            self._run_store.write_plan_version(run_id, version, plan)
            self._log(
                "loop.plan.version",
                run_id=run_id,
                plan_id=plan.plan_id,
                version=version,
            )
            before = {n.id: n.status for n in plan.nodes}
            executor = Executor(
                self._store,
                self._registry,
                max_concurrency=self._max_concurrency,
                run_store=self._run_store,
                run_id=run_id,
                custom_checkers=self._custom_checkers,
                audit_logger=self._audit_logger,
                plan_version=version,
                gate_callback=self._gate_callback_for(run_id, plan.plan_id),
            )
            plan = asyncio.run(executor.run(plan.plan_id))
            ran = [
                n
                for n in plan.nodes
                if before.get(n.id) != n.status
                and n.status in (PlanStatus.COMPLETED, PlanStatus.FAILED)
            ]
            judge.record_pass(ran)
            all_measurements = self._run_store.read_measurements(run_id)
            version_measurements = [
                m for m in all_measurements if m.get("plan_version", 1) == version
            ]
            progressed = judge.progress.observe(version_measurements)
            attempts.append(
                {
                    "version": version,
                    "status": plan.status.value,
                    "failed": [n.id for n in plan.nodes if n.status == PlanStatus.FAILED],
                    "verdicts": {
                        m.get("node_id"): m.get("verdict") for m in version_measurements
                    },
                }
            )

            # M4: a node paused at a gate ends this pass — the run waits
            # for a human decision, it does not push through.
            awaiting = [
                n for n in plan.nodes if n.status == PlanStatus.AWAITING_APPROVAL
            ]
            if awaiting:
                node = awaiting[0]
                packets = [
                    pkt
                    for pkt in self._gate_store.list_for_run(run_id)
                    if pkt.node_id == node.id
                ]
                if not packets:
                    # No packet was created: this action was already denied
                    # in this run. Never re-ask — fail honestly instead.
                    node.status = PlanStatus.FAILED
                    node.error = (
                        "approval already denied for this action in this run;"
                        " not re-asked"
                    )
                    self._store.save(plan)
                    escalation = (
                        f"gate denied previously for node {node.id}; not re-asked"
                    )
                    decision_history.append(
                        {"version": version, "decision": "escalate", "reason": escalation}
                    )
                    break
                packet = packets[-1]
                self._gate_packets[run_id] = packet.packet_id
                escalation = (
                    f"awaiting approval: gate packet {packet.packet_id}"
                    f" for node {node.id} ({packet.action})"
                )
                decision_history.append(
                    {
                        "version": version,
                        "decision": "awaiting_approval",
                        "reason": escalation,
                    }
                )
                self._log(
                    "loop.gate.awaiting",
                    run_id=run_id,
                    packet_id=packet.packet_id,
                    node_id=node.id,
                )
                break

            if plan.status == PlanStatus.COMPLETED:
                decision_history.append(
                    {"version": version, "decision": "continue", "reason": "all nodes succeeded"}
                )
                self._record_experiments(
                    run_id, goal_key, domain, ran, version_measurements, "CONTINUE"
                )
                break

            decision = judge.decide(plan, version_measurements)
            decision_history.append(
                {
                    "version": version,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                }
            )
            self._log(
                "loop.judge.decision",
                run_id=run_id,
                version=version,
                decision=decision.decision.value,
                reason=decision.reason,
            )
            # M2: lessons are written at judge time, from the measurements.
            self._record_experiments(
                run_id,
                goal_key,
                domain,
                ran,
                version_measurements,
                decision.decision.value,
            )

            if decision.decision == Decision.RETRY:
                self._reset_nodes(plan, decision.retry_node_ids)
                continue

            if decision.decision == Decision.REPLAN:
                judge.replans_used += 1
                judge.progress.note_replan(progressed)
                brief = judge.build_replan_brief(
                    contract,
                    plan,
                    version_measurements,
                    memory=self._memory_store.experiments_for_goal(goal_key, domain),
                    plan_version=version,
                )
                # Retry #20: the replan phase starts with a fresh forbidden
                # list, so an approach rejected in initial planning (Flask)
                # came back in the replan. Carry the initial planning's
                # rejected approaches through the plan metadata into the
                # replan brief; replan_from_brief seeds its repair prompts
                # from it and writes the accumulated list back.
                seed = plan.metadata.get("rejected_approaches") or []
                if seed:
                    brief["seed_forbidden"] = list(seed)
                replan = replan_fn or (lambda b: replan_from_brief(b))
                try:
                    new_nodes = replan(brief)
                except Exception as exc:  # noqa: BLE001 - re-planner failure escalates
                    escalation = f"re-planner failed: {exc}"
                    break
                rejection = judge.validate_delta_plan(
                    brief,
                    new_nodes,
                    known_failures=self._memory_store.failed_approaches(
                        goal_key, domain
                    ),
                )
                if rejection is not None:
                    escalation = f"delta plan rejected: {rejection}"
                    break
                version += 1
                metadata = dict(plan.metadata or {})
                if brief.get("rejected_approaches"):
                    metadata["rejected_approaches"] = list(
                        brief["rejected_approaches"]
                    )
                plan = build_execution_plan(
                    plan.intent, new_nodes, plan_id=plan.plan_id, metadata=metadata
                )
                self._store.save(plan)
                continue

            # ESCALATE (or any other terminal decision): end honestly.
            escalation = decision.reason
            break

        return self._finish_run(
            run_id,
            plan,
            contract,
            version=version,
            attempts=attempts,
            decision_history=decision_history,
            escalation=escalation,
            started=started,
            started_at=started_at,
        )

    def _finish_run(
        self,
        run_id: str,
        plan: ExecutionPlan,
        contract: GoalContract,
        *,
        version: int,
        attempts: list[dict[str, Any]],
        decision_history: list[dict[str, Any]],
        escalation: str | None,
        started: float,
        started_at: str,
    ) -> RunResult:
        """Evaluate the goal, write the summary, and build the result."""
        measurements = self._run_store.read_measurements(run_id)
        if not measurements:
            measurements = [n.measurement for n in plan.nodes if n.measurement]

        elapsed_s = time.monotonic() - started
        goal_verdict, goal_detail = self._evaluate_goal(
            contract, plan, measurements, elapsed_s, escalation
        )
        summary_path = str(
            self._write_summary(
                run_id,
                plan,
                contract,
                measurements,
                goal_verdict,
                goal_detail,
                started_at,
                elapsed_s,
                plan_versions=version,
                attempts=attempts,
                decision_history=decision_history,
                escalation=escalation,
            )
        )
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
            plan_versions=version,
            attempts=attempts,
            escalation=escalation,
            decision_history=decision_history,
            gate_packet_id=self._gate_packets.get(run_id),
        )

    # ── Approval gates (M4) ──────────────────────────────────────────────

    def _gate_evidence(self, run_id: str) -> list[str]:
        """Artifact paths and measurement refs supporting a gate packet."""
        evidence: list[str] = []
        run_dir = self._run_store.run_dir(run_id)
        for sub in ("research", "nodes", "sandbox", "gates"):
            folder = run_dir / sub
            if folder.is_dir():
                for found in sorted(folder.rglob("*")):
                    if found.is_file():
                        evidence.append(str(found.relative_to(run_dir)))
                        if len(evidence) >= 10:
                            return evidence
        for measurement in self._run_store.read_measurements(run_id):
            evidence.append(
                f"measurement:{measurement.get('node_id')}:{measurement.get('verdict')}"
            )
        return evidence[:10]

    def _gate_callback_for(self, run_id: str, plan_id: str):
        """Build the executor's gate callback for one run (M4).

        The callback creates a gate packet for an irreversible node and
        returns False, so the executor pauses the node at
        AWAITING_APPROVAL instead of running it. It returns False without
        creating a packet when the same action was already denied in this
        run, or when an identical pending packet already exists.
        """

        def _callback(node: PlanNode) -> bool:
            action = str(node.inputs.get("action") or node.assigned_claw or node.id)
            evidence = self._gate_evidence(run_id)
            packet_hash = compute_packet_hash(action, node.id, dict(node.inputs))
            if packet_hash in self._gate_store.denied_hashes_for_run(run_id):
                self._log(
                    "loop.gate.denied_before",
                    run_id=run_id,
                    node_id=node.id,
                    action=action,
                )
                return False
            for existing in self._gate_store.list_for_run(run_id):
                if existing.node_id == node.id and existing.packet_hash == packet_hash:
                    # The approval is bound to this exact action: an
                    # APPROVED packet with the same hash lets the node run;
                    # a PENDING one means it is already waiting.
                    if existing.status == GateStatus.APPROVED:
                        return True
                    if existing.status == GateStatus.PENDING:
                        return False
            packet = build_packet(
                run_id=run_id,
                plan_id=plan_id,
                node_id=node.id,
                action=action,
                what=node.objective,
                why=f"next step toward: {node.objective}",
                risk=str(node.inputs.get("risk") or "not stated — see evidence"),
                cost=float(node.inputs.get("cost", 0.0)),
                reversible=node.reversible,
                evidence=evidence,
                inputs=dict(node.inputs),
            )
            self._gate_store.create(packet)
            gates_dir = self._run_store.run_dir(run_id) / "gates"
            gates_dir.mkdir(parents=True, exist_ok=True)
            (gates_dir / f"{packet.packet_id}.json").write_text(
                packet.model_dump_json(indent=2), encoding="utf-8"
            )
            self._gate_packets[run_id] = packet.packet_id
            self._log(
                "loop.gate.created",
                run_id=run_id,
                packet_id=packet.packet_id,
                node_id=node.id,
                action=action,
            )
            return False

        return _callback

    def resume_run(
        self,
        run_id: str,
        plan_id: str,
        contract: GoalContract,
        *,
        replan_fn: ReplanFn | None = None,
    ) -> RunResult:
        """Resume a run paused at a gate, after a human decision (M4).

        Approved: the node returns to READY and the run continues.
        Denied or expired: the node fails honestly (deny on timeout),
        the denial is recorded in experiment memory, and the run ends
        without re-asking. A packet whose hash no longer matches the
        node is void — the action changed, so the approval does not apply.
        """
        plan = self._store.load(plan_id)
        if plan is None:
            raise ValueError(f"plan {plan_id!r} not found in store.")
        awaiting = [n for n in plan.nodes if n.status == PlanStatus.AWAITING_APPROVAL]
        if not awaiting:
            raise ValueError(f"plan {plan_id!r} has no node awaiting approval.")
        node = awaiting[0]
        packets = [
            pkt
            for pkt in self._gate_store.list_for_run(run_id)
            if pkt.node_id == node.id
        ]
        if not packets:
            raise ValueError(f"no gate packet for node {node.id!r} in run {run_id!r}.")
        packet = packets[-1]
        if packet.status == GateStatus.PENDING:
            raise ValueError(f"gate packet {packet.packet_id} is still pending.")
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        if packet.status == GateStatus.APPROVED:
            current = compute_packet_hash(packet.action, node.id, dict(node.inputs))
            if current != packet.packet_hash:
                node.status = PlanStatus.FAILED
                node.error = (
                    "gate packet no longer matches the node (action or inputs"
                    " changed); approval void"
                )
                self._store.save(plan)
                return self._finish_run(
                    run_id,
                    plan,
                    contract,
                    version=1,
                    attempts=[],
                    decision_history=[
                        {
                            "version": 1,
                            "decision": "escalate",
                            "reason": "gate packet hash mismatch — approval void",
                        }
                    ],
                    escalation="gate packet hash mismatch — approval void",
                    started=started,
                    started_at=started_at,
                )
            node.status = PlanStatus.READY
            node.error = None
            self._store.save(plan)
            self._log(
                "loop.gate.approved",
                run_id=run_id,
                packet_id=packet.packet_id,
                node_id=node.id,
            )
            return self.run_plan(plan, contract, run_id=run_id, replan_fn=replan_fn)
        # DENIED or EXPIRED: deny on timeout is a denial.
        reason = (
            f"gate denied by {packet.decided_by}"
            if packet.status == GateStatus.DENIED
            else "gate expired without a decision (deny on timeout)"
        )
        node.status = PlanStatus.FAILED
        node.error = reason
        node.measurement = {
            "node_id": node.id,
            "verdict": VERDICT_FAIL,
            "detail": reason,
            "plan_version": 1,
        }
        self._store.save(plan)
        self._record_experiments(
            run_id,
            goal_key_for(contract.objective),
            plan.intent.project_type,
            [node],
            [node.measurement],
            "ESCALATE",
        )
        self._log(
            "loop.gate.denied",
            run_id=run_id,
            packet_id=packet.packet_id,
            node_id=node.id,
            reason=reason,
        )
        return self._finish_run(
            run_id,
            plan,
            contract,
            version=1,
            attempts=[],
            decision_history=[
                {"version": 1, "decision": "escalate", "reason": reason}
            ],
            escalation=reason,
            started=started,
            started_at=started_at,
        )

    def run_goal(
        self,
        contract: GoalContract,
        intent: TaskIntent,
        nodes: list[PlanNode] | list[dict[str, Any]] | None = None,
        *,
        run_id: str | None = None,
        plan_id: str | None = None,
        replan_fn: ReplanFn | None = None,
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
        return self.run_plan(plan, contract, run_id=run_id, replan_fn=replan_fn)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _find_repeated_approach(
        self, plan: ExecutionPlan, goal_key: str, domain: str
    ) -> str | None:
        """A node that repeats a recorded failed approach for this goal.

        Returns a human description of the repeat, or None. Matching is
        by approach signature (memory.approach_signature): the node's
        mechanism and targets, never its objective/hypothesis wording —
        and only against approaches that were actually tried (executed
        and measured FAIL; see ExperimentStore.failed_approaches).
        Plumbing nodes (test runs, live checks, server starts) have no
        signature and can never be a repeat. A node with a stated
        ``differs_by`` is not a repeat — the change is acknowledged.
        """
        known = self._memory_store.failed_approaches(goal_key, domain)
        if not known:
            return None
        for node in plan.nodes:
            signature = approach_signature(node)
            if signature is not None and signature in known and not node.differs_by:
                return f"{describe_signature(signature)} (node {node.id})"
        return None

    def _record_experiments(
        self,
        run_id: str,
        goal_key: str,
        domain: str,
        ran_nodes: list[PlanNode],
        measurements: list[dict[str, Any]],
        decision_value: str,
    ) -> None:
        """Append one experiment record per measured node (M2).

        Written to the SQLite memory store (cross-run) and to the run's
        memory.jsonl (per-run record). A node that passed records CONTINUE;
        a failed node records the judge's decision for the pass.
        """
        by_id = {m.get("node_id"): m for m in measurements}
        for node in ran_nodes:
            measurement = by_id.get(node.id) or node.measurement or {}
            if not measurement:
                continue
            # Decisions are stored uppercase (CONTINUE/RETRY/REPLAN/
            # ESCALATE) to match the design doc's experiment schema; the
            # judge's enum values are lowercase.
            node_decision = (
                "CONTINUE"
                if measurement.get("verdict") == VERDICT_PASS
                else decision_value.upper()
            )
            record = self._memory_store.record(
                run_id=run_id,
                goal_key=goal_key,
                domain=domain,
                node=node,
                measurement=measurement,
                decision=node_decision,
            )
            self._run_store.append_memory(run_id, record)

    def _reset_nodes(self, plan: ExecutionPlan, node_ids: list[str]) -> None:
        """Return nodes to READY for a retry, unchanged (M1)."""
        for node in plan.nodes:
            if node.id in node_ids:
                node.status = PlanStatus.READY
                node.error = None
                node.measurement = {}
        plan.touch()
        self._store.save(plan)

    # ── Goal evaluation ──────────────────────────────────────────────────

    def _evaluate_goal(
        self,
        contract: GoalContract,
        plan: ExecutionPlan,
        measurements: list[dict[str, Any]],
        elapsed_s: float,
        escalation: str | None,
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
        if escalation is not None:
            return VERDICT_FAIL, f"escalated: {escalation}"
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
        attempts: list[dict[str, Any]],
        decision_history: list[dict[str, Any]],
        escalation: str | None,
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
            "## Attempts",
            "",
        ]
        for attempt in attempts:
            lines.append(
                f"- v{attempt['version']}: {attempt['status']} "
                f"(failed: {attempt['failed'] or 'none'}, "
                f"verdicts: {attempt['verdicts']})"
            )
        lines += ["", "## Judge decisions", ""]
        for entry in decision_history:
            lines.append(
                f"- v{entry['version']}: {entry['decision']} — {entry['reason']}"
            )
        if escalation:
            lines += ["", f"Escalation: {escalation}", ""]
        lines += ["", "## Nodes (final version)", ""]
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
                f"- `{measurement.get('node_id')}` (v{measurement.get('plan_version', 1)}): "
                f"{measurement.get('verdict')} "
                f"(exit {measurement.get('exit_code')}, "
                f"{measurement.get('duration_s')}s)"
            )
        lines.append("")
        return self._run_store.write_summary(run_id, "\n".join(lines))

    def _log(self, event: str, **fields: Any) -> None:
        if self._audit_logger is not None:
            self._audit_logger.log(event, **fields)
