"""SQLite-backed persistence store for ExecutionPlan objects.

Provides a lightweight, restart-safe local store so the orchestrator can
recover in-flight plans after a process restart without any external services.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .models import ExecutionPlan

_DEFAULT_DB_PATH = Path("freetalon_state.db")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL
)
"""


class ExecutionPlanStateStore:
    """Local SQLite store for :class:`ExecutionPlan` objects.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database file.  Defaults to
        ``freetalon_state.db`` in the current working directory.
    """

    def __init__(self, db_path: Path | str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def save(self, plan: ExecutionPlan) -> None:
        """Upsert *plan* into the store (insert or replace by plan_id).

        Parameters
        ----------
        plan:
            The :class:`ExecutionPlan` to persist.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO execution_plans (plan_id, plan_json) VALUES (?, ?)",
            (plan.plan_id, plan.model_dump_json()),
        )
        self._conn.commit()

    def load(self, plan_id: str) -> Optional[ExecutionPlan]:
        """Return the :class:`ExecutionPlan` for *plan_id*, or ``None`` if absent.

        Parameters
        ----------
        plan_id:
            Identifier of the plan to retrieve.
        """
        cursor = self._conn.execute(
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
        cursor = self._conn.execute(
            "DELETE FROM execution_plans WHERE plan_id = ?",
            (plan_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_ids(self) -> list[str]:
        """Return a list of all stored plan IDs in insertion order."""
        cursor = self._conn.execute("SELECT plan_id FROM execution_plans")
        return [row[0] for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
