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

from .contract_clauses import (
    PROBE_RESULTS_METADATA_KEY,
    WAIVERS_METADATA_KEY,
    clause_states,
    clauses_from_metadata,
    probe_failures,
    run_contract_probes,
)
from .critic import CriticSettings, critic_settings_from_env, review_write_nodes
from .executor import Executor
from .gates import GateStatus, GateStore, build_packet, compute_packet_hash
from .judge import (
    Decision,
    Judge,
    JudgeDecision,
    assess_approach_repeat,
    timeout_diagnostics,
)
from .measure import (
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_UNMEASURABLE,
    _compare,
)
from .memory import ExperimentStore, goal_key_for
from .models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from .planner import (
    build_execution_plan,
    drain_pending_planner_attempts,
    planner_attempt_sink,
    replan_from_brief,
    try_local_repair,
    try_scoped_edit_repair,
)
from .run_store import RunStore, new_run_id
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry

if True:  # TYPE_CHECKING-friendly import for audit logger
    from freetalon.audit import AuditLogger

# A re-plan function takes a brief dict and returns delta-plan nodes.
ReplanFn = Callable[[dict[str, Any]], list[PlanNode]]

# Campaign build #6: the failure classes whose ESCALATE the loop
# converts into a re-plan while re-plan budget remains. The set is
# deliberately a single class — conversion is for failures a
# different plan can plausibly fix:
#
# - timeout_retries_exhausted (CONVERTIBLE): a node hit its hard
#   timeout on every attempt. The timeout is evidence about the
#   tree the plan produced — arm #15's shipped server hung on POST
#   flows, so the plan's own test node timed out twice — which is
#   exactly what a re-plan briefed with the clause-probe failures
#   and the timeout diagnostics exists to repair. Escalating there
#   ended the run with all three re-plans unspent and the probes'
#   real findings unused.
#
# Every other class stays an escalation:
# - missing_tool: no plan can invent a capability mid-run.
# - wall_clock: the run's time budget is spent; a re-plan's
#   execution would run past it.
# - unmeasurable: the same node was unmeasurable twice — the plan
#   is underspecified in a way re-planning this goal has already
#   failed to fix (the judge's standing rule).
# - stalled: the progress rule fired — no criterion newly passed
#   across the stall limit of re-plans; more re-plans are the
#   failure mode that rule guards against.
# - replan_budget_spent: no budget remains, by definition (the
#   conversion also checks the budget directly).
# - plan_state: the plan ended in a non-failure terminal state
#   (e.g. cancelled); there is no failure evidence to brief.
_CONVERTIBLE_FAILURE_CLASSES = frozenset({"timeout_retries_exhausted"})


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
        critic_settings: CriticSettings | None = None,
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
        # Track B: the substance critic (default None = off; the
        # env-configured path is critic_settings_from_env, resolved
        # per run in run_plan).
        self._critic_settings = critic_settings

    # ── Public entry points ──────────────────────────────────────────────

    def run_plan(
        self,
        plan: ExecutionPlan,
        contract: GoalContract,
        *,
        run_id: str | None = None,
        replan_fn: ReplanFn | None = None,
    ) -> RunResult:
        """Execute a plan under a goal contract, with judge + re-plan (M1).

        Campaign build #9: for this run's duration the planner's
        attempt sink points at this run's store, so every planner
        attempt the run makes (the re-plan skeleton and content
        fill phases of the default re-plan path) persists to
        planner_attempts.jsonl in the run directory — phase,
        attempt number, validation outcome, and raw response
        text — next to the measurements and memory records. The
        sink is observability only: the planner guards sink
        failures, so persistence can never fail a run. An injected
        replan_fn makes no planner calls and records nothing.

        Campaign build #10: the run's plan was usually produced
        by an initial planning call that ran BEFORE this entry
        point installed any sink (the driver flow plans first,
        then runs), so its attempts — plan_skeleton and
        content_fill — never reached the jsonl (arm #19: the
        jsonl held only the run's 3 re-plan records). Those
        records wait in the planner's pending buffer; drain them
        into this run's sink first, so the run's trail opens
        with the planning that produced its plan.
        """
        run_id = run_id or new_run_id()

        def _attempt_sink(record: dict[str, Any]) -> None:
            self._run_store.append_planner_attempt(run_id, record)

        with planner_attempt_sink(_attempt_sink):
            drain_pending_planner_attempts(_attempt_sink)
            return self._run_plan(
                plan, contract, run_id=run_id, replan_fn=replan_fn
            )

    def _run_plan(
        self,
        plan: ExecutionPlan,
        contract: GoalContract,
        *,
        run_id: str | None = None,
        replan_fn: ReplanFn | None = None,
    ) -> RunResult:
        """run_plan's body (campaign build #9 split it out so the
        public entry can scope the planner attempt sink)."""
        run_id = run_id or new_run_id()
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        judge = Judge(contract.budget)
        # Track B: the substance critic for this run — injected
        # settings win; otherwise the env decides (the production
        # driver constructs the runner with no critic argument).
        # None means no critic: the loop below is exactly the
        # pre-critic loop.
        critic = (
            self._critic_settings
            if self._critic_settings is not None
            else critic_settings_from_env()
        )

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
        # that repeats one without a system repair note escalates before
        # anything runs; the earlier failure is cited, not repeated.
        # A signature match is adjudicated on substance first (see
        # _assess_plan_repeats): a match the substance check clears
        # proceeds, with the historical warning recorded in the plan
        # metadata and the audit log — never silently.
        goal_key = goal_key_for(contract.objective)
        domain = plan.intent.project_type
        repeat, repeat_warnings = self._assess_plan_repeats(
            plan, goal_key, domain
        )
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
        elif repeat_warnings:
            metadata = dict(plan.metadata or {})
            metadata["repeated_approach_warnings"] = list(repeat_warnings)
            plan.metadata = metadata
            # Persist now: the executor reloads the plan from the
            # state store, so a metadata change that lives only on
            # this in-memory copy would never reach the run record.
            self._store.save(plan)
            for warning in repeat_warnings:
                self._log(
                    "loop.memory.repeat_warning",
                    run_id=run_id,
                    plan_id=plan.plan_id,
                    warning=warning,
                )

        while not skip_execution:
            self._run_store.write_plan_version(run_id, version, plan)
            self._log(
                "loop.plan.version",
                run_id=run_id,
                plan_id=plan.plan_id,
                version=version,
            )
            before = {n.id: n.status for n in plan.nodes}
            # Track B (GAN critic): an accepted version's write
            # nodes are measured for substance before they
            # execute. A hollow verdict fails those nodes with
            # critic measurements (see _critic_review) and the
            # version does not execute — the judge below
            # consumes the failure exactly as an executed one.
            # No critic configured, no hollow verdict, or a
            # degraded critic all fall through to the executor
            # unchanged.
            if critic is not None and self._critic_review(
                plan, contract, critic, run_id=run_id, version=version
            ):
                plan.status = PlanStatus.FAILED
                plan.touch()
                self._store.save(plan)
            else:
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

            # Campaign build #3: the deterministic contract probes
            # run against the tree this version produced — outside
            # the model's self-written tests — and their results
            # are stamped into the plan metadata, where the judge's
            # brief and the verdict gate read them. A completed
            # plan with failing probes has NOT met the contract,
            # whatever its own nodes report.
            probe_failures_now = self._run_contract_probes(run_id, plan, version)

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

            if plan.status == PlanStatus.COMPLETED and not probe_failures_now:
                decision_history.append(
                    {"version": version, "decision": "continue", "reason": "all nodes succeeded"}
                )
                self._record_experiments(
                    run_id, goal_key, domain, ran, version_measurements, "CONTINUE"
                )
                break

            decision = judge.decide(plan, version_measurements)
            if decision.decision == Decision.CONTINUE and probe_failures_now:
                # The plan completed but the contract probes fail:
                # the judge's CONTINUE judges the plan's own nodes,
                # and the probes outrank them. While re-plan budget
                # remains this is a re-plan (its brief carries the
                # failing clauses); at budget end it is an honest
                # escalation, and the verdict gate reports the
                # clause accounting.
                failed_clause_ids = sorted(
                    {str(record["clause_id"]) for record in probe_failures_now}
                )
                if judge.remaining_budget()["replans"] > 0:
                    decision = JudgeDecision(
                        Decision.REPLAN,
                        "contract probes failing: " + ", ".join(failed_clause_ids),
                    )
                else:
                    decision = JudgeDecision(
                        Decision.ESCALATE,
                        "contract probes failing and re-plan budget "
                        "exhausted: " + ", ".join(failed_clause_ids),
                    )
            # Campaign build #6: escalation-to-re-plan conversion.
            # A convertible escalation (see
            # _CONVERTIBLE_FAILURE_CLASSES) with re-plan budget
            # remaining is not the end of the run. The conversion
            # routes through the ordinary REPLAN path below; the
            # brief is augmented there with the current probe
            # failures verbatim and the timeout diagnostics, and
            # the conversion is audit-logged here and stamped into
            # the new version's metadata — never silent.
            conversion: dict[str, Any] | None = None
            conversion_timeout_diagnostics: list[dict[str, Any]] = []
            if (
                decision.decision == Decision.ESCALATE
                and decision.failure_class in _CONVERTIBLE_FAILURE_CLASSES
                and judge.remaining_budget()["replans"] > 0
            ):
                conversion_timeout_diagnostics = timeout_diagnostics(
                    plan, judge
                )
                conversion = {
                    "failure_class": decision.failure_class,
                    "original_reason": decision.reason,
                    "from_version": version,
                    "timeout_nodes": [
                        entry["node_id"]
                        for entry in conversion_timeout_diagnostics
                    ],
                }
                self._log(
                    "loop.replan.escalation_converted",
                    run_id=run_id,
                    plan_id=plan.plan_id,
                    version=version,
                    failure_class=decision.failure_class,
                    reason=decision.reason,
                    timeout_nodes=conversion["timeout_nodes"],
                    failing_clauses=sorted(
                        {str(r["clause_id"]) for r in probe_failures_now}
                    ),
                )
                decision = JudgeDecision(
                    Decision.REPLAN,
                    "escalation converted to re-plan: "
                    + decision.reason
                    + " — re-plan budget remains and the failure is "
                    "one a re-plan can address; the brief carries "
                    "the clause-probe failures and the timeout "
                    "diagnostics",
                )
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
                # Contract pinning: the accepted plan's module
                # contract rides the same metadata -> brief channel
                # as seed_forbidden. The brief renders it as binding
                # and replan_from_brief passes it back into parse,
                # which validates the delta plan against the pin —
                # a re-plan can no longer re-invent the interface
                # it just failed against.
                pinned_contract = plan.metadata.get("module_contract")
                if pinned_contract:
                    brief["module_contract"] = pinned_contract
                # Campaign build #6: the current version's clause-
                # probe failures ride every re-plan brief verbatim
                # (clause id, probe kind, detail) — the judge's
                # uncovered-clause list embeds them as reasons, but
                # the raw records are the evidence a repair targets.
                if probe_failures_now:
                    brief["contract_probe_failures"] = [
                        dict(record) for record in probe_failures_now
                    ]
                # Campaign build #6: a converted escalation's brief
                # also carries the conversion record and the timeout
                # diagnostics — why this re-plan exists and the wall
                # the previous version hit.
                if conversion is not None:
                    brief["escalation_conversion"] = dict(conversion)
                    brief["timeout_diagnostics"] = (
                        conversion_timeout_diagnostics
                    )
                replan = replan_fn or (lambda b: replan_from_brief(b))
                new_nodes: list[PlanNode] | None = None
                # Campaign build #2: the repair seams record WHY
                # they declined into this list (one structured
                # record per declined attempt); the records are
                # audit-logged below and stamped into the new
                # version's metadata. An injected replan_fn skips
                # the seams entirely, so the list stays empty then.
                repair_declines: list[dict[str, Any]] = []
                declines_logged = 0
                # Campaign build #3: the bypass itself is now a
                # recorded fact, not an empty list's silence. Every
                # exam arm through #11 injected replan_fn and so
                # never exercised the seams; the firing/decline
                # accounting is only complete when the bypass is
                # stamped alongside the declines it replaced.
                seams_bypassed: dict[str, Any] | None = None
                if replan_fn is not None:
                    seams_bypassed = {
                        "reason": "replan_fn_injected",
                        "version": version,
                    }
                    self._log(
                        "loop.replan.seams_bypassed",
                        run_id=run_id,
                        plan_id=plan.plan_id,
                        version=version,
                        reason="replan_fn_injected",
                    )
                if replan_fn is None:
                    # Cycle 8: before re-planning the whole task, try
                    # repairing only the node that produced the code a
                    # measurement failed on — the definition as it
                    # stands on disk plus the failure's diagnostics,
                    # in one focused call. The attempt rides THIS
                    # re-plan decision: replans_used was incremented
                    # above, before either path runs, so a local
                    # repair and a whole-task re-plan each cost
                    # exactly one budget unit, never two. Any doubt
                    # in the local path returns None and the
                    # whole-task re-plan below runs exactly as
                    # before. An injected replan_fn is a caller
                    # override of the whole re-plan step, so the
                    # local seam — part of the default path — is not
                    # attempted under it.
                    new_nodes = try_local_repair(
                        plan, brief, declines=repair_declines
                    )
                    if new_nodes is not None:
                        self._log(
                            "loop.replan.local_repair",
                            run_id=run_id,
                            plan_id=plan.plan_id,
                            version=version,
                        )
                    if new_nodes is None:
                        declines_logged = self._log_decline_records(
                            repair_declines,
                            run_id,
                            plan.plan_id,
                            version,
                            declines_logged,
                        )
                        # Scoped-edit repair (campaign step 1): when
                        # the single-definition seam cannot identify
                        # its window, try editing the one suspect
                        # file the failing tests exercise — the
                        # model returns search/replace blocks, never
                        # a whole file. Same budget unit (the
                        # increment above already paid for this
                        # re-plan decision); any doubt returns None
                        # and the whole-task re-plan below runs
                        # exactly as before.
                        new_nodes = try_scoped_edit_repair(
                            plan, brief, declines=repair_declines
                        )
                        if new_nodes is not None:
                            self._log(
                                "loop.replan.scoped_edit_repair",
                                run_id=run_id,
                                plan_id=plan.plan_id,
                                version=version,
                            )
                        else:
                            declines_logged = self._log_decline_records(
                                repair_declines,
                                run_id,
                                plan.plan_id,
                                version,
                                declines_logged,
                            )
                if new_nodes is None:
                    try:
                        new_nodes = replan(brief)
                    except Exception as exc:  # noqa: BLE001 - re-planner failure escalates
                        escalation = f"re-planner failed: {exc}"
                        break
                rejection = judge.validate_delta_plan(
                    brief,
                    new_nodes,
                    known_failure_records=(
                        self._memory_store.executed_approach_records(
                            goal_key, domain
                        )
                    ),
                )
                if rejection is not None:
                    escalation = f"delta plan rejected: {rejection}"
                    break
                version += 1
                metadata = dict(plan.metadata or {})
                if repair_declines:
                    # Campaign build #2: why each repair seam
                    # declined rides into the new version's
                    # metadata, next to the repeat warnings below —
                    # never silently. (When the judge rejects the
                    # delta there is no new version to stamp; the
                    # audit events above still carry the records.)
                    metadata["repair_declines"] = [
                        dict(record) for record in repair_declines
                    ]
                if conversion is not None:
                    # Campaign build #6: the conversion record rides
                    # the new version's metadata, next to the seam
                    # accounting — a version that exists because an
                    # escalation was converted says so.
                    metadata["escalation_conversion"] = dict(conversion)
                if seams_bypassed is not None:
                    # Campaign build #3: the bypass record rides
                    # the same channel — a version produced under
                    # an injected replan_fn says so in its metadata.
                    metadata["seams_bypassed"] = dict(seams_bypassed)
                if brief.get("rejected_approaches"):
                    metadata["rejected_approaches"] = list(
                        brief["rejected_approaches"]
                    )
                if brief.get("replan_clause_coverage"):
                    # Campaign build #7: the accepted re-plan's
                    # declared clause coverage (the brief write-back
                    # from replan_from_brief: the open clauses it was
                    # assigned and the implements/verifies it
                    # declared), appended per version next to the
                    # frozen clause list the wholesale metadata copy
                    # above already carries. The record is the
                    # delta's claim; the verdict gate's per-version
                    # clause accounting and the probes judge the
                    # new version's actual state independently.
                    coverage_history = metadata.get("replan_clause_coverage")
                    coverage_history = (
                        list(coverage_history)
                        if isinstance(coverage_history, list)
                        else []
                    )
                    coverage_record = dict(brief["replan_clause_coverage"])
                    coverage_record["plan_version"] = version
                    coverage_history.append(coverage_record)
                    metadata["replan_clause_coverage"] = coverage_history
                if brief.get("intent_corrections"):
                    # Campaign build #11: the accepted re-plan's
                    # deterministic skeleton-intent corrections
                    # (the brief write-back from replan_from_brief:
                    # the declared intents parse corrected against
                    # the tree), appended per version next to the
                    # coverage record above — each entry gains the
                    # version whose delta declared it, so the run
                    # record shows which versions needed the
                    # system's correction and why.
                    correction_history = metadata.get("intent_corrections")
                    correction_history = (
                        list(correction_history)
                        if isinstance(correction_history, list)
                        else []
                    )
                    for correction in brief["intent_corrections"]:
                        correction_record = dict(correction)
                        correction_record["plan_version"] = version
                        correction_history.append(correction_record)
                    metadata["intent_corrections"] = correction_history
                if judge.repeat_warnings:
                    # Cross-run signature matches the substance check
                    # cleared: the historical warnings ride into the
                    # new version's metadata, never silently.
                    metadata["repeated_approach_warnings"] = list(
                        judge.repeat_warnings
                    )
                    for warning in judge.repeat_warnings:
                        self._log(
                            "loop.memory.repeat_warning",
                            run_id=run_id,
                            plan_id=plan.plan_id,
                            warning=warning,
                        )
                    judge.repeat_warnings.clear()
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

    def _critic_review(
        self,
        plan: ExecutionPlan,
        contract: GoalContract,
        critic: CriticSettings,
        *,
        run_id: str,
        version: int,
    ) -> bool:
        """Measure a plan version's write nodes for substance (Track B).

        Returns True when at least one write node was judged hollow:
        each such node is FAILED with a critic measurement recorded
        in the shape the executor records an executed failure in
        (node.measurement set, the measurement appended to the run
        store, the verdict text merged into the node's inputs as
        stderr — so the judge's re-plan brief carries the critic's
        reason as the failure's diagnostics, cycle 5). The version
        then does not execute; the judge treats the verdicts as
        informative failures and re-plans under the existing budget.

        The criterion result is measured=False by design: the
        approach was refused before it ran, so experiment memory
        files the record as a planning-stage rejection (cycle 3's
        stage rule) and the content-blind approach signatures in
        failed_approaches are never poisoned by a content judgment.
        Within the run the discipline is unaffected — the judge's
        delta validation computes failed signatures from the brief's
        failed nodes directly.

        A critic that cannot judge (backend down, verdict
        unparseable) degrades to no-critic: returns False, the
        version executes, and the degradation is audit-logged. The
        second model's availability never blocks the loop.

        A hollow verdict without verbatim evidence is neither of
        those: it is VOID (cycle 16) — the node is treated as
        substantive, the version is not blocked on it, and the
        voiding is audit-logged (loop.critic.voided) with the
        reason the evidence check failed.
        """
        try:
            review = review_write_nodes(plan, contract.objective, critic)
        except Exception as exc:  # noqa: BLE001 - a broken critic degrades
            review = None
            degraded = f"{type(exc).__name__}: {exc}"
        else:
            degraded = review.degraded
        if degraded is not None:
            self._log(
                "loop.critic.degraded",
                run_id=run_id,
                plan_id=plan.plan_id,
                version=version,
                detail=degraded,
            )
            return False
        assert review is not None  # for the type checker; degraded is None here
        if review.verdicts:
            self._log(
                "loop.critic.reviewed",
                run_id=run_id,
                plan_id=plan.plan_id,
                version=version,
                reviewed=len(review.verdicts),
                hollow=len(review.hollow),
            )
        for voided in review.voided:
            self._log(
                "loop.critic.voided",
                run_id=run_id,
                plan_id=plan.plan_id,
                version=version,
                node_id=voided.node_id,
                detail=voided.detail,
            )
        nodes = plan.node_map()
        for verdict in review.hollow:
            node = nodes[verdict.node_id]
            detail = f"critic review (hollow): {verdict.reason}"
            node.status = PlanStatus.FAILED
            node.error = detail
            node.inputs = dict(node.inputs or {})
            node.inputs["stderr"] = detail
            measurement: dict[str, Any] = {
                "node_id": node.id,
                "verdict": VERDICT_FAIL,
                "results": [
                    {
                        "criterion": {"type": "critic_review"},
                        "passed": False,
                        "measured": False,
                        "detail": detail,
                    }
                ],
                "exit_code": None,
                "duration_s": 0.0,
                "metrics": {},
                "envelope_status": "critic",
                "measured_at": datetime.now(UTC).isoformat(),
                "plan_version": version,
            }
            node.measurement = measurement
            self._run_store.append_measurement(run_id, measurement)
            self._log(
                "loop.critic.hollow",
                run_id=run_id,
                plan_id=plan.plan_id,
                version=version,
                node_id=node.id,
                reason=verdict.reason,
            )
        return bool(review.hollow)

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

    def _assess_plan_repeats(
        self, plan: ExecutionPlan, goal_key: str, domain: str
    ) -> tuple[str | None, list[str]]:
        """Adjudicate a plan's nodes against experiment memory.

        Returns (veto, warnings): the first vetoed repeat's human
        description, and the warnings for signature matches the
        substance check cleared to proceed. Matching is by approach
        signature (memory.approach_signature): the node's mechanism
        and targets, never its objective/hypothesis wording — and
        only against approaches that were actually tried (executed
        stage; see ExperimentStore.executed_approach_records). A
        signature match alone no longer vetoes: the substance check
        (judge.assess_approach_repeat) decides whether this plan is
        the approach that was measured FAIL — identical payload, or
        hollow by the deterministic confession rule — or a different
        payload that merely shares the mechanism and targets, which
        proceeds with the historical failure carried as a warning.
        Plumbing nodes (test runs, live checks, server starts) have
        no signature and can never be a repeat. A node carrying the
        system's repair note (``differs_by``, written only by the
        repair path when it splices a measured fix; parse strips any
        model-emitted value) is not a repeat — the change is
        acknowledged by the system itself.
        """
        records = self._memory_store.executed_approach_records(goal_key, domain)
        if not records:
            return None, []
        veto: str | None = None
        warnings: list[str] = []
        for node in plan.nodes:
            assessment = assess_approach_repeat(node, records)
            if assessment is None:
                continue
            if assessment.veto:
                if veto is None:
                    veto = assessment.reason
            else:
                warnings.append(assessment.reason)
        return veto, warnings

    def _find_repeated_approach(
        self, plan: ExecutionPlan, goal_key: str, domain: str
    ) -> str | None:
        """A node whose repeat of a recorded failed approach vetoes.

        The veto-only view of _assess_plan_repeats: a human
        description of the first substantively-repeating node, or
        None when every signature match was cleared by the substance
        check (or there was no match at all).
        """
        veto, _warnings = self._assess_plan_repeats(plan, goal_key, domain)
        return veto

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

    def _run_contract_probes(
        self, run_id: str, plan: ExecutionPlan, version: int
    ) -> list[dict[str, Any]]:
        """Run the plan's contract probes; stamp + return failures.

        Campaign build #3: the frozen clause list rides the plan
        metadata (system-stamped at planning); a plan without one
        has no probes and this is a no-op, exactly as before. The
        results replace the metadata's previous record — the brief
        and the verdict gate judge the CURRENT tree, while the
        audit log keeps every version's outcome.
        """
        clauses = clauses_from_metadata(plan.metadata)
        if not clauses:
            return []
        root_value = (plan.metadata or {}).get("repo_root")
        if not root_value:
            return []
        results = run_contract_probes(clauses, str(root_value))
        plan.metadata[PROBE_RESULTS_METADATA_KEY] = results
        self._store.save(plan)
        failures = probe_failures(results)
        self._log(
            "loop.contract_probes",
            run_id=run_id,
            plan_id=plan.plan_id,
            version=version,
            probes=len(results),
            failing=sorted({str(record["clause_id"]) for record in failures}),
        )
        return failures

    def _evaluate_goal(
        self,
        contract: GoalContract,
        plan: ExecutionPlan,
        measurements: list[dict[str, Any]],
        elapsed_s: float,
        escalation: str | None,
    ) -> tuple[str, str]:
        """Evaluate the goal: base measures, then the clause gate.

        The base evaluation (measures, plan state, escalation) is
        unchanged from before campaign build #3. When the plan
        carries a frozen contract-clause list, the clause
        adjustment then applies: a PASS requires every clause
        verified or explicitly waived — the loop does not grade
        its own homework — and the budget-ended and partial
        outcomes get their own verdict words (PARTIAL /
        INCONCLUSIVE) instead of being conflated into FAIL.
        Plans without clauses return the base verdict untouched.
        """
        verdict, detail, kind = self._evaluate_goal_base(
            contract, plan, measurements, elapsed_s, escalation
        )
        clauses = clauses_from_metadata(plan.metadata)
        if not clauses:
            return verdict, detail
        waivers_raw = (plan.metadata or {}).get(WAIVERS_METADATA_KEY)
        waivers = waivers_raw if isinstance(waivers_raw, dict) else None
        probe_results = (plan.metadata or {}).get(PROBE_RESULTS_METADATA_KEY)
        states = clause_states(
            clauses,
            plan.nodes,
            probe_results if isinstance(probe_results, list) else [],
            waivers,
        )
        verified = sorted(
            cid for cid, info in states.items() if info["state"] == "verified"
        )
        waived = sorted(
            cid for cid, info in states.items() if info["state"] == "waived"
        )
        failed = sorted(
            cid for cid, info in states.items() if info["state"] == "failed"
        )
        unverified = sorted(
            cid for cid, info in states.items() if info["state"] == "unverified"
        )
        accounting = (
            f"contract clauses: {len(verified) + len(waived)}/{len(clauses)} "
            "verified or waived"
        )
        if failed:
            accounting += f"; failed: {', '.join(failed)}"
        if unverified:
            accounting += f"; unverified: {', '.join(unverified)}"
        detail = f"{detail} [{accounting}]"
        if verdict == VERDICT_PASS:
            if not failed and not unverified:
                return VERDICT_PASS, detail
            if failed and not verified and not waived:
                return VERDICT_FAIL, detail
            return VERDICT_PARTIAL, detail
        if verdict == VERDICT_FAIL and kind in ("escalation", "plan_failed"):
            if kind == "escalation" and not failed and unverified:
                return (
                    VERDICT_INCONCLUSIVE,
                    detail
                    + " — the run ended before verification completed",
                )
            if (verified or waived) and (failed or unverified):
                return VERDICT_PARTIAL, detail
        return verdict, detail

    def _evaluate_goal_base(
        self,
        contract: GoalContract,
        plan: ExecutionPlan,
        measurements: list[dict[str, Any]],
        elapsed_s: float,
        escalation: str | None,
    ) -> tuple[str, str, str]:
        """The pre-build-#3 goal evaluation, plus a failure kind.

        The kind names which channel produced a FAIL (wall clock,
        escalation, plan state, or the contract's own measures) so
        the clause adjustment can tell a budget-ended run from a
        verified-unmet one without sniffing the detail text. The
        base rule stands as before: a run that passes every node
        but misses the contract's success measures is a goal
        failure — and evidence the criteria need strengthening, by
        a human, between runs.
        """
        if elapsed_s > contract.budget.max_wall_clock_s:
            return VERDICT_FAIL, (
                f"wall-clock budget exceeded: {elapsed_s:.1f}s > "
                f"{contract.budget.max_wall_clock_s}s"
            ), "wall_clock"
        if escalation is not None:
            return VERDICT_FAIL, f"escalated: {escalation}", "escalation"
        if plan.status == PlanStatus.FAILED:
            failed = [n.id for n in plan.nodes if n.status == PlanStatus.FAILED]
            return VERDICT_FAIL, f"plan failed; failed nodes: {failed}", "plan_failed"
        if plan.status == PlanStatus.NEEDS_TOOL:
            return VERDICT_FAIL, "plan needs a tool that does not exist yet", "needs_tool"
        if plan.status == PlanStatus.AWAITING_APPROVAL:
            return VERDICT_UNMEASURABLE, "plan paused awaiting approval", "awaiting"
        if not contract.success_measures:
            return VERDICT_PASS, "plan completed; no success measures declared", "pass"

        merged: dict[str, Any] = {}
        for measurement in measurements:
            merged.update(measurement.get("metrics") or {})
        for measure in contract.success_measures:
            if measure.metric not in merged:
                return VERDICT_UNMEASURABLE, (
                    f"success measure {measure.name!r} unmeasurable: "
                    f"metric {measure.metric!r} not reported"
                ), "measure_missing"
            value = merged[measure.metric]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return VERDICT_FAIL, (
                    f"success measure {measure.name!r}: metric "
                    f"{measure.metric!r} is not numeric: {value!r}"
                ), "measure_unmet"
            if not _compare(measure.op, float(value), float(measure.value)):
                return VERDICT_FAIL, (
                    f"success measure {measure.name!r}: {measure.metric} "
                    f"{value} {measure.op} {measure.value} is false"
                ), "measure_unmet"
        return VERDICT_PASS, "all success measures met", "pass"

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

    def _log_decline_records(
        self,
        records: list[dict[str, Any]],
        run_id: str,
        plan_id: str,
        version: int,
        seen: int,
    ) -> int:
        """Audit-log the decline records appended since ``seen``.

        Campaign build #2: one event per record, named for its
        seam (loop.replan.local_repair_declined /
        loop.replan.scoped_edit_repair_declined) and carrying the
        reason code + detail the seam recorded. Returns the new
        high-water mark, so each record is logged exactly once
        across the two seam attempts of one re-plan decision.
        """
        for record in records[seen:]:
            event = (
                "loop.replan.local_repair_declined"
                if record.get("seam") == "local_repair"
                else "loop.replan.scoped_edit_repair_declined"
            )
            self._log(
                event,
                run_id=run_id,
                plan_id=plan_id,
                version=version,
                reason=record.get("reason", ""),
                detail=record.get("detail", ""),
            )
        return len(records)
