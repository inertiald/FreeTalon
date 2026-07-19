# FreeTalon

A local openclaw hive, security hardened and hardware optimized.

## 5-minute local UI

Golden path on a fresh Ubuntu-like machine with Python 3 and Docker available:

```bash
python3 installer.py --yes
python3 dashboard.py
```

Then open:

```text
http://127.0.0.1:7860
```

One-command variant:

```bash
python3 installer.py --yes --start-ui
```

What the installer does:
- creates or reuses `./.venv`
- installs pinned Python dependencies into that venv
- generates `.env` and `docker-compose.yml`
- detects Docker/GPU support and falls back to CPU-safe compose defaults when needed
- optionally prepares local Playwright + Chromium with `--enable-browser`

`python3 dashboard.py` is the supported UI start command after install. It will reuse the project venv automatically, so you do not need to remember `source .venv/bin/activate`.

## Setup modes

| Mode | Command | Use for |
|---|---|---|
| Local UI | `python3 installer.py --yes --mode ui` | NiceGUI dashboard on localhost |
| Local API/CLI | `python3 installer.py --yes --mode api` | `python -m freetalon.cli ...` workflows |
| Docker only | `python3 installer.py --yes --mode docker` | Compose and GPU/runtime validation only |
| Full | `python3 installer.py --yes --mode full` | UI + API + Docker defaults |

## Docker / GPU notes

- NVIDIA hosts use `runtime: nvidia` in generated compose instead of CDI-only reservations.
- If an NVIDIA GPU is present but Docker lacks the `nvidia` runtime, the installer warns early and generates CPU-safe compose output instead of leaving `docker compose up -d` to fail later.
- AMD hosts keep the ROCm path when `/dev/kfd` and `/dev/dri` are available; otherwise the installer falls back to CPU mode.
- If Docker or the Docker Compose plugin is missing, the local UI/API path still works.

## Optional local browser automation

To prepare host-side Playwright for `claw_browser.py`:

```bash
python3 installer.py --yes --enable-browser
```

That installs the pinned Playwright Python package and Chromium browser binaries automatically.

To also build the local Docker images used by claw profiles:

```bash
python3 installer.py --yes --enable-browser --build-images
```

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

## Quickstart (local API/CLI)

1. Run the installer:
   ```bash
   python3 installer.py --yes --mode api
   ```
2. Set API token (required):
   ```bash
   export FREETALON_API_TOKEN='change-me-local-token'
   ```
3. Start hive daemon:
   ```bash
   ./.venv/bin/python -m freetalon.cli start
   ```
4. Health + status:
   ```bash
   ./.venv/bin/python -m freetalon.cli health
   ./.venv/bin/python -m freetalon.cli status --token "$FREETALON_API_TOKEN"
   ```
5. Submit task:
   ```bash
   ./.venv/bin/python -m freetalon.cli submit --token "$FREETALON_API_TOKEN" --action echo --text "hello hive"
   ./.venv/bin/python -m freetalon.cli submit --token "$FREETALON_API_TOKEN" --action docker_claw --code "print('hello from docker claw')"
   ```
6. Stop daemon:
   ```bash
   ./.venv/bin/python -m freetalon.cli stop
   ```

## Docker quickstart

After `python3 installer.py --yes --mode docker` or `--mode full`:

```bash
docker compose up -d
docker compose --profile browser up -d
```

Use the browser profile only after preparing the local browser image path (`python3 installer.py --yes --enable-browser --build-images`) or when your workflow specifically needs browser automation.

## Why the installer-first flow is the supported default

- `dashboard.py` depends on NiceGUI, so the installer provisions the venv and dependencies first.
- Docker/browser features have host-specific prerequisites, so the installer validates them up front and prints deterministic next steps.
- The CLI/API workflow remains available and unchanged once the environment is prepared.

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

## Roadmap — distributed and parallel compute

Detailed design in [ADR 0002](docs/adr/0002-distributed-parallel-compute.md).

| Capability | Status |
|---|---|
| Ring topology (ring-allreduce, peer-to-peer task flow) | 📋 Planned |
| Star topology (coordinator + worker fan-out) | 📋 Planned |
| RDMA transport (InfiniBand / RoCE v2 / iWARP, opt-in) | 📋 Planned |
| Pipeline parallelism (`pipeline_parallel_size` parameter) | 📋 Planned |
| Tensor parallelism (`tensor_parallel_size` parameter) | 📋 Planned |
| vLLM inference engine (vendored, SHA256-pinned, quarterly CVE audit) | 📋 Planned |
| DeepSpeed training engine (vendored, SHA256-pinned, quarterly CVE audit) | 📋 Planned |
| NCCL collective backend (system or vendored, version-documented) | 📋 Planned |
| Pydantic config schema for all parallelism/topology knobs | 📋 Planned |
| Extended `health` endpoint covering GPU/NCCL worker status | 📋 Planned |

Supply-chain policy for all new frameworks: same as existing OpenClaw-parity model — copy a
point-in-time release, pin every dependency with a SHA256 hash, add to
`docs/approved-dependency-baseline.md`, and gate updates behind a PR with an advisory-database clear.
