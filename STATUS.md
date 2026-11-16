# Status — 2026-11-14 (standup update)

Branch: `dev/seeker7-single-node` (not yet pushed — GitHub not connected).
Mainline: `2085f6cd`. Plan: `PLAN.md`.

## Health

- Tests: 229/229 green (`python -m unittest discover -s tests`).
- OS: Ubuntu 24.04, kernel 7.0.0-43, Nvidia 595.91.07, zero packages upgradable.
- Services: Tailscale + SSH up, Ollama up (gemma-3-abliterated, localhost-only),
  xrdp up but restricted to Tailscale.
- CI: `tests.yml` added this session (not yet run — needs the branch pushed);
  `trusted-dependency-policy.yml` already existed.

## Done today

- OS patch + reboot verified: kernel 7.0.0-43, driver 595.91.07, access
  returned, Ollama auto-started, intake re-verified.
- xrdp hardened to Tailscale-only with 15-minute check-in watchdog; checked
  in, hardening kept. Rollback: `tools/harden-xrdp.sh rollback`.
- Test suite fixed: 26 broken -> 229/229 green (JSON fix, flat distributed
  config, strict registry, planner normalisation, prompt contracts,
  executor audit events). Bootstrap scaffolder quarantine fixed.
- Version control: branch created, local-only artefacts gitignored
  (*.bak, *.db, .env, Modelfile, models/, generated/), CI workflow added,
  PLAN.md + this file added.

## Doing

- Autonomous-loop design document (being written; will land in the home-lab
  goal docs).
- GitHub connection (needs Pat — see Blocked).

## Blocked

- GitHub not connected: Muse has no GitHub access yet. Pat needs to connect
  GitHub and install the Muse GitHub App on `inertiald/FreeTalon`. Until
  then the branch cannot be pushed and CI cannot run.
- Loop design doc not yet read/aligned into PLAN.md (build still running).
- Switch model unknown; fleet work blocked on it and on milestones M0–M2.
- Two remote Copilot branches untriaged (hypervisor claw, DAG visualisation).

## Next

1. Pat connects GitHub -> push `dev/seeker7-single-node`, watch CI go green.
2. Triage the two Copilot branches (merge / rework / drop).
3. Read the finished loop design doc, align PLAN.md milestones with it.
4. M0: run-and-measure loop (executor judges exit codes + acceptance).

## Update - 2026-11-14 16:24

- Loop design doc finished, read, and aligned into PLAN.md (section 9):
  five phases, measurement contract (PASS/FAIL/UNMEASURABLE, SUCCEEDED
  only when execution completed AND measurement passed), run-directory
  layout, gate packets bound to packet hash, five decisions for Pat
  before Phase 1.
- GitHub still not connected - branch push + CI still blocked on Pat.

## Update - 2026-11-14 16:33

- Branch dev/seeker7-single-node created on GitHub from main 2085f6cd.
- GitHub App connected as inertiald; branch creation worked.
- Snapshot push of the full branch content was refused (403, tree
  creation) - the app integration could not write file contents.
  Diagnosing with this single-file write; full push to follow once
  the write path is confirmed.

## Update - 2026-11-14 19:42

- Push finished: 16 of 17 files on GitHub branch dev/seeker7-single-node
  (head aa367d0c), content verified by blob SHA. The one missing file is
  .github/workflows/tests.yml: GitHub refuses app writes under
  .github/workflows/ (403, needs the separate workflow permission).
- CI: zero workflow runs on this branch so far. The Tests workflow does
  not exist on the branch (file missing), and the dependency-policy
  workflow only triggers on main pushes and PRs, so it never ran here
  either. Once tests.yml lands (via a git push from seeker7), pushes to
  dev/* will run the suite.

## Update - 2026-11-14 19:53

- CI ran for the first time on the branch (Tests #1): 228/229 passed,
  1 failure — test_detect_rdma_false_when_tools_absent. Root cause: the
  test mocked the tool probes but not the kernel-module probe
  (_rxe_module_loaded reads /proc/modules directly), and the GitHub
  runner loads ib_core, so detection returned True. Fix: mock the
  module probe in the test too (tests/test_docker_manager.py).
  Pushed as a new commit; waiting on the re-run.

## Update - 2026-11-14 19:54

- CI green: Tests #2 (run 43674364531) passed — 229/229 on the branch.
  The RDMA test fix did it. Branch head a4409fc.

## Update - 2026-11-14 20:13 (M0 DONE)

- M0 Run and measure is DONE and green: 270/270 tests (was 229).
  - New: freetalon/orchestrator/measure.py (deterministic evaluators,
    verdicts PASS/FAIL/UNMEASURABLE), run_store.py (run directories),
    loop.py (LoopRunner, GoalContract evaluation, summary.md).
  - Executor now measures every node: non-zero exit code fails the node
    even with no criteria; timeouts kill the process group; measurements
    recorded on the node and in measurements.jsonl.
  - GoalContract with conservative budgets (3600s, 3 attempts, 3 re-plans,
    50 nodes, $0 real money). Sanitizer fixed (echo accepts "don't" etc.).
    vLLM decided DEFERRED; Ollama is the working path.
  - Live smoke: run-20261115T041349Z-bc5c66 — exit-1 node FAILED,
    file-writing node COMPLETED, run dir + summary verified on disk.
- Next: M1 Judge and re-plan.

## Update - 2026-11-14 20:26 (M1 DONE)

- M1 Judge and re-plan is DONE and green: 289/289 tests in ~9s (was 270).
  - New: freetalon/orchestrator/judge.py (four decisions, budget
    accounting, progress rule, re-plan briefs, duplicate-approach
    validation); planner replan_from_brief via Ollama; LoopRunner now
    loops through judge decisions with numbered plan versions.
  - Verified: re-plan once and succeed; budget-exhausted escalation with
    full attempt history; duplicate re-plan rejected; timeout retry;
    repeated UNMEASURABLE escalation.
  - Note: an early M1 run was slow (98s) because failing M0 tests
    triggered live LLM re-plans; fixed by injecting a refusing re-plan
    function in those tests. The suite needs no LLM.
- Next: M2 Experiment memory.

## Update - 2026-11-14 20:35 (M2 DONE)

- M2 Experiment memory is DONE and green: 300/300 tests in ~9s (was 289).
  - New: freetalon/orchestrator/memory.py (ExperimentStore, SQLite table
    next to execution_plans; goal_key + domain scoping; failed_approaches;
    deterministic factual lessons).
  - Loop now checks an initial plan against memory (repeat without
    differs_by escalates before anything runs), feeds memory into
    re-plan briefs, and validates re-plans against recorded failures.
    Records land in SQLite and in the run's memory.jsonl.
  - Fixed during M2: a bad patch had stranded the state store's
    _write_lock inside a property getter (45 errors); repaired, all green.
- Next: M3 Research and data tools.

## Update - 2026-11-14 20:50 (M3 DONE)

- M3 Research and data tools is DONE and green: 320/320 tests in ~9s
  (was 300).
  - New: freetalon/orchestrator/research.py (research + fetch_dataset
    claws; DuckDuckGo / Stack Exchange / Wikipedia engines; sourced,
    dated artifacts with trust notes; dataset provenance sidecars) and
    freetalon/orchestrator/sandbox.py (read-only analysis: hash, type,
    strings, structure; refuses paths outside the run dir; never
    executes the file).
  - Executor now injects run_dir into node inputs so a dataset fetched
    by one node can be analysed by the next.
  - Live smoke on seeker7 (run-20261115T043909Z-504ac2): research,
    fetch, and sandbox analysis all PASS; goal PASS. DuckDuckGo is
    bot-blocked from this VM (HTTP 202) — recorded in the artifact,
    Stack Exchange + Wikipedia carry the research.
  - The first smoke run failed and M2 memory correctly refused the
    identical re-run; the re-run stated differs_by and proceeded.
- Next: M4 Approval gates.

## Update - 2026-11-14 21:05 (M4 DONE)

- M4 Approval gates is DONE and green: 346/346 tests in ~13s (was 320).
  - New: freetalon/orchestrator/gates.py (GatePacket, GateStore in the
    plan store's SQLite file; hash-bound approvals; expiry sweep =
    deny on timeout; denial recorded, never re-asked).
  - Loop: irreversible nodes (reversible=False) pause at
    AWAITING_APPROVAL; resume_run continues after approval, fails
    honestly after denial/expiry. The executor refuses to run an
    irreversible node with no gate configured.
  - API: GET /gates (+ status/run filters), GET /gates/<id>,
    POST /gates/<id>/approve|deny (token auth, same as /tasks).
  - Dashboard: Approval Gates section in the right panel with
    Approve / Deny buttons, refreshed every 2s.
  - Live smoke (run-20261115T044604Z-1a2ab1): pause, API list,
    HTTP approve, resume to COMPLETED, goal PASS.
  - Fixed during M4: the packet hash must NOT include evidence
    (it grows during a run and would void approvals).
- Next: M5 Verticals (finance + ECU).

## Update - 2026-11-14 21:15 (M5 DONE — loop ready for hardware integration)

- M5 Verticals is DONE and green: 368/368 tests in ~13s (was 346).
  - New: freetalon/orchestrator/finance.py (backtest, train_model,
    paper_trade, invest stub, kill switch) and
    freetalon/orchestrator/ecu.py (can_capture simulated, verify_backup,
    tune_surface, flash stub).
  - Finance: paper mode is the only mode (non-paper refused); position
    caps are hard constraints in sizing; kill switch file blocks trading.
  - ECU: CAN capture is simulated or replayed from a file; live capture
    refused; tune surface is built from the sandbox report, never the
    raw file; a verified backup is a pre-step to the flash gate.
  - Live smoke: finance run-20261115T045041Z-f529e6 and ECU
    run-20261115T045041Z-666823 — both paused at their gates, both
    gated handlers refuse after approval. No real money, no live ECU,
    no flashing.
  - Fixed during M5: the synthetic price series never crossed (0
    trades); reshaped to fall-rise-fall so the SMA crossover fires.
- M0–M5 are DONE. The loop is ready for hardware integration (M6 fleet
  remains out of scope).

## Update - 2026-11-14 21:00

- LLM end-to-end test (first real Gemma-planned run, run-20261115T045558Z-a052cd):
  goal in plain language -> Gemma intake -> Gemma plan -> loop. FAILED honestly
  in 24s. Gemma invented a claw name ("script", not registered), wrote to
  /tmp instead of the workspace, planned 1 node (no run, no verify), and gave
  no structured criteria. Executor failed the node ("No handler registered"),
  judge chose REPLAN, then Gemma echoed the replan brief instead of a DAG
  ("Planner output must contain a 'nodes' array") and the run escalated.
  Loop machinery behaved as designed; the LLM planning is the weak link.
  Fix queued for Pat: planner hardening (claw allowlist + plan repair retry
  + structured criteria). The M5 smoke runs used hand-written plans, not
  LLM plans -- that distinction is now on the record.

## Update - 2026-11-14 21:12

- Planner hardened (Pat: "harden the planner now"): claw allowlist
  enforced in code (KNOWN_CLAW_NAMES; aliases accepted), structured
  criteria required per node (paths must be files the node itself writes
  or runs), plan repair retry (max 2, then honest raise) in
  plan_task_intent + replan_from_brief, prompts state the allowlist /
  criteria / cover-every-step / never-echo-the-brief. Intake fixed too:
  with temperature 0 Gemma echoed the prompt's few-shot example verbatim
  (3/3 calls) — example removed, user message framed as a request line;
  intake now returns the full goal. Tests 368 -> 381, CI green.
- Live proof, first attempt: run-20261115T051123Z-8cc0c1, same Fibonacci
  goal, Gemma planned 3 nodes (scaffold fib.py, run it, verify output),
  all PASS, goal PASS. fib.py verified independently: prints exactly
  0 1 1 2 3 5 8 13 21 34. M5 smoke still passes unchanged.
- Branch head: check git log (docs commit on top of 1ca667d).

## Update - 2026-11-15 17:35

- Harder planner goal (Pat: "keep developing"): extend GET /health in
  freetalon/api.py to report Ollama reachability (localhost:11434) and
  GPU temperature via nvidia-smi, add unit tests, keep the suite green,
  verify live. Attempt 1, run-20261116T013052Z-917367: FAILED.
- What Gemma planned (3 nodes): scaffold a NEW file
  freetalon-workspace/api/health.py with a "#TODO" stub (never read the
  existing api.py, never found the existing /health endpoint); run tests
  with a malformed command ("python3-munittestdiscover-s ..." — exit
  127); verify live with curl to localhost:8000 (wrong port; API is
  8765). Node-1 PASS (stub file existed), node-2 FAIL (exit 127).
  Judge chose REPLAN; the replan LLM call hit a CUDA error instead.
- GPU FAILURE during the replan call: the RTX 3090 fell off the bus.
  dmesg: "Xid 79, pid=127944, name=llama-server, GPU has fallen off
  the bus", then "Xid 154, GPU recovery action changed to Node Reboot
  Required". nvidia-smi now reports "No devices were found".
  Peak temp during the run was 62C (fans manual 65%) — NOT a thermal
  event; the card failed at moderate temperature. This is the failure
  Pat warned about ("might literally jump off the bus").
- LLM work STOPPED per the thermal rules. No attempt 2 or 3: the GPU
  is gone and Ollama cannot run inference. Cleanup done: temp logger
  stopped, Gemma's stub file deleted, repo tree clean.
- Verified after the failure: full suite still 381/381 green, M5 smoke
  still passes (finance run-20261116T013248Z-16f0c4, ECU
  run-20261116T013248Z-a5fafe) — the loop machinery does not need the
  GPU. Fans could not be restored to auto (nvidia-settings needs the
  GPU); they reset to auto on reboot.
- Blocked on: GPU recovery. Xid 154 says a node reboot is required.
  Reboot is Pat's call — not done by the worker. Until the GPU is
  back, no LLM work is possible on seeker7.
- Also new weakness exposed (independent of the GPU): Gemma planned a
  metric criterion ("test_count") on a bash node that cannot report
  metrics — the executor records it as not reported. The planner's
  criteria validation accepts metric criteria on any claw.
