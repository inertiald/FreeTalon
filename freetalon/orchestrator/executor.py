"""Asyncio-driven DAG execution engine for FreeTalon."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING, TypedDict

from .measure import (
    VERDICT_PASS,
    build_envelope,
    measure_node,
    measurement_failed_detail,
)
from .models import ExecutionPlan, PlanNode, PlanStatus
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry, UnknownCapabilityError

if TYPE_CHECKING:
    from freetalon.audit import AuditLogger
    from .run_store import RunStore
    from .tool_scaffolder import ToolScaffolder

logger = logging.getLogger(__name__)

_NO_RESULT: dict[str, Any] = {}
DEPENDENCY_MISSING_KEY = "dependency_missing"
MAX_INJECTION_ATTEMPTS = 3


class _DependencyRequestRequired(TypedDict):
    objectives: list[str]


class DependencyRequest(_DependencyRequestRequired, total=False):
    inputs: dict[str, Any]


def _is_dependency_missing(result: Any) -> bool:
    return isinstance(result, dict) and DEPENDENCY_MISSING_KEY in result


class ExecutorError(RuntimeError):
    """Raised when the executor cannot make forward progress."""


class Executor:
    def __init__(
        self,
        store: ExecutionPlanStateStore,
        registry: ToolRegistry,
        max_concurrency: int = 8,
        subdag_planner: Callable[[DependencyRequest], list[PlanNode]] | None = None,
        scaffolder: ToolScaffolder | None = None,
        audit_logger: AuditLogger | None = None,
        run_store: "RunStore | None" = None,
        run_id: str | None = None,
        custom_checkers: dict | None = None,
        plan_version: int = 1,
        gate_callback: "Callable[[PlanNode], bool] | None" = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._subdag_planner = subdag_planner
        self._injection_counts: dict[str, int] = {}
        self._scaffolder = scaffolder
        self._audit_logger = audit_logger
        # Loop (M0): when a run store is attached, node outputs and
        # measurements are recorded in the run directory.
        self._run_store = run_store
        self._run_id = run_id
        self._custom_checkers = custom_checkers
        self._plan_version = plan_version
        # M4: called before an irreversible node (reversible is False)
        # runs. Returns True when a human has approved the action; False
        # pauses the node at AWAITING_APPROVAL. None means no gate is
        # configured — an irreversible node then fails rather than running.
        self._gate_callback = gate_callback

    async def run(self, plan_id: str) -> ExecutionPlan:
        plan = self._store.load(plan_id)
        if plan is None:
            raise ExecutorError(f"Plan {plan_id!r} not found in store.")

        logger.info("Executor starting plan %s (%d nodes).", plan_id, len(plan.nodes))
        plan.status = PlanStatus.RUNNING
        plan.touch()
        self._store.save(plan)

        while True:
            plan = self._load_or_raise(plan_id)

            if _is_terminal(plan.status):
                break

            runnable = _collect_runnable(plan)
            in_flight = _count_in_flight(plan)

            if not runnable and in_flight == 0:
                break

            if runnable:
                tasks = [
                    asyncio.create_task(
                        self._execute_node(plan_id, node),
                        name=f"node-{node.id}",
                    )
                    for node in runnable
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                continue

            if in_flight > 0:
                await asyncio.sleep(0.05)
                continue

        plan = self._load_or_raise(plan_id)
        final_status = _derive_plan_status(plan)
        if plan.status != final_status:
            plan.status = final_status
            plan.touch()
            self._store.save(plan)

        logger.info("Executor finished plan %s — status: %s.", plan_id, plan.status)
        return plan

    async def _execute_node(self, plan_id: str, node: PlanNode) -> None:
        async with self._semaphore:
            plan = self._load_or_raise(plan_id)
            target = _find_node(plan, node.id)
            if target is None or _is_terminal(target.status):
                return
            # M4: an irreversible node never runs without a human approval.
            if target.reversible is False:
                if self._gate_callback is None:
                    self._record_failure(
                        plan_id,
                        node.id,
                        "irreversible node requires an approval gate"
                        " (no gate callback configured).",
                    )
                    return
                if not self._gate_callback(target):
                    target.status = PlanStatus.AWAITING_APPROVAL
                    plan.touch()
                    self._store.save(plan)
                    return
            target.status = PlanStatus.RUNNING
            plan.touch()
            self._store.save(plan)

            capability = target.assigned_claw or node.id
            logger.debug("Dispatching node %s (capability=%r).", node.id, capability)
            started = time.monotonic()

            try:
                handler = self._registry.resolve(capability)

                # Resolve context piping placeholders (e.g. {{step_1.stdout}})
                completed_results = {
                    n.id: n.inputs
                    for n in plan.nodes
                    if n.status == PlanStatus.COMPLETED
                }
                resolved_inputs = _resolve_input_variables(dict(target.inputs), completed_results)
                # Loop (M0): handlers run in the node's run directory when a
                # run store is attached, with the node's hard timeout.
                workdir = self._node_workdir(node.id)
                resolved_inputs.setdefault("workdir", str(workdir))
                # M3: the run directory is shared by every node in the run,
                # so a dataset fetched by one node can be analysed by the
                # next (research artifacts, sandbox quarantine live here).
                if self._run_store is not None and self._run_id is not None:
                    resolved_inputs.setdefault(
                        "run_dir", str(self._run_store.run_dir(self._run_id))
                    )
                resolved_inputs.setdefault("timeout_s", target.timeout_s)

                try:
                    result = await asyncio.wait_for(
                        handler(resolved_inputs), timeout=target.timeout_s
                    )
                except asyncio.TimeoutError:
                    duration_s = time.monotonic() - started
                    envelope = build_envelope(
                        node.id, None, duration_s=duration_s, status="timeout"
                    )
                    measurement = self._measure_and_record(target, envelope)
                    self._record_failure(
                        plan_id,
                        node.id,
                        f"Node timed out after {target.timeout_s}s.",
                        measurement,
                    )
                    return
                duration_s = time.monotonic() - started

                if _is_dependency_missing(result):
                    dep_request: DependencyRequest = result[DEPENDENCY_MISSING_KEY]

                    if self._subdag_planner is None:
                        self._record_failure(
                            plan_id,
                            node.id,
                            "DependencyMissing returned but no subdag_planner is configured.",
                        )
                        return

                    count = self._injection_counts.get(node.id, 0)
                    if count >= MAX_INJECTION_ATTEMPTS:
                        self._record_failure(
                            plan_id,
                            node.id,
                            f"Injection loop guard triggered for node {node.id!r}.",
                        )
                        return

                    self._injection_counts[node.id] = count + 1
                    try:
                        sub_nodes = self._subdag_planner(dep_request)
                        plan = self._load_or_raise(plan_id)
                        plan = _inject_subdag(plan, node.id, sub_nodes)
                        plan.touch()
                        self._store.save(plan)
                    except Exception as planner_exc:
                        self._record_failure(
                            plan_id,
                            node.id,
                            f"sub-DAG planner raised an error: {planner_exc}",
                        )
                        return
                    return

                # Loop (M0): the handler returning is only an input to
                # measurement. The node is COMPLETED only when its
                # measurement passes; a non-zero exit code or a failed
                # criterion makes it FAILED with the reason attached.
                # A handler may report its own execution status (e.g. the
                # bash handler reports "timeout" when it kills the process
                # group). Only execution statuses count here — handlers also
                # return business statuses like "success", which mean the
                # handler completed normally.
                handler_status = "completed"
                if isinstance(result, dict) and result.get("status") in {
                    "timeout",
                    "killed",
                    "failed",
                }:
                    handler_status = result["status"]
                envelope = build_envelope(
                    node.id,
                    result if isinstance(result, dict) else {},
                    duration_s=duration_s,
                    status=handler_status,
                )
                measurement = self._measure_and_record(target, envelope)
                plan = self._load_or_raise(plan_id)
                target = _find_node(plan, node.id)
                if target is not None:
                    # The plan was reloaded after execution, so attach the
                    # measurement to this fresh node object (the pre-run
                    # object measured above is a different instance).
                    target.measurement = measurement
                    target.exit_code = envelope.get("exit_code")
                    target.duration_s = envelope.get("duration_s")
                    if isinstance(result, dict):
                        target.inputs.update(result)
                    if measurement["verdict"] == VERDICT_PASS:
                        target.status = PlanStatus.COMPLETED
                        target.error = None
                        logger.info("Node %s completed (measurement PASS).", node.id)
                    else:
                        target.status = PlanStatus.FAILED
                        target.error = measurement_failed_detail(measurement)
                        logger.info(
                            "Node %s failed measurement: %s", node.id, target.error
                        )
                    plan.touch()
                    self._store.save(plan)
            except UnknownCapabilityError as exc:
                if self._scaffolder is not None:
                    self._handle_missing_capability(plan_id, node.id, capability, exc)
                else:
                    self._record_failure(plan_id, node.id, str(exc))
            except Exception as exc:
                duration_s = time.monotonic() - started
                envelope = build_envelope(
                    node.id,
                    None,
                    duration_s=duration_s,
                    status="failed",
                    error={"kind": type(exc).__name__, "message": str(exc)},
                )
                measurement = self._measure_and_record(target, envelope)
                self._record_failure(plan_id, node.id, str(exc), measurement)

    def _handle_missing_capability(
        self,
        plan_id: str,
        node_id: str,
        capability: str,
        exc: UnknownCapabilityError,
    ) -> None:
        from .tool_scaffolder import CapabilityNameError

        assert self._scaffolder is not None
        try:
            proposal = self._scaffolder.propose(capability)
            error_msg = (
                f"Missing capability {capability!r}: draft scaffold proposed at {proposal.path!s}."
            )
            if self._audit_logger is not None:
                self._audit_logger.log(
                    "tool.scaffold.proposed",
                    capability=capability,
                    path=str(proposal.path),
                    node_id=node_id,
                    plan_id=plan_id,
                )
        except CapabilityNameError as val_exc:
            error_msg = f"Missing capability {capability!r} and scaffold rejected: {val_exc}"

        try:
            plan = self._load_or_raise(plan_id)
            node = _find_node(plan, node_id)
            if node is not None:
                node.status = PlanStatus.NEEDS_TOOL
                node.error = error_msg
                plan.touch()
                self._store.save(plan)
                if self._audit_logger is not None:
                    self._audit_logger.log(
                        "node.needs_tool",
                        capability=capability,
                        node_id=node_id,
                        plan_id=plan_id,
                    )
        except ExecutorError:
            logger.error("Could not record NEEDS_TOOL for node %s.", node_id)

    def _record_failure(
        self,
        plan_id: str,
        node_id: str,
        error: str,
        measurement: dict[str, Any] | None = None,
    ) -> None:
        try:
            plan = self._load_or_raise(plan_id)
            node = _find_node(plan, node_id)
            if node is not None:
                node.status = PlanStatus.FAILED
                node.error = error
                if measurement is not None:
                    node.measurement = measurement
                    node.exit_code = measurement.get("exit_code")
                    node.duration_s = measurement.get("duration_s")
                plan.touch()
                self._store.save(plan)
        except ExecutorError:
            logger.error("Could not record failure for node %s.", node_id)

    def _node_workdir(self, node_id: str) -> Path:
        """Working directory for a node: its run directory, else the cwd."""
        if self._run_store is not None and self._run_id is not None:
            node_dir = self._run_store.node_dir(
                self._run_id, node_id, self._plan_version
            )
            node_dir.mkdir(parents=True, exist_ok=True)
            return node_dir
        return Path.cwd()

    def _measure_and_record(
        self, node: PlanNode, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Measure a finished node and persist the verdict (M0)."""
        workdir = self._node_workdir(node.id)
        measurement = measure_node(node, envelope, workdir, self._custom_checkers)
        node.measurement = measurement
        node.exit_code = envelope.get("exit_code")
        node.duration_s = envelope.get("duration_s")
        # Stamp the plan version on the measurement so attempt history
        # stays attributable across re-plans.
        measurement["plan_version"] = self._plan_version
        if self._run_store is not None and self._run_id is not None:
            paths = self._run_store.write_node_output(
                self._run_id,
                node.id,
                stdout=envelope.get("stdout", ""),
                stderr=envelope.get("stderr", ""),
                version=self._plan_version,
            )
            envelope["stdout_path"] = paths["stdout_path"]
            envelope["stderr_path"] = paths["stderr_path"]
            self._run_store.append_measurement(self._run_id, measurement)
        if self._audit_logger is not None:
            self._audit_logger.log(
                "node.measured",
                node_id=node.id,
                verdict=measurement["verdict"],
                exit_code=envelope.get("exit_code"),
            )
        return measurement

    def _load_or_raise(self, plan_id: str) -> ExecutionPlan:
        plan = self._store.load(plan_id)
        if plan is None:
            raise ExecutorError(f"Plan {plan_id!r} disappeared from store mid-run.")
        return plan


def _resolve_input_variables(inputs: dict[str, Any], completed_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    resolved = {}
    for key, value in inputs.items():
        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
            target_ref = value[2:-2].strip()
            if "." in target_ref:
                step_id, field = target_ref.split(".", 1)
                parent_result = completed_results.get(step_id, {})
                resolved[key] = parent_result.get(field, value)
            else:
                resolved[key] = value
        else:
            resolved[key] = value
    return resolved


def _is_terminal(status: PlanStatus) -> bool:
    return status in {
        PlanStatus.COMPLETED,
        PlanStatus.FAILED,
        PlanStatus.CANCELLED,
        PlanStatus.NEEDS_TOOL,
    }


def _all_dependencies_met(node: PlanNode, node_map: dict[str, PlanNode]) -> bool:
    for dep_id in node.depends_on:
        dep = node_map.get(dep_id)
        if dep is None or dep.status != PlanStatus.COMPLETED:
            return False
    return True


def _collect_runnable(plan: ExecutionPlan) -> list[PlanNode]:
    node_map = plan.node_map()
    return [
        node
        for node in plan.nodes
        if node.status in {PlanStatus.DRAFT, PlanStatus.READY}
        and _all_dependencies_met(node, node_map)
    ]


def _count_in_flight(plan: ExecutionPlan) -> int:
    return sum(1 for node in plan.nodes if node.status == PlanStatus.RUNNING)


def _find_node(plan: ExecutionPlan, node_id: str) -> PlanNode | None:
    return plan.node_map().get(node_id)


def _derive_plan_status(plan: ExecutionPlan) -> PlanStatus:
    statuses = {node.status for node in plan.nodes}
    if PlanStatus.FAILED in statuses or PlanStatus.NEEDS_TOOL in statuses:
        return PlanStatus.FAILED
    if PlanStatus.CANCELLED in statuses:
        return PlanStatus.CANCELLED
    if PlanStatus.AWAITING_APPROVAL in statuses:
        return PlanStatus.AWAITING_APPROVAL
    if all(_is_terminal(s) for s in statuses):
        return PlanStatus.COMPLETED
    return PlanStatus.RUNNING


def _inject_subdag(
    plan: ExecutionPlan,
    orig_node_id: str,
    sub_nodes: list[PlanNode],
) -> ExecutionPlan:
    if not sub_nodes:
        orig = _find_node(plan, orig_node_id)
        if orig is not None:
            orig.status = PlanStatus.DRAFT
            orig.error = None
        return plan

    existing_ids = {n.id for n in plan.nodes}
    id_map: dict[str, str] = {}
    for k, sub_node in enumerate(sub_nodes):
        base = f"injected-{orig_node_id}-{k}"
        candidate = base
        suffix = 0
        while candidate in existing_ids:
            suffix += 1
            candidate = f"{base}-{suffix}"
        id_map[sub_node.id] = candidate
        existing_ids.add(candidate)

    remapped: list[PlanNode] = []
    for sub_node in sub_nodes:
        new_deps = [id_map.get(d, d) for d in sub_node.depends_on]
        remapped.append(
            PlanNode(
                id=id_map[sub_node.id],
                objective=sub_node.objective,
                depends_on=new_deps,
                assigned_claw=sub_node.assigned_claw,
                status=PlanStatus.DRAFT,
                inputs=dict(sub_node.inputs),
                outputs=list(sub_node.outputs),
                acceptance=list(sub_node.acceptance),
            )
        )

    all_sub_deps = {d for n in remapped for d in n.depends_on}
    terminal_ids = [n.id for n in remapped if n.id not in all_sub_deps]

    plan.nodes.extend(remapped)

    orig = _find_node(plan, orig_node_id)
    if orig is not None:
        for tid in terminal_ids:
            if tid not in orig.depends_on:
                orig.depends_on.append(tid)
        orig.status = PlanStatus.DRAFT
        orig.error = None

    return plan
