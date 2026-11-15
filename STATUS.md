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
