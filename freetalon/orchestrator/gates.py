"""Approval gates (M4): human decisions on irreversible actions.

A node that would do something irreversible (``PlanNode.reversible is
False`` — invest real money, flash firmware, delete data) never runs on
its own. The loop builds a *gate packet* — what, why, risk, cost,
reversibility, and the evidence behind it — and pauses. A human approves
or denies the packet in the FreeTalon interface (dashboard or API).

Rules, from the design doc:
- Approvals are per-action and bound to the packet hash: if the action
  or its inputs change, the old approval is void.
- Packets expire (deny on timeout): an undecided packet past its
  expiry is treated exactly like a denial.
- A denial is recorded and never re-asked in the same run.
- Every transition is written to the gates table (the audit trail) and
  to the run's gates/ directory.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_GATE_TTL_S = 3600  # 1 hour; expired packets deny on timeout

_DEFAULT_DB_PATH = Path.home() / "freetalon-workspace" / "freetalon_state.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gates (
    packet_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    action TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gates_run ON gates(run_id);
CREATE INDEX IF NOT EXISTS idx_gates_status ON gates(status);
"""


class GateStatus(str, Enum):
    """Lifecycle of one gate packet."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"  # deny on timeout


class GatePacket(BaseModel):
    """One approval request: the exact action, its evidence, its decision."""

    model_config = ConfigDict(extra="forbid")

    packet_id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    plan_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    action: str = Field(..., min_length=1, description="Machine action name, e.g. flash_ecu")
    what: str = Field(..., min_length=1, description="The exact action, in plain language")
    why: str = Field(default="", description="Why this action is the next step")
    risk: str = Field(default="", description="What could go wrong")
    cost: float = Field(default=0.0, ge=0.0, description="Money at stake, 0.0 unless stated")
    reversible: bool = Field(default=False, description="Can the action be undone?")
    evidence: list[str] = Field(
        default_factory=list,
        description="Artifact paths / measurement refs supporting the request",
    )
    packet_hash: str = Field(..., min_length=1, description="sha256 binding the approval to this exact action")
    status: GateStatus = Field(default=GateStatus.PENDING)
    created_at: str = Field(default="")
    expires_at: str = Field(default="")
    decided_at: str | None = Field(default=None)
    decided_by: str | None = Field(default=None)
    decision_note: str | None = Field(default=None)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def compute_packet_hash(action: str, node_id: str, inputs: dict[str, Any]) -> str:
    """Hash binding an approval to one exact action.

    Any change to the action, the node, or its inputs changes the hash,
    so an approval for a different action cannot be reused. Evidence is
    deliberately NOT part of the hash: it is supporting material for the
    human, and it grows as the run progresses — including it would void
    an approval the moment a new artifact appeared.
    """
    canonical = json.dumps(
        {
            "action": action,
            "node_id": node_id,
            "inputs": inputs,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_packet(
    run_id: str,
    plan_id: str,
    node_id: str,
    action: str,
    what: str,
    why: str,
    risk: str,
    cost: float,
    reversible: bool,
    evidence: list[str],
    inputs: dict[str, Any],
    ttl_s: int = DEFAULT_GATE_TTL_S,
) -> GatePacket:
    """Build a new pending gate packet (not yet stored)."""
    now = datetime.now(UTC)
    return GatePacket(
        packet_id=f"gate-{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        plan_id=plan_id,
        node_id=node_id,
        action=action,
        what=what,
        why=why,
        risk=risk,
        cost=cost,
        reversible=reversible,
        evidence=list(evidence),
        packet_hash=compute_packet_hash(action, node_id, inputs),
        status=GateStatus.PENDING,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
    )


class GateStore:
    """SQLite store for gate packets (same file as the plan store)."""

    def __init__(
        self, db_path: Path | str | None = None, audit_logger: Any | None = None
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit = audit_logger
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.executescript(_CREATE_TABLE_SQL)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, packet: GatePacket) -> GatePacket:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO gates (packet_id, run_id, plan_id, node_id, action,"
                " packet_json, status, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    packet.packet_id,
                    packet.run_id,
                    packet.plan_id,
                    packet.node_id,
                    packet.action,
                    packet.model_dump_json(),
                    packet.status.value,
                    packet.created_at,
                    packet.expires_at,
                ),
            )
            conn.commit()
        if self._audit is not None:
            self._audit.log(
                "gate.created",
                packet_id=packet.packet_id,
                run_id=packet.run_id,
                node_id=packet.node_id,
                action=packet.action,
                packet_hash=packet.packet_hash[:12],
            )
        return packet

    def get(self, packet_id: str) -> GatePacket | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT packet_json FROM gates WHERE packet_id = ?", (packet_id,)
            ).fetchone()
        if row is None:
            return None
        return GatePacket.model_validate_json(row["packet_json"])

    def pending(self, run_id: str | None = None) -> list[GatePacket]:
        """Pending packets, optionally for one run (expiry swept first)."""
        self.expire_overdue()
        query = "SELECT packet_json FROM gates WHERE status = 'pending'"
        params: list[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [GatePacket.model_validate_json(r["packet_json"]) for r in rows]

    def all(self) -> list[GatePacket]:
        """All packets, newest first (expiry swept first)."""
        self.expire_overdue()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT packet_json FROM gates ORDER BY created_at DESC"
            ).fetchall()
        return [GatePacket.model_validate_json(r["packet_json"]) for r in rows]

    def list_for_run(self, run_id: str) -> list[GatePacket]:
        self.expire_overdue()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT packet_json FROM gates WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [GatePacket.model_validate_json(r["packet_json"]) for r in rows]

    def denied_hashes_for_run(self, run_id: str) -> set[str]:
        """Packet hashes denied or expired in this run — never re-ask these."""
        self.expire_overdue()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT packet_json FROM gates WHERE run_id = ?"
                " AND status IN ('denied', 'expired')",
                (run_id,),
            ).fetchall()
        return {
            GatePacket.model_validate_json(r["packet_json"]).packet_hash for r in rows
        }

    def decide(
        self,
        packet_id: str,
        decision: str,
        decided_by: str,
        note: str | None = None,
    ) -> GatePacket:
        """Approve or deny a pending packet. Raises ValueError otherwise."""
        if decision not in ("approve", "deny"):
            raise ValueError(f"decision must be 'approve' or 'deny', got {decision!r}")
        self.expire_overdue()
        packet = self.get(packet_id)
        if packet is None:
            raise ValueError(f"no gate packet {packet_id!r}")
        if packet.status != GateStatus.PENDING:
            raise ValueError(
                f"gate packet {packet_id!r} is already {packet.status.value}"
            )
        packet.status = GateStatus.APPROVED if decision == "approve" else GateStatus.DENIED
        packet.decided_at = _now_iso()
        packet.decided_by = decided_by
        packet.decision_note = note
        with self._connect() as conn:
            conn.execute(
                "UPDATE gates SET packet_json = ?, status = ? WHERE packet_id = ?",
                (packet.model_dump_json(), packet.status.value, packet.packet_id),
            )
            conn.commit()
        if self._audit is not None:
            self._audit.log(
                f"gate.{packet.status.value}",
                packet_id=packet.packet_id,
                run_id=packet.run_id,
                node_id=packet.node_id,
                decided_by=decided_by,
            )
        return packet

    def expire_overdue(self, now: datetime | None = None) -> list[GatePacket]:
        """Mark pending packets past expiry as EXPIRED (deny on timeout)."""
        now = now or datetime.now(UTC)
        expired: list[GatePacket] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT packet_json FROM gates WHERE status = 'pending'"
            ).fetchall()
            for row in rows:
                packet = GatePacket.model_validate_json(row["packet_json"])
                if datetime.fromisoformat(packet.expires_at) <= now:
                    packet.status = GateStatus.EXPIRED
                    packet.decided_at = now.isoformat()
                    packet.decision_note = "expired without a decision (deny on timeout)"
                    conn.execute(
                        "UPDATE gates SET packet_json = ?, status = 'expired'"
                        " WHERE packet_id = ?",
                        (packet.model_dump_json(), packet.packet_id),
                    )
                    expired.append(packet)
            conn.commit()
        for packet in expired:
            if self._audit is not None:
                self._audit.log(
                    "gate.expired",
                    packet_id=packet.packet_id,
                    run_id=packet.run_id,
                    node_id=packet.node_id,
                )
        return expired
