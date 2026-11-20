"""Dashboard storage_secret hygiene.

The dashboard storage secret was a hardcoded literal in dashboard.py
until 2026-11-19 (see docs/secret-hygiene.md). It is now resolved
from the FREETALON_DASHBOARD_STORAGE_SECRET env var / the workspace
dashboard.env file. These tests pin that: no literal may come back,
and the env wiring must stay. They never read the real value.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_SOURCE = (REPO_ROOT / "dashboard.py").read_text(encoding="utf-8")


class DashboardStorageSecretTests(unittest.TestCase):
    def test_no_storage_secret_literal(self) -> None:
        literal = re.search(
            r"""storage_secret\s*=\s*(?:"[^"\n]*"|'[^'\n]*')""",
            DASHBOARD_SOURCE,
        )
        self.assertIsNone(literal, "dashboard.py must not hardcode a storage_secret")

    def test_secret_resolved_from_env(self) -> None:
        self.assertIn("FREETALON_DASHBOARD_STORAGE_SECRET", DASHBOARD_SOURCE)
        self.assertIn("storage_secret=STORAGE_SECRET", DASHBOARD_SOURCE)


if __name__ == "__main__":
    unittest.main()
