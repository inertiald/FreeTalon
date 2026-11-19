"""Vision pass for content draft generation (slice 1c).

Slice 1b grounded the drafts in Pat's blurb plus media metadata
(filename, size, pixel dimensions). This module adds the missing
sense: for each still image on an entry, a vision model describes
what the photo actually shows, and the description joins the draft
prompts as grounded observation - the same honesty rules as the
blurb, because a vision note is a fact the model was given, never a
license to invent more.

Failure discipline matches the rest of the pipeline: a failed or
unsupported vision call never fails an entry. describe_image()
returns None and the generator drafts from metadata alone.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Callable

from ..orchestrator.intake import LLMSettings, _normalize_base_url, _post_json
from .queue_client import ContentQueueError

# describe(image_bytes) -> description text
VisionFn = Callable[[bytes], str]

_DEFAULT_VISION_MODEL = "gemma3:27b"

# Descriptions are grounding notes, not essays: cap them so a
# rambling describer cannot crowd the blurb out of the draft prompt.
MAX_DESCRIPTION_WORDS = 120

VISION_SYSTEM_PROMPT = (
    "You describe photographs factually for a writer who cannot see "
    "them. You report only what is visible in the image - never "
    "guess, infer beyond the frame, or add context you were not shown."
)

VISION_PROMPT = (
    "Describe this photo in at most 120 words, covering only what is "
    "visible: the subjects (vehicles, people, objects), the setting, "
    "any action, colors, light, and mood. Do not invent facts that "
    "are not visible: no model years, no names, no locations, no "
    "events, no specs. If something cannot be identified from the "
    "image, describe its appearance instead of naming it. Return "
    "only the description, no preamble."
)


def cap_words(text: str, limit: int = MAX_DESCRIPTION_WORDS) -> str:
    """Trim a description to at most `limit` words, deterministically."""
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit])


def clean_description(text: str) -> str:
    """Tidy a vision model's raw reply: drop a wrapping code fence
    and outer blank lines (mirrors the draft cleanup in generator),
    collapse blank-line runs, then apply the word cap."""
    cleaned = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cap_words(cleaned)


def make_ollama_vision(
    settings: LLMSettings,
    post_json: Callable[..., dict[str, Any]] | None = None,
) -> VisionFn:
    """A vision callable over Ollama's /api/chat.

    DEVIATION (same pattern as generator.make_ollama_chat dropping
    intake's format="json" pin): the house chat path
    (intake._call_ollama) builds text-only messages and cannot carry
    images, so this calls the same _post_json helper directly with
    the user message's "images" field (base64) populated - the only
    payload difference from make_ollama_chat, plus no format pin
    (descriptions are free text). post_json is injectable for tests.
    """
    post = post_json if post_json is not None else _post_json

    def describe(image_bytes: bytes) -> str:
        payload: dict[str, Any] = {
            "model": settings.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": VISION_PROMPT,
                    "images": [base64.b64encode(image_bytes).decode("ascii")],
                },
            ],
            "options": {
                "temperature": settings.temperature,
                "num_ctx": settings.num_ctx if settings.num_ctx else 4096,
            },
        }
        if settings.think is not None:
            payload["think"] = settings.think
        response = post(
            f"{_normalize_base_url(settings.base_url)}/api/chat",
            payload,
            timeout_seconds=settings.timeout_seconds,
        )
        content = response.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ContentQueueError("vision response did not include message.content text.")
        return clean_description(content)

    return describe
