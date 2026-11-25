"""Campaign build #15 (2026-11-24) — attribute-chain grounding.

Arm #24's landed repair referenced ``self.server.pairing_store``
in a file whose handler reaches the store as ``self.store`` (its
make_server convention): build #3's grounding checks first-level
``self.<attr>`` loads, ``self.server`` is grounded by the stdlib
handler table, and the second level — an attribute of the server
object — was never judged. The AttributeError turned the hang the
repair targeted into a crash (shipped 27/35, C09 still failed).

Build #15 extends grounding to introduced attribute CHAINS:
``attribute_grounding_violations`` judges each deeper attribute of
a ``self``-rooted chain the edit introduces, resolving the stepped-
onto object only from evidence in the file itself (an annotation
or constructor assignment naming an in-file class, or the request
handler's ``self.server``). Both repair seams decline such a
candidate with the new reason ``ungrounded_attribute``; the
plan-form sites keep folding chain violations into the existing
``ungrounded_reference`` code. The decline text carries the
class's established ``self.*`` vocabulary — and, when one
vocabulary attribute is the leaf's own suffix component, names it
as the file's spelling for that role.

No GPU is touched: every LLM call is mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.grounding import (
    attribute_grounding_violations,
    edit_grounding_violations,
)
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from freetalon.orchestrator.planner import parse_planner_response

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

GOAL = "Build a small fixture package with a pairing server module."

# The arm #24 shape: the handler's convention is self.store, the
# server is a bare stdlib ThreadingHTTPServer, and nothing in the
# file gives pairing_store a home.
PAIRING_TEXT = '''"""Fixture pairing server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        records = self.store.list_records()
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        view = self.store.register("CODE-1", "nonce")
        self.send_response(201)
        self.end_headers()


def make_server(store, token, host="127.0.0.1", port=0):
    server = ThreadingHTTPServer((host, port), Handler)
    return server
'''

# The same shape nested inside make_server, as the exam's loop
# actually writes it.
NESTED_PAIRING_TEXT = '''"""Fixture pairing server, nested-handler shape."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_server(store, token, host="127.0.0.1", port=0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            records = self.store.list_records()
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            view = self.store.register("CODE-1", "nonce")
            self.send_response(201)
            self.end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    return server
'''

# The repo's own pairing-server shape: self.server.pairing_store is
# legitimate there — the server class declares it and make_server
# assigns it.
REPO_SHAPED_TEXT = '''"""Fixture pairing server, repo shape."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _PairingHTTPServer(_ThreadingHTTPServer):
    pairing_store: object
    pairing_token: str


class _PairingHandler(BaseHTTPRequestHandler):
    server: "_PairingHTTPServer"

    def _handle_list(self):
        self.send_response(200)
        self.end_headers()


def make_server(store, token, host="127.0.0.1", port=0):
    httpd = _PairingHTTPServer((host, port), _PairingHandler)
    httpd.pairing_store = store
    httpd.pairing_token = token
    return httpd
'''

PAIRING_TEST_TEXT = '''import unittest

from pkg import handler


class HandlerTests(unittest.TestCase):
    def test_handler_module_loads(self):
        self.assertTrue(hasattr(handler, "Handler"))
'''

FAILING_ID = "tests.test_handler.HandlerTests.test_handler_module_loads"

REGISTER_LINE = '        view = self.store.register("CODE-1", "nonce")'
BAD_REGISTER_LINE = '        view = self.server.pairing_store.register("CODE-1", "nonce")'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _block(search: str, replace: str) -> str:
    return (
        "<<<<<<< SEARCH\n"
        f"{search}\n"
        "=======\n"
        f"{replace}\n"
        ">>>>>>> REPLACE\n"
    )


# ── The chain check itself ──────────────────────────────────────────


class TestChainGroundingUnit(unittest.TestCase):
    def test_arm24_chain_is_caught(self):
        broken = PAIRING_TEXT.replace(REGISTER_LINE, BAD_REGISTER_LINE)
        violations = attribute_grounding_violations(PAIRING_TEXT, broken)
        self.assertEqual(len(violations), 1)
        self.assertIn("self.server.pairing_store", violations[0])
        # The decline is actionable: it names the file's own
        # spelling for the role.
        self.assertIn("self.store", violations[0])
        # And the folded check reports it too.
        self.assertTrue(
            any(
                "self.server.pairing_store" in v
                for v in edit_grounding_violations(PAIRING_TEXT, broken)
            )
        )

    def test_arm24_chain_is_caught_in_the_nested_shape(self):
        broken = NESTED_PAIRING_TEXT.replace(
            "            view = self.store.register",
            "            view = self.server.pairing_store.register",
        )
        violations = attribute_grounding_violations(
            NESTED_PAIRING_TEXT, broken
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("self.server.pairing_store", violations[0])

    def test_same_edit_spelled_the_files_way_is_clean(self):
        edited = PAIRING_TEXT.replace(
            REGISTER_LINE,
            '        view = self.store.confirm("CODE-1", "nonce")',
        )
        self.assertEqual(attribute_grounding_violations(PAIRING_TEXT, edited), [])
        self.assertEqual(edit_grounding_violations(PAIRING_TEXT, edited), [])

    def test_framework_attribute_chain_is_clean(self):
        edited = PAIRING_TEXT.replace(
            REGISTER_LINE,
            '        addr = self.server.server_address\n' + REGISTER_LINE,
        )
        self.assertEqual(attribute_grounding_violations(PAIRING_TEXT, edited), [])
        self.assertEqual(edit_grounding_violations(PAIRING_TEXT, edited), [])

    def test_preexisting_chain_is_not_introduced(self):
        # The old text already carried the bad chain (broken before
        # the edit): an edit that does not touch it is not blamed.
        old = PAIRING_TEXT.replace(REGISTER_LINE, BAD_REGISTER_LINE)
        new = old.replace("self.send_response(201)", "self.send_response(202)")
        self.assertEqual(attribute_grounding_violations(old, new), [])
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_repo_shaped_server_chain_is_grounded(self):
        edited = REPO_SHAPED_TEXT.replace(
            "        self.send_response(200)",
            "        records = self.server.pairing_store.list_records()\n"
            "        token = self.server.pairing_token\n"
            "        self.send_response(200)",
        )
        self.assertEqual(
            attribute_grounding_violations(REPO_SHAPED_TEXT, edited), []
        )
        self.assertEqual(
            edit_grounding_violations(REPO_SHAPED_TEXT, edited), []
        )

    def test_repo_shaped_bogus_leaf_is_caught(self):
        edited = REPO_SHAPED_TEXT.replace(
            "        self.send_response(200)",
            "        vault = self.server.pairing_vault\n"
            "        self.send_response(200)",
        )
        violations = attribute_grounding_violations(REPO_SHAPED_TEXT, edited)
        self.assertEqual(len(violations), 1)
        self.assertIn("self.server.pairing_vault", violations[0])

    def test_edit_that_gives_the_attribute_a_home_is_clean(self):
        # The repair may legitimately introduce the chain when the
        # same edit attaches the attribute where the file builds
        # the server — the first-level rule's "assign it as part of
        # the edit" posture, one level down.
        edited = PAIRING_TEXT.replace(REGISTER_LINE, BAD_REGISTER_LINE).replace(
            "    server = ThreadingHTTPServer((host, port), Handler)\n"
            "    return server",
            "    server = ThreadingHTTPServer((host, port), Handler)\n"
            "    server.pairing_store = store\n"
            "    return server",
        )
        self.assertEqual(attribute_grounding_violations(PAIRING_TEXT, edited), [])
        self.assertEqual(edit_grounding_violations(PAIRING_TEXT, edited), [])

    def test_unknown_provenance_chain_is_exempt(self):
        # self.config's type is imported, not defined in the file:
        # what it carries is unverifiable here, so doubt passes.
        old = (
            "from .config import Config\n\n"
            "class Worker:\n"
            "    def __init__(self):\n"
            "        self.config = Config()\n"
            "    def get(self):\n"
            "        return 1\n"
        )
        new = old.replace("return 1", "return self.config.timeout")
        self.assertEqual(attribute_grounding_violations(old, new), [])
        self.assertEqual(edit_grounding_violations(old, new), [])

    def test_dynamic_server_class_is_exempt(self):
        old = (
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n\n"
            "class FlexServer(ThreadingHTTPServer):\n"
            "    def __getattr__(self, name):\n"
            "        return None\n\n"
            "def make_server(store, token, host=\"127.0.0.1\", port=0):\n"
            "    return FlexServer((host, port), Handler)\n"
        )
        new = old.replace(
            "        self.send_response(200)",
            "        records = self.server.pairing_store.list_records()\n"
            "        self.send_response(200)",
        )
        self.assertEqual(attribute_grounding_violations(old, new), [])

    def test_unparseable_text_yields_no_violations(self):
        self.assertEqual(
            attribute_grounding_violations(PAIRING_TEXT, "def broken(:"), []
        )
        self.assertEqual(
            attribute_grounding_violations("def broken(:", PAIRING_TEXT), []
        )


# ── The seams decline with the new reason ───────────────────────────


class PairingCase(unittest.TestCase):
    """Build #3's seam-fixture shape, with the pairing text."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.handler_path = self.root / "pkg" / "handler.py"
        self.test_path = self.root / "tests" / "test_handler.py"
        _write(self.root / "pkg" / "__init__.py", "")
        _write(self.handler_path, PAIRING_TEXT)
        _write(self.test_path, PAIRING_TEST_TEXT)

    def _write_node(self, node_id: str) -> PlanNode:
        return PlanNode(
            id=node_id,
            objective="Write handler.py",
            hypothesis="the module computes its values",
            assigned_claw="scaffold",
            inputs={"file_path": str(self.handler_path), "content": PAIRING_TEXT},
            outputs=[str(self.handler_path)],
            acceptance=["The module exists."],
            criteria=[{"type": "file_exists", "path": str(self.handler_path)}],
            depends_on=[],
            status=PlanStatus.COMPLETED,
        )

    def _run_node(self, node_id: str, diagnostics: str) -> PlanNode:
        return PlanNode(
            id=node_id,
            objective="Run the tests",
            hypothesis="the tests pass",
            assigned_claw="bash",
            inputs={"command": f"./.venv/bin/python {self.test_path}"},
            outputs=[],
            acceptance=["The tests pass."],
            criteria=[{"type": "exit_code", "expected": 0}],
            depends_on=["node-1"],
            status=PlanStatus.FAILED,
            error="exit_code 1 != 0",
            exit_code=1,
            measurement={
                "verdict": "FAIL",
                "results": [
                    {
                        "criterion": {"type": "exit_code", "expected": 0},
                        "detail": "exit_code 1 != 0",
                        "measured": True,
                        "passed": False,
                    }
                ],
                "exit_code": 1,
            },
        )

    def _plan(self, nodes: list[PlanNode]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-build15",
            intent=TaskIntent(goal=GOAL, constraints={"workspace": str(self.root)}),
            nodes=nodes,
            metadata={"repo_root": str(self.root)},
        )

    def _brief(self, diagnostics: str) -> dict:
        return {
            "goal": {"objective": GOAL},
            "plan_id": "plan-build15",
            "plan_version": 1,
            "failing_tests": [FAILING_ID],
            "produced_files": ["pkg/handler.py", "tests/test_handler.py"],
            "failed_nodes": [{"id": "node-2", "diagnostics": diagnostics}],
            "measurements": [],
            "past_experiments": [],
            "remaining_budget": {"replans": 2},
        }


class TestScopedSeamChainGrounding(PairingCase):
    def test_ungrounded_chain_declines_with_its_own_reason(self):
        nodes = [self._write_node("node-1"), self._run_node("node-2", "AssertionError")]
        plan = self._plan(nodes)
        declines: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return _block(REGISTER_LINE, BAD_REGISTER_LINE)

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan, self._brief("AssertionError"), SETTINGS, declines=declines
            )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["ungrounded_attribute"])
        self.assertIn("self.server.pairing_store", declines[0]["detail"])

    def test_grounded_chain_edit_still_repairs(self):
        nodes = [self._write_node("node-1"), self._run_node("node-2", "AssertionError")]
        plan = self._plan(nodes)
        declines: list[dict] = []

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return _block(
                REGISTER_LINE,
                REGISTER_LINE + "\n        addr = self.server.server_address",
            )

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_scoped_edit_repair(
                plan, self._brief("AssertionError"), SETTINGS, declines=declines
            )
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])


class TestLocalSeamChainGrounding(PairingCase):
    def test_ungrounded_chain_repair_declines_with_its_own_reason(self):
        diagnostics = (
            "Traceback (most recent call last):\n"
            f'  File "{self.handler_path}", line 13, in do_POST\n'
            "AssertionError: register returned the wrong view"
        )
        nodes = [
            self._write_node("node-1"),
            self._run_node("node-2", diagnostics),
        ]
        plan = self._plan(nodes)
        declines: list[dict] = []
        response = (
            "```python\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        records = self.store.list_records()\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "\n"
            "    def do_POST(self):\n"
            '        view = self.server.pairing_store.register("CODE-1", "nonce")\n'
            "        self.send_response(201)\n"
            "        self.end_headers()\n"
            "```\n"
        )

        def fake_call_llm(system_prompt, user_prompt, settings=None):
            return response

        with mock.patch.object(planner_module, "call_llm", fake_call_llm):
            result = planner_module.try_local_repair(
                plan, self._brief(diagnostics), SETTINGS, declines=declines
            )
        self.assertIsNone(result)
        self.assertEqual([d["reason"] for d in declines], ["ungrounded_attribute"])
        self.assertIn("self.server.pairing_store", declines[0]["detail"])


# ── The plan form keeps the existing code ───────────────────────────


def _raw_node(node_id, claw, inputs, criteria, depends_on=None, outputs=None):
    return {
        "id": node_id,
        "objective": f"Step {node_id}",
        "depends_on": depends_on or [],
        "assigned_claw": claw,
        "inputs": inputs,
        "outputs": outputs if outputs is not None else [],
        "acceptance": ["The step's effect holds."],
        "criteria": criteria,
        "error": None,
    }


class TestPlanFormChainGrounding(PairingCase):
    def _parse(self, nodes):
        return parse_planner_response(
            json.dumps({"nodes": nodes}),
            goal_text=GOAL,
            workspace=str(self.root),
            repo_root=self.root,
        )

    def test_scoped_edit_with_ungrounded_chain_is_a_plan_error(self):
        node = _raw_node(
            "step_1",
            "scaffold",
            {
                "file_path": str(self.handler_path),
                "edits": [
                    {"search": REGISTER_LINE, "replace": BAD_REGISTER_LINE}
                ],
            },
            [{"type": "file_exists", "path": str(self.handler_path)}],
            outputs=[str(self.handler_path)],
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse([node])
        codes = [r.code for r in caught.exception.plan_errors]
        self.assertEqual(codes, ["ungrounded_reference"])
        self.assertIn("self.server.pairing_store", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
