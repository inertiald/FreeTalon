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
    # prompts do not fit in 4096 (retry #22).
    num_ctx: int | None = None
    format_schema: dict[str, Any] | None = None

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

        if backend == "ollama":
            return cls(
                backend="ollama",
                base_url=os.environ.get("FREETALON_OLLAMA_BASE_URL", _OLLAMA_BASE_URL).strip(),
                model=os.environ.get("FREETALON_OLLAMA_MODEL", _OLLAMA_MODEL).strip(),
                timeout_seconds=timeout_seconds,
                temperature=temperature,
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


def _call_ollama(settings: LLMSettings, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": settings.model,
        "stream": False,
        "format": settings.format_schema if settings.format_schema else "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": settings.temperature,
        	"num_ctx": settings.num_ctx if settings.num_ctx else 4096},
    }
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
        **{**llm_settings.__dict__, "format_schema": TaskIntent.model_json_schema()}
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
