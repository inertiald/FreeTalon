"""Tests for the M3 research and data claws (fake HTTP — no network)."""

import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.research import (
    fetch_dataset,
    fetch_page,
    research_topic,
    search_duckduckgo,
    search_stackexchange,
    search_web,
    search_wikipedia,
    trust_note_for,
)

_DDG_HTML = """<html><body>
<div class="result">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fforum.example.com%2Fecu-brick&amp;rut=x">ECU bricked after flash</a>
<a class="result__snippet" href="#">Users report bricked ECUs when flashing with a low battery.</a>
</div>
<div class="result">
<a rel="nofollow" class="result__a" href="https://docs.example.com/ecu/flashing">ECU flashing guide</a>
<a class="result__snippet" href="#">Always back up the stock tune before flashing.</a>
</div>
</body></html>"""

_WIKI_JSON = json.dumps({
    "query": {"search": [
        {"title": "Engine control unit",
         "snippet": "An <span class=\"searchmatch\">engine control unit</span> is an embedded system."},
    ]},
})

_FORUM_HTML = """<html><head><style>.x{}</style></head><body>
<script>var x = 1;</script>
<h1>ECU bricked after flash</h1>
<p>Three users bricked their ECU flashing with a low battery.</p>
</body></html>"""


def _fake_http(url: str, timeout: float = 20.0):
    if "duckduckgo" in url:
        return 200, {"content-type": "text/html"}, _DDG_HTML.encode()
    if "wikipedia.org" in url:
        return 200, {"content-type": "application/json"}, _WIKI_JSON.encode()
    if "forum.example.com" in url:
        return 200, {"content-type": "text/html"}, _FORUM_HTML.encode()
    if "docs.example.com" in url:
        return 200, {"content-type": "text/html"}, b"<html><body><p>Always back up.</p></body></html>"
    if "tune.example.com" in url:
        return 200, {"content-type": "application/octet-stream"}, b"\x00\x01TUNE-DATA\x00\xff"
    if "api.stackexchange.com" in url:
        payload = json.dumps({"items": [
            {"title": "Can reading from the ODB port interfere with a custom STO map?",
             "link": "https://mechanics.stackexchange.com/questions/11532/x"},
        ]})
        return 200, {"content-type": "application/json"}, payload.encode()
    return 404, {}, b"not found"


class TestSearch(unittest.TestCase):
    def test_duckduckgo_parses_results_and_unwraps_redirect(self):
        results = search_duckduckgo("ECU brick", http_get=_fake_http)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://forum.example.com/ecu-brick")
        self.assertIn("bricked", results[0]["title"].lower())
        self.assertIn("low battery", results[0]["snippet"])
        self.assertEqual(results[0]["engine"], "duckduckgo")
        self.assertIn("retrieved_at", results[0])

    def test_wikipedia_opensearch(self):
        results = search_wikipedia("engine control unit", http_get=_fake_http)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://en.wikipedia.org/wiki/Engine_control_unit")
        self.assertEqual(results[0]["engine"], "wikipedia")
        self.assertNotIn("<span", results[0]["snippet"])

    def test_search_web_falls_back_to_next_engine(self):
        def ddg_down(url, timeout=20.0):
            if "duckduckgo" in url:
                return 500, {}, b""
            return _fake_http(url, timeout)

        results, failures = search_web("engine control unit", http_get=ddg_down)
        self.assertEqual(len(results), 4)  # 3 stackexchange + 1 wikipedia
        self.assertEqual(results[0]["engine"], "stackexchange")
        self.assertEqual(results[-1]["engine"], "wikipedia")
        self.assertEqual(failures[0]["engine"], "duckduckgo")

    def test_stackexchange_aggregates_sites(self):
        results = search_stackexchange("ECU flash brick", http_get=_fake_http)
        self.assertEqual(len(results), 3)  # one per site in the fake
        self.assertEqual(results[0]["engine"], "stackexchange")
        self.assertIn("mechanics.stackexchange.com", results[0]["url"])

    def test_search_web_records_unknown_engine(self):
        _, failures = search_web("x", http_get=_fake_http, engines=("nope",))
        self.assertEqual(failures[0]["engine"], "nope")


class TestFetchPage(unittest.TestCase):
    def test_extracts_text_skipping_scripts_and_styles(self):
        page = fetch_page("https://forum.example.com/ecu-brick", http_get=_fake_http)
        self.assertIn("ECU bricked after flash", page["text"])
        self.assertIn("low battery", page["text"])
        self.assertNotIn("var x = 1", page["text"])
        self.assertNotIn(".x{}", page["text"])

    def test_non_200_raises(self):
        with self.assertRaises(RuntimeError):
            fetch_page("https://example.com/missing", http_get=_fake_http)


class TestTrustNotes(unittest.TestCase):
    def test_reference_community_unverified(self):
        self.assertEqual(trust_note_for("https://en.wikipedia.org/wiki/X"), "reference")
        self.assertIn("community", trust_note_for("https://www.reddit.com/r/x"))
        self.assertEqual(trust_note_for("https://random.example.com/x"), "unverified")


class TestResearchTopic(unittest.TestCase):
    def test_artifact_has_sources_fetched_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = research_topic(
                "ECU brick risk",
                ["ECU brick risk flashing", "engine control unit"],
                Path(tmp),
                http_get=_fake_http,
            )
            self.assertGreaterEqual(len(artifact["sources"]), 2)
            urls = {s["url"] for s in artifact["sources"]}
            self.assertIn("https://forum.example.com/ecu-brick", urls)
            for source in artifact["sources"]:
                self.assertIn("trust_note", source)
                self.assertIn("retrieved_at", source)
                self.assertIn("engine", source)
            self.assertGreaterEqual(len(artifact["fetched"]), 1)
            self.assertIn("generated_at", artifact)
            path = Path(artifact["artifact_path"])
            self.assertTrue(path.is_file())
            stored = json.loads(path.read_text())
            self.assertEqual(stored["topic"], "ECU brick risk")


class TestFetchDataset(unittest.TestCase):
    def test_file_and_provenance_written(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "tune.bin"
            result = fetch_dataset("https://tune.example.com/tune.bin", dest, http_get=_fake_http)
            self.assertTrue(dest.is_file())
            self.assertEqual(result["sha256"], hashlib.sha256(b"\x00\x01TUNE-DATA\x00\xff").hexdigest())
            sidecar = Path(result["provenance_path"])
            self.assertTrue(sidecar.is_file())
            prov = json.loads(sidecar.read_text())
            self.assertEqual(prov["url"], "https://tune.example.com/tune.bin")
            self.assertEqual(prov["bytes"], 13)
            self.assertTrue(prov["sha256_match"])

    def test_hash_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                fetch_dataset(
                    "https://tune.example.com/tune.bin",
                    Path(tmp) / "tune.bin",
                    http_get=_fake_http,
                    expected_sha256="0" * 64,
                )


if __name__ == "__main__":
    unittest.main()
