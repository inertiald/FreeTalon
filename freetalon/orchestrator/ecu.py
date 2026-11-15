"""ECU vertical (M5): simulated CAN capture, tune-surface analysis,
verified-backup check — and a flash gate that never flashes.

Rules, from the design doc and the standing safety constraints:
- No live ECU is ever touched. CAN capture is simulated (seeded) or
  replayed from a recorded file. ``source="live"`` is refused.
- The tune file itself is never read outside the sandbox: the surface
  analysis works from the sandbox analysis report.
- Flashing is a gated node whose handler is a stub that refuses: it
  exists so the gate has something to pause on, not so it can run.
  A verified backup is a pre-step: the flash plan requires it.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

_MAP_PATTERN = re.compile(r"\b(map|table|curve|axis|load|boost|timing|fuel|ignition)\b", re.I)
_RISK_PATTERN = re.compile(r"\b(write|flash|erase|unlock|bypass|disable|checksum)\b", re.I)


def _resolve(path_value: str, base: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else base / path


def simulate_can_frames(seed: int = 11, count: int = 200) -> list[dict[str, Any]]:
    """Deterministic synthetic CAN frames (no hardware, no live ECU)."""
    rng = random.Random(seed)
    ids = [0x100, 0x200, 0x2A0, 0x300, 0x420]
    frames: list[dict[str, Any]] = []
    for i in range(count):
        can_id = rng.choice(ids)
        frames.append(
            {
                "timestamp_s": round(i * 0.01, 3),
                "id": hex(can_id),
                "data": [rng.randrange(256) for _ in range(8)],
                "dlc": 8,
            }
        )
    return frames


def verify_backup_file(path: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    """Check a backup exists and (optionally) matches its expected hash."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"backup not found: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    match = expected_sha256 is None or expected_sha256.lower() == digest
    return {
        "path": str(path),
        "exists": True,
        "sha256": digest,
        "expected_sha256": expected_sha256,
        "match": match,
    }


def analyse_tune_surface(report: dict[str, Any]) -> dict[str, Any]:
    """Build a tune surface summary from a sandbox analysis report.

    Works on the report (strings, structure, file type) — never on the
    raw tune file — so the untrusted bytes stay in the sandbox.
    """
    strings = report.get("strings", [])
    text = "\n".join(str(s) for s in strings)
    maps = sorted({m.group(0).lower() for m in _MAP_PATTERN.finditer(text)})
    risks = sorted({m.group(0).lower() for m in _RISK_PATTERN.finditer(text)})
    structure = report.get("structure", {})
    return {
        "file_type": report.get("file_type"),
        "size_bytes": report.get("size_bytes"),
        "sha256": report.get("sha256"),
        "maps_found": maps,
        "risk_terms": risks,
        "structure": structure,
        "summary": (
            f"{report.get('file_type')} tune file, "
            f"{report.get('size_bytes')} bytes; "
            f"{len(maps)} map-related terms, {len(risks)} risk terms in strings."
        ),
    }


# ── Claw handlers ─────────────────────────────────────────────────────────


def make_can_capture_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        source = str(inputs.get("source", "simulated"))
        if source == "live":
            raise ValueError(
                "live CAN capture is not supported — use 'simulated' or a "
                "recorded frames file. No live ECU is ever touched."
            )
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        if source == "simulated":
            frames = simulate_can_frames(
                seed=int(inputs.get("seed", 11)),
                count=int(inputs.get("count", 200)),
            )
        else:
            frames_path = _resolve(source, base)
            if not frames_path.is_file():
                raise ValueError(f"frames file not found: {frames_path}")
            frames = json.loads(frames_path.read_text(encoding="utf-8"))
        out_path = base / str(inputs.get("output") or "can_frames.jsonl")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            for frame in frames:
                handle.write(json.dumps(frame) + "\n")
        unique_ids = sorted({f["id"] for f in frames})
        return {
            "status": "success",
            "frames_path": str(out_path),
            "metrics": {"frames": len(frames), "unique_ids": len(unique_ids)},
        }

    handler.__name__ = "can_capture_handler"
    return handler


def make_verify_backup_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        raw = str(inputs.get("backup_path") or inputs.get("path") or "").strip()
        if not raw:
            raise ValueError("verify_backup requires 'backup_path'.")
        path = _resolve(raw, base)
        result = verify_backup_file(path, inputs.get("expected_sha256"))
        if not result["match"]:
            raise RuntimeError(
                f"backup hash mismatch: got {result['sha256']}, "
                f"expected {inputs.get('expected_sha256')}"
            )
        return {
            "status": "success",
            "backup_path": result["path"],
            "sha256": result["sha256"],
            "metrics": {"backup_verified": 1},
        }

    handler.__name__ = "verify_backup_handler"
    return handler


def make_tune_surface_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        raw = str(inputs.get("report") or inputs.get("analysis_path") or "").strip()
        if not raw:
            raise ValueError("tune_surface requires 'report' (analysis report path).")
        report_path = _resolve(raw, base)
        if not report_path.is_file():
            raise ValueError(f"analysis report not found: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        surface = analyse_tune_surface(report)
        out_path = base / str(inputs.get("output") or "tune_surface.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(surface, indent=2), encoding="utf-8")
        return {
            "status": "success",
            "surface_path": str(out_path),
            "metrics": {
                "maps_found": len(surface["maps_found"]),
                "risk_terms": len(surface["risk_terms"]),
            },
        }

    handler.__name__ = "tune_surface_handler"
    return handler


def make_flash_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "live ECU flashing is not implemented. This node exists only "
            "to be gated: a human must approve it, and even then there is "
            "no flashing code to run."
        )

    handler.__name__ = "flash_handler"
    return handler


def register_ecu_claws(registry: Any) -> None:
    """Register the ECU claws on a ToolRegistry."""
    registry.register("can_capture", make_can_capture_handler())
    registry.register("verify_backup", make_verify_backup_handler())
    registry.register("tune_surface", make_tune_surface_handler())
    registry.register("flash_ecu", make_flash_handler())
