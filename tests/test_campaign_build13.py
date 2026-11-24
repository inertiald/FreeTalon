"""Campaign build #13 (2026-11-24): the repair seams get the
build #9 treatment — attempt persistence plus a format re-ask.

Arm #22 is the evidence. Build #12's suspect-map wiring worked:
the scoped-edit seam reached its model call in all three cycles
with the suspect file and the probe's flow diagnostics — and
declined 3/3 with the new reason no_edit_blocks. What the model
actually returned was recorded NOWHERE (not the driver log, not
the run store), so the decline was a verdict without evidence —
the third time an unobserved response class cost an arm (arm
#17's fill payloads -> build #8's capture; arm #18's re-plan JSON
-> build #9's planner persistence).

Part A — seam attempt persistence: every scoped-edit and
local-repair model call records seam name, attempt number, the
response's disposition and decline reason, cycle context, and
the FULL raw response text, to the build #9 channels: a labeled
stdout block, and the attempt sink when one is installed —
LoopRunner routes seam records (kind="seam_attempt") to the
run's seam_attempts.jsonl, leaving planner_attempts.jsonl's
shape unchanged. Seam records never enter the pending buffer.

Part B — the format re-ask (build #8's shape-repair pattern): a
scoped-edit response carrying no parseable edit blocks earns ONE
re-ask inside the same invocation (the defect named in plain
words, the exact block format restated, a minimal worked
example, and the contract's one non-block answer — an explicitly
empty return — restated). A malformed response is NOT re-asked:
it attempted the format and broke its mechanics, and declines
malformed_edit_blocks on the first response as before. Local
repair is symmetric — extraction failure is its no-blocks class
— so it gets the same one re-ask with its own format restated.
The re-ask consumes no extra budget unit: it lives inside the
one seam invocation the loop already paid for.

No GPU is touched: every LLM call is mocked or scripted.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.intake import LLMSettings
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanNode,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)
from tests.test_campaign_build12 import (
    FIX_BLOCK,
    ProbeSeamCase,
    SERVER_REL,
)
from tests.test_campaign_build3 import HandlerCase
from tests.test_campaign_build6 import (
    FIXTURE_SERVER_HANG,
    SMOKE_CLAUSES,
    _fixture_tree,
    _write,
)

SETTINGS = LLMSettings(
    backend="ollama", base_url="http://localhost:11434", model="test"
)

PROSE_ANSWER = (
    "The register handler sleeps before answering, which is why "
    "the flow times out. I would remove the sleep so the handler "
    "answers immediately."
)
SECOND_PROSE_ANSWER = "As explained, the sleep is the problem."


def _scripted(responses):
    """A call_llm fake answering from a queue; returns (fake, calls)."""
    queue = list(responses)
    calls: list[tuple[str, str]] = []

    def fake_call_llm(system_prompt, user_prompt, settings=None):
        calls.append((system_prompt, user_prompt))
        return queue.pop(0) if queue else ""

    return fake_call_llm, calls


# ── Part B: the scoped-edit re-ask (arm #22's exact path) ────────


class ScriptedProbeSeamCase(ProbeSeamCase):
    def _run_scripted(self, responses, plan=None, brief=None):
        fake, calls = _scripted(responses)
        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_scoped_edit_repair(
                plan if plan is not None else self._plan(),
                brief if brief is not None else self._brief(),
                SETTINGS,
                declines=declines,
            )
        return result, calls, declines, records


class TestScopedEditReask(ScriptedProbeSeamCase):
    def test_no_blocks_first_response_triggers_one_reask(self):
        result, calls, declines, records = self._run_scripted(
            [PROSE_ANSWER, FIX_BLOCK]
        )
        # The second response's blocks are applied normally.
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])
        self.assertEqual(len(calls), 2)
        # The re-ask prompt carries the shape-repair block: the
        # defect in plain words, the exact format, the worked
        # example, and the contract's empty-return exception.
        reask_prompt = calls[1][1]
        self.assertIn("REJECTED", reask_prompt)
        self.assertIn("no SEARCH/REPLACE", reask_prompt)
        self.assertIn("<<<<<<< SEARCH", reask_prompt)
        self.assertIn("LIMIT = 10", reask_prompt)
        self.assertIn("LIMIT = 20", reask_prompt)
        self.assertIn("empty response", reask_prompt)
        self.assertIn("NOT acceptable", reask_prompt)
        # Both calls are on record, the second marked as the
        # shape-repair re-ask.
        self.assertEqual(len(records), 2)
        first, second = records
        self.assertEqual(first["kind"], "seam_attempt")
        self.assertEqual(first["seam"], "scoped_edit")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["outcome"], "rejected")
        self.assertEqual(first["decline_reason"], "no_edit_blocks")
        self.assertFalse(first["shape_repair"])
        self.assertEqual(first["response_text"], PROSE_ANSWER)
        self.assertEqual(first["target"], SERVER_REL)
        self.assertEqual(first["plan_version"], 1)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["outcome"], "accepted")
        self.assertEqual(second["decline_reason"], "")
        self.assertTrue(second["shape_repair"])
        self.assertEqual(second["response_text"], FIX_BLOCK)

    def test_two_no_blocks_responses_decline_with_both_recorded(self):
        result, calls, declines, records = self._run_scripted(
            [PROSE_ANSWER, SECOND_PROSE_ANSWER]
        )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(declines), 1)
        self.assertEqual(declines[0]["reason"], "no_edit_blocks")
        # The arm #22 gap, closed: BOTH raw responses are in the
        # attempt record, so the decline is reconstructable.
        self.assertEqual(
            [record["response_text"] for record in records],
            [PROSE_ANSWER, SECOND_PROSE_ANSWER],
        )
        self.assertEqual(
            [record["decline_reason"] for record in records],
            ["no_edit_blocks", "no_edit_blocks"],
        )
        self.assertEqual(
            [record["shape_repair"] for record in records],
            [False, True],
        )

    def test_malformed_response_is_not_reasked(self):
        # Block markers present but broken: the model attempted
        # the format and broke its mechanics — a different defect
        # from never attempting it, declined on the first
        # response exactly as before build #13.
        malformed = "<<<<<<< SEARCH\nfoo\n>>>>>>> REPLACE\n"
        result, calls, declines, records = self._run_scripted(
            [malformed, FIX_BLOCK]
        )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [d["reason"] for d in declines], ["malformed_edit_blocks"]
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["decline_reason"], "malformed_edit_blocks")
        self.assertEqual(records[0]["response_text"], malformed)


class TestShapeRepairBlockWording(unittest.TestCase):
    def test_empty_problem_names_the_missing_blocks(self):
        block = planner_module._scoped_edit_shape_repair_block("empty", [])
        self.assertIn("no SEARCH/REPLACE", block)
        self.assertIn("<<<<<<< SEARCH", block)
        self.assertIn(">>>>>>> REPLACE", block)
        self.assertIn("LIMIT = 10", block)
        self.assertIn("empty response", block)

    def test_missing_section_names_the_pair_form(self):
        targets = [
            planner_module._ScopedEditTarget(
                failed_id="node-9",
                culprit_id="node-1",
                target_path="/x/store.py",
                relative_path="pkg/store.py",
                content="",
                failing_tests=[],
                criterion_detail="",
            ),
            planner_module._ScopedEditTarget(
                failed_id="node-9",
                culprit_id="node-2",
                target_path="/x/server.py",
                relative_path="pkg/server.py",
                content="",
                failing_tests=[],
                criterion_detail="",
            ),
        ]
        block = planner_module._scoped_edit_shape_repair_block(
            "missing_section", targets
        )
        self.assertIn("FILE: pkg/store.py", block)
        self.assertIn("FILE: pkg/server.py", block)
        self.assertIn("one section per file", block)


# ── Part B: the local-repair re-ask (the symmetric seam) ─────────

LOCAL_DIAGNOSTICS = (
    "Traceback (most recent call last):\n"
    '  File "{path}", line 8, in do_GET\n'
    "AssertionError: poll returned the wrong view"
)

GOOD_DEFINITION = (
    "def do_GET(self):\n"
    '    view = self.server.store.poll("KESTREL-0001")\n'
    "    self.send_response(200)\n"
    "    self.log_request(200)\n"
    "    self.end_headers()\n"
)


class TestLocalRepairReask(HandlerCase):
    def _run_scripted(self, responses):
        nodes = [
            self._write_node("node-1"),
            self._run_node(
                "node-2",
                LOCAL_DIAGNOSTICS.format(path=self.handler_path),
            ),
        ]
        plan = self._plan(nodes)
        brief = self._brief(
            LOCAL_DIAGNOSTICS.format(path=self.handler_path)
        )
        fake, calls = _scripted(responses)
        declines: list[dict] = []
        records: list[dict] = []
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(records.append),
        ):
            result = planner_module.try_local_repair(
                plan, brief, SETTINGS, declines=declines
            )
        return result, calls, declines, records

    def test_unextractable_first_answer_is_reasked_and_repairs(self):
        result, calls, declines, records = self._run_scripted(
            ["I cannot repair this definition.", GOOD_DEFINITION]
        )
        self.assertIsNotNone(result)
        self.assertEqual(declines, [])
        self.assertEqual(len(calls), 2)
        reask_prompt = calls[1][1]
        self.assertIn("REJECTED", reask_prompt)
        self.assertIn("no definition matching Handler.do_GET", reask_prompt)
        self.assertIn("compute_total", reask_prompt)
        # The candidate carries the repaired definition.
        by_id = {node.id: node for node in result}
        self.assertIn(
            "self.log_request(200)", by_id["node-1"].inputs["content"]
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["seam"], "local_repair")
        self.assertEqual(records[0]["outcome"], "rejected")
        self.assertEqual(
            records[0]["decline_reason"], "no_definition_extracted"
        )
        self.assertTrue(records[0]["target"].endswith("Handler.do_GET"))
        self.assertEqual(records[1]["outcome"], "accepted")
        self.assertTrue(records[1]["shape_repair"])

    def test_two_unextractable_answers_decline_with_both_recorded(self):
        result, calls, declines, records = self._run_scripted(
            ["no code here", "still no code"]
        )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [d["reason"] for d in declines], ["no_definition_extracted"]
        )
        self.assertEqual(
            [record["response_text"] for record in records],
            ["no code here", "still no code"],
        )
        self.assertEqual(
            [record["shape_repair"] for record in records], [False, True]
        )


# ── Part A: the persistence channels ─────────────────────────────


class TestSeamAttemptChannels(ScriptedProbeSeamCase):
    def test_direct_caller_gets_the_stdout_block_and_no_buffering(self):
        # No sink installed: the record stands in the stdout
        # block (the driver-log channel), and seam records never
        # enter the planner's pending buffer — that buffer is
        # for initial-planning records awaiting a run's sink,
        # and a seam record there would be misattributed to
        # whatever run started next.
        fake, _calls = _scripted([PROSE_ANSWER, FIX_BLOCK])
        before = len(planner_module._PENDING_ATTEMPTS)
        stdout = io.StringIO()
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            contextlib.redirect_stdout(stdout),
        ):
            result = planner_module.try_scoped_edit_repair(
                self._plan(), self._brief(), SETTINGS, declines=[]
            )
        self.assertIsNotNone(result)
        text = stdout.getvalue()
        self.assertIn(
            "SEAM ATTEMPT [seam=scoped_edit attempt=1 "
            "outcome=rejected reason=no_edit_blocks",
            text,
        )
        self.assertIn(PROSE_ANSWER, text)
        self.assertIn("attempt=2 outcome=accepted", text)
        self.assertEqual(len(planner_module._PENDING_ATTEMPTS), before)

    def test_a_failing_sink_never_raises_into_the_seam(self):
        def bad_sink(_record):
            raise RuntimeError("disk full")

        fake, _calls = _scripted([FIX_BLOCK])
        stdout = io.StringIO()
        with (
            mock.patch.object(planner_module, "call_llm", fake),
            planner_module.planner_attempt_sink(bad_sink),
            contextlib.redirect_stdout(stdout),
        ):
            result = planner_module.try_scoped_edit_repair(
                self._plan(), self._brief(), SETTINGS, declines=[]
            )
        self.assertIsNotNone(result)
        self.assertIn("seam attempt sink failed", stdout.getvalue())

    def test_run_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            record = {
                "kind": "seam_attempt",
                "seam": "scoped_edit",
                "attempt": 1,
                "outcome": "rejected",
                "decline_reason": "no_edit_blocks",
                "shape_repair": False,
                "plan_id": "plan-x",
                "plan_version": 1,
                "target": SERVER_REL,
                "response_text": "raw seam text",
            }
            store.append_seam_attempt("run-1", record)
            store.append_seam_attempt(
                "run-1", {**record, "attempt": 2, "shape_repair": True}
            )
            records = store.read_seam_attempts("run-1")
            self.assertEqual([item["attempt"] for item in records], [1, 2])
            self.assertEqual(records[0]["response_text"], "raw seam text")
            self.assertEqual(store.read_seam_attempts("run-none"), [])
            # The planner file is a separate channel, untouched.
            self.assertEqual(store.read_planner_attempts("run-1"), [])


# ── Parts A+B in a loop run: the seam fires, the run records ─────

CHECK_SCRIPT = '''
import importlib.util
import json
import sys
import threading
import urllib.request

spec = importlib.util.spec_from_file_location("fixture_server", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
server = module.make_server(None, None, "127.0.0.1", 0)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    request = urllib.request.Request(
        "http://127.0.0.1:%d/pairing/register" % server.server_address[1],
        data=json.dumps({"code": "SMOKE-1"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            status = response.status
    except Exception as exc:  # noqa: BLE001 - the check is the verdict
        print(f"register failed: {exc}")
        sys.exit(1)
    if status != 201:
        print(f"register answered {status}")
        sys.exit(1)
    print("register ok")
finally:
    server.shutdown()
    server.server_close()
'''

HANG_FIX_BLOCK = (
    "<<<<<<< SEARCH\n"
    "    def do_POST(self):\n"
    "        time.sleep(30)\n"
    "=======\n"
    "    def do_POST(self):\n"
    ">>>>>>> REPLACE\n"
)


class TestLoopRecordsSeamAttempts(unittest.TestCase):
    def test_seam_repair_lands_and_records_in_the_run_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionPlanStateStore(db_path=Path(tmp) / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            run_store = RunStore(Path(tmp) / "runs")
            runner = LoopRunner(
                store,
                registry,
                run_store=run_store,
                memory_store=ExperimentStore(Path(tmp) / "memory.db"),
            )
            tree = _fixture_tree(Path(tmp) / "tree", FIXTURE_SERVER_HANG)
            server_path = tree / "freetalon" / "pairing" / "server.py"
            # A test script by name as well as by role: the repeat
            # gate's plumbing classification (memory._segment_kind)
            # exempts interpreter-run test scripts from approach
            # signatures, exactly as it exempts the exam's unittest
            # nodes — a measuring node is plumbing, never a repeat.
            check_path = tree / "test_register_check.py"
            _write(check_path, CHECK_SCRIPT)
            plan = ExecutionPlan(
                plan_id="plan-build13",
                intent=TaskIntent(goal="pairing goal"),
                nodes=[
                    PlanNode(
                        id="node-1",
                        objective="Write the pairing server module",
                        assigned_claw="scaffold",
                        inputs={
                            "file_path": str(server_path),
                            "content": FIXTURE_SERVER_HANG,
                        },
                        criteria=[
                            {"type": "file_exists", "path": str(server_path)}
                        ],
                        hypothesis="the server answers the contract flows",
                    ),
                    PlanNode(
                        id="node-2",
                        objective="Check the register flow end to end",
                        assigned_claw="bash",
                        inputs={
                            "command": (
                                f"python3 {check_path} {server_path}"
                            )
                        },
                        criteria=[{"type": "exit_code", "equals": 0}],
                        depends_on=["node-1"],
                        timeout_s=30,
                        hypothesis="register answers 201 promptly",
                    ),
                ],
                metadata={
                    "contract_clauses": SMOKE_CLAUSES,
                    "repo_root": str(tree),
                },
            )
            store.save(plan)
            contract = GoalContract(
                objective="pairing goal", budget=RunBudget(max_replans=2)
            )

            def fake_call_llm(system_prompt, user_prompt, settings=None):
                if "scoped-edit repairer" in system_prompt:
                    if "REJECTED" in user_prompt:
                        return HANG_FIX_BLOCK
                    return PROSE_ANSWER
                # Any other seam (local repair, if it calls) gets
                # prose it cannot use, so it declines and the
                # scoped-edit seam runs — the arm #22 order.
                return PROSE_ANSWER

            with mock.patch.object(planner_module, "call_llm", fake_call_llm):
                result = runner.run_plan(plan, contract)
            # The seam's candidate repaired the hang: v2 passes.
            self.assertEqual(result.goal_verdict, "PASS")
            self.assertEqual(result.plan_versions, 2)
            # Two seam model calls, exactly ONE re-plan unit: the
            # re-ask lives inside the seam invocation.
            self.assertEqual(
                [d["decision"] for d in result.decision_history],
                ["replan", "continue"],
            )
            seam_records = run_store.read_seam_attempts(result.run_id)
            scoped = [
                record
                for record in seam_records
                if record["seam"] == "scoped_edit"
            ]
            self.assertEqual(len(scoped), 2)
            self.assertEqual(scoped[0]["attempt"], 1)
            self.assertEqual(scoped[0]["outcome"], "rejected")
            self.assertEqual(scoped[0]["decline_reason"], "no_edit_blocks")
            self.assertEqual(scoped[0]["response_text"], PROSE_ANSWER)
            self.assertEqual(scoped[1]["attempt"], 2)
            self.assertEqual(scoped[1]["outcome"], "accepted")
            self.assertTrue(scoped[1]["shape_repair"])
            self.assertEqual(scoped[1]["response_text"], HANG_FIX_BLOCK)
            self.assertTrue(
                all(
                    record["kind"] == "seam_attempt"
                    for record in seam_records
                )
            )
            # Routing: no seam record leaked into the planner file.
            planner_records = run_store.read_planner_attempts(result.run_id)
            self.assertTrue(
                all(
                    record.get("kind") != "seam_attempt"
                    for record in planner_records
                )
            )


if __name__ == "__main__":
    unittest.main()
