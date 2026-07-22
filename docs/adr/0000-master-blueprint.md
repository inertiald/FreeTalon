# ADR 0000: FreeTalon Master Blueprint — Distributed Task Orchestrator

## Status
Accepted (living document)

## AGENT EXECUTION PROTOCOL
If you are an AI assistant reading this document, you are acting as the system architect for FreeTalon. You must follow this loop:
1. **Analyze:** Read this document to understand the distributed architecture.
2. **Select:** Find the first uncompleted task `[ ]` in the Execution Backlog.
3. **Execute:** Write the necessary Python code and update existing files. Do not break existing single-node local execution.
4. **Update:** Modify this file to mark the task as `[x]` complete with a 1-sentence summary of your changes.
5. **Yield:** Stop generating and wait for the user to say "Next".

---

## Intent

FreeTalon is a **general-purpose** distributed task orchestration engine. It is not built for any
single domain: it provisions infrastructure dynamically, discovers compute nodes on the local
network, and executes complex Directed Acyclic Graphs (DAGs) for arbitrary data processing,
simulation, and model-development workloads.

**Clarification on simulation workloads:** the reference workload for this blueprint is *not*
"simulated execution" for its own sake. It is **simulated trading whose outputs are fed into a
test/train pipeline** to develop a model for a specific downstream task. That is, simulation is a
data-generation stage in a model-development DAG:

```
simulate (trading) ──► dataset ──► train ──► evaluate/test ──► model artifact
```

That particular task is an *example* of what FreeTalon can run, not the purpose of the repo. The
orchestrator, planner, executor, and mesh layers must remain domain-agnostic; domain-specific
logic lives in tool handlers registered with the `ToolRegistry`.

## Relationship to other ADRs

- **ADR 0001 (Local Hive Runtime):** provides the hardened single-node execution kernel this
  blueprint builds on. Nothing here may break single-node local execution.
- **ADR 0002 (Distributed and Parallel Compute):** defines topology (ring/star), interconnect
  (RDMA), and framework strategy. This blueprint sequences the concrete implementation work.

## Target Architecture: Distributed Execution Mesh

*   **Node Provisioning:** The orchestrator can dynamically request isolated environments via Libvirt.
*   **Network Automation:** Uses standard IaC libraries (Netmiko) to apply network configurations to local hardware.
*   **Dynamic DAG Generation:** The execution engine can pause a running task, generate a sub-DAG to resolve missing dependencies, and resume.
*   **Domain-agnostic core:** planner/executor/mesh know nothing about trading, weather, or any
    other domain; workloads plug in as tools and plan payloads.

All new dependencies (Netmiko, libvirt-python, lldpctl bindings) follow the supply-chain rules in
`docs/approved-dependency-baseline.md`: pinned, audited, no floating references.

---

## Execution Backlog

### Phase 1: Installation & Network Discovery
- [x] **Task 1.1: Multi-Node Installer.** Update `installer.py`. Add a CLI prompt to select "Primary Orchestrator" or "Worker Node". *(Done: added `--node-role {orchestrator,worker}` flag with interactive prompt fallback; the selected role is persisted to `.env` as `FREETALON_NODE_ROLE`.)*
- [x] **Task 1.2: Network Topology Mapping.** Create `freetalon/mesh/recon.py`. Write a subprocess wrapper for `lldpctl` to parse local network topology (DAC/Ethernet connections) into a JSON model. *(Done: added `freetalon/mesh/` package with `Neighbor` and `NetworkTopology` frozen dataclasses, `parse_lldp_json` / `parse_lldp_keyvalue` parsers, and a `discover_topology()` entry-point that uses `shutil.which`, a 3-second timeout, and never raises — falling back to an empty topology on any failure.)*
- [x] **Task 1.3: Netmiko Configuration Module.** Scaffold `freetalon/orchestrator/claws/network.py`. Build a utility that accepts JSON configurations and applies them to local switches using the Netmiko library. *(Done: created `freetalon/orchestrator/claws/network.py` with whitelist input sanitization, a `render_plan()` dry-run, and a mandatory Human-In-The-Loop approval gate (`Approval` token must match the rendered plan's SHA-256 `plan_id` before any push proceeds); Netmiko is imported lazily and raises a clear error directing operators to vendor and pin it per `docs/approved-dependency-baseline.md`; all security-relevant actions are audit-logged; `tests/test_network.py` provides hermetic coverage with no netmiko dependency.)*

### Phase 2: Dynamic Task Routing
- [x] **Task 2.1: Sub-DAG Injection.** Update `freetalon/orchestrator/executor.py`. If a task returns a `DependencyMissing` payload, the executor must call the planner to generate a sub-DAG, insert it into the `ExecutionPlan`, and resolve it before continuing. *(Done: added `DEPENDENCY_MISSING_KEY` / `DependencyRequest` / `_is_dependency_missing` signal protocol, injectable `subdag_planner` constructor parameter, `_inject_subdag` helper that remaps ids and rewires the originating node to DRAFT, a per-node injection-count loop guard capped at `MAX_INJECTION_ATTEMPTS`, and a no-planner fallback that fails the node gracefully; all covered by new tests in `tests/test_executor.py`.)*
- [ ] **Task 2.2: Dynamic Tool Loading.** Update the routing logic so that if a required tool script is missing, the orchestrator triggers a code-generation task to scaffold the missing Python script, loads it dynamically, and retries.
- [ ] **Task 2.1: Sub-DAG Injection.** Update `freetalon/orchestrator/executor.py`. If a task returns a `DependencyMissing` payload, the executor must call the planner to generate a sub-DAG, insert it into the `ExecutionPlan`, and resolve it before continuing.
- [x] **Task 2.2: Dynamic Tool Loading.** Update the routing logic so that if a required tool script is missing, the orchestrator handles it. *(Done: a missing capability now generates a draft scaffold proposed for human review in `generated/proposed_tools/<capability>/` via `ToolScaffolder`, the node is marked `NEEDS_TOOL`, and audit events are emitted; **runtime code-generation-and-execution was deliberately NOT implemented per security review** — generation and execution remain separated by a human and a commit.)*

### Phase 3: Infrastructure Provisioning
- [x] **Task 3.1: Libvirt Environment Management.** Create `freetalon/orchestrator/claws/hypervisor.py`. Write a module that accepts resource parameters (CPU/RAM) and uses `libvirt-python` to provision and teardown KVM virtual machines for isolated task execution. *(Done: added whitelist sanitization for domain requests, host-capacity CPU/RAM cross-checks, dry-run domain-XML rendering, and deferred `libvirt-python` execution paths that stay blocked until the dependency is vendored and SHA256-pinned per the approved baseline.)*

### Phase 4: UI & Pipeline Validation
- [ ] **Task 4.1: DAG Visualization.** Update `dashboard.py`. Enhance the NiceGUI interface to render deeply nested DAG structures to monitor complex pipeline executions.
- [ ] **Task 4.2: E2E Pipeline Test.** Build an integration test for a "Data Aggregation, Simulation, and Model-Development Pipeline". The test should prompt for mock API keys, generate a plan that aggregates external data (e.g. weather and financial feeds), runs a localized trading simulation to produce a dataset, feeds that dataset into a test/train stage, and outputs the resulting model artifact and evaluation results.
