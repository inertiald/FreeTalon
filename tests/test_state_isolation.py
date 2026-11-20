"""Tests for state-store path isolation (FREETALON_STATE_DB_PATH).

The suite once wrote freetalon_state.db into the live
~/freetalon-workspace on a user system: tests built HiveAPIServer,
whose default GateStore() resolved to the production default path.
The stores now honor an env override, and these tests pin both halves
of the contract — the override is respected by every store, and the
fallback is unchanged when it is not set.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator.gates import GateStore
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.state_store import (
    STATE_DB_PATH_ENV,
    ExecutionPlanStateStore,
    default_db_path,
)


class DefaultDbPathTests(unittest.TestCase):
    def test_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {STATE_DB_PATH_ENV: "/tmp/elsewhere.db"}):
            self.assertEqual(
                default_db_path(Path("/prod/x.db")), Path("/tmp/elsewhere.db")
            )

    def test_fallback_without_override(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != STATE_DB_PATH_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                default_db_path(Path("/prod/x.db")), Path("/prod/x.db")
            )


class StoreOverrideTests(unittest.TestCase):
    def test_all_stores_share_the_override_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state.db"
            with mock.patch.dict(os.environ, {STATE_DB_PATH_ENV: str(target)}):
                plans = ExecutionPlanStateStore()
                gates = GateStore()
                experiments = ExperimentStore()
            self.assertEqual(plans.db_path, target)
            self.assertEqual(gates._db_path, target)
            self.assertEqual(experiments.db_path, target)
            with sqlite3.connect(str(target)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertTrue({"execution_plans", "gates", "experiments"} <= tables)


if __name__ == "__main__":
    unittest.main()
