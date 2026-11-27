"""LLM-backed intake pipeline for structured TaskIntent generation."""

from __future__ import annotations
import re
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from .models import TaskIntent
from .prompts import INTAKE_SYSTEM_PROMPT

_OLLAMA_BASE_URL = "http://localhost:11434"
_OLLAMA_MODEL = "gemma-3-abliterated:latest"
_OPENAI_BASE_URL = "http://localhost:8000/v1"
_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_TIMEOUT_SECONDS = 60.0


class LLMBackendError(RuntimeError):
    """Raised when the configured LLM backend cannot be reached or decoded."""


class LLMResponseError(ValueError):
    """Raised when an LLM response does not match the expected JSON contract."""


@dataclass(frozen=True)
class LLMSettings:
    """Runtime configuration for the intake/planner LLM backend."""

    backend: Literal["ollama", "openai_compatible"] = "ollama"
    base_url: str = _OLLAMA_BASE_URL
    model: str = _OLLAMA_MODEL
    api_key: str | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    # Sampling temperature for every call made with these settings
    # (intake, planner, and the formatter pass, which derives its
    # settings from the planner's). The default 0.0 keeps generation
    # deterministic — the loop's live A/B measurements replicate
    # exactly at temperature 0 (CP2). FREETALON_LLM_TEMPERATURE raises
    # it when a run needs distinct samples instead of replicas.
    temperature: float = 0.0
    # Ollama context window for this call (num_ctx). None means the
    # backend default (4096). The planner sets it explicitly: its
    # prompts do not fit in 4096 (retry #22). FREETALON_LLM_NUM_CTX
    # sets it from the environment; when set, the planner respects
    # it instead of substituting its own default (cycle 14:
    # qwen3.8's plan verbosity overflows the 12288 planner window).
    num_ctx: int | None = None
    # Ollama per-call generation ceiling (num_predict). None means no
    # ceiling is sent — generation ends only at the context window,
    # which is how the pairing run's planner call burned ~21,850
    # tokens (89% of its window) inside one schema field before the
    # stream was cut. The planner substitutes its own default
    # (_PLANNER_NUM_PREDICT) for plan calls. FREETALON_LLM_NUM_PREDICT
    # sets it from the environment under the same tolerant-knob rule
    # as num_ctx: unset/unparsable/non-positive leaves it None.
    num_predict: int | None = None
    format_schema: dict[str, Any] | None = None
    # Campaign build #14: the transport format directive, stated
    # explicitly instead of being overloaded onto format_schema's
    # None. "schema" sends format_schema as the format constraint;
    # "json" sends the bare JSON directive; "unconstrained" sends
    # NO format field at all, for calls whose expected answer is
    # free-form (edit blocks, a code definition, prose). None —
    # the default — resolves from format_schema: "schema" when a
    # schema is set, "json" otherwise (the pre-build-#14 behavior
    # for every call that does not state a mode). Before this
    # field existed, a call that dropped its schema to escape the
    # plan envelope (the repair seams) silently fell back to
    # "json": the transport ordered JSON while the prompt ordered
    # edit blocks, and arm #23's seam answered with fabricated
    # API-error JSON bodies instead of blocks.
    format_mode: Literal["schema", "json", "unconstrained"] | None = None
    # Ollama thinking control (the top-level "think" chat field).
    # None — the default, and what an unset or unrecognized
    # FREETALON_LLM_THINK yields — sends no field at all: the payload
    # stays exactly what it was before cycle 13, and models without a
    # thinking mode never see a field they cannot honor. False asks a
    # thinking model (qwen3.8, gemma4) to answer directly: re-run #9
    # showed both spending the whole generation thinking — qwen3.8's
    # plan JSON truncated mid-string against the context window and
    # gemma4's first planning call returned empty content.
    think: bool | None = None

    @classmethod
    def from_env(cls) -> "LLMSettings":
        """Build backend settings from environment variables."""
        backend = os.environ.get("FREETALON_LLM_BACKEND", "ollama").strip().lower()
        timeout_text = os.environ.get(
            "FREETALON_LLM_TIMEOUT_SECONDS",
            str(_DEFAULT_TIMEOUT_SECONDS),
        )
        try:
            timeout_seconds = float(timeout_text)
        except ValueError as exc:
            raise ValueError(
                "FREETALON_LLM_TIMEOUT_SECONDS must be a positive number."
            ) from exc
        if timeout_seconds <= 0:
            raise ValueError("FREETALON_LLM_TIMEOUT_SECONDS must be greater than zero.")

        # Temperature is a tuning knob, not an operational limit: a
        # mistyped or negative value falls back to the deterministic
        # default instead of bricking intake (0.0 is always safe —
        # it is the behavior every run shipped with).
        temperature_text = os.environ.get("FREETALON_LLM_TEMPERATURE", "0")
        try:
            temperature = float(temperature_text)
        except ValueError:
            temperature = 0.0
        if temperature < 0:
            temperature = 0.0

        # Think is a tuning knob like temperature: an unset or
        # unrecognized value leaves it None (the field is not sent —
        # today's behavior), never an error that bricks intake.
        think_text = os.environ.get("FREETALON_LLM_THINK", "").strip().lower()
        think: bool | None = None
        if think_text in ("1", "true", "yes", "on"):
            think = True
        elif think_text in ("0", "false", "no", "off"):
            think = False

        # Num_ctx is a tuning knob like temperature: an unset,
        # unparsable, or non-positive value leaves it None — the
        # field unsent, the planner's own default in force (today's
        # behavior) — never an error that bricks intake.
        num_ctx_text = os.environ.get("FREETALON_LLM_NUM_CTX", "").strip()
        num_ctx: int | None = None
        if num_ctx_text:
            try:
                parsed_num_ctx = int(num_ctx_text)
            except ValueError:
                parsed_num_ctx = 0
            if parsed_num_ctx > 0:
                num_ctx = parsed_num_ctx

        # Num_predict is a tuning knob like num_ctx: an unset,
        # unparsable, or non-positive value leaves it None — no
        # ceiling sent, the planner's own default in force — never
        # an error that bricks intake.
        num_predict_text = os.environ.get(
            "FREETALON_LLM_NUM_PREDICT", ""
        ).strip()
        num_predict: int | None = None
        if num_predict_text:
            try:
                parsed_num_predict = int(num_predict_text)
            except ValueError:
                parsed_num_predict = 0
            if parsed_num_predict > 0:
                num_predict = parsed_num_predict

        if backend == "ollama":
            return cls(
                backend="ollama",
                base_url=os.environ.get("FREETALON_OLLAMA_BASE_URL", _OLLAMA_BASE_URL).strip(),
                model=os.environ.get("FREETALON_OLLAMA_MODEL", _OLLAMA_MODEL).strip(),
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                think=think,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )
        if backend == "openai_compatible":
            api_key = os.environ.get("FREETALON_OPENAI_API_KEY")
            return cls(
                backend="openai_compatible",
                base_url=os.environ.get(
                    "FREETALON_OPENAI_BASE_URL",
                    _OPENAI_BASE_URL,
                ).strip(),
                model=os.environ.get("FREETALON_OPENAI_MODEL", _OPENAI_MODEL).strip(),
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                think=think,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )
        raise ValueError(
            "Unsupported FREETALON_LLM_BACKEND. Expected 'ollama' or 'openai_compatible'."
        )


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("LLM base URL must not be empty.")
    return normalized


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_text = _decode_response_text(response.read())
    except urllib.error.HTTPError as exc:
        body_text = _decode_response_text(exc.read())
        raise LLMBackendError(f"LLM request failed with HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise LLMBackendError(f"LLM request failed: {exc.reason}") from exc

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LLMBackendError("LLM backend returned a non-JSON HTTP response.") from exc
    if not isinstance(data, dict):
        raise LLMBackendError("LLM backend returned an unexpected JSON payload.")
    return data


def _decode_response_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace")


def _extract_json_candidate(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    return stripped


def parse_json_object(raw_text: str) -> dict[str, Any]:
    """Parse a JSON object from model output with small recovery heuristics."""
    candidate = _extract_json_candidate(raw_text)
    attempts = [candidate]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and start < end:
        attempts.append(candidate[start : end + 1])

    for attempt in attempts:
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            raise LLMResponseError("Expected a JSON object in the LLM response.")
        return parsed
    raise LLMResponseError(
        "LLM response was not valid JSON. Expected a single JSON object matching the prompt contract."
    )


def json_truncation_diagnosis(raw_text: str) -> str | None:
    """Why ``raw_text`` reads as a TRUNCATED JSON document, or None.

    Campaign build #31: the planner's parse failure used to be a
    single undifferentiated "not valid JSON", but arm #39's three
    rejected re-plan skeletons were a different failure wearing
    that label — each was cut off mid-document at the context
    wall (the Ollama log shows the slot stopping at the window
    edge, truncated=1, on all three). A truncated reply is an
    unfinished PREFIX of a JSON document: scanned with string and
    bracket state, it ends inside a string or with brackets still
    open. Malformed-but-complete text (balanced, with its defect
    mid-document) is not truncation and returns None, as does
    anything that parses, anything fenced the usual way that
    parses after extraction, and anything not starting with "{"
    (prose is a shape failure, never a length failure). The scan
    is deliberately conservative: it can only ever relabel a
    rejection the parse already made, never accept anything.
    """
    candidate = _extract_json_candidate(raw_text)
    if not candidate.startswith("{"):
        return None
    try:
        json.loads(candidate)
        return None
    except json.JSONDecodeError:
        pass
    depth = 0
    in_string = False
    escaped = False
    for char in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth < 0:
                return None
    if in_string:
        return "the text ends inside a string"
    if depth > 0:
        return f"the text ends with {depth} bracket(s) still open"
    return None


def sanitize_llm_output(raw_text: str) -> str:
    """Clean raw SentencePiece UNK_BYTE token artifacts output by Gemma models."""
    # Same two-pass decode as the planner's copy (see planner.py):
    # the UNK_BYTE pass runs first and consumes the bracketed
    # artifacts whole -- the artifact hex (0xe29681) is U+2581's own
    # UTF-8 encoding, so it restores the same space the bare-marker
    # conversion below restores. A bare U+2581 IS the SentencePiece
    # space marker; deleting it (the old behavior here) fused words
    # in goal text and stripped indentation from any marker-carrying
    # field. This duplicate was flagged by the planner sanitize fix
    # (b3a0dfb) and is corrected to match it.
    cleaned = re.sub(r'\[UNK_BYTE_[^\]]+▁?([^\]]*)\]', r' \1', raw_text)
    cleaned = cleaned.replace("\xe2\x96\x81", " ").replace("▁", " ")
    return cleaned

def parse_task_intent_response(raw_text: str, user_request: str | None = None) -> TaskIntent:
    """Validate raw model output as a TaskIntent with defensive fallbacks."""
    # 1. Sanitize Gemma tokenizer artifacts
    raw_text = sanitize_llm_output(raw_text)
    
    # 2. Print RAW output for live terminal logs
    print(f"\n--- RAW INTAKE OUTPUT ---\n{raw_text}\n--------------------------\n")
    
    payload = parse_json_object(raw_text)
    # ... rest of function remains the same ...
    if not isinstance(payload, dict):
        payload = {}

    # 3. Defensive fallbacks if LLM returns null/missing values
    if not payload.get("goal") or not isinstance(payload.get("goal"), str):
        payload["goal"] = user_request or "Unspecified task"

    if not isinstance(payload.get("capabilities"), list):
        payload["capabilities"] = []

    if not isinstance(payload.get("constraints"), dict):
        payload["constraints"] = {}

    if not isinstance(payload.get("missing_inputs"), list):
        payload["missing_inputs"] = []

    if not payload.get("project_type") or not isinstance(payload.get("project_type"), str):
        payload["project_type"] = "general"

    try:
        return TaskIntent.model_validate(payload)
    except ValidationError as exc:
        raise LLMResponseError(f"LLM response did not match TaskIntent schema: {exc}") from exc


def resolved_format_mode(
    settings: LLMSettings,
) -> Literal["schema", "json", "unconstrained"]:
    """The format directive a call actually carries (build #14).

    An explicit format_mode wins; otherwise the mode resolves
    from format_schema — a set schema means "schema", no schema
    means "json" (the historical default every unstated call
    shipped with).
    """
    if settings.format_mode is not None:
        return settings.format_mode
    return "schema" if settings.format_schema else "json"


def _call_ollama(settings: LLMSettings, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": settings.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": settings.temperature,
        	"num_ctx": settings.num_ctx if settings.num_ctx else 4096},
    }
    # Campaign build #14: the format field is set from the
    # resolved mode — "schema" carries the schema dict, "json"
    # the bare directive, and "unconstrained" omits the field
    # entirely so a free-form answer (edit blocks, code, prose)
    # is not contradicted by a JSON order at the transport.
    _format_mode = resolved_format_mode(settings)
    if _format_mode == "schema":
        payload["format"] = (
            settings.format_schema if settings.format_schema else "json"
        )
    elif _format_mode == "json":
        payload["format"] = "json"
    if settings.think is not None:
        # Ollama's think switch is a top-level chat field, not an
        # "options" entry. Sent only when configured: at the default
        # the payload is identical to the pre-cycle-13 one.
        payload["think"] = settings.think
    if settings.num_predict is not None:
        # The generation ceiling IS an "options" entry. Sent only
        # when configured: at the default the payload is identical
        # to the pre-redesign one. A generation cut by the ceiling
        # fails parse cheaply instead of filling the context window.
        payload["options"]["num_predict"] = settings.num_predict
    response = _post_json(
        f"{_normalize_base_url(settings.base_url)}/api/chat",
        payload,
        timeout_seconds=settings.timeout_seconds,
    )
    content = response.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMBackendError("Ollama response did not include message.content text.")
    return content


def _call_openai_compatible(
    settings: LLMSettings,
    system_prompt: str,
    user_prompt: str,
) -> str:
    headers: dict[str, str] = {}
    if settings.api_key:
        headers["Authorization"] = "Bearer " + settings.api_key
    payload = {
        "model": settings.model,
        "temperature": settings.temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response = _post_json(
        f"{_normalize_base_url(settings.base_url)}/chat/completions",
        payload,
        timeout_seconds=settings.timeout_seconds,
        headers=headers,
    )
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMBackendError("OpenAI-compatible response did not include choices.")
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMBackendError(
            "OpenAI-compatible response did not include choices[0].message.content text."
        )
    return content


def call_llm(system_prompt: str, user_prompt: str, settings: LLMSettings | None = None) -> str:
    """Dispatch a chat-style request to the configured LLM backend."""
    llm_settings = settings or LLMSettings.from_env()
    if llm_settings.backend == "ollama":
        return _call_ollama(llm_settings, system_prompt, user_prompt)
    if llm_settings.backend == "openai_compatible":
        return _call_openai_compatible(llm_settings, system_prompt, user_prompt)
    raise ValueError(f"Unsupported LLM backend: {llm_settings.backend}")


def intake_request(raw_request: str, settings: LLMSettings | None = None) -> TaskIntent:
    """Convert a raw user request into a validated TaskIntent."""
    request_text = raw_request.strip()
    if not request_text:
        raise ValueError("Raw user request must not be empty.")

    llm_settings = settings or LLMSettings.from_env()
    
    # Inject Pydantic schema for Ollama JSON schema enforcement
    strict_settings = llm_settings.__class__(
        **{
            **llm_settings.__dict__,
            "format_schema": TaskIntent.model_json_schema(),
            "format_mode": "schema",
        }
    )
    
    # Send strict_settings (NOT settings) to enforce schema output in Ollama.
    # Frame the user message as the request line: with temperature 0 the
    # model otherwise echoes whatever JSON it last saw in the prompt.
    response_text = call_llm(
        INTAKE_SYSTEM_PROMPT,
        f"User Request: {request_text}\nJSON Output:",
        settings=strict_settings,
    )
    return parse_task_intent_response(response_text, user_request=request_text)
