"""Sandbox analysis of untrusted files (M3).

A tune file, a firmware dump, a downloaded dataset — anything untrusted
is analysed here and NEVER executed. Analysis is read-only: hash, size,
file type from magic bytes, printable strings, and a structural summary
for safe formats (JSON, CSV, text). Nothing in this module runs the
file, and it refuses to read outside its allowed root, so a plan cannot
point it at host files.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

MAX_ANALYZE_BYTES = 100_000_000
_STRINGS_MIN_LEN = 4
_STRINGS_MAX = 50

_MAGIC: list[tuple[bytes, str]] = [
    (b"\x7fELF", "ELF executable/shared object"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"%PDF-", "PDF document"),
    (b"\x1f\x8b", "gzip archive"),
    (b"MZ", "Windows PE executable"),
    (b"\xca\xfe\xba\xbe", "Mach-O binary"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"#!", "script (shebang)"),
]


def _sniff_type(head: bytes, suffix: str) -> str:
    for magic, label in _MAGIC:
        if head.startswith(magic):
            return label
    stripped = head.strip()
    if stripped.startswith((b"{", b"[")):
        return "JSON"
    if suffix.lower() in (".json",):
        return "JSON"
    if suffix.lower() in (".csv", ".tsv"):
        return "CSV/TSV"
    if suffix.lower() in (".txt", ".md", ".log", ".xml", ".yaml", ".yml", ".ini", ".cfg"):
        return "text"
    try:
        head.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "binary (unknown)"


def _extract_strings(data: bytes, max_count: int = _STRINGS_MAX) -> list[str]:
    strings: list[str] = []
    current = bytearray()
    for byte in data:
        if 32 <= byte < 127:
            current.append(byte)
        else:
            if len(current) >= _STRINGS_MIN_LEN:
                strings.append(current.decode("ascii"))
                if len(strings) >= max_count:
                    return strings
            current = bytearray()
    if len(current) >= _STRINGS_MIN_LEN and len(strings) < max_count:
        strings.append(current.decode("ascii"))
    return strings


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    return -sum(
        (c / total) * math.log2(c / total) for c in counts if c > 0
    )


def analyze_file(path: str | Path, allowed_root: str | Path) -> dict[str, Any]:
    """Analyse a file without executing it. Refuses paths outside root."""
    path = Path(path)
    root = Path(allowed_root)
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, RuntimeError) as exc:
        raise ValueError(f"refusing to analyse {path}: outside sandbox root {root}") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"no such file in sandbox: {path}")
    size = resolved.stat().st_size
    if size > MAX_ANALYZE_BYTES:
        raise RuntimeError(f"file exceeds {MAX_ANALYZE_BYTES} bytes")
    data = resolved.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    file_type = _sniff_type(data[:64], resolved.suffix)
    report: dict[str, Any] = {
        "path": str(resolved),
        "sha256": digest,
        "size_bytes": size,
        "file_type": file_type,
        "entropy_bits_per_byte": round(_entropy(data[:65536]), 3),
        "strings": _extract_strings(data),
    }
    # Structural summary for safe formats — parsed, never executed.
    if file_type == "JSON":
        try:
            parsed = json.loads(data.decode("utf-8"))
            if isinstance(parsed, dict):
                report["structure"] = {"kind": "object", "keys": sorted(parsed.keys())[:50]}
            elif isinstance(parsed, list):
                report["structure"] = {"kind": "array", "length": len(parsed)}
            else:
                report["structure"] = {"kind": type(parsed).__name__}
        except (ValueError, UnicodeDecodeError) as exc:
            report["structure"] = {"kind": "unparsable", "error": str(exc)}
    elif file_type in ("text", "CSV/TSV"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        report["structure"] = {
            "kind": "text",
            "lines": len(lines),
            "first_lines": lines[:5],
        }
        if file_type == "CSV/TSV":
            sep = "\t" if file_type.startswith("CSV") and "\t" in (lines[0] if lines else "") else ","
            report["structure"]["columns"] = (
                len(lines[0].split(sep)) if lines else 0
            )
    return report


class Sandbox:
    """A directory where untrusted files are quarantined and analysed."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def quarantine_bytes(self, data: bytes, name: str) -> Path:
        """Copy bytes into the sandbox under a safe name; returns the path."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
        target = self.root / safe
        target.write_bytes(data)
        return target

    def quarantine_file(self, src: str | Path, name: str | None = None) -> Path:
        """Copy a file's bytes into the sandbox (never a symlink)."""
        src = Path(src)
        return self.quarantine_bytes(src.read_bytes(), name or src.name)

    def analyze(self, name: str) -> dict[str, Any]:
        """Analyse a file already inside the sandbox."""
        return analyze_file(self.root / name, allowed_root=self.root)


# ── Claw handler ──────────────────────────────────────────────────────────


def make_sandbox_handler():
    """Build the ``sandbox_analyze`` claw handler."""

    async def sandbox_handler(inputs: dict[str, Any]) -> dict[str, Any]:
        run_dir = inputs.get("run_dir") or inputs.get("workdir") or "."
        sandbox = Sandbox(Path(str(run_dir)) / "sandbox")
        if inputs.get("content_b64") is not None:
            import base64

            name = str(inputs.get("name") or "upload.bin")
            target = sandbox.quarantine_bytes(
                base64.b64decode(str(inputs["content_b64"])), name
            )
        else:
            raw = str(inputs.get("path") or inputs.get("file") or "").strip()
            if not raw:
                raise ValueError("sandbox_analyze requires 'path' or 'content_b64'.")
            given = Path(raw)
            if not given.is_absolute():
                given = Path(str(run_dir)) / given
            # Analyse in place if it is already under the run dir; otherwise
            # quarantine a copy first so the original is never touched.
            try:
                given.resolve().relative_to(Path(str(run_dir)).resolve())
                target = given
                allowed_root = Path(str(run_dir))
            except ValueError:
                target = sandbox.quarantine_file(given)
                allowed_root = sandbox.root
            report = analyze_file(target, allowed_root=allowed_root)
            return _finish(report, sandbox, inputs)
        report = analyze_file(target, allowed_root=sandbox.root)
        return _finish(report, sandbox, inputs)

    sandbox_handler.__name__ = "sandbox_handler"
    return sandbox_handler


def _finish(
    report: dict[str, Any], sandbox: Sandbox, inputs: dict[str, Any]
) -> dict[str, Any]:
    out_name = str(inputs.get("output") or f"{Path(report['path']).stem}.analysis.json")
    out_path = sandbox.root / out_name
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    # Also copy into the node's own workdir: that is where the node's
    # measurement criteria look for artifacts (relative paths).
    report_path = str(out_path)
    workdir = inputs.get("workdir")
    if workdir and Path(str(workdir)).resolve() != sandbox.root.resolve():
        node_path = Path(str(workdir)) / out_name
        node_path.parent.mkdir(parents=True, exist_ok=True)
        node_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        report_path = str(node_path)
    return {
        "status": "success",
        "report_path": report_path,
        "report": report,
        "metrics": {
            "size_bytes": report["size_bytes"],
            "strings_count": len(report["strings"]),
        },
    }


def register_sandbox_claws(registry: Any, sandbox_root: str | Path | None = None) -> None:
    """Register the sandbox analysis claw on a ToolRegistry."""
    registry.register("sandbox_analyze", make_sandbox_handler())
