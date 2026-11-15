"""JSON fallback encoder for FreeTalon task state, audit and API output.

Single place for "how do we serialise a value json can't handle".
Kept stdlib-only and dependency-free so hive.py, audit.py and api.py
can all import it without import cycles.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def json_default(obj: Any) -> Any:
    """Convert one non-JSON value to something ``json`` can encode.

    Deliberately lossy in the safe direction: a Path becomes its string,
    a datetime its ISO form, a set a sorted list, bytes decoded text,
    a dataclass its dict form, an Enum its value -- and anything truly
    unknown becomes ``str(obj)`` rather than raising ``TypeError``.
    State persistence, audit logging and API responses must never crash
    because a task result held an unexpected type.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, Enum):
        return obj.value
    return str(obj)


def dumps(obj: Any, **kwargs: Any) -> str:
    """``json.dumps`` with the FreeTalon fallback wired in."""
    kwargs.setdefault("default", json_default)
    return json.dumps(obj, **kwargs)


def dump(obj: Any, fh: Any, **kwargs: Any) -> None:
    """``json.dump`` with the FreeTalon fallback wired in."""
    kwargs.setdefault("default", json_default)
    json.dump(obj, fh, **kwargs)
