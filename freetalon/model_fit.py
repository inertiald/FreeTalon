"""Planner-model VRAM fit, surfaced as an additive field on GET /health.

/health used to report "ready" from reachability alone, even when the
configured planner model could not fit in VRAM — measured on an 8GB
card with a 17.7GB model: 0.49GB resident, the rest spilled to CPU,
health still green, and the failure only surfaced later as LLM
timeouts. This module asks Ollama what is actually true:

- model loaded: compare its total footprint against the VRAM-resident
  share Ollama reports (``/api/ps``) — a shortfall is a partial CPU
  offload, which is legitimate but slow;
- model not loaded: compare its on-disk size against total VRAM
  (``/api/tags`` + nvidia-smi).

Every failure mode degrades to ``status: "unknown"``: this probe must
never break /health, so it swallows its own errors by design.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from typing import Any, Callable

_TIMEOUT_SECONDS = 1.5


def _fetch_json(url: str, timeout: float = _TIMEOUT_SECONDS) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _vram_total_bytes() -> int | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip().split("\n")[0]) * 1024 * 1024
    except ValueError:
        return None


def _names_match(reported: str, model: str) -> bool:
    # Ollama always reports a tag; a bare configured name means :latest.
    return reported == model or reported == f"{model}:latest"


def model_fit_report(
    *,
    base_url: str | None = None,
    model: str | None = None,
    fetch_json: Callable[[str], Any] | None = None,
    vram_total_bytes: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    """Fit report for the configured planner model.

    Keys are always present; byte counts are None when not applicable
    or not measurable. ``status`` is one of ``fits``,
    ``partial_offload``, ``exceeds_vram``, ``unknown``.
    """
    report: dict[str, Any] = {
        "model": model or "",
        "status": "unknown",
        "model_bytes": None,
        "vram_bytes": None,
        "resident_bytes": None,
        "detail": "",
    }
    if base_url is None or model is None:
        try:
            from .orchestrator.intake import LLMSettings

            settings = LLMSettings.from_env()
        except Exception:
            report["detail"] = "planner settings unavailable"
            return report
        if settings.backend != "ollama":
            report["model"] = settings.model
            report["detail"] = "planner backend is not Ollama"
            return report
        base_url = base_url or settings.base_url
        model = model or settings.model
        report["model"] = model

    fetch = fetch_json or _fetch_json
    base = base_url.rstrip("/")

    try:
        ps = fetch(f"{base}/api/ps")
    except Exception:
        report["detail"] = "Ollama not reachable"
        return report
    for entry in ps.get("models", []) if isinstance(ps, dict) else []:
        name = str(entry.get("name") or entry.get("model") or "")
        if not _names_match(name, model):
            continue
        size = entry.get("size")
        resident = entry.get("size_vram")
        report["model_bytes"] = size
        report["resident_bytes"] = resident
        if isinstance(size, int) and isinstance(resident, int) and size > 0:
            if resident >= size:
                report["status"] = "fits"
                report["detail"] = "model fully resident in VRAM"
            else:
                report["status"] = "partial_offload"
                report["detail"] = (
                    "model partially offloaded to CPU; inference will be slow"
                )
        else:
            report["detail"] = "model loaded; Ollama reported no sizes"
        return report

    try:
        tags = fetch(f"{base}/api/tags")
    except Exception:
        report["detail"] = "model not loaded; Ollama tags unavailable"
        return report
    for entry in tags.get("models", []) if isinstance(tags, dict) else []:
        if not _names_match(str(entry.get("name") or ""), model):
            continue
        size = entry.get("size")
        report["model_bytes"] = size
        vram = (vram_total_bytes or _vram_total_bytes)()
        report["vram_bytes"] = vram
        if isinstance(size, int) and vram:
            if size <= vram:
                report["status"] = "fits"
                report["detail"] = "model fits in VRAM (not currently loaded)"
            else:
                report["status"] = "exceeds_vram"
                report["detail"] = (
                    "model is larger than total VRAM; it will run "
                    "partially on CPU and be slow"
                )
        else:
            report["detail"] = "model not loaded; VRAM size unavailable"
        return report

    report["detail"] = "model not present in Ollama"
    return report
