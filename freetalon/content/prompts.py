"""Prompts for content draft generation.

The voice notes are drawn from the site's own Archives posts
(Ebisu Drift Matsuri, Midnight at Daikoku Futo, Tsukuba Time Attack):
first-person enthusiast writing, scene first, concrete detail,
short paragraphs, markdown section headers - and no marketing voice.

The honesty rule is load-bearing: Pat's blurb and the media list are
the ONLY facts the model gets, and every prompt says so. A thin blurb
earns a post about the scene and the feeling, never invented specs,
names, dates, or people.
"""

from __future__ import annotations

from typing import Any

VOICE_SYSTEM_PROMPT = (
    "You write for inertiald.com, a personal site about Japanese car culture, "
    "in the voice of its owner: a knowledgeable enthusiast writing for friends "
    "who get it. First person, warm and unhurried, concrete about cars and "
    "places, allergic to hype, clickbait, and marketing speak. Short "
    "paragraphs. You only state facts you were given - never invent specs, "
    "names, dates, places, people, or events."
)

_HONESTY = (
    "Use ONLY the facts in the notes and media list below. If the notes are "
    "thin, write about the scene and the feeling in general terms rather "
    "than inventing specifics."
)


def describe_media(media: list[dict[str, Any]]) -> list[str]:
    """One prompt line per media item; `media` dicts may carry a
    pre-filled 'dimensions' key of (width, height) or None."""
    lines = []
    for item in media:
        parts = [f"{item.get('kind', 'file')}: {item.get('originalName') or item.get('filename', '')}"]
        dimensions = item.get("dimensions")
        if dimensions:
            parts.append(f"{dimensions[0]}x{dimensions[1]}")
        size = item.get("size")
        if isinstance(size, (int, float)) and size:
            parts.append(f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{max(size / 1024, 0.1):.0f} KB")
        lines.append("- " + ", ".join(parts))
    return lines


def entry_context(entry: dict[str, Any]) -> str:
    blurb = str(entry.get("blurb") or "").strip()
    lines = ["Pat's notes (the source of truth):", blurb if blurb else "(no notes provided)"]
    media_lines = describe_media(list(entry.get("media") or []))
    if media_lines:
        lines += ["", "Attached media:", *media_lines]
    return "\n".join(lines)


def blog_prompt(entry: dict[str, Any]) -> str:
    return (
        f"{entry_context(entry)}\n\n"
        "Write the blog post draft for the site's Archives, in Markdown. "
        "Start with a '# ' title line, then the post with '## ' section "
        "headers, 350-650 words. Build a real post around the notes - the "
        "scene, the machine, why it matters - instead of restating them. "
        f"{_HONESTY} Return only the Markdown, no preamble."
    )


def instagram_prompt(entry: dict[str, Any]) -> str:
    return (
        f"{entry_context(entry)}\n\n"
        "Write the Instagram caption: 2-4 short paragraphs in the same "
        "voice, then a blank line and one line of 5-10 relevant hashtags. "
        f"{_HONESTY} Return only the caption."
    )


def youtube_prompt(entry: dict[str, Any]) -> str:
    return (
        f"{entry_context(entry)}\n\n"
        "Write the publish package for a YouTube video on this subject. "
        "First line exactly: 'TITLE: ' followed by the video title (under "
        "80 characters, no clickbait). Then a blank line, then the video "
        "description: 2-3 short paragraphs saying what the video covers "
        "and why it is worth watching, closing with one line inviting "
        f"comments. {_HONESTY} Return only that text."
    )


PROMPT_BUILDERS = {
    "blog": blog_prompt,
    "instagram": instagram_prompt,
    "youtube": youtube_prompt,
}
