"""Scoped edits: deterministic search/replace application.

The repair primitive of the scoped-edit repair seam (architecture
campaign step 1, 2026-11-23). A model asked to repair one file
answers with Aider-style edit blocks:

    <<<<<<< SEARCH
    the existing text, copied exactly
    =======
    the replacement text
    >>>>>>> REPLACE

This module is the deterministic half of that exchange — parsing
and application are pure text operations with no model in the loop:

- parsing is strict about the markers: a line that opens like a
  SEARCH block but does not complete as a well-formed block makes
  the whole response unparseable (None), never a partial read;
- application requires every SEARCH text to occur EXACTLY ONCE in
  the original content, computes all spans on the original, and
  rejects overlapping spans, empty searches, and no-op edits;
- application is all-or-nothing: any violation raises
  ScopedEditError and the content is untouched — there is no fuzzy
  matching and no partial application to recover from.

Text outside blocks (prose, code fences) is ignored by the parser;
the seam that consumes this module treats "no blocks" and
"unparseable" identically: the repair declines.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

_SEARCH_MARKER = "<<<<<<< SEARCH"
_DIVIDER_MARKER = "======="
_REPLACE_MARKER = ">>>>>>> REPLACE"


class ScopedEditError(ValueError):
    """A scoped edit set cannot be applied exactly as written."""


@dataclass(frozen=True)
class ScopedEdit:
    """One search/replace pair, texts exactly as they stand."""

    search: str
    replace: str


def parse_scoped_edits(text: str) -> list[ScopedEdit] | None:
    """The edit blocks in a model response, or None when malformed.

    Well-formed blocks only; prose between blocks is ignored. None
    (not a partial list) when any block-opening line fails to
    complete as a block: a SEARCH marker with no divider, a divider
    with no REPLACE marker, or a marker line that is not exactly
    the marker. An empty list means the response carried no blocks
    at all — the caller treats both outcomes as "no repair".
    """
    lines = text.splitlines()
    edits: list[ScopedEdit] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith("<<<<<<<"):
            index += 1
            continue
        if stripped != _SEARCH_MARKER:
            return None
        index += 1
        search_lines: list[str] = []
        while index < len(lines) and lines[index].strip() != _DIVIDER_MARKER:
            if lines[index].strip().startswith(("<<<<<<<", ">>>>>>>")):
                return None
            search_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            return None
        index += 1  # the divider
        replace_lines: list[str] = []
        while index < len(lines) and lines[index].strip() != _REPLACE_MARKER:
            if lines[index].strip().startswith(("<<<<<<<", "=======")):
                return None
            replace_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            return None
        index += 1  # the REPLACE marker
        edits.append(
            ScopedEdit(
                search="\n".join(search_lines),
                replace="\n".join(replace_lines),
            )
        )
    return edits


def apply_scoped_edits(content: str, edits: list[ScopedEdit]) -> str:
    """Apply an edit set to ``content`` exactly, or raise.

    Every search text must occur exactly once in the ORIGINAL
    content; spans are computed there, overlapping spans are
    rejected, and replacements are applied last-span-first so the
    offsets stay valid. Any violation raises ScopedEditError with
    the original content untouched (the result is built fresh and
    only returned whole).
    """
    if not edits:
        raise ScopedEditError("the edit set is empty")
    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        if not edit.search:
            raise ScopedEditError("an edit's search text is empty")
        if edit.search == edit.replace:
            raise ScopedEditError(
                "an edit's replacement is identical to its search text"
            )
        first = content.find(edit.search)
        if first == -1:
            raise ScopedEditError(
                "a search text does not occur in the content: "
                + repr(edit.search[:80])
            )
        if content.find(edit.search, first + 1) != -1:
            raise ScopedEditError(
                "a search text occurs more than once in the content: "
                + repr(edit.search[:80])
            )
        spans.append((first, first + len(edit.search), edit.replace))
    spans.sort(key=lambda span: span[0])
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            raise ScopedEditError("two edits' search spans overlap")
    result = content
    for start, end, replace in reversed(spans):
        result = result[:start] + replace + result[end:]
    if result == content:
        raise ScopedEditError("the edit set changes nothing")
    return result


def python_syntax_error(content: str) -> str | None:
    """The SyntaxError message for unparseable Python, else None."""
    try:
        ast.parse(content)
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno})"
    return None
