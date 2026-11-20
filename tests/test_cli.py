"""Tests for freetalon.cli argument parsing.

The connection flags (--host/--port/--workspace/...) are global, but the
natural place to type them is after the subcommand (`freetalon serve
--port 8765`). Both positions must parse to the same namespace; the
global position was the only one accepted before the install test on a
user system surfaced the cryptic argparse error.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from freetalon.cli import build_parser


class CliParserTests(unittest.TestCase):
    def test_global_flags_before_subcommand(self) -> None:
        args = build_parser().parse_args(["--port", "9001", "serve"])
        self.assertEqual(args.cmd, "serve")
        self.assertEqual(args.port, 9001)

    def test_global_flags_after_subcommand(self) -> None:
        args = build_parser().parse_args(["serve", "--port", "9001"])
        self.assertEqual(args.cmd, "serve")
        self.assertEqual(args.port, 9001)

    def test_subcommand_position_overrides_global(self) -> None:
        args = build_parser().parse_args(["--port", "9001", "serve", "--port", "9002"])
        self.assertEqual(args.port, 9002)

    def test_defaults_unchanged(self) -> None:
        args = build_parser().parse_args(["status"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.workspace, str(Path.home() / "freetalon-workspace"))
        self.assertEqual(
            args.state_path, str(Path.home() / "freetalon-workspace" / "hive-state.json")
        )

    def test_all_connection_flags_after_subcommand(self) -> None:
        args = build_parser().parse_args(
            [
                "status",
                "--host",
                "10.0.0.9",
                "--port",
                "9999",
                "--workspace",
                "/tmp/ws",
                "--state-path",
                "/tmp/ws/state.json",
                "--audit-log-path",
                "/tmp/ws/audit.log",
                "--token-file",
                "/tmp/ws/token",
                "--token",
                "abc",
            ]
        )
        self.assertEqual(args.host, "10.0.0.9")
        self.assertEqual(args.port, 9999)
        self.assertEqual(args.workspace, "/tmp/ws")
        self.assertEqual(args.state_path, "/tmp/ws/state.json")
        self.assertEqual(args.audit_log_path, "/tmp/ws/audit.log")
        self.assertEqual(args.token_file, "/tmp/ws/token")
        self.assertEqual(args.token, "abc")

    def test_submit_keeps_its_own_flags(self) -> None:
        args = build_parser().parse_args(
            ["submit", "--action", "echo", "--text", "hi", "--port", "9010"]
        )
        self.assertEqual(args.action, "echo")
        self.assertEqual(args.text, "hi")
        self.assertEqual(args.port, 9010)


if __name__ == "__main__":
    unittest.main()
