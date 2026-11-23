"""CONTRACT.md completeness guard.

The pairing exam treats freetalon/pairing/CONTRACT.md as the complete
and only specification, but the shipped pairing tests once exercised
public surface the contract never named (the STATUS_* constants,
make_server). This test keeps that class of gap from coming back:
every name the shipped pairing tests import from the pairing package
must be mentioned in CONTRACT.md. Cheap and deterministic — file
reads and an AST parse, no server, no clock.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "freetalon" / "pairing" / "CONTRACT.md"
PAIRING_MODULES = {"freetalon.pairing.store", "freetalon.pairing.server"}
TEST_FILES = ("test_pairing_store.py", "test_pairing_server.py")


def _imported_pairing_names(test_file: Path) -> set[str]:
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in PAIRING_MODULES:
            names.update(alias.name for alias in node.names)
    return names


class PairingContractCompletenessTests(unittest.TestCase):
    def test_contract_names_every_test_imported_pairing_name(self) -> None:
        contract_text = CONTRACT.read_text(encoding="utf-8")
        for test_name in TEST_FILES:
            imported = _imported_pairing_names(REPO_ROOT / "tests" / test_name)
            self.assertTrue(imported, f"{test_name} imports no pairing names")
            for name in sorted(imported):
                self.assertRegex(
                    contract_text,
                    rf"\b{re.escape(name)}\b",
                    f"{name} (imported by {test_name}) is not named in "
                    "freetalon/pairing/CONTRACT.md",
                )


if __name__ == "__main__":
    unittest.main()
