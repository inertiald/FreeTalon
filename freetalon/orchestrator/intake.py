"""LLM-backed intake pipeline for structured TaskIntent generation."""

from __future__ import annotations

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
_OLLAMA_MODEL = "llama3.1"
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

        if backend == "ollama":
            return cls(
                backend="ollama",
                base_url=os.environ.get("FREETALON_OLLAMA_BASE_URL", _OLLAMA_BASE_URL).strip(),
                model=os.environ.get("FREETALON_OLLAMA_MODEL", _OLLAMA_MODEL).strip(),
                timeout_seconds=timeout_seconds,
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
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="backslashreplace")
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


def parse_task_intent_response(raw_text: str) -> TaskIntent:
    """Validate raw model output as a TaskIntent."""
    payload = parse_json_object(raw_text)
    try:
        return TaskIntent.model_validate(payload)
    except ValidationError as exc:
        raise LLMResponseError(f"LLM response did not match TaskIntent schema: {exc}") from exc


def _call_ollama(settings: LLMSettings, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": settings.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0},
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
        "temperature": 0,
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
    response_text = call_llm(INTAKE_SYSTEM_PROMPT, request_text, settings=settings)
    return parse_task_intent_response(response_text)
