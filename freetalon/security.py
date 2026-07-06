from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

SAFE_TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9 _.,:@/+-]{0,256}$")
_DOCKER_PROFILES = frozenset({"default", "video", "youtube_upload"})


def load_secret(path: str | None, env_var: str) -> str:
    env_token = os.environ.get(env_var, "").strip()
    if env_token:
        return env_token
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
            if token:
                return token
    raise ValueError(
        f"Missing secret token. Set {env_var} or provide a non-empty secret file."
    )


def authorize(bearer_token: str | None, configured_token: str) -> bool:
    if not bearer_token:
        return False
    return hmac.compare_digest(bearer_token, configured_token)


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", "")).strip().lower()
    if action not in {"echo", "sum", "sleep", "docker_claw"}:
        raise ValueError("Unsupported action; allowed: echo, sum, sleep, docker_claw")

    retries = int(payload.get("retries", 0))
    if retries < 0 or retries > 5:
        raise ValueError("retries must be between 0 and 5")

    backoff_seconds = float(payload.get("backoff_seconds", 0.2))
    if backoff_seconds < 0 or backoff_seconds > 60:
        raise ValueError("backoff_seconds must be between 0 and 60")

    requires_gpu = bool(payload.get("requires_gpu", False))

    clean: dict[str, Any] = {
        "action": action,
        "retries": retries,
        "backoff_seconds": backoff_seconds,
        "requires_gpu": requires_gpu,
    }

    if action == "echo":
        text = str(payload.get("text", "")).strip()
        if not text or not SAFE_TEXT_PATTERN.fullmatch(text):
            raise ValueError("echo text contains unsupported characters or is empty")
        clean["text"] = text
    elif action == "sum":
        values = payload.get("values", [])
        if not isinstance(values, list) or not values:
            raise ValueError("sum requires a non-empty values list")
        parsed = [float(v) for v in values]
        if len(parsed) > 1000:
            raise ValueError("values list too large")
        clean["values"] = parsed
    elif action == "sleep":
        duration = float(payload.get("duration", 0))
        if duration < 0 or duration > 30:
            raise ValueError("sleep duration must be between 0 and 30 seconds")
        clean["duration"] = duration

    elif action == "docker_claw":
        code = str(payload.get("code", "")).strip()
        if not code:
            raise ValueError("docker_claw requires non-empty code")
        if len(code) > 4096:
            raise ValueError("code exceeds 4096 character limit")
        if not all(c.isprintable() or c in {"\n", "\t", "\r"} for c in code):
            raise ValueError("code contains invalid characters")
        profile = str(payload.get("profile", "default")).strip().lower()
        if profile not in _DOCKER_PROFILES:
            raise ValueError(f"profile must be one of: {sorted(_DOCKER_PROFILES)}")
        timeout = float(payload.get("timeout", 30.0))
        if timeout < 1 or timeout > 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        clean["code"] = code
        clean["profile"] = profile
        clean["timeout"] = timeout

    return clean


def redact_secret(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"redacted:{digest}"
