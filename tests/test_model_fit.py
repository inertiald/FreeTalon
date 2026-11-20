"""Tests for freetalon.model_fit — the /health model-fit signal.

All Ollama responses are faked; no live server is needed. The measured
case from the seeker88 install test (17.7GB model, 0.49GB resident on
an 8GB card, /health still green) is pinned by
test_partial_offload_when_resident_smaller_than_model.
"""

from __future__ import annotations

import unittest
from typing import Any

from freetalon.model_fit import model_fit_report

_MODEL = "qwen3.8:27b"
_MODEL_BYTES = 18_267_714_352
_RESIDENT_BYTES = 488_479_129
_VRAM_8GB = 8 * 1024 * 1024 * 1024


def _fetch_from(mapping: dict[str, Any]):
    def fetch(url: str) -> Any:
        for suffix, payload in mapping.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected url: {url}")

    return fetch


class ModelFitReportTests(unittest.TestCase):
    def test_fully_resident_model_fits(self) -> None:
        fetch = _fetch_from(
            {"/api/ps": {"models": [{"name": _MODEL, "size": 1000, "size_vram": 1000}]}}
        )
        report = model_fit_report(base_url="http://x", model=_MODEL, fetch_json=fetch)
        self.assertEqual(report["status"], "fits")
        self.assertEqual(report["model_bytes"], 1000)
        self.assertEqual(report["resident_bytes"], 1000)

    def test_partial_offload_when_resident_smaller_than_model(self) -> None:
        fetch = _fetch_from(
            {
                "/api/ps": {
                    "models": [
                        {
                            "name": _MODEL,
                            "size": _MODEL_BYTES,
                            "size_vram": _RESIDENT_BYTES,
                        }
                    ]
                }
            }
        )
        report = model_fit_report(base_url="http://x", model=_MODEL, fetch_json=fetch)
        self.assertEqual(report["status"], "partial_offload")
        self.assertEqual(report["model_bytes"], _MODEL_BYTES)
        self.assertEqual(report["resident_bytes"], _RESIDENT_BYTES)

    def test_unloaded_model_larger_than_vram_exceeds(self) -> None:
        fetch = _fetch_from(
            {
                "/api/ps": {"models": []},
                "/api/tags": {"models": [{"name": _MODEL, "size": _MODEL_BYTES}]},
            }
        )
        report = model_fit_report(
            base_url="http://x",
            model=_MODEL,
            fetch_json=fetch,
            vram_total_bytes=lambda: _VRAM_8GB,
        )
        self.assertEqual(report["status"], "exceeds_vram")
        self.assertEqual(report["vram_bytes"], _VRAM_8GB)

    def test_unloaded_model_within_vram_fits(self) -> None:
        fetch = _fetch_from(
            {
                "/api/ps": {"models": []},
                "/api/tags": {"models": [{"name": _MODEL, "size": 4_000_000_000}]},
            }
        )
        report = model_fit_report(
            base_url="http://x",
            model=_MODEL,
            fetch_json=fetch,
            vram_total_bytes=lambda: _VRAM_8GB,
        )
        self.assertEqual(report["status"], "fits")

    def test_unreachable_ollama_is_unknown_not_an_error(self) -> None:
        fetch = _fetch_from({"/api/ps": ConnectionError("refused")})
        report = model_fit_report(base_url="http://x", model=_MODEL, fetch_json=fetch)
        self.assertEqual(report["status"], "unknown")

    def test_model_absent_from_tags_is_unknown(self) -> None:
        fetch = _fetch_from(
            {"/api/ps": {"models": []}, "/api/tags": {"models": [{"name": "other:1b", "size": 1}]}}
        )
        report = model_fit_report(
            base_url="http://x",
            model=_MODEL,
            fetch_json=fetch,
            vram_total_bytes=lambda: _VRAM_8GB,
        )
        self.assertEqual(report["status"], "unknown")

    def test_bare_model_name_matches_latest_tag(self) -> None:
        fetch = _fetch_from(
            {
                "/api/ps": {
                    "models": [{"name": "gemma-3-abliterated:latest", "size": 10, "size_vram": 10}]
                }
            }
        )
        report = model_fit_report(
            base_url="http://x", model="gemma-3-abliterated", fetch_json=fetch
        )
        self.assertEqual(report["status"], "fits")


if __name__ == "__main__":
    unittest.main()
