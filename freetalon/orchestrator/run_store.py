"""Per-run directory store for the autonomous loop (design doc Stage 3).

Every run gets a directory Pat can inspect directly::

    freetalon-workspace/runs/<run-id>/
        contract.json        # the goal contract for this run
        plan-v001.json       # every plan version, numbered
        plan-v002.json
        nodes/<node-id>/     # stdout, stderr, artifacts per node
        measurements.jsonl   # one verdict record per node
        memory.jsonl         # experiment records (M2)
        gates/               # approval requests and decisions (M4)
        summary.md           # human-readable run report
        logs/                # service-level logs, as today

A run that cannot be reconstructed from its directory afterwards did not
happen, as far as the loop is concerned: re-planning and memory only ever
read these records, never the model's recollection of them.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freetalon.jsonutil import json_default

# The run store lives next to the other loop state; resolve lazily so tests
# can pass an explicit base directory.
_DEFAULT_BASE = Path.home() / "freetalon-workspace" / "runs"


def new_run_id() -> str:
    """Timestamped, unique run id, e.g. run-20261114T203000Z-a1b2c3."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


class RunStore:
    """Manages run directories under ``base_dir``."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else _DEFAULT_BASE
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ── Paths ────────────────────────────────────────────────────────────

    def run_dir(self, run_id: str) -> Path:
        return self.base_dir / run_id

    def node_dir(self, run_id: str, node_id: str, version: int = 1) -> Path:
        """Node working directory. Version 1 keeps the M0 layout
        (nodes/<node-id>/); later plan versions nest under
        nodes/v002/<node-id>/ so attempts never overwrite each other."""
        if version <= 1:
            return self.run_dir(run_id) / "nodes" / node_id
        return self.run_dir(run_id) / "nodes" / f"v{version:03d}" / node_id

    # ── Run lifecycle ────────────────────────────────────────────────────

    def create_run(self, run_id: str, contract: dict[str, Any]) -> Path:
        """Create the run directory and write contract.json. Returns the dir."""
        run_dir = self.run_dir(run_id)
        (run_dir / "nodes").mkdir(parents=True, exist_ok=True)
        (run_dir / "gates").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        (run_dir / "contract.json").write_text(
            json.dumps(contract, indent=2, sort_keys=True, default=json_default),
            encoding="utf-8",
        )
        return run_dir

    def write_plan_version(self, run_id: str, version: int, plan: Any) -> Path:
        """Write a numbered plan version. ``plan`` is a pydantic model or dict."""
        if hasattr(plan, "model_dump_json"):
            text = plan.model_dump_json(indent=2)
        else:
            text = json.dumps(plan, indent=2, sort_keys=True, default=json_default)
        path = self.run_dir(run_id) / f"plan-v{version:03d}.json"
        path.write_text(text, encoding="utf-8")
        return path

    # ── Node outputs ─────────────────────────────────────────────────────

    def write_node_output(
        self,
        run_id: str,
        node_id: str,
        *,
        stdout: str = "",
        stderr: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        version: int = 1,
    ) -> dict[str, str]:
        """Write a node's stdout/stderr to its directory. Returns paths."""
        node_dir = self.node_dir(run_id, node_id, version)
        node_dir.mkdir(parents=True, exist_ok=True)
        out_path = node_dir / "stdout.txt"
        err_path = node_dir / "stderr.txt"
        out_path.write_text(stdout or "", encoding="utf-8")
        err_path.write_text(stderr or "", encoding="utf-8")
        written = {"stdout_path": str(out_path), "stderr_path": str(err_path)}
        for artifact in artifacts or []:
            src = Path(str(artifact.get("path", "")))
            if src.is_file():
                # Copy declared artifacts into the node dir for the record.
                dest = node_dir / src.name
                dest.write_bytes(src.read_bytes())
                written.setdefault("artifacts", str(dest))
        return written

    # ── JSONL records ────────────────────────────────────────────────────

    def _append_jsonl(self, run_id: str, filename: str, record: dict[str, Any]) -> None:
        path = self.run_dir(run_id) / filename
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=json_default) + "\n")

    def append_measurement(self, run_id: str, measurement: dict[str, Any]) -> None:
        """Append one verdict record to measurements.jsonl."""
        self._append_jsonl(run_id, "measurements.jsonl", measurement)

    def append_memory(self, run_id: str, record: dict[str, Any]) -> None:
        """Append one experiment record to memory.jsonl."""
        self._append_jsonl(run_id, "memory.jsonl", record)

    def read_measurements(self, run_id: str) -> list[dict[str, Any]]:
        """Read back all measurement records for a run (empty list if none)."""
        return self._read_jsonl(run_id, "measurements.jsonl")

    def read_memory(self, run_id: str) -> list[dict[str, Any]]:
        """Read back all experiment records for a run (empty list if none)."""
        return self._read_jsonl(run_id, "memory.jsonl")

    def _read_jsonl(self, run_id: str, filename: str) -> list[dict[str, Any]]:
        path = self.run_dir(run_id) / filename
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    # ── Gates (M4) ───────────────────────────────────────────────────────

    def write_gate(self, run_id: str, packet: dict[str, Any]) -> Path:
        """Store a gate packet under gates/<packet_id>.json. Returns the path."""
        packet_id = str(packet.get("packet_id", "gate-unknown"))
        path = self.run_dir(run_id) / "gates" / f"{packet_id}.json"
        path.write_text(
            json.dumps(packet, indent=2, sort_keys=True, default=json_default),
            encoding="utf-8",
        )
        return path

    def read_gate(self, run_id: str, packet_id: str) -> dict[str, Any] | None:
        path = self.run_dir(run_id) / "gates" / f"{packet_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Summary ──────────────────────────────────────────────────────────

    def write_summary(self, run_id: str, summary_md: str) -> Path:
        """Write summary.md for a run. Returns the path."""
        path = self.run_dir(run_id) / "summary.md"
        path.write_text(summary_md, encoding="utf-8")
        return path

    @staticmethod
    def artifact_sha256(path: Path) -> str:
        """SHA-256 of a file, for artifact records."""
        return hashlib.sha256(path.read_bytes()).hexdigest()
