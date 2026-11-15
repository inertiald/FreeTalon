"""Tests for the M5 ECU vertical (simulated capture, no live ECU)."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.ecu import (
    analyse_tune_surface,
    make_can_capture_handler,
    make_flash_handler,
    make_verify_backup_handler,
    simulate_can_frames,
    verify_backup_file,
)


class TestSimulatedCan(unittest.TestCase):
    def test_deterministic_frames(self):
        a = simulate_can_frames(seed=11, count=50)
        b = simulate_can_frames(seed=11, count=50)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 50)
        self.assertEqual(a[0]["dlc"], 8)
        self.assertTrue(all(f["id"].startswith("0x") for f in a))

    def test_can_capture_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_can_capture_handler()
            result = asyncio.run(
                handler({"source": "simulated", "count": 25, "run_dir": tmp})
            )
            self.assertEqual(result["metrics"]["frames"], 25)
            lines = Path(result["frames_path"]).read_text().strip().split("\n")
            self.assertEqual(len(lines), 25)
            self.assertEqual(json.loads(lines[0])["dlc"], 8)

    def test_live_capture_is_refused(self):
        handler = make_can_capture_handler()
        with self.assertRaises(ValueError):
            asyncio.run(handler({"source": "live", "run_dir": "/tmp"}))


class TestVerifyBackup(unittest.TestCase):
    def test_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "stock.bin"
            backup.write_bytes(b"STOCK-TUNE")
            digest = hashlib.sha256(b"STOCK-TUNE").hexdigest()
            ok = verify_backup_file(backup, expected_sha256=digest)
            self.assertTrue(ok["match"])
            bad = verify_backup_file(backup, expected_sha256="0" * 64)
            self.assertFalse(bad["match"])

    def test_missing_backup_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                verify_backup_file(Path(tmp) / "nope.bin")

    def test_handler_refuses_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "stock.bin"
            backup.write_bytes(b"STOCK-TUNE")
            handler = make_verify_backup_handler()
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    handler(
                        {
                            "backup_path": str(backup),
                            "expected_sha256": "0" * 64,
                            "run_dir": tmp,
                        }
                    )
                )


class TestTuneSurface(unittest.TestCase):
    def test_surface_from_report(self):
        report = {
            "file_type": "binary (unknown)",
            "size_bytes": 128,
            "sha256": "a" * 64,
            "strings": ["FUEL-MAP", "ignition timing table", "write checksum", "hello"],
            "structure": {},
        }
        surface = analyse_tune_surface(report)
        self.assertIn("map", surface["maps_found"])
        self.assertIn("timing", surface["maps_found"])
        self.assertIn("write", surface["risk_terms"])
        self.assertIn("checksum", surface["risk_terms"])
        self.assertIn("128 bytes", surface["summary"])


class TestFlashStub(unittest.TestCase):
    def test_flash_always_refuses(self):
        handler = make_flash_handler()
        with self.assertRaises(RuntimeError):
            asyncio.run(handler({"file": "tune.bin"}))


if __name__ == "__main__":
    unittest.main()
