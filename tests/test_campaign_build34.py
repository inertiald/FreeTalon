"""Campaign build #34: probe positive controls + post-patch re-probe.

Held-out A run7 at build #33 shipped the blind spot: a family
patch fixed all 11 diagnosed method-matrix defects while the
tree answered a legitimate GET /queue/jobs with 405 — invisible
to the loop, because the method_matrix family was negative
cells only (every measured GET was a wrong-method GET on a
POST-only route; the parent read the family's 34 measured
members firsthand and found no positive control among them).
The re-screen proved the over-block was generation variance;
the blind spot is nevertheless real, and this build closes it
with the two scoped changes Pat's freeze allows:

- positive controls: _method_matrix_flows now also emits, from
  the same route table, one cell per route x method the route
  DEFINES — the answer must be the route's own, never the 405
  of a wrong method or the 404 of an unknown route;
- post-patch re-probe: a direct-repair patch that flips a
  measurement which was passing on the standing tree is a
  FAILED attempt (file restored, flip named in the retry),
  judged by the loop's re-run of the standing probe set
  (contract_clauses.probe_flips).

Tested at the levels the build operates:

- the derivation: scripted requesters over both route tables
  (controls present, named, appended; a correct server passes
  all; the run7 over-block signature — negatives green, the
  legitimate GET refused — is caught by exactly the controls),
  plus the real jobqueue runner against an over-blocking
  fixture server;
- probe_flips: the pure comparison (pass->fail and
  pass->vanished are flips; fail->pass is the repair working;
  other files are not this patch's blast radius);
- the executor (mocked call_llm, scripted reprobe): a flipping
  patch is rejected and rolled back byte-for-byte, the retry
  prompt names the flip, a clean patch inside the budget is
  accepted; flips on every attempt exhaust with the standing
  file untouched; no hook is build #32 exactly;
- the loop pass (real store probes): a family fix that also
  breaks the family's passing sibling is refused by the
  re-probe and retried; the clean fix lands, and the lineage
  carries the flip on the obligation, the per-defect summary,
  and the markdown.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    _method_matrix_flows,
    probe_flips,
    run_contract_probes,
)
from freetalon.orchestrator.gates import GateStore
from freetalon.orchestrator.lineage import (
    LineageRecorder,
    defect_id_for_record,
    drain_pending_lineage,
)
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    TaskIntent,
)
from freetalon.orchestrator.planner import (
    drain_pending_planner_attempts,
    execute_direct_repair_obligation,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)
from tests.test_campaign_build23 import (
    FIXTURE_JOBQUEUE_SERVER_GOOD,
    JOBQUEUE_CLAUSES,
    _jobqueue_tree,
)
from tests.test_campaign_build33 import (
    CLAIM_DEFECT,
    CORRUPT_DEFECT,
    EXPIRY_DEFECT,
    FAMILY_CLAUSE,
    FAMILY_FIX,
    FIXTURE_STORE_FAMILY_DEFECTIVE,
    STORE_PATH,
)

UNKNOWN = "/smoke-probe/no-such-route"

PAIRING_ROUTES = [
    ("/pairing/register", frozenset({"POST"}), "known"),
    ("/pairing", frozenset({"GET"}), "list"),
    ("/pairing/SMOKE-0001", frozenset({"GET"}), "poll"),
    ("/pairing/SMOKE-0001/confirm", frozenset({"POST"}), "confirm"),
]

JOBQUEUE_ROUTES = [
    ("/queue/jobs", frozenset({"GET", "POST"}), "known"),
    ("/queue/claim", frozenset({"POST"}), "claim"),
    ("/queue/jobs/job-000001", frozenset({"GET"}), "item"),
    ("/queue/jobs/job-000001/complete", frozenset({"POST"}), "complete"),
    ("/queue/jobs/job-000001/fail", frozenset({"POST"}), "fail"),
]

# A correct jobqueue server's own answers to bare legitimate
# requests: reads succeed; the action routes answer with their
# contract request/state answers. None is 404 or 405.
JOBQUEUE_ROUTE_ANSWERS = {
    ("GET", "/queue/jobs"): 200,
    ("POST", "/queue/jobs"): 400,
    ("POST", "/queue/claim"): 400,
    ("GET", "/queue/jobs/job-000001"): 200,
    ("POST", "/queue/jobs/job-000001/complete"): 409,
    ("POST", "/queue/jobs/job-000001/fail"): 409,
}

EXPECTED_JOBQUEUE_CONTROLS = [
    "positive_control_get_known",
    "positive_control_post_known",
    "positive_control_post_on_claim",
    "positive_control_get_on_item",
    "positive_control_post_on_complete",
    "positive_control_post_on_fail",
]

EXPECTED_PAIRING_CONTROLS = [
    "positive_control_post_known",
    "positive_control_get_on_list",
    "positive_control_get_on_poll",
    "positive_control_post_on_confirm",
]


def _requester(answers, *, over_block=()):
    """A scripted requester: wrong methods 405 on known paths,
    404 on the unknown path, `answers` on defined pairs — except
    the pairs in `over_block`, which are refused with 405 (or a
    (pair, status) entry's status) like an over-blocking server.
    """
    refused = dict(over_block) if over_block else {}

    def request(name, method, path):
        if (method, path) in refused:
            return (refused[(method, path)], b""), None
        if (method, path) in answers:
            return (answers[(method, path)], b""), None
        return ((404 if path == UNKNOWN else 405), b""), None

    return request


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _ScriptedLLM:
    """A call_llm stand-in: queued replies, captured prompts."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, system_prompt, prompt, settings=None):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("the scripted LLM ran out of replies")
        return self.replies.pop(0)


class _DrainSinks:
    def tearDown(self):
        drain_pending_lineage(lambda record: None)
        drain_pending_planner_attempts(lambda record: None)
        super().tearDown()


# ── Part A: the positive-control derivation ────────────────────


class TestPositiveControlDerivation(unittest.TestCase):
    def test_jobqueue_controls_present_named_and_appended(self):
        flows = _method_matrix_flows(
            _requester(JOBQUEUE_ROUTE_ANSWERS),
            routes=JOBQUEUE_ROUTES,
            unknown_path=UNKNOWN,
        )
        names = [name for name, _ok, _d in flows]
        self.assertEqual(len(names), 40)
        self.assertEqual(names[34:], EXPECTED_JOBQUEUE_CONTROLS)
        self.assertTrue(all(ok for _n, ok, _d in flows))

    def test_pairing_controls_present_named_and_appended(self):
        answers = {
            ("POST", "/pairing/register"): 400,
            ("GET", "/pairing"): 401,
            ("GET", "/pairing/SMOKE-0001"): 410,
            ("POST", "/pairing/SMOKE-0001/confirm"): 401,
        }
        flows = _method_matrix_flows(
            _requester(answers),
            routes=PAIRING_ROUTES,
            unknown_path=UNKNOWN,
        )
        names = [name for name, _ok, _d in flows]
        self.assertEqual(len(names), 33)
        self.assertEqual(names[29:], EXPECTED_PAIRING_CONTROLS)
        self.assertTrue(all(ok for _n, ok, _d in flows))

    def test_run7_signature_negatives_green_control_red(self):
        # The build #33 run7 server, in miniature: wrong methods
        # refused correctly, the legitimate collection GET ALSO
        # refused with 405. Every negative cell passes; the one
        # control catches it.
        flows = _method_matrix_flows(
            _requester(
                JOBQUEUE_ROUTE_ANSWERS,
                over_block={("GET", "/queue/jobs"): 405},
            ),
            routes=JOBQUEUE_ROUTES,
            unknown_path=UNKNOWN,
        )
        failing = [(n, d) for n, ok, d in flows if not ok]
        self.assertEqual([n for n, _d in failing], ["positive_control_get_known"])
        self.assertIn(
            "positive_control_get_known_status: expected the "
            "route's own answer, got 405",
            failing[0][1],
        )
        self.assertIn("over-block", failing[0][1])
        self.assertTrue(
            all(
                ok
                for n, ok, _d in flows
                if n.startswith("method_matrix_")
            )
        )

    def test_blanket_404_control_fails_with_its_own_fact(self):
        flows = _method_matrix_flows(
            _requester(
                JOBQUEUE_ROUTE_ANSWERS,
                over_block={("GET", "/queue/jobs/job-000001"): 404},
            ),
            routes=JOBQUEUE_ROUTES,
            unknown_path=UNKNOWN,
        )
        failing = [(n, d) for n, ok, d in flows if not ok]
        self.assertEqual([n for n, _d in failing], ["positive_control_get_on_item"])
        self.assertIn("answered as unknown", failing[0][1])

    def test_blocked_route_fails_its_control_with_the_setup_detail(self):
        routes = [
            ("/queue/jobs", frozenset({"GET", "POST"}), "known"),
            ("/queue/claim", frozenset({"POST"}), "claim"),
            (None, frozenset({"GET"}), "item"),
            (None, frozenset({"POST"}), "complete"),
            (None, frozenset({"POST"}), "fail"),
        ]
        calls = []

        def recording_requester(name, method, path):
            calls.append((method, path))
            return _requester(JOBQUEUE_ROUTE_ANSWERS)(name, method, path)

        flows = _method_matrix_flows(
            recording_requester,
            routes=routes,
            unknown_path=UNKNOWN,
            blocked_detail="setup: no id minted",
        )
        by_name = {n: (ok, d) for n, ok, d in flows}
        for name in (
            "positive_control_get_on_item",
            "positive_control_post_on_complete",
            "positive_control_post_on_fail",
        ):
            ok, detail = by_name[name]
            self.assertFalse(ok)
            self.assertEqual(detail, "setup: no id minted")
        # No request was issued for a blocked control.
        self.assertTrue(
            all(path is not None for _m, path in calls)
        )
        self.assertNotIn(("GET", None), calls)


# A jobqueue server with run7's exact defect: the collection
# GET is refused with the wrong-method answer; everything else
# is the good fixture.
FIXTURE_JOBQUEUE_SERVER_OVERBLOCK_GET = (
    FIXTURE_JOBQUEUE_SERVER_GOOD.replace(
        '            self._json(200, {"jobs": list(type(self).jobs.values())})',
        '            self._json(405, {"error": "method not allowed"})',
    )
)
assert FIXTURE_JOBQUEUE_SERVER_OVERBLOCK_GET != FIXTURE_JOBQUEUE_SERVER_GOOD


class TestPositiveControlsAgainstTheRealRunner(unittest.TestCase):
    def test_overblocking_jobqueue_server_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _jobqueue_tree(
                Path(tmp), FIXTURE_JOBQUEUE_SERVER_OVERBLOCK_GET
            )
            results = run_contract_probes(JOBQUEUE_CLAUSES, root)
            matrix = [
                r for r in results if r["probe"] == "method_matrix"
            ]
            self.assertEqual(len(matrix), 40)
            failing = [r["flow"] for r in matrix if not r["ok"]]
            self.assertEqual(failing, ["positive_control_get_known"])
            # The whole negative sweep is green: before this
            # build, no record the loop produces could see the
            # defect at all.
            self.assertTrue(
                all(
                    r["ok"]
                    for r in matrix
                    if r["flow"].startswith("method_matrix_")
                )
            )


# ── Part B: probe_flips, the pure comparison ───────────────────


def _rec(flow, ok, *, clause="C09-http", probe="method_matrix",
         file="pkg/server.py", detail=""):
    return {
        "clause_id": clause,
        "probe": probe,
        "flow": flow,
        "ok": ok,
        "detail": detail,
        "file": file,
    }


class TestProbeFlips(unittest.TestCase):
    BASELINE = [
        _rec("positive_control_get_known", True, detail="was 200"),
        _rec("method_matrix_delete_known", False, detail="was 404"),
        _rec("keep_check", True, clause="C01-x", probe="store_behavior",
             detail="held"),
        _rec("other_file_flow", True, file="pkg/other.py"),
    ]

    def test_pass_to_fail_and_pass_to_vanished_are_flips(self):
        fresh = [
            _rec("positive_control_get_known", False, detail="now 405"),
            _rec("method_matrix_delete_known", True, detail="now 405"),
            # keep_check vanished entirely.
            _rec("other_file_flow", False, file="pkg/other.py"),
        ]
        flips = probe_flips(self.BASELINE, fresh, target="pkg/server.py")
        self.assertEqual(
            [f["defect_id"] for f in flips],
            [
                "C09-http::method_matrix::positive_control_get_known",
                "C01-x::store_behavior::keep_check",
            ],
        )
        self.assertIn("now 405", flips[0]["detail"])
        self.assertIn("no longer exists", flips[1]["detail"])
        self.assertTrue(all(f["file"] == "pkg/server.py" for f in flips))

    def test_fail_to_pass_is_the_repair_not_a_flip(self):
        fresh = [
            _rec("positive_control_get_known", True),
            _rec("method_matrix_delete_known", True),
            _rec("keep_check", True, clause="C01-x",
                 probe="store_behavior"),
            _rec("other_file_flow", True, file="pkg/other.py"),
        ]
        self.assertEqual(
            probe_flips(self.BASELINE, fresh, target="pkg/server.py"), []
        )

    def test_no_baseline_measurements_no_flips(self):
        self.assertEqual(probe_flips([], [], target="pkg/server.py"), [])
        self.assertEqual(
            probe_flips(self.BASELINE, [], target="pkg/unmeasured.py"),
            [],
        )


# ── Part C: the executor with a re-probe hook ──────────────────

MOD_STANDING = "VALUE = 1\nOTHER = 2\n"
MOD_RECORD = {
    "clause_id": "C01-fixture",
    "probe": "store_behavior",
    "flow": "fix_value",
    "detail": "the measured failure: VALUE must be 3",
    "file": "pkg/mod.py",
}
MOD_DEFECT = "C01-fixture::store_behavior::fix_value"
MOD_FIX = json.dumps(
    {"edits": [{"search": "VALUE = 1\n", "replace": "VALUE = 3\n"}]}
)
MOD_FIX_V2 = json.dumps(
    {
        "edits": [
            {"search": "VALUE = 1\n", "replace": "VALUE = 3\n"},
            {"search": "OTHER = 2\n", "replace": "OTHER = 2\n# kept\n"},
        ]
    }
)
KEEP_FLIP = {
    "defect_id": "C01-fixture::store_behavior::keep_check",
    "clause_id": "C01-fixture",
    "probe": "store_behavior",
    "flow": "keep_check",
    "ok": False,
    "detail": "keep_check: the keep check now fails",
    "file": "pkg/mod.py",
}


class _ScriptedReprobe:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.script:
            raise AssertionError("the reprobe script ran out")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class TestExecutorReprobe(_DrainSinks, unittest.TestCase):
    def _run(self, root: Path, llm: _ScriptedLLM, **overrides):
        kwargs = {
            "defect_id": MOD_DEFECT,
            "record": dict(MOD_RECORD),
            "target": "pkg/mod.py",
            "repo_root": root,
            "goal_text": "fix the fixture",
        }
        kwargs.update(overrides)
        with mock.patch.object(planner_module, "call_llm", llm):
            return execute_direct_repair_obligation(**kwargs)

    def test_flip_rejects_restores_and_retries_inside_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            llm = _ScriptedLLM([MOD_FIX, MOD_FIX_V2])
            reprobe = _ScriptedReprobe([[dict(KEEP_FLIP)], []])
            outcome = self._run(root, llm, reprobe=reprobe)
            self.assertEqual(outcome["status"], "patched")
            self.assertEqual(reprobe.calls, 2)
            attempts = outcome["attempts"]
            self.assertEqual(
                [a["outcome"] for a in attempts], ["rejected", "accepted"]
            )
            self.assertEqual(
                attempts[0]["flips"],
                ["C01-fixture::store_behavior::keep_check"],
            )
            self.assertEqual(
                [f["defect_id"] for f in outcome["reprobe_flips"]],
                ["C01-fixture::store_behavior::keep_check"],
            )
            # The retry prompt names the regression.
            self.assertIn("regressed: defect", llm.prompts[1])
            self.assertIn("keep_check", llm.prompts[1])
            # The accepted content is attempt 2's, whole.
            self.assertEqual(
                (root / "pkg" / "mod.py").read_text(encoding="utf-8"),
                "VALUE = 3\nOTHER = 2\n# kept\n",
            )

    def test_flips_on_every_attempt_exhaust_with_the_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            llm = _ScriptedLLM([MOD_FIX, MOD_FIX, MOD_FIX])
            reprobe = _ScriptedReprobe(
                [[dict(KEEP_FLIP)], [dict(KEEP_FLIP)], [dict(KEEP_FLIP)]]
            )
            outcome = self._run(root, llm, reprobe=reprobe)
            self.assertEqual(outcome["status"], "exhausted")
            self.assertFalse(outcome["changed"])
            self.assertEqual(
                [a["outcome"] for a in outcome["attempts"]],
                ["rejected", "rejected", "rejected"],
            )
            self.assertEqual(len(outcome["reprobe_flips"]), 3)
            self.assertEqual(
                (root / "pkg" / "mod.py").read_text(encoding="utf-8"),
                MOD_STANDING,
            )

    def test_an_unrunnable_reprobe_is_judged_as_a_flip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            llm = _ScriptedLLM([MOD_FIX, MOD_FIX_V2])
            reprobe = _ScriptedReprobe([RuntimeError("probe env down"), []])
            outcome = self._run(root, llm, reprobe=reprobe)
            self.assertEqual(outcome["status"], "patched")
            self.assertEqual(
                outcome["attempts"][0]["flips"], ["reprobe"]
            )
            self.assertIn(
                "could not run", outcome["reprobe_flips"][0]["detail"]
            )

    def test_no_hook_is_build32_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pkg" / "mod.py", MOD_STANDING)
            llm = _ScriptedLLM([MOD_FIX])
            outcome = self._run(root, llm)
            self.assertEqual(outcome["status"], "patched")
            self.assertNotIn("reprobe_flips", outcome)
            self.assertNotIn("flips", outcome["attempts"][0])


# ── Part D: the loop pass, real probes, a flipping family fix ──

# The family fix PLUS a second edit that quietly retires the
# corrupt-file raise — the family's passing sibling flips.
_FAMILY_FIX_PARSED = json.loads(FAMILY_FIX)
BAD_FIX = json.dumps(
    {
        "edits": _FAMILY_FIX_PARSED["edits"]
        + [
            {
                "search": (
                    "            try:\n"
                    "                data = json.loads("
                    "self._path.read_text(encoding=\"utf-8\"))\n"
                    "            except ValueError as exc:\n"
                    "                raise StoreCorruptError(str(exc)) "
                    "from exc\n"
                ),
                "replace": (
                    "            try:\n"
                    "                data = json.loads("
                    "self._path.read_text(encoding=\"utf-8\"))\n"
                    "            except ValueError:\n"
                    "                data = {\"records\": {}}\n"
                ),
            }
        ]
    }
)


class TestLoopPassReprobe(_DrainSinks, unittest.TestCase):
    def test_flipping_family_fix_is_refused_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = root / "tree"
            _write(tree / "freetalon" / "__init__.py", "")
            _write(tree / "freetalon" / "pairing" / "__init__.py", "")
            _write(tree / STORE_PATH, FIXTURE_STORE_FAMILY_DEFECTIVE)
            store = ExecutionPlanStateStore(root / "state.db")
            registry = ToolRegistry(strict=True)
            register_default_claws(registry)
            run_store = RunStore(root / "runs")
            gates = GateStore(root / "state.db")
            runner = LoopRunner(
                store, registry, run_store=run_store, gate_store=gates
            )
            plan = ExecutionPlan(
                plan_id="b34-reprobe",
                intent=TaskIntent(goal="fix the pairing store"),
                nodes=[],
                metadata={
                    "repo_root": str(tree),
                    "contract_clauses": [FAMILY_CLAUSE],
                },
            )
            results = run_contract_probes([FAMILY_CLAUSE], tree)
            failing = [r for r in results if not r["ok"]]
            self.assertEqual(
                sorted(defect_id_for_record(r) for r in failing),
                [CLAIM_DEFECT, EXPIRY_DEFECT],
            )
            recorder = LineageRecorder(run_store, "run-reprobe")
            recorder.note_baseline(
                {STORE_PATH: FIXTURE_STORE_FAMILY_DEFECTIVE.encode()}
            )
            recorder.diagnosis(1, results, failing)
            version_outcomes = {
                1: {
                    "version": 1,
                    "probe_green": 2,
                    "probe_total": 4,
                    "failed_nodes": 2,
                    "failed_criteria": 1,
                }
            }
            recorder.version_verified(
                1,
                version_outcomes[1],
                {STORE_PATH: FIXTURE_STORE_FAMILY_DEFECTIVE.encode()},
            )
            llm = _ScriptedLLM([BAD_FIX, FAMILY_FIX])
            with mock.patch.object(planner_module, "call_llm", llm):
                result = runner._direct_repair_pass(
                    "run-reprobe",
                    plan,
                    parent_version=1,
                    failures=failing,
                    goal_text="fix the pairing store",
                    declared_targets_all=set(),
                    version_outcomes=version_outcomes,
                    version_measurements_map={1: []},
                    version_probe_results={1: list(results)},
                    version_snapshots={},
                    repair_candidate_versions=set(),
                    lineage_recorder=recorder,
                )
            # Two model calls: the flipping fix was refused, the
            # clean fix landed. The retry prompt names the flip.
            self.assertEqual(len(llm.prompts), 2)
            self.assertIn("regressed: defect", llm.prompts[1])
            self.assertIn("store_corrupt_raises", llm.prompts[1])
            self.assertIsNotNone(result)
            self.assertEqual(result["version"], 2)
            self.assertEqual(result["failures"], [])
            self.assertEqual(version_outcomes[2]["probe_green"], 4)
            recorder.finish(
                shipping=None,
                escalation=None,
                decision_history=[],
                attempts=[],
                goal_verdict="FAIL",
                goal_detail="",
                plan_versions=2,
            )
            record = json.loads(
                (run_store.run_dir("run-reprobe") / "lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            obligations = record["obligations"][0]["obligations"]
            self.assertEqual(len(obligations), 2)
            for item in obligations:
                self.assertEqual(item["status"], "patched")
                self.assertEqual(
                    [a["outcome"] for a in item["attempts"]],
                    ["rejected", "accepted"],
                )
                self.assertEqual(
                    item["attempts"][0]["flips"], [CORRUPT_DEFECT]
                )
                self.assertEqual(
                    [f["defect_id"] for f in item["reprobe_flips"]],
                    [CORRUPT_DEFECT],
                )
            row = record["defects"][CLAIM_DEFECT]
            self.assertEqual(
                row["direct_repair"]["reprobe_flips"], [CORRUPT_DEFECT]
            )
            table_md = (
                run_store.run_dir("run-reprobe") / "lineage.md"
            ).read_text(encoding="utf-8")
            self.assertIn("re-probe flips:", table_md)
            self.assertIn("store_corrupt_raises", table_md)


class TestSuspectCapCoverage(unittest.TestCase):
    """The seam's suspect cap judges partial evidence, not a count.

    Build #34 fallout, found by the full suite: a hang-everything
    pairing server fails 12 probe flows under the enlarged probe
    set (8 at build #33), and _scoped_edit_targets declined
    suspect_cap_exceeded on the raw count even though the
    probe-derived suspect map covered every failing entry and
    named exactly one produced file — the seam never ran and the
    run died in the whole-task re-plan. The cap now bites only
    when the map actually is partial (an entry it does not
    cover), which is the partial-evidence case it exists for.
    """

    CONTENT = "def handler():\n    return 200\n"

    def _targets(self, failing, suspect_map):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = root / "pkg" / "server.py"
        target.parent.mkdir(parents=True)
        target.write_text(self.CONTENT, encoding="utf-8")
        node = PlanNode(
            id="node-1",
            objective="Write the server module",
            assigned_claw="scaffold",
            status=PlanStatus.COMPLETED,
            inputs={"file_path": str(target), "content": self.CONTENT},
        )
        plan = ExecutionPlan(
            plan_id="plan-build34-cap",
            intent=TaskIntent(goal="pairing goal"),
            nodes=[node],
        )
        brief = {"failing_tests": failing, "failed_nodes": [{"id": "node-2"}]}
        declines: list[dict] = []
        result = planner_module._scoped_edit_targets(
            plan, brief, root, suspect_map, declines
        )
        return result, declines

    def test_complete_map_over_the_cap_still_targets(self):
        failing = [f"probe:flow_{i}" for i in range(12)]
        suspect_map = {
            name: {"file": None, "modules": ["pkg/server.py"]}
            for name in failing
        }
        result, declines = self._targets(failing, suspect_map)
        self.assertEqual(declines, [])
        self.assertIsNotNone(result)
        self.assertEqual([t.relative_path for t in result], ["pkg/server.py"])

    def test_partial_map_over_the_cap_still_declines(self):
        failing = [f"probe:flow_{i}" for i in range(12)]
        suspect_map = {
            name: {"file": None, "modules": ["pkg/server.py"]}
            for name in failing[:-1]
        }
        result, declines = self._targets(failing, suspect_map)
        self.assertIsNone(result)
        self.assertEqual(declines[0]["reason"], "suspect_cap_exceeded")


if __name__ == "__main__":
    unittest.main()
