"""Tests for the M3 sandbox: analysis never executes the file."""

import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.sandbox import Sandbox, analyze_file


class TestAnalyzeFile(unittest.TestCase):
    def test_json_structure_lists_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tune.json"
            target.write_text(json.dumps({"maps": [1, 2], "rpm_limit": 6500}))
            report = analyze_file(target, allowed_root=root)
            self.assertEqual(report["file_type"], "JSON")
            self.assertEqual(report["structure"]["keys"], ["maps", "rpm_limit"])
            self.assertEqual(len(report["sha256"]), 64)

    def test_text_structure_counts_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("line one\nline two\nline three\n")
            report = analyze_file(target, allowed_root=root)
            self.assertEqual(report["file_type"], "text")
            self.assertEqual(report["structure"]["lines"], 3)

    def test_binary_reports_strings_and_entropy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tune.bin"
            target.write_bytes(b"\x7fELF\x00\x02TUNE-MAP\x00\xff\xfe" + b"A" * 100)
            report = analyze_file(target, allowed_root=root)
            self.assertIn("ELF", report["file_type"])
            self.assertIn("TUNE-MAP", report["strings"])
            self.assertIn("entropy_bits_per_byte", report)

    def test_script_is_analysed_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "pwned.txt"
            script = root / "evil.sh"
            script.write_text(f"#!/bin/sh\ntouch {marker}\n")
            report = analyze_file(script, allowed_root=root)
            self.assertIn("script", report["file_type"])
            self.assertFalse(marker.exists(), "analysis must never execute the file")

    def test_refuses_paths_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sandbox"
            root.mkdir()
            outside = Path(tmp) / "secret.txt"
            outside.write_text("do not read")
            with self.assertRaises(ValueError):
                analyze_file(outside, allowed_root=root)

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                analyze_file(Path(tmp) / "nope.bin", allowed_root=Path(tmp))


class TestSandbox(unittest.TestCase):
    def test_quarantine_and_analyze_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Sandbox(Path(tmp) / "sandbox")
            target = box.quarantine_bytes(b'{"a": 1}', "tune.json")
            self.assertTrue(target.is_file())
            report = box.analyze("tune.json")
            self.assertEqual(report["structure"]["keys"], ["a"])

    def test_quarantine_file_copies_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "orig.bin"
            src.write_bytes(b"hello\x00world")
            box = Sandbox(Path(tmp) / "sandbox")
            target = box.quarantine_file(src)
            self.assertEqual(target.read_bytes(), b"hello\x00world")
            self.assertNotEqual(target.resolve(), src.resolve())


if __name__ == "__main__":
    unittest.main()
