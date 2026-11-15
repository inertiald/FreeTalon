"""Tests for the deterministic measurement module (loop Stage 4, M0)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from freetalon.orchestrator.measure import (
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNMEASURABLE,
    build_envelope,
    evaluate_criterion,
    measure_node,
    measurement_failed_detail,
    register_checker,
)


def _node(node_id="n1", criteria=None):
    return SimpleNamespace(id=node_id, criteria=criteria or [])


def _envelope(**overrides):
    env = {
        "node_id": "n1",
        "status": "completed",
        "exit_code": 0,
        "duration_s": 0.5,
        "stdout": "",
        "stderr": "",
        "artifacts": [],
        "metrics": {},
        "error": None,
    }
    env.update(overrides)
    return env


class TestBuildEnvelope(unittest.TestCase):
    def test_extracts_exit_code_metrics_artifacts(self):
        env = build_envelope(
            "n1",
            {"exit_code": 3, "metrics": {"score": 0.9}, "artifacts": ["a.txt"]},
            duration_s=1.25,
        )
        self.assertEqual(env["exit_code"], 3)
        self.assertEqual(env["metrics"], {"score": 0.9})
        self.assertEqual(env["artifacts"], ["a.txt"])
        self.assertEqual(env["duration_s"], 1.25)

    def test_missing_exit_code_is_none(self):
        env = build_envelope("n1", {"ok": True}, duration_s=0.1)
        self.assertIsNone(env["exit_code"])


class TestExitCodeCriterion(unittest.TestCase):
    def test_passes_on_zero(self):
        r = evaluate_criterion({"type": "exit_code", "equals": 0}, _envelope(), Path("."))
        self.assertTrue(r["passed"])
        self.assertTrue(r["measured"])

    def test_fails_on_nonzero(self):
        r = evaluate_criterion(
            {"type": "exit_code", "equals": 0}, _envelope(exit_code=1), Path(".")
        )
        self.assertFalse(r["passed"])
        self.assertIn("1 != 0", r["detail"])

    def test_unmeasurable_without_exit_code(self):
        r = evaluate_criterion(
            {"type": "exit_code", "equals": 0}, _envelope(exit_code=None), Path(".")
        )
        self.assertFalse(r["passed"])
        self.assertFalse(r["measured"])


class TestFileCriteria(unittest.TestCase):
    def test_file_exists_pass_and_unmeasurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "out.txt").write_text("hi", encoding="utf-8")
            ok = evaluate_criterion({"type": "file_exists", "path": "out.txt"}, _envelope(), workdir)
            self.assertTrue(ok["passed"])
            missing = evaluate_criterion(
                {"type": "file_exists", "path": "nope.txt"}, _envelope(), workdir
            )
            self.assertFalse(missing["passed"])
            self.assertFalse(missing["measured"])  # missing artifact = UNMEASURABLE

    def test_file_matches_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            data = b"abc123"
            (workdir / "f.bin").write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            ok = evaluate_criterion(
                {"type": "file_matches", "path": "f.bin", "sha256": digest},
                _envelope(),
                workdir,
            )
            self.assertTrue(ok["passed"])
            bad = evaluate_criterion(
                {"type": "file_matches", "path": "f.bin", "sha256": "0" * 64},
                _envelope(),
                workdir,
            )
            self.assertFalse(bad["passed"])
            self.assertTrue(bad["measured"])

    def test_file_matches_json_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "r.json").write_text(json.dumps({"a": 1, "b": 2}), encoding="utf-8")
            ok = evaluate_criterion(
                {"type": "file_matches", "path": "r.json", "json_keys": ["a", "b"]},
                _envelope(),
                workdir,
            )
            self.assertTrue(ok["passed"])
            bad = evaluate_criterion(
                {"type": "file_matches", "path": "r.json", "json_keys": ["a", "zz"]},
                _envelope(),
                workdir,
            )
            self.assertFalse(bad["passed"])


class TestMetricCriterion(unittest.TestCase):
    def test_comparisons(self):
        env = _envelope(metrics={"sharpe": 1.2})
        for op, expected, passed in [
            (">=", 0.8, True),
            (">", 1.2, False),
            ("<", 2.0, True),
            ("==", 1.2, True),
            ("<=", 1.2, True),
        ]:
            r = evaluate_criterion(
                {"type": "metric", "name": "sharpe", "op": op, "value": expected},
                env,
                Path("."),
            )
            self.assertEqual(r["passed"], passed, f"{op} {expected}")

    def test_missing_metric_is_unmeasurable(self):
        r = evaluate_criterion(
            {"type": "metric", "name": "sharpe", "op": ">=", "value": 0.8},
            _envelope(),
            Path("."),
        )
        self.assertFalse(r["measured"])

    def test_non_numeric_metric_fails(self):
        r = evaluate_criterion(
            {"type": "metric", "name": "sharpe", "op": ">=", "value": 0.8},
            _envelope(metrics={"sharpe": "high"}),
            Path("."),
        )
        self.assertFalse(r["passed"])
        self.assertTrue(r["measured"])


class TestTestsPassCriterion(unittest.TestCase):
    def test_passing_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "suite"
            suite.mkdir()
            (suite / "test_ok.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_one(self):\n"
                "        self.assertEqual(1, 1)\n",
                encoding="utf-8",
            )
            r = evaluate_criterion(
                {"type": "tests_pass", "suite": str(suite)}, _envelope(), Path(tmp)
            )
            self.assertTrue(r["passed"], r["detail"])

    def test_failing_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "suite"
            suite.mkdir()
            (suite / "test_bad.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_one(self):\n"
                "        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )
            r = evaluate_criterion(
                {"type": "tests_pass", "suite": str(suite)}, _envelope(), Path(tmp)
            )
            self.assertFalse(r["passed"])

    def test_missing_suite_is_unmeasurable(self):
        r = evaluate_criterion(
            {"type": "tests_pass", "suite": "no-such-dir"}, _envelope(), Path(".")
        )
        self.assertFalse(r["measured"])


class TestCustomCheckCriterion(unittest.TestCase):
    def test_registered_checker(self):
        register_checker("always_ok", lambda env, wd: (True, "fine"))
        r = evaluate_criterion(
            {"type": "custom_check", "name": "always_ok"}, _envelope(), Path(".")
        )
        self.assertTrue(r["passed"])

    def test_unknown_checker_is_unmeasurable(self):
        r = evaluate_criterion(
            {"type": "custom_check", "name": "no_such_checker"}, _envelope(), Path(".")
        )
        self.assertFalse(r["measured"])


class TestMeasureNode(unittest.TestCase):
    def test_nonzero_exit_fails_even_without_criteria(self):
        # The structural fix for false success.
        m = measure_node(_node(), _envelope(exit_code=1), Path("."))
        self.assertEqual(m["verdict"], VERDICT_FAIL)
        self.assertIn("exit_code 1 != 0", measurement_failed_detail(m))

    def test_timeout_status_fails(self):
        m = measure_node(_node(), _envelope(status="timeout", exit_code=None), Path("."))
        self.assertEqual(m["verdict"], VERDICT_FAIL)

    def test_no_criteria_and_clean_exit_passes(self):
        m = measure_node(_node(), _envelope(), Path("."))
        self.assertEqual(m["verdict"], VERDICT_PASS)

    def test_one_failed_criterion_fails(self):
        node = _node(criteria=[{"type": "metric", "name": "s", "op": ">=", "value": 1.0}])
        m = measure_node(node, _envelope(metrics={"s": 0.5}), Path("."))
        self.assertEqual(m["verdict"], VERDICT_FAIL)

    def test_missing_artifact_is_unmeasurable(self):
        node = _node(criteria=[{"type": "file_exists", "path": "missing.txt"}])
        m = measure_node(node, _envelope(), Path("."))
        self.assertEqual(m["verdict"], VERDICT_UNMEASURABLE)

    def test_all_criteria_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "out.txt").write_text("x", encoding="utf-8")
            node = _node(criteria=[
                {"type": "exit_code", "equals": 0},
                {"type": "file_exists", "path": "out.txt"},
                {"type": "metric", "name": "s", "op": ">=", "value": 0.5},
            ])
            m = measure_node(node, _envelope(metrics={"s": 0.9}), workdir)
            self.assertEqual(m["verdict"], VERDICT_PASS)

    def test_unknown_criterion_type_is_unmeasurable(self):
        r = evaluate_criterion({"type": "nope"}, _envelope(), Path("."))
        self.assertFalse(r["measured"])


if __name__ == "__main__":
    unittest.main()
