"""The substance critic (Track B — Pat's GAN pairing): a second model
as an internal measurement of a plan's write nodes, before execution.

Design: the planner (seeker7's Gemma) generates; a second model —
by configuration another node's Ollama, e.g. seeker88's llama3 —
critiques. Critique is a judgment task, not re-emission: the
formatter role already failed for the second model (matrix Arm B:
re-emitting a plan degraded its content), while nothing has tested
asking it only to judge. The critic is wired as a MEASUREMENT, not
a gate: gates belong at cliff edges (publish, real money, flashing,
sending), and a substance verdict is none of those — it is one more
measured failure the judge's existing machinery consumes.

Shape of the wiring (loop.LoopRunner owns the seam): after a plan
version is accepted and before it executes, the critic reviews each
write node's content against the goal for SUBSTANCE ONLY — does
this code actually compute/produce what the goal asks, or is it a
placeholder, an invented constant for a measurable value, an
unchanged copy of the current code, or the right code in a vessel
that cannot run? A hollow verdict becomes a measured failure on
that node (see loop._critic_review for the exact record shape),
the version does not execute, and the judge re-plans from a brief
carrying the critic's reason — under the existing re-plan budget,
with no new human gate anywhere.

Plumbing follows the cycle-2 formatter pattern exactly: a frozen
settings dataclass whose None fields fall back to the planner
call's own settings, an env reader that returns None when
unconfigured (FREETALON_CRITIC_ENABLED truthy, or either endpoint
variable set — FREETALON_CRITIC_OLLAMA_BASE_URL /
FREETALON_CRITIC_OLLAMA_MODEL; pointing the critic at seeker88 is
FREETALON_CRITIC_OLLAMA_BASE_URL=http://100.87.140.12:11434 with
FREETALON_CRITIC_OLLAMA_MODEL=llama3:latest, the endpoint the
matrix's Arm B used for the formatter), and degradation on any
backend failure: a critic that cannot judge must never block the
loop, so an unreachable/erroring/timing-out critic degrades the
whole review to no-critic and the version executes (the loop
audit-logs the degradation).

Two deliberate settings choices differ from the formatter's: the
temperature is pinned to 0.0 (a measurement must be reproducible —
the planner may be sampled at FREETALON_LLM_TEMPERATURE for
diversity, but the ruler must not move with it), and the verdict is
constrained by a small JSON Schema (critic_verdict_schema), the
cycle-2 two-channel shape: the verdict is data, so it travels
through the grammar; code never does.

A third choice lands with cycle 16: a hollow verdict must carry
EVIDENCE — the exact line(s) from the shown content that assign
the alleged invented constant, quoted verbatim — and the harness
verifies the quote against the content the critic was shown
before the verdict may block anything. The live enable test
(2026-11-18) showed why: llama3 hollow-flagged both write nodes
of a plan whose code genuinely computed both measured values,
and its reason restated the rubric line for the goal's shape
instead of anything in the content — a judge matching the goal
instead of reading the code. An unquotable suspicion is not a
verdict: a hollow verdict whose evidence is missing, empty, or
not found verbatim in the shown content is VOID — the node is
treated as substantive, the voiding is audit-logged
(loop.critic.voided), and the review continues with the
remaining nodes. Voiding is not degradation: the critic
answered; its verdict simply carried no checkable evidence.

Cycle 17 relaxes the quote check from whole-string to
per-line: every non-empty line of a quote, stripped, must
appear within some stripped line of the shown content. A
judge re-quoting an indented block naturally drops the
block's common indentation — cycle 16's battery had
qwen2.5-coder:7b quote all three Ellipsis lines correctly
but dedented, and the whole-string check voided a right
verdict. Line for line the words remain verbatim — each
quoted line must exist in the content — so the grounding
requirement is unchanged; only block re-indentation stops
counting as fabrication. The prompt now also asks for the
evidence as a list, one quoted line per item.

Cycle 18 adds a deterministic rule beside the judge. One
hollowness shape needs no judgment at all: a line that binds a
literal value (a dict entry or an assignment) under a comment
confessing the value is a placeholder — "replace with actual
check" and its kin. The confession is the tell: legitimate
literal bindings (a fixed status label, a fallback initializer
in front of a real probe) do not confess, so the rule requires
BOTH halves on ONE line — a literal binding AND a confession
marker in that line's comment. A firing line is hollow by
construction, with the line itself as the evidence (it passes
the per-line quote check trivially), and the verdict's reason
is prefixed "deterministic:" so the audit trail never confuses
it with a judge's call. The rule runs before the judge for
each node: a node it convicts is not put to the judge at all.
The battery motivated the split — qwen2.5-coder:7b, the one
judge that never false-flagged a substantive node, judged the
confession-carrying corpus case substantive outright in every
round, while llama3 could find it only when steered, and
convicted the positive control in exchange. What a comment
confesses, code should decide; what it does not, the judge
still judges.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path
from typing import Any

from .intake import LLMSettings, call_llm
from .models import ExecutionPlan, PlanNode
from .prompts import CRITIC_SYSTEM_PROMPT

# The write claws, mirroring planner._WRITE_CLAWS (cycle 8): anchored
# edits arrive here as scaffold nodes after materialization, so the
# pair covers scaffold / file_writer / anchored-edit payloads alike.
_WRITE_CLAWS = ("scaffold", "file_writer")

# The planner's context window (retry #22): the critic's prompt
# carries a node's full content plus the target's current source.
_CRITIC_NUM_CTX = 12288

# Prompt bounds: full source at these sizes is the grounded
# skeleton's budget family (planner: 10,000 chars full-source) — a
# larger payload is shown truncated, with the truncation stated, and
# the critic judges what it can see.
_CONTENT_MAX_CHARS = 10_000
_CURRENT_MAX_CHARS = 8_000


@dataclass(frozen=True)
class CriticSettings:
    """Endpoint/model override for the optional critic pass.

    None fields fall back to the planner call's own settings, so
    with only FREETALON_CRITIC_ENABLED set the critic is the same
    local Ollama the planner uses; pointing it at another node's
    model (the GAN pairing) is an env change, not a code change.
    """

    base_url: str | None = None
    model: str | None = None


def critic_settings_from_env() -> CriticSettings | None:
    """The configured critic, or None when not configured.

    The pass is enabled by FREETALON_CRITIC_ENABLED (truthy) or by
    setting either endpoint variable; FREETALON_CRITIC_OLLAMA_BASE_
    URL / FREETALON_CRITIC_OLLAMA_MODEL then select where the
    critic call goes. With none of the three set, the loop runs
    exactly as before this plumbing existed.
    """
    enabled = os.environ.get(
        "FREETALON_CRITIC_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    base_url = (
        os.environ.get("FREETALON_CRITIC_OLLAMA_BASE_URL", "").strip() or None
    )
    model = (
        os.environ.get("FREETALON_CRITIC_OLLAMA_MODEL", "").strip() or None
    )
    if not enabled and base_url is None and model is None:
        return None
    return CriticSettings(base_url=base_url, model=model)


def critic_verdict_schema() -> dict[str, Any]:
    """The critic verdict as a JSON Schema for Ollama's ``format``.

    Kept shallow like the plan envelope (small models degrade on
    deeply nested schemas): one verdict word, one reason line, and
    the evidence a hollow verdict must produce — a list of quoted
    lines, one per item (a single string is speakable too; the
    parser accepts both) — the harness verifies each quoted line
    against the shown content (see _parse_verdict); the schema
    only makes the field speakable.
    """
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["substantive", "hollow"]},
            "reason": {"type": "string"},
            "evidence": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
        },
        "required": ["verdict", "reason"],
    }


@dataclass(frozen=True)
class CriticVerdict:
    """One write node's substance verdict.

    ``evidence`` carries the verified verbatim quote(s) behind a
    hollow verdict (empty for substantive verdicts, and for
    verdicts the harness voided — those are recorded separately
    on the review as VoidedVerdict records).
    """

    node_id: str
    substantive: bool
    reason: str
    evidence: str = ""


@dataclass(frozen=True)
class VoidedVerdict:
    """A hollow verdict the harness voided for lack of evidence.

    The critic answered, but its hollow verdict quoted nothing
    checkable from the shown content — no evidence field, an
    empty quote, or a quote the content does not contain. The
    verdict is void: the node is treated as substantive, and the
    voiding is recorded here so the caller can audit-log it
    (loop.critic.voided). A voided verdict never marks a node
    FAILED, and it does not degrade the review: the remaining
    nodes' verdicts stand on their own evidence.
    """

    node_id: str
    reason: str
    evidence: str
    detail: str


@dataclass
class CriticReview:
    """The outcome of reviewing one plan version's write nodes.

    ``degraded`` is set when the critic could not judge at all
    (backend unreachable, verdict unparseable): the review is then
    void — no verdict in it may block execution — and the caller
    proceeds as if there were no critic, recording the degradation.
    A partial review is never trusted: one node's failure voids the
    whole version's review rather than blocking on half a ruler.
    """

    verdicts: list[CriticVerdict] = field(default_factory=list)
    voided: list[VoidedVerdict] = field(default_factory=list)
    degraded: str | None = None

    @property
    def hollow(self) -> list[CriticVerdict]:
        return [v for v in self.verdicts if not v.substantive]


def _node_target(node: PlanNode) -> str | None:
    """The node's write target, by the scaffold claw's own expression.

    Mirrors tool_registry's scaffold handler exactly (file_path,
    else path, else the first inputs outputs entry) — the same
    expression cycle 7's missing_target floor mirrors — so the
    critic judges the file the claw will actually write.
    """
    inputs = node.inputs or {}
    target = inputs.get("file_path") or inputs.get("path")
    if not target:
        outputs = inputs.get("outputs")
        if isinstance(outputs, (list, tuple)) and outputs:
            target = outputs[0]
    return target if isinstance(target, str) and target else None


def _node_content(node: PlanNode) -> str | None:
    """The content the claw will write (content, else file_content)."""
    inputs = node.inputs or {}
    content = inputs.get("content") or inputs.get("file_content")
    if isinstance(content, str) and content.strip():
        return content
    return None


def _reviewable(node: PlanNode) -> bool:
    """A write node with content to judge.

    A write node with no content string is skipped, not judged:
    there is nothing to measure, and the defect it may carry
    (no target, no content) belongs to the planning validators
    (missing_target and family), not to a substance judgment.
    """
    return node.assigned_claw in _WRITE_CLAWS and _node_content(node) is not None


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated at {limit} chars ...]"


def _current_source(plan: ExecutionPlan, target: str) -> str | None:
    """The target file as it stands on disk now, when it exists.

    The unchanged-copy verdict class is only judgeable against the
    current source. Relative targets resolve against the plan's
    stamped repo_root (cycle 4) when present. Returns None when the
    file does not exist or cannot be read — a new file has no
    current form to copy.
    """
    path = Path(target).expanduser()
    if not path.is_absolute():
        stamped = (plan.metadata or {}).get("repo_root")
        if isinstance(stamped, str) and stamped:
            path = Path(stamped) / path
    try:
        if not path.is_file():
            return None
        return _bounded(path.read_text(encoding="utf-8"), _CURRENT_MAX_CHARS)
    except OSError:
        return None


def _critic_prompt(goal_text: str, plan: ExecutionPlan, node: PlanNode) -> str:
    target = _node_target(node)
    content = _node_content(node) or ""
    lines = [
        f"Goal: {goal_text}",
        "",
        f"Node {node.id} (claw: {node.assigned_claw}) — objective: {node.objective}",
        f"Target file: {target or '(unnamed)'}",
    ]
    anchor = (node.inputs or {}).get("anchor")
    if isinstance(anchor, str) and anchor:
        position = (node.inputs or {}).get("position") or "replace"
        lines.append(
            f"Anchored edit: this content is the {position} text for the "
            f"definition {anchor} inside the target file, not the whole file."
        )
    lines += [
        "",
        "The content this node will write:",
        "---",
        _bounded(content, _CONTENT_MAX_CHARS),
        "---",
    ]
    current = _current_source(plan, target) if target else None
    if current is not None:
        lines += [
            "",
            "The target file as it stands on disk now:",
            "---",
            current,
            "---",
        ]
    lines += ["", "JSON Output:"]
    return "\n".join(lines)


def _evidence_quotes(value: Any) -> list[str] | None:
    """The verdict's evidence as a list of quotes, or None.

    The contract asks for a list of strings, one quoted line per
    item (cycle 17); a single string quoting the offending line(s)
    verbatim is accepted too. Anything else — absent, a number, an
    empty list, a list holding non-strings — is no usable
    evidence.
    """
    if isinstance(value, str):
        return [value]
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
    ):
        return list(value)
    return None


def _quote_lines(quote: str) -> list[str]:
    """A quote's non-empty lines, each stripped of surrounding
    whitespace — the units the evidence check verifies."""
    return [line.strip() for line in quote.splitlines() if line.strip()]


def _quote_verified(quote: str, content_lines: list[str]) -> bool:
    """Per-line verification of one evidence quote (cycle 17).

    The quote verifies when every one of its non-empty lines,
    stripped, appears within some stripped line of the shown
    content (``content_lines`` is the shown content pre-split and
    pre-stripped). A quote with no non-empty lines never
    verifies. Single-line quotes behave exactly as the cycle-16
    whole-string check did; multi-line quotes gain only the
    freedom to re-indent the block they copy — every quoted line
    must still exist in the content, character for character
    after the strip.
    """
    lines = _quote_lines(quote)
    if not lines:
        return False
    return all(
        any(line in content_line for content_line in content_lines)
        for line in lines
    )


def _parse_verdict(
    node_id: str, response_text: str, shown_content: str
) -> tuple[CriticVerdict, VoidedVerdict | None]:
    """Parse one verdict; any defect raises (the review degrades).

    A verdict the harness cannot parse is not a verdict — trusting
    half of one would let a malformed reply block real work, so the
    whole review degrades to no-critic instead (see CriticReview).

    A HOLLOW verdict is additionally checked, not trusted: it must
    quote, verbatim, the line(s) of the shown content that assign
    the alleged invented constant. A hollow verdict that quotes
    nothing checkable is VOID — returned here as a substantive
    verdict plus a VoidedVerdict record — because the failure this
    guards against is a judge restating the rubric for the goal's
    shape while the content it was shown computes the value. The
    check is per line (cycle 17): every non-empty line of a quote,
    stripped of surrounding whitespace, must appear within some
    stripped line of the shown content. Within a line the match
    is character for character; across lines a quote may
    re-indent the block it copies — a judge quoting an indented
    block drops its common indentation, and that alone must not
    void a grounded verdict.
    """
    data = json.loads(response_text.strip())
    if not isinstance(data, dict):
        raise ValueError("critic verdict is not a JSON object")
    verdict = data.get("verdict")
    if verdict not in ("substantive", "hollow"):
        raise ValueError(f"critic verdict has no usable verdict field: {verdict!r}")
    reason = data.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if verdict == "substantive":
        return CriticVerdict(node_id=node_id, substantive=True, reason=reason), None
    if not reason:
        reason = "the critic judged this content hollow (no reason given)"
    raw_evidence = data.get("evidence")
    quotes = _evidence_quotes(raw_evidence)
    if isinstance(raw_evidence, str):
        offered = raw_evidence
    elif raw_evidence is None:
        offered = ""
    else:
        offered = json.dumps(raw_evidence)
    void_detail: str | None = None
    if quotes is None:
        void_detail = "hollow verdict carried no usable evidence quote"
    else:
        content_lines = [line.strip() for line in shown_content.splitlines()]
        if any(not _quote_lines(quote) for quote in quotes):
            void_detail = "hollow verdict's evidence quote is empty"
        else:
            missing = [
                quote.strip()
                for quote in quotes
                if not _quote_verified(quote, content_lines)
            ]
            if missing:
                void_detail = (
                    "hollow verdict's evidence is not in the shown content: "
                    f"{missing[0][:120]!r}"
                )
    if void_detail is not None:
        return (
            CriticVerdict(node_id=node_id, substantive=True, reason=reason),
            VoidedVerdict(
                node_id=node_id,
                reason=reason,
                evidence=offered,
                detail=void_detail,
            ),
        )
    assert quotes is not None  # for the type checker; void_detail is None here
    return (
        CriticVerdict(
            node_id=node_id,
            substantive=False,
            reason=reason,
            evidence="\n".join(quote.strip() for quote in quotes),
        ),
        None,
    )


# Cycle 18: the deterministic confession rule. A line that binds a
# literal under a comment confessing placeholder status is hollow
# by construction — no judge call is spent on it, and none is
# needed: the confession is in the content, checkable by anyone.
_CONFESSION_MARKERS = (
    "replace with actual",
    "placeholder",
    "todo",
    "fixme",
    "dummy",
    "stub",
    "mock",
    "fake",
    "for now",
    "temporary",
)

# A literal the rule recognizes: a boolean, a number, or a quoted
# string. Deliberately narrow — an Ellipsis, a name, or a call is
# not a literal binding, whatever comment rides beside it.
_LITERAL_PATTERN = (
    r"(?:True|False|-?\d+(?:\.\d+)?|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
)

# A dict entry binding a literal at the end of the code part of a
# line (an optional trailing comma aside): 'key': <literal>.
_DICT_LITERAL_RE = re.compile(
    r"['\"][^'\"\n]+['\"]\s*:\s*" + _LITERAL_PATTERN + r"\s*,?\s*$"
)

# An assignment binding a literal at the end of the code part:
# name = <literal> (subscript/attribute targets included; the
# lookarounds keep ==, <=, >=, != from counting as bindings).
_ASSIGN_LITERAL_RE = re.compile(
    r"(?<![<>=!])=(?!=)\s*" + _LITERAL_PATTERN + r"\s*$"
)


def _split_comment(line: str) -> tuple[str, str]:
    """Split a source line into (code, comment) at the first '#'
    outside a string literal; the comment is '' when the line has
    none. Quote state is tracked within the line, so a '#' inside
    a quoted string is not a comment start."""
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index], line[index + 1 :]
        index += 1
    return line, ""


def _confession_marker(comment: str) -> str | None:
    """The first placeholder-confession marker in a comment, if any."""
    lowered = comment.lower()
    for marker in _CONFESSION_MARKERS:
        if marker in lowered:
            return marker
    return None


def deterministic_hollow_evidence(
    shown_content: str,
) -> tuple[str, str] | None:
    """The deterministic confession rule over one node's shown content.

    Returns (line, marker) for the first line that BOTH binds a
    literal value (a dict entry or an assignment) AND carries a
    comment confessing the value is a placeholder — or None when
    no line does. The confession requirement is the safety: a
    literal binding alone is often legitimate (a fixed status
    label, a fallback initializer in front of a real probe), and
    a confession alone is just a note; together on one line they
    are an admitted invention of a value.
    """
    for raw_line in shown_content.splitlines():
        code, comment = _split_comment(raw_line)
        if not comment:
            continue
        marker = _confession_marker(comment)
        if marker is None:
            continue
        if _DICT_LITERAL_RE.search(code) or _ASSIGN_LITERAL_RE.search(code):
            return raw_line.strip(), marker
    return None


def review_write_nodes(
    plan: ExecutionPlan,
    goal_text: str,
    critic: CriticSettings,
    settings: LLMSettings | None = None,
) -> CriticReview:
    """Review a plan version's write nodes for substance.

    One judge call per reviewable write node, in plan order —
    except a node the deterministic confession rule convicts,
    which is decided on its own line without a call. Never
    raises: any backend or parse failure voids the review
    (``degraded`` carries the detail) and the caller executes the
    version as if there were no critic — the second model's
    availability must never block the loop.
    """
    targets = [node for node in plan.nodes if _reviewable(node)]
    if not targets:
        return CriticReview()
    effective = settings or LLMSettings.from_env()
    effective = dataclass_replace(
        effective,
        base_url=critic.base_url or effective.base_url,
        model=critic.model or effective.model,
        format_schema=critic_verdict_schema(),
        format_mode="schema",
        temperature=0.0,
        num_ctx=_CRITIC_NUM_CTX,
    )
    verdicts: list[CriticVerdict] = []
    voided: list[VoidedVerdict] = []
    try:
        for node in targets:
            # The evidence check runs against exactly what the
            # prompt shows the critic for this node — the bounded
            # content _critic_prompt renders — and the
            # deterministic rule judges that same text.
            shown = _bounded(_node_content(node) or "", _CONTENT_MAX_CHARS)
            # Cycle 18: the deterministic confession rule runs
            # before the judge. A line that binds a literal under
            # a comment confessing it is a placeholder is hollow
            # by construction, so the node is convicted on the
            # line itself and never put to the judge.
            confession = deterministic_hollow_evidence(shown)
            if confession is not None:
                line, marker = confession
                verdicts.append(
                    CriticVerdict(
                        node_id=node.id,
                        substantive=False,
                        reason=(
                            "deterministic: the content binds a literal "
                            "value under a comment confessing it is a "
                            f"placeholder ({marker!r}) — the value is "
                            "admitted invented, not computed or measured"
                        ),
                        evidence=line,
                    )
                )
                continue
            response = call_llm(
                CRITIC_SYSTEM_PROMPT,
                _critic_prompt(goal_text, plan, node),
                settings=effective,
            )
            verdict, void = _parse_verdict(node.id, response, shown)
            verdicts.append(verdict)
            if void is not None:
                voided.append(void)
    except Exception as exc:  # noqa: BLE001 - any critic failure degrades the review
        return CriticReview(degraded=f"{type(exc).__name__}: {exc}")
    return CriticReview(verdicts=verdicts, voided=voided)
