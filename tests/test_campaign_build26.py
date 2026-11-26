"""Campaign build #26: planning failure is a recorded outcome, and
the HTTP method surface is gated at planning time.

Arm #34 (2026-11-26) died before execution: the pairing skeleton
proposed a justified whole-file rewrite of the package __init__.py
on all three skeleton_intent attempts but never added the
impact-check companion the override demands; the repair loop
oscillated between that rejection and a suite_placement rejection
until the budget exhausted, the terminal LLMResponseError was
recorded by the build #17 machinery and then re-raised into a
driver with no handler — the process died with a traceback and the
LoopRunner never ran. Held-out A at the same stack split 24/24 +
23/24 on the DELETE cell: build #25's method-surface guidance is
prompt text, so whether the shipped handler defined the closed
method set stayed a coin flip — detection existed after execution
(the static probe, the live matrix), but nothing at planning or
fill time declined a deficient handler.

The build is tested at the levels it operates:

- companion synthesis (parse): a justified whole_file node whose
  only defect is the missing impact-check companion is accepted
  with a deterministic companion synthesized by parse — the
  justification is the model's claim (never fabricated: its
  absence keeps the rejection), the companion is a mechanical
  form the rule itself fully determines;
- planning outcome (planner + loop): a terminal LLMResponseError
  from plan_task_intent returns a FAILED plan carrying the
  planning_failure stamp (the build #17 record is still written),
  and LoopRunner.run_plan finishes such a plan as a recorded FAIL
  with zero versions and zero re-plans — never an exception;
- method-surface gate (contract_clauses + planner): the AST gaps
  helper, the fill-level decline (mirroring build #22's import
  grounding, with its standing-gap conservatism), and the
  parse-level rejection for pre-filled plans — all gated on a
  method_surface clause, so the contract decides where the gate
  applies.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from freetalon.orchestrator.contract_clauses import handler_surface_gaps
from freetalon.orchestrator.intake import LLMResponseError, LLMSettings
from freetalon.orchestrator.loop import LoopRunner
from freetalon.orchestrator.measure import VERDICT_FAIL
from freetalon.orchestrator.memory import ExperimentStore
from freetalon.orchestrator.models import (
    ExecutionPlan,
    GoalContract,
    PlanStatus,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.planner import (
    _filled_candidate_errors,
    drain_pending_planner_attempts,
    parse_planner_response,
    plan_task_intent,
)
from freetalon.orchestrator.run_store import RunStore
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)

JUSTIFICATION = (
    "The existing __init__.py has 0 definitions (empty); the "
    "contract requires it to re-export the package's public names."
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

SERVER_SOURCE_EXTENDED = SERVER_SOURCE + (
    "\n"
    "    def handle_twice(self):\n"
    "        return self.server\n"
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


def _bash_node(node_id, command="true", depends_on=None, **extra_inputs):
    inputs = {"command": command}
    inputs.update(extra_inputs)
    return {
        "id": node_id,
        "objective": f"Run {command}",
        "depends_on": depends_on or [],
        "assigned_claw": "bash",
        "inputs": inputs,
        "criteria": [{"type": "exit_code", "equals": 0}],
    }


def _payload(*nodes) -> str:
    return json.dumps({"nodes": list(nodes), "metadata": {}})


class _TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "server.py").write_text(SERVER_SOURCE, encoding="utf-8")


# ── Companion synthesis ─────────────────────────────────────────────


class TestImpactCheckSynthesis(_TreeCase):
    def _parse(self, text, **kwargs):
        return parse_planner_response(
            text, repo_root=self.root, skeleton=True, **kwargs
        )

    def test_arm34_shape_is_accepted_with_synthesized_companion(self) -> None:
        # The arm #34 skeleton, verbatim in shape: a justified
        # whole-file rewrite of the existing empty __init__.py and
        # no impact-check companion. Before this build it was the
        # fatal rejection; parse now supplies the companion the
        # rule determines.
        nodes, metadata = self._parse(
            _payload(
                _write_node(
                    "step_3",
                    "pkg/__init__.py",
                    "whole_file",
                    overwrite_justification=JUSTIFICATION,
                )
            )
        )
        self.assertEqual(len(nodes), 2)
        write = nodes[0]
        companion = nodes[1]
        self.assertEqual(companion.depends_on, [write.id])
        self.assertEqual(
            companion.inputs.get("impact_check_for"), "pkg/__init__.py"
        )
        self.assertEqual(companion.assigned_claw, "bash")
        # No tests/test_pkg.py exists in the fixture tree, so the
        # derived check imports the rewritten package.
        self.assertEqual(
            companion.inputs.get("command"), 'python3 -c "import pkg"'
        )
        self.assertEqual(
            companion.criteria, [{"type": "exit_code", "equals": 0}]
        )
        records = metadata.get("impact_check_synthesis")
        self.assertTrue(records)
        self.assertEqual(records[0]["for_node"], write.id)
        self.assertEqual(records[0]["target"], "pkg/__init__.py")
        self.assertEqual(records[0]["node"], companion.id)

    def test_companion_command_prefers_the_modules_tests(self) -> None:
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_server.py").write_text(
            "import unittest\n", encoding="utf-8"
        )
        nodes, _metadata = self._parse(
            _payload(
                _write_node(
                    "w1",
                    "pkg/server.py",
                    "whole_file",
                    overwrite_justification=JUSTIFICATION,
                )
            )
        )
        companion = nodes[-1]
        self.assertEqual(
            companion.inputs.get("command"), "python3 tests/test_server.py"
        )

    def test_companion_command_uses_flattened_test_name(self) -> None:
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_pkg_server.py").write_text(
            "import unittest\n", encoding="utf-8"
        )
        nodes, _metadata = self._parse(
            _payload(
                _write_node(
                    "w1",
                    "pkg/server.py",
                    "whole_file",
                    overwrite_justification=JUSTIFICATION,
                )
            )
        )
        companion = nodes[-1]
        self.assertEqual(
            companion.inputs.get("command"),
            "python3 tests/test_pkg_server.py",
        )

    def test_no_justification_no_synthesis(self) -> None:
        # The justification is the model's claim to make; without
        # it the rejection stands exactly as before.
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                _payload(_write_node("w1", "pkg/__init__.py", "whole_file"))
            )
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("skeleton_intent", codes)

    def test_existing_companion_is_not_duplicated(self) -> None:
        nodes, metadata = self._parse(
            _payload(
                _write_node(
                    "w1",
                    "pkg/__init__.py",
                    "whole_file",
                    overwrite_justification=JUSTIFICATION,
                ),
                _bash_node(
                    "check",
                    "python3 -m py_compile pkg/__init__.py",
                    depends_on=["w1"],
                    impact_check_for="pkg/__init__.py",
                ),
            )
        )
        self.assertEqual(len(nodes), 2)
        self.assertFalse(metadata.get("impact_check_synthesis"))

    def test_missing_target_stays_create_domain(self) -> None:
        # whole_file on a target that exists nowhere is corrected
        # to "create" (build #11); no companion is synthesized for
        # a create.
        nodes, metadata = self._parse(
            _payload(
                _write_node(
                    "w1",
                    "pkg/store.py",
                    "whole_file",
                    overwrite_justification=JUSTIFICATION,
                )
            )
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].inputs["content_intent"], "create")
        self.assertFalse(metadata.get("impact_check_synthesis"))

    def test_non_python_target_is_not_synthesized(self) -> None:
        (self.root / "pkg" / "data.txt").write_text(
            "standing data\n", encoding="utf-8"
        )
        with self.assertRaises(LLMResponseError) as caught:
            self._parse(
                _payload(
                    _write_node(
                        "w1",
                        "pkg/data.txt",
                        "whole_file",
                        overwrite_justification=JUSTIFICATION,
                    )
                )
            )
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("skeleton_intent", codes)

    def test_prefilled_whole_file_also_gains_companion(self) -> None:
        # The single-shot shape: content rides the node itself and
        # the assembly rule (_scaffold_overwrite_errors) is the one
        # that would reject. Parse synthesizes the companion there
        # too.
        node = _write_node(
            "w1",
            "pkg/server.py",
            None,
            content=SERVER_SOURCE_EXTENDED,
            overwrite_justification=JUSTIFICATION,
        )
        nodes, metadata = parse_planner_response(
            _payload(node), repo_root=self.root, skeleton=False
        )
        self.assertEqual(len(nodes), 2)
        self.assertEqual(
            nodes[1].inputs.get("impact_check_for"), "pkg/server.py"
        )
        self.assertTrue(metadata.get("impact_check_synthesis"))


# ── Planning failure is a recorded outcome ──────────────────────────

UNJUSTIFIED_SKELETON = json.dumps(
    {
        "nodes": [
            _write_node("step_3", "pkg/__init__.py", "whole_file"),
        ],
        "metadata": {},
    }
)


class TestPlanningFailureOutcome(_TreeCase):
    def setUp(self) -> None:
        super().setUp()
        drain_pending_planner_attempts(lambda record: None)
        self.settings = LLMSettings(
            backend="ollama", base_url="http://localhost:11434", model="test"
        )

    def _plan(self, intent):
        def _call(system_prompt, user_prompt, settings=None):
            return UNJUSTIFIED_SKELETON

        with patch(
            "freetalon.orchestrator.planner.call_llm", side_effect=_call
        ):
            return plan_task_intent(
                intent, settings=self.settings, repo_root=self.root
            )

    def test_terminal_planning_failure_returns_failed_plan(self) -> None:
        workspace = Path(self._tmp.name) / "ws"
        workspace.mkdir()
        intent = TaskIntent(
            goal="Implement the pairing package",
            constraints={"workspace": str(workspace)},
        )
        plan = self._plan(intent)
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertEqual(plan.nodes, [])
        stamp = plan.metadata.get("planning_failure")
        self.assertIsInstance(stamp, dict)
        self.assertIn("whole-file override", stamp["error"])
        run_id = stamp.get("run_id")
        self.assertIsNotNone(run_id)
        # The build #17 record still exists, unchanged in shape.
        run_dir = workspace / "runs" / str(run_id)
        self.assertTrue(run_dir.is_dir())
        contract = json.loads(
            (run_dir / "contract.json").read_text(encoding="utf-8")
        )
        self.assertIn("planning_failure", contract)
        summary = (run_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Goal verdict: **FAIL**", summary)

    def test_terminal_failure_without_workspace_still_returns(self) -> None:
        intent = TaskIntent(goal="Implement the pairing package")
        plan = self._plan(intent)
        self.assertEqual(plan.status, PlanStatus.FAILED)
        stamp = plan.metadata.get("planning_failure")
        self.assertIsInstance(stamp, dict)
        self.assertIsNone(stamp.get("run_id"))


class TestLoopFinishesPlanningFailure(unittest.TestCase):
    def test_run_plan_records_fail_without_executing(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        store = ExecutionPlanStateStore(db_path=base / "state.db")
        registry = ToolRegistry(strict=True)
        register_default_claws(registry)
        runner = LoopRunner(
            store,
            registry,
            run_store=RunStore(base / "runs"),
            memory_store=ExperimentStore(base / "memory.db"),
        )
        intent = TaskIntent(goal="Implement the pairing package")
        # The exact shape plan_task_intent's terminal-failure
        # conversion returns (constructed directly: the plan
        # builder refuses an empty node list by design).
        plan = ExecutionPlan(
            plan_id="plan-build26-loop-test",
            intent=intent,
            nodes=[],
            status=PlanStatus.FAILED,
            metadata={
                "planning_failure": {
                    "error": "Planner output was rejected: whole-file "
                    "override missing",
                    "error_type": "LLMResponseError",
                    "run_id": None,
                }
            },
        )
        contract = GoalContract(
            objective="Implement the pairing package",
            success_measures=[],
            budget=RunBudget(max_replans=3),
        )
        result = runner.run_plan(plan, contract)
        self.assertEqual(result.goal_verdict, VERDICT_FAIL)
        self.assertIn("planning failed", result.escalation or "")
        self.assertEqual(result.attempts, [])
        self.assertEqual(result.decision_history, [])
        self.assertEqual(result.plan_versions, 1)


# ── Method-surface gate ─────────────────────────────────────────────

CLAUSE_405 = {
    "id": "C405-method",
    "text": "A known path with the wrong method returns 405.",
    "method_surface": True,
}

HANDLER_DEFICIENT = (
    "from http.server import BaseHTTPRequestHandler\n"
    "\n"
    "\n"
    "class QueueHandler(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        pass\n"
    "\n"
    "    def do_POST(self):\n"
    "        pass\n"
)

HANDLER_FULL = HANDLER_DEFICIENT + (
    "\n"
    "    def do_PUT(self):\n"
    "        pass\n"
    "\n"
    "    def do_DELETE(self):\n"
    "        pass\n"
    "\n"
    "    def do_PATCH(self):\n"
    "        pass\n"
    "\n"
    "    def do_HEAD(self):\n"
    "        pass\n"
    "\n"
    "    def do_OPTIONS(self):\n"
    "        pass\n"
)

HANDLER_DISPATCH = HANDLER_DEFICIENT + (
    "\n"
    "    def handle_one_request(self):\n"
    "        pass\n"
)


class TestHandlerSurfaceGaps(unittest.TestCase):
    def test_deficient_handler_names_all_missing(self) -> None:
        gaps = handler_surface_gaps(HANDLER_DEFICIENT)
        self.assertEqual(len(gaps), 1)
        name, missing = gaps[0]
        self.assertEqual(name, "QueueHandler")
        self.assertEqual(
            missing,
            ["do_PUT", "do_DELETE", "do_PATCH", "do_HEAD", "do_OPTIONS"],
        )

    def test_full_surface_has_no_gaps(self) -> None:
        self.assertEqual(handler_surface_gaps(HANDLER_FULL), [])

    def test_partial_surface_is_deliberate_not_a_gap(self) -> None:
        # The contract probe's own semantics: ANY handler beyond
        # do_GET/do_POST makes the surface deliberate; the gate
        # mirrors the probe, it does not out-strict it. Finer
        # per-method behavior is the live matrix's domain.
        content = HANDLER_DEFICIENT + (
            "\n"
            "    def do_DELETE(self):\n"
            "        pass\n"
        )
        self.assertEqual(handler_surface_gaps(content), [])

    def test_dispatch_override_covers(self) -> None:
        self.assertEqual(handler_surface_gaps(HANDLER_DISPATCH), [])

    def test_getattr_covers(self) -> None:
        content = HANDLER_DEFICIENT + (
            "\n"
            "    def __getattr__(self, name):\n"
            "        raise AttributeError(name)\n"
        )
        self.assertEqual(handler_surface_gaps(content), [])

    def test_in_file_base_chain_covers(self) -> None:
        content = (
            "from http.server import BaseHTTPRequestHandler\n"
            "\n"
            "\n"
            "class FullHandler(BaseHTTPRequestHandler):\n"
            + "".join(
                f"    def {name}(self):\n        pass\n\n"
                for name in (
                    "do_GET",
                    "do_POST",
                    "do_PUT",
                    "do_DELETE",
                    "do_PATCH",
                    "do_HEAD",
                    "do_OPTIONS",
                )
            )
            + "\n"
            "class ChildHandler(FullHandler):\n"
            "    def do_GET(self):\n"
            "        pass\n"
        )
        self.assertEqual(handler_surface_gaps(content), [])

    def test_non_handler_and_broken_content_ignored(self) -> None:
        self.assertEqual(
            handler_surface_gaps("class Plain:\n    pass\n"), []
        )
        self.assertEqual(handler_surface_gaps("def broken(:\n"), [])


def _scaffold(node_id: str, path: Path, content: str) -> dict:
    return {
        "id": node_id,
        "objective": f"Write {path.name}",
        "depends_on": [],
        "assigned_claw": "scaffold",
        "inputs": {"file_path": str(path), "content": content},
        "outputs": [str(path)],
        "criteria": [{"type": "file_exists", "path": str(path)}],
    }


def _method_errors(errors: list[str]) -> list[str]:
    return [e for e in errors if "method surface" in e]


class _QueueTreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        package = self.root / "queueapp"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.target = self.root / "queueapp" / "server.py"


class TestMethodSurfaceFillGate(_QueueTreeCase):
    def test_deficient_create_is_declined(self) -> None:
        candidate = _scaffold("step_1", self.target, HANDLER_DEFICIENT)
        errors = _filled_candidate_errors(
            candidate, [], self.root, "Build the queue service",
            clauses=[CLAUSE_405],
        )
        method_errors = _method_errors(errors)
        self.assertTrue(method_errors)
        joined = "\n".join(method_errors)
        for name in ("do_PUT", "do_DELETE", "do_PATCH", "do_HEAD", "do_OPTIONS"):
            self.assertIn(name, joined)

    def test_full_surface_create_passes(self) -> None:
        candidate = _scaffold("step_1", self.target, HANDLER_FULL)
        errors = _filled_candidate_errors(
            candidate, [], self.root, "Build the queue service",
            clauses=[CLAUSE_405],
        )
        self.assertEqual(_method_errors(errors), [])

    def test_no_method_clause_no_gate(self) -> None:
        candidate = _scaffold("step_1", self.target, HANDLER_DEFICIENT)
        errors = _filled_candidate_errors(
            candidate, [], self.root, "Build the queue service",
        )
        self.assertEqual(_method_errors(errors), [])

    def test_standing_gap_is_not_the_fills_sin(self) -> None:
        # The file already stands with the deficient handler; a
        # fill that leaves the handler exactly as it stood (an
        # unrelated addition) is not declined for the standing gap.
        self.target.write_text(HANDLER_DEFICIENT, encoding="utf-8")
        candidate = _scaffold(
            "step_1",
            self.target,
            '"""Queue service."""\n\n' + HANDLER_DEFICIENT,
        )
        errors = _filled_candidate_errors(
            candidate, [], self.root, "Build the queue service",
            clauses=[CLAUSE_405],
        )
        self.assertEqual(_method_errors(errors), [])

    def test_worsening_the_gap_is_declined(self) -> None:
        # The standing file has the full surface; the fill removes
        # handlers — the gap is this fill's own work.
        self.target.write_text(HANDLER_FULL, encoding="utf-8")
        candidate = _scaffold("step_1", self.target, HANDLER_DEFICIENT)
        errors = _filled_candidate_errors(
            candidate, [], self.root, "Build the queue service",
            clauses=[CLAUSE_405],
        )
        self.assertTrue(_method_errors(errors))


class TestMethodSurfaceParseGate(_QueueTreeCase):
    def _declared(self, content: str) -> dict:
        node = _scaffold("step_1", self.target, content)
        node["inputs"]["clauses"] = {
            "implements": ["C405-method"],
            "verifies": ["C405-method"],
        }
        return node

    def test_prefilled_deficient_plan_is_rejected(self) -> None:
        node = self._declared(HANDLER_DEFICIENT)
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                _payload(node),
                repo_root=self.root,
                clauses=[CLAUSE_405],
            )
        codes = [
            record.code
            for record in getattr(caught.exception, "plan_errors", [])
        ]
        self.assertIn("method_surface", codes)

    def test_prefilled_full_plan_parses(self) -> None:
        node = self._declared(HANDLER_FULL)
        nodes, _metadata = parse_planner_response(
            _payload(node),
            repo_root=self.root,
            clauses=[CLAUSE_405],
        )
        self.assertEqual(len(nodes), 1)


if __name__ == "__main__":
    unittest.main()
