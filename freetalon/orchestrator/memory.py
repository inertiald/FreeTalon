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
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generator

from .measure import measurement_failed_detail

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
        self._db_path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
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
            # The approach identity is (capability, hypothesis-or-objective),
            # matching judge.approach_key, so memory lookups line up.
            "hypothesis": getattr(node, "hypothesis", "")
            or getattr(node, "objective", ""),
            "approach": approach_for(node),
            "measurement": {
                "verdict": measurement.get("verdict"),
                "criteria": measurement.get("results", []),
                "metrics": measurement.get("metrics", {}),
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

    def failed_approaches(
        self, goal_key: str, domain: str
    ) -> set[tuple[str, str]]:
        """(capability, hypothesis) pairs that measured FAIL for this goal."""
        failed: set[tuple[str, str]] = set()
        for record in self.experiments_for_goal(goal_key, domain, limit=500):
            if record["measurement"].get("verdict") == "FAIL":
                approach = record["approach"]
                failed.add(
                    (str(approach.get("capability", "")), str(record["hypothesis"]))
                )
        return failed

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
