"""Defect-lineage provenance for the autonomous loop (campaign build #29).

The diagnostic battery (2026-11-26) proved the exam model can explain
and repair every standing pairing defect under direct conditions, yet
the loop landed none of them across up to 22 arms — and one arm's plan
named all eight then-current defects while shipping a byte-identical
tree. A six-service frontier consultation converged on the missing
observation: the loop never recorded whether a diagnosed defect became
a plan obligation, whether that obligation was scheduled and filled,
whether any candidate actually changed the tree, or whether selection
kept the change. Byte-identity of the shipped tree cannot separate an
execution failure from a selection failure.

This module is that record. It is OBSERVABILITY ONLY: every entry
point is guarded, a recorder failure can never fail a run, and no
planner, scheduler, fill, verification, or selection behavior reads
anything written here.

Defect identity is derived mechanically from the contract-probe
records — never from free-text matching of model prose:

    defect_id = "<clause_id>::<probe>::<flow>"   (flow when present)
              = "<clause_id>::<probe>"           (static probes)

Per run the recorder streams stage events to ``lineage.jsonl`` in the
run directory (via RunStore), saves every assembled fill prompt
verbatim under ``lineage_prompts/``, and at run end assembles
``lineage.json`` (the machine-readable per-run lineage record) plus
``lineage.md`` (the rendered per-defect table whose columns are
Diagnosed | Planned | Scheduled | Generated diff | Verified fixed |
Shipped fixed — the first Yes->No transition names the stage that
dropped the defect).

The fill-prompt channel mirrors the planner attempt sink (builds
#9/#10/#13): the planner emits through a module-level sink the loop
installs for the run's duration; records from the initial planning
phases (which run before any run exists, in the driver flow) wait in
a bounded pending buffer that LoopRunner.run_plan drains on entry.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Callable

# ── Defect identity ──────────────────────────────────────────────────


def defect_id_for_record(record: dict[str, Any]) -> str:
    """The stable defect id of one contract-probe record.

    Derived from the record's own identity fields (clause, probe
    kind, flow) — the same fields the probe layer stamped when it
    produced the record. Two records name the same defect iff these
    fields agree; the human acceptance tests group onto these ids
    (e.g. the six method_matrix flows of one clause are six defects
    of one family, told apart by flow).
    """
    clause_id = str(record.get("clause_id") or "")
    probe = str(record.get("probe") or "")
    flow = record.get("flow")
    if flow:
        return f"{clause_id}::{probe}::{flow}"
    return f"{clause_id}::{probe}"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def tree_hash_for(target_hashes: dict[str, str | None]) -> str | None:
    """One hash naming a whole snapshot: per-target hashes, sorted.

    ``target_hashes`` maps a declared write target to the SHA-256 of
    its bytes at snapshot time, or None when the target was absent.
    Returns None for an empty mapping (no snapshot was possible).
    """
    if not target_hashes:
        return None
    lines = [
        f"{target}={target_hashes[target] if target_hashes[target] is not None else 'ABSENT'}"
        for target in sorted(target_hashes)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def snapshot_hashes(
    entries: dict[str, bytes | None] | None,
) -> dict[str, str | None]:
    """Per-target SHA-256 for a version snapshot's raw entries."""
    hashes: dict[str, str | None] = {}
    for target, content in (entries or {}).items():
        hashes[str(target)] = (
            _sha256_bytes(content) if content is not None else None
        )
    return hashes


# ── Fill-prompt sink (the planner side) ──────────────────────────────
#
# The planner's content fill assembles the exact prompt a node is
# filled from and, on acceptance, the candidate it produced. Those
# two facts exist only inside planner._fill_node_payload; this sink
# is how they reach the run record without the planner knowing what
# a run is — the build #9 attempt-sink pattern, carried to prompts.
# With no sink installed the emit is a no-op apart from the bounded
# pending buffer for the initial planning phases (the driver flow
# plans before LoopRunner.run_plan exists to install a sink).

#: The installed lineage sink, if any (see lineage_sink).
_LINEAGE_SINK: Callable[[dict[str, Any]], None] | None = None

#: The phases whose records buffer when no sink is installed — the
#: initial fill phase (the re-plan fill phase only ever runs inside
#: a run, where a sink is installed; buffering it would misattribute
#: one caller's fills to whatever run started next).
_BUFFERED_PHASES = frozenset({"content_fill"})

#: Fill records awaiting a run's sink, oldest first. Bounded: a
#: planning call's fills are a handful of plan-sized texts, and the
#: cap holds the most recent ones when no run ever claims them.
_PENDING_LINEAGE: list[dict[str, Any]] = []
_PENDING_LINEAGE_LIMIT = 96


@contextmanager
def lineage_sink(sink: Callable[[dict[str, Any]], None] | None):
    """Install ``sink`` as the lineage sink for a block.

    Records go to the innermost installed sink; nesting restores
    the outer sink on exit. Passing None installs a blackout.
    LoopRunner.run_plan is the production installer.
    """
    global _LINEAGE_SINK
    previous = _LINEAGE_SINK
    _LINEAGE_SINK = sink
    try:
        yield
    finally:
        _LINEAGE_SINK = previous


def drain_pending_lineage(
    sink: Callable[[dict[str, Any]], None],
) -> int:
    """Hand the buffered initial-planning fill records to ``sink``.

    LoopRunner.run_plan calls this with its run sink on entry — the
    initial planning that produced the run's plan happened before
    that sink existed, and its fill records waited in the pending
    buffer. Returns the number of records delivered. The buffer is
    cleared either way. A sink failure on one record is noted on
    stdout and the drain continues — persistence is observability,
    never a gate.
    """
    global _PENDING_LINEAGE
    pending = _PENDING_LINEAGE
    _PENDING_LINEAGE = []
    delivered = 0
    for record in pending:
        try:
            sink(record)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                "lineage drain failed "
                f"({type(exc).__name__}: {exc}); the fill record "
                "is lost to the run record."
            )
    return delivered


def _emit_lineage(record: dict[str, Any]) -> None:
    """Offer one fill record to the sink (or the pending buffer)."""
    if _LINEAGE_SINK is not None:
        try:
            _LINEAGE_SINK(record)
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                "lineage sink failed "
                f"({type(exc).__name__}: {exc}); the fill record "
                "is lost to the run record."
            )
    elif record.get("phase") in _BUFFERED_PHASES:
        _PENDING_LINEAGE.append(record)
        del _PENDING_LINEAGE[:-_PENDING_LINEAGE_LIMIT]


def record_fill_prompt(
    *,
    phase: str,
    attempt: int,
    node_id: str | None,
    target: str | None,
    intent: str | None,
    prompt: str,
    system_prompt: str,
    existing_source: str | None,
) -> None:
    """Record the EXACT prompt one fill attempt is about to send.

    Called by planner._fill_node_payload immediately after the
    prompt is assembled, before the LLM call — one record per
    attempt, retries included (a retry's prompt differs: it carries
    the rejection block). The prompt text rides the record; the
    recorder saves it verbatim to a file in the run directory and
    keeps only its hash in the lineage events.
    """
    _emit_lineage(
        {
            "kind": "fill_prompt",
            "phase": phase,
            "attempt": attempt,
            "node_id": node_id,
            "target": target,
            "intent": intent,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "existing_source_sha256": (
                _sha256_text(existing_source)
                if existing_source is not None
                else None
            ),
            "existing_source_chars": (
                len(existing_source) if existing_source is not None else None
            ),
        }
    )


def record_fill_outcome(
    *,
    phase: str,
    attempt: int,
    node_id: str | None,
    target: str | None,
    intent: str | None,
    outcome: str,
    candidate_inputs: dict[str, Any] | None = None,
    existing_source: str | None = None,
    errors: Any = (),
) -> None:
    """Record how one node's fill ended (accepted / failed).

    For an accepted fill the record carries the candidate's shape:
    the full-content hash when the payload carried whole content,
    whether that content is byte-identical to the file as it stood
    (the no-change case the battery could not see), the
    materialization marker, and the scoped-edit / anchor shape when
    the payload came in those forms instead.
    """
    inputs = candidate_inputs or {}
    content = inputs.get("content")
    content_is_text = isinstance(content, str)
    edits = inputs.get("edits")
    _emit_lineage(
        {
            "kind": "fill_outcome",
            "phase": phase,
            "attempt": attempt,
            "node_id": node_id,
            "target": target,
            "intent": intent,
            "outcome": outcome,
            "materialized_from": inputs.get("_materialized_from"),
            "content_sha256": (
                _sha256_text(content) if content_is_text else None
            ),
            "content_chars": len(content) if content_is_text else None,
            "content_equals_existing": (
                (content == existing_source)
                if content_is_text and existing_source is not None
                else None
            ),
            "edits_count": len(edits) if isinstance(edits, list) else None,
            "has_anchor": bool(inputs.get("anchor")),
            "errors": [str(error) for error in errors][:20],
        }
    )


# ── The recorder (the loop side) ─────────────────────────────────────


def _rank_key(outcome: dict[str, Any]) -> tuple[int, int, int, int]:
    """The best-version rank tuple of an outcome record.

    Mirrors loop._version_rank_key (campaign build #18) exactly —
    duplicated here so the lineage record can name each candidate's
    rank without importing the loop (the loop imports this module).
    """
    return (
        -int(outcome.get("probe_green", 0)),
        int(outcome.get("failed_nodes", 0)),
        int(outcome.get("failed_criteria", 0)),
        -int(outcome.get("version", 0)),
    )


class LineageRecorder:
    """Accumulates one run's defect lineage; writes its records.

    Constructed by LoopRunner.run_plan with the run's store and id.
    Every public method is guarded: an exception inside the recorder
    is printed and swallowed, so instrumentation can never fail,
    stall, or alter the run it observes.
    """

    def __init__(self, run_store: Any, run_id: str) -> None:
        self._run_store = run_store
        self._run_id = run_id
        self._seq = 0
        self._prompt_count = 0
        self._finalized = False
        # defect_id -> defect state (see diagnosis()).
        self._defects: dict[str, dict[str, Any]] = {}
        # version -> {defect_id: ok} from that version's probe run.
        self._probe_status: dict[int, dict[str, bool]] = {}
        # version -> plan binding record (see plan_written()).
        self._plans: dict[int, dict[str, Any]] = {}
        # version -> [execution records] (a retry appends another).
        self._executions: dict[int, list[dict[str, Any]]] = {}
        # version -> candidate record (see version_verified()).
        self._candidates: dict[int, dict[str, Any]] = {}
        # Fill records with their attributed plan version (or None).
        self._fills: list[dict[str, Any]] = []
        # Fill records not yet attributed to a plan version.
        self._pending_fills: list[dict[str, Any]] = []
        self._acceptances: list[dict[str, Any]] = []
        self._baseline_hashes: dict[str, str | None] | None = None
        self._baseline_tree_hash: str | None = None
        self._shipping_event: dict[str, Any] | None = None
        self._finish_info: dict[str, Any] | None = None

    # ── plumbing ─────────────────────────────────────────────────────

    def _append(self, record: dict[str, Any]) -> None:
        self._seq += 1
        stamped = {"seq": self._seq, "run_id": self._run_id, **record}
        try:
            self._run_store.append_lineage(self._run_id, stamped)
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                "lineage append failed "
                f"({type(exc).__name__}: {exc}); the event stands "
                "in the recorder's memory only."
            )

    def _guarded(self, label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — observability, not a gate
            print(
                f"lineage recorder: {label} failed "
                f"({type(exc).__name__}: {exc}); the run is unaffected."
            )

    # ── fill sink records ────────────────────────────────────────────

    def handle_sink_record(self, record: dict[str, Any]) -> None:
        """Route one planner fill record from the lineage sink."""
        self._guarded(
            "fill record", lambda: self._handle_sink_record(record)
        )

    def _handle_sink_record(self, record: dict[str, Any]) -> None:
        kind = record.get("kind")
        if kind == "fill_prompt":
            self._prompt_count += 1
            node_id = str(record.get("node_id") or "node")
            phase = str(record.get("phase") or "fill")
            filename = (
                f"{self._prompt_count:04d}-{phase}-{node_id}"
                f"-a{record.get('attempt')}.txt"
            )
            prompt_text = str(record.get("prompt") or "")
            rel_path = self._run_store.write_lineage_prompt(
                self._run_id, filename, prompt_text
            )
            stored = {
                key: value
                for key, value in record.items()
                if key not in ("prompt", "system_prompt")
            }
            stored["prompt_file"] = rel_path
            stored["prompt_sha256"] = _sha256_text(prompt_text)
            stored["prompt_chars"] = len(prompt_text)
            system_prompt = str(record.get("system_prompt") or "")
            stored["system_prompt_sha256"] = _sha256_text(system_prompt)
            stored["system_prompt_chars"] = len(system_prompt)
            self._pending_fills.append(stored)
            self._append(stored)
        elif kind == "fill_outcome":
            stored = dict(record)
            self._pending_fills.append(stored)
            self._append(stored)

    def _attribute_pending_fills(self, version: int | None) -> None:
        for stored in self._pending_fills:
            attributed = {**stored, "plan_version": version}
            self._fills.append(attributed)
        self._pending_fills = []

    # ── stage events from the loop ───────────────────────────────────

    def note_baseline(
        self, entries: dict[str, bytes | None] | None
    ) -> None:
        """Record the declared targets' state before version 1 runs.

        This is version 1's parent tree: the state the run started
        from, against which version 1's own diff is measured.
        """

        def _record() -> None:
            if self._baseline_hashes is not None or not entries:
                return
            self._baseline_hashes = snapshot_hashes(entries)
            self._baseline_tree_hash = tree_hash_for(self._baseline_hashes)
            self._append(
                {
                    "kind": "baseline",
                    "tree_hash": self._baseline_tree_hash,
                    "targets": dict(self._baseline_hashes),
                }
            )

        self._guarded("baseline", _record)

    def plan_written(
        self, version: int, nodes: list[dict[str, Any]]
    ) -> None:
        """Record one plan version's node -> clause bindings.

        ``nodes`` is the loop's own reading of the plan: per node its
        id, objective, claw, the clause ids it implements / verifies
        (from inputs["clauses"], via node_clause_declaration), its
        repair lineage fields (responds_to / differs_by), and whether
        it carries filled content (with that content's hash). The
        per-defect binding derived here — which nodes own a diagnosed
        defect's clause in this version — is the Planned column's
        evidence.
        """

        def _record() -> None:
            if version == 1 and self._pending_fills:
                # The drained initial-planning fills produced this
                # plan; they belong to version 1 by construction.
                self._attribute_pending_fills(1)
            bindings: dict[str, dict[str, Any]] = {}
            for defect_id, defect in self._defects.items():
                if defect["first_diagnosed_version"] >= version:
                    continue
                clause_id = defect["clause_id"]
                implement = [
                    node["id"]
                    for node in nodes
                    if clause_id in (node.get("implements") or [])
                ]
                verify = [
                    node["id"]
                    for node in nodes
                    if clause_id in (node.get("verifies") or [])
                ]
                if implement or verify:
                    bindings[defect_id] = {
                        "implement_nodes": implement,
                        "verify_nodes": verify,
                    }
            self._plans[version] = {
                "version": version,
                "nodes": nodes,
                "defect_bindings": bindings,
            }
            self._append(
                {
                    "kind": "plan",
                    "version": version,
                    "nodes": nodes,
                    "defect_bindings": bindings,
                }
            )

        self._guarded("plan", _record)

    def diagnosis(
        self,
        version: int,
        results: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> None:
        """Record one version's contract-probe results as diagnoses.

        Every failing record becomes (or refreshes) a defect: its
        identity from the record itself, its diagnosis text the
        record's detail (the probe layer's own account — the loop
        produces no model-authored diagnosis text; the model's
        response to the diagnosis is what the plan binding and fill
        records capture). The full per-version ok/failing status of
        every defect ever diagnosed is kept, so a later version's
        candidate can be credited with resolving — or introducing —
        each defect mechanically.
        """

        def _record() -> None:
            status: dict[str, bool] = {}
            for record in results:
                defect_id = defect_id_for_record(record)
                status[defect_id] = bool(record.get("ok"))
                if record.get("ok"):
                    continue
                defect = self._defects.get(defect_id)
                if defect is None:
                    defect = {
                        "defect_id": defect_id,
                        "clause_id": str(record.get("clause_id") or ""),
                        "probe": str(record.get("probe") or ""),
                        "flow": record.get("flow"),
                        "file": record.get("file"),
                        "first_diagnosed_version": version,
                        "diagnosis_detail": str(record.get("detail") or ""),
                        "diagnosis_source": "contract_probe_record",
                    }
                    self._defects[defect_id] = defect
                defect["last_failing_version"] = version
                defect["last_failing_detail"] = str(
                    record.get("detail") or ""
                )
            self._probe_status[version] = status
            self._append(
                {
                    "kind": "diagnosis",
                    "version": version,
                    "results": len(results),
                    "failing_defects": sorted(
                        defect_id
                        for defect_id, ok in status.items()
                        if not ok
                    ),
                }
            )

        self._guarded("diagnosis", _record)

    def version_executed(
        self, version: int, nodes: list[dict[str, Any]]
    ) -> None:
        """Record one version's execution pass, per node.

        ``nodes`` carries each node's status before/after, error,
        measurement verdict, and — for write nodes — the handler's
        merged outputs (path, bytes_written). A bound node appearing
        here with a terminal status is the Scheduled column's
        evidence; a bound node absent or still pending is a node the
        scheduler never ran.
        """

        def _record() -> None:
            self._executions.setdefault(version, []).append(
                {"version": version, "nodes": nodes}
            )
            self._append(
                {"kind": "execution", "version": version, "nodes": nodes}
            )

        self._guarded("execution", _record)

    def version_verified(
        self,
        version: int,
        outcome: dict[str, Any],
        snapshot_entries: dict[str, bytes | None] | None,
    ) -> None:
        """Record one version as a candidate: score, tree, parentage.

        The tree hash names the version's produced files (the loop's
        own snapshot); the parent is the previous executed version's
        tree, or the pre-run baseline for version 1. Resolved /
        introduced defect sets are computed from the probe-status
        maps the diagnosis events built — a defect is resolved by
        this version when it failed in the previous version's probe
        run and passes in this one's, introduced when the reverse
        holds.
        """

        def _record() -> None:
            hashes = snapshot_hashes(snapshot_entries)
            tree_hash = tree_hash_for(hashes)
            previous = max(
                (v for v in self._candidates if v < version),
                default=None,
            )
            if previous is not None:
                parent_hash = self._candidates[previous]["tree_hash"]
                parent_status = self._probe_status.get(previous, {})
            else:
                parent_hash = self._baseline_tree_hash
                parent_status = {}
            status = self._probe_status.get(version, {})
            if previous is None:
                # The first measured version has no parent
                # measurement: its failing defects were diagnosed
                # against the tree it produced, but the run's own
                # records cannot say whether it introduced them or
                # inherited them from the starting tree — so it is
                # credited with neither resolution nor introduction.
                resolved = []
                introduced = []
            else:
                resolved = sorted(
                    defect_id
                    for defect_id, was_ok in parent_status.items()
                    if not was_ok and status.get(defect_id)
                )
                introduced = sorted(
                    defect_id
                    for defect_id, ok in status.items()
                    if not ok and parent_status.get(defect_id, True)
                )
            candidate = {
                "version": version,
                "outcome": dict(outcome),
                "rank_key": list(_rank_key(outcome)),
                "tree_hash": tree_hash,
                "parent_tree_hash": parent_hash,
                "parent_version": previous,
                "target_hashes": hashes,
                "resolved_defects": resolved,
                "introduced_defects": introduced,
            }
            self._candidates[version] = candidate
            self._append({"kind": "candidate", **candidate})

        self._guarded("candidate", _record)

    def plan_accepted(
        self,
        version: int,
        *,
        source: str | None,
        nodes: list[dict[str, Any]],
        coverage_writeback: Any,
        plan_emission: Any,
        repair_declines: list[dict[str, Any]],
        seams_bypassed: dict[str, Any] | None,
    ) -> None:
        """Record a re-plan's acceptance as the next plan version.

        The fills the re-plan produced are attributed to the version
        they built. The brief's write-backs (declared clause
        coverage, plan emission) ride along verbatim — they are the
        planner's own claim about the delta, next to which the
        plan_written binding of the accepted plan is the fact.
        """

        def _record() -> None:
            self._attribute_pending_fills(version)
            acceptance = {
                "kind": "plan_accepted",
                "version": version,
                "source": source,
                "nodes": nodes,
                "coverage_writeback": coverage_writeback,
                "plan_emission": plan_emission,
                "repair_declines": repair_declines,
                "seams_bypassed": seams_bypassed,
            }
            self._acceptances.append(acceptance)
            self._append(acceptance)

        self._guarded("plan_accepted", _record)

    def plan_rejected(
        self,
        after_version: int,
        *,
        source: str | None,
        rejection: str,
        repair_declines: list[dict[str, Any]],
    ) -> None:
        """Record a delta plan the judge rejected (no new version)."""

        def _record() -> None:
            acceptance = {
                "kind": "plan_rejected",
                "after_version": after_version,
                "source": source,
                "rejection": rejection,
                "repair_declines": repair_declines,
            }
            self._acceptances.append(acceptance)
            self._append(acceptance)

        self._guarded("plan_rejected", _record)

    def shipping_settled(
        self,
        shipping: dict[str, Any],
        outcomes: list[dict[str, Any]],
    ) -> None:
        """Record the best-version shipping settlement, with ranks."""

        def _record() -> None:
            self._shipping_event = {
                "kind": "shipping",
                "shipping": shipping,
                "rank_keys": {
                    str(outcome.get("version")): list(_rank_key(outcome))
                    for outcome in outcomes
                },
            }
            self._append(self._shipping_event)

        self._guarded("shipping", _record)

    # ── assembly ─────────────────────────────────────────────────────

    def finish(
        self,
        *,
        shipping: dict[str, Any] | None,
        escalation: str | None,
        decision_history: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        goal_verdict: str,
        goal_detail: str,
        plan_versions: int,
    ) -> None:
        """Assemble and write lineage.json + lineage.md (idempotent)."""

        def _record() -> None:
            if self._finalized:
                return
            self._finish_info = {
                "shipping": shipping,
                "escalation": escalation,
                "decision_history": decision_history,
                "attempts": attempts,
                "goal_verdict": goal_verdict,
                "goal_detail": goal_detail,
                "plan_versions": plan_versions,
            }
            self._assemble_and_write()

        self._guarded("finish", _record)

    def finalize(self) -> None:
        """Assemble from whatever exists (the crash-path backstop).

        LoopRunner.run_plan calls this in a finally block; when
        _finish_run already assembled the record via finish(), this
        is a no-op.
        """

        def _record() -> None:
            if self._finalized:
                return
            if self._finish_info is None:
                self._finish_info = {
                    "shipping": (
                        self._shipping_event["shipping"]
                        if self._shipping_event
                        else None
                    ),
                    "escalation": None,
                    "decision_history": [],
                    "attempts": [],
                    "goal_verdict": "",
                    "goal_detail": "",
                    "plan_versions": max(self._candidates, default=0),
                }
            self._assemble_and_write()

        self._guarded("finalize", _record)

    def _shipped_version(self) -> int | None:
        info = self._finish_info or {}
        shipping = info.get("shipping")
        if isinstance(shipping, dict) and shipping.get("shipped_version"):
            return int(shipping["shipped_version"])
        if self._candidates:
            return max(self._candidates)
        return None

    def _defect_generated_diff(
        self, defect: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Did any post-diagnosis version actually change anything?

        Two independent witnesses: (a) a fill attributed to a plan
        version after the diagnosis, for a node that version's plan
        bound to the defect's clause as an implementer, whose
        payload changed content (whole content differing from the
        file as it stood, scoped edits, or an anchored insert); and
        (b) the defect's own file changing hash between consecutive
        version snapshots after the diagnosis. Either suffices —
        (b) also catches bash-node writes the fill channel never
        sees.
        """
        diagnosed = defect["first_diagnosed_version"]
        clause_id = defect["clause_id"]
        evidence: list[dict[str, Any]] = []
        fills_by_version: dict[int, list[dict[str, Any]]] = {}
        for fill in self._fills:
            if fill.get("kind") == "fill_outcome":
                fills_by_version.setdefault(
                    fill.get("plan_version"), []
                ).append(fill)
        for version in sorted(self._plans):
            if version <= diagnosed:
                continue
            binding = self._plans[version]["defect_bindings"].get(
                defect["defect_id"]
            )
            if not binding:
                continue
            implementers = set(binding["implement_nodes"])
            for fill in fills_by_version.get(version, []):
                if fill.get("node_id") not in implementers:
                    continue
                if fill.get("outcome") != "accepted":
                    continue
                changed = (
                    fill.get("content_equals_existing") is False
                    or (fill.get("edits_count") or 0) > 0
                    or bool(fill.get("has_anchor"))
                    or fill.get("materialized_from")
                    in ("scoped_edits", "anchored_edit")
                )
                if changed:
                    evidence.append(
                        {
                            "version": version,
                            "witness": "fill",
                            "node_id": fill.get("node_id"),
                            "target": fill.get("target"),
                        }
                    )
        defect_file = defect.get("file")
        if defect_file:
            versions = sorted(self._candidates)
            for index, version in enumerate(versions):
                if version <= diagnosed:
                    continue
                hashes = self._candidates[version]["target_hashes"]
                if index == 0:
                    previous_hashes = self._baseline_hashes or {}
                else:
                    previous_hashes = self._candidates[versions[index - 1]][
                        "target_hashes"
                    ]
                current = _hash_for_file(hashes, defect_file)
                previous = _hash_for_file(previous_hashes, defect_file)
                if current != previous:
                    evidence.append(
                        {
                            "version": version,
                            "witness": "snapshot",
                            "file": defect_file,
                            "from_sha256": previous,
                            "to_sha256": current,
                        }
                    )
        return bool(evidence), evidence

    def _assemble(self) -> dict[str, Any]:
        shipped = self._shipped_version()
        shipped_status = (
            self._probe_status.get(shipped, {}) if shipped else {}
        )
        defects: dict[str, Any] = {}
        table: list[dict[str, Any]] = []
        for defect_id in sorted(
            self._defects,
            key=lambda d: (
                self._defects[d]["first_diagnosed_version"],
                self._defects[d]["clause_id"],
                d,
            ),
        ):
            defect = self._defects[defect_id]
            diagnosed = defect["first_diagnosed_version"]
            planned_version: int | None = None
            planned_nodes: dict[str, Any] | None = None
            for version in sorted(self._plans):
                if version <= diagnosed:
                    continue
                binding = self._plans[version]["defect_bindings"].get(
                    defect_id
                )
                if binding:
                    planned_version = version
                    planned_nodes = binding
                    break
            scheduled = False
            scheduled_evidence: list[dict[str, Any]] = []
            for version in sorted(self._plans):
                if version <= diagnosed:
                    continue
                binding = self._plans[version]["defect_bindings"].get(
                    defect_id
                )
                if not binding:
                    continue
                bound = set(binding["implement_nodes"]) | set(
                    binding["verify_nodes"]
                )
                for execution in self._executions.get(version, []):
                    for node in execution["nodes"]:
                        if node["id"] in bound and node.get(
                            "status_after"
                        ) in ("completed", "failed"):
                            scheduled = True
                            scheduled_evidence.append(
                                {
                                    "version": version,
                                    "node_id": node["id"],
                                    "status_after": node["status_after"],
                                }
                            )
            generated, generated_evidence = self._defect_generated_diff(
                defect
            )
            verified_version: int | None = None
            for version in sorted(self._probe_status):
                if version <= diagnosed:
                    continue
                if self._probe_status[version].get(defect_id):
                    verified_version = version
                    break
            shipped_fixed = bool(shipped_status.get(defect_id))
            stages = [
                ("planned", planned_version is not None),
                ("scheduled", scheduled),
                ("generated_diff", generated),
                ("verified_fixed", verified_version is not None),
                ("shipped_fixed", shipped_fixed),
            ]
            first_no = next(
                (name for name, yes in stages if not yes), None
            )
            row = {
                "defect_id": defect_id,
                "clause_id": defect["clause_id"],
                "probe": defect["probe"],
                "flow": defect["flow"],
                "file": defect["file"],
                "diagnosed": True,
                "diagnosed_version": diagnosed,
                "planned": planned_version is not None,
                "planned_version": planned_version,
                "planned_nodes": planned_nodes,
                "scheduled": scheduled,
                "scheduled_evidence": scheduled_evidence,
                "generated_diff": generated,
                "generated_diff_evidence": generated_evidence,
                "verified_fixed": verified_version is not None,
                "verified_fixed_version": verified_version,
                "shipped_fixed": shipped_fixed,
                "first_no_transition": first_no,
                "diagnosis_detail": defect["diagnosis_detail"],
                "last_failing_version": defect.get("last_failing_version"),
                "last_failing_detail": defect.get("last_failing_detail"),
            }
            defects[defect_id] = row
            table.append(row)
        info = self._finish_info or {}
        shipping = info.get("shipping")
        shipped_tree_hash = None
        if shipped is not None and shipped in self._candidates:
            shipped_tree_hash = self._candidates[shipped]["tree_hash"]
        return {
            "run_id": self._run_id,
            "build": "build-29-lineage",
            "shipped_version": shipped,
            "shipped_tree_hash": shipped_tree_hash,
            "shipping": shipping,
            "shipping_settled": self._shipping_event is not None,
            "goal_verdict": info.get("goal_verdict"),
            "escalation": info.get("escalation"),
            "decision_history": info.get("decision_history", []),
            "candidates": [
                self._candidates[v] for v in sorted(self._candidates)
            ],
            "defects": defects,
            "table": table,
            "fills": self._fills,
            "acceptances": self._acceptances,
        }

    def _assemble_and_write(self) -> None:
        if self._pending_fills:
            # Fills no accepted plan ever claimed (a rejected delta's
            # fills, or a run that ended mid-re-plan): attribute them
            # to no version, but keep them in the record.
            self._attribute_pending_fills(None)
        record = self._assemble()
        self._run_store.write_lineage(self._run_id, record)
        self._run_store.write_lineage_table(
            self._run_id, render_lineage_table(record)
        )
        self._append(
            {
                "kind": "lineage_assembled",
                "defects": len(record["table"]),
                "shipped_version": record["shipped_version"],
            }
        )
        self._finalized = True


def _hash_for_file(
    target_hashes: dict[str, str | None], repo_relative: str
) -> str | None:
    """The hash of one repo-relative file in a target-hash map.

    Snapshot targets are recorded as the plan named them (often
    absolute paths); probe records name files repo-relative. Match
    exactly, else by path suffix.
    """
    if repo_relative in target_hashes:
        return target_hashes[repo_relative]
    suffix = "/" + repo_relative
    for target, value in target_hashes.items():
        if target.endswith(suffix):
            return value
    return None


def render_lineage_table(record: dict[str, Any]) -> str:
    """Render the per-defect lineage table (lineage.md)."""
    lines = [
        f"# Defect lineage — {record.get('run_id')}",
        "",
        f"Shipped version: {record.get('shipped_version')} "
        f"(tree {record.get('shipped_tree_hash')})",
        "",
        "| Defect | Diagnosed | Planned | Scheduled | Generated diff "
        "| Verified fixed | Shipped fixed |",
        "|---|---|---|---|---|---|---|",
    ]
    table = record.get("table") or []
    if not table:
        lines.append("")
        lines.append(
            "No defects were diagnosed in this run (no failing "
            "contract-probe records)."
        )
    for row in table:
        def _cell(yes: bool, version: Any = None) -> str:
            if not yes:
                return "No"
            return f"Yes (v{version})" if version is not None else "Yes"

        lines.append(
            "| {defect} | {diagnosed} | {planned} | {scheduled} "
            "| {generated} | {verified} | {shipped} |".format(
                defect=row["defect_id"],
                diagnosed=_cell(True, row["diagnosed_version"]),
                planned=_cell(row["planned"], row["planned_version"]),
                scheduled=_cell(row["scheduled"]),
                generated=_cell(row["generated_diff"]),
                verified=_cell(
                    row["verified_fixed"], row["verified_fixed_version"]
                ),
                shipped=_cell(row["shipped_fixed"]),
            )
        )
    lines.append("")
    lines.append("First Yes->No transition per defect:")
    for row in table:
        transition = row["first_no_transition"] or "none — shipped fixed"
        lines.append(f"- {row['defect_id']}: {transition}")
    lines.append("")
    return "\n".join(lines)
