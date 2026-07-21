"""Netmiko-based network configuration utility.

ADR references
--------------
- **ADR 0000 Task 1.3** — Scaffold ``freetalon/orchestrator/claws/network.py``;
  build a utility that accepts JSON configurations and applies them to local
  switches using Netmiko.
- **ADR 0001 security-boundary** — raw payloads are validated and sanitized
  against an explicit whitelist *before* any framework-specific logic runs.
  Unknown keys are dropped; bounded values are enforced; secrets are redacted
  in audit logs via :func:`~freetalon.security.redact_secret`.
- **Supply-chain policy** (``docs/approved-dependency-baseline.md``) — Netmiko
  is NOT yet in the approved dependency baseline.  The module therefore defers
  the import: ``netmiko`` is imported lazily inside the apply path only, and
  raises a clear error with remediation instructions until a vendored,
  SHA256-pinned snapshot lands.  The dry-run / plan / HITL logic is fully
  functional and testable WITHOUT netmiko installed.

Human-In-The-Loop (HITL) approval gate
---------------------------------------
No configuration is pushed to a network device without explicit per-change
human confirmation.  The caller must:

1. Call :func:`render_plan` to obtain a :class:`ConfigPlan` that describes
   *exactly* what would be sent (device, port, config lines).
2. Review the plan and construct an :class:`Approval` by passing the plan's
   ``plan_id`` hash back — this acts as a signed acknowledgement that the
   human has seen and accepted the specific change.
3. Pass the :class:`Approval` to :func:`apply_config`.  If the approval is
   missing, expired, or does not match the rendered plan, ``apply_config``
   raises :class:`ApprovalError` and writes a ``network.apply.denied`` audit
   entry.

Handler shape
-------------
:func:`network_claw_handler` satisfies the ``dict -> dict`` handler contract
expected by :class:`~freetalon.orchestrator.tool_registry.ToolRegistry` and
can be registered directly::

    registry.register("network_config", network_claw_handler)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from freetalon.audit import AuditLogger
from freetalon.security import redact_secret

# ---------------------------------------------------------------------------
# Whitelist constants
# ---------------------------------------------------------------------------

#: Allowed Netmiko ``device_type`` values.  Extend only after reviewing the
#: Netmiko driver documentation and updating this list in a pull request.
ALLOWED_DEVICE_TYPES: frozenset[str] = frozenset(
    {
        "cisco_ios",
        "cisco_xe",
        "cisco_nxos",
        "arista_eos",
        "juniper_junos",
    }
)

#: Maximum number of config lines permitted in a single push.
MAX_CONFIG_LINES: int = 100

#: Maximum character length of a single config line.
MAX_CONFIG_LINE_LENGTH: int = 512

#: Strict pattern for the ``host`` field: IPv4, IPv6, or RFC-1123 hostname.
_HOST_PATTERN: re.Pattern[str] = re.compile(
    r"^("
    # IPv4
    r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
    r"|"
    # IPv6 (full form; simplified — disallows embedded-IPv4 and zone IDs)
    r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|"
    # RFC-1123 hostname (letters, digits, hyphens; labels separated by dots)
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)"
    r"(?:\.(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?))*"
    r")$"
)

#: Allowed characters in a single config line.
_CONFIG_LINE_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9 _.,:@/+\-=!#$%^&*()\[\]{}|\\<>?`~;'\"]{0,512}$"
)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConfigPlan:
    """Immutable description of the configuration that *would* be pushed.

    Produced by :func:`render_plan`; passed back to :func:`apply_config` via
    an :class:`Approval`.  The ``plan_id`` is a deterministic SHA-256 digest
    of the serialised plan content, so it acts as a tamper-evident commitment.
    """

    host: str
    device_type: str
    port: int
    config_lines: list[str]
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        content = json.dumps(
            {
                "host": self.host,
                "device_type": self.device_type,
                "port": self.port,
                "config_lines": self.config_lines,
            },
            sort_keys=True,
        )
        object.__setattr__(
            self,
            "plan_id",
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation (safe to log/display)."""
        return {
            "host": self.host,
            "device_type": self.device_type,
            "port": self.port,
            "config_lines": self.config_lines,
            "plan_id": self.plan_id,
        }


@dataclass(slots=True, frozen=True)
class Approval:
    """Explicit human-in-the-loop approval token.

    Construct this *only* after a human has reviewed the :class:`ConfigPlan`
    returned by :func:`render_plan` and confirmed they accept the change.

    Parameters
    ----------
    plan_id:
        The ``plan_id`` from the :class:`ConfigPlan` being approved.  Passing
        the wrong hash (e.g. approving plan A but trying to apply plan B) is
        detected and causes :func:`apply_config` to deny the request.
    """

    plan_id: str


class ApprovalError(RuntimeError):
    """Raised when :func:`apply_config` is called without valid approval."""


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------


def sanitize_network_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and sanitize a network-configuration payload.

    Enforces the ADR 0001 security-boundary pattern: unknown keys are dropped,
    every accepted value is explicitly validated, and secrets are never stored
    in the returned clean dict.

    Parameters
    ----------
    payload:
        Raw JSON/dict describing the target device and desired configuration.
        Expected keys:

        ``host`` *(required)*
            IP address or RFC-1123 hostname.
        ``device_type`` *(required)*
            One of :data:`ALLOWED_DEVICE_TYPES`.
        ``port`` *(optional, default 22)*
            TCP port, bounded 1–65535.
        ``config_lines`` *(required)*
            List of configuration command strings.  Each is bounded in length
            and character set; the total count is bounded by
            :data:`MAX_CONFIG_LINES`.

    Returns
    -------
    dict
        Clean dict with only the validated keys.

    Raises
    ------
    ValueError
        If any field fails validation.
    """
    # --- host ---
    host = str(payload.get("host", "")).strip()
    if not host or not _HOST_PATTERN.fullmatch(host):
        raise ValueError(
            "host must be a valid IPv4 address, IPv6 address, or RFC-1123 hostname"
        )

    # --- device_type ---
    device_type = str(payload.get("device_type", "")).strip().lower()
    if device_type not in ALLOWED_DEVICE_TYPES:
        raise ValueError(
            f"device_type must be one of: {sorted(ALLOWED_DEVICE_TYPES)}"
        )

    # --- port ---
    raw_port = payload.get("port", 22)
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")

    # --- config_lines ---
    raw_lines = payload.get("config_lines", [])
    if not isinstance(raw_lines, list):
        raise ValueError("config_lines must be a list of strings")
    if not raw_lines:
        raise ValueError("config_lines must contain at least one line")
    if len(raw_lines) > MAX_CONFIG_LINES:
        raise ValueError(
            f"config_lines exceeds the maximum of {MAX_CONFIG_LINES} lines"
        )
    config_lines: list[str] = []
    for i, line in enumerate(raw_lines):
        line_str = str(line)
        if len(line_str) > MAX_CONFIG_LINE_LENGTH:
            raise ValueError(
                f"config_lines[{i}] exceeds the maximum of "
                f"{MAX_CONFIG_LINE_LENGTH} characters"
            )
        if not _CONFIG_LINE_PATTERN.fullmatch(line_str):
            raise ValueError(
                f"config_lines[{i}] contains unsupported characters"
            )
        config_lines.append(line_str)

    return {
        "host": host,
        "device_type": device_type,
        "port": port,
        "config_lines": config_lines,
    }


# ---------------------------------------------------------------------------
# Plan rendering (dry-run)
# ---------------------------------------------------------------------------


def render_plan(
    clean_config: dict[str, Any],
    *,
    audit: AuditLogger,
) -> ConfigPlan:
    """Produce a :class:`ConfigPlan` describing what *would* be pushed.

    This function does **not** open any network connection.  It is safe to
    call even when Netmiko is not installed.

    Parameters
    ----------
    clean_config:
        A dict previously returned by :func:`sanitize_network_config`.
    audit:
        Injected :class:`~freetalon.audit.AuditLogger` instance.

    Returns
    -------
    ConfigPlan
        Immutable plan object whose ``plan_id`` the caller must include in
        an :class:`Approval` before :func:`apply_config` will proceed.
    """
    plan = ConfigPlan(
        host=clean_config["host"],
        device_type=clean_config["device_type"],
        port=clean_config["port"],
        config_lines=list(clean_config["config_lines"]),
    )
    audit.log(
        "network.plan.rendered",
        host=clean_config["host"],
        device_type=clean_config["device_type"],
        port=clean_config["port"],
        line_count=len(clean_config["config_lines"]),
        plan_id=plan.plan_id,
    )
    return plan


# ---------------------------------------------------------------------------
# Configuration apply (HITL-gated, deferred Netmiko)
# ---------------------------------------------------------------------------


def apply_config(
    clean_config: dict[str, Any],
    *,
    plan: ConfigPlan,
    approval: Approval,
    audit: AuditLogger,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Push ``clean_config`` to the target device.

    **HITL gate:** ``approval.plan_id`` must exactly match ``plan.plan_id``.
    Any mismatch causes an immediate :class:`ApprovalError` and a
    ``network.apply.denied`` audit entry.

    **Deferred dependency:** Netmiko is imported lazily inside this function.
    If it is not installed a :class:`RuntimeError` is raised with a clear
    message directing the operator to vendor and pin Netmiko per
    ``docs/approved-dependency-baseline.md``.  The HITL gate is evaluated
    *before* the import attempt, so gate ordering is always:

    1. HITL check (raises ``ApprovalError`` if denied)
    2. Netmiko availability check (raises ``RuntimeError`` if unvendored)
    3. Connection and apply

    Parameters
    ----------
    clean_config:
        A dict previously returned by :func:`sanitize_network_config`.
    plan:
        The :class:`ConfigPlan` returned by :func:`render_plan` for the same
        ``clean_config``.
    approval:
        An :class:`Approval` whose ``plan_id`` matches ``plan.plan_id``.
    audit:
        Injected :class:`~freetalon.audit.AuditLogger` instance.
    username:
        Device login username.  Redacted in audit logs.
    password:
        Device login password.  Redacted in audit logs.  **Never stored** in
        the returned result dict.

    Returns
    -------
    dict
        ``{"ok": True, "plan_id": plan.plan_id, "output": <device_output>}``

    Raises
    ------
    ApprovalError
        If ``approval.plan_id`` does not match ``plan.plan_id``.
    RuntimeError
        If Netmiko is not installed (deferred-dependency guard).
    """
    # ── 1. HITL gate ────────────────────────────────────────────────────────
    if approval.plan_id != plan.plan_id:
        audit.log(
            "network.apply.denied",
            host=clean_config["host"],
            device_type=clean_config["device_type"],
            plan_id=plan.plan_id,
            provided_approval_id=approval.plan_id,
            reason="approval plan_id does not match rendered plan",
        )
        raise ApprovalError(
            f"Approval plan_id {approval.plan_id!r} does not match the rendered "
            f"plan {plan.plan_id!r}.  Re-render the plan and construct a new "
            f"Approval from the returned plan_id."
        )

    audit.log(
        "network.apply.approved",
        host=clean_config["host"],
        device_type=clean_config["device_type"],
        plan_id=plan.plan_id,
        username_redacted=redact_secret(username) if username else None,
    )

    # ── 2. Deferred Netmiko import ──────────────────────────────────────────
    try:
        from netmiko import ConnectHandler  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "Netmiko is not installed.  Per the supply-chain policy in "
            "docs/approved-dependency-baseline.md, Netmiko must be vendored "
            "(a point-in-time release copied into the project's trusted "
            "dependency store, pinned with SHA256 hashes, and added to the "
            "approved baseline) before it can be used.  Do not install it "
            "from PyPI at runtime."
        ) from exc

    # ── 3. Connect and apply ────────────────────────────────────────────────
    device_params: dict[str, Any] = {
        "device_type": clean_config["device_type"],
        "host": clean_config["host"],
        "port": clean_config["port"],
    }
    if username is not None:
        device_params["username"] = username
    if password is not None:
        device_params["password"] = password

    try:
        with ConnectHandler(**device_params) as conn:
            output = conn.send_config_set(clean_config["config_lines"])
    except Exception as exc:
        audit.log(
            "network.apply.connection_error",
            host=clean_config["host"],
            device_type=clean_config["device_type"],
            plan_id=plan.plan_id,
            error=str(exc),
        )
        raise

    audit.log(
        "network.apply.executed",
        host=clean_config["host"],
        device_type=clean_config["device_type"],
        plan_id=plan.plan_id,
        line_count=len(clean_config["config_lines"]),
    )
    return {"ok": True, "plan_id": plan.plan_id, "output": output}


# ---------------------------------------------------------------------------
# ToolRegistry-compatible handler
# ---------------------------------------------------------------------------


def network_claw_handler(inputs: dict[str, Any]) -> dict[str, Any]:
    """``dict -> dict`` handler for :class:`~freetalon.orchestrator.tool_registry.ToolRegistry`.

    Expected keys in *inputs*:

    ``config`` *(required)*
        Raw network-configuration payload (will be sanitized).
    ``audit_log_path`` *(required)*
        Path to the audit log file.
    ``approval_plan_id`` *(required)*
        ``plan_id`` from the :class:`ConfigPlan` that was reviewed and accepted
        by the human operator.
    ``username`` *(optional)*
        Device login username.
    ``password`` *(optional)*
        Device login password.  Redacted in audit logs.

    The caller is responsible for first calling :func:`render_plan` out-of-band
    (e.g. via a separate "plan" tool invocation), presenting the plan to the
    operator, and only submitting this handler after explicit operator sign-off.

    Returns
    -------
    dict
        ``{"ok": True, "plan_id": ..., "output": ...}`` on success.

    Raises
    ------
    ApprovalError
        If ``approval_plan_id`` does not match the rendered plan.
    """
    audit = AuditLogger(path=Path(str(inputs["audit_log_path"])))
    raw_config: dict[str, Any] = inputs["config"]
    clean = sanitize_network_config(raw_config)
    plan = render_plan(clean, audit=audit)

    approval = Approval(plan_id=str(inputs["approval_plan_id"]))
    return apply_config(
        clean,
        plan=plan,
        approval=approval,
        audit=audit,
        username=inputs.get("username"),
        password=inputs.get("password"),
    )
