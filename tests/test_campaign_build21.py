"""Campaign build #21: verification-interpreter defect.

The held-out screening set (tools/heldout, preregistration frozen
2026-11-25) measured the loop's own verdict disagreeing with reality
on task B in 4 of 4 runs: the shipped tree passed all 17 hidden
tests, yet the runs closed FAILED/INCONCLUSIVE because verification
nodes died with exit 127 — "/bin/sh: 1: ./.venv/bin/python: not
found". The mechanism, diagnosed from the run artifacts:

- the planner grounds every python invocation to ./.venv/bin/python
  (normalization rewrites even bare `python`/`python3` to it), and
  the validator exempts that spelling from its existence check on
  the reasoning that the venv is a machine fact;
- the executor ran the command verbatim, so in a FRESH checkout
  (the held-out driver clones one per run; the pairing exam
  worktree is long-lived and HAS a .venv — the control case) the
  interpreter does not exist;
- the judge knew one non-informative failure class (timeouts), so
  each 127 was an "informative" code failure and burned re-plan
  budget on a phantom.

The fix is two-layered, both in the loop:

- executor._resolve_grounded_interpreter: at dispatch (and for
  criterion commands at measure time), when the grounded
  interpreter does not exist under the plan's repo root, the token
  is rewritten to sys.executable — the interpreter the loop itself
  runs under. When the venv exists, commands are byte-identical.
- judge._is_environment_failure: a failed node with exit exactly
  127 whose captured stderr carries the shell not-found signature
  for a token of its own command is an ENVIRONMENT failure; a pass
  whose only failures are environmental escalates with
  failure_class="environment" instead of re-planning. The raw
  measurement is never altered.

These tests pin the resolution shapes, the judge signature's
narrowness, the decide() routing, and an end-to-end reproduction
in the task-B shape (venv-less tree, grounded command, executor).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.executor import (
    Executor,
    _resolve_grounded_interpreter,
)
from freetalon.orchestrator.judge import (
    Decision,
    Judge,
    _is_environment_failure,
)
from freetalon.orchestrator.models import (
    ExecutionPlan,
    PlanNode,
    PlanStatus,
    RunBudget,
    TaskIntent,
)
from freetalon.orchestrator.state_store import ExecutionPlanStateStore
from freetalon.orchestrator.tool_registry import (
    ToolRegistry,
    register_default_claws,
)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TestResolveGroundedInterpreter(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_venv(self) -> None:
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")

    def test_noop_when_venv_exists(self) -> None:
        self._make_venv()
        command = "cd /x && ./.venv/bin/python -m unittest tests.foo"
        resolved, substitutions = _resolve_grounded_interpreter(command, self.root)
        self.assertEqual(resolved, command)
        self.assertEqual(substitutions, [])

    def test_noop_without_repo_root(self) -> None:
        command = "./.venv/bin/python -m unittest tests.foo"
        resolved, substitutions = _resolve_grounded_interpreter(command, None)
        self.assertEqual(resolved, command)
        self.assertEqual(substitutions, [])

    def test_grounded_token_resolved_when_venv_missing(self) -> None:
        command = (
            f"cd {self.root} && ./.venv/bin/python "
            "-m unittest tests.test_notes_change_request -v"
        )
        resolved, substitutions = _resolve_grounded_interpreter(command, self.root)
        self.assertEqual(
            resolved,
            f"cd {self.root} && {sys.executable} "
            "-m unittest tests.test_notes_change_request -v",
        )
        self.assertEqual(
            substitutions, [("./.venv/bin/python", sys.executable)]
        )

    def test_dotless_spelling_resolved(self) -> None:
        resolved, substitutions = _resolve_grounded_interpreter(
            ".venv/bin/python -m pytest tests/", self.root
        )
        self.assertEqual(resolved, f"{sys.executable} -m pytest tests/")
        self.assertEqual(len(substitutions), 1)

    def test_every_segment_resolved(self) -> None:
        command = (
            "./.venv/bin/python -m unittest tests.a && "
            "./.venv/bin/python -m unittest tests.b"
        )
        resolved, substitutions = _resolve_grounded_interpreter(command, self.root)
        self.assertEqual(
            resolved,
            f"{sys.executable} -m unittest tests.a && "
            f"{sys.executable} -m unittest tests.b",
        )
        self.assertEqual(len(substitutions), 2)

    def test_env_assignment_prefix_resolved(self) -> None:
        resolved, _ = _resolve_grounded_interpreter(
            "FOO=1 ./.venv/bin/python -m unittest tests.a", self.root
        )
        self.assertEqual(
            resolved, f"FOO=1 {sys.executable} -m unittest tests.a"
        )

    def test_quoted_interpreter_resolved(self) -> None:
        resolved, _ = _resolve_grounded_interpreter(
            "'./.venv/bin/python' -m unittest tests.a", self.root
        )
        self.assertEqual(resolved, f"{sys.executable} -m unittest tests.a")

    def test_argument_position_untouched(self) -> None:
        # The spelling as DATA (an argument, or after a builtin) is
        # not an interpreter token and must not be rewritten.
        for command in (
            "echo ./.venv/bin/python",
            "ls ./.venv/bin/python",
            "cat ./.venv/bin/python",
        ):
            resolved, substitutions = _resolve_grounded_interpreter(
                command, self.root
            )
            self.assertEqual(resolved, command, command)
            self.assertEqual(substitutions, [], command)

    def test_other_binaries_untouched(self) -> None:
        command = "./scripts/run.sh && /usr/bin/make test"
        resolved, substitutions = _resolve_grounded_interpreter(command, self.root)
        self.assertEqual(resolved, command)
        self.assertEqual(substitutions, [])

    def test_heredoc_body_untouched(self) -> None:
        command = (
            "cat > /tmp/x.py <<'EOF'\n"
            "./.venv/bin/python\n"
            "EOF\n"
            "./.venv/bin/python -m unittest tests.a"
        )
        resolved, substitutions = _resolve_grounded_interpreter(command, self.root)
        self.assertEqual(
            resolved,
            "cat > /tmp/x.py <<'EOF'\n"
            "./.venv/bin/python\n"
            "EOF\n"
            f"{sys.executable} -m unittest tests.a",
        )
        self.assertEqual(len(substitutions), 1)


class TestCriterionResolution(unittest.TestCase):
    def test_criterion_command_resolved_on_a_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = Executor(
                ExecutionPlanStateStore(db_path=root / "test.db"),
                ToolRegistry(),
            )
            executor._plan_repo_root = root
            node = PlanNode(
                id="n1",
                objective="probe",
                assigned_claw="bash",
                inputs={"command": "true"},
                criteria=[
                    {
                        "type": "exit_code",
                        "equals": 0,
                        "command": "./.venv/bin/python -c \"pass\"",
                    },
                    {"type": "exit_code", "equals": 0},
                ],
            )
            copied = executor._node_with_resolved_criteria(node)
            self.assertEqual(
                copied.criteria[0]["command"],
                f"{sys.executable} -c \"pass\"",
            )
            # The plan's own node keeps the grounded command.
            self.assertEqual(
                node.criteria[0]["command"],
                "./.venv/bin/python -c \"pass\"",
            )
            self.assertEqual(copied.criteria[1], node.criteria[1])


def _judge_node(**kw) -> PlanNode:
    fields = {
        "id": "node-1",
        "objective": "run the new tests",
        "assigned_claw": "bash",
        "hypothesis": "h1",
    }
    fields.update(kw)
    node = PlanNode(**fields)
    node.status = PlanStatus.FAILED
    return node


def _env_measurement(exit_code=127):
    return {
        "node_id": "node-1",
        "verdict": "FAIL",
        "results": [],
        "envelope_status": "completed",
        "exit_code": exit_code,
    }


_ENV_COMMAND = (
    "cd /tmp/worktree && ./.venv/bin/python "
    "-m unittest tests.test_notes_change_request -v"
)
_ENV_STDERR = "/bin/sh: 1: ./.venv/bin/python: not found"


class TestEnvironmentFailureSignature(unittest.TestCase):
    def test_dash_not_found_signature(self) -> None:
        node = _judge_node(
            inputs={"command": _ENV_COMMAND, "stderr": _ENV_STDERR},
            measurement=_env_measurement(),
        )
        self.assertTrue(_is_environment_failure(node))

    def test_bash_no_such_file_signature(self) -> None:
        node = _judge_node(
            inputs={
                "command": _ENV_COMMAND,
                "stderr": (
                    "/bin/bash: line 1: ./.venv/bin/python: "
                    "No such file or directory"
                ),
            },
            measurement=_env_measurement(),
        )
        self.assertTrue(_is_environment_failure(node))

    def test_exit_1_is_not_environment(self) -> None:
        node = _judge_node(
            inputs={"command": _ENV_COMMAND, "stderr": _ENV_STDERR},
            measurement=_env_measurement(exit_code=1),
        )
        self.assertFalse(_is_environment_failure(node))

    def test_127_without_not_found_is_not_environment(self) -> None:
        node = _judge_node(
            inputs={"command": _ENV_COMMAND, "stderr": "segfault-ish noise"},
            measurement=_env_measurement(),
        )
        self.assertFalse(_is_environment_failure(node))

    def test_not_found_subject_must_be_a_command_token(self) -> None:
        node = _judge_node(
            inputs={
                "command": _ENV_COMMAND,
                "stderr": "/bin/sh: 1: ./other/missing-tool: not found",
            },
            measurement=_env_measurement(),
        )
        self.assertFalse(_is_environment_failure(node))

    def test_no_command_no_classification(self) -> None:
        node = _judge_node(
            inputs={"stderr": _ENV_STDERR},
            measurement=_env_measurement(),
        )
        self.assertFalse(_is_environment_failure(node))


class TestJudgeDecideEnvironment(unittest.TestCase):
    def _plan(self, nodes) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="p1",
            intent=TaskIntent(goal="g"),
            nodes=nodes,
            status=PlanStatus.FAILED,
        )

    def test_pure_environment_failure_escalates_as_environment(self) -> None:
        judge = Judge(RunBudget())
        node = _judge_node(
            inputs={"command": _ENV_COMMAND, "stderr": _ENV_STDERR},
            measurement=_env_measurement(),
            error="exit_code 127 != 0",
        )
        decision = judge.decide(self._plan([node]), [_env_measurement()])
        self.assertEqual(decision.decision, Decision.ESCALATE)
        self.assertEqual(decision.failure_class, "environment")
        self.assertIn("environment failure", decision.reason)

    def test_environment_plus_informative_still_replans(self) -> None:
        judge = Judge(RunBudget())
        env_node = _judge_node(
            inputs={"command": _ENV_COMMAND, "stderr": _ENV_STDERR},
            measurement=_env_measurement(),
            error="exit_code 127 != 0",
        )
        code_node = _judge_node(
            id="node-2",
            inputs={"command": "./.venv/bin/python -m unittest tests.b"},
            measurement=_env_measurement(exit_code=1),
            error="exit_code 1 != 0",
        )
        decision = judge.decide(
            self._plan([env_node, code_node]), [_env_measurement()]
        )
        self.assertEqual(decision.decision, Decision.REPLAN)


class TestExecutorFreshCheckoutReproduction(unittest.TestCase):
    """The task-B shape end to end: a venv-less tree, a grounded
    verification command, and the executor — the node's tests must
    actually RUN (pre-fix: exit 127, node FAILED)."""

    def test_verification_node_runs_in_venv_less_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "clone"
            tests_dir = root / "tests"
            tests_dir.mkdir(parents=True)
            (tests_dir / "__init__.py").write_text("")
            (tests_dir / "test_sample.py").write_text(
                "import unittest\n\n\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertEqual(1 + 1, 2)\n"
            )
            store = ExecutionPlanStateStore(db_path=Path(tmp) / "exec.db")
            registry = ToolRegistry()
            register_default_claws(registry)
            node = PlanNode(
                id="node-5",
                objective="Run the new tests in isolation",
                assigned_claw="bash",
                inputs={
                    "command": (
                        f"cd {root} && ./.venv/bin/python "
                        "-m unittest tests.test_sample"
                    )
                },
                criteria=[{"type": "exit_code", "equals": 0}],
            )
            plan = ExecutionPlan(
                plan_id="p-build21",
                intent=TaskIntent(goal="change the notes service"),
                nodes=[node],
                metadata={"repo_root": str(root)},
            )
            store.save(plan)
            executor = Executor(store, registry)
            finished = _run(executor.run(plan.plan_id))
            self.assertEqual(finished.status, PlanStatus.COMPLETED)
            measured = finished.nodes[0].measurement or {}
            self.assertEqual(measured.get("exit_code"), 0)
            # The plan's own node keeps the grounded command — only
            # the dispatched copy was resolved.
            self.assertIn(
                "./.venv/bin/python", finished.nodes[0].inputs["command"]
            )


if __name__ == "__main__":
    unittest.main()
