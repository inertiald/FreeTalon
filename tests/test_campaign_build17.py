"""Campaign build #17: the fill's no-change spelling + planning-
failure run records.

Arm #26 died in content fill on the pairing exam's store node:
asked to fill an edit node against a standing implementation it
judged already correct, the model spelled "no change" as an
identity scoped edit (search == replace over the whole method)
on all three attempts, the validator rejected each as
no_op_edit, and the exhausted fill's LLMResponseError then
propagated uncaught out of plan_task_intent — the driver died
with no run record at all (the same terminal shape as arm #17).

The build is tested at the levels it operates:

- the partition (_split_identity_edits): an identity edit over
  text occurring exactly once in the file as it stands is a
  no-change declaration; identity over absent/ambiguous text,
  malformed entries, and real edits are not;
- plan_task_intent end to end with a dispatching call_llm mock:
  an all-identity fill completes the node as no-change on the
  FIRST attempt (content = the standing file, the system stamp
  on the node, the plan-emission metadata naming it, the
  declaration on the attempt record); a mixed fill proceeds on
  its real edits with the declaration recorded; an identity
  claim over absent text still meets the no_op_edit rejection
  and exhausts;
- the authored-plan guard is unchanged: a raw plan carrying an
  identity scoped edit is rejected with no_op_edit;
- fill exhaustion with the driver flow's workspace constraint
  writes the run record (contract, drained planner attempts,
  summary with the FAIL planning-failure verdict); build #26
  then RETURNS the failure as a FAILED plan carrying the
  planning_failure stamp {error, error_type, run_id} instead of
  propagating the exception — arm #34 measured that no caller
  handles the raise (both drivers call plan_task_intent bare),
  so the invariant is now: planning failure is a recorded
  outcome, never an uncaught exception. Without the workspace
  constraint the same FAILED plan returns with run_id None.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.models import PlanStatus, TaskIntent
from freetalon.orchestrator.planner import (
    _split_identity_edits,
    drain_pending_planner_attempts,
    parse_planner_response,
    plan_task_intent,
)

SERVER_SOURCE = (
    "class Server:\n"
    "    pass\n"
    "\n"
    "\n"
    "class Handler:\n"
    "    def __init__(self, server):\n"
    "        self.server = server\n"
    "\n"
    "    def handle(self):\n"
    "        return self.server\n"
)

HANDLE = "    def handle(self):\n        return self.server\n"

IDENTITY_FILL = json.dumps({"edits": [{"search": HANDLE, "replace": HANDLE}]})

MIXED_FILL = json.dumps(
    {
        "edits": [
            {"search": HANDLE, "replace": HANDLE},
            {
                "search": HANDLE,
                "replace": (
                    HANDLE
                    + "\n"
                    + "    def handle_twice(self):\n"
                    + "        return self.server\n"
                ),
            },
        ]
    }
)

ABSENT_IDENTITY_FILL = json.dumps(
    {"edits": [{"search": "def missing():\n", "replace": "def missing():\n"}]}
)

NOT_FOUND_FILL = json.dumps(
    {
        "edits": [
            {
                "search": "def missing(self):\n    pass\n",
                "replace": "def missing(self):\n    return 1\n",
            }
        ]
    }
)


def _write_node(node_id, target, intent=None, **extra_inputs):
    inputs = {"file_path": target}
    if intent is not None:
        inputs["content_intent"] = intent
    inputs.update(extra_inputs)
    return {
        "id": node_id,
        "objective": f"Write {target}",
        "depends_on": [],
        "assigned_claw": "scaffold",
        "inputs": inputs,
        "criteria": [{"type": "file_exists", "path": target}],
    }


def _bash_node(node_id, command="true", depends_on=None):
    return {
        "id": node_id,
        "objective": f"Run {command}",
        "depends_on": depends_on or [],
        "assigned_claw": "bash",
        "inputs": {"command": command},
        "criteria": [{"type": "exit_code", "equals": 0}],
    }


EDIT_SKELETON = json.dumps(
    {
        "nodes": [
            _write_node("w-server", "pkg/server.py", "edit"),
            _bash_node("run", "true", depends_on=["w-server"]),
        ],
        "metadata": {},
    }
)


class TestSplitIdentityEdits(unittest.TestCase):
    def test_identity_over_unique_text_declares(self) -> None:
        real, declared = _split_identity_edits(
            [{"search": HANDLE, "replace": HANDLE}], SERVER_SOURCE
        )
        self.assertEqual(real, [])
        self.assertEqual(len(declared), 1)

    def test_real_edit_is_not_a_declaration(self) -> None:
        edit = {"search": HANDLE, "replace": HANDLE + "\n"}
        real, declared = _split_identity_edits([edit], SERVER_SOURCE)
        self.assertEqual(real, [edit])
        self.assertEqual(declared, [])

    def test_identity_over_absent_text_is_not_a_declaration(self) -> None:
        edit = {"search": "def missing():\n", "replace": "def missing():\n"}
        real, declared = _split_identity_edits([edit], SERVER_SOURCE)
        self.assertEqual(real, [edit])
        self.assertEqual(declared, [])

    def test_identity_over_ambiguous_text_is_not_a_declaration(self) -> None:
        doubled = SERVER_SOURCE + "\n" + HANDLE
        edit = {"search": HANDLE, "replace": HANDLE}
        real, declared = _split_identity_edits([edit], doubled)
        self.assertEqual(real, [edit])
        self.assertEqual(declared, [])

    def test_malformed_entries_stay_real(self) -> None:
        entries = [
            {"search": "", "replace": ""},
            {"search": HANDLE},
            "not-a-dict",
            {"search": 5, "replace": 5},
        ]
        real, declared = _split_identity_edits(entries, SERVER_SOURCE)
        self.assertEqual(real, entries)
        self.assertEqual(declared, [])

    def test_unknown_source_declares_nothing(self) -> None:
        edit = {"search": HANDLE, "replace": HANDLE}
        real, declared = _split_identity_edits([edit], None)
        self.assertEqual(real, [edit])
        self.assertEqual(declared, [])


class _FillCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "server.py").write_text(SERVER_SOURCE, encoding="utf-8")
        self.intent = TaskIntent(goal="Extend the pairing server")
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )
        self.prompts: list[str] = []

    def _dispatcher(self, fill_payloads):
        state = {"fills": 0}

        def _call(system_prompt, user_prompt, settings=None):
            if "CONTENT FILL" in user_prompt:
                state["fills"] += 1
                self.prompts.append(user_prompt)
                return fill_payloads[
                    min(state["fills"], len(fill_payloads)) - 1
                ]
            return EDIT_SKELETON

        _call.state = state
        return _call

    def _plan(self, dispatcher, **kwargs):
        intent = kwargs.pop("intent", self.intent)
        with patch(
            "freetalon.orchestrator.planner.call_llm",
            side_effect=dispatcher,
        ):
            return plan_task_intent(
                intent, settings=self.settings, repo_root=self.root, **kwargs
            )


class TestNoChangeFill(_FillCase):
    def test_all_identity_fill_completes_no_change_first_attempt(self) -> None:
        records: list[dict] = []
        dispatcher = self._dispatcher([IDENTITY_FILL])
        plan = self._plan(dispatcher, attempt_sink=records.append)
        # The declaration is an answer, not a failure: one fill
        # call, no re-ask.
        self.assertEqual(dispatcher.state["fills"], 1)
        node = plan.nodes[0]
        self.assertEqual(node.inputs["content"], SERVER_SOURCE)
        self.assertEqual(node.inputs["_materialized_from"], "no_change")
        emission = plan.metadata["plan_emission"]
        self.assertEqual(emission["no_change_nodes"], [node.id])
        self.assertEqual(emission["content_attempts"], {node.id: 1})
        # The declaration is on the attempt record.
        fill_records = [
            record for record in records if record["phase"] == "content_fill"
        ]
        self.assertEqual(len(fill_records), 1)
        self.assertEqual(fill_records[0]["outcome"], "accepted")
        self.assertEqual(len(fill_records[0]["fill_notes"]), 1)
        self.assertIn("no-change", fill_records[0]["fill_notes"][0])

    def test_mixed_fill_proceeds_on_real_edits(self) -> None:
        records: list[dict] = []
        dispatcher = self._dispatcher([MIXED_FILL])
        plan = self._plan(dispatcher, attempt_sink=records.append)
        self.assertEqual(dispatcher.state["fills"], 1)
        node = plan.nodes[0]
        content = node.inputs["content"]
        self.assertIn("handle_twice", content)
        self.assertIn(HANDLE, content)
        # The real edit materialized through the ordinary seam.
        self.assertEqual(node.inputs["_materialized_from"], "scoped_edits")
        self.assertNotIn("no_change_nodes", plan.metadata["plan_emission"])
        fill_records = [
            record for record in records if record["phase"] == "content_fill"
        ]
        self.assertEqual(len(fill_records), 1)
        self.assertEqual(fill_records[0]["outcome"], "accepted")
        self.assertEqual(len(fill_records[0]["fill_notes"]), 1)

    def test_identity_over_absent_text_still_exhausts(self) -> None:
        dispatcher = self._dispatcher([ABSENT_IDENTITY_FILL])
        plan = self._plan(dispatcher)
        self.assertEqual(dispatcher.state["fills"], 3)
        # Build #26: exhaustion returns a FAILED plan whose stamp
        # names the fill's own error; nothing propagates.
        self.assertEqual(plan.status, PlanStatus.FAILED)
        stamp = plan.metadata.get("planning_failure")
        self.assertIsInstance(stamp, dict)
        self.assertIn("no_op_edit", stamp["error"])
        # No workspace constraint: no record was written.
        self.assertIsNone(stamp.get("run_id"))


class TestAuthoredGuardUnchanged(_FillCase):
    def test_authored_identity_edit_rejected_no_op(self) -> None:
        node = {
            "id": "step_1",
            "objective": "Edit the server",
            "depends_on": [],
            "assigned_claw": "scaffold",
            "inputs": {
                "file_path": "pkg/server.py",
                "edits": [{"search": HANDLE, "replace": HANDLE}],
            },
            "outputs": ["pkg/server.py"],
            "acceptance": ["The file exists."],
            "criteria": [{"type": "file_exists", "path": "pkg/server.py"}],
        }
        raw = json.dumps({"nodes": [node], "metadata": {}})
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                raw,
                goal_text="Extend the pairing server",
                repo_root=self.root,
            )
        self.assertIn("no_op_edit", str(caught.exception))


class TestPlanningFailureRecord(_FillCase):
    def setUp(self) -> None:
        super().setUp()
        # The pending-attempts buffer is process-global: start
        # from empty so the records drained into this test's run
        # record are exactly this test's planning call.
        drain_pending_planner_attempts(lambda record: None)

    def _workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_fill_exhaustion_records_the_run(self) -> None:
        workspace = self._workspace()
        intent = TaskIntent(
            goal="Extend the pairing server",
            constraints={"workspace": str(workspace)},
        )
        dispatcher = self._dispatcher([NOT_FOUND_FILL])
        plan = self._plan(dispatcher, intent=intent)
        self.assertEqual(dispatcher.state["fills"], 3)
        # Build #26: the failure returns as a FAILED plan; the
        # stamp carries the fill's error and the record's run id.
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertEqual(plan.nodes, [])
        stamp = plan.metadata.get("planning_failure")
        self.assertIsInstance(stamp, dict)
        self.assertEqual(stamp["error_type"], "LLMResponseError")
        self.assertIn("Content fill for planner node", stamp["error"])
        run_id = stamp.get("run_id")
        self.assertIsNotNone(run_id)
        run_dir = workspace / "runs" / str(run_id)
        self.assertTrue(run_dir.is_dir())
        contract = json.loads(
            (run_dir / "contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["objective"], "Extend the pairing server")
        self.assertIn("planning_failure", contract)
        self.assertIn(
            "Content fill for planner node",
            contract["planning_failure"]["error"],
        )
        attempts = [
            json.loads(line)
            for line in (run_dir / "planner_attempts.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        phases = [record["phase"] for record in attempts]
        self.assertIn("plan_skeleton", phases)
        self.assertIn("content_fill", phases)
        fill_attempts = [
            record for record in attempts if record["phase"] == "content_fill"
        ]
        self.assertEqual(len(fill_attempts), 3)
        self.assertTrue(
            all(r["outcome"] == "rejected" for r in fill_attempts)
        )
        summary = (run_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Goal verdict: **FAIL**", summary)
        self.assertIn("planning failure", summary)
        self.assertIn("Extend the pairing server", summary)

    def test_exhaustion_without_workspace_returns_failed_plan(self) -> None:
        dispatcher = self._dispatcher([NOT_FOUND_FILL])
        plan = self._plan(dispatcher)
        # Build #26: no workspace constraint means no record was
        # written (run_id None) — but the outcome is still the
        # returned FAILED plan, never an exception.
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertEqual(plan.nodes, [])
        stamp = plan.metadata.get("planning_failure")
        self.assertIsInstance(stamp, dict)
        self.assertIsNone(stamp.get("run_id"))
        self.assertIn("Content fill for planner node", stamp["error"])


if __name__ == "__main__":
    unittest.main()
