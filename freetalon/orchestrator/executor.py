"""Asyncio-driven DAG execution engine for FreeTalon.

The :class:`Executor` walks an :class:`~freetalon.orchestrator.models.ExecutionPlan`
stored in a :class:`~freetalon.orchestrator.state_store.ExecutionPlanStateStore`,
schedules nodes whose dependencies are satisfied, dispatches them to
:class:`~freetalon.orchestrator.tool_registry.ToolRegistry` handlers, and
persists status updates back to the store.

Typical usage::

    registry = ToolRegistry()
    registry.register("scaffold", my_scaffold_handler)

    store = ExecutionPlanStateStore()
    executor = Executor(store=store, registry=registry, max_concurrency=4)
    await executor.run("plan-id-123")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .models import ExecutionPlan, PlanNode, PlanStatus
from .state_store import ExecutionPlanStateStore
from .tool_registry import ToolRegistry, UnknownCapabilityError

logger = logging.getLogger(__name__)

# Sentinel used to indicate a node completed without returning a dict result.
_NO_RESULT: dict[str, Any] = {}


class ExecutorError(RuntimeError):
    """Raised when the executor cannot make forward progress."""


class Executor:
    """Asyncio DAG executor that drives an :class:`ExecutionPlan` to completion.

    Parameters
    ----------
    store:
        Persistent store used to load and save the execution plan.
    registry:
        Tool registry that maps capability names to async handlers.
    max_concurrency:
        Maximum number of nodes that may be in-flight simultaneously.
        Defaults to ``8``.
    """

    def __init__(
        self,
        store: ExecutionPlanStateStore,
        registry: ToolRegistry,
        max_concurrency: int = 8,
    ) -> None:
        self._store = store
        self._registry = registry
        self._semaphore = asyncio.Semaphore(max_concurrency)

    # ── Public interface ──────────────────────────────────────────────────

    async def run(self, plan_id: str) -> ExecutionPlan:
        """Execute the plan identified by *plan_id* until completion or stall.

        The method repeatedly identifies *runnable* nodes (all dependencies
        met, status ``READY`` or ``DRAFT``), dispatches them concurrently up
        to *max_concurrency*, collects results, and persists each state
        transition.  The loop ends when:

        - all nodes reach a terminal status (``COMPLETED`` / ``FAILED`` /
          ``CANCELLED``), **or**
        - no runnable nodes remain but non-terminal nodes still exist (stall).

        Parameters
        ----------
        plan_id:
            Identifier of the persisted :class:`ExecutionPlan` to execute.

        Returns
        -------
        ExecutionPlan
            The final state of the plan after execution.

        Raises
        ------
        ExecutorError
            If the plan cannot be found in the store, or if no progress can
            be made (cyclic / unfulfillable dependencies).
        """
        plan = self._store.load(plan_id)
        if plan is None:
            raise ExecutorError(f"Plan {plan_id!r} not found in store.")

        logger.info("Executor starting plan %s (%d nodes).", plan_id, len(plan.nodes))
        plan.status = PlanStatus.RUNNING
        plan.touch()
        self._store.save(plan)

        while True:
            plan = self._load_or_raise(plan_id)

            if _is_terminal(plan.status) and plan.status != PlanStatus.RUNNING:
                # Plan was externally cancelled or otherwise terminated.
                break

            runnable = _collect_runnable(plan)
            in_flight = _count_in_flight(plan)

            if not runnable and in_flight == 0:
                # Nothing running, nothing can run — decide final status.
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
                # Reload plan after the batch so the next iteration sees fresh state.
                continue

            if in_flight > 0:
                # Something else is running (e.g. external caller); yield briefly.
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

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _execute_node(self, plan_id: str, node: PlanNode) -> None:
        """Dispatch *node* to its handler and persist the result.

        The semaphore limits the number of concurrently running nodes.
        All exceptions are caught and recorded as node failures so they
        do not abort sibling nodes.
        """
        async with self._semaphore:
            # Mark as RUNNING and persist before dispatching.
            plan = self._load_or_raise(plan_id)
            target = _find_node(plan, node.id)
            if target is None or _is_terminal(target.status):
                # Node was already handled (race guard).
                return
            target.status = PlanStatus.RUNNING
            plan.touch()
            self._store.save(plan)

            capability = target.assigned_claw or node.id
            logger.debug("Dispatching node %s (capability=%r).", node.id, capability)

            try:
                handler = self._registry.resolve(capability)
                result = await handler(dict(target.inputs))
                # Persist success.
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
                self._record_failure(plan_id, node.id, str(exc))
                logger.error("Node %s failed: unknown capability %r.", node.id, capability)
            except Exception as exc:  # noqa: BLE001
                self._record_failure(plan_id, node.id, str(exc))
                logger.exception("Node %s failed with exception.", node.id)

    def _record_failure(self, plan_id: str, node_id: str, error: str) -> None:
        """Persist a FAILED status and error message for *node_id*."""
        try:
            plan = self._load_or_raise(plan_id)
            node = _find_node(plan, node_id)
            if node is not None:
                node.status = PlanStatus.FAILED
                node.error = error
                plan.touch()
                self._store.save(plan)
        except ExecutorError:
            logger.error("Could not record failure for node %s — plan gone.", node_id)

    def _load_or_raise(self, plan_id: str) -> ExecutionPlan:
        """Load plan from the store or raise :class:`ExecutorError`."""
        plan = self._store.load(plan_id)
        if plan is None:
            raise ExecutorError(f"Plan {plan_id!r} disappeared from store mid-run.")
        return plan


# ── Module-level helpers (pure functions, easy to unit-test) ──────────────────


def _is_terminal(status: PlanStatus) -> bool:
    """Return ``True`` if *status* is a terminal (non-running) state."""
    return status in {
        PlanStatus.COMPLETED,
        PlanStatus.FAILED,
        PlanStatus.CANCELLED,
    }


def _all_dependencies_met(node: PlanNode, node_map: dict[str, PlanNode]) -> bool:
    """Return ``True`` if every upstream node has ``COMPLETED`` status.

    A dependency that is not found in *node_map* is treated as not yet
    complete so execution is deferred (safe default).
    """
    for dep_id in node.depends_on:
        dep = node_map.get(dep_id)
        if dep is None or dep.status != PlanStatus.COMPLETED:
            return False
    return True


def _collect_runnable(plan: ExecutionPlan) -> list[PlanNode]:
    """Return nodes that are ready to be dispatched.

    A node is runnable when its status is ``DRAFT`` or ``READY`` **and** all
    of its declared ``depends_on`` nodes are ``COMPLETED``.
    """
    node_map = plan.node_map()
    return [
        node
        for node in plan.nodes
        if node.status in {PlanStatus.DRAFT, PlanStatus.READY}
        and _all_dependencies_met(node, node_map)
    ]


def _count_in_flight(plan: ExecutionPlan) -> int:
    """Return the number of nodes currently in ``RUNNING`` status."""
    return sum(1 for node in plan.nodes if node.status == PlanStatus.RUNNING)


def _find_node(plan: ExecutionPlan, node_id: str) -> PlanNode | None:
    """Return the :class:`PlanNode` with *node_id*, or ``None``."""
    return plan.node_map().get(node_id)


def _derive_plan_status(plan: ExecutionPlan) -> PlanStatus:
    """Infer the overall plan status from the terminal state of all nodes.

    Rules:
    - Any node ``FAILED`` → plan ``FAILED``
    - Any node ``CANCELLED`` → plan ``CANCELLED``
    - All nodes ``COMPLETED`` → plan ``COMPLETED``
    - Otherwise → ``RUNNING`` (still in progress)
    """
    statuses = {node.status for node in plan.nodes}
    if PlanStatus.FAILED in statuses:
        return PlanStatus.FAILED
    if PlanStatus.CANCELLED in statuses:
        return PlanStatus.CANCELLED
    if all(_is_terminal(s) for s in statuses):
        return PlanStatus.COMPLETED
    return PlanStatus.RUNNING
