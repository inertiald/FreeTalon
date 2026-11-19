"""Content draft generator: queue in, drafts out, review stays Pat's.

One pass (run_once) lists the entries waiting at 'received', claims
each in turn, drafts every output the entry selected, and submits.
Any failure on an entry returns it to 'received' with the error
noted and moves on - a bad entry never wedges the queue, and no
entry is ever left parked at 'processing' by a crash in this code.

Slice 1c: before drafting, each still image also goes through a
vision pass (freetalon.content.vision) whose description joins the
draft prompts as grounded observation. The vision callable is
optional and its failures are never fatal: an image the vision
model cannot describe is drafted from its metadata alone, with the
fallback noted in the log.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..orchestrator.intake import LLMSettings, _normalize_base_url, _post_json
from .media_info import image_dimensions
from .prompts import PROMPT_BUILDERS, VOICE_SYSTEM_PROMPT
from .queue_client import ContentQueueClient, ContentQueueError
from .vision import VisionFn, make_ollama_vision

log = logging.getLogger("freetalon.content")

ChatFn = Callable[[str, str], str]

_DEFAULT_SITE_BASE_URL = "http://100.87.140.12:3000"
_DEFAULT_MODEL = "qwen3.8:27b"
_DEFAULT_VISION_MODEL = "gemma3:27b"


def clean_draft(text: str) -> str:
    """Deterministic tidy-up of one model draft: drop a wrapping code
    fence and outer blank lines; the words are the model's own."""
    cleaned = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def parse_youtube_draft(text: str) -> dict[str, str]:
    """Split a YouTube draft into {title, description}: the first line
    is the title (a leading 'TITLE:' label is stripped), the rest is
    the description."""
    cleaned = clean_draft(text)
    lines = cleaned.split("\n")
    title = lines[0].strip()
    if title.upper().startswith("TITLE:"):
        title = title[len("TITLE:") :].strip()
    description = "\n".join(lines[1:]).strip()
    if not title or not description:
        raise ValueError("youtube draft did not yield a title and a description")
    return {"title": title, "description": description}


def make_ollama_chat(settings: LLMSettings) -> ChatFn:
    """A chat callable over Ollama's /api/chat, shaped like the
    orchestrator's backend (intake._call_ollama) but WITHOUT its
    format="json" pin: intake needs machine-readable JSON for its
    contracts, copy drafting needs free text."""

    def chat(system_prompt: str, user_prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": settings.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": settings.temperature,
                "num_ctx": settings.num_ctx if settings.num_ctx else 4096,
            },
        }
        if settings.think is not None:
            payload["think"] = settings.think
        response = _post_json(
            f"{_normalize_base_url(settings.base_url)}/api/chat",
            payload,
            timeout_seconds=settings.timeout_seconds,
        )
        content = response.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ContentQueueError("Ollama response did not include message.content text.")
        return content

    return chat


@dataclass
class EntryResult:
    entry_id: str
    outcome: str  # 'drafts-ready' | 'failed' | 'skipped'
    detail: str = ""


class ContentGenerator:
    def __init__(
        self,
        client: ContentQueueClient,
        chat: ChatFn,
        vision: VisionFn | None = None,
    ) -> None:
        self._client = client
        self._chat = chat
        self._vision = vision

    def _enrich_media(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Attach image dimensions (parsed from the stored bytes)
        and, when a vision callable is configured, a vision
        description to the entry's media dicts - best effort
        throughout: a fetch failure leaves the item dimensionless,
        and a vision failure leaves it metadata-only. Neither ever
        fails the entry; both are noted in the log."""
        enriched = dict(entry)
        media = []
        for item in entry.get("media") or []:
            item = dict(item)
            if item.get("kind") == "image" and item.get("filename"):
                raw: bytes | None = None
                try:
                    raw = self._client.fetch_media(entry["id"], item["filename"])
                    item["dimensions"] = image_dimensions(raw)
                except ContentQueueError as exc:
                    log.warning("media fetch failed for %s: %s", entry["id"], exc)
                    item["dimensions"] = None
                if raw is not None and self._vision is not None:
                    item["vision"] = self._describe(
                        self._vision, entry["id"], item["filename"], raw
                    )
            media.append(item)
        enriched["media"] = media
        return enriched

    @staticmethod
    def _describe(
        vision: VisionFn, entry_id: str, filename: str, raw: bytes
    ) -> str | None:
        """One vision call, failure-isolated: any error or an empty
        description degrades this image to metadata-only grounding."""
        try:
            description = vision(raw)
        except Exception as exc:  # vision is advisory, never fatal
            log.warning(
                "vision failed for %s/%s (%s: %s); drafting from metadata only",
                entry_id,
                filename,
                type(exc).__name__,
                exc,
            )
            return None
        if not description or not description.strip():
            log.warning(
                "vision returned no description for %s/%s; drafting from metadata only",
                entry_id,
                filename,
            )
            return None
        return description.strip()

    def generate_drafts(self, entry: dict[str, Any]) -> dict[str, Any]:
        """One chat call per selected output; raises on any failure so
        the caller can fail the whole entry back cleanly."""
        enriched = self._enrich_media(entry)
        drafts: dict[str, Any] = {}
        for output in entry.get("outputs") or []:
            builder = PROMPT_BUILDERS.get(output)
            if builder is None:
                raise ContentQueueError(f"unknown output requested: {output}")
            raw = self._chat(VOICE_SYSTEM_PROMPT, builder(enriched))
            if output == "youtube":
                drafts["youtube"] = parse_youtube_draft(raw)
            else:
                draft = clean_draft(raw)
                if not draft:
                    raise ContentQueueError(f"model returned an empty {output} draft")
                drafts[output] = draft
        return drafts

    def process_entry(self, entry: dict[str, Any]) -> EntryResult:
        entry_id = str(entry.get("id", ""))
        try:
            self._client.claim(entry_id)
        except ContentQueueError as exc:
            # Claimed by someone else, or already moved on: not ours.
            log.info("skip %s: %s", entry_id, exc)
            return EntryResult(entry_id, "skipped", str(exc))
        try:
            drafts = self.generate_drafts(entry)
            self._client.submit_drafts(entry_id, drafts)
        except Exception as exc:  # one bad entry never wedges the queue
            note = f"{type(exc).__name__}: {exc}"[:900]
            log.warning("entry %s failed: %s", entry_id, note)
            try:
                self._client.fail(entry_id, note)
            except ContentQueueError as fail_exc:
                log.error("entry %s could not be failed back: %s", entry_id, fail_exc)
            return EntryResult(entry_id, "failed", note)
        log.info("entry %s -> drafts-ready", entry_id)
        return EntryResult(entry_id, "drafts-ready")

    def run_once(self) -> list[EntryResult]:
        entries = self._client.list_received()
        log.info("%d entr%s waiting", len(entries), "y" if len(entries) == 1 else "ies")
        return [self.process_entry(entry) for entry in entries]


def settings_from_env() -> LLMSettings:
    """Generation settings: qwen3.8 with thinking off (the /health
    proof's configuration) at a copy-friendly temperature."""
    return LLMSettings(
        model=os.environ.get("CONTENT_OLLAMA_MODEL", _DEFAULT_MODEL),
        timeout_seconds=600.0,
        temperature=0.7,
        num_ctx=24576,
        think=False,
    )


def vision_settings_from_env() -> LLMSettings:
    """Vision settings: gemma3:27b (multimodal, already on the box)
    at a factual-description temperature. think stays unset (None):
    gemma3 has no thinking mode to switch off, so the payload omits
    the field entirely."""
    return LLMSettings(
        model=os.environ.get("CONTENT_VISION_MODEL", _DEFAULT_VISION_MODEL),
        timeout_seconds=600.0,
        temperature=0.2,
        num_ctx=8192,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freetalon-content",
        description="Draft content for queued inertiald.com submissions (one pass).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CONTENT_SITE_BASE_URL", _DEFAULT_SITE_BASE_URL),
        help="inertiald.com app base URL (default: seeker88 over the tailnet)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("CONTENT_PIPELINE_TOKEN", "")
    if not token:
        print("CONTENT_PIPELINE_TOKEN is not set; refusing to run unauthenticated.")
        return 2

    client = ContentQueueClient(args.base_url, token)
    generator = ContentGenerator(
        client,
        make_ollama_chat(settings_from_env()),
        vision=make_ollama_vision(vision_settings_from_env()),
    )
    try:
        results = generator.run_once()
    except ContentQueueError as exc:
        print(f"content queue error: {exc}")
        return 1
    for result in results:
        suffix = f" ({result.detail})" if result.detail else ""
        print(f"{result.entry_id}: {result.outcome}{suffix}")
    print(f"done: {len(results)} processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
