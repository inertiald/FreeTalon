# FreeTalon

A local openclaw hive, security hardened and hardware optimized.

## Current state vs vision (gap analysis)

### What previously existed
- Docker-oriented claw orchestration primitives (`orchestrator.py`, `resource_manager.py`, `claw_browser.py`)
- NiceGUI dashboard shell (`dashboard.py`)
- Installer scaffolding (`installer.py`)

### What was missing for **local openclaw hive**
- No cohesive local hive runtime with queue/scheduler semantics
- No task retries/backoff/cancellation lifecycle model
- No worker heartbeat/liveness model and status API for a full hive loop
- No single CLI path for start/stop/status/submit workflows

### What was missing for **security hardened**
- No strict central config validation
- No authenticated API boundary for task operations
- No standardized payload sanitization boundary for task submission
- No structured security/audit event stream with secret redaction

### What was missing for **hardware optimized**
- No explicit host capability model connected to adaptive scheduler sizing
- No bounded queue tied to computed runtime capacity
- No benchmark to show tuning effect
- No documented optimization knobs tied to runtime behavior

## Implemented architecture (MVP-complete local hive)

```text
CLI (freetalon.cli)
  ├─ start/stop/status/health/submit/cancel
  └─ local authenticated HTTP API
       ├─ HiveController (scheduler + worker pool)
       │    ├─ retries + backoff + cancellation
       │    ├─ heartbeat/liveness tracking
       │    └─ persisted task state
       ├─ Security boundary (auth + payload sanitization)
       └─ Audit logging (structured JSON events)
```

## Quickstart (local)

1. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```
2. Set API token (required):
   ```bash
   export FREETALON_API_TOKEN='change-me-local-token'
   ```
3. Start hive daemon:
   ```bash
   python -m freetalon.cli start
   ```
4. Health + status:
   ```bash
   python -m freetalon.cli health
   python -m freetalon.cli status --token "$FREETALON_API_TOKEN"
   ```
5. Submit task:
   ```bash
   python -m freetalon.cli submit --token "$FREETALON_API_TOKEN" --action echo --text "hello hive"
   ```
6. Stop daemon:
   ```bash
   python -m freetalon.cli stop
   ```

## Trusted dependency baseline

- Python runtime dependencies are pinned to exact versions with SHA256 hashes in `requirements.txt`.
- External container images are pinned by immutable digest in `docker-compose.yml` and Dockerfiles.
- Local images use explicit version tags: `trusted-python-base:1.0.0` and `freetalon-claw-browser:1.0.0`.
- Policy and allowlisted sources are documented in `docs/approved-dependency-baseline.md`.
- Automated policy check:
  ```bash
  python scripts/check_trusted_dependencies.py
  ```

## Security hardening controls

- Strict config schema validation (`freetalon/config.py`, Pydantic)
- Deny-by-default auth on status/metrics/task APIs (shared token required)
- Input validation/sanitization for all task payloads (`freetalon/security.py`)
- Secret loading from env/secret file with explicit token redaction in logs
- Structured audit log (`audit.log`) for start/stop/auth failures/task actions

## Hardware optimization controls

- Host CPU/memory/GPU capability detection (`freetalon/hardware.py`)
- Adaptive worker-pool and bounded queue sizing from host capacity
- Optional acceleration fast-path tagging when libs (cupy/torch/numba) are present
- Runtime knobs: `worker_cap`, `queue_multiplier`, `poll_interval_seconds`
- Benchmark script:
  ```bash
  python scripts/benchmark_hive.py
  ```

## Capability matrix (before → after)

| Capability | Before | After |
|---|---:|---:|
| Local runnable hive loop | Partial | ✅ |
| Multi-worker scheduler | ❌ | ✅ |
| Retries/backoff/cancel/status | ❌ | ✅ |
| Heartbeat/liveness | ❌ | ✅ |
| Strict config validation | ❌ | ✅ |
| Authenticated task API | ❌ | ✅ |
| Payload sanitization | Partial | ✅ |
| Structured audit logging | ❌ | ✅ |
| Adaptive host-aware tuning | Partial | ✅ |
| Benchmark for tuning effect | ❌ | ✅ |
| Deterministic tests | ❌ | ✅ |

## Hardening checklist

- [x] Bind API to localhost by default
- [x] Require token for sensitive endpoints
- [x] Validate all task payload fields
- [x] Bound queue and worker concurrency
- [x] Redact token in logs
- [x] Persist and recover task state

## Known limitations / next steps

- Single-node local deployment only (no distributed cluster federation yet)
- Token auth is single-shared-secret (can evolve to mTLS or multi-role local ACL)
- Retry policy is exponential-like linear backoff (`backoff * attempt`), can be extended with jitter
- Existing Docker-focused modules remain available but are not yet integrated with the new local API runtime
