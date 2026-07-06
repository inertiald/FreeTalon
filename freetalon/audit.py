from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import local as _threadlocal
from typing import Any

_tls = _threadlocal()


def set_request_id(request_id: str) -> None:
    """Bind a request ID to the current thread so audit events inherit it."""
    _tls.request_id = request_id


def clear_request_id() -> None:
    _tls.request_id = None


def current_request_id() -> str | None:
    return getattr(_tls, "request_id", None)


@dataclass(slots=True)
class AuditLogger:
    path: Path

    def log(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event": event,
        }
        rid = current_request_id()
        if rid:
            entry["request_id"] = rid
        entry.update(fields)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
