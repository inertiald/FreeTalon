# ADR 0002: Distributed and Parallel Compute — Topology, Interconnect, and Framework Strategy

## Status
Proposed

## Context

FreeTalon is a local-first project designed to support millions of downloads (not millions of concurrent users).
The hardened single-node hive runtime (ADR 0001) provides the secure, validated execution kernel.
The next capability tier extends that kernel across multiple local nodes (e.g. a home cluster, a lab rack,
or an air-gapped enterprise workstation pool) and unlocks the compute primitives required for large-model
inference and training workloads.

The same supply-chain philosophy that governs the single-node build applies here without exception:
- Dependencies are copied, audited, and pinned — not fetched live.
- External frameworks (vLLM, DeepSpeed, NCCL) are treated identically to how OpenClaw dependencies are
  handled: vendor a point-in-time snapshot, run a trusted-dependency audit, track CVEs with an enterprise
  update cadence, and document every allowed source in `docs/approved-dependency-baseline.md`.
- Floating references and unreviewed transitive pulls are disallowed.

## Decision

### 1. Node topology support — ring and star

Two logical topologies will be supported:

**Ring topology** — nodes form a directed cycle; tasks and gradient updates flow peer-to-peer around the
ring.  Well-suited for all-reduce (ring-allreduce) patterns where each node communicates with exactly two
neighbors.  Minimises long-range traffic and balances bandwidth across the ring.

**Star topology** — a central coordinator node fans work out to worker nodes and aggregates results.
Suitable for parameter-server gradient aggregation, coarse-grained task dispatch, and deployments where a
dedicated host node is available.  Easier to reason about for operator-level debugging and monitoring.

Both topologies are expressed as named configuration profiles in the hive config schema so the operator
selects them at start-time without code changes.  The HiveController scheduler will route tasks
differently based on the active topology profile.

### 2. Interconnect — RDMA

Remote Direct Memory Access (RDMA) will be supported as an optional transport layer between hive nodes
when the hardware is present (InfiniBand, RoCE v2, iWARP).  RDMA bypasses the OS kernel for
memory-to-memory transfers, dramatically reducing latency and CPU overhead for large tensor exchanges.

Integration points:
- Detect RDMA capability during `freetalon/hardware.py` host profiling (check for `ibstat`, `rxe` kernel
  module, or `rdma` CLI tool).
- Expose a `transport` config knob (`tcp` | `rdma`).  `tcp` is the safe default; `rdma` is opt-in.
- When RDMA is active, tensor data moves via an RDMA-capable backend (e.g. `libibverbs` Python bindings
  or the NCCL RDMA plugin) rather than the TCP socket path.
- RDMA credentials and queue-pair setup are logged in the structured audit stream.

### 3. Parallelism strategies

#### 3a. Pipeline parallelism
Model layers are partitioned across nodes (or local GPUs) in a pipeline.  Each stage processes a
micro-batch and forwards activations to the next stage while simultaneously receiving the next micro-batch.
FreeTalon will expose a `pipeline_stages` parameter in the task payload schema.  The scheduler maps each
stage to a worker that owns the relevant layer shard.

#### 3b. Tensor parallelism
Individual weight matrices are sharded column- or row-wise across devices.  Each device holds a
partition of the tensor and participates in collective operations (all-reduce / all-gather) to produce
correct outputs.  FreeTalon will expose a `tensor_parallel_size` parameter.  When set to N > 1, the
scheduler requires N worker slots with matching device capability before dispatching the task.

Both strategies can be combined (3D parallelism: data × pipeline × tensor), following the pattern
established by Megatron-LM and DeepSpeed.

### 4. vLLM — vendored, audited, enterprise update cadence

vLLM is adopted as the primary high-throughput LLM inference engine for local and air-gapped deployments.

Supply-chain policy (identical to OpenClaw dependency model):
- A point-in-time release of vLLM is copied into the project's trusted dependency store.
- The full dependency tree is resolved at copy time and every package is pinned with a SHA256 hash.
- All pinned entries are added to `docs/approved-dependency-baseline.md`.
- CVE tracking: the vLLM pinned snapshot is reviewed against the GitHub Advisory Database on a quarterly
  cadence (or immediately on a critical advisory).  The `scripts/check_trusted_dependencies.py` policy
  checker is extended to cover the vLLM sub-tree.
- Updates are pull-request gated: a PR bumping the vLLM pin must include an updated baseline document,
  an advisory-database clear (or documented mitigations), and a passing CI policy gate.
- vLLM is not fetched at runtime; it is installed from the vendored snapshot or a private mirror during
  image build.

Integration points:
- A new `freetalon/inference.py` module wraps the vLLM `LLM` / `AsyncLLMEngine` interface behind the
  same task-payload + security-boundary pattern established in ADR 0001.
- The hive scheduler recognises `task_type: "inference"` and routes to workers with the vLLM engine
  loaded.
- `tensor_parallel_size` and `pipeline_parallel_size` from the task payload are forwarded to the vLLM
  engine constructor.

### 5. DeepSpeed — vendored, audited, enterprise update cadence

DeepSpeed is adopted for distributed training workloads (ZeRO stages 1/2/3, mixed precision, gradient
checkpointing).

Supply-chain policy: identical to vLLM above.  DeepSpeed's dependency tree (including `triton`,
`hjson`, `py-cpuinfo`, etc.) is fully enumerated, pinned, and added to the approved baseline.

Integration points:
- A `freetalon/training.py` module wraps the DeepSpeed `initialize()` interface behind the same
  task-payload + security-boundary pattern.
- The scheduler recognises `task_type: "training"` and requires workers with matching GPU/CPU capability
  flags from the hardware profile.
- ZeRO stage and optimizer state partitioning are exposed as task parameters.

### 6. Parameterization

All distributed and parallelism knobs are expressed in the existing Pydantic config schema
(`freetalon/config.py`) rather than as free-form environment variables or CLI flags.  This means:
- Every parameter is schema-validated at startup.
- Invalid combinations (e.g. `tensor_parallel_size` > available GPUs) are caught and reported before
  any worker is spawned.
- The config schema is versioned; breaking changes increment the config version and a migration path
  is documented.

Key parameters planned:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `topology` | `ring` \| `star` | `star` | Logical node topology |
| `transport` | `tcp` \| `rdma` | `tcp` | Node-to-node transport |
| `tensor_parallel_size` | int | 1 | Number of tensor-parallel shards |
| `pipeline_parallel_size` | int | 1 | Number of pipeline stages |
| `data_parallel_size` | int | 1 | Number of data-parallel replicas |
| `nccl_socket_ifname` | str | `lo` | Network interface for NCCL rendezvous |
| `nccl_debug` | bool | false | Enable verbose NCCL diagnostic output |
| `deepspeed_zero_stage` | int (0–3) | 0 | ZeRO optimization stage |
| `vllm_max_model_len` | int | 4096 | Max sequence length for vLLM engine |
| `vllm_dtype` | str | `auto` | vLLM weight dtype (`auto`, `float16`, `bfloat16`) |

### 7. NCCL — vendored or system-installed, audited

NCCL (NVIDIA Collective Communications Library) is the collective-operations backend for GPU-to-GPU
communication (all-reduce, all-gather, broadcast, reduce-scatter) when NVIDIA hardware is present.

Supply-chain policy:
- Prefer the system-installed NCCL that ships with the CUDA toolkit on operator-managed hosts (avoids
  a second vendored copy of a large native library).
- Where a specific NCCL version must be pinned (e.g. for reproducibility or a security fix), a known
  release is vendored as a wheel or included in the trusted container image layer.
- NCCL version and the hash of the wheel/shared library are recorded in the approved baseline.

Integration points:
- `freetalon/hardware.py` detects NCCL availability (`nccl` package, `libnccl.so` presence, CUDA
  version compatibility).
- The hive config `nccl_socket_ifname` parameter is forwarded to the `NCCL_SOCKET_IFNAME` environment
  variable at worker-spawn time to control rendezvous interface selection.
- `nccl_debug: true` sets `NCCL_DEBUG=INFO` for diagnostic sessions.
- NCCL health is included in the `health` CLI endpoint response when GPU workers are active.

## Tradeoffs

- Topology as a config profile rather than dynamic topology means the cluster must be restarted to
  switch modes.  This is acceptable for local/lab deployments and simplifies the control plane
  significantly.
- RDMA is opt-in with `tcp` as the safe default.  This avoids kernel/driver complexity for operators
  who do not have RDMA hardware.
- vLLM and DeepSpeed are vendored rather than live-fetched.  This adds operational overhead (manual
  pin updates) but is non-negotiable given the air-gap and enterprise audit requirements.
- NCCL prefers system-installed over vendored to avoid shipping large native binaries, with the
  tradeoff that the system NCCL version must be explicitly documented per deployment.

## Consequences

- Operators gain a clear, documented path from single-node hive to multi-node ring or star deployments.
- vLLM and DeepSpeed are available for high-throughput inference and distributed training under the
  same supply-chain guarantees as all other FreeTalon dependencies.
- The Pydantic config schema becomes the single source of truth for every parallelism and topology
  knob — no undocumented environment-variable overrides.
- Security and audit coverage extends to the full distributed surface: NCCL rendezvous, RDMA
  queue-pair setup, and framework initialisation are all logged in the structured audit stream.
- Future work (federation, mTLS node auth, multi-user ACL) can build on the topology abstraction
  established here without changing the task-payload or security boundary contracts.
