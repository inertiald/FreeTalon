"""The judge (loop Stage 5, M1): four decisions, all from measurements.

After each executor pass the judge looks at what was measured — never at
the model's recollection — and decides:

- ``CONTINUE`` — every node succeeded; the run proceeds to goal evaluation.
- ``RETRY`` — a failure looks transient (a timeout) and retries remain.
- ``REPLAN`` — a failure is informative (a criterion missed, a hypothesis
  refuted); the planner gets a structured brief, not a fresh prompt.
- ``ESCALATE`` — budget spent, stop condition hit, repeated UNMEASURABLE,
  no progress across re-plans, or a missing tool. The run ends honestly
  with its full attempt history.

Anti-self-deception rule (design doc): the judge may not lower a
threshold, edit a criterion, or reclassify a FAIL to keep a run alive.
Changing the contract is a human act, done between runs, in the open.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .measure import VERDICT_UNMEASURABLE
from .memory import (
    approach_signature,
    describe_signature,
    write_targets_for_node,
    write_targets_for_nodes,
)
from .models import ExecutionPlan, GoalContract, PlanNode, PlanStatus, RunBudget


class Decision(str, Enum):
    """The judge's four decisions."""

    CONTINUE = "continue"
    RETRY = "retry"
    REPLAN = "replan"
    ESCALATE = "escalate"


@dataclass
class JudgeDecision:
    """One judge decision, with the reason recorded for the audit trail."""

    decision: Decision
    reason: str
    retry_node_ids: list[str] = field(default_factory=list)
    replan_brief: dict[str, Any] | None = None


class ProgressTracker:
    """The progress rule: re-plans must earn new passing criteria.

    If no criterion anywhere has newly passed after a set number of
    re-plans, the run escalates instead of continuing — this bounds
    endless re-planning and budget burn with no progress.
    """

    STALL_LIMIT = 2

    def __init__(self) -> None:
        self.passed_signatures: set[str] = set()
        self.stale_replans = 0

    def observe(self, measurements: list[dict[str, Any]]) -> bool:
        """Record newly passing criteria. Returns True if any is new."""
        progressed = False
        for measurement in measurements:
            for result in measurement.get("results", []):
                if result.get("passed"):
                    sig = json.dumps(result.get("criterion"), sort_keys=True, default=str)
                    if sig not in self.passed_signatures:
                        self.passed_signatures.add(sig)
                        progressed = True
        return progressed

    def note_replan(self, progressed: bool) -> None:
        """Call after a re-plan: resets the stall count on progress."""
        self.stale_replans = 0 if progressed else self.stale_replans + 1

    def stalled(self) -> bool:
        return self.stale_replans >= self.STALL_LIMIT


def _is_timeout(node: PlanNode) -> bool:
    measurement = node.measurement or {}
    if measurement.get("envelope_status") == "timeout":
        return True
    return "timed out" in (node.error or "")


def _is_unmeasurable(node: PlanNode) -> bool:
    return (node.measurement or {}).get("verdict") == VERDICT_UNMEASURABLE


# Bounds for the diagnostic excerpt a re-plan brief carries per failed
# node (see _failure_diagnostics): the tail of the node's captured
# output, where test runners put the failure block and the assertion.
_DIAGNOSTIC_TAIL_LINES = 12
_DIAGNOSTIC_LINE_CHARS = 300
_DIAGNOSTIC_MAX_CHARS = 1500


def _failure_diagnostics(node: PlanNode) -> str:
    """The bounded tail of a failed node's captured output.

    The executor merges the handler's result into the node's inputs,
    so a bash node's own stderr (else stdout) is on the node at brief
    time. That text is where the substance of an executed failure
    lives — the assertion a test raised, the error a tool printed —
    while the measurement itself records only the criterion's shape
    ("exit_code 1 != 0"). The excerpt is the output's last lines
    (test runners print the failure block and its assertion at the
    tail), each line and the whole capped, so a full-suite dump
    cannot flood the re-plan prompt. Returns "" when the node
    captured no output; the brief then renders exactly as it did
    before diagnostics existed.
    """
    inputs = node.inputs or {}
    text = ""
    for key in ("stderr", "stdout"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break
    if not text:
        return ""
    lines = [line.rstrip() for line in text.strip("\n").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    excerpt = lines[-_DIAGNOSTIC_TAIL_LINES:]
    excerpt = [
        line
        if len(line) <= _DIAGNOSTIC_LINE_CHARS
        else line[: _DIAGNOSTIC_LINE_CHARS - 3] + "..."
        for line in excerpt
    ]
    while len(excerpt) > 1 and len("\n".join(excerpt)) > _DIAGNOSTIC_MAX_CHARS:
        excerpt.pop(0)
    return "\n".join(excerpt)


def _diagnostic_fingerprint(diagnostics: str) -> str | None:
    """The identity of a failure for repeat detection: its Error line.

    The last line of the excerpt naming an error (AssertionError,
    ValueError, ...) is the failure's substance in one line — the
    same assertion raised again by a later plan version is the same
    failure, whatever the surrounding steps were. Returns None when
    the excerpt names no error; such failures are carried in the
    brief but not tracked for repeats.
    """
    fingerprint: str | None = None
    for line in diagnostics.splitlines():
        if "Error" in line:
            fingerprint = line.strip()
    return fingerprint


# ── Failing tests + stagnation (re-plan anchoring, 2026-11-23) ──────
#
# The brief used to carry a failed test-run node only as a criterion
# line ("exit_code 1 != 0") plus a bounded diagnostics tail. Exam
# re-arm #8 showed what that costs: the same three tests failed in
# all four plan versions, every re-plan rewrote the one file it had
# already rewritten, and nothing in the brief ever said the failure
# set had not moved. The judge therefore names the failing tests
# themselves — extracted mechanically from the failed nodes' captured
# output — and tracks the set across the briefs it issues, so
# stagnation is a computed fact in the brief, not a pattern the
# planner is left to notice. (The cross-model consultation of
# 2026-11-23 ranked this signal first, near-unanimously.)

_UNITTEST_FAILURE_RE = re.compile(
    r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([^)]+)\)", re.MULTILINE
)
_UNITTEST_BARE_FAILURE_RE = re.compile(
    r"^(?:FAIL|ERROR):\s+(\w+)\s*$", re.MULTILINE
)
# A pytest nodeid always carries "::"; requiring it keeps
# unittest's own summary line ("FAILED (failures=2)") out of the set.
_PYTEST_FAILURE_RE = re.compile(r"^FAILED\s+(\S+::\S+)", re.MULTILINE)

# A failing-test set is evidence, not a transcript: cap it so a
# catastrophic run cannot flood the brief. The set is sorted before
# the cap, so the cap is deterministic.
_FAILING_TESTS_CAP = 50


def _failing_test_ids(node: PlanNode) -> set[str]:
    """Failing test identifiers in a node's captured output.

    unittest prints a header per failure — "FAIL: test_x
    (pkg.mod.Class.test_x)" — whose parenthesised part is the
    test's dotted identity (module and class alone in older shapes,
    completed here with the method name); pytest's short summary
    prints "FAILED path::test", already an identity. A bare unittest
    header (no parenthesised class) contributes the method name
    alone, and is read only when no fuller form matched. Returns an
    empty set when the node captured no test-runner output: such
    failures ride the brief exactly as before, untracked here.
    """
    inputs = node.inputs or {}
    text = ""
    for key in ("stderr", "stdout"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            text += value + "\n"
    ids: set[str] = set()
    for match in _UNITTEST_FAILURE_RE.finditer(text):
        method, qualified = match.group(1), match.group(2)
        # The parenthesised form is usually the full dotted id,
        # method included ("pkg.mod.Class.test_x"); when it names
        # only module and class, the method completes it.
        ids.add(
            qualified
            if qualified.endswith("." + method)
            else f"{qualified}.{method}"
        )
    for match in _PYTEST_FAILURE_RE.finditer(text):
        ids.add(match.group(1))
    if not ids:
        for match in _UNITTEST_BARE_FAILURE_RE.finditer(text):
            ids.add(match.group(1))
    return ids


class Judge:
    """Stateful per-run judge: budget accounting, decisions, re-plan briefs."""

    def __init__(self, budget: RunBudget) -> None:
        self.budget = budget
        self.started = time.monotonic()
        self.replans_used = 0
        self.attempts_by_hypothesis: dict[str, int] = {}
        self.retries_by_node: dict[str, int] = {}
        self.unmeasurable_streak: dict[str, int] = {}
        self.progress = ProgressTracker()
        # Diagnostic fingerprints of the briefs this judge has already
        # issued in this run, keyed by fingerprint with the plan
        # version first briefed — a later brief whose failed node
        # reproduces one names the repeat (see build_replan_brief).
        self._briefed_diagnostics: dict[str, int] = {}
        # Re-plan anchoring state (2026-11-23): the failing-test set
        # of the previous brief this judge issued and the plan
        # version that set was first briefed at, plus the run's
        # write history — each declared target's last-writing plan
        # version, and every target ever declared — from which the
        # brief's stagnation record and file lists are computed.
        self._previous_failing_tests: frozenset[str] | None = None
        self._failing_tests_since: int | None = None
        self._file_last_written: dict[str, int] = {}
        self._declared_targets: set[str] = set()

    # ── Bookkeeping ──────────────────────────────────────────────────────

    def record_pass(self, ran_nodes: list[PlanNode]) -> None:
        """Record one executor pass: count attempts per hypothesis."""
        for node in ran_nodes:
            key = node.hypothesis or node.id
            self.attempts_by_hypothesis[key] = self.attempts_by_hypothesis.get(key, 0) + 1
            if _is_unmeasurable(node):
                self.unmeasurable_streak[node.id] = self.unmeasurable_streak.get(node.id, 0) + 1
            else:
                self.unmeasurable_streak[node.id] = 0

    def wall_clock_exceeded(self) -> bool:
        return (time.monotonic() - self.started) > self.budget.max_wall_clock_s

    def remaining_budget(self) -> dict[str, Any]:
        return {
            "replans": max(self.budget.max_replans - self.replans_used, 0),
            "wall_clock_s": max(
                self.budget.max_wall_clock_s - (time.monotonic() - self.started), 0.0
            ),
            "max_attempts_per_hypothesis": self.budget.max_attempts_per_hypothesis,
            "max_nodes": self.budget.max_nodes,
            "max_money": self.budget.max_money,
        }

    # ── Decisions ────────────────────────────────────────────────────────

    def decide(
        self, plan: ExecutionPlan, measurements: list[dict[str, Any]]
    ) -> JudgeDecision:
        """Decide the next step from a finished executor pass."""
        if plan.status == PlanStatus.COMPLETED:
            return JudgeDecision(Decision.CONTINUE, "all nodes succeeded")

        needs_tool = [n for n in plan.nodes if n.status == PlanStatus.NEEDS_TOOL]
        if needs_tool:
            return JudgeDecision(
                Decision.ESCALATE,
                "missing capability: "
                + ", ".join(n.assigned_claw or n.id for n in needs_tool),
            )
        if self.wall_clock_exceeded():
            return JudgeDecision(
                Decision.ESCALATE,
                f"wall-clock budget spent ({self.budget.max_wall_clock_s}s)",
            )

        failed = [n for n in plan.nodes if n.status == PlanStatus.FAILED]
        if not failed:
            # Plan is not COMPLETED but nothing failed (e.g. cancelled).
            return JudgeDecision(
                Decision.ESCALATE, f"plan ended in state {plan.status.value}"
            )

        # Repeated UNMEASURABLE is an escalation, not a re-plan: the plan
        # is underspecified and re-planning the same way will not fix it.
        for node in failed:
            if self.unmeasurable_streak.get(node.id, 0) >= 2:
                return JudgeDecision(
                    Decision.ESCALATE,
                    f"node {node.id!r} unmeasurable twice in a row; "
                    "the plan is underspecified",
                )

        # Transient failures only: retry the timed-out nodes, unchanged.
        timeout_nodes = [n for n in failed if _is_timeout(n)]
        informative = [n for n in failed if n not in timeout_nodes]
        if timeout_nodes and not informative:
            retryable = [
                n
                for n in timeout_nodes
                if self.retries_by_node.get(n.id, 0) < n.max_retries
                and self.attempts_by_hypothesis.get(n.hypothesis or n.id, 0)
                < self.budget.max_attempts_per_hypothesis
            ]
            if retryable:
                for node in retryable:
                    self.retries_by_node[node.id] = (
                        self.retries_by_node.get(node.id, 0) + 1
                    )
                return JudgeDecision(
                    Decision.RETRY,
                    "transient timeout; retries remain",
                    retry_node_ids=[n.id for n in retryable],
                )
            return JudgeDecision(
                Decision.ESCALATE,
                "timed-out nodes exhausted their retries",
            )

        # Informative failure: re-plan if budget and progress allow.
        if self.replans_used >= self.budget.max_replans:
            return JudgeDecision(
                Decision.ESCALATE,
                f"re-plan budget spent ({self.budget.max_replans} re-plans)",
            )
        if self.progress.stalled():
            return JudgeDecision(
                Decision.ESCALATE,
                "no criterion has newly passed across "
                f"{ProgressTracker.STALL_LIMIT} re-plans; stalled",
            )
        return JudgeDecision(
            Decision.REPLAN,
            "informative failure: "
            + "; ".join(f"{n.id}: {n.error or 'failed'}" for n in failed),
        )

    # ── Re-plan briefs ───────────────────────────────────────────────────

    def build_replan_brief(
        self,
        contract: GoalContract,
        plan: ExecutionPlan,
        measurements: list[dict[str, Any]],
        memory: list[dict[str, Any]],
        plan_version: int,
    ) -> dict[str, Any]:
        """Structured brief for the planner: same goal, new information."""
        failed = [n for n in plan.nodes if n.status == PlanStatus.FAILED]
        failed_entries: list[dict[str, Any]] = []
        new_fingerprints: dict[str, int] = {}
        for n in failed:
            diagnostics = _failure_diagnostics(n)
            entry: dict[str, Any] = {
                "id": n.id,
                "objective": n.objective,
                "hypothesis": n.hypothesis,
                "assigned_claw": n.assigned_claw,
                # The node's inputs ride along so repeat detection
                # (validate_delta_plan) can compute the failed
                # approach's substance signature from what the
                # node actually did, not from its wording. The
                # planner's brief prompt reads only the prose
                # fields and the bounded diagnostics below; the raw
                # inputs are for the judge.
                "inputs": dict(n.inputs or {}),
                "measurement": n.measurement,
                "error": n.error,
                # The failure's substance, from the node's own
                # captured output: the criterion detail alone
                # ("exit_code 1 != 0") tells the planner THAT the
                # node failed, never WHAT failed. Empty when the
                # node captured no output.
                "diagnostics": diagnostics,
            }
            fingerprint = _diagnostic_fingerprint(diagnostics)
            if fingerprint is not None:
                first_version = self._briefed_diagnostics.get(fingerprint)
                if first_version is not None and first_version < plan_version:
                    entry["repeated_diagnostic"] = fingerprint
                    entry["repeated_from_version"] = first_version
                new_fingerprints.setdefault(fingerprint, plan_version)
            failed_entries.append(entry)
        self._briefed_diagnostics.update(new_fingerprints)
        brief = {
            "goal": contract.model_dump(mode="json"),
            "plan_id": plan.plan_id,
            "plan_version": plan_version,
            "failed_nodes": failed_entries,
            "measurements": measurements,
            "memory": memory,
            "remaining_budget": self.remaining_budget(),
            "instruction": (
                "Produce a delta plan. Do not repeat a recorded failed "
                "approach: repeats are detected structurally, by "
                "capability and targets, never by wording — if you "
                "revisit a failed node's ground, change the mechanism "
                "or the targets. Shrink scope before growing it."
            ),
        }
        brief.update(self._anchoring_fields(plan, plan_version, failed))
        return brief

    def _anchoring_fields(
        self,
        plan: ExecutionPlan,
        plan_version: int,
        failed: list[PlanNode],
    ) -> dict[str, Any]:
        """The brief's anchoring record: failing tests + write history.

        Computed from the executed plan and this judge's own brief
        history, never from model output. "failing_tests" names the
        tests the failed nodes' captured output reports.
        "stagnation" is present only when that set is identical to
        the previous brief's — the signal exam re-arm #8 lacked while
        three re-plans rewrote the same file against the same three
        failures. The file lists name what the run has produced:
        targets the most recent version did not revise, and targets
        declared by some version but missing on disk.
        """
        failing: set[str] = set()
        for node in failed:
            failing |= _failing_test_ids(node)
        failing_tests = sorted(failing)[:_FAILING_TESTS_CAP]
        current_targets = write_targets_for_nodes(plan.nodes)
        for target in current_targets:
            self._file_last_written[target] = plan_version
            self._declared_targets.add(target)
        never_revised = sorted(
            target
            for target, version in self._file_last_written.items()
            if version < plan_version
        )
        missing = self._missing_declared_targets(plan)
        missing_set = set(missing)
        fields: dict[str, Any] = {
            "failing_tests": failing_tests,
            "produced_files": sorted(
                target
                for target in self._declared_targets
                if target not in missing_set
            ),
            "never_revised_files": never_revised,
            "missing_declared_files": missing,
        }
        current_set = frozenset(failing_tests)
        if current_set and current_set == self._previous_failing_tests:
            fields["stagnation"] = {
                "failing_tests": failing_tests,
                "unchanged_since_version": self._failing_tests_since,
                "last_change_files": current_targets,
            }
        elif current_set != self._previous_failing_tests:
            self._failing_tests_since = plan_version if current_set else None
        self._previous_failing_tests = current_set
        return fields

    def _missing_declared_targets(self, plan: ExecutionPlan) -> list[str]:
        """Declared write targets that do not exist on disk.

        Resolved against the plan's recorded repo root (plan
        metadata, stamped at planning time); without a root there is
        no base to judge existence against and the list is empty. An
        unresolvable path is not evidence of absence.
        """
        root_value = (plan.metadata or {}).get("repo_root")
        if not root_value:
            return []
        root = Path(str(root_value))
        missing = []
        for target in sorted(self._declared_targets):
            try:
                exists = (root / target).exists()
            except OSError:
                exists = True
            if not exists:
                missing.append(target)
        return missing

    def validate_delta_plan(
        self,
        brief: dict[str, Any],
        new_nodes: list[PlanNode],
        known_failures: set[tuple[Any, ...]] | None = None,
    ) -> str | None:
        """Reject a re-plan that repeats a failed approach without a reason.

        Returns an error string when the delta plan is rejected, else None.
        A node repeats a failed approach when its approach signature
        (memory.approach_signature — mechanism and targets, never the
        node's wording) matches a failed node's and it carries no
        system repair note. The note is the PlanNode ``differs_by``
        field, written only by the repair path when it splices a
        measured fix into a copied node; parse strips any
        model-emitted value, so a note read here is the system's
        own, never model prose. Plumbing nodes (test runs, live
        checks, server
        starts) have no signature and can never repeat. The brief's
        failed nodes all executed in this run, so their failures are
        executed-stage by construction. ``known_failures`` adds the
        signatures recorded in experiment memory (M2) — executed-stage
        measured failures only (see ExperimentStore.failed_approaches).
        """
        failed_signatures: set[tuple[Any, ...]] = set()
        for f in brief.get("failed_nodes", []):
            signature = approach_signature(
                PlanNode(
                    id=f["id"],
                    objective=f["objective"],
                    assigned_claw=f.get("assigned_claw", ""),
                    hypothesis=f.get("hypothesis", ""),
                    inputs=f.get("inputs") or {},
                )
            )
            if signature is not None:
                failed_signatures.add(signature)
        if known_failures:
            failed_signatures |= set(known_failures)
        stagnation_error = self._stagnation_gate_error(brief, new_nodes)
        if stagnation_error is not None:
            return stagnation_error
        if not failed_signatures:
            return None
        for node in new_nodes:
            signature = approach_signature(node)
            if (
                signature is not None
                and signature in failed_signatures
                and not node.differs_by
            ):
                return (
                    f"re-plan repeats failed approach "
                    f"({describe_signature(signature)}) "
                    f"without a system repair note"
                )
        return None

    def _stagnation_gate_error(
        self, brief: dict[str, Any], new_nodes: list[PlanNode]
    ) -> str | None:
        """Reject a delta plan re-attacking a stagnant failure set
        through the same files the last change already wrote.

        Fires only when the brief carries this judge's stagnation
        record — the failing-test set identical to the previous
        brief's, which by construction means this is at least the
        second re-plan — and the delta plan's write targets are a
        non-empty subset of the last change's targets. A write node
        carrying a system repair note (``differs_by``, the local
        repair path's splice) exempts the plan, exactly as in the
        repeat-approach check: the note is the system's own evidence
        the mechanism changed. A changed failure set, a widened
        target set, or a first re-plan leaves the gate silent. The
        rejection names the unexamined suspects — files produced by
        earlier versions and never revised, and (when the planner's
        suspect map rode back into the brief) the modules the
        failing tests exercise — and states the precedence rule.
        """
        stagnation = brief.get("stagnation")
        if not isinstance(stagnation, dict):
            return None
        failing = [str(t) for t in stagnation.get("failing_tests") or []]
        if not failing:
            return None
        last_change = {
            str(t) for t in stagnation.get("last_change_files") or []
        }
        new_targets = set(write_targets_for_nodes(new_nodes))
        if not new_targets or not new_targets <= last_change:
            return None
        for node in new_nodes:
            if write_targets_for_node(node) and node.differs_by:
                return None
        message = (
            "re-plan repeats failed approach (stagnation: the failing "
            f"tests {', '.join(failing)} are unchanged since plan "
            f"version {stagnation.get('unchanged_since_version', '?')}, "
            "and the delta plan writes only files the last change "
            f"already wrote: {', '.join(sorted(new_targets))}) "
            "without a system repair note"
        )
        suspects = [str(t) for t in brief.get("never_revised_files") or []]
        if suspects:
            message += (
                "; unexamined suspects — files produced by earlier "
                "plan versions and not revised since: "
                + ", ".join(suspects)
            )
        modules: set[str] = set()
        suspect_map = brief.get("suspect_map")
        if isinstance(suspect_map, dict):
            for entry in suspect_map.values():
                if isinstance(entry, dict):
                    modules.update(
                        str(m) for m in entry.get("modules") or []
                    )
        if modules:
            message += (
                "; the failing tests exercise: "
                + ", ".join(sorted(modules))
            )
        message += (
            "; tests produced during this run are derived artifacts, "
            "not ground truth — where one contradicts the governing "
            "spec or goal, fix the test, not the code"
        )
        return message
