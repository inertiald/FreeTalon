"""Experiment memory (loop Stage 5/6, M2): append-only, cross-run.

Every measured node writes one experiment record: hypothesis, approach,
measurement, the judge's decision, and a lesson written at judge time from
the measurements — one or two sentences, factual, with the numbers
attached. The model may phrase a lesson; it may not invent its content,
so lessons here are generated deterministically from the measurement.

Storage is SQLite, in the same database file as the execution-plan store
(``ExecutionPlanStateStore``) — the ``experiments`` table sits next to the
``execution_plans`` table. Nothing is deleted: refuted hypotheses are the
most valuable records in the store. Retention and any pruning are Pat's
call, made outside the loop.

Retrieval is by goal (a hash of the objective) and domain (the intent's
project type). A finance run does not silently import ECU lessons or vice
versa; shared low-level lessons are tagged with domain ``"infrastructure"``
and visible to all goals.

An approach's identity — for repeat detection — is its substance, not
its wording: the mechanism a node used and what it acted on (see
``approach_signature``). Until the cycle-3 fix the identity was
(capability, hypothesis-or-objective text), and the 2026-11-17 live
matrix run was blocked before executing a single node because its
generic "Verify /health live" step text-matched a historical record
whose curl had failed only because that earlier plan never started a
server. Two guards now keep that class of false repeat out:

* universal plumbing — running the test suite, fetching a URL to check
  it, starting a server — has no signature at all: every plan for a
  goal shares those steps, and their failures are symptoms of the
  substantive steps, not approaches that were tried;
* a failure counts only at the "executed" stage: the approach was
  actually tried — the node ran and was measured, or it reached its
  approval gate and was refused there. A failure recorded at planning
  time (the approach was never executed) is not a measured failure
  and cannot block a later plan that merely resembles the
  unplannable output.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generator

from .measure import measurement_failed_detail
from .state_store import default_db_path

_DEFAULT_DB_PATH = Path.home() / "freetalon-workspace" / "freetalon_state.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    goal_key TEXT NOT NULL,
    domain TEXT NOT NULL,
    node_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    approach_json TEXT NOT NULL,
    measurement_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    lesson TEXT NOT NULL
)
"""

# Input keys that never identify an approach (volatile or environmental).
_VOLATILE_INPUT_KEYS = {"workdir", "timeout_s"}


def goal_key_for(objective: str) -> str:
    """Stable, short key for a goal: hash of the normalized objective."""
    normalized = " ".join(objective.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def approach_for(node: Any) -> dict[str, Any]:
    """The approach a node took: capability + key params + data window."""
    inputs = dict(getattr(node, "inputs", {}) or {})
    key_params = {
        k: v
        for k, v in inputs.items()
        if k not in _VOLATILE_INPUT_KEYS and not isinstance(v, (dict, list))
    }
    return {
        "capability": getattr(node, "assigned_claw", "") or "",
        "key_params": key_params,
        "data_window": str(inputs.get("data_window", "")),
    }


# ── Approach signatures ──────────────────────────────────────────────
#
# The signature is computed from the recorded approach dict (see
# approach_for), so a stored record and a live candidate node produce
# the identical signature by construction — one conversion function,
# both sides. Forms:
#
#   (claw, "write", (target, ...))    a file write/edit: the claw and
#                                     the files it writes. Content is
#                                     NOT part of the identity — the
#                                     content is what a repair varies;
#                                     mechanism + targets are the
#                                     approach that was tried.
#   ("bash", "command", text)         a substantive shell command, by
#                                     its whitespace-normalized text.
#   (claw, "params", params, window)  any other claw, by its recorded
#                                     scalar parameters.
#
# A plumbing node returns None (no signature): it cannot create a
# failed-approach entry and can never match one.

# Input keys that are execution OUTPUTS, not approach inputs: the
# executor merges results (exit code, captured output) into a bash
# node's inputs before the node is recorded, so a stored approach can
# carry them. They never identify an approach.
_OUTPUT_INPUT_KEYS = frozenset({"exit_code", "stdout", "stderr", "duration_s"})

_WRITE_CLAWS = frozenset({"scaffold", "file_writer"})

# Shell words that are neutral inside a compound command: shell
# bookkeeping (cd, export, loop control) and read-only filters
# (grep/head/tail and friends — they only shape what another segment
# produced). A command made ONLY of neutral segments still gets its
# full-text signature: a lone read-only check is that node's whole
# act, and only its literal repeat can match it. Neutrality only
# decides that a filter riding on a plumbing segment (curl | grep)
# does not make the pipeline substantive.
_NEUTRAL_SEGMENT_WORDS = frozenset(
    {
        "cd", "export", "declare", "echo", "printf", "sleep", "set",
        "source", ".", "for", "do", "done", "while", "until", "if",
        "then", "else", "fi", "{", "}", ":", "wait", "pushd", "popd",
        "umask", "break", "continue",
        "grep", "egrep", "fgrep", "head", "tail", "cat", "awk", "sed",
        "sort", "uniq", "wc", "cut", "tr", "jq", "ls", "stat", "pwd",
    }
)

_INTERPRETER_WORDS = frozenset(
    {"python", "python3", "node", "bash", "sh", "ruby", "perl"}
)

_TEST_RUNNER_RE = re.compile(r"\b(?:unittest|pytest|py\.test|nosetests)\b")
_TEST_SUITE_RE = re.compile(r"\b(?:npm|yarn|pnpm|make|go|cargo)\s+test\b")
_SERVER_SEGMENT_RE = re.compile(
    r"(?:\buvicorn\b|\bgunicorn\b|\bhypercorn\b|\bdaphne\b|waitress"
    r"|http\.server|\bflask\s+run\b|\brunserver\b|\brails\s+server\b"
    r"|\bnpm\s+start\b|\bcli\s+(?:start|serve|stop|restart)\b"
    r"|\bdocker(?:-|\s+)compose\s+up\b)"
)
_SCRIPT_TOKEN_RE = re.compile(r"[^\s;|&]+\.(?:py|sh|js|rb|pl)\b")
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_REDIRECT_TARGET_RE = re.compile(r">>?\s*(?![&\d])(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
_TEE_TARGET_RE = re.compile(r"\btee\s+(?:-\w+\s+)*(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
_DD_TARGET_RE = re.compile(r"\bof=(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
_DOWNLOAD_TARGET_RE = re.compile(
    r"(?:^|\s)(?:-o|-O|--output)[=\s]+(\"[^\"]+\"|'[^']+'|[^\s;|&]+)"
)
_COPY_TARGET_RE = re.compile(
    r"^(?:cp|mv)\s+(?:.*\s)?(\"[^\"]+\"|'[^']+'|\S+)\s*$"
)
_TOUCH_TARGET_RE = re.compile(r"^touch\s+(?:-\w+\s+)*(\"[^\"]+\"|'[^']+'|\S+)\s*$")
_SED_TARGET_RE = re.compile(
    r"^sed\s+(?:-\w+\s+)*-[a-zA-Z]*i[a-zA-Z]*(?:\s+\S+)*?\s+(\"[^\"]+\"|'[^']+'|\S+)\s*$"
)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _bash_write_targets(command: str) -> tuple[str, ...]:
    """Files a shell command writes, conservatively extracted.

    Covers the forms plans actually use: output redirects (including
    heredoc writes), tee, dd of=, download-to-file, cp/mv/touch, and
    in-place sed. A miss here is safe — the command falls back to its
    full-text signature, which is still substance, just finer.
    """
    targets: set[str] = set()
    for match in _REDIRECT_TARGET_RE.finditer(command):
        target = _unquote(match.group(1))
        if target and target != "/dev/null":
            targets.add(target)
    for regex in (_TEE_TARGET_RE, _DD_TARGET_RE, _DOWNLOAD_TARGET_RE):
        for match in regex.finditer(command):
            target = _unquote(match.group(1))
            if target and target != "/dev/null":
                targets.add(target)
    for segment in _SEGMENT_SPLIT_RE.split(command):
        segment = segment.strip()
        for regex in (_COPY_TARGET_RE, _TOUCH_TARGET_RE, _SED_TARGET_RE):
            match = regex.match(segment)
            if match:
                target = _unquote(match.group(1))
                if target and target != "/dev/null":
                    targets.add(target)
    return tuple(sorted(targets))


def _segment_program(segment: str) -> tuple[str, list[str]]:
    """(program basename, tokens) for one shell segment.

    Leading environment assignments and wrappers (sudo/time) are
    skipped; an assignment-only segment has no program.
    """
    tokens = segment.split()
    while tokens and (
        _ENV_ASSIGN_RE.match(tokens[0]) or tokens[0] in ("sudo", "time")
    ):
        tokens = tokens[1:]
    if not tokens:
        return "", []
    return tokens[0].rsplit("/", 1)[-1], tokens


def _is_test_script_token(token: str) -> bool:
    basename = token.rsplit("/", 1)[-1].lower()
    return "test" in basename and basename.rsplit(".", 1)[-1] in (
        "py", "sh", "js", "rb", "pl",
    )


def _segment_kind(segment: str) -> str:
    """Classify one shell segment: plumbing, neutral, or substantive."""
    program, tokens = _segment_program(segment)
    if not program:
        return "neutral"
    if program in _NEUTRAL_SEGMENT_WORDS:
        return "neutral"
    text = " ".join(tokens)
    if program in ("curl", "wget"):
        return "plumbing"  # fetching a URL to check it
    if _TEST_RUNNER_RE.search(text) or _TEST_SUITE_RE.search(text):
        return "plumbing"  # running the test suite
    if _SERVER_SEGMENT_RE.search(text):
        return "plumbing"  # starting/stopping a server
    if _is_test_script_token(tokens[0]):
        return "plumbing"  # executing a test script directly
    if (
        program in _INTERPRETER_WORDS or program.startswith("python")
    ) and len(tokens) > 1 and _is_test_script_token(tokens[1]):
        return "plumbing"  # interpreter running a test script
    return "substantive"


def _bash_signature(command: str) -> tuple[Any, ...] | None:
    """The signature of a bash node, from its command text."""
    targets = _bash_write_targets(command)
    if targets:
        return ("bash", "write", targets)
    saw_plumbing = False
    for segment in _SEGMENT_SPLIT_RE.split(command):
        kind = _segment_kind(segment)
        if kind == "substantive":
            return ("bash", "command", " ".join(command.split()))
        saw_plumbing = saw_plumbing or kind == "plumbing"
    if saw_plumbing:
        return None
    return ("bash", "command", " ".join(command.split()))


def signature_from_approach(approach: dict[str, Any]) -> tuple[Any, ...] | None:
    """The approach signature of a recorded approach dict.

    Returns None for universal plumbing (see the section header).
    Objective/hypothesis text is never consulted: two nodes that word
    the same step differently but do the same thing share a signature;
    identical wording over a different mechanism does not.
    """
    capability = str(approach.get("capability", "") or "")
    key_params = {
        k: v
        for k, v in (approach.get("key_params") or {}).items()
        if k not in _OUTPUT_INPUT_KEYS
    }
    if capability in _WRITE_CLAWS:
        target = key_params.get("file_path") or key_params.get("path") or ""
        return (capability, "write", (str(target),))
    if capability == "bash":
        return _bash_signature(str(key_params.get("command", "") or ""))
    params = tuple(
        sorted((str(k), str(v)) for k, v in key_params.items())
    )
    return (capability, "params", params, str(approach.get("data_window", "")))


def approach_signature(node: Any) -> tuple[Any, ...] | None:
    """The approach signature of a live node (None for plumbing)."""
    return signature_from_approach(approach_for(node))


def write_targets_for_node(node: Any) -> tuple[str, ...]:
    """The files a node writes, read off its approach signature.

    A write-form signature — (claw, "write", (target, ...)) for the
    write claws, ("bash", "write", targets) for a shell command the
    extractor proved writes files — carries the targets as its third
    element. Anything else (a substantive command, a params call,
    plumbing with no signature) writes nothing this function can
    prove, so it contributes no targets.
    """
    signature = approach_signature(node)
    if signature is not None and len(signature) >= 3 and signature[1] == "write":
        return tuple(str(target) for target in signature[2])
    return ()


def write_targets_for_nodes(nodes: Any) -> list[str]:
    """The sorted union of write targets across a node list."""
    targets: set[str] = set()
    for node in nodes or []:
        targets.update(write_targets_for_node(node))
    return sorted(targets)


def describe_signature(signature: tuple[Any, ...]) -> str:
    """A human description of an approach signature, for messages."""
    capability, form = str(signature[0]), str(signature[1])
    if form == "write":
        targets = ", ".join(str(t) for t in signature[2]) or "(no target)"
        return f"{capability!r} write of {targets}"
    if form == "command":
        text = str(signature[2])
        if len(text) > 80:
            text = text[:77] + "..."
        return f"{capability!r} command {text!r}"
    return f"{capability!r} approach (recorded parameters)"


# ── Approach payloads ────────────────────────────────────────────────
#
# The signature deliberately excludes content — but the record keeps
# it: a write claw's content rides in key_params (it is a scalar, so
# approach_for keeps it), and a bash node's command likewise. The
# payload is what the repeat gate's substance check compares (see
# judge.assess_approach_repeat): a signature match says the same
# mechanism touched the same targets; the payload says whether this
# attempt is the same substance that was measured, or a different
# one. Extraction mirrors the signature's own construction — one
# conversion (approach_for) feeds both sides, stored record and live
# node alike.


def approach_payload(approach: dict[str, Any]) -> str | None:
    """The substantive payload a recorded approach carried, if any.

    Write claws: the content they wrote (``content``, else
    ``file_content`` — the critic's own precedence). Bash: the
    command text. Any other claw's identity already IS its recorded
    parameters (the params-form signature), so it has no separate
    payload and returns None, as does a write whose content was not
    recorded (e.g. it arrived as a non-scalar input approach_for
    drops).
    """
    capability = str(approach.get("capability", "") or "")
    key_params = approach.get("key_params") or {}
    if capability in _WRITE_CLAWS:
        for key in ("content", "file_content"):
            value = key_params.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None
    if capability == "bash":
        value = key_params.get("command")
        if isinstance(value, str) and value.strip():
            return value
    return None


def node_payload(node: Any) -> str | None:
    """The substantive payload a live node carries, if any."""
    return approach_payload(approach_for(node))


def normalize_payload(text: str) -> str:
    """Whitespace-normalized payload text for identity comparison.

    The same normalization the bash signature applies to command
    text (" ".join(split())): two payloads that differ only in
    layout are the same substance; anything else is a different
    payload, however similar it reads.
    """
    return " ".join(text.split())


def _stage_for_measurement(measurement: dict[str, Any]) -> str:
    """The failure stage a measurement belongs to.

    "executed" when the node actually ran and at least one criterion
    was measured; "planning" when nothing was ever executed (the
    approach was rejected or abandoned before it could be tried).
    Accepts both measurement shapes: a live executor measurement
    (criteria under "results") and a stored record's blob (under
    "criteria").

    A measurement with NO criterion results at all was decided during
    the execution pass, not by the measurer: the only such producer
    is the approval-gate path (denied, or expired = deny on timeout),
    where the approach was fully tried and refused at the cliff edge.
    That is executed-stage — a denied irreversible action must never
    be silently re-asked (the M4 gate contract).
    """
    results = measurement.get("results") or measurement.get("criteria") or []
    if not results:
        return "executed"
    for result in results:
        if isinstance(result, dict) and result.get("measured"):
            return "executed"
    return "planning"


def lesson_for(node: Any, measurement: dict[str, Any], decision: str) -> str:
    """A factual lesson from a measurement. Deterministic, no invention."""
    verdict = measurement.get("verdict", "?")
    detail = measurement_failed_detail(measurement)
    exit_code = measurement.get("exit_code")
    duration = measurement.get("duration_s")
    hypothesis = getattr(node, "hypothesis", "") or getattr(node, "objective", "")
    parts = [
        f"{getattr(node, 'assigned_claw', '') or node.id} "
        f"({hypothesis}): verdict {verdict} — {detail}"
    ]
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    if duration is not None:
        parts.append(f"{duration}s")
    parts.append(f"decision {decision}")
    return "; ".join(parts) + "."


class ExperimentStore:
    """Append-only SQLite store for experiment records.

    Each public method opens and closes its own database connection, the
    same pattern as :class:`ExecutionPlanStateStore`. A lock serializes
    writes.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = (
            Path(db_path) if db_path is not None else default_db_path(_DEFAULT_DB_PATH)
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE_SQL)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        try:
            yield conn
        finally:
            conn.close()

    # ── Writes ───────────────────────────────────────────────────────────

    def record(
        self,
        *,
        run_id: str,
        goal_key: str,
        domain: str,
        node: Any,
        measurement: dict[str, Any],
        decision: str,
        lesson: str | None = None,
    ) -> dict[str, Any]:
        """Append one experiment record. Returns the record dict."""
        record = {
            "run_id": run_id,
            "goal_key": goal_key,
            "domain": domain,
            "node_id": getattr(node, "id", "?"),
            "ts": datetime.now(UTC).isoformat(),
            # The hypothesis is kept for the lesson and for human
            # readers; the approach identity used by repeat detection
            # is the substance signature (approach_signature), computed
            # from the approach dict below — never from this text.
            "hypothesis": getattr(node, "hypothesis", "")
            or getattr(node, "objective", ""),
            "approach": approach_for(node),
            "measurement": {
                "verdict": measurement.get("verdict"),
                "criteria": measurement.get("results", []),
                "metrics": measurement.get("metrics", {}),
                # The failure stage: "executed" when the node ran and
                # was measured, "planning" when it never got to run.
                # Only executed failures are measured failures (see
                # failed_approaches).
                "stage": _stage_for_measurement(measurement),
            },
            "decision": decision,
            "lesson": lesson if lesson is not None else lesson_for(node, measurement, decision),
        }
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO experiments
                   (run_id, goal_key, domain, node_id, ts, hypothesis,
                    approach_json, measurement_json, decision, lesson)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["run_id"],
                    record["goal_key"],
                    record["domain"],
                    record["node_id"],
                    record["ts"],
                    record["hypothesis"],
                    json.dumps(record["approach"], sort_keys=True, default=str),
                    json.dumps(record["measurement"], sort_keys=True, default=str),
                    record["decision"],
                    record["lesson"],
                ),
            )
            conn.commit()
            record["id"] = int(cursor.lastrowid)
            return record

    # ── Reads ────────────────────────────────────────────────────────────

    def experiments_for_goal(
        self, goal_key: str, domain: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Past experiments for a goal (plus shared infrastructure lessons)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT run_id, goal_key, domain, node_id, ts, hypothesis,
                          approach_json, measurement_json, decision, lesson
                   FROM experiments
                   WHERE (goal_key = ? AND domain = ?)
                      OR domain = 'infrastructure'
                   ORDER BY id DESC LIMIT ?""",
                (goal_key, domain, limit),
            )
            return [self._row_to_record(row) for row in cursor.fetchall()]

    def executed_approach_records(
        self, goal_key: str, domain: str
    ) -> list[dict[str, Any]]:
        """Executed-stage records with a substance signature, oldest first.

        Each entry carries the record's signature, its approach dict
        (payload included — see approach_payload), verdict, run id,
        node id, timestamp and lesson. This is the evidence the
        repeat gate's substance check adjudicates against (see
        judge.assess_approach_repeat): failed_approaches below is
        the FAIL-only view of the same set. A record contributes
        only when its stage is "executed" (the approach was actually
        tried) and its approach has a signature (plumbing has none).
        Records written before stages were stored derive the stage
        from their criteria the same way (see _stage_for_measurement).
        """
        records: list[dict[str, Any]] = []
        for record in reversed(
            self.experiments_for_goal(goal_key, domain, limit=500)
        ):
            measurement = record["measurement"]
            stage = measurement.get("stage") or _stage_for_measurement(measurement)
            if stage != "executed":
                continue
            signature = signature_from_approach(record["approach"])
            if signature is None:
                continue
            records.append(
                {
                    "signature": signature,
                    "approach": record["approach"],
                    "verdict": measurement.get("verdict"),
                    "run_id": record["run_id"],
                    "node_id": record["node_id"],
                    "ts": record["ts"],
                    "lesson": record["lesson"],
                }
            )
        return records

    def failed_approaches(
        self, goal_key: str, domain: str
    ) -> set[tuple[Any, ...]]:
        """Signatures of approaches TRIED and measured FAIL for this goal.

        A record contributes only when all three hold: its verdict is
        FAIL; its stage is "executed" (the node actually ran and was
        measured — a planning-stage failure means the approach was
        never tried, however similar its shape); and its approach has
        a substance signature (plumbing — test runs, live checks,
        server starts — has none and is never an approach). Records
        written before stages were stored derive the stage from their
        criteria the same way (see _stage_for_measurement).
        """
        return {
            record["signature"]
            for record in self.executed_approach_records(goal_key, domain)
            if record["verdict"] == "FAIL"
        }

    def count(self) -> int:
        """Total experiment records (append-only; nothing is deleted)."""
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM experiments")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _row_to_record(row: tuple) -> dict[str, Any]:
        (run_id, goal_key, domain, node_id, ts, hypothesis,
         approach_json, measurement_json, decision, lesson) = row
        return {
            "run_id": run_id,
            "goal_key": goal_key,
            "domain": domain,
            "node_id": node_id,
            "ts": ts,
            "hypothesis": hypothesis,
            "approach": json.loads(approach_json),
            "measurement": json.loads(measurement_json),
            "decision": decision,
            "lesson": lesson,
        }
