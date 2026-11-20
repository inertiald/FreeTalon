"""File-backed pairing record store (DESIGN.md section 6).

The contract this module implements lives in CONTRACT.md beside it:
status vocabulary, record fields, exception behavior, and the clock
seam are all pinned there. Tests and the HTTP layer answer to the same
contract.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CLAIMED = "claimed"
STATUS_EXPIRED = "expired"

_CODE_RE = re.compile(r"^[A-Z]{2,}-\d{4}$")
_NONCE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_MAX_TEXT = 128
_MAX_BUNDLE_BYTES = 32768
_STORE_VERSION = 1


class PairingError(Exception):
    """Base class for every pairing-store error."""


class CodeNotFound(PairingError):
    """The code has never been registered."""


class CodeConflict(PairingError):
    """The code is already bound to a different nonce or machine-id."""


class CodeExpired(PairingError):
    """The code expired before the attempted operation."""


class CodeAlreadyClaimed(PairingError):
    """The code's bundle was already claimed (burned)."""


class CodeAlreadyConfirmed(PairingError):
    """The code already carries a credential bundle."""


class InvalidRegistration(PairingError, ValueError):
    """Registration input failed validation."""


class InvalidBundle(PairingError, ValueError):
    """A credential bundle failed validation."""


class StoreCorruptError(PairingError):
    """The store file exists but cannot be read. Never silently reset:
    a reset would resurrect burned codes."""


@dataclass(frozen=True)
class PollOutcome:
    """Result of a poll. ``credentials`` is non-None exactly once in a
    code's life: on the poll that claims a confirmed bundle."""

    status: str
    credentials: dict[str, Any] | None


def _validate_text(value: Any, field: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise InvalidRegistration(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise InvalidRegistration(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise InvalidRegistration(f"{field} must not be empty")
    if len(text) > _MAX_TEXT:
        raise InvalidRegistration(f"{field} exceeds {_MAX_TEXT} characters")
    if any(ord(c) < 0x20 for c in text):
        raise InvalidRegistration(f"{field} contains control characters")
    return text


def _public_view(record: dict[str, Any]) -> dict[str, Any]:
    """The only shape that ever leaves the store about a record:
    never the nonce, never the credentials."""
    return {
        "code": record["code"],
        "status": record["status"],
        "machine_id": record["machine_id"],
        "hostname": record["hostname"],
        "mac": record["mac"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "confirmed_at": record["confirmed_at"],
        "claimed_at": record["claimed_at"],
    }


class PairingStore:
    """Pairing records in one JSON file, written atomically.

    ``now_fn`` is the store's only clock (the contract's clock seam):
    every time read goes through it, so tests inject a fake clock
    instead of patching ``time.time``.
    """

    def __init__(
        self,
        path: str | Path,
        ttl_seconds: float = 1800.0,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        ttl = float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._path = Path(path)
        self._ttl = ttl
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreCorruptError(
                f"pairing store at {self._path} is unreadable: {exc}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("records"), dict):
            raise StoreCorruptError(
                f"pairing store at {self._path} has an unexpected shape"
            )
        return raw["records"]

    def _save(self) -> None:
        payload = {"version": _STORE_VERSION, "records": self._records}
        text = json.dumps(payload, indent=2, sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._path)
        # Make the rename itself durable.
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # -- internals -----------------------------------------------------

    def _now(self) -> float:
        return float(self._now_fn())

    def _refresh(self, record: dict[str, Any]) -> bool:
        """Materialize expiry. Returns True if the record changed."""
        if record["status"] in (STATUS_PENDING, STATUS_CONFIRMED):
            if self._now() >= float(record["expires_at"]):
                record["status"] = STATUS_EXPIRED
                record["credentials"] = None
                return True
        return False

    def _get(self, code: str) -> dict[str, Any]:
        record = self._records.get(code)
        if record is None:
            raise CodeNotFound(f"unknown pairing code: {code}")
        return record

    # -- public API ----------------------------------------------------

    def register(
        self,
        code: Any,
        nonce: Any,
        machine_id: Any,
        hostname: Any = None,
        mac: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
            raise InvalidRegistration(
                "code must look like WORD-NNNN (uppercase word, dash, 4 digits)"
            )
        if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
            raise InvalidRegistration("nonce must be 32 hex characters")
        nonce = nonce.lower()
        machine = _validate_text(machine_id, "machine_id", required=True)
        host = _validate_text(hostname, "hostname", required=False)
        mac_addr = _validate_text(mac, "mac", required=False)
        with self._lock:
            existing = self._records.get(code)
            if existing is not None:
                changed = self._refresh(existing)
                if (
                    existing["nonce"] != nonce
                    or existing["machine_id"] != machine
                ):
                    raise CodeConflict(
                        f"code {code} is bound to a different node identity"
                    )
                if existing["status"] == STATUS_EXPIRED:
                    # Same node, same code, new window: fresh pending
                    # record (CONTRACT.md, Expiry).
                    existing.update(
                        status=STATUS_PENDING,
                        credentials=None,
                        hostname=host,
                        mac=mac_addr,
                        created_at=self._now(),
                        expires_at=self._now() + self._ttl,
                        confirmed_at=None,
                        claimed_at=None,
                    )
                    changed = True
                if changed:
                    self._save()
                return _public_view(existing)
            now = self._now()
            record = {
                "code": code,
                "nonce": nonce,
                "machine_id": machine,
                "hostname": host,
                "mac": mac_addr,
                "status": STATUS_PENDING,
                "credentials": None,
                "created_at": now,
                "expires_at": now + self._ttl,
                "confirmed_at": None,
                "claimed_at": None,
            }
            self._records[code] = record
            self._save()
            return _public_view(record)

    def confirm(self, code: str, credentials: Any) -> dict[str, Any]:
        if not isinstance(credentials, dict) or not credentials:
            raise InvalidBundle("credentials must be a non-empty JSON object")
        try:
            blob = json.dumps(credentials)
        except (TypeError, ValueError) as exc:
            raise InvalidBundle(f"credentials are not JSON-serializable: {exc}")
        if len(blob.encode("utf-8")) > _MAX_BUNDLE_BYTES:
            raise InvalidBundle(
                f"credentials exceed {_MAX_BUNDLE_BYTES} bytes serialized"
            )
        with self._lock:
            record = self._get(code)
            if self._refresh(record):
                self._save()
            status = record["status"]
            if status == STATUS_EXPIRED:
                raise CodeExpired(f"pairing code {code} has expired")
            if status == STATUS_CLAIMED:
                raise CodeAlreadyClaimed(f"pairing code {code} was already claimed")
            if status == STATUS_CONFIRMED:
                raise CodeAlreadyConfirmed(
                    f"pairing code {code} is already confirmed"
                )
            record["credentials"] = json.loads(blob)  # detach from caller
            record["status"] = STATUS_CONFIRMED
            record["confirmed_at"] = self._now()
            self._save()
            return _public_view(record)

    def poll(self, code: str) -> PollOutcome:
        with self._lock:
            record = self._get(code)
            changed = self._refresh(record)
            status = record["status"]
            if status == STATUS_CONFIRMED:
                bundle = record["credentials"]
                record["credentials"] = None
                record["status"] = STATUS_CLAIMED
                record["claimed_at"] = self._now()
                self._save()
                return PollOutcome(status=STATUS_CONFIRMED, credentials=bundle)
            if changed:
                self._save()
            return PollOutcome(status=status, credentials=None)

    def list_records(self) -> list[dict[str, Any]]:
        with self._lock:
            changed = False
            for record in self._records.values():
                changed = self._refresh(record) or changed
            if changed:
                self._save()
            return [
                _public_view(self._records[code])
                for code in sorted(self._records)
            ]
