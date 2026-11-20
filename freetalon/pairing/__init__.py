"""M6 pairing-join server (DESIGN.md section 6).

The module contract — status vocabulary, record fields, exception
behavior, HTTP routes — is pinned in CONTRACT.md in this package.
"""

from .store import (
    STATUS_CLAIMED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    CodeAlreadyClaimed,
    CodeAlreadyConfirmed,
    CodeConflict,
    CodeExpired,
    CodeNotFound,
    InvalidBundle,
    InvalidRegistration,
    PairingError,
    PairingStore,
    PollOutcome,
    StoreCorruptError,
)

__all__ = [
    "STATUS_CLAIMED",
    "STATUS_CONFIRMED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "CodeAlreadyClaimed",
    "CodeAlreadyConfirmed",
    "CodeConflict",
    "CodeExpired",
    "CodeNotFound",
    "InvalidBundle",
    "InvalidRegistration",
    "PairingError",
    "PairingStore",
    "PollOutcome",
    "StoreCorruptError",
]
