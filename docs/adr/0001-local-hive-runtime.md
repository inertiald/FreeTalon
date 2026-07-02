# ADR 0001: Local Hive Runtime and Security Boundary

## Status
Accepted

## Context
The repository vision requires a local, security-hardened, hardware-optimized hive runtime. Existing code had useful building blocks but no cohesive control-plane and lifecycle for local task execution.

## Decision
Implement a Python-native local hive runtime with:
- `HiveController` scheduler + worker pool + persisted task state
- strict validated config via Pydantic
- authenticated localhost API and CLI-driven operability
- host capability detection to adapt worker/queue sizing
- structured audit logging for security-relevant events

## Tradeoffs
- Chose a shared token (simple local operations) over heavier auth infrastructure to keep local deployment practical.
- Chose in-process thread workers over external brokers to remain self-hostable and dependency-light.
- Kept existing Docker-oriented modules intact to avoid breaking existing workflows while adding a coherent local MVP.

## Consequences
- New contributors can run the hive locally with one token and CLI commands.
- Security and optimization claims are now evidenced by code + docs + tests.
- Future work can add distributed/federated worker backends while preserving the same API semantics.
