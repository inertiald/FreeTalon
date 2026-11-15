"""Asyncio-driven DAG execution engine for FreeTalon."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TYPE_CHECKING, TypedDict

from .models import ExecutionPlan, PlanNode, PlanStatus
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry, UnknownCapabilityError

if TYPE_CHECKING:
    from freetalon.audit import AuditLogger
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
    ) -> None:
        self._store = store
        self._registry = registry
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._subdag_planner = subdag_planner
        self._injection_counts: dict[str, int] = {}
        self._scaffolder = scaffolder
        self._audit_logger = audit_logger

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
            target.status = PlanStatus.RUNNING
            plan.touch()
            self._store.save(plan)

            capability = target.assigned_claw or node.id
            logger.debug("Dispatching node %s (capability=%r).", node.id, capability)

            try:
                handler = self._registry.resolve(capability)

                # Resolve context piping placeholders (e.g. {{step_1.stdout}})
                completed_results = {
                    n.id: n.inputs
                    for n in plan.nodes
                    if n.status == PlanStatus.COMPLETED
                }
                resolved_inputs = _resolve_input_variables(dict(target.inputs), completed_results)

                result = await handler(resolved_inputs)

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

                plan = self._load_or_raise(plan_id)
                target = _find_node(plan, node.id)
                if target is not None:
                    target.status = PlanStatus.COMPLETED
                    if isinstance(result, dict):
                        target.inputs.update(result)
                    target.error = None
                    plan.touch()
                    self._store.save(plan)
                logger.info("Node %s completed.", node.id)
            except UnknownCapabilityError as exc:
                if self._scaffolder is not None:
                    self._handle_missing_capability(plan_id, node.id, capability, exc)
                else:
                    self._record_failure(plan_id, node.id, str(exc))
            except Exception as exc:
                self._record_failure(plan_id, node.id, str(exc))

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

    def _record_failure(self, plan_id: str, node_id: str, error: str) -> None:
        try:
            plan = self._load_or_raise(plan_id)
            node = _find_node(plan, node_id)
            if node is not None:
                node.status = PlanStatus.FAILED
                node.error = error
                plan.touch()
                self._store.save(plan)
        except ExecutorError:
            logger.error("Could not record failure for node %s.", node_id)

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
