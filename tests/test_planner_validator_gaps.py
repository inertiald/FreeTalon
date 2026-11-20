"""Tests for the planner validator gap closures (2026-11-20).

Four known gaps from the planner assessment backlog, one section each:

1. The schema-error path in _validate_raw_nodes skipped the full
   _collect_plan_errors run, so a schema-valid node carrying a real
   defect was invisible whenever a sibling node failed schema
   validation. The path now runs the full collector on the nodes that
   DID validate and merges their per-node records.
2. Parse failures masked stand-in detection: scaffold content that
   failed to parse never had its hardcoded measured bindings named,
   and normalize's structural raises (duplicate ids, self-dependency,
   dependency cycle) named only the structural defect. Both seams now
   run the detection independently.
3. The bash echo-write detectors (_LITERAL_WRITE_RE / _ECHO_WRITE_RE)
   stopped the body at the first ">", so a ">" inside a quoted echo
   body truncated the write out of detection.
4. A literal write binding measured fields to constants is a
   fabricated measurement in any file: the stand-in check now names
   it for non-code targets too (the general literal-JSON-to-data-file
   shape stays allowed by the recorded data-file exemption — see
   TestMeasuredBindingDataWrite for the boundary).
"""

from __future__ import annotations

import json
import unittest

from freetalon.orchestrator.intake import LLMResponseError
from freetalon.orchestrator.planner import (
    CheapRejectionError,
    _bash_code_writes,
    _hardcoded_measured_bindings,
    _literal_write,
    parse_planner_response,
)


def _dag(nodes: list[dict]) -> str:
    return json.dumps({"nodes": nodes, "metadata": {}})


class TestEchoWriteQuotedRedirect(unittest.TestCase):
    """Gap 3: a ">" inside a quoted echo body is content, not the redirect."""

    def test_literal_write_detects_gt_inside_single_quotes(self) -> None:
        self.assertEqual(
            _literal_write("echo '{\"status\": \"ok > fine\"}' > out.json"),
            ("out.json", '{"status": "ok > fine"}'),
        )

    def test_literal_write_detects_gt_inside_double_quotes(self) -> None:
        # The shell command as the model writes it: a double-quoted
        # body whose JSON quotes are backslash-escaped.
        inner = json.dumps({"status": "ok > fine"}).replace('"', '\\"')
        written = _literal_write(f'echo "{inner}" > out.json')
        self.assertIsNotNone(written)
        target, literal = written
        self.assertEqual(target, "out.json")
        # The segment keeps its \" escapes; json.loads reads them as
        # the JSON literal the model wrote.
        self.assertEqual(json.loads(literal), {"status": "ok > fine"})

    def test_literal_write_detects_assert_sentence_with_gt(self) -> None:
        self.assertEqual(
            _literal_write("echo 'assert health is ok > really' > test.sh"),
            ("test.sh", "assert health is ok > really"),
        )

    def test_literal_write_printf_format_is_not_content(self) -> None:
        # printf's first argument is the format; the written literal is
        # the argument segment.
        self.assertEqual(
            _literal_write("printf '%s' '{\"a\": \">\"}' > out.json"),
            ("out.json", '{"a": ">"}'),
        )

    def test_literal_write_printf_format_only_is_content(self) -> None:
        self.assertEqual(
            _literal_write("printf '{\"a\": 1}' > out.json"),
            ("out.json", '{"a": 1}'),
        )

    def test_literal_write_plain_echo_still_none(self) -> None:
        self.assertIsNone(_literal_write("echo hello > out.txt"))

    def test_literal_write_append_redirect(self) -> None:
        self.assertEqual(
            _literal_write("echo '{\"a\": \">\"}' >> out.json"),
            ("out.json", '{"a": ">"}'),
        )

    def test_bash_code_writes_extracts_quoted_gt_body(self) -> None:
        writes = _bash_code_writes("echo '{\"x\": \"a > b\"}' > probe.py")
        self.assertEqual(writes, [("probe.py", '{"x": "a > b"}', False)])

    def test_standin_fires_for_quoted_gt_fake(self) -> None:
        # End to end: the fake the regex gap used to let through — an
        # echo-written JSON answer whose text contains ">", claimed by
        # a file_exists criterion on the same path.
        nodes = [
            {
                "id": "step-1",
                "objective": "Produce the health report",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": "echo '{\"status\": \"ok > fine\"}' > report.json"
                },
                "outputs": ["report.json"],
                "acceptance": ["report exists"],
                "criteria": [{"type": "file_exists", "path": "report.json"}],
            }
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag(nodes))
        self.assertIn("hardcoded answer", str(caught.exception))
        self.assertIn("report.json", str(caught.exception))


class TestMeasuredBindingDataWrite(unittest.TestCase):
    """Gap 4: a literal binding measured fields to constants is a
    fabricated measurement in any file, under any criterion.

    The boundary is deliberate: the general literal-JSON-to-data-file
    shape stays allowed (the recorded data-file exemption — writing a
    JSON config is legitimate file production). Only the measured
    bindings fire, extending shape 3's principle to literal writes.
    """

    BINDINGS_JSON = (
        '{"status": "ok", "ollama_reachable": true, "gpu_temperature": 42}'
    )

    def _bash_node(self, criteria: list[dict]) -> list[dict]:
        return [
            {
                "id": "step-1",
                "objective": "Produce the status report file",
                "depends_on": [],
                "assigned_claw": "bash",
                "inputs": {
                    "command": f"echo '{self.BINDINGS_JSON}' > report.json"
                },
                "outputs": ["report.json"],
                "acceptance": ["report written"],
                "criteria": criteria,
            }
        ]

    def test_detector_names_constants_and_ignores_computed(self) -> None:
        self.assertEqual(
            _hardcoded_measured_bindings('{"gpu_temperature": 42}'),
            ['"gpu_temperature": 42'],
        )
        self.assertEqual(
            _hardcoded_measured_bindings(
                '{"ollama_reachable": check_reachable()}'
            ),
            [],
        )

    def test_bash_bindings_to_data_file_rejected_under_exit_code(self) -> None:
        with self.assertRaises(CheapRejectionError) as caught:
            parse_planner_response(
                _dag(self._bash_node([{"type": "exit_code", "equals": 0}]))
            )
        message = str(caught.exception)
        self.assertIn("fabricated measurement", message)
        self.assertIn('"ollama_reachable": true', message)
        self.assertIn('"gpu_temperature": 42', message)
        self.assertIn("report.json", message)

    def test_scaffold_bindings_to_data_file_rejected(self) -> None:
        nodes = [
            {
                "id": "step-1",
                "objective": "Produce the status report file",
                "depends_on": [],
                "assigned_claw": "scaffold",
                "inputs": {
                    "file_path": "report.json",
                    "content": self.BINDINGS_JSON,
                },
                "outputs": ["report.json"],
                "acceptance": ["report written"],
                "criteria": [{"type": "exit_code", "equals": 0}],
            }
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag(nodes))
        self.assertIn("fabricated measurement", str(caught.exception))

    def test_file_exists_claim_keeps_the_shape1_error(self) -> None:
        # A claimed write is shape 1's, exactly as before: the
        # bindings error must not displace or duplicate it.
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                _dag(
                    self._bash_node(
                        [{"type": "file_exists", "path": "report.json"}]
                    )
                )
            )
        message = str(caught.exception)
        self.assertIn("writes a static file", message)
        self.assertNotIn("fabricated measurement", message)

    def test_literal_data_without_measured_bindings_still_accepted(self) -> None:
        # The exemption boundary: static metadata and config literals
        # are legitimate file production, even under exit_code only.
        for content in (
            '{"status": "ok", "service": "freetalon"}',
            '{"port": 8765, "host": "localhost"}',
        ):
            nodes = [
                {
                    "id": "step-1",
                    "objective": "Write the data file",
                    "depends_on": [],
                    "assigned_claw": "bash",
                    "inputs": {"command": f"echo '{content}' > report.json"},
                    "outputs": ["report.json"],
                    "acceptance": ["report written"],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                }
            ]
            parsed, _ = parse_planner_response(_dag(nodes))
            self.assertEqual(len(parsed), 1)


class TestParseErrorDoesNotMaskStandin(unittest.TestCase):
    """Gap 2: a parse failure must not mask stand-in detection.

    Two seams: scaffold content that fails to parse still has its
    hardcoded measured bindings named (retry #24's recorded
    weakness), and normalize's structural raises (duplicate ids,
    self-dependency, dependency cycle) carry the plan's collected
    content errors instead of naming the structure alone.
    """

    BINDING_CONTENT = (
        "import json\n\n"
        "def health():\n"
        "    status = {\"ollama_reachable\": True, \"gpu_temperature\": 25}\n"
        "    return json.dumps(status)\n"
        "def broken(:\n"
    )

    def _scaffold_plan(self, content: str) -> str:
        return _dag(
            [
                {
                    "id": "step-1",
                    "objective": "Add the health probe module",
                    "depends_on": [],
                    "assigned_claw": "scaffold",
                    "inputs": {
                        "file_path": "freetalon/health_probe.py",
                        "content": content,
                    },
                    "outputs": ["freetalon/health_probe.py"],
                    "acceptance": ["module written"],
                    "criteria": [{"type": "exit_code", "equals": 0}],
                }
            ]
        )

    def test_unparseable_scaffold_names_measured_bindings(self) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(self._scaffold_plan(self.BINDING_CONTENT))
        message = str(caught.exception)
        self.assertIn("does not parse as Python", message)
        self.assertIn("binds measured fields to constants", message)
        self.assertIn('"ollama_reachable": True', message)
        self.assertIn('"gpu_temperature": 25', message)

    def test_unparseable_scaffold_without_bindings_names_only_parse(
        self,
    ) -> None:
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(
                self._scaffold_plan("def broken(:\n    pass\n")
            )
        message = str(caught.exception)
        self.assertIn("does not parse as Python", message)
        self.assertNotIn("binds measured fields", message)

    def test_parseable_scaffold_bindings_are_not_the_parse_check(
        self,
    ) -> None:
        # Scoped deliberately: a constant inside WORKING code is the
        # execution layer's judgment, not this check's. The plan is
        # accepted at plan time.
        content = (
            "import json\n\n"
            "def health():\n"
            "    status = {\"ollama_reachable\": True}\n"
            "    return json.dumps(status)\n"
        )
        parsed, _ = parse_planner_response(self._scaffold_plan(content))
        self.assertEqual(len(parsed), 1)

    @staticmethod
    def _fake_node(node_id: str, depends_on: list[str]) -> dict:
        return {
            "id": node_id,
            "objective": "Produce the health report",
            "depends_on": depends_on,
            "assigned_claw": "bash",
            "inputs": {
                "command": "echo '{\"status\": \"ok\"}' > report.json"
            },
            "outputs": ["report.json"],
            "acceptance": ["report exists"],
            "criteria": [{"type": "file_exists", "path": "report.json"}],
        }

    @staticmethod
    def _clean_node(node_id: str, depends_on: list[str]) -> dict:
        return {
            "id": node_id,
            "objective": "Show the report",
            "depends_on": depends_on,
            "assigned_claw": "bash",
            "inputs": {"command": "cat report.json"},
            "outputs": [],
            "acceptance": ["report shown"],
            "criteria": [{"type": "exit_code", "equals": 0}],
        }

    def test_cycle_rejection_also_names_the_fake(self) -> None:
        nodes = [
            self._fake_node("step-1", ["step-2"]),
            self._clean_node("step-2", ["step-1"]),
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag(nodes))
        message = str(caught.exception)
        self.assertIn("dependency cycle", message)
        self.assertIn("hardcoded answer", message)

    def test_cycle_without_a_fake_keeps_the_bare_rejection(self) -> None:
        nodes = [
            self._clean_node("step-1", ["step-2"]),
            self._clean_node("step-2", ["step-1"]),
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag(nodes))
        self.assertEqual(
            str(caught.exception),
            "Planner response contained a dependency cycle.",
        )
        records = caught.exception.plan_errors
        self.assertEqual([r.code for r in records], ["dependency_cycle"])

    def test_duplicate_ids_rejection_also_names_the_fake(self) -> None:
        nodes = [
            self._fake_node("step-1", []),
            self._clean_node("step-1", []),
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag(nodes))
        message = str(caught.exception)
        self.assertIn("duplicate node ids", message)
        self.assertIn("hardcoded answer", message)

    def test_self_dependency_rejection_also_names_the_fake(self) -> None:
        nodes = [
            self._clean_node("step-1", ["step-1"]),
            self._fake_node("step-2", []),
        ]
        with self.assertRaises(LLMResponseError) as caught:
            parse_planner_response(_dag(nodes))
        message = str(caught.exception)
        self.assertIn("cannot depend on itself", message)
        self.assertIn("hardcoded answer", message)


if __name__ == "__main__":
    unittest.main()
