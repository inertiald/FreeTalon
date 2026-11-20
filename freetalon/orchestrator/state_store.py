"""SQLite-backed persistence store for ExecutionPlan objects.

Provides a lightweight, restart-safe local store so the orchestrator can
recover in-flight plans after a process restart without any external services.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from .models import ExecutionPlan

_DEFAULT_DB_PATH = Path("freetalon_state.db")

# Tests and sandboxed runs point this at a temp file so constructing a
# store never writes to the live default path (the suite once created
# ~/freetalon-workspace/freetalon_state.db on a user system this way).
STATE_DB_PATH_ENV = "FREETALON_STATE_DB_PATH"


def default_db_path(fallback: Path) -> Path:
    """Resolve a store's default DB path, honoring the env override."""
    override = os.environ.get(STATE_DB_PATH_ENV)
    return Path(override).expanduser() if override else fallback

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL
)
"""


class ExecutionPlanStateStore:
    """Local SQLite store for :class:`ExecutionPlan` objects.

    Each public method opens and closes its own database connection, which
    is the safest pattern for multi-threaded access to a single SQLite file.
    A :class:`threading.Lock` serializes writes to prevent concurrent
    modification.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database file.  Defaults to
        ``freetalon_state.db`` in the current working directory, or to
        ``$FREETALON_STATE_DB_PATH`` when that is set.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = (
            Path(db_path) if db_path is not None else default_db_path(_DEFAULT_DB_PATH)
        )
        self._write_lock = threading.Lock()
        # Initialise the schema on first use.
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE_SQL)

    @property
    def db_path(self) -> Path:
        """Filesystem path of the SQLite file (M2: memory shares this file)."""
        return self._db_path

    # ── Internal helpers ──────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a short-lived connection, yield it, then close it."""
        conn = sqlite3.connect(str(self._db_path))
        try:
            yield conn
        finally:
            conn.close()

    # ── Public API ────────────────────────────────────────────────────────

    def save(self, plan: ExecutionPlan) -> None:
        """Upsert *plan* into the store (insert or replace by plan_id).

        Parameters
        ----------
        plan:
            The :class:`ExecutionPlan` to persist.
        """
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO execution_plans (plan_id, plan_json) VALUES (?, ?)",
                (plan.plan_id, plan.model_dump_json()),
            )
            conn.commit()

    def load(self, plan_id: str) -> Optional[ExecutionPlan]:
        """Return the :class:`ExecutionPlan` for *plan_id*, or ``None`` if absent.

        Parameters
        ----------
        plan_id:
            Identifier of the plan to retrieve.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT plan_json FROM execution_plans WHERE plan_id = ?",
                (plan_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ExecutionPlan.model_validate_json(row[0])

    def delete(self, plan_id: str) -> bool:
        """Remove the plan identified by *plan_id*.

        Returns
        -------
        bool
            ``True`` if a record was deleted, ``False`` if it did not exist.
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM execution_plans WHERE plan_id = ?",
                (plan_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_ids(self) -> list[str]:
        """Return a list of all stored plan IDs ordered by insertion time."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT plan_id FROM execution_plans ORDER BY rowid"
            )
            return [row[0] for row in cursor.fetchall()]

