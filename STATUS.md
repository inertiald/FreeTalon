# Status — 2026-11-14 (standup update)

Branch: `dev/seeker7-single-node` (not yet pushed — GitHub not connected).
Mainline: `2085f6cd`. Plan: `PLAN.md`.

## Health

- Tests: 402/402 green (`python -m unittest discover -s tests`).
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

## Update - 2026-11-16 08:05 (planner weaknesses fixed, CPU-only)

- The three planner weaknesses from the failed /health goal are fixed,
  with no LLM calls and no GPU. Tests 381 -> 397, all green; M5 smoke
  still passes unchanged (finance run-20261116T160136Z-f1cec9, ECU
  run-20261116T160136Z-5a0f5b — all real steps PASS, invest/flash stubs
  refuse as designed).
- Fix 1, repo grounding: new freetalon/orchestrator/repo_index.py builds
  a read-only index of the repo (modules, HTTP routes from api.py, API
  port from config.py) and renders it as REPO CONTEXT in the planner and
  re-planner prompts. Plan validation now rejects any scaffold node that
  creates a new file duplicating an existing route or module (e.g. a new
  api/health.py while GET /health already exists in freetalon/api.py) —
  the plan must extend the existing file instead. Prompts carry a
  "GROUND IN THE REPO" rule. The exact failed node-1 is now rejected,
  and a plan targeting freetalon/api.py itself is accepted.
- Fix 2, command/port validation: plan validation now tokenizes bash
  commands (shlex — unbalanced quotes rejected) and rejects words glued
  together (the failed run's "python3-munittestdiscover-s ..." and
  "curl-shttp://localhost:8000/health"). Localhost ports must be known
  services: FreeTalon API on 8765, Ollama on 11434. Port 8000 is the
  OpenAI-compatible LLM backend, not a service to curl — a plan using
  it for an API route is rejected naming the correct port (8765). A
  drift-guard test asserts the ports match HiveConfig.port and
  intake._OLLAMA_BASE_URL.
- Fix 3, metric/claw capability map: new CLA_METRICS in tool_registry.py
  records which metrics each claw's handler actually reports (bash and
  scaffold report none; llm_synthesis reports summary_chars; backtest
  reports trade_count, etc.), with a drift-guard test that the map
  covers every known claw. Plan validation now rejects a metric
  criterion naming a metric the node's claw cannot report — the failed
  run's test_count-on-bash is rejected at plan time instead of
  measuring "not reported" at execution time.
- Verified against the real failed plan: plan-v001.json from
  run-20261116T013052Z-917367 is now rejected (metric test_count on
  bash, the first of its three errors to surface).
- Note: validation errors surface one at a time, so a plan with
  several independent errors can exhaust the 2-repair budget before
  all of them are fixed. Collecting all validation errors into one
  rejection is a possible follow-up; not done here.
- Still blocked: the GPU is dead (falls off the bus on first CUDA
  load after a cold boot) and the gh token is invalid — the /health
  goal retry waits for a working GPU, and this commit is local-only
  until Pat re-auths gh on seeker7.

## Update - 2026-11-16 09:55 (planner validation collects all errors)

- Follow-up to the three planner fixes: plan validation used to raise
  on the first failing check, so a plan with several independent errors
  burned one repair retry per error. Now every node is checked against
  every rule (claw allowlist, structured criteria, command/port
  validation, repo grounding) and ALL errors are collected into a
  single rejection, grouped by node, so one repair round can fix them
  all. The repair re-prompt now says to fix ALL of the listed reasons.
- Implementation (freetalon/orchestrator/planner.py): the four
  first-error validators are replaced by per-node collectors
  (_claw_error, _criteria_errors, _command_errors, _grounding_error)
  driven by _collect_plan_errors; _validate_bash_command now returns
  every problem in a command (unbalanced quotes, each glued token,
  each unknown port) instead of the first. Individual error wordings
  are unchanged, so existing rejection assertions still match.
  Structural errors (bad JSON, duplicate ids, dependency cycles) still
  raise first in normalize_plan_nodes — the node list cannot be
  interpreted until the DAG is structurally valid.
- Tests 397 -> 402, all green; M5 smoke still passes unchanged
  (finance run-20261116T174941Z-ab08e2, ECU
  run-20261116T174941Z-ee0cd1). New TestPlannerCollectsAllErrors (5
  tests): the real failed /health plan now surfaces all 5 of its
  errors in one rejection (duplicate /health, test_count on bash,
  glued test command, glued curl, port 8000 naming 8765);
  single-error plans still reject naming the one error; valid plans
  still pass; a mocked repair fixes the multi-error plan in ONE round
  (2 LLM calls, repair prompt carries all five error markers).
- Still blocked: the GPU is dead (fell off the bus a third time,
  2026-11-16 ~08:32, while idle — Xid 79 + Xid 154, this time "GPU
  Reset Required") and the gh token is invalid — this commit is
  local-only until Pat re-auths gh on seeker7. No LLM loads until a
  power limit is re-applied (Pat's decision).

## Update - 2026-11-16 21:00 (/health retry — planner echo, GPU survived)

- GPU is BACK: Pat reseated the FTW3 in the top slot (PCIEX16_1), link
  width x16 (was x4), 300W limit applied persistently via systemd unit
  nvidia-power-limit.service (verified Current/Requested 300W). First
  CUDA load survived (50-token generation, 40C, 172W).
- /health retry (Pat: "Go — run the /health retry"): FAILED at the
  planning stage, before any run was created — no run id, no judge
  involvement. Gemma echoed the TaskIntent JSON verbatim 3/3 times
  (initial attempt + 2 repairs) instead of producing a plan DAG;
  parse_planner_response raised "Planner output must contain a 'nodes'
  array" and plan_task_intent raised honestly. Nothing touched disk.
  Log: freetalon-workspace/logs/llm-end-to-end-health-retry-2026-11-16.log.
- Root cause — same failure family as the Nov 14 intake echo:
  temperature 0 + the planner prompt embeds the intent JSON verbatim
  ("TaskIntent JSON:\n{...}"), and at temp 0 Gemma copies the most
  salient JSON in the prompt instead of generating a plan. The intake
  code already documents this ("with temperature 0 the model otherwise
  echoes whatever JSON it last saw in the prompt") — the planner has
  the same flaw. The repair re-prompt re-includes the failed output,
  which reinforces the echo instead of breaking it.
- A blind re-run would almost certainly fail identically (same prompt,
  temp 0, 3/3 identical echoes across differing repair prompts). The
  fix belongs in the planner prompt/framing, not in another attempt.
- GPU survived the retry: peak 48C, limit held at 300W, link x16,
  zero Xid 79/154 after. Fans restored to auto. Suite still 402/402
  green. The endpoint was NOT extended, no new tests were written,
  no live verification happened — planning never produced a DAG.
- Still blocked: gh token invalid — this commit is local-only until
  Pat re-auths gh on seeker7 (classic PAT plan, 2026-11-17).

## Update - 2026-11-16 21:05 (planner echo fix — CPU-only)

- Planner echo fix DONE: the /health retry failed because Gemma echoed
  the TaskIntent JSON verbatim 3/3 times. Root cause was the planner
  prompt ending with the raw intent JSON at temperature 0, plus a repair
  loop that pasted the failed output back in. Fix: intent is now rendered
  as prose lines (no copyable JSON), prompts end with "Produce a NEW
  plan DAG ... JSON Output:", the repair re-prompt describes the failure
  instead of quoting it, and the planner system prompt states the
  anti-echo rule (same family as the Nov 14 intake fix). Re-plan brief
  got the same trailing framing. Temperature unchanged at 0.
- Tests 402 -> 408/408 green (+6 in new TestPlannerEchoFix, incl. the
  exact /health echo case now repairing to a valid plan in one round).
  M5 smoke still passes. Log:
  freetalon-workspace/logs/planner-echo-fix-2026-11-16.log.
- /health retry is unblocked by this fix but NOT re-run: it needs GPU
  load and Pat's go. No LLM calls were made for this fix.
- Still blocked: gh token invalid — commits local-only until Pat
  re-auths gh on seeker7 (classic PAT plan, 2026-11-17).

## Update - 2026-11-16 21:55 (/health retry #2 — plan produced, execution failed, replan echoed memory)

- /health retry #2 (Pat: "Go — retry the /health goal", echo fix in
  place): run-20261117T054715Z-698173, goal verdict FAIL in 65s. Log:
  freetalon-workspace/logs/llm-end-to-end-health-retry2-2026-11-16.log.
  Plan id plan-7885a4ef9d99 — the same id as attempt 1.
- Progress vs attempt 2: the initial planner echo is FIXED — Gemma
  produced a real plan DAG on the first call (prose intent prompt
  works). But the plan was still wrong in the same way as attempt 1:
  node-1 (bash) wrote a hardcoded fake {"health": "OK", ...} to
  freetalon-workspace/api.json instead of extending the existing
  /health in freetalon/api.py; node-2 wrote a fake test.sh instead of
  real unit tests. The repo-grounding rule did not stop the model
  from faking the endpoint with a static file. Node-1 PASS (file
  exists), node-2 PASS (file exists), node-3 FAIL — running test.sh
  exits 127 (its contents are an assert line, not a command). Judge
  chose REPLAN.
- Replan failed: Gemma echoed the replan brief's own content instead
  of a delta plan — first repair echoed the instruction +
  remaining_budget fields, second returned an experiment-memory record
  from attempt 1 (run-20261116T013052Z-917367, node-1) as the "nodes"
  array. That failed PlanNode schema validation (10 errors: missing
  id/objective, extra run_id/goal_key/domain/node_id/ts/approach/
  decision/lesson forbidden) and the run escalated to FAILED. The
  replan brief includes memory records verbatim — same echo family,
  new surface.
- Cleanup: fake api.json and test.sh deleted from the workspace. Repo
  tree clean; freetalon/api.py untouched (existing minimal /health at
  api.py:44 still the only one). No new tests written, no live
  verification. Suite still 408/408 green.
- GPU survived — third CUDA load since the reseat: peak 76C at 263.7W
  during the run (limit held at 300W, link x16 throughout), cooled to
  56C after; zero Xid 79/154. Fans manual 65% during, restored to
  auto. 76C is the hottest seen yet (previous peaks 62-63C) but below
  the 85C pause threshold; the longer run (intake + plan + 2 replan
  calls) explains it.
- Still blocked: gh token invalid — commits local-only until Pat
  re-auths gh on seeker7 (device flow, as before; PAT idea dropped).

## Update - 2026-11-16 (evening)

- Planner fake + memory-prose fix DONE (CPU-only, no LLM calls, no GPU
  load): both weaknesses from /health retry #2 fixed and tested.
  - Anti-fake validation (planner.py): (1) static stand-in rule - a bash
    node that echo/printf/heredoc-writes a literal JSON/assert answer into
    a file and then claims file_exists on that same file is rejected
    ("manufactures the deliverable instead of producing it"); plain echo
    writes are untouched. (2) route-engagement rule - when the goal names
    an existing API route (path literal or "<name> endpoint"), at least
    one node must read, modify, or call the module that serves it
    (freetalon/api.py for /health); otherwise the plan is rejected naming
    the module and route. Both rules are collected into the single
    all-errors rejection, so a repair round sees them together.
    PLANNER_SYSTEM_PROMPT gains "Never fake a deliverable".
  - Prose re-plan brief (planner.py, prompts.py): replan_from_brief now
    renders the brief as prose lines (goal + success measures, failed
    nodes with exact measurements, past experiments as prose with the
    lesson, remaining budget) instead of pasting the brief JSON verbatim.
    No copyable memory JSON is ever last in the prompt; measurements stay
    exact (exit 127, verdict FAIL, failed-criterion detail). Repair
    re-prompts describe the failed output instead of pasting it, so a
    memory-record echo cannot be reinforced. Temperature stays 0.
  - Tests: 408 -> 419/419 green (+11 TestPlannerFakeAndMemoryFix). One
    older grounding test needed its mocked plan retargeted at
    freetalon/api.py (it scaffolded fib.py for a /health goal, which the
    new route rule now correctly rejects).
  - Mocked demos: (a) the verbatim retry-#2 plan-v001.json nodes are now
    rejected at plan time with 3 errors (node-1 static api.json, node-2
    static test.sh, route engagement); (b) a mocked replan whose first
    reply echoes the memory record verbatim repairs to a valid delta plan
    in one round (2 calls), and gives up after the budget (3 calls).
  - M5 smoke passes (finance run-20261117T060055Z-fa301c, ECU
    run-20261117T060055Z-f541c8).
- Next: /health retry #3 - NOT run here (loads the GPU; separate step).
  Still blocked: gh token invalid - commits local-only until Pat re-auths
  gh on seeker7 (device flow, as before).

- /health retry #3 (2026-11-16 evening, run under Pat's 12h mandate, no
  approval gate for GPU tests): FAILED at the planning stage - no run
  created, judge never involved, nothing touched disk (api.py untouched,
  no new tests, no stray files). 3 planner attempts (initial + 2 repairs),
  all rejected:
  - Attempt 1 faked the endpoint AGAIN (echo hardcoded JSON > health.json
    && mv to api/health.json, file_exists) - the new anti-fake rule caught
    it and the repair DROPPED the fake node. The rule works as a steering
    signal.
  - But all 3 attempts wrote the Python test file as a single-line
    echo '...' with the newlines collapsed (glued-word violation:
    "import unittest, requests, json from freetalon.api import apiclass
    TestHealthEndpoint..."), and neither repair fixed it. Final rejection:
    1 error, glued bash command on node-1. The model has no working way
    to emit a multi-line file through echo in this interface, and the
    repair prompt offers no alternative (heredoc / scaffold claw).
  - GPU: peak 83C (hottest yet, under the 85C pause), limit 300W held,
    x16, zero Xid. Fans manual 65% during, auto after. Suite 419/419
    green after. Log: logs/llm-end-to-end-health-retry3-2026-11-16.log.
  - Next fix (same family): teach the planner a valid multi-line file
    write (heredoc or scaffold claw) and name it in the repair message
    when the glued-word rule fires; the validator is doing its job, the
    model just has no valid move.

## Update - 2026-11-16 22:23

- Planner heredoc fix (retry #3, CPU-only, no LLM/GPU calls): the model
  had no valid move for writing a multi-line file - it tried 3 times to
  echo a test file as one glued line and the validator correctly rejected
  it every time. Now the planner prompt names the scaffold claw and the
  bash heredoc as THE ways to write a multi-line file (both system
  prompts), and a glued-word rejection names them as the alternative.
  Validation accepts a heredoc write: heredoc bodies are file content,
  not shell (a URL inside the file no longer trips the glued check or
  the port check), and a multi-line test file is no longer flagged as a
  static stand-in answer (the "assert" check now only fires on
  single-line literals; a heredoc fake JSON answer is still rejected).
  Demo: tools/demo-glued-echo-repair.py reads the verbatim retry-#3
  command from the retry3 log - rejected with the guidance, heredoc
  version accepted. Tests 419 -> 427/427 green (new
  TestPlannerHeredocFix, 8 tests), M5 smoke OK
  (finance run-20261117T062208Z-06cb8e, ecu run-20261117T062208Z-d6dc96).
  Log: logs/planner-heredoc-fix-2026-11-16.log.
  - Next: /health retry #4 (live, GPU) - not run here by design.

## Update - 2026-11-16 22:26

- /health retry #4 (live, GPU, fans 85%): FAILED in 50.0s, run
  run-20261117T062659Z-bcd569, plan plan-7885a4ef9d99. Intake clean.
  Initial planning took 3 attempts: two fake api.json/test.sh plans
  rejected, third accepted - but it never engaged freetalon/api.py:
  node-1 curled Ollama's /health (no such endpoint) -> FAIL exit 1,
  node-2 nvidia-smi PASS, node-3 echoed a hardcoded JSON to stdout.
  Judge chose REPLAN. Replan 1 echoed the brief's own delta steps
  back (incl. retry-#2's test.sh / glued unittest failures); replan 2
  faked again (echo api/health.py + glued unittest); replan 3 returned
  {"nodes":[]} -> re-planner failed ("must include at least one plan
  node") -> escalated FAIL. Endpoint NOT extended, no new tests, no
  live curl. Suite 427/427 green after; no stray files. GPU survived,
  zero Xid, limit 300W held, x16; 70C at fan restore (peak not sampled
  mid-run; 85% fans ran cooler than retry #3's 83C at 65%).
  Log: logs/llm-end-to-end-health-retry4-2026-11-16.log.
  - Next fix (same family): route engagement is too loose - it counts
    any "/health" string as engagement, so node-1 curling Ollama's
    /health (port 11434) satisfied it without touching freetalon/api.py.
    Tighten it (module mention, or a call to the route on the API's own
    port), and give the replan path the same no-copy treatment: replan 1
    echoed the brief's structured steps verbatim. The model also needs a
    grounded plan shape for route goals (modify the serving module, write
    tests via scaffold/heredoc, curl the real API port) instead of
    free-form probe/echo nodes.

## Update - 2026-11-16 22:40

- Planner route + replan fix DONE (CPU-only, no GPU/LLM): route
  engagement now requires the serving-module mention or a call to the
  route on the API's own port (8765) — the retry-#4 plan (curl
  11434/health, never touching freetalon/api.py) is now rejected at
  plan time naming the wrong service. Replan brief got the no-copy
  treatment: memory params are described by kind, never quoted
  ("command=a shell command"); a brief-echo-shaped reply is rejected
  by name; the replan prompt carries a grounded ROUTE GOAL shape
  (edit module -> tests -> curl API port). Tests 427 -> 437/437
  green (new TestPlannerRouteEngagementFix, 10 tests), M5 smoke
  passes (finance run-20261117T063750Z-21a835, ecu
  run-20261117T063750Z-5e46df). Demo tools/demo-route-engagement.py
  shows the exact retry-#4 plan rejected and a correct plan accepted.
  Log: logs/planner-route-fix-2026-11-16.log.
  - Next: /health retry #5 (live, GPU) - not run here by design.

## Update - 2026-11-16 22:45

- /health retry #5 FAILED at planning (no run created, DRIVER_EXIT:1,
  ~108s, 1 intake + 3 planner calls). The port-aware rule WORKED as a
  steering signal - attempt 3's probe plan (curl 11434/health, nvidia-smi,
  echo JSON answer) was rejected with the wrong-service error naming the
  fix ("Call /health on port 8765 or extend freetalon/api.py") - but the
  model burned its first two attempts on the retry-#2 fake verbatim
  (echo hardcoded JSON > api.json, fake test.sh), so no repair round was
  left for the engagement error. Endpoint NOT extended, no new tests,
  suite 437/437 green, no stray files, api.py untouched.
  GPU survived: 59C at check after, 300W limit held, x16, zero Xid;
  fans 85% during, restored to auto. Log:
  logs/llm-end-to-end-health-retry5-2026-11-16.log.
  - Next fix (same family): the model keeps reaching for the fake and
    for probe/echo nodes instead of the grounded ROUTE GOAL shape
    (edit freetalon/api.py via scaffold/heredoc -> write + run tests ->
    curl :8765/health). The anti-fake rejection steers away from the
    fake but not TOWARD the grounded shape, and the repair budget (max
    2) is spent before the engagement error ever fires. Consider: name
    the grounded shape in the anti-fake rejection too, or give the
    planner a first-attempt route-goal template so it does not have to
    discover the shape by trial and error.

## Update - 2026-11-16 22:48

- Planner grounded-shape fix DONE (retry #5): the anti-fake rejection
  now names the grounded move (extend the serving module via
  scaffold/heredoc, write real tests, curl the live API port) when the
  goal names an existing route, and the first-attempt planner prompt
  carries the ROUTE GOAL shape the replan prompt already had
  (_route_goal_shape gains a delta flag: "the plan must follow this
  shape" for first attempts, "the delta plan..." for replans).
  Tests 437 -> 444/444 green (new TestPlannerGroundedShapeFix, 7
  tests), M5 smoke passes (finance run-20261117T064804Z-7758b4, ecu
  run-20261117T064804Z-31927b). Demo tools/demo-grounded-shape.py
  shows the exact retry-#5 fake rejected with the shape named and a
  mocked repair landing on a grounded plan in one round (2 calls).
  Log: logs/planner-grounded-shape-fix-2026-11-16.log.
  - Next: /health retry #6 (live, GPU) - not run here by design.
Verification (all CPU-only, mocked, no LLM, no GPU):
- Full suite: 444/444 OK (was 437; +7 new TestPlannerGroundedShapeFix).
- M5 smoke: OK - finance run-20261117T064804Z-7758b4, ecu run-20261117T064804Z-31927b.
- Demo (a): verbatim retry-#5 attempt-1 fake (read from
  logs/llm-end-to-end-health-retry5-2026-11-16.log) rejected at plan
  time with 3 errors, and the rejection names the grounded move
  (freetalon/api.py, 8765, scaffold/heredoc, curl :8765/health).
- Demo (b): mocked first reply with that fake repairs to a valid
  grounded plan in one round (2 calls); node-1 scaffolds
  freetalon/api.py, last node curls :8765/health; the repair prompt
  contains the grounded move and the first prompt carries ROUTE GOAL.
  Demo tool: /home/seeker/freetalon-workspace/tools/demo-grounded-shape.py,
  log logs/demo-grounded-shape.log.
- Regression guards pass: retry-#4 plan rejected (wrong service),
  retry-#3 glued echo rejected (heredoc alternative named), valid
  heredoc /health plan passes; replan prompt still says "delta plan".
- No stray files (no api.json, test.sh, test_health.py, api/health.py).
Deliberately left out:
- Live /health retry #6 was NOT run - loads the GPU, separate step.
- No push (per instructions).

## Update - 2026-11-16 23:00

- /health retry #6 FAILED at planning (no run created, DRIVER_EXIT:1,
  ~115s, log logs/llm-end-to-end-health-retry6-2026-11-16.log).
  The grounded-shape fix IS steering the model: attempt 1 named
  freetalon/api.py + port 8765, attempt 2 used a heredoc and curled
  :8765/health. But the failure mode moved again:
  - Attempt 1: the retry-#2 fake shape again, rejected for the glued
    echo on node-2 (with the heredoc/scaffold guidance named).
  - Attempt 2: a real heredoc plan (Flask app via heredoc, test.sh,
    curl :8765) but ALL THREE nodes had no criteria at all - rejected
    with 3 errors. Also unflagged: it wrote to /tmp/freetalon/api.py
    instead of the repo module, invented Flask instead of extending
    the existing stdlib handler, and curled Ollama /health (which
    does not exist).
  - Attempt 3: the model "learned" criteria must exist but guessed
    the shape wrong - criteria as a bare dict {"tests_pass": true}
    instead of a list of criterion objects - PlanNode schema error,
    repair budget (max 2) exhausted.
  Endpoint NOT extended, no new tests, suite 444/444 green, no stray
  files, api.py untouched. GPU survived: post-run 62C, 46W, 300W,
  x16, zero Xid (fans 85% during, auto after).
  - Next weakness to fix (CPU-only): the criteria shape is never
    shown to the model - the repair says "every node needs at least
    one measurable criterion" but gives no example, so the model
    emits a dict. Show the criterion list shape in the prompt/repair
    (e.g. [{"type": "exit_code", "equals": 0}]) and/or coerce a
    bare-dict criteria into a one-element list at parse time.
    Also consider flagging file-write nodes that target /tmp instead
    of the serving module for route goals.

## Update - 2026-11-16 (evening, planner criteria-shape fix — CPU-only)

  Fix for retry #6: the criteria shape is now shown to the model and a
  bare-dict criteria is coerced at parse time (commit below).
  - planner.py: a bare-dict "criteria" ({"tests_pass": true}) is coerced
    to a one-element list in parse_planner_response, so the guess becomes
    a validation error the repair loop can fix instead of a structural
    PlanNode schema error that burned the whole repair budget. A
    criterion with no "type" names the shape in its error, and the
    no-criteria rejection shows the shape example:
    [{"type": "exit_code", "equals": 0}] (file nodes:
    [{"type": "file_exists", "path": "<the file the node writes>"}]).
  - prompts.py: schema rule 4 — "criteria" MUST be a JSON array (a LIST)
    of criterion objects, never a single object; same note on the replan
    criteria bullet.
  - Secondary: a route-goal plan whose write node targets /tmp is
    rejected by name (attempt 2 wrote a Flask app to /tmp/freetalon/
    api.py and passed route engagement because the /tmp path contains
    the module name as a substring).
  - Tests 444 -> 455/455 green (+11 TestPlannerCriteriaShapeFix);
    M5 smoke passes (finance run-20261117T065859Z-875666, ECU
    run-20261117T065859Z-45334c). Demo: verbatim retry-#6 attempt-3
    bare-dict plan rejected with the shape named (not a schema error),
    mocked repair lands on a grounded plan in one round.
    Tool: freetalon-workspace/tools/demo-criteria-shape.py, log
    freetalon-workspace/logs/demo-criteria-shape.log.
  - Next: /health retry #7 (GPU load, separate step).
## Update - 2026-11-16 (evening, /health retry #7 — GPU load)

  Retry #7 FAILED at the planning stage (~112s, DRIVER_EXIT:1, no run
  created). Intake succeeded cleanly. The three planner attempts:
  (1) the fake shape again (echo a hardcoded JSON answer, echo "curl
  ..." as the test) — rejected; (2) real progress in shape — a heredoc
  plan curling :8765/health — but it wrote a hallucinated Flask app to
  /tmp/freetalon/api.py (wrong path AND wrong framework; the real API
  is http.server-based) with a broken test.sh, and all three nodes had
  NO criteria — rejected by the /tmp rule and the no-criteria rule;
  (3) the model abandoned the PlanNode schema entirely and invented
  node types {"type": "write_file", "path": ..., "content": ...} /
  {"type": "execute_command", ...} with no "objective" and no
  "assigned_claw" — a structural PlanNode schema error (4 validation
  errors: objective missing; type/path/content extra-forbidden) raised
  before validation, repair budget exhausted. The criteria-shape fix
  DID steer attempt 2 (no-criteria rejection named the shape; /tmp
  write rejected by name), but a schema invention is not a repairable
  validation error yet. Endpoint NOT extended, no new tests, suite
  455/455 green, no stray files. GPU survived (55C, 300W, x16, zero
  Xid). Log: freetalon-workspace/logs/llm-end-to-end-health-retry7-2026-11-16.log.
  Next weakness: structural schema errors (missing objective, invented
  node fields like type/path/content) raise instead of becoming a
  numbered, repairable rejection — and the node shape itself
  (id/objective/assigned_claw/inputs/criteria/depends_on) is never
  shown to the model as an example. Also worth grounding: the API is
  not Flask; the serving module's framework should be visible to the
  planner so it stops writing Flask apps.

## Update - 2026-11-16 (evening, planner node-shape fix — CPU-only)

  Structural PlanNode schema errors are now collected, numbered
  validation errors instead of a raise: _validate_raw_nodes names the
  missing fields (objective) and the extra-forbidden fields (type/path/
  content/command) per node and shows the required node shape, so a
  schema invention becomes a repair-round fix instead of burning the
  whole budget (retry #7 attempt 3: invented write_file/execute_command
  node types). The planner prompt now shows the PlanNode shape as an
  example (NODE SHAPE rule). Secondary: the repo index records each
  module's web framework from its imports and the REPO CONTEXT names
  it — freetalon/api.py is built on stdlib http.server — and a
  route-goal plan that writes a different framework (Flask) is
  rejected by name. Tests 455 -> 470/470 green (+15 new
  TestPlannerNodeShapeFix), M5 smoke passes, verbatim retry-#7
  attempt-3 output is now a numbered rejection (11 errors) and a mocked
  first reply with invented node types repairs to a grounded plan in one
  round. Regression guards pass (retry-#5 fake, retry-#4 wrong service,
  retry-#3 glued echo, bare-dict criteria coercion). One older test
  updated: the memory-echo budget test now expects the numbered
  rejection, and the /tmp-write test now expects both the /tmp and the
  framework errors on the same Flask heredoc node. Log:
  freetalon-workspace/logs/planner-node-shape-fix-2026-11-16.log.
  Next: /health retry #8 (GPU load, separate step).

## Update - 2026-11-16 (evening, /health retry #8 — GPU load)

  FAILED in 22.0s, run run-20261117T071848Z-640cb5 (plan
  plan-7885a4ef9d99), goal verdict FAIL — escalated: re-planner
  failed. v1 was accepted on the first attempt but was a fake in a new
  disguise: node-1 echoed a JSON answer to stdout (no file write, so
  the stand-in rule did not fire) while claiming file_exists on
  /home/seeker/freetalon-workspace/health.json, which the command never
  writes; node-2 "write unit tests" was just a curl probe. Execution
  caught it — node-1 UNMEASURABLE (exit 0, 0.001s, health.json does not
  exist) — and the judge chose REPLAN. The replan then failed 3x: (1)
  it re-serialized the ROUTE GOAL shape prose as a tokenized JSON
  object (no nodes), (2) {"nodes": []}, (3) invented node/action/
  target/port fields with no id/objective. The node-shape fix (091ebfd)
  worked as designed — attempt 3 became a 16-error numbered rejection
  instead of a budget-burning raise — but the model never repaired it
  within the replan budget. Endpoint NOT extended, no new tests, suite
  470/470 green, no stray files. GPU survived (51C idle after, 300W,
  x16, zero Xid; peak temp not sampled mid-run — the parent runtime
  restarted during the run and polling was interrupted; fans restored
  to auto). Log: freetalon-workspace/logs/llm-end-to-end-health-
  retry8-2026-11-16.log. Next weakness: a file_exists criterion on a
  path the node never writes is unmeasurable by construction and
  should be rejected at plan time; the replan also needs a guard
  against re-serializing prompt prose as JSON.

## Update - 2026-11-16 (evening, planner unwritten-file + prose fix — CPU-only)

  Both retry-#8 weaknesses fixed. (1) A file_exists criterion on a
  path no node in the plan writes is now rejected at plan time: the
  check looks at the whole plan's writes (scaffold file_path, bash
  redirect, heredoc target), any node's bash command (the node runs or
  reads the file), or disk existence — listing the path in "outputs"
  alone does not count, which was the hole (node-1 echoed to stdout
  and listed health.json in outputs). Nodes with an unknown claw are
  skipped (the claw error already rejects them). (2) A payload with no
  "nodes" array whose keys read like prompt prose tokenized into JSON
  is rejected by name as a prose re-serialization; the ROUTE GOAL
  shape now forbids re-serializing the instructions as JSON, and the
  replan prompt rule covers any instruction text, not just the brief.
  Prompt text updated to match (outputs alone does not count; a later
  node may check a file an earlier node wrote). Tests 470 -> 484/484
  green (+14 new TestPlannerUnwrittenFileFix), M5 smoke passes,
  verbatim retry-#8 v1 plan now rejected with 1 error naming
  health.json and a mocked stdout-echo fake repairs to a grounded
  plan in one round; verbatim tokenized prose rejected by name.
  Regression guards pass (retry-#7 invented schema 11 errors,
  retry-#5 fake, retry-#4 wrong service, retry-#3 glued echo,
  bare-dict criteria coercion). Log:
  freetalon-workspace/logs/planner-unwritten-file-fix-2026-11-16.log.
  Next: /health retry #9 (GPU load, separate step).

## Update - 2026-11-16 (evening, /health retry #9 — GPU load)

  Retry #9 FAILED (run run-20261117T073452Z-2e7e88, 30.5s, escalated:
  re-planner failed — Planner response must include at least one plan
  node). The v1 plan was ACCEPTED on the first attempt, but node-1
  was a nonsense probe: it grepped for "def health(self)" on line 1
  of /home/seeker/freetalon-workspace/freetalon/api.py — a path that
  does not exist (the repo is /home/seeker/projects/FreeTalon, the
  workspace is /home/seeker/freetalon-workspace, and the model used
  the workspace path for the module). head failed, exit 1, node-1
  FAIL; node-2 (same probe shape against a nonexistent
  freetalon/tests/test_api.py) and node-3 (curl :8765/health) never
  ran. Judge chose REPLAN. Replan failed 3x: (1) invented
  node/action schema with no objective/assigned_claw, (2) a scaffold
  plan writing /health.py at the filesystem root (not
  freetalon/api.py), (3) {"nodes": []} — escalated FAIL. Endpoint NOT
  extended, no new tests, suite 484/484 green, no stray files. GPU
  survived (67C at fan restore, now 51C, 300W, x16, zero Xid; fans
  85% during, auto after). Log: freetalon-workspace/logs/llm-end-
  to-end-health-retry9-2026-11-16.log. Next weakness: the planner
  confuses the workspace path with the repo root when naming module
  paths — repo grounding should name the repo root explicitly, and
  the replan still burns its budget on invented schemas instead of
  repairing toward the grounded shape.

## Update - 2026-11-16 (evening, planner repo-root fix — CPU-only)

  Planner fix for the two weaknesses that killed /health retry #9.
  (1) Repo/workspace path confusion: the REPO CONTEXT now names the
  repo root explicitly ("Repo root: /home/seeker/projects/FreeTalon
  — code modules live here") and the workspace as NOT the repo root;
  the grounded move and the ROUTE GOAL shape use the absolute repo
  path of the serving module. New wrong-location validation: a node
  path under the workspace that runs inside a repo tree
  (freetalon/... or tests/) and does not exist on disk is rejected
  by name, with the repo root as the correct location — genuine
  generated files (test_health.py, health.json) at the workspace
  root are not flagged. (2) Replan budget: the replan prompt now
  carries the NODE SHAPE block on every replan (general, not
  route-scoped) alongside the route-scoped ROUTE GOAL shape, so the
  first replan attempt already has both shapes in front of it.
  Tests 484 -> 498/498 green (+14 new TestPlannerRepoRootFix), M5
  smoke passes, verbatim retry-#9 v1 plan now rejected at plan time
  with exactly 2 errors naming the wrong location and the repo root,
  mocked repair lands on a repo-pathed grounded plan in one round;
  regression guards pass (retry-#8 unwritten file, retry-#7 invented
  schema 11 errors, retry-#5 fake, retry-#4 wrong service, retry-#3
  glued echo, bare-dict criteria coercion). Demo:
  freetalon-workspace/tools/demo-repo-root.py, log
  freetalon-workspace/logs/demo-repo-root.log; apply script
  freetalon-workspace/tools/apply-planner-repo-root-fix.py (verified
  byte-for-byte against a pristine 3b0cd16 worktree). Log:
  freetalon-workspace/logs/planner-repo-root-fix-2026-11-16.log.
  Next: /health retry #10 (GPU load, separate step).

## Update - 2026-11-16 (evening, /health retry #10 — FAIL at planning)

  /health retry #10 FAILED at planning (~80s, DRIVER_EXIT:1, no run
  created). Pre-flight green (51C, 300W, x16, 0 Xid, head 9461914,
  498/498, Ollama gemma-3-abliterated:latest; fans 85% during,
  auto after). Intake succeeded. Planner attempts: (1) {"nodes":
  []} — empty; (2) the stdout-echo fake again (echo JSON answer to
  stdout, echo "# Unit tests..." as the "tests", no criteria on any
  node) — rejected; (3) a comment-only plan whose step_1/2 commands
  are just "# Add code..." comments, with file_exists criteria on
  the correct repo paths (/home/seeker/projects/FreeTalon/freetalon/
  api.py — the repo-root fix IS steering) — but step_2 depends_on
  ["endpoint_implemented"], an OUTPUT name, not a node id, so
  normalize_plan_nodes raised "Planner node 'step_2' depends on
  unknown node 'endpoint_implemented'" before validation could
  produce a numbered rejection -> repair budget exhausted. Judge/
  replan never ran, no run directory created. Endpoint NOT
  extended, no new tests, suite 498/498 green, no stray files. GPU
  survived (63C cooling at fan restore, 300W, x16, zero Xid).
  Log: freetalon-workspace/logs/llm-end-to-end-health-retry10-2026-
  11-16.log. Next weakness: the model keeps putting OUTPUT names
  in depends_on instead of node ids, and an unknown depends_on is
  still a budget-burning structural raise instead of a numbered
  rejection — same family as the bare-dict criteria fix (798a116)
  and the node-shape fix (091ebfd): show the depends_on shape
  (node ids only) and convert unknown-dependency errors into
  repairable rejections.

## Update - 2026-11-16 (evening, planner depends_on fix)

  Planner depends_on fix DONE (retry #10 weakness): the planner
  prompt (schema rule 5 + NODE SHAPE block), the replan prompt, and
  _NODE_SHAPE_EXAMPLE now show the depends_on shape -- node ids only
  (e.g. "step_1"), never an output name, a file path, or a
  description. Unknown-dependency errors are collected into ONE
  numbered rejection naming the node, the bad value, the available
  node ids, and -- when the value is an output of some node -- which
  node produces it and the id to depend on instead (retry-#10
  attempt 3: "endpoint_implemented" is an output of step_1, depend
  on step_1). Duplicate ids, dependency cycles, and self-dependency
  still raise first. Tests 498 -> 514/514 green (+16
  TestPlannerDependsOnFix), M5 smoke passes, verbatim retry-#10
  attempt-3 output now rejected with 2 errors (no structural raise),
  mocked repair lands on a grounded plan with correct depends_on in
  one round; regression guards pass (retry-#9 wrong location,
  retry-#8 unwritten file, retry-#7 invented schema 11 errors,
  retry-#5 fake, retry-#4 wrong service, retry-#3 glued echo,
  bare-dict criteria coercion; duplicate ids / cycles /
  self-dependency still raise). Demo:
  freetalon-workspace/tools/demo-depends-on.py, log
  freetalon-workspace/logs/demo-depends-on.log; apply script
  freetalon-workspace/tools/apply-planner-depends-on-fix.py
  (verified byte-for-byte against a pristine 3b17997 worktree).
  Log: freetalon-workspace/logs/planner-depends-on-fix-2026-11-16.log.
  Next: /health retry #11 (GPU load, separate step).

## Update - 2026-11-17 (early, /health retry #11 — FAIL at planning)

  /health retry #11 FAILED at planning (~50s, DRIVER_EXIT:1, no run
  created). Pre-flight green (51C, 300W, x16, 0 Xid, head 1cae79e,
  514/514, Ollama gemma-3-abliterated:latest; fans 85% during,
  auto after). Intake succeeded. Planner attempts: (1) {"nodes":
  []} — empty; (2) the stdout-echo fake again (step_1 echo JSON
  answer to stdout, step_2 echo "# Unit tests..." as the "tests",
  step_3 curl :8765/health, no criteria on any node, depends_on
  ["endpoint_implemented"] / ["tests_added"] — output names again)
  — rejected; (3) a NEW invented schema: nodes with
  "description"/"output" fields, no objective, no assigned_claw,
  no inputs, no criteria — but depends_on now uses correct node
  ids ["step_1"]/["step_2"] (the depends_on fix IS steering). The
  node-shape fix worked as designed: attempt 3 was an 8-error
  numbered rejection naming the missing objective and the unknown
  description/output fields, NOT a structural raise — but the
  repair budget was already spent. Judge/replan never ran, no run
  directory created. Endpoint NOT extended, no new tests, suite
  514/514 green, no stray files. GPU survived (59C peak sampled
  mid-run, 55C after, 300W, x16, zero Xid).
  Log: freetalon-workspace/logs/llm-end-to-end-health-retry11-2026-
  11-16.log. Next weakness: the model fixes the named error but
  drops the rest of the PlanNode shape — attempt 3 fixed depends_on
  and invented description/output nodes instead. Same family as
  the node-shape fix (091ebfd): the shape must survive repair
  rounds. Candidate: show the full minimal valid node in the
  repair prompt itself, so a repair that drops required fields
  (objective, assigned_claw, inputs, criteria) is corrected
  rather than invented around.

## Update - 2026-11-17 (early, planner shape-survives-repair fix)

  Planner shape-survives-repair fix DONE (retry #11 weakness): the
  repair prompt itself -- in _call_llm_with_repair, used by both
  plan_task_intent and replan_from_brief -- now carries the full
  NODE SHAPE block in every repair round, and tells the model every
  node must match it (do not drop a field from it, do not invent a
  field outside it). New _dropped_fields_note: when a repair round's
  output drops a required field the previous attempt had (objective,
  assigned_claw, inputs, criteria), the repair prompt names the
  dropped fields explicitly ("Your previous attempt had 'objective',
  'assigned_claw', 'inputs' on its nodes; do not drop them").
  Field names only, no quoted content. Tests 514 -> 531/531 green
  (+17 TestPlannerShapeSurvivesRepair); M5 smoke OK (finance
  run-20261117T081628Z-11da5e, ECU run-20261117T081628Z-e6cb04).
  Demo: retry-#11 attempt-3 output rejected with 8 errors naming
  the shape, mocked repair lands on a grounded plan in one round,
  and a [fake, invention, grounded] sequence repairs within the
  2-repair budget with the dropped fields named on the second round.
  Demo: freetalon-workspace/tools/demo-shape-survives-repair.py, log
  freetalon-workspace/logs/demo-shape-survives-repair.log; apply
  script freetalon-workspace/tools/apply-planner-shape-survives-
  repair-fix.py (verified byte-for-byte against a pristine 0758177
  worktree). Log: freetalon-workspace/logs/planner-shape-survives-
  repair-fix-2026-11-16.log. Known remaining gaps (pre-existing,
  not fixed here): file_matches has the same outputs-only hole as
  file_exists had; comment-only commands are not validated as
  no-ops. Next: /health retry #12 (GPU load, separate step).

## Update - 2026-11-17 (early, /health retry #12)

  /health retry #12 FAILED at planning (DRIVER_EXIT:1, ~70s, no
  run created). Pre-flight green (51C idle, 300W, x16, zero Xid,
  head 7c11000, suite 531/531). Intake succeeded. Three planner
  attempts, all rejected: (1) {"nodes":[]} -- empty plan;
  (2) the stdout-echo fake in a new disguise -- echo of a JSON
  answer redirected INTO freetalon/api.py itself (would have
  overwritten the serving module with a JSON literal), node ids
  as integers (1, 2) and depends_on [1] -- rejected with 3 errors
  (id must be a valid string x2, depends_on.0 must be a valid
  string); the stand-in rule did NOT fire (criteria is exit_code,
  not file_exists), so only the int-id type error caught it --
  had the ids been strings, the plan may have been accepted;
  (3) a scaffold plan writing a Flask app to freetalon/api.py,
  otherwise well-shaped (correct repo paths, string ids, correct
  node-id depends_on, objective/assigned_claw/inputs/criteria
  present -- the shape fixes ARE steering) -- rejected with 1
  error by the framework rule, naming stdlib http.server.
  Repair budget (max 2) was already spent on the first two junk
  attempts, so the model never got a repair round for the Flask
  error; driver raised at plan_task_intent (planner.py:1794).
  Judge/replan never ran. Endpoint NOT extended, no new tests,
  suite 531/531 green after, no stray files. GPU survived (65C
  at fan restore, now 51C idle, 300W, x16, zero Xid; fans auto).
  Log: freetalon-workspace/logs/llm-end-to-end-health-retry12-
  2026-11-16.log. Next weakness: an echo of a literal answer
  INTO a file with an exit_code criterion slips past the stand-in
  rule (it only fires on a file_exists claim on the same file) --
  the validator should reject writing a literal JSON answer into
  any file, and/or reject overwriting the serving module with
  non-code content. Second: the first two junk attempts burn the
  repair budget before a real (single-error, repairable) rejection
  arrives -- consider a repair round reserved for the last
  rejection, or rejecting empty/echo plans without spending the
  full budget.
