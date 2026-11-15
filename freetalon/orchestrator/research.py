"""Research and data tools (M3).

Research tools are first-class capabilities with their own measurement
criteria: a research node succeeds only if it returns sourced, dated,
usable material — not merely text. Every source is stored with its URL,
retrieval date, the engine that found it, and a trust note.

Engines (tried in order; each result records which engine produced it):
- ``duckduckgo`` — the public HTML endpoint, no key required.
- ``stackexchange`` — the Stack Exchange API (electronics, mechanics,
  stackoverflow), no key required at low volume.
- ``wikipedia`` — the full-text search API, no key required.

No engine is a secret: a failed engine is recorded in the artifact,
never hidden. Tests inject a fake ``http_get``; production uses urllib.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) FreeTalon-research/0.1"
DEFAULT_TIMEOUT_S = 20.0
MAX_PAGE_BYTES = 1_000_000
MAX_DATASET_BYTES = 50_000_000
MAX_SEARCH_RESULTS = 10

# Domains treated as reference material; forums are community sources
# (useful, but verify); everything else is unverified.
_REFERENCE_DOMAINS = ("wikipedia.org", "docs.", "github.com", "readthedocs.io")
_COMMUNITY_DOMAINS = ("reddit.com", "forum", "stackexchange.com", "stackoverflow.com")

HttpGet = Callable[[str, float], tuple[int, dict[str, str], bytes]]


def default_http_get(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> tuple[int, dict[str, str], bytes]:
    """GET a URL with a UA header and a size cap. Returns (status, headers, body)."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "text/html,application/json;q=0.9,*/*;q=0.8")
    request.add_header("Accept-Language", "en-US,en;q=0.9")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_PAGE_BYTES + 1)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), headers, body
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}, exc.read(MAX_PAGE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def trust_note_for(url: str) -> str:
    """Heuristic trust note for a source URL."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(d in host for d in _REFERENCE_DOMAINS):
        return "reference"
    if any(d in host for d in _COMMUNITY_DOMAINS):
        return "community — verify against a second source"
    return "unverified"


def _clean_url(raw: str) -> str | None:
    if not raw:
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.netloc == "duckduckgo.com" and parsed.path.startswith("/l/"):
        # DDG redirect link: the real URL is in the uddg parameter.
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return target
        return None
    if parsed.scheme in ("http", "https"):
        return raw
    if raw.startswith("//"):
        return "https:" + raw
    return None


class _DDGParser(HTMLParser):
    """Extract DuckDuckGo HTML results: title links + snippets."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "") or ""
        if "result__a" in cls:
            url = _clean_url(attrs_d.get("href", "") or "")
            if url:
                self._current = {"url": url, "title": ""}
                self._capture = "title"
        elif "result__snippet" in cls and self._current is not None:
            self._capture = "snippet"

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._capture in ("title", "snippet"):
            self._current[self._capture] = self._current.get(self._capture, "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None and self._capture == "snippet":
            title = re.sub(r"\s+", " ", self._current.get("title", "")).strip()
            snippet = re.sub(r"\s+", " ", self._current.get("snippet", "")).strip()
            if title:
                self.results.append(
                    {"title": title, "url": self._current["url"], "snippet": snippet}
                )
            self._current = None
            self._capture = None
        elif tag == "a" and self._capture == "title":
            self._capture = None


def search_duckduckgo(
    query: str, max_results: int = 5, http_get: HttpGet | None = None
) -> list[dict[str, Any]]:
    """Search DuckDuckGo's HTML endpoint. Returns sourced results."""
    get = http_get or default_http_get
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    status, _headers, body = get(url, DEFAULT_TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"DuckDuckGo search failed with HTTP {status}")
    parser = _DDGParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    results = []
    for item in parser.results[:max_results]:
        results.append({**item, "engine": "duckduckgo", "retrieved_at": _now_iso()})
    return results


def search_wikipedia(
    query: str, max_results: int = 5, http_get: HttpGet | None = None
) -> list[dict[str, Any]]:
    """Search Wikipedia's full-text search API. Returns sourced results."""
    get = http_get or default_http_get
    url = (
        "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
        + urllib.parse.quote(query)
        + f"&srlimit={max_results}&format=json"
    )
    status, _headers, body = get(url, DEFAULT_TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"Wikipedia search failed with HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
        hits = payload["query"]["search"]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Wikipedia returned an unexpected payload: {exc}") from exc
    results = []
    for hit in hits:
        title = str(hit.get("title", ""))
        snippet = re.sub(r"<[^>]+>", "", str(hit.get("snippet", "")))
        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
            title.replace(" ", "_")
        )
        results.append(
            {
                "title": title,
                "url": page_url,
                "snippet": re.sub(r"\s+", " ", snippet).strip(),
                "engine": "wikipedia",
                "retrieved_at": _now_iso(),
            }
        )
    return results


def search_stackexchange(
    query: str,
    max_results: int = 5,
    http_get: HttpGet | None = None,
    sites: tuple[str, ...] = ("electronics", "mechanics", "stackoverflow"),
) -> list[dict[str, Any]]:
    """Search Stack Exchange forums (no key needed at low volume).

    Queries the most relevant sites in order and aggregates the results.
    """
    get = http_get or default_http_get
    results: list[dict[str, Any]] = []
    for site in sites:
        if len(results) >= max_results:
            break
        url = (
            "https://api.stackexchange.com/2.3/search/advanced"
            "?order=desc&sort=relevance&q="
            + urllib.parse.quote(query)
            + f"&site={site}&pagesize={max_results - len(results)}"
        )
        status, _headers, body = get(url, DEFAULT_TIMEOUT_S)
        if status != 200:
            raise RuntimeError(f"Stack Exchange ({site}) failed with HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
            items = payload.get("items", [])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Stack Exchange ({site}) returned an unexpected payload: {exc}"
            ) from exc
        for item in items:
            results.append(
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("link", "")),
                    "snippet": "",
                    "engine": "stackexchange",
                    "retrieved_at": _now_iso(),
                }
            )
    return results


_ENGINES: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "duckduckgo": search_duckduckgo,
    "stackexchange": search_stackexchange,
    "wikipedia": search_wikipedia,
}


def search_web(
    query: str,
    max_results: int = 5,
    http_get: HttpGet | None = None,
    engines: tuple[str, ...] = ("duckduckgo", "stackexchange", "wikipedia"),
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Search the web across engines, in order, until enough results.

    Returns (results, failures): failures name engines that errored, so a
    dead engine is visible in the artifact instead of silently skipped.
    """
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for engine in engines:
        if len(results) >= max_results:
            break
        fn = _ENGINES.get(engine)
        if fn is None:
            failures.append({"engine": engine, "error": "unknown engine"})
            continue
        try:
            found = fn(query, max_results - len(results), http_get)
        except Exception as exc:  # noqa: BLE001 - engine failure is data
            failures.append({"engine": engine, "error": str(exc)})
            continue
        results.extend(found)
    return results[:max_results], failures


class _TextExtractor(HTMLParser):
    """Extract readable text from HTML, skipping scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        joined = " ".join(self._chunks)
        return re.sub(r"\s+", " ", html_lib.unescape(joined)).strip()


def fetch_page(
    url: str, http_get: HttpGet | None = None, max_chars: int = 20000
) -> dict[str, Any]:
    """Fetch a page and extract readable text (size-capped)."""
    get = http_get or default_http_get
    status, headers, body = get(url, DEFAULT_TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"GET {url} failed with HTTP {status}")
    content_type = headers.get("content-type", "")
    if "html" in content_type or not content_type:
        extractor = _TextExtractor()
        extractor.feed(body.decode("utf-8", errors="replace"))
        text = extractor.text()
    else:
        text = body.decode("utf-8", errors="replace")
    return {
        "url": url,
        "status": status,
        "content_type": content_type,
        "text": text[:max_chars],
        "retrieved_at": _now_iso(),
    }


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:60] or "research"


def research_topic(
    topic: str,
    questions: list[str],
    workdir: Path,
    http_get: HttpGet | None = None,
    max_results: int = 5,
    fetch_top: int = 2,
) -> dict[str, Any]:
    """Research a topic: search per question, fetch top pages, store it all.

    Writes ``research/<slug>.json`` under ``workdir`` and returns the
    artifact dict (also returned to the caller for measurement).
    """
    questions = questions or [topic]
    sources: list[dict[str, Any]] = []
    fetched: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for question in questions:
        found, engine_failures = search_web(
            question, max_results=max_results, http_get=http_get
        )
        failures.extend(engine_failures)
        for item in found:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            sources.append({**item, "trust_note": trust_note_for(item["url"])})
        for item in found[:fetch_top]:
            try:
                page = fetch_page(item["url"], http_get=http_get)
            except Exception as exc:  # noqa: BLE001 - fetch failure is data
                failures.append({"engine": "fetch", "error": f"{item['url']}: {exc}"})
                continue
            fetched.append(
                {
                    "url": page["url"],
                    "title": item.get("title", ""),
                    "excerpt": page["text"][:2000],
                    "retrieved_at": page["retrieved_at"],
                    "trust_note": trust_note_for(page["url"]),
                }
            )
    artifact = {
        "topic": topic,
        "questions": questions,
        "sources": sources,
        "fetched": fetched,
        "failures": failures,
        "generated_at": _now_iso(),
    }
    out_dir = Path(workdir) / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slug(topic)}.json"
    out_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    artifact["artifact_path"] = str(out_path)
    return artifact


def fetch_dataset(
    url: str,
    dest: Path,
    http_get: HttpGet | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Fetch a dataset with provenance. Writes the file + a sidecar JSON.

    The sidecar records the source URL, hash, byte count, and retrieval
    time, so a later criterion (file_matches) can verify the artifact.
    """
    import hashlib

    get = http_get or default_http_get
    status, _headers, body = get(url, 60.0)
    if status != 200:
        raise RuntimeError(f"GET {url} failed with HTTP {status}")
    if len(body) > MAX_DATASET_BYTES:
        raise RuntimeError(f"dataset exceeds {MAX_DATASET_BYTES} bytes")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    provenance = {
        "url": url,
        "sha256": digest,
        "bytes": len(body),
        "retrieved_at": _now_iso(),
        "expected_sha256": expected_sha256,
        "sha256_match": (expected_sha256 is None or expected_sha256.lower() == digest),
    }
    sidecar = dest.with_suffix(dest.suffix + ".provenance.json")
    sidecar.write_text(json.dumps(provenance, indent=2, sort_keys=True), encoding="utf-8")
    if expected_sha256 is not None and not provenance["sha256_match"]:
        raise RuntimeError(
            f"dataset hash mismatch: got {digest}, expected {expected_sha256}"
        )
    return {
        "path": str(dest),
        "sha256": digest,
        "bytes": len(body),
        "provenance_path": str(sidecar),
        "provenance": provenance,
    }


# ── Claw handlers (registered on a ToolRegistry) ──────────────────────────


def _resolve_base(inputs: dict[str, Any], key: str, default: str) -> Path:
    base = inputs.get(key) or inputs.get("workdir") or default
    return Path(str(base))


def make_research_handler(http_get: HttpGet | None = None):
    """Build the ``research`` claw handler (injectable HTTP for tests)."""

    async def research_handler(inputs: dict[str, Any]) -> dict[str, Any]:
        topic = str(inputs.get("topic") or inputs.get("query") or "").strip()
        if not topic:
            raise ValueError("research claw requires 'topic' or 'query'.")
        questions = inputs.get("questions") or [topic]
        if isinstance(questions, str):
            questions = [questions]
        max_results = int(inputs.get("max_results", 5))
        base = _resolve_base(inputs, "run_dir", ".")
        artifact = research_topic(
            topic,
            [str(q) for q in questions],
            base,
            http_get=http_get,
            max_results=max_results,
        )
        return {
            "status": "success",
            "artifact_path": artifact["artifact_path"],
            "sources": artifact["sources"],
            "metrics": {
                "source_count": len(artifact["sources"]),
                "fetched_count": len(artifact["fetched"]),
            },
        }

    research_handler.__name__ = "research_handler"
    return research_handler


def make_dataset_handler(http_get: HttpGet | None = None):
    """Build the ``fetch_dataset`` claw handler (injectable HTTP for tests)."""

    async def dataset_handler(inputs: dict[str, Any]) -> dict[str, Any]:
        url = str(inputs.get("url") or "").strip()
        if not url:
            raise ValueError("fetch_dataset claw requires 'url'.")
        dest_raw = str(inputs.get("dest") or inputs.get("path") or "").strip()
        if not dest_raw:
            raise ValueError("fetch_dataset claw requires 'dest'.")
        base = _resolve_base(inputs, "run_dir", ".")
        dest = Path(dest_raw)
        if not dest.is_absolute():
            dest = base / dest
        result = fetch_dataset(
            url, dest, http_get=http_get,
            expected_sha256=inputs.get("expected_sha256"),
        )
        return {
            "status": "success",
            "path": result["path"],
            "sha256": result["sha256"],
            "provenance_path": result["provenance_path"],
            "metrics": {"bytes": result["bytes"]},
        }

    dataset_handler.__name__ = "dataset_handler"
    return dataset_handler


def register_research_claws(registry: Any, http_get: HttpGet | None = None) -> None:
    """Register the research + data claws on a ToolRegistry."""
    registry.register("research", make_research_handler(http_get))
    registry.register("web_search", make_research_handler(http_get))
    registry.register("fetch_dataset", make_dataset_handler(http_get))
    registry.register("data_fetch", make_dataset_handler(http_get))
