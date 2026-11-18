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
- Planner forbidden-steer fix DONE (retry #19): probe confirmed hypothesis (b) - the repair prompt named Flask as forbidden and the live model wrote a Flask scaffold anyway. Forbidden approaches now carry their concrete replacement, the Flask entry is followed by the grounded-move sentence, and the block states the consequence plus the repair count. Suite 737/737 green, M5 smoke passes, demo tools/demo-forbidden-steer.py ALL CHECKS PASSED.


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

## planner stand-in-overwrite + free-reprompt fix (retry #12)

  Done 2026-11-16 evening (commit pending): the stand-in rule is
  widened -- a literal JSON answer written into a CODE file (.py and
  friends) is rejected by name as overwriting code with a hardcoded
  answer, regardless of criteria, for bash and scaffold writes alike
  (data/generated files exempt: a JSON config to a new file is
  legitimate). Cheap rejections -- an empty plan, or a rejection
  whose only errors are stand-in fakes -- are re-prompted for free,
  up to 2 free re-prompts (CheapRejectionError); the max-2 repair
  budget is NOT raised. Tests 531 -> 555/555 green (+24
  TestPlannerStandinOverwriteFix), M5 smoke passes, retry-#12
  attempt-2 string-ids version now rejected with 1 error naming the
  code-overwrite, mocked [empty, fake, Flask] sequence repairs to a
  grounded plan in 4 calls. Demo: tools/demo-standin-overwrite.py.
  Known remaining gap: a literal JSON answer to a NEW non-code file
  with only an exit_code criterion still passes validation.
  Next: /health retry #13.

## /health retry #13 (2026-11-16 evening)

  FAILED at planning (DRIVER_EXIT:1, ~86s, no run created). Pre-flight
  green (52C, 300W, x16, 0 Xid, head 51bdfc3, 555/555; fans 85%).
  Intake succeeded. Four planner calls: (1) {"nodes":[]} -- empty plan,
  CHEAP rejection, free re-prompt (the new fix worked); (2) the
  retry-#12 echo fake verbatim (echo JSON answer > freetalon/api.py,
  int ids) -- rejected with 3 schema errors (id/depends_on must be
  strings); the code-overwrite stand-in error did NOT fire because
  schema validation raises before the stand-in check runs, so this
  one still spent a repair round -- the cheap path only triggers when
  the stand-in error is the ONLY error; (3) a scaffold plan writing
  a Flask app to freetalon/api.py -- otherwise well-shaped (correct
  repo paths, string ids, correct node-id depends_on, all fields
  present; the shape fixes ARE steering) -- rejected with 1 error by
  the framework rule; (4) a MORE elaborate Flask scaffold (Ollama and
  nvidia-smi subprocess checks, app.run on 8765) -- rejected with 1
  error by the framework rule again. The model got a repair round for
  the Flask error and used it to double down on Flask instead of
  switching to http.server; budget exhausted -> driver raised at
  plan_task_intent (planner.py:1890). Judge/replan never ran.
  Endpoint NOT extended, no new tests, suite 555/555 green after, no
  stray files. GPU survived (56C, 300W, x16, zero Xid; fans auto).
  Log: freetalon-workspace/logs/llm-end-to-end-health-retry13-
  2026-11-16.log. Note: an early --help probe accidentally started a
  real run that head -30 killed mid-intake (SIGPIPE); it created no
  run dir and left nothing behind; the real run was re-launched after
  a 60s idle. Next weakness: the model converges on the Flask
  scaffold and doubles down across repair rounds instead of switching
  to http.server -- the framework rejection should name the grounded
  move concretely (extend api.py's existing http.server handler in
  place; add a do_GET branch for /health), the way the anti-fake
  rejection names the grounded move. Secondary: the stand-in check
  should run even when schema errors are present, so the int-id echo
  fake is a cheap rejection instead of a repair-consuming one.

## planner framework-steer + stand-in-despite-schema fix (retry #13)

  Planner fix for the two weaknesses that killed /health retry #13.
  (1) Framework rejection names the grounded move: the repo index now
  records each route's request handler (for /health, the do_GET method
  of the Handler class defined inside HiveAPIServer.start in
  freetalon/api.py), and the framework rejection names the grounded move
  concretely -- extend freetalon/api.py in place, add a /health branch
  to that do_GET method -- the way the anti-fake rejection names the
  grounded move; the ROUTE GOAL shape carries the same concrete move,
  and the repo context names the handler per route. (2) Stand-in check
  runs despite schema errors: _validate_raw_nodes runs it on lenient
  views of the raw nodes, so a rejection containing a stand-in error is
  cheap regardless of accompanying schema errors -- the int-id echo
  fake is now a free re-prompt, not a repair round. Genuinely structural
  errors (bad JSON, duplicate ids, dependency cycles, self-dependency)
  still raise first.
  Tests 555 -> 579/579 green (+24 new TestPlannerFrameworkSteerFix),
  M5 smoke passes (finance run-20261117T085448Z-da9c48, ECU
  run-20261117T085448Z-7eacd6), verbatim retry-#13 attempt-2 output now
  a cheap rejection (4 errors: 1 stand-in + 3 schema) naming the fake
  and the grounded move, attempt-4 Flask scaffold rejected with 1 error
  naming the handler branch, mocked repair lands on a grounded plan in
  one round, [empty, fake, Flask, grounded] repairs in 4 calls;
  regression guards pass (retry-#12 code-overwrite, retry-#11 invented
  schema 8 errors, retry-#10 unknown dependency, retry-#9 wrong
  location, retry-#8 unwritten file, retry-#7 invented schema 11
  errors, retry-#5 fake, retry-#4 wrong service, retry-#3 glued echo,
  bare-dict criteria coercion; duplicate ids, cycles, self-dependency
  still raise first). Demo:
  freetalon-workspace/tools/demo-framework-steer.py, log
  freetalon-workspace/logs/demo-framework-steer.log; apply script
  freetalon-workspace/tools/apply-planner-framework-steer-fix.py
  (verified byte-for-byte against a pristine b79ac6f worktree). Log:
  freetalon-workspace/logs/planner-framework-steer-fix-2026-11-16.log.
  Next: /health retry #14 (GPU load, separate step).

## /health retry #14 (2026-11-17, run run-20261117T090107Z-c77c7a)

  Goal FAIL in 54.7s -- escalated: node 'node-1' unmeasurable twice
  in a row; the plan is underspecified. First retry where a plan was
  ACCEPTED on the first attempt AND executed through the loop with
  replans (v1 -> v2 -> v3). Intake succeeded cleanly (valid
  TaskIntent, no echo). v1 plan plan-7885a4ef9d99 (3 bash nodes,
  correct node-id depends_on, exit_code criteria) -- the
  framework-steer fix IS working: node-1's command names the real
  handler (HiveAPIServer, do_GET of the Handler class inside
  HiveAPIServer.start, absolute repo path). But the "command" was
  prose, not shell -- /bin/sh: Syntax error: "(" unexpected, exit 2
  in 0.001s. Judge chose REPLAN. The v1->v2 replan: a scaffold of
  '/health' with a JSON literal was rejected by the duplicate-route
  rule (scaffolding '/health' duplicates the existing /health route
  in freetalon/api.py -- extend it instead); v2 accepted: scaffold
  freetalon/api.py with "# Scaffolded code for /health endpoint."
  (39 bytes, status success) -- measurement UNMEASURABLE (no exit
  code reported). Judge chose REPLAN. The v2->v3 replan: a single-node
  object (prose re-serialization, rejected by name) and {"nodes":[]}
  (rejected) before v3 accepted: one scaffold node, absolute
  file_path /home/seeker/projects/FreeTalon/freetalon/api.py, content
  "# Placeholder for health endpoint logic." (40 bytes, status
  success) -- measurement UNMEASURABLE again. Judge ESCALATED:
  node-1 unmeasurable twice in a row.
  The v3 scaffold OVERWROTE the real freetalon/api.py with the
  1-line placeholder comment (219 lines -> 1 line, 1 insertion /
  219 deletions); the loop never noticed, because scaffold nodes
  report no exit code. api.py was restored with git checkout --
  freetalon/api.py (tracked, committed at 5a82b74); suite re-run
  579/579 green after the restore; tree clean apart from this file.
  Endpoint NOT extended, no new tests, no live curl (node-3 never
  ran). No stray files (health.json, api.json, test.sh,
  test_health.py, /health all absent). Log:
  freetalon-workspace/logs/llm-end-to-end-health-retry14-2026-11-16.log
  (259 lines); run dir
  freetalon-workspace/runs/run-20261117T090107Z-c77c7a (plan-v001 /
  v002 / v003.json, measurements.jsonl, memory.jsonl).
  GPU survived: mid-run peak sampled 66C at 260.53W, post-run 61C at
  fan restore (cooling), now idle, limit 300W holding, link x16,
  zero Xid 79/154; fans manual 85% during, restored to auto after.
  No thermal pause (never >=85C), no fall-off.
  Next weakness (for the parent to decide): (1) scaffold nodes are
  UNMEASURABLE -- the exit_code criterion cannot measure scaffold
  work, so the loop escalates without learning anything; scaffold
  should report an exit code / a file-written measurement. (2) The
  scaffold claw overwrites an existing code file with placeholder
  content and no guard -- a scaffold write to an existing module
  should be refused unless the content is real code (or at least
  not a comment-only placeholder), and an absolute file_path
  reaching outside the run dir into the repo needs a guard. (3) The
  v1 prose-command problem remains: the grounded move names the
  handler, but the model still emits a description in the command
  field instead of an executable edit (heredoc/scaffold with real
  code) -- bash commands that are prose descriptions should be
  rejected at plan time. Known gaps not exercised: file_matches
  outputs-only hole; literal JSON to a new non-code file with only
  an exit_code criterion still passes; comment-only commands not
  validated as no-ops.
  Next: planner fix for scaffold measurability + overwrite guard,
  then /health retry #15.

## Planner scaffold-guard fix (2026-11-17, retry #14 weaknesses)

  Three fixes for the three weaknesses that killed retry #14 (v1 prose
  command, v2/v3 placeholder scaffolds, unmeasurable scaffold nodes).
  (1) The scaffold claw now returns exit_code 0 on a successful write,
  so an exit_code or file_exists criterion on a scaffold node measures
  PASS instead of UNMEASURABLE. (2) Placeholder guard, both ends: at
  plan time a scaffold whose content is comment-only / docstring-only
  is rejected when its target is an EXISTING code file (new files may
  still start as a comment stub); at execution time the scaffold claw
  refuses to overwrite an existing code file with placeholder content
  -- the refusal surfaces as a measured FAIL and the file is unchanged.
  Absolute repo paths are NOT banned (repo edits are the point); the
  content guard applies regardless of path. (3) At plan time a bash
  command that is prose, not shell, is rejected by name -- comment-only
  commands, and segments that read as sentences (code citation first
  token, or capitalised first word with sentence markers) -- naming the
  grounded move (heredoc or scaffold with real code). Legit commands
  (curl, python3, nvidia-smi, heredocs, assignments) still pass.
  Helpers moved: CODE_FILE_SUFFIXES / is_code_path /
  is_placeholder_content now live in tool_registry.py (planner imports
  them); prompts gain a prose + placeholder rule. The grounded-plan
  fixtures now carry real code (a do_GET-branch fragment) instead of
  the "# extended health" comment stub, and the 10 demo tools' grounded
  fixtures were updated the same way.
  Tests 579 -> 605/605 green: new TestPlannerScaffoldGuardFix (20
  tests: verbatim retry-#14 v1 rejected with exactly 3 prose errors,
  v2 with 2 errors, v3 with 1 error naming freetalon/api.py; mocked
  prose and placeholder repairs land on the grounded plan in one
  round; 15 regression guards covering retries #3-#13, bare-dict
  criteria, duplicate ids / cycles / self-dependency) and
  TestScaffoldGuardExecutor (6 tests: scaffold measures PASS on
  exit_code + file_exists, placeholder overwrite refused at handler
  and at executor as a measured FAIL with the file unchanged, real
  code overwrite succeeds, comment stub to a new file succeeds).
  M5 smoke passes. New demo tools/demo-scaffold-guard.py passes (v1
  3 prose errors, v3 1 placeholder error, scaffold measures PASS);
  all 12 existing demos re-run and pass. One pre-existing demo
  failure found and fixed along the way: demo-standin-overwrite.py
  asserted "3 errors" for the int-id attempt-2, stale since the
  framework-steer fix made it a 4-error cheap rejection (1 stand-in
  + 3 schema) -- assertion corrected to 4 errors; it failed the same
  way on pristine 2ee4744, so not caused by this fix.
  freetalon/api.py untouched (0-line diff vs HEAD). No LLM calls, no
  GPU, no e2e driver, no reboot, no power-limit change. GPU baseline
  after the fix: 46C, 42.91W, limit 300W, link x16, zero Xid.
  Known gaps deliberately left out: file_matches outputs-only hole;
  a literal JSON answer written to a NEW non-code file with only an
  exit_code criterion still passes validation; repair budget (max 2)
  not raised.
  Next: /health retry #15.

## /health retry #15 (2026-11-17, DRIVER_EXIT:1, no run created)

  Verdict: FAIL at planning (~2 min, 3 planner calls, no run created).
  Pre-flight green: GPU 46C, 43.00W, limit 300.00W, link x16, zero Xid
  79/154; Ollama reachable with gemma-3-abliterated:latest; git head
  174d7ec, tree clean; suite 605/605 green. Fans manual 85% during
  the run, restored to auto after. Log:
  logs/llm-end-to-end-health-retry15-2026-11-16.log (181 lines).
  Intake succeeded cleanly (valid TaskIntent, no echo).
  Attempt 1: the prose-command plan again -- step_1/2/3 commands are
  handler-citation prose ("/home/seeker/projects/FreeTalon/freetalon/
  api.py:HiveAPIServer, _ThreadingHTTPServer - Add a /health branch to
  the do_GET method..."). Rejected with exactly 3 errors by the new
  prose rule (174d7ec working as designed), each naming the grounded
  move (heredoc or scaffold with real code).
  Attempt 2: a Flask heredoc plan (cat > /tmp/freetalon_api.py with a
  Flask app + psutil, test file, curl checks) -- but MALFORMED: the
  three nodes' fields are concatenated into a single object (the
  "},{" separators between nodes are missing, and outputs/acceptance/
  criteria are nested inside inputs). Duplicate-key last-wins
  collapsed it to a 1-node plan (node-3, depends_on ["node-2"]) ->
  rejected with 1 error, unknown dependency -- which names NOTHING
  about the real defect (no Flask, no /tmp, no missing separators).
  The framework and /tmp rules never fired because normalize raised
  the dependency error before _collect_plan_errors ran.
  Attempt 3: the echo fake to /tmp (echo JSON > /tmp/health.json;
  glued echo test into /tmp/test.sh; /bin/bash /tmp/test.sh && curl).
  Rejected with 4 errors (glued words on node-2, code-overwrite
  stand-in naming the grounded move, /tmp write x2). Budget exhausted
  (initial + 2 repairs = 3 calls; no free re-prompts used -- no empty
  plan, no stand-in-only rejection) -> driver raised at
  plan_task_intent (planner.py:2150 via _call_llm_with_repair line
  2106 raising last_error). Judge/replan never ran.
  Endpoint NOT extended. freetalon/api.py untouched (219 lines,
  0-line diff vs HEAD) -- the minimal /health at api.py:44-45
  ({"ok": True, "status": "ready"}) is still the only one. No new
  tests written. Full suite re-run after the failure: 605/605 OK.
  No live curl happened -- no plan was ever accepted, so nothing ran.
  No stray files: health.json, api.json, test.sh, test_health.py,
  health.py all absent from the workspace; /tmp/health.json,
  /tmp/test.sh, /tmp/freetalon_api.py absent too.
  GPU survived: mid-run peak sampled 62C at 258.11W; post-run 40C
  idle, 42.60W, limit 300W holding, link x16, zero Xid 79/154. No
  thermal pause triggered (never >=85C), no fall-off. Fans confirmed
  auto.
  Next: planner fix for duplicate-key / merged-node collapse (a node
  object, or the nodes array, with repeated keys must be rejected by
  name, naming the duplicated keys and the likely cause -- missing
  "},{" between nodes, outputs/acceptance/criteria nested inside
  inputs -- instead of collapsing silently and surfacing only a
  downstream dependency error), and for the framework//tmp signal
  being lost when normalize raises before _collect_plan_errors runs.

## Planner duplicate-keys fix (2026-11-17, retry #15 weaknesses)

  Two fixes for the two weaknesses that killed retry #15 (merged-node
  collapse, framework//tmp signal lost).
  (1) Duplicate-key detection: the raw planner output is scanned for
  duplicate JSON keys (object_pairs_hook) before the normal parse
  collapses it; a node object (or the top-level object) with repeated
  keys is rejected by name, naming the duplicated keys and the likely
  cause (missing `},{` separators between nodes; outputs/acceptance/
  criteria nested inside `inputs`). Scoped to node-shaped objects only,
  so a flat prose object whose keys are repeated words (retry #8
  tokenized prose) is still rejected as a prose re-serialization, not
  shadowed by a duplicate-key error; and nested fields only count
  inside an actual `inputs` dict, so a node that merely has command
  and criteria at the same level (retry #7 invented schema) is still a
  schema error, not a nesting.
  (2) Dependency rejection carries the validation errors: when
  normalize raises an unknown-dependency error, the rejection also
  names the plan's other validation errors (framework, /tmp, prose,
  stand-in, route engagement, unwritten file) found in the lenient
  node views, so the repair round sees the real defects in the same
  rejection. Duplicate ids, dependency cycles, and self-dependency
  still raise first, as before.
  Retry-#10 count changed by design: the attempt-3 rejection is now 7
  errors (2 unknown-dependency + 2 unwritten file_exists + 2
  comment-only commands + 1 bad criterion type) instead of 2; the two
  count assertions were updated with a comment explaining the 7.
  Tests 605 -> 626/626 green: new TestPlannerDuplicateKeysFix (21
  tests: verbatim retry-#15 attempt-2 rejected by name as a
  duplicate-key collapse, naming the duplicated keys and the likely
  cause, not only as an unknown dependency; mocked merged-node repair
  lands on the grounded plan in one round; unknown dependency AND
  framework//tmp named in the same rejection; valid grounded plan
  passes; 16 regression guards covering retries #3-#14, bare-dict
  criteria, duplicate ids / cycles / self-dependency) and the two
  retry-#10 count assertions updated to 7 errors.
  M5 smoke passes (finance run-20261117T101828Z-503d07, ecu
  run-20261117T101828Z-f455ae). New demo tools/demo-duplicate-keys.py
  passes (attempt-2 rejected by name as a duplicate-key collapse;
  dependency + framework//tmp in one rejection; mocked repair lands
  in one round); log logs/demo-duplicate-keys.log.
  freetalon/api.py untouched (0-line diff vs HEAD). No LLM calls, no
  GPU, no e2e driver, no reboot, no power-limit change.
  Known gaps deliberately left out: file_matches outputs-only hole;
  a literal JSON answer written to a NEW non-code file with only an
  exit_code criterion still passes validation; repair budget (max 2)
  not raised.
  Transport note: heredoc delivery strips single-quote characters
  from the transported text (the first helper draft lost its quotes
  and had obj['id'] mangled to obj[id]); the fix and the tests were
  written locally and transferred with scp, which preserves bytes
  exactly. The helper now avoids literal single quotes in source
  (uses repr() / !r for quoted output).
  Next: /health retry #16.

## /health retry #16 (2026-11-17, DRIVER_EXIT:0, goal FAIL, run run-20261117T102447Z-e8bfa4)

  Verdict: FAIL -- escalated: re-planner failed: "Planner response must
  include at least one plan node." First retry since #8 where a plan was
  accepted AND executed. Elapsed: ~2 min planning + 9.1s execution.
  Pre-flight green: GPU 47C, 43.19W, limit 300.00W, link x16, zero Xid
  79/154; Ollama reachable with gemma-3-abliterated:latest; git head
  b66c5ac, tree clean; suite 626/626 green. Fans manual 85% during
  the run, restored to auto after. Log:
  logs/llm-end-to-end-health-retry16-2026-11-16.log (321 lines).
  Intake succeeded cleanly (valid TaskIntent, no echo).
  Attempt 1: the prose-command plan again -- step_1/2/3 commands are
  handler-citation prose (api.py:HiveAPIServer, _ThreadingHTTPServer -
  Add a /health branch to the do_GET method...). Rejected with exactly
  3 errors by the prose rule (174d7ec working as designed), each naming
  the grounded move (heredoc or scaffold with real code).
  Attempt 2: the retry-#15 Flask heredoc plan again (cat >
  /tmp/freetalon_api.py, Flask + psutil, test file, curl checks) --
  still malformed, nodes concatenated into a single object (missing
  "},{" separators, outputs/acceptance/criteria nested inside inputs).
  Rejected with exactly 2 errors by the NEW duplicate-keys fix
  (b66c5ac working as designed): duplicate JSON keys in node object
  (id 'node-3'): 'id'/'objective'/'depends_on'/'assigned_claw'/
  'inputs' each 3 times, naming the missing "},{" separators, plus the
  nesting error. NOT named in this rejection: the Flask content and
  the /tmp paths -- the duplicate-key check raises before
  _collect_plan_errors runs, so the framework//tmp signal is still
  lost on this path (b66c5ac weakness-2 fix covers only the
  unknown-dependency raise, not the duplicate-key raise).
  Attempt 3: ACCEPTED -- plan-7885a4ef9d99, 3 bash nodes, exit_code
  criteria. node-1: echo '# /health endpoint' >> freetalon/api.py.
  node-2: echo 'def health():\nreturn "OK"' >> freetalon/api.py.
  node-3: curl http://localhost:8765/health | grep "OK".
  Execution: node-1 PASS (exit 0, 0.001s), node-2 PASS (exit 0,
  0.001s), node-3 FAIL (exit 1, 0.006s). Judge chose REPLAN
  (informative failure: node-3 failed criterion exit_code 1 != 0).
  Replan: 5 x {"nodes":[]} -- the re-planner returned empty plans 5
  times in a row (initial + 2 free cheap re-prompts + 2 repairs) ->
  escalated FAIL. The model gave up entirely after the informative
  failure.
  The v1 plan CORRUPTED freetalon/api.py while "passing": node-1 +
  node-2 appended 3 lines (# /health endpoint, def health():, return
  "OK" at column 0) -- an IndentationError (expected an indented block
  after function definition on line 221). exit_code criteria passed
  while the module broke: suite run against the corrupted file was
  608 tests, 18 failures + 2 errors (vs 626/626 before). The loop
  never noticed, because no node measured the file's validity. The
  file was restored with git checkout -- freetalon/api.py (tracked,
  committed at b66c5ac); suite re-run 626/626 green after the restore.
  Node-3 failed because nothing serves :8765 -- the plan never starts
  the API server, so curl has nothing to hit and grep finds no "OK".
  Endpoint NOT extended (the minimal /health at api.py:44-45 is still
  the only one). No new tests written. No live curl succeeded. No
  stray files: health.json, api.json, test.sh, test_health.py,
  health.py absent from the workspace; /tmp/health.json, /tmp/test.sh,
  /tmp/freetalon_api.py absent too.
  GPU survived: post-run 60C cooling at first check, then 51C idle,
  43.12W, limit 300W holding, link x16, zero Xid 79/154. No thermal
  pause triggered (never >=85C), no fall-off. Mid-run peak not
  sampled (execution phase only 9.1s). Fans confirmed auto.
  Next: planner fix for (1) bash appends/redirects that write code
  into an EXISTING code file -- the placeholder guard covers scaffold
  only, and the stand-in rule fires only on JSON literals, so an
  echo-append of broken code with only an exit_code criterion is
  accepted and measured PASS while corrupting the module; a node that
  modifies a code file should have to measure the file still parsing
  (or the suite still green); (2) the re-planner returning empty plans
  5 times -- the model gives up after an informative failure and the
  escalation carries no grounded guidance; (3) the duplicate-key
  rejection not naming Flask//tmp (secondary -- the structural defect
  IS named, but the framework//tmp defects never reach the repair
  round on this path).

## Update - 2026-11-17 (early, planner code-append fix)

- Code-append fix DONE (CPU-only, no LLM/e2e/push/reboot): the three
  retry-#16 weaknesses are fixed in freetalon/orchestrator/planner.py.
  (1) Bash writes into an EXISTING code file are now guarded: for
  Python the write is simulated at plan time (existing file + appended
  lines, dash-echo escapes interpreted the way execution interprets
  them) and the result must still parse with ast.parse; for other code
  files (.js, .sh, ...) the plan must contain a node that measures the
  file still working (tests_pass, a test-suite run, or py_compile on
  the file). The exact retry-#16 v1 plan is now rejected with 1 error
  naming node-2, freetalon/api.py, and the grounded move. Legit writes
  still pass: heredoc/scaffold of real code to a NEW file, writes to
  non-code files, writes followed by a validity check. (2) After any
  empty reply the repair prompt carries a CONCRETE PLAN SKELETON for
  the route goal (a fill-in-the-blanks 4-node shape with the serving
  module, a test file, a test run, and a live curl -- a template to
  complete, not a canned plan); an escalation on empty plans names the
  grounded move. Mocked empty x2 then grounded lands in 3 calls;
  mocked empty x5 escalates with the grounded move named (5 calls).
  (3) The duplicate-key rejection now also carries the per-node
  validation errors from the split nodes: verbatim retry-#15
  attempt-2 is rejected with 7 errors -- duplicate keys, nesting,
  2 parse errors, 2 /tmp errors, 1 Flask error. Also fixed: an
  unterminated heredoc body now runs to the end of the command, so the
  framework//tmp rules can see it; and repo_root is threaded from
  _call_llm_with_repair into parse_planner_response.
- Bugs found by the fix's own verification (all fixed, all now
  covered by tests): the echo-write regex matched an empty op (the
  first ">" belongs to the (?P<op> group syntax -- use >{1,2}); the
  regex could not span the real newline inside retry-#16 node-2's
  quoted literal (needs re.DOTALL); the split/hoist in
  _duplicate_key_errors popped fields out of the inputs dicts before
  the nesting check read them, so the nesting error vanished.
- Tests: 653/653 green (626 + 27 new TestPlannerCodeAppendFix).
  M5 smoke passes (finance run-20261117T110403Z-9b33db, ECU
  run-20261117T110403Z-e49f2b). Demo tools/demo-code-append-guard.py
  passes all checks (log logs/demo-code-append-guard.log, 54 lines):
  retry-#16 v1 rejected by name, retry-#15 attempt-2 rejected naming
  both duplicate keys and Flask//tmp, mocked empty-replan sequence
  lands on a grounded plan.
- freetalon/api.py untouched (219 lines, 0-line diff vs HEAD). No
  stray files. GPU baseline: 47C, 43.01W, limit 300W, link x16,
  zero Xid 79/154.
- Known remaining gaps (not fixed): file_matches has the same
  outputs-only hole the unwritten-file fix closed for file_exists; a
  literal JSON answer written to a NEW non-code file with only an
  exit_code criterion still passes; a scaffold whose content is real
  code but syntactically invalid (retry-#13 attempt-4's Flask app) is
  not parse-checked; a scaffold-with-fragment overwriting a whole
  module is a pre-existing hazard (the scaffold handler replaces the
  file; the ROUTE GOAL shape already steers there).
- Next: /health retry #17 on the fixed planner.
- CI-only test failure fixed (Tests run 44065722972 on the ca55456 push:
  3 failures, GPU tests green): the retry-#10 attempt-3 count assertions
  expected 7 errors but CI saw 9. Root cause, not a validator bug: the
  fixture names the absolute seeker7 path
  /home/seeker/projects/FreeTalon/freetalon/api.py, and
  _unwritten_file_errors fires only when that path does not exist on
  disk -- on seeker7 it exists (7 errors), in CI the checkout lives
  elsewhere (9: the same 7 plus one unwritten-file error per node).
  Fix, tests only: new _expected_retry10_error_count() helper (7 where
  the fixture path exists, 9 elsewhere) used by all 4 retry-#10 tests.
  A second disk-dependent wording turned up in the same audit: the
  code-append check says "no longer parses as Python" against an
  existing file but "does not parse as Python" against content alone,
  so the 2 retry-#16 tests now assert either phrasing via
  _assert_code_append_parse_error(). The retry-#15 test (7 errors) is
  unchanged -- probed stable at 7 in both disk states. Full-module
  audit under a simulated-CI stub leaves only 5 failures, all known
  false positives: their fixtures use __file__-derived paths, which
  exist in real CI under /home/runner (4 of them passed in the real CI
  626 run; the 5th uses the identical fixture as one that did).
- Tests: 653/653 green, no new tests. M5 smoke passes (finance
  run-20261117T111951Z-b4fc9a, ECU run-20261117T111951Z-33abf7). The 6
  fixed tests pass both unpatched on seeker7 and under the simulated-CI
  stub. freetalon/api.py untouched (219 lines, 0-line diff vs HEAD).
  GPU baseline: 46C, 42.95W, limit 300W, link x16, zero Xid 79/154.
- Next: push (CI check for this fix), then /health retry #17.
- /health retry #17 FAILED at planning (DRIVER_EXIT:1, ~145s, no run
  created; log logs/llm-end-to-end-health-retry17-2026-11-16.log, 317
  lines). Pre-flight green (49C, 43.22W, limit 300W, link x16, zero Xid;
  Ollama gemma-3-abliterated:latest; head 49a6a38; suite 653/653; fans
  85%). Intake succeeded. Four planner attempts, all rejected (each
  re-run through parse_planner_response to confirm exactly):
  (1) The prose-command plan again -- rejected with exactly 3 errors by
  the prose rule, each naming the grounded move (heredoc or scaffold
  with real code). (2) The retry-#15 Flask heredoc plan again --
  rejected with exactly 7 errors: duplicate keys
  (id/objective/depends_on/assigned_claw/inputs each 3 times, naming
  the missing },{ separators), nesting, 2 parse errors (the heredoc
  bodies do not parse as Python -- unterminated string literal), 2 /tmp
  errors, 1 Flask error. The b66c5ac weakness-3 fix AND the 1cfcc20
  code-append parse check both worked as designed -- the structural
  defect IS named and so are Flask//tmp. (3) {"nodes":[]} -- empty plan,
  EmptyPlanError, cheap rejection, free re-prompt; the repair prompt
  carried the concrete grounded skeleton. (4) A skeleton-shaped plan
  (step-1 scaffold api.py, step-2 write tests, step-3 run tests,
  step-4 curl live) -- the model DID follow the skeleton shape after
  the empty plan, but filled it with placeholder comments: rejected
  with 1 error, node-1 scaffolds api.py with placeholder content only
  ("# TODO: replace this comment with the real code ..."). The
  placeholder guard fired as designed. Budget exhausted (initial + 2
  repairs spent on prose and Flask, 1 free re-prompt on the empty plan;
  the placeholder rejection needed a 3rd repair) -> driver raised at
  plan_task_intent. Endpoint NOT extended, no new tests, no live curl.
  freetalon/api.py untouched (219 lines, 0-line diff). No stray files.
  GPU survived (post-run 71C at 238.58W, then 56C cooling, 300W, x16,
  zero Xid; fans auto). Suite re-run 653/653 green.
- Next weaknesses, in priority order: (1) The model follows the
  grounded skeleton shape but fills it with placeholder comments -- the
  placeholder guard catches the api.py scaffold, but the repair budget
  is already spent on prose + Flask by then, so the model never gets a
  repair round for the placeholder error. Consider making the
  placeholder rejection cheap (free re-prompt, like empty plans and
  stand-in fakes), or giving the skeleton repair prompt a concrete
  code sketch the model completes instead of a blank to fill. (2) The
  Flask attempt keeps returning verbatim despite the framework//tmp
  rejections naming the fix -- the model never learns across attempts
  within one planning call; the repair prompt may need to forbid the
  previously rejected approach by name. (3) The placeholder rejection
  names the node 'node-1' while the plan's id is 'step-1' (lenient-view
  naming quirk, cosmetic).
- Next: fix weakness (1), then /health retry #18.


## Update - 2026-11-17 (early, planner placeholder-cheap fix)

- Placeholder-cheap fix DONE (CPU-only, no LLM/e2e/push/reboot): the
  three retry-#17 weaknesses are fixed in
  freetalon/orchestrator/planner.py and repo_index.py. (1) A rejection
  whose only errors are placeholder scaffolds is now CHEAP (free
  re-prompt, like empty plans and stand-in fakes) -- the model gets a
  repair round for the placeholder error instead of the budget dying
  on prose + Flask first. The grounded skeleton's step-1 also shows
  the existing handler branch as a commented sketch (new
  RepoIndex.route_branches, read from the real module -- for /health
  exactly 'if path == "/health": return self._send(200, {...})'), so
  the model has real code to extend instead of a blank TODO; a
  verbatim copy of the sketch is still placeholder-only, so the guard
  rejects it cheaply. The placeholder error now names the grounded
  move for route goals, like the stand-in rejection. (2) The repair
  prompt names the approaches already tried and rejected in this
  planning call (Flask, /tmp writes, placeholder scaffolds, prose
  commands, hardcoded answers) -- approach names only, never quoted
  model content -- so the model is told not to repeat them. (3) The
  rejection names the model's own node ids: parse_planner_response
  collects errors on copies of the nodes relabeled to their original
  ids ('step-1', not 'node-1'); the returned nodes keep normalized
  ids. The repair budget (max 2 repairs + 2 free re-prompts) is NOT
  raised.
- Tests: 653 + new TestPlannerPlaceholderCheapFix green. Mocked
  [prose, Flask, empty, placeholder, grounded] lands on the grounded
  plan in 5 calls (placeholder round free); mocked [Flask, Flask,
  grounded] lands in 3 calls with Flask named as forbidden in the
  second repair prompt. Verbatim retry-#17 attempt-4 is a CHEAP
  placeholder rejection naming 'step-1' and freetalon/api.py.
  Regression guards mirror the code-append fix (retries #16-#3,
  bare-dict criteria, duplicate ids / cycles / self-dependency).
  M5 smoke passes. Demo: tools/demo-placeholder-cheap.py.
- Next: /health retry #18.


## Update - 2026-11-17 (early, /health retry #18)

- /health retry #18 FAILED at planning (DRIVER_EXIT:1, ~148s, no run
  created). Pre-flight all green (GPU 52C idle, 300W, x16, zero Xid;
  Ollama gemma-3-abliterated:latest; head ff299a2; suite 679/679).
  Fans manual 85% during, restored to auto after. Three planner
  attempts, all rejected (each re-run through parse_planner_response
  to confirm exactly):
  (1) The prose-command plan again -- rejected with exactly 3 errors
  by the prose rule, each naming the grounded move.
  (2) The retry-#15 Flask heredoc plan again -- rejected with exactly
  1 error: step_2's test-file heredoc does not parse as Python
  (expected an indented block after function definition on line 7).
  The code-append parse check fired, but NOTHING else was named: the
  Flask app heredoc overwriting api.py (no flask import in the body,
  so the framework check misses it), the test path
  freetalon/tests/test_api.py (that directory does not exist -- repo
  tests live at repo-root tests/), and the /tmp redirect in step_3.
  (3) A sed + heredoc plan (4 nodes, skeleton-shaped) -- rejected
  with exactly 3 errors: step_4 invents a 'response_content'
  criterion type; step_2's test heredoc does not parse (indented
  block after class definition, line 6); step_2 writes a Flask app
  (framework rule fired -- the body has 'from flask import jsonify').
  NOT named: step_1's sed targets 'def health(self):', which does
  not exist in api.py -- sed -i with a non-matching pattern is a
  silent no-op; and the test path is still freetalon/tests/.
  Budget exhausted (initial + 2 repairs; no free re-prompts used --
  no empty plan, no placeholder-only rejection). Judge/replan never
  ran.
- Endpoint NOT extended, no new tests, no live curl. freetalon/api.py
  untouched (219 lines, 0-line diff vs HEAD). Suite re-run 679/679
  green after the failure. No stray files. GPU survived (post-run
  68C cooling, 300W, x16, zero Xid).
- Weaknesses, in priority order:
  (1) Single-error signal loss: when the code-append parse check is
  the only error, the framework / /tmp / wrong-location defects in
  the same plan never reach the repair round -- the model fixes the
  one named defect (indentation) and keeps the unnamed ones (Flask,
  wrong test path) into the next attempt. The repair prompt needs the
  per-node validation errors alongside the parse error, like the
  duplicate-key and dependency rejections already do.
  (2) The framework check misses Flask code with no flask import:
  attempt 2's heredoc writes '@app.route' + 'jsonify' with no import
  statement, so nothing flags the Flask overwrite of the serving
  module. Detect framework usage (route decorators, jsonify, app
  object) as well as imports.
  (3) sed -i edits with a non-matching pattern are silent no-ops:
  validate at plan time that a sed/replace target actually exists in
  the file being edited (or require a validity measurement after).
  (4) Heredoc/scaffold writes to a non-existent directory under the
  repo (freetalon/tests/) are not flagged -- the test path is wrong
  in both attempts 2 and 3 and nothing names it.
  (5) The /tmp redirect inside a compound command ('curl ... | jq .
  > /tmp/health_response.json') was not named by the /tmp rule.
- Known gaps still open: file_matches outputs-only hole; literal
  JSON answer to a NEW non-code file with only an exit_code
  criterion; scaffold content not parse-checked; scaffold-with-
  fragment overwriting a whole module; echo regex misses a '>'
  inside a quoted echo body.
- Next: fix weakness (1), then /health retry #19.

## Update - 2026-11-17 (CI fix: retry-#16 line number is disk-dependent)

- CI on the ff299a2 push: Tests 44078864370 FAILED (3m8s), GPU tests
  44078864446 SUCCESS (2m28s) -- exactly 1 failure:
  test_retry16_v1_rejected_by_name asserts "line 221" but CI got
  "line 2". Same family as the 49a6a38 disk-dependent fix.
- Root cause: the code-append parse check simulates existing file +
  appended lines on seeker7 (real api.py, 219 lines -> IndentationError
  on line 221) but falls back to parsing the appended content alone in
  CI (fixture path does not exist there -> line 2). The validator is
  correct; the test was fragile.
- Fix (tests only): new _expected_retry16_error_line() helper -- 221
  where the fixture path exists, 2 where it does not -- mirroring
  _expected_retry10_error_count(). No validator or fixture changes.
- Verified: suite 679/679 green, M5 smoke passes. The fixed test passes
  unpatched on seeker7 (line 221) and under a simulated-CI stub (line 2,
  message identical to the real CI log). Both classes unpatched: 53/53.
  Under the stub 5 placeholder tests fail, all known false positives of
  the stub: their fixtures are retargeted to this checkout's api.py via
  __file__, which exists in real CI under /home/runner (and all 5 passed
  in the real CI 679 run).
- Next: signal-loss fix (retry #18 weakness 1), then /health retry #19.

## Update - 2026-11-17 (planner signal-loss fix, retry #18 weaknesses)

- All five retry-#18 weaknesses fixed, CPU-only. Diagnosis, verified
  by probe before editing: _collect_plan_errors already aggregates
  all errors into one rejection -- the "signal loss" was that three
  checks were blind to these shapes, so fixing the checks restores
  the aggregation; there is no separate parse-error raise path.
  (1) Parse-error rejection now carries the per-node validation
  errors: _code_append_errors returns the split plan nodes and the
  parse rejection is built from _collect_plan_errors on those nodes,
  so one rejection names the parse error AND the framework / /tmp /
  wrong-location defects. This also unbounds the
  forbid-repeated-approaches mechanism, which can only forbid
  approaches the rejection names -- Flask is now forbidden after
  the first rejection.
  (2) Framework check detects usage as well as imports:
  RepoIndex.framework_in_text now also matches route decorators
  (@app.route, @router.get), response helpers (jsonify), and
  app-object construction (Flask(...), FastAPI(...)) in scaffold
  content and heredoc bodies. Imports are checked first, usage
  second; bare app.get(...) is deliberately not a marker.
  (3) sed -i no-ops rejected at plan time: new _sed_noop_errors
  parses sed scripts (s/// with any delimiter, -i/-iSUFFIX/
  --in-place, bundles with i, -e scripts; -f skipped) and requires
  each pattern to occur in the target file on disk (literal
  substring or regex match, covering BRE vs ERE). Grounded move
  appended. Only files that exist on disk are checked; edits to
  files the plan itself creates are not rejected.
  (4) Missing-directory check: new _missing_directory_errors
  rejects a write node targeting a path under a repo-tree directory
  that does not exist on disk, naming the correct location
  (freetalon/tests/ -> repo-root tests/). Genuine generated files
  at the workspace root and paths that actually exist are NOT
  flagged.
  (5) /tmp check scans every redirect segment: _tmp_write_errors
  now uses _node_written_paths (all redirects in compound/piped
  commands), not only a bare redirect.
- Verbatim retry-#18 attempt 2 is now rejected with 4 errors
  (parse + framework + missing-directory + /tmp); attempt 3 with
  5 errors (the old 3 + sed no-op + missing-directory). Mocked
  [Flask-heredoc, grounded] and [sed-no-op, grounded] both land
  on the grounded plan in one repair round.
- Tests 679 -> 708/708 green (+29 TestPlannerSignalLossFix):
  verbatim attempt-2/attempt-3 rejections, both mocked repairs,
  grounded-plan pass (incl. sed against an existing pattern and
  writes to existing dirs), framework-usage / /tmp-in-compound /
  sed-parser / missing-directory unit cases, and regression guards
  for retries #17-#3, bare-dict criteria, duplicate ids, cycles,
  self-dependency (retry-#10 count via _expected_retry10_error_count,
  retry-#16 line via _expected_retry16_error_line -- no hard-coded
  disk-dependent values).
- Demo: tools/demo-signal-loss.py, log logs/demo-signal-loss.log
  (ALL CHECKS PASSED) -- verbatim attempt 2 rejected with the parse
  error plus framework, missing-directory, and /tmp errors; mocked
  repair lands on the grounded plan in one round with Flask named
  forbidden in the repair prompt.
- M5 smoke passes (finance run-20261117T122820Z-90a451, ECU
  run-20261117T122820Z-a3c7e4). freetalon/api.py untouched (219
  lines, 0-line diff). No stray files. No LLM calls, no GPU, no
  e2e driver, no push, no reboot, no power-limit change.
- Known gaps still open (unchanged): file_matches outputs-only
  hole; literal JSON answer to a NEW non-code file with only an
  exit_code criterion; scaffold content not parse-checked (bash
  writes only); scaffold-with-fragment overwriting a whole module;
  echo regex misses a '>' inside a quoted echo body; tee writes
  not caught by the /tmp check; perl -pi -e and other in-place
  editors not covered by the sed check; a mkdir -p earlier in the
  plan is not credited by the missing-directory check;
  _validate_raw_nodes (schema-error path) runs only the stand-in
  check, so a parse error in a plan that also has schema errors is
  still lost there.
- Next: /health retry #19.

## Update - 2026-11-17 (early, /health retry #19)

- /health retry #19 FAILED at planning (DRIVER_EXIT:1, ~145s, no run
  created). Pre-flight all green (GPU 52C idle, 300W, x16, zero Xid;
  Ollama gemma-3-abliterated:latest; head faccbfa; suite 708/708).
  Fans manual 85% during, restored to auto after. Three planner
  attempts, all rejected (each re-run through parse_planner_response
  to confirm exactly):
  (1) The prose-command plan again -- rejected with exactly 3 errors
  by the prose rule, each naming the grounded move.
  (2) The retry-#15 Flask heredoc plan again -- rejected with exactly
  4 errors: step_2 test-file heredoc does not parse as Python
  (expected an indented block after function definition on line 7);
  step_3 writes to /tmp/health_response.json (piped /tmp redirect
  now named); step_1 writes a Flask app (framework rule now fires
  with no flask import -- route decorator + jsonify detected);
  step_2 writes to freetalon/tests/test_api.py but its directory
  does not exist (missing-directory error names repo-root tests/).
  The signal-loss fix WORKED as designed -- the parse error now
  carries the framework / /tmp / missing-directory defects in the
  same rejection.
  (3) A Flask scaffold plan -- rejected with exactly 1 error:
  step_1 writes a Flask app (framework rule, grounded move named).
  Flask was named in the attempt-2 rejection, so the
  forbid-repeated-approaches mechanism should have forbidden it in
  the repair prompt -- but the model returned a Flask scaffold
  anyway. The forbid mechanism did not steer the model this time.
  Budget exhausted (initial + 2 repairs; no free re-prompts used --
  no empty plan, no placeholder-only rejection). Judge/replan never
  ran. Endpoint NOT extended, no new tests, no live curl.
  freetalon/api.py untouched (219 lines, 0-line diff). Suite re-run
  708/708 green. No stray files. GPU survived (post-run 50C idle,
  43.36W, 300W, x16, zero Xid; mid-run peak not sampled -- planning
  only). Log: logs/llm-end-to-end-health-retry19-2026-11-16.log
  (294 lines).
- /health retry #20 (2026-11-17, ~05:03 PST; head 5703752, forbidden-steer fix) -- FAILED, goal FAIL in 32.6s, escalated: re-planner failed (DRIVER_EXIT:0 -- the loop returned FAILED rather than raising). First retry since #8/#14/#16 where a plan was accepted AND executed. Run run-20261117T130347Z-325642, plan plan-7885a4ef9d99.
  Initial planning (4 calls; 1 repair + 1 free used): (1) The prose-command plan again -- rejected with exactly 3 errors by the prose rule, each naming the grounded move. (2) The retry-#15 Flask heredoc plan again -- rejected with exactly 4 errors: step_1 heredoc into api.py breaks the module (no longer parses, line 6); step_2 test heredoc does not parse (line 4); step_1 writes a Flask app (framework rule, new imperative form + grounded move); step_2 writes to freetalon/tests/test_api.py, directory does not exist (missing-directory names repo-root tests/). Signal-loss fix still working -- all four defects in one rejection. (3) {"nodes":[]} -- empty plan, cheap, free re-prompt (skeleton carried). (4) ACCEPTED -- a placeholder-heredoc plan: step-1/2 write '#TODO: replace this comment with the real code/test code for /health' via cat heredoc into workspace health.py / test_health.py, step-3 runs the comment-only test file (exit 0), step-4 curls :8765/health.
  Execution v1: node-1 PASS (health.py exists), node-2 PASS (test_health.py exists), node-3 PASS (exit 0 -- a comment-only file), node-4 FAIL (curl exit 7, connection refused -- nothing serves :8765, the plan never starts the server). Judge chose REPLAN (informative failure).
  Replan (3 calls, both repairs spent): (1) a single-node object with no 'nodes' array and criteria nested in inputs -- rejected with 2 errors (nesting + route engagement). (2) A scaffold of '/health' + curl /health -- rejected with 2 errors (duplicates existing /health route + route engagement). (3) A scaffold of freetalon/api.py with a Flask app (@app.route('/health'), def health(): return 'OK') -- rejected with exactly 1 error: step_1 writes a Flask app (new forbidden-block form, grounded move named). Budget exhausted -- escalated FAIL.
  Forbidden-steer verdict: PARTIAL. In initial planning the Flask ban HELD -- attempt 4 was placeholder heredocs, not Flask (retry #19 attempt 3 was a Flask scaffold). But the replan phase starts with a FRESH forbidden list (rejected_approaches is a local inside _call_llm_with_repair; replan_from_brief calls it separately), so Flask -- rejected in initial planning -- was never named as forbidden in the replan repair prompts, and it came back as replan attempt 3. The new form was never exercised in the replan phase: the Flask rejection was the terminal error and no repair round followed it.
  Endpoint NOT extended, no new tests, no live curl succeeded. freetalon/api.py untouched (219 lines, 0-line diff). Suite re-run 737/737 green. Fake files health.py / test_health.py (comment-only placeholders) deleted. GPU survived (post-run 51C idle, 43.40W, 300W, x16, zero Xid; mid-run peak not sampled -- 69C cooling at first check after fans restored to auto). Log: logs/llm-end-to-end-health-retry20-2026-11-16.log (284 lines); probe log: logs/probe-retry20-rejections.log.
  Weaknesses for the next fix, in priority order: (1) Forbidden approaches are tracked per planning call, not per run -- thread the rejected-approaches list from initial planning into replan_from_brief so the first replan repair prompt already names Flask (and prose / /tmp / placeholder) as forbidden with replacements. (2) Placeholder heredocs to NEW files are accepted -- the placeholder guard covers scaffold writes to existing code files only; extend it (cheap rejection) to bash heredoc/echo writes of comment-only/TODO-only content claimed by file_exists or run as a test. (3) No plan ever starts the API server, so the live-curl node cannot pass (exit 7) -- the ROUTE GOAL shape should include starting the server, or validation should require a server-start node before a live curl on the API port. (4) The replan budget is burned on junk before the steering signal appears (replan attempts 1-2 were a single-node object and a /health scaffold); related to (1).
- Planner seeded-replan fix (2026-11-17, CPU-only): all three retry-#20 weaknesses. (1) Forbidden approaches are now tracked per RUN, not per planning call: _call_llm_with_repair takes seed_forbidden and returns the accumulated list in the metadata under "rejected_approaches"; plan_task_intent carries it in the plan metadata; the loop injects it into the replan brief as seed_forbidden and preserves/accumulates it when rebuilding the plan after a replan; replan_from_brief accepts the seed as a kwarg or from the brief and writes the accumulated list back into the brief. The first replan repair prompt now names Flask with its replacement move, the grounded move, and the consequence. (2) Placeholder guard extended to bash heredoc/echo writes: _placeholder_bash_errors rejects a bash write of comment-only/TODO-only content to a code file when claimed by a file_exists criterion anywhere in the plan or run as a test by some node; the rejection is cheap (free re-prompt). One placeholder error per rejection: when a scaffold placeholder already names the approach, the bash placeholder is suppressed, so retry-#17 attempt-4 stays a 1-error cheap rejection and the cheap free re-prompt lets the model iterate one placeholder defect at a time. Verbatim retry-#20 v1 is now rejected CHEAPLY with 2 errors naming health.py and test_health.py (probe tools/probe-retry20.py confirmed it was ACCEPTED before the fix, and that a comment-only heredoc overwriting the existing api.py was also ACCEPTED). (3) ROUTE GOAL shape + grounded skeleton + grounded move name the server-start step: node 3 starts the API server (cd <repo> && ./.venv/bin/python -m freetalon.cli start, stop a stale server first; freetalon.cli start daemonizes itself, stop returns 1 when idle so the stop;start chain is safe) and waits until curl -sf answers, node 4 curls live. Shape only, NO new validation error -- the canonical grounded plan curls without starting and is asserted to pass in the earlier fix classes, so enforcing it would mean rewriting the grounded fixture everywhere; if a future retry curls without starting despite the shape, validation can be added then (with the fixture update). One existing test updated (expected -- the error surface changed): the base plan_task_intent test now expects metadata {"planner": "llm", "rejected_approaches": []}. Also fixed a stale demo: demo-depends-on.py asserted "2 errors" for retry-#10 attempt-3, but the duplicate-keys fix (b66c5ac) made it 7 errors -- the demo failed identically on the pristine tree; assertion updated to 7 with the breakdown. Tests 737 -> 777/777 green (TestPlannerSeededReplanFix 40/40), M5 smoke passes, demo tools/demo-seeded-replan.py ALL CHECKS PASSED (13 checks), all 19 demos pass. No stray files; freetalon/api.py untouched (219 lines, 0-line diff). GPU baseline 47C, 300W, x16, zero Xid (CPU-only work, no LLM calls).
- /health retry #21 (2026-11-17, ~05:45 PST; head 2d50c10, seeded-replan fix) -- FAILED at planning (DRIVER_EXIT:1, ~132s, no run created). Pre-flight green (50C, 128.44W, 300W, x16, zero Xid, Ollama gemma-3-abliterated:latest, head 2d50c10 clean, suite 777/777). Intake succeeded cleanly (valid TaskIntent, no echo). Five planner calls (initial + 2 free + 2 repairs), all rejected -- probe log logs/probe-retry21-rejections.log re-ran all five raw outputs through parse_planner_response and confirmed each count exactly: (1) {"nodes":[]} -- EmptyPlanError, cheap, free re-prompt. (2) The retry-#20 placeholder-heredoc plan again (step-1 cat heredoc TODO test into workspace test_health.py, step-2 run it, step-3 start the API server, step-4 curl live) -- rejected with exactly 1 error: step-4 has unknown field "assigned_claws" (a typo for assigned_claw). The placeholder-heredoc error did NOT fire here: the schema-error path runs only the stand-in check, so the placeholder defect was masked by the typo. (3) A Flask scaffold plan (scaffold api.py with from flask import Flask, jsonify and @app.route("/health"), then run api.py and curl) -- rejected with exactly 1 error: step_1 writes a Flask app (framework rule, grounded move named, now including the server-start step). (4) {"nodes":[]} again -- EmptyPlanError, cheap, free re-prompt (second free used). (5) The placeholder-heredoc plan again, typo fixed -- rejected CHEAPLY with exactly 1 error: step-1 writes workspace test_health.py with placeholder content only via a bash heredoc, claimed by a file_exists criterion; grounded move named. Budget exhausted (2 free + 2 repairs spent) -- driver raised at plan_task_intent (planner.py:3474, via _call_llm_with_repair line 3430 raising last_error). Judge/replan never ran.
  Seeded-replan fix verdict: NOT EXERCISED live. The run failed in initial planning, so no replan branch ran and seed_forbidden was never used; the per-run forbidden list is still unproven in a live retry. Placeholder-heredoc guard: FIRED -- attempt 5 is the new guard working as designed (cheap 1-error rejection naming test_health.py), and the verbatim retry-#20 v1 plan came back twice in this run. Server-start shape: STEERING -- attempts 2 and 5 both include a step-3 "Start the API server for /health" (cli stop, cli start, 30s curl -sf wait loop), copied from the shape; it never executed because the plan was rejected for other reasons. Note the model wrote ./venv/bin/python (wrong -- the venv is .venv), so that step would have failed if the plan had been accepted.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff). Suite re-run 777/777 green. No stray files; no new run dir. GPU survived (post-run 60C cooling at first check after fans restored to auto, 44.92W, 300W, x16, zero Xid; mid-run peak not sampled -- planning only). Fans restored to auto. Log: logs/llm-end-to-end-health-retry21-2026-11-16.log (303 lines); probe log: logs/probe-retry21-rejections.log (282 lines); probe script: tools/probe-retry21-rejections.py.
  Weaknesses for the next fix, in priority order: (1) Placeholder signal lost behind a schema error -- attempt 2 carried BOTH the placeholder heredoc and the assigned_claws typo, and the schema-error path (_validate_raw_nodes) runs only the stand-in check, so the placeholder defect never reached the repair prompt; the model fixed the typo and returned the same placeholder plan, burning another round. Run _placeholder_bash_errors (and ideally the full _collect_plan_errors) on lenient node views in the schema-error path too, so one rejection names the typo AND the placeholder. This is the known gap already noted for parse errors, now observed live with the placeholder check. (2) Cheap re-prompts burned by cycling -- two empty plans plus the placeholder plan consumed the free budget, so the call-5 placeholder rejection had no free re-prompt left; the model now cycles a small set of attractors (empty, placeholder-heredoc, Flask) and never produces a grounded plan. Consider treating a second empty plan in the same planning call as a repair rather than free, or carrying the grounded skeleton more forcefully after an empty plan. (3) The model never produces a grounded plan at all -- in 5 calls, zero plans engage api.py with real code; the skeleton step-1 sketch is copied as a placeholder comment into a new test file instead of extending api.py. The skeleton step-1 may need to show the branch as real code to insert rather than a commented sketch, or the repair prompt after a placeholder rejection should carry the concrete grounded move as fill-in-the-blanks with the actual branch code. (4) The server-start shape does not give the exact venv path -- the model guessed ./venv; name ./.venv/bin/python explicitly in the shape and grounded move.
- Planner schema-signal fix (2026-11-17, CPU-only): all four retry-#21 weaknesses. (1) The schema-error path now runs the placeholder checks on lenient node views: _validate_raw_nodes takes repo_root (threaded through _coerce_nodes from normalize_plan_nodes) and, when schema errors exist, runs _placeholder_scaffold_errors + _placeholder_bash_errors on the lenient views alongside the stand-in check, with the same dedup as _collect_plan_errors; a rejection naming a placeholder (or stand-in) fake is cheap. Verbatim retry-#21 attempt-2 is now a CHEAP 2-error rejection (placeholder heredoc naming test_health.py + the assigned_claws typo) instead of the typo alone. The full _collect_plan_errors is deliberately NOT run in the schema path: several of its errors duplicate the schema errors (unknown claw for a missing assigned_claw, no-criteria for a missing criteria field), which would change the pinned counts for the retry-#7/#11 invented-schema fixtures. (2) The skeleton is carried more forcefully after an empty plan: _call_llm_with_repair tracks empty_count (total empties in the call, not just the streak); a second empty gets the skeleton again with the count and the consequence named ("You have already returned an empty plan 2 times... Your free re-prompts are used up -- another empty plan will spend one of your remaining repairs"). The budget is NOT changed -- the second empty still gets its free re-prompt (the 5-call accounting for empty runs is pinned by existing tests). (3) The skeleton's step-2 is runnable real test code instead of a TODO heredoc: it starts the API server in-process on an ephemeral port (the tests/test_api.py pattern) and asserts the goal's three fields for /health (service_status, ollama_reachable, gpu_temperature; other routes get generic ok/status assertions). Probed live: the test runs and fails ONLY on the missing fields (AssertionError: 'service_status' not found in {'ok': True, 'status': 'ready'}), so it enforces the deliverable instead of passing on the unextended endpoint. The repair prompt after a placeholder rejection now also carries the skeleton (_grounded_skeleton gains after="placeholder" with its own lead-in, so the model is never told its reply was empty when it was a stub). Step-1's sketch stays commented: showing it as uncommented real code would let the model scaffold a 2-line fragment over the whole module (a clobber hazard); a verbatim copy is still placeholder-only and rejected cheaply. (4) The grounded-move sentence names the exact venv path: "start the API server from the repo root with ./.venv/bin/python -m freetalon.cli start (stop a stale server first with ./.venv/bin/python -m freetalon.cli stop)". Tests 777 -> 813/813 green (TestPlannerSchemaSignalFix 36/36), M5 smoke passes (finance run-20261117T142027Z-a5c141, ECU run-20261117T142027Z-37f18f), demo tools/demo-schema-signal.py ALL CHECKS PASSED (16 checks), probe tools/probe-skeleton-test.py 6/6. The demo shows the verbatim attempt-2 output rejected with the typo AND the placeholder error in one rejection, mocked [empty, empty, grounded] and [placeholder-heredoc, grounded] landing within budget, and the shape naming ./.venv/bin/python. All 20 demo tools pass (19 existing + the new demo-schema-signal.py, re-run after the fix). No stray files; freetalon/api.py untouched (219 lines, 0-line diff). GPU baseline 47C, 300W, x16, zero Xid (CPU-only work, no LLM calls).
- Next: /health retry #22.
- /health retry #22 (2026-11-17, ~06:23 PST; head 6ed9c6c, schema-signal fix) -- FAILED at planning (DRIVER_EXIT:1, ~40s, no run created). Pre-flight green (53C, 150.89W, 300W, x16, zero Xid, Ollama gemma-3-abliterated:latest, head 6ed9c6c clean, suite 813/813). Intake succeeded cleanly (valid TaskIntent, no echo). Five planner calls, ALL empty plans -- {"nodes":[]} five times in a row (initial + 2 free + 2 repairs). No prose plan, no Flask plan, no placeholder plan this time -- the model returned nothing at all. The second-empty skeleton force WAS exercised: the final EmptyPlanError names the repeated empties and the grounded move ("The model returned empty plans repeatedly instead of a plan DAG -- produce the grounded plan above, do not return another empty plan"), and the repair prompts carried the skeleton with the count and consequence named -- the model still returned empty. Probe tools/probe-empty-retry22.py re-ran all five raw outputs through parse_planner_response and confirmed each raises EmptyPlanError (no numbered rejection -- there is nothing to count); probe log logs/probe-retry22-empty.log.
  Schema-signal fix verdict: NOT EXERCISED live. No attempt carried a placeholder or a schema error, so the schema-path placeholder signal never fired; no replan branch ran, so the seeded forbidden list is still unproven live; the real skeleton test step-2 never appeared in any plan; no server-start step appeared (no plan at all). The model has now cycled through attractors across retries: prose, Flask, placeholder-heredoc, and now pure empty.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff). Suite re-run 813/813 green. No stray files; no new run dir (newest run dirs are the schema-signal fix M5 smoke runs run-20261117T142027Z-a5c141 / -37f18f). GPU survived (post-run 52C idle, 43.79W, 300W, x16, zero Xid; mid-run peak not sampled -- planning only; pre-flight 53C at 150.89W was the warmest reading). Fans restored to auto. Log: logs/llm-end-to-end-health-retry22-2026-11-16.log (73 lines).
  Weaknesses for the next fix, in priority order: (1) The model returns empty plans even with the skeleton and the consequence named -- the repair prompt is pure text steering and the model ignores it. The empty-plan path needs something stronger than another prompt sentence: consider making a second empty plan cost a repair instead of a free re-prompt (so the call ends sooner and the failure is cheaper), or seeding the repair prompt with a fill-in-the-blanks plan the model only has to complete, or detecting a run of empties and escalating with a different prompt shape (e.g. asking for a single-node plan first). (2) The model cycles a small set of attractors (empty, prose, Flask, placeholder-heredoc) and never produces a grounded plan -- the ROUTE GOAL shape is already in the initial prompt, so check whether it is actually reaching the model or whether the initial prompt is too long and the shape is buried. (3) The seeded forbidden list is still unproven live after three retries (#20/#21/#22) -- it only matters if a plan is ever accepted and executed; nothing to fix there, but the next fix should be judged on whether a plan is accepted at all. (4) No plan ever starts the API server in a live run yet -- the server-start shape steered in retries #20/#21 but has not executed; if a plan is accepted without it, the live curl will fail exit 7 as in retry #20.
- Planner empty-escalation fix (2026-11-17, CPU-only): both retry-#22 weaknesses, root cause measured not guessed. (1) The planner prompts do not fit in Ollama's 4096-token context window (num_ctx hardcoded in _call_ollama): token counts with the Gemma 3 tokenizer from models/tokenizer.json against the exact strings the driver assembles (tools/dump-prompts.py; logs/measure-prompt-*.txt) -- initial call 4,255 tokens (system 3,030 + user 1,225), already 159 over before a single output token; first repair 6,415 (2,319 over); a grounded-plan output is 1,687 tokens, so a full repair round needs 8,102 tokens of context. The model never received the skeleton intact and could not emit a full plan -- the 5-token {"nodes":[]} is the only output that fits the leftover headroom, which is why all five retry-#22 replies are byte-identical. Fix: planner/replan calls use _PLANNER_NUM_CTX = 12288, threaded through a new optional LLMSettings.num_ctx field (None keeps the 4096 default); _call_llm_with_repair derives effective settings once per call, and an explicit num_ctx is respected, never overridden. 12288 covers the measured 8,102-token repair round with ~4,000 spare (8192 would leave only ~90). The intake path keeps 4096 (its prompts are small). KV cost at 12288 is ~4.6 GB vs ~1.5 GB at 4096 (0.375 MB/token for this 12B model, from models/config.json: 48 layers, 8 KV heads, 256 head dim) -- Ollama fits it by offloading a few more layers to CPU if VRAM runs short, a speed cost, not a failure. (2) The ROUTE GOAL shape moves last in the initial prompt and the replan prompt: it sat at ~70% of the initial prompt (offset 2845 of 4093 chars) ahead of the instruction paragraph; now only "\n\nJSON Output:" follows it (measured live: initial prompt 4095 chars, shape offset 3015). The repair budget is NOT raised and no fail-fast was added: the skeleton mechanism was already built and proven in mocks -- the live failure was delivery, not content -- so ending failures sooner would not produce a plan; five empties still burn all 5 calls and escalate with the grounded move (pinned by test). Tests 813 -> 854/854 green (TestPlannerEmptyEscalationFix 41/41), M5 smoke passes (finance run-20261117T145208Z-c71de5, ECU run-20261117T145208Z-e39762), demo tools/demo-empty-escalation.py ALL CHECKS PASSED (21 checks), all 21 demo tools pass (exit 0). Mocked [empty, empty, grounded] lands in 3 calls, [empty, empty, empty, grounded] in 4, [empty, placeholder, grounded] and [Flask, empty, grounded] in 3. No stray files; freetalon/api.py untouched (219 lines, 0-line diff). GPU baseline 46C, 300W, x16, zero Xid (CPU-only work, no LLM calls). Probe tools: tools/measure-prompt.py (logs/measure-prompt.log), tools/dump-prompts.py (logs/measure-prompt-*.txt); token counting ran locally with the Gemma tokenizer (staging venv).
- /health retry #23 (2026-11-17, ~07:02 PST; head 39edb04, empty-escalation fix) -- FAILED, goal FAIL in 208s, escalated: re-planner failed (DRIVER_EXIT:0 -- the loop returned FAILED rather than raising). First retry since #20 where a plan was accepted AND executed, and the first time the replan branch ran twice. Run run-20261117T150407Z-a461f2, plan plan-7885a4ef9d99. Pre-flight green (50C, 128.38W, 300W, x16, zero Xid, Ollama gemma-3-abliterated:latest, head 39edb04 clean, suite 854/854). Intake succeeded cleanly (valid TaskIntent, no echo). FIVE planner calls, all non-empty -- the num_ctx raise worked: the model emitted full plans for the first time since retry #20. Probe tools/probe-retry23-rejections.py re-ran all five raw outputs through parse_planner_response and confirmed each verdict (probe log logs/probe-retry23-rejections.log, 272 lines; restored-state re-probe logs/probe-retry23-rejections-restored.log, 273 lines): (1) ACCEPTED (3 nodes) -- a stdlib http.server scaffold plan: step-1 overwrites api.py with a whole new BaseHTTPRequestHandler module (handle_health + HiveAPIServer + __main__ block), step-2 scaffolds a workspace test_api.py that curls the live server, step-3 starts the server with ./venv/bin/python (wrong venv). Became v1. (2) Replan attempt 1, ACCEPTED (5 nodes) -- a Flask plan: step-1 scaffolds api.py with from flask import Flask, jsonify + @app.route("/health"), step-2 scaffolds tests/test_api.py with a Flask test client, step-3 runs it with bare python, step-4 starts the server with ./freetalon/api.py start, step-5 curls live. Became v2. (3) Replan attempt 2 call 1, REJECTED with exactly 1 error: step-4 has an unusable criterion, unknown type string_matches. The step-1 placeholder comment (# This is a placeholder, replace with actual implementation) plus a minimal def health() was NOT named -- the placeholder guard treats content with real code as non-placeholder. (4) Same plan again, REJECTED with exactly 1 error: step-4 string_matches. Only the criterion shape changed between (3) and (4) (pattern/input to equals/field); step-1 repeated verbatim. (5) Identical to (4), REJECTED with exactly 1 error: step-4 string_matches. Budget exhausted (initial + 2 repairs, no free re-prompts) -- escalated FAIL.
  Execution v1: node-1 PASS (file_exists + exit_code curl -- the curl criterion passed because _eval_exit_code checks the NODE exit code and never runs the criterion command; the scaffold write exited 0, so the live curl was never executed), node-2 PASS (exit_code 0 -- same mechanism), node-3 FAIL (exit 127 -- ./venv/bin/python not found). Judge chose REPLAN (informative failure). Execution v2: node-1 PASS (file_exists), node-2 PASS (file_exists), node-3 FAIL (exit 127 -- /bin/sh: python: not found; the step-3 criterion has no command at all, just exit_code equals 0). Judge chose REPLAN again. Replan attempt 2 burned its 3 calls on (3)-(5) above.
  Empty-escalation fix verdict: EXERCISED and CONFIRMED live. The model emitted non-empty plans in all five calls (vs five byte-identical empty plans in retry #22) -- the 4096-token window was the root cause, and 12288 fixed the delivery. Shape-last: output (3) step-3 uses the correct .venv path (/home/seeker/projects/FreeTalon/.venv/bin/python -m freetalon.cli start) -- the shape and venv-path fixes steered it, but the plan was rejected for string_matches so it never executed. Server-start step: still never executed live -- v1 step-3 and v2 step-4 both used wrong interpreters and the runs died at node-3 first. Seeded forbidden list: the replan branch RAN (twice) for the first time since the fix landed, but the seed was EMPTY both times -- initial planning had zero rejections to seed from, and replan attempt 1 had zero rejections -- so Flask was never named forbidden in any replan prompt and came back as attempt (2). Still unproven as a steering mechanism.
  CORRUPTION: v1 node-1 and v2 node-1 both overwrote freetalon/api.py wholesale with scaffold content (unparseable -- bad indentation), and v2 node-2 overwrote tests/test_api.py with a Flask test. git diff showed 26 insertions, 465 deletions across the two files. Both restored via git checkout (api.py back to 219 lines); workspace test_api.py stray deleted. Suite re-run 854/854 green after restore.
  ROOT CAUSE (measured): the framework guard is state-dependent on the file it protects, and a first bad write disarms it for the rest of the run. _framework_errors checks index.frameworks, built by build_repo_index from _imported_modules, which returns an empty set on SyntaxError -- so once v1 overwrote api.py with unparseable code, the index recorded NO framework for api.py and the Flask check silently skipped every later plan. Proven by re-probe: with api.py restored, output (2) is REJECTED with 2 errors (step-1 AND step-2 write a Flask app, grounded move named) and outputs (4)/(5) with 2 errors (string_matches + step-1 Flask); live (corrupted api.py) they were ACCEPTED / 1 error. The guard was armed at initial planning (real api.py parses, http.server recorded) but disarmed from replan attempt 1 on.
  Endpoint NOT extended, no new tests that survive, no live curl ever executed. No stray files after cleanup. GPU survived (post-run 69C cooling at first check after fans restored to auto, then 52C idle, 43.47W, 300W, x16, zero Xid; mid-run peak not sampled). Fans restored to auto. Log: logs/llm-end-to-end-health-retry23-2026-11-16.log (325 lines).
  Weaknesses for the next fix, in priority order: (1) The framework guard disarms itself after the first bad scaffold write -- build the serving-framework lookup from the pristine repo state (git HEAD, or the last known-good index), or fall back to the committed file when the working-tree copy does not parse, or record the framework once at run start before any node executes. Also: scaffold-claw content is not parse-checked (known gap) -- a scaffold write of unparseable Python to an existing code file should be rejected at plan time, the same way bash heredoc writes are simulated/parse-checked. (2) exit_code criteria never run their command -- _eval_exit_code compares the NODE exit code to the expected value and ignores the criterion command field, so a scaffold node passes a live-curl criterion without any curl happening. Fix: when an exit_code criterion carries a command, run that command as the measurement (or reject the criterion as unusable at plan time). Related: a bare exit_code criterion with no command on a bash node (v2 step-3) is accepted at plan time but can only fail at runtime -- require a command on exit_code criteria for bash nodes, or default the check to the node command explicitly. (3) Single-error signal loss in replan: outputs (3)/(4)/(5) were rejected for string_matches alone while step-1 carried a placeholder comment, a Flask app without an import, and a wrong test path (freetalon/tests/ does not exist -- repo tests live at repo-root tests/), and step-3 used bare python; the model fixed only the criterion shape and repeated step-1 verbatim three times. Run the full _collect_plan_errors on replan outputs so one rejection names the placeholder, the framework use, the wrong location, and the unusable criterion together. (4) Bash commands use interpreters that do not exist (./venv/bin/python, bare python) and the validator never checks them -- v1 and v2 both died at node-3 with exit 127. Validate at plan time that a bash command interpreter/binary exists in the repo or on PATH (at least that python resolves -- it does not on Ubuntu, only python3), and name the grounded form (./.venv/bin/python). (5) The seeded forbidden list carried nothing because initial planning had zero rejections -- the seed only accumulates from rejected approaches, so a first-try acceptance leaves the replan unprotected. Consider seeding the replan with the run-level known-bad approaches (Flask, placeholder heredoc) from the start, or carrying the repo-level forbidden list (Flask is always forbidden for route goals) into every replan prompt regardless of this call history.
- Next: fix retry-#23 weaknesses, then /health retry #24.
- Planner framework-guard fix (2026-11-17, CPU-only): all five retry-#23 weaknesses. (1) The framework guard can no longer disarm itself: build_repo_index falls back to the committed HEAD source (git show HEAD:<rel>, process-cached) for the whole per-file analysis when the working-tree file does not parse, so routes and framework survive a bad scaffold overwrite; scaffold-claw content is now parse-checked at plan time (_scaffold_parse_errors) -- an unparseable Python scaffold to any .py path is rejected with the grounded move, the same way bash heredoc writes are simulated/parse-checked. (2) exit_code criteria now run their command: _eval_exit_code executes the criterion's own command (timeout_s, default 60s) and compares ITS exit code; a bare exit_code criterion explicitly defaults to the node exit code. At plan time a criterion command must be a non-empty string and its interpreter is validated. (3) Single-error signal loss closed at the source -- the replan path already aggregated _collect_plan_errors; the missing signals were the disarmed framework check, unparse-checked scaffolds, unvalidated interpreters, and a placeholder blind spot: a placeholder STUB (placeholder/replace comment + at most 3 code lines) is now rejected cheaply in both the scaffold and bash placeholder checks. (4) Bash interpreter existence: _bash_interpreter_errors rejects bare python (names ./.venv/bin/python) and repo-relative interpreter paths that do not exist; .venv paths are exempt (the venv is a machine fact, not a repo-tree fact) and other bare names are not PATH-checked (PATH varies by machine). (5) The replan seed now carries repo-level known-bad approaches from the start: for a route goal, _repo_forbidden_approaches adds Flask + placeholder to the seed, and the seed block appears in the FIRST replan prompt (ROUTE GOAL shape stays last), not only in repair prompts. Tests 854 -> 869/869 green (TestPlannerFrameworkGuardFix 15/15), demo tools/demo-framework-guard.py ALL CHECKS PASSED, probe re-run: all five retry-#23 outputs REJECTED with 4/5/3/5/5 errors (live they were ACCEPTED / ACCEPTED / 1 / 1 / 1); probe log logs/probe-retry23-framework-guard.log. Deliberate pinned-count updates, each with the reason named in the test: retry-#13 attempt-4 1->3 (10 methods), retry-#18 attempt-2 4->5 and attempt-3 5->6 (step-3 bare python), retry-#19 attempt-2 4->5, attempt-3 1->3 (scaffold-parse + bare python), retry-#14 v2 2->3 (missing ./test.sh). Retry-#7/#11 invented-schema fixtures and retry-#21 attempt-2 CHEAP 2-error rejection unchanged. freetalon/api.py untouched (219 lines, 0-line diff). GPU baseline 45C, 300W, x16, zero Xid (CPU-only work, no LLM calls).
- Next: /health retry #24.
- /health retry #24 (2026-11-17, ~09:47 PST; head f042e53, framework-guard fix) -- FAILED at planning (DRIVER_EXIT:1, no run created). Pre-flight green (50C, 300W, x16, zero Xid, Ollama gemma-3-abliterated:latest, head f042e53 clean, suite 869/869). Intake succeeded cleanly (valid TaskIntent, no echo). Three planner calls (initial + 2 repairs), ALL rejected -- probe tools/probe-retry24-rejections.py re-ran all three raw outputs through parse_planner_response (probe log logs/probe-retry24-rejections.log): (1) REJECTED with 4 errors: step_2 criterion command uses bare python (criterion-command interpreter validation); step_3 uses ./venv/bin/python, which does not exist in the repo (interpreter path validation, names ./.venv/bin/python); step_1 scaffolds api.py with code that does not parse (line 5); step_2 scaffolds workspace test_api.py with code that does not parse (line 5). (2) REJECTED with exactly 1 error: step_1 scaffold of api.py does not parse (line 5) -- this output also hardcoded gpu_temp 25 / ollama_reachable True and passed a bare function as the HTTPServer handler class, but with the content unparseable no other check named a defect. (3) REJECTED with exactly 2 errors (the live raised error): step_2 uses bare python (python -m pytest freetalon/tests/ -- the path is also wrong, repo tests live at repo-root tests/, but only the interpreter error was named for that node); step_1 scaffold of api.py does not parse (line 7). Budget exhausted -- driver raised at plan_task_intent. Judge/replan never ran.
  Framework-guard fix verdict: EXERCISED and CONFIRMED live, in the direction of safety. Every defect class the fix added fired at plan time: scaffold parse check (all three attempts), bare-python rejection (attempts 1 and 3), nonexistent interpreter path (attempt 1), criterion-command validation (attempt 1). The contrast with retry #23 is the point: there the attempt-1 unparseable scaffold was ACCEPTED, executed, and disarmed the framework guard for the rest of the run; here nothing was accepted, nothing executed, and freetalon/api.py was never touched. The seeded replan forbidden list is still unproven live (no plan accepted, no replan branch ran), and exit_code-runs-its-command is still unexercised live (no node ever executed).
  The blocking defect is singular and consistent: in all three attempts the model emits scaffold content for api.py with ALL indentation stripped -- every block body flush-left -- so the content cannot parse no matter how the surrounding plan improves. Attempt 2 fixed the interpreter and venv-path defects from attempt 1 and still failed on the same parse error; attempt 3 reverted to bare python and kept the same parse error. The model also keeps re-deriving the module from scratch (a BaseHTTPRequestHandler subclass named HiveAPIServer) instead of extending the real module in place, despite the grounded move naming the Handler class inside HiveAPIServer.start.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff). Suite re-run 869/869 green. No stray files; no new run dir; nothing listening on 8765. GPU survived (mid-run peak 75C at 249W sampled during planning; post-run 48C idle, 300W, x16, zero Xid). Fans restored to auto. Log: logs/llm-end-to-end-health-retry24-2026-11-17.log (221 lines); probe log: logs/probe-retry24-rejections.log.
  Weaknesses for the next fix, in priority order: (1) Scaffold content arrives with indentation stripped -- determine whether the model emits it that way or something in the pipeline (JSON extraction / content handling) strips leading whitespace; if it is the model, the repair prompt after a scaffold-parse rejection should carry the grounded step-1 as real indented code to insert (fill-in-the-blanks), not only prose describing the move -- the parse rejection already names py_compile, but the model repeated the identical defect twice after seeing it. (2) Attempt 2 hardcoded stand-in values (gpu_temp 25, ollama_reachable True) inside unparseable content were never named -- the parse error masks stand-in detection on the same content; consider running the stand-in check on scaffold content even when it does not parse, so one rejection names both. (3) Attempt 3 wrong test path (freetalon/tests/) was masked by the bare-python error on the same node -- interpreter and path checks on one command could both fire. (4) No NEW guard gaps: the seeded replan list and exit_code-command execution remain unproven only because no plan has been accepted since they landed.
- Next: investigate the scaffold indentation collapse, then /health retry #25.
- CI determinism fix (2026-11-17, CPU-only): the f042e53 push failed hosted CI (run 44140283101) on exactly 1 test -- test_retry23_output3_rejected_with_three_errors expected 3 errors, CI got 2 (string_matches + scaffold-parse; the placeholder stub was not named). The environment-sensitive check was _placeholder_scaffold_errors: stub detection is pure text, but the error was gated on _path_exists_on_disk, which checks absolute paths as-is -- the fixture's /home/seeker/projects/FreeTalon/freetalon/api.py exists on seeker7 but not on a GitHub runner. Fix: existence is now judged against the repo the plan targets, via _resolve_plan_target(target, resolve_repo_root(repo_root)).exists() -- the repo-relative resolution other checks already use -- so the same plan validates identically on any machine. The guard is not weakened: a stub scaffolded over an existing module is still rejected everywhere, and a stub to a genuinely new file is still allowed (pinned by test_placeholder_scaffold_to_new_file_still_passes). Reproduced before fixing: a shallow (depth 1) no-.venv clone in freetalon-workspace/ci-sim passed while the main checkout was visible, and FAILED with the byte-identical CI assertion under a mount namespace hiding the main checkout's freetalon/ dir. Tests 869 -> 870/870 green (new regression test test_placeholder_stub_fires_for_foreign_absolute_path pins the foreign-prefix case); TestPlannerFrameworkGuardFix 16/16 in the main checkout AND in the CI-sim clone, plain and with the main checkout hidden. Demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched (219 lines, 0-line diff). Log: logs/ci-fix-f042e53-2026-11-17.log.
- Next: parent pushes this fix; then the sanitize_llm_output U+2581 fix, then /health retry #25.
- Planner sanitize U+2581 fix (2026-11-17, CPU-only): the indentation collapse measured in logs/indent-measure-2026-11-17.log is fixed at its source. sanitize_llm_output (freetalon/orchestrator/planner.py) now converts a bare U+2581 to a space instead of deleting it: the mojibake replacement is unchanged, .replace("▁", "") -> .replace("▁", " "). Ordering worked out from what each artifact is: the UNK_BYTE regex pass runs FIRST and consumes each bracketed artifact whole (the marker inside it included), restoring one space per artifact -- and the artifact hex 0xe29681 IS U+2581's UTF-8 encoding, so the artifact pass and the bare-marker pass restore the same space arriving through two decode paths; the bare-marker conversion therefore only ever sees the bare runs (the indentation) plus any bare mid-line markers. Converting the markers ALONE on the raw wire content does not parse (the measurement's line-9 unterminated string) precisely because the artifacts are still in the text -- the fixed sanitize is the combination. Byte-level proof (tools/probe-sanitize-fix.py; before/after in logs/sanitize-fix-2026-11-17.log): the captured raw wire response (logs/indent-measure-raw.json; 338 U+2581, 122 UNK_BYTE artifacts, all hex e29681) through the fixed sanitize + the normal parse path -- the api.py scaffold (34 lines, 24 indented) and the workspace test_api.py scaffold (16 lines, 10 indented) both ast.parse cleanly with indentation intact ("def handle_health(self):" / "    try:" / "        import subprocess"); before the fix both were flush-left SyntaxErrors. Through the full parse_planner_response, the same generation (retry-#24 attempt 1, reproduced at temperature 0) drops from 4 errors to the 2 non-parse errors (step_2 bare-python criterion command, step_3 ./venv/bin/python). Probe re-run (tools/probe-retry24-rejections.py; logs/probe-retry24-after-sanitize-fix.log): profile UNCHANGED at 4 / 1 / 2 with the parse errors still named -- expected, not a miss: the probe feeds on the driver log's RAW PLANNER OUTPUT blocks, which parse_planner_response prints AFTER sanitize runs (planner.py:691-692), so the log text is post-sanitize and its markers were already deleted before the text existed; no sanitizer change can restore them, and attempts 2/3 have no pre-sanitize capture (their log parse errors carry the identical flush-left signature). No previously-rejected fixture changes verdict, for the same reason: the retry fixtures in the test suite are captured from driver logs (post-sanitize flush-left text; a grep confirms no U+2581 or UNK_BYTE text in tests/), so the conversion never fires on them -- confirmed empirically: no pinned count moved. New class TestPlannerSanitizeFix (6 tests): bare marker runs convert to indentation; bare mid-line markers convert to spaces; the UNK_BYTE + bare-run interaction case from the capture (exact-string equality + ast.parse); mojibake behavior pinned unchanged; plain text unchanged; and a regression test that a U+2581-indented scaffold survives parse_planner_response with content that parses (raw text built with ensure_ascii=False so the markers stay literal, exactly as the wire delivers them). No pinned expectation changed (no test referenced sanitize_llm_output before this class). No guard weakened -- the change is faithful text decoding only; the scaffold parse check still rejects genuinely unparseable content. Tests 870 -> 876/876 green, demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched (219 lines, 0-line diff). Log: logs/sanitize-fix-2026-11-17.log. NOT touched (out of scope, flagged): freetalon/orchestrator/intake.py carries a duplicate sanitize_llm_output with the same deletion, used for TaskIntent parsing.
- Next: parent pushes this fix; then /health retry #25 (first live exercise of the sanitize fix; still unproven live: seeded replan forbidden list, exit_code-runs-its-command, and whether attempt 2's masked stand-in values get named once its content parses).
- Intake sanitize fix DONE (2026-11-17; on top of b3a0dfb): the duplicate sanitize_llm_output in freetalon/orchestrator/intake.py (TaskIntent parsing) carried the same U+2581 deletion the planner fix removed. Side-by-side read of the two copies: step-for-step identical (same UNK_BYTE regex pass, same mojibake 3-char conversion) except the bare-marker replace -- planner converts to a space, intake deleted -- and the explanatory comment block the planner copy gained in b3a0dfb, which the intake copy lacked. Fix is the same one functional line (.replace("▁", "") -> .replace("▁", " ")) plus a condensed comment recording the two-pass ordering rationale (UNK_BYTE pass first, consuming the bracketed artifacts whole; the artifact hex 0xe29681 is U+2581's own UTF-8 encoding, so both passes restore the same space through two decode paths). Before/after demo (freetalon-workspace/tools/demo-intake-sanitize.py): a marker-carrying TaskIntent payload (goal words separated by U+2581 markers, a code-ish constraint value with marker-run indentation, json.dumps with ensure_ascii=False exactly as the wire delivers) parsed BEFORE to goal 'AddaGET/healthendpointtotheAPI' and constraint 'handler:\nreturn{'ok':True}' -- words fused, indentation and inner spaces gone; AFTER it parses to goal 'Add a GET /health endpoint to the API' and constraint 'handler:\n    return {'ok': True}'. New class TestIntakeSanitizeFix (6 tests) mirrors TestPlannerSanitizeFix: bare marker runs convert to indentation; bare mid-line markers convert to spaces; the UNK_BYTE + bare-run interaction case from the capture (exact-string equality + ast.parse); mojibake behavior pinned unchanged; plain text unchanged; and a regression test that a marker-carrying intent survives parse_task_intent_response with spaces and indentation restored. No pinned expectation changed. Tests 876 -> 882/882 green, demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched (219 lines, 0-line diff). Log: logs/intake-sanitize-fix-2026-11-17.log. Future cleanup, deliberately NOT done here (scope discipline): the two sanitize_llm_output copies (planner.py, intake.py) are now behaviorally identical -- deduplicating them into one shared helper is safe to do as its own change.
- Next: parent verifies + pushes this fix; then /health retry #25 (first live exercise of both sanitize fixes; still unproven live: seeded replan forbidden list, exit_code-runs-its-command, and whether attempt 2's masked stand-in values get named once its content parses).
- /health retry #25 (2026-11-17, ~10:43 PST; head 92cd154, planner + intake sanitize fixes) -- FAILED at planning (DRIVER_EXIT:1, no run created). Pre-flight green (52C idle, 129.14W with the model loaded, 300.00W, Gen4 x16, zero Xid, Ollama gemma-3-abliterated:latest, head 92cd154 clean, suite 882/882). Intake succeeded cleanly (valid TaskIntent, goal text intact -- the intake sanitize fix visible live: no fused words). Three planner calls (initial + 2 repairs), ALL rejected with exactly 2 errors -- probe tools/probe-retry25-rejections.py re-ran all three raw outputs through parse_planner_response (probe log logs/probe-retry25-rejections.log): (1) REJECTED with 2 errors, NEITHER a parse error: step_2 criterion command uses bare python (python /home/seeker/freetalon-workspace/test_api.py); step_3 uses ./venv/bin/python, which does not exist in the repo (interpreter-path validation, names ./.venv/bin/python). The step_1 api.py scaffold (34 lines, 24 indented) and the step_2 workspace test scaffold (16 lines, 10 indented) both ast.parse cleanly -- repr evidence in the probe log ('def handle_health(self):' / '    try:' / '        import subprocess'). (2) REJECTED with 2 errors: step_1 is a giant sed -i one-liner whose command does not parse as shell (No closing quotation); step_2 uses bare python (python -m freetalon.cli test ...). (3) REJECTED with 2 errors (the live raised error): step_2 uses bare python (python -m unittest discover); step_1 runs sed -i with the pattern 'def do_GET(self):', which does not occur in api.py, with the grounded move named (add a /health branch to the do_GET method of the Handler class defined inside HiveAPIServer.start; start the server with ./.venv/bin/python -m freetalon.cli start). Budget exhausted -- driver raised at plan_task_intent. Judge/replan never ran.
  Sanitize fix verdict: EXERCISED and CONFIRMED live -- the indentation collapse is gone. Contrast retry #24 attempt 1 (4 errors including 2 scaffold-parse errors on flush-left content): here the same-shaped scaffold plan arrives fully indented, both scaffolds parse, and the only attempt-1 defects are the two interpreter paths. The intake fix is likewise confirmed: the TaskIntent goal reached the planner verbatim. The model still re-derives api.py from scratch (its own handle_health + HiveAPIServer module) instead of extending the real module in place, but the content now parses, so that habit no longer blocks at the parse check.
  Stand-in naming: attempt 1's scaffold computed real values (subprocess nvidia-smi for the GPU temperature, a requests check against localhost:11434 for Ollama), so no stand-in error existed to name -- the retry-#24 masking scenario did not recur in scaffold form. Output 3's sed payload DID hardcode "ollama_reachable": True / "gpu_temp": "N/A" inside a bash sed string; the scaffold stand-in check does not cover bash payloads, and the named errors for that output were bare-python + sed-pattern mismatch.
  Still unproven live: the seeded replan forbidden list (no plan accepted, no replan branch ran; replan prompts are not logged, and no Flask plan appeared -- outputs 2/3 went all-bash sed instead) and exit_code-runs-its-command (no node ever executed).
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff); tree clean throughout -- nothing was accepted or executed, so no restore was needed. Suite re-run 882/882 green. No stray files; no new run dir; nothing listening on 8765. GPU survived (mid-run peak sampled 68C at 249.14W during planning; post-run 39C idle, 300W, x16, zero Xid). Fans restored to auto. Log: logs/llm-end-to-end-health-retry25-2026-11-17.log; probe log: logs/probe-retry25-rejections.log.
  Weaknesses for the next fix, in priority order: (1) The interpreter defect class is now the sole persistent blocker and the model repeats it in every attempt despite each rejection naming ./.venv/bin/python explicitly: bare python appeared in all three outputs and ./venv in output 1. Prompt naming alone is not steering this; consider carrying the exact interpreter string in the grounded skeleton's commands and criteria so the model copies it verbatim, or showing the corrected command for the specific node in the repair prompt. (2) The sed attractor: after attempt 1's scaffold plan was rejected only on interpreter paths, attempts 2/3 abandoned scaffold entirely for giant sed -i one-liners that fail shell quoting and pattern grounding. The repair loop should steer back to the scaffold shape that was one fix away, or sed -i edits to api.py should join the seeded forbidden approaches for this goal shape. (3) Stand-in values inside bash payloads (output 3's hardcoded ollama_reachable / gpu_temp in a sed string) sit outside the stand-in check's scope -- extend stand-in detection to bash heredoc/sed payloads that write .py files.
- Next: fix retry-#25 weaknesses (interpreter steering, sed attractor), then /health retry #26.
- Interpreter-grounding fix (2026-11-17; planner, CPU-only) -- retry #25's three weaknesses addressed. (1) INTERPRETER GROUNDING: the ROUTE GOAL shape now carries a COMMANDS block with the exact interpreter string in copyable slots -- the test run as a quoted bash command ("cd <root> && ./.venv/bin/python -m unittest discover -s tests"), the same string named as an exit_code criterion's "command", and the server start as a quoted bash command -- instead of only naming ./.venv/bin/python in prose and rejection messages. The block rides in the initial prompt, in every repair prompt (via the user prompt), and in replan prompts (via the shape). (2) SED ATTRACTOR: sed -i joins _APPROACH_MARKERS (marker = the sed no-op rejection's own wording) with the scaffold/heredoc replacement, and joins the _repo_forbidden_approaches seed for route goals; and the repair prompt now steers back to the scaffold shape when a scaffold plan failed only on command/criterion strings (_shape_steering_block: the previous output used the scaffold claw AND the rejection names no content defect). Deliberately NOT a validator ban on sed -i: the signal-loss fix (retry #18) pins a matching-pattern sed edit as a valid plan (test_sed_against_existing_pattern_and_write_to_existing_dir_pass), and that design stands -- sed is fought at the prompt layer (seed + forbidden marker + shape steering) plus the existing no-op rejection. (3) STAND-IN SHAPE 3: _static_standin_errors now scans bash payloads that write Python files -- heredoc/echo bodies (_bash_code_writes) and sed -i replacement texts (_sed_script_replacements, with a tolerant remainder for a delimiter inside a quoted string, e.g. "N/A") -- for dict literals binding measured fields (reachability / temperature keys) to constants (_hardcoded_measured_bindings). Computed values (names, calls) are untouched; whole-literal payloads stay owned by shapes 1-2.
  Probe re-run on retry #25's three outputs (logs/probe-retry25-after-interpreter-fix.log): 2 / 2 / 3 -- attempt 3 GAINS the stand-in error naming "ollama_reachable": True / "gpu_temp": "N/A"; attempts 1/2 profiles exactly as recorded (the prompt-side changes cannot alter validation of old outputs -- they are proven by the new prompt/steering unit tests instead). Deliberate pinned-count change: the retry-#18/#19 attempt-2 fixtures go 5 -> 6 errors in nine tests across TestPlannerSignalLossFix / ForbiddenSteerFix / SeededReplanFix / SchemaSignalFix / EmptyEscalationFix -- their step-1 heredoc binds 'ollama_reachable': True / 'gpu_temperature': 0 as constants (the temperature line even comments "Replace with actual GPU temperature retrieval"), a real stand-in defect the old rejection never named; each updated test names the reason. No other fixture moved. Suite 893/893 green (882 baseline + 11 new TestPlannerInterpreterGroundingFix). Demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched (219 lines, 0-line diff). Log: logs/interpreter-grounding-fix-2026-11-17.log.
- Next: parent verifies + pushes this fix; then /health retry #26 (first live exercise of the interpreter grounding, the sed seed, and the shape steering; still unproven live: seeded replan forbidden list, exit_code-runs-its-command).
- /health retry #26 (2026-11-17, ~11:43 PST; head 69c0ece, interpreter-grounding fix) -- FAILED at execution/replan (DRIVER_EXIT:0 -- the driver completed its flow; goal verdict FAIL, escalated at the replan). Run created: run-20261117T194345Z-f15fbd. FIRST plan acceptance since the framework-guard fix -- and the acceptance exposed a validation asymmetry (below). Pre-flight green (49C idle, 300.00W limit, Gen4 x16, zero Xid, Ollama gemma-3-abliterated:latest, head 69c0ece clean, suite 893/893). Intake succeeded cleanly (valid TaskIntent, goal verbatim). Five planner outputs total (2 in initial planning, 3 in the replan). Probe tools/probe-retry26-rejections.py re-ran all five raw outputs through parse_planner_response with the explicit repo root (probe log logs/probe-retry26-rejections.log); root-cause probe tools/probe-retry26-repo-root.py isolated the acceptance mechanism.
  Initial planning: (1) REJECTED with 3 errors: step_2 and step_3 use ./venv/bin/python, which does not exist in the repo (interpreter-path validation, names ./.venv/bin/python -- the check fires on the cd ... && ./venv/bin/python compound form too); step_1 scaffolds api.py with code that does not parse (the model emitted ONE U+2581 marker per indent level, so after the faithful sanitize conversion nested blocks sit at the same 1-space depth and the content cannot parse -- the sanitize fix is not the defect here, the model's marker discipline at depth is). The scaffold also hardcoded ollama_reachable = True / gpu_temp = "N/A" with Placeholder comments, again masked by the parse error on the same content. (2) REJECTED with 2 errors under the explicit repo root (step_2 + step_3 ./venv/bin/python; the scaffold itself parses -- 38 lines, 27 indented, real check_ollama_reachability() via urllib and get_gpu_temperature() via subprocess nvidia-smi) -- BUT ACCEPTED LIVE as plan-7885a4ef9d99. Root cause, measured by the repo-root probe from both the repo and workspace CWDs: the driver calls plan_task_intent(intent) with repo_root=None, and with repo_root=None the bash-interpreter check never fires (output 1 drops to 1 error -- the parse error only -- and output 2 is ACCEPTED), while the explicit repo root rejects both (3 / 2 errors). _call_llm_with_repair has no waiver or salvage path (it returns only a successful parse or raises the last error), so the acceptance is entirely explained by the argument difference. Every unit test and probe passes repo_root explicitly, so the suite pins a strictness the live driver path never applies.
  Interpreter-grounding verdict: NOT exercised as designed -- the model did not copy the grounded string. Across all five outputs there are ZERO occurrences of ./.venv/bin/python in a model-written command; every interpreter reference is ./venv/bin/python (missing dot), including replan output 4's hallucinated freefalcon package. The COMMANDS block did not steer the string this run; what saved the run from a silent bad execution was not the prompt but the (repo-root-gated) validator -- which the live path did not apply. Sed verdict: no sed anywhere in retry #26 -- the retry-#25 sed drift did not recur (repairs and replans stayed scaffold-shaped); one run cannot distinguish the seed/steering as the cause.
  Execution of plan v1 (the accepted output 2): node-1 scaffold PASS on its file_exists criterion -- and overwrote freetalon/api.py wholesale (219 -> 37 lines; the replacement drops HiveAPIServer, the controller, auth, and every other route). node-2 FAILED, honestly: its exit_code criterion RAN its command (cd /home/seeker/projects/FreeTalon && ./venv/bin/python -m unittest discover -s tests) and measured the real exit code 127 (./venv/bin/python does not exist) -- FIRST LIVE PROOF that exit_code criteria run their commands (the f042e53 fix); in retry #23 this node class passed without running anything. node-3 never ran. Judge: replan (informative failure: exit_code 127 != 0). Replan outputs: (3) REJECTED with 6 errors (4 nodes with no criteria at all + 2 interpreter errors); (4) REJECTED with 2 errors (step_3 ./venv/bin/python -m freefalcon.cli start; step_1 writes a Flask app -- the framework guard fired BY NAME in the replan path with the grounded move spelled out; the same output hallucinated the freefalcon package in its test scaffold); (5) not valid JSON (a freenect-import hallucination with an unterminated string) -- escalation: re-planner failed, run FAILED. The accepted plan also never wrote any tests (no test-writing node), itself a goal shortfall the criteria did not catch.
  CORRUPTION: node-1 overwrote freetalon/api.py (git diff: 34 insertions, 216 deletions). Restored via git checkout (api.py back to 219 lines); suite re-run 893/893 green after the restore. No other files changed; no stray files. Endpoint NOT verified live (the server never started; node-3 never ran). GPU survived (peak sampled 77C at 150.77W immediately post-run; post-restore 51C, 43.30W, 300W, x16, zero Xid). Fans restored to auto. Log: logs/llm-end-to-end-health-retry26-2026-11-17.log (362 lines); probe log: logs/probe-retry26-rejections.log.
  Weaknesses for the next fix, in priority order: (1) The repo_root=None validation asymmetry: any caller that omits repo_root (the driver is one) validates WITHOUT the interpreter check -- and any other check gated on a resolved repo root -- while the suite pins the strict behavior. resolve_repo_root(None) should fall back to the repo containing the freetalon package (the grounding the prompt context already effectively uses), so live validation matches the pinned behavior; pin it with a test that parses the retry-#26 output-2 fixture with repo_root=None and expects the 2 interpreter errors. (2) The model still does not copy ./.venv/bin/python verbatim from the COMMANDS block (0 occurrences in 5 outputs). With (1) fixed, the interpreter errors at least fire live and the repair loop gets named defects with budget to fix them; if copying still fails after that, consider naming the corrected command per node in the repair prompt. (3) A scaffold node still replaces an existing module wholesale and passes on a file_exists criterion, and a plan can omit the test-writing node the goal demands and still be accepted -- consider a shrink/route-preservation check for scaffolds overwriting existing modules, and a goal-coverage check for required deliverables. (4) One-marker-per-level indentation (output 1) collapses nested blocks; the parse check catches it, noted for the record.
- Next: fix the repo_root=None validation asymmetry, then /health retry #27.
- Repo-root bypass + overwrite-guard fix (2026-11-17; planner + repo_index, CPU-only) -- retry #26's two measured weaknesses closed. (A) REPO-ROOT ASYMMETRY: resolve_repo_root(None) already fell back (FREETALON_REPO_ROOT env, else the checkout holding the freetalon package); the audit found exactly ONE None-sensitive check -- _bash_interpreter_errors resolved the root only when one was passed (root = resolve_repo_root(repo_root) if repo_root is not None else None, then `if root is None: continue` skipped every path-token existence check), reached from node bash commands AND exit_code criterion commands, which is exactly how the driver path (plan_task_intent with repo_root=None) accepted retry #26's output 2 live. Every other check already resolves unconditionally (sed no-op and missing-directory via index.root-if-present-else-resolve; placeholder via _resolve_plan_target since 7e7e13e; code-append / unwritten-file via disk resolution; scaffold parse is content-only) or reads only the index, and a repo-wide grep confirms the conditional pattern was unique to that one function. Fix: resolve unconditionally; explicit-root behavior unchanged. Probe proof (tools/probe-retry26-repo-root.py, from both the repo and workspace CWDs): None-root and explicit-root profiles are now IDENTICAL -- output 1 rejects with 3 errors (scaffold parse + 2 interpreter), output 2 REJECTS (was ACCEPTED live as plan-7885a4ef9d99). Output 2 shows 3 errors under both roots rather than the pre-fix explicit 2 because direction B adds its overwrite error to the same fixture; direction A alone changes nothing under an explicit root. (B) WHOLESALE-OVERWRITE GUARD: new _scaffold_overwrite_errors + repo_index.qualified_def_names (dotted names of every def/class at any depth). Calibration measured, not guessed: the real api.py has only 2 top-level names but 12 qualified definitions, and the retry-#26 replacement keeps 1 and drops 11/12 (92%). The guard fires only when the target is an existing parseable .py module, the scaffold content parses and is itself module-scale (>= 4 qualified definitions), the existing module has >= 4 qualified definitions, and the scaffold drops >= 3 AND >= 50% of them; the error names the dropped symbols (first 8 + count) and the grounded alternative (scaffold the FULL updated module, or extend in place via a bash heredoc edit). Recalibration, recorded honestly: the first cut (no scaffold-scale floor) fired on the grounded-plan fixtures' 3-line zero-definition branch fragments and broke 93 tests -- a fragment is not a module replacement, so the module-scale floor was added; fragments stay owned by the parse / placeholder / framework / grounding checks, and stub-scale scaffolds stay owned by the placeholder check. (C) Goal-asks-for-tests completeness check DELIBERATELY NOT implemented: it would shift this fix's own pinned profile (retry-#26 output 1 has no test-writing node and must stay at exactly 3 errors), and the /health goal text is the fixture goal for ~25 retries of pinned counts, most plans lacking a test-writing node -- it deserves its own fixture audit; the judge/replan layer already caught the shortfall at run level in retry #26 (goal verdict FAIL).
  Pinned-expectation changes: 27 tests updated, ALL direction-A consequences -- tests that parse with no repo_root (the driver path) now get the interpreter existence check, and every new error names a path that genuinely does not exist; each updated test names the reason. retry-14 v1 3 -> 5 errors in 8 copies plus the ScaffoldGuardFix copy (node-1/node-2 citation tokens name nonexistent repo paths; the 3 prose errors unchanged); retry-14 v2 2 -> 3 in 8 copies (node-3's ./test.sh does not exist; method renamed); retry-15 attempt-2 7 -> 8 in 7 copies (node-1's mangled command names '@app.route(/health)' as a repo path; two methods renamed); CollectsAllErrors 5 -> 7 (node-2/node-3 glued tokens; renamed); retry-21 attempt-5 rewritten in 2 copies -- now 3 errors and NOT a CheapRejectionError (two real interpreter defects plus the placeholder error; cheap covers only stand-in/placeholder fakes). No direction-B fixture moved. New class TestPlannerRepoRootOverwriteFix (10 tests): None-vs-explicit message-identical parity for retry-#26 outputs 1 and 2, criterion-command interpreter parity (negative and positive), placeholder parity control, the overwrite guard rejecting output 2 naming "drops 11 of the existing module's 12 definitions" with _ThreadingHTTPServer / HiveAPIServer.start / HiveAPIServer.start.Handler named, qualified_def_names == 12 for the current api.py, a definitions-preserving scaffold passing under both roots, a new-file scaffold passing, the small-module exemption, and the fragment exemption. Suite 893 -> 903/903 green. Demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched (219 lines, 0-line diff). Log: logs/repo-root-overwrite-fix-2026-11-17.log.
- Next: parent verifies + pushes this fix; then /health retry #27 (first live exercise of the closed bypass and the overwrite guard; direction C -- the goal-asks-for-tests completeness check -- as its own fix with a fixture audit if wanted).
- /health retry #27 (2026-11-17, ~12:37 PST; head 9ea9a43, repo-root bypass + overwrite-guard fix) -- FAILED at planning (DRIVER_EXIT:1; _call_llm_with_repair raised the last rejection out of plan_task_intent, no run created, judge/replan never ran). HEADLINE: the repo_root=None bypass is CLOSED LIVE -- probe tools/probe-retry27-rejections.py re-ran all three raw outputs through parse_planner_response under repo_root=None (the driver path) and the explicit root and the profiles are IDENTICAL (3 / invalid-JSON / 2); in retry #26 that same driver path accepted the 2-interpreter-error output 2, while here the identical defect was rejected live and the raised error names both commands. Pre-flight: suite 903/903 green beforehand (logs/retry27-preflight-suite.log); the original worker recorded none of the other pre-flight values (idle temp, 300.00W, x16, Xid, Ollama tags) to disk -- a logging gap, noted rather than reconstructed. Intake succeeded cleanly (valid TaskIntent, goal verbatim). Three planner outputs (initial + 2 repairs), all rejected: (1) 3 errors -- step_2 and step_3 use ./venv/bin/python, which does not exist in the repo (interpreter-path validation, names ./.venv/bin/python), and step_1's api.py scaffold does not parse: the model again emitted ONE U+2581 marker per indent level, so nested blocks collapse to a single depth (25 lines, 0 lines at 4-space indent; 'expected an indented block after function definition on line 17'); its placeholder bindings (ollama_reachable = True # Placeholder, gpu_temp = "N/A" # Placeholder) were again masked by the parse error on the same content. (2) Not valid JSON -- the response ends mid-node (step_1 unclosed, tab padding after); its scaffold fragment was properly indented but hardcoded ollama_reachable: True / gpu_temp: 'N/A' as constants, never reached by validation. (3) 2 errors, the interpreter pair only (step_2 './venv/bin/python -m unittest discover -s tests'; step_3 './venv/bin/python -m freetalon.cli start'; step_4 'curl -sf http://localhost:8765/health'): the scaffold is the model's best yet -- 42 lines, 30 indented, ast.parse OK, REAL computed values (socket.create_connection to localhost:11434; subprocess nvidia-smi for the temperature), correct plan shape (scaffold -> tests -> start -> curl). Verbatim-copy verdict: STILL failing -- zero occurrences of ./.venv/bin/python in any model-written command across all three outputs; the repair loop received the live interpreter errors for the first time and still could not steer the string in 2 repairs. Overwrite-guard verdict: NOT exercised -- output 3's scaffold carries only 3 qualified definitions (handle, HiveAPIServer, HiveAPIServer.do_GET), below the guard's >=4 module-scale floor, so it is fragment-exempt by the guard's calibration; output 1's scaffold never reached it (parse error first). No sed anywhere in the run.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff); tree clean throughout -- nothing was accepted or executed, so no restore was needed. Suite re-run 903/903 green. No stray files; no new run dir; nothing listening on 8765. GPU survived (run-window peak sampled 75C at 255.42W during planning, logs/retry27-gpu-samples.log; post-run 40C idle, zero NVRM Xid -- the one 'xid' dmesg line is the Realtek NIC's XID 541). Fans were left MANUAL 85% by the stalled run; restored to auto in this wrap-up (GPUFanControlState verified 0). Log: logs/llm-end-to-end-health-retry27-2026-11-17.log (369 lines); probe log: logs/probe-retry27-rejections.log.
  Weaknesses for the next fix, in priority order: (1) Verbatim interpreter copying has now failed live in retries #25, #26 and #27 despite the COMMANDS block, explicitly named rejections, and (as of this run) a live repair loop receiving the errors -- prompting cannot teach this string. Fix deterministically instead: normalize the known-wrong interpreter tokens (./venv/bin/python, bare python) to ./.venv/bin/python in bash commands and exit_code criterion commands at parse time, or have the prompt carry a placeholder token that code expands. (2) One-marker-per-level indentation collapsed output 1's nested blocks again (also retry #26 output 1); the parse check catches it -- noted for the record. (3) Output 2's mid-node truncation wasted a repair attempt on a JSON error; nothing in the pipeline salvages a truncated response -- noted.
- Next: deterministic interpreter-token normalization fix, then /health retry #28.
- /health retry #28 (2026-11-17, ~14:23 PST; head d7aab15, interpreter-normalization fix) -- FAILED at planning (DRIVER_EXIT:1; _call_llm_with_repair raised the last rejection out of plan_task_intent, no run created, judge/replan never ran). HEADLINE: interpreter normalization is PROVEN LIVE -- outputs 1 and 2 write ./venv/bin/python in their step_2/step_3 commands and output 3 writes bare python3, and NOT ONE interpreter error appears anywhere in the run (probe tools/probe-retry28-rejections.py, log logs/probe-retry28-rejections.log); the defect class that killed retries #25, #26 and #27 is gone from the profile. Pre-flight FULLY recorded this time (closing the retry-#27 logging gap): 52C idle, power limit exactly 300.00W, Gen4 x16, zero Xid 79/154, Ollama tags list gemma-3-abliterated:latest, head d7aab15 clean, baseline suite 911/911 green. Intake succeeded cleanly (valid TaskIntent, goal verbatim). Three planner outputs (initial + 2 repairs), each rejected with exactly 1 error, profiles IDENTICAL under repo_root=None and the explicit root: (1) scaffold parse failure -- the one-U+2581-marker-per-indent-level collapse again (25 lines, 0 indented; 'expected an indented block after function definition on line 17'). (2) Overwrite guard -- parse-clean scaffold (38 lines, 27 indented) that drops 11 of the existing module's 12 definitions. (3) Overwrite guard again -- parse-clean scaffold (107 lines, 71 indented) that drops 8 of 12 definitions; its values were still placeholders ('ollama_reachable': True # Replace with actual check, 'gpu_temperature': 'N/A') and its later nodes drifted off-goal entirely (running api.py into squares_out.txt and grepping for 1 / 25 / 5 -- a squares exercise, not /health). The failure frontier has MOVED: the model now insists on replacing api.py wholesale with full-module scaffolds instead of extending it in place, and the overwrite guard -- exercised live for the first time -- caught both module-scale replacements; the repair loop could not steer replacement into in-place extension in 2 repairs. No sed anywhere in the run.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (tree clean throughout -- nothing was accepted or executed, so no restore was needed). Suite re-run 911/911 green. GPU survived (run-window peak sampled 80C at 254.32W, logs/retry28-gpu-samples.log; post-run 50C idle, zero Xid 79/154). Fans manual 85% for the run, restored to auto in this same job (GPUFanControlState verified 0). Nothing listening on 8765. Log: logs/llm-end-to-end-health-retry28-2026-11-17.log; probe log: logs/probe-retry28-rejections.log.
  Weaknesses for the next fix, in priority order: (1) The scaffold strategy: for an existing module the planner reaches for whole-file replacement; it needs a grounded in-place-extension path -- e.g. the route-goal shape presenting the existing module's qualified definitions and steering the scaffold to carry every one of them plus the extension, or a patch/append-style edit node for existing files. (2) One-marker-per-level indentation collapse (retries #26, #27, #28 output 1) still burns one attempt per run; the parse check catches it -- a deterministic marker-run de-collapse at sanitize time is conceivable but risky. (3) Output 3's off-goal drift (squares) was never named by validation -- the overwrite error fired on step_1 and the drift nodes are individually valid bash; only the judge would catch that at run level.
- Next: scaffold in-place-extension steering fix, then /health retry #29.
- Planner extension-steering fix (2026-11-17, CPU-only): retry #28's weakness -- the model replaces freetalon/api.py wholesale instead of extending it in place, and the repair loop cannot steer it out of that shape. Audit (freetalon-workspace/logs/extension-steering-fix-2026-11-17.log, written before the code change): (1) nothing in the initial prompt ever named the module's existing definitions -- the repo context lists only top-level names (api.py: HiveAPIServer, _ThreadingHTTPServer) and the ROUTE GOAL shape named the handler in prose, so 10 of the 12 qualified definitions were invisible until the overwrite guard named the first 8 dropped ones in a rejection; the model was asked to reproduce a 219-line module it had never seen, and attempt 3's restructure (Handler hoisted out of HiveAPIServer.start to module level, dropping all 8 HiveAPIServer.start.Handler.* names) is the predictable result. (2) The repair prompt after a guard rejection carried only the error text (whose dropped list truncates at 8 names), the guard's wording matched no _APPROACH_MARKERS entry (no DO-NOT-repeat block fired), and _shape_steering_block actively MISFIRED -- measured on the real retry-#28 outputs: it told both repair rounds "none of its code was rejected -- keep the same code; fix ONLY the named strings", because the guard's phrase was not in _CONTENT_DEFECT_MARKERS. (3) A scaffold node cannot express a partial edit: the scaffold handler write_text()s the whole content (inputs are file_path + content only); a bash heredoc can append but cannot insert a branch inside an existing method -- so no new node type; the fix is the strongest prompt-layer version. Fix: (a) _existing_module_definitions helper (qualified_def_names of an existing parseable module at the guard's own >= 4 floor; [] for a new, unparseable, or tiny file) and _route_goal_shape now appends an EXISTING MODULE block per serving module naming every qualified definition plus the preservation contract (the scaffold content is the full updated module, every definition in the same place with the same nesting), placed before the COMMANDS block. (b) The guard's phrase "of the existing module's" joins _CONTENT_DEFECT_MARKERS (the shape-steering block now stays silent on overwrite rejections) and _APPROACH_MARKERS (wholesale replacement is named forbidden, with its replacement, in later repair prompts and replan seeds). (c) New _extension_steering_block in the repair prompt: recomputes the failed scaffold against the module on disk with the guard's own thresholds and carries the FULL dropped list (attempt 2: all 11, including HiveAPIServer.stop and HiveAPIServer.start.Handler.log_message, which the guard error truncates away), the FULL existing list, and the concrete in-place pattern. The overwrite guard is untouched: retry-#28 attempts 2 and 3 still reject with exactly 1 error each (pinned). Probe on the real retry-#28 outputs: the shape now lists all 12 definitions; _shape_steering_block is empty for both overwrite rejections (it fired at 489 chars before); _extension_steering_block fires with the full lists; output 1 (parse error) steering unchanged. Suite 911 -> 920/920 green (new class TestPlannerExtensionSteeringFix, 9 tests: shape block contents + initial prompt, helper branches, both fixture rejections unchanged, steering-block behavior on both fixtures, string-only-rejection control, both repair prompts). NO pre-existing pin changed. Demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched (219 lines, 0-line diff).
- Next: /health retry #29.
- /health retry #29 (2026-11-17, ~15:03 PST; head 95a8c8e, extension-steering fix) -- FAILED at planning (DRIVER_EXIT:1; _call_llm_with_repair raised the last rejection out of plan_task_intent, no run created, judge/replan never ran). HEADLINE: extension steering CHANGED the model's approach but not the outcome -- both repair outputs adopt the steering's in-place framing verbatim in their step_1 objective ('Extend .../freetalon/api.py with a /health branch to the do_GET method of the Handler class (defined inside HiveAPIServer.start)'), and the overwrite guard fired on both parse-clean scaffolds; the model still cannot reproduce the module's qualified-name layout: it hoists Handler out of HiveAPIServer.start to module level (dropping all 7 HiveAPIServer.start.Handler.* names plus one more each time), and its final attempt writes the nesting literally as 'class HiveAPIServer.start.Handler', which does not parse. Interpreter errors stayed at ZERO (outputs write ./venv/bin/python; normalization rewrites at parse; probe tools/probe-retry29-rejections.py, log logs/probe-retry29-rejections.log). Pre-flight FULLY recorded: 52C idle, power limit exactly 300.00W, Gen4 x16, zero Xid 79/154, Ollama tags list gemma-3-abliterated:latest, head 95a8c8e clean, baseline suite 920/920 green. Intake succeeded cleanly (valid TaskIntent, goal verbatim). Three planner outputs (initial + 2 repairs), profiles IDENTICAL under repo_root=None and the explicit root: (1) 2 errors -- the overwrite guard (parse-clean 65-line scaffold, 9 qualified defs with Handler's methods hoisted to module level, drops 8 of 12) plus an unusable step_3 criterion (file_exists on /home/seeker/projects/FreeTalon/.venv, a path the node neither writes nor runs). (2) 1 error -- the overwrite guard again: 55 lines, parse OK, 11 qualified defs (a full module-level Handler class with all 7 methods plus HiveAPIServer __init__/start/stop) but still drops 8 of 12 (the 7 nested HiveAPIServer.start.Handler.* names + _ThreadingHTTPServer); its /health values were hardcoded constants (ollama_reachable: True, gpu_temperature: 'N/A'). (3) 1 error -- scaffold parse failure: 78 lines, the model writes 'class HiveAPIServer.start.Handler(BaseHTTPRequestHandler)' (invalid syntax on line 30); the parse error masks any guard verdict on the same content; its health values were placeholder comments ('# Replace with actual check'). No sed anywhere in the run.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff; tree clean throughout -- nothing was accepted or executed, so no restore was needed). Suite re-run 920/920 green. GPU survived (run-window samples in freetalon-workspace/logs/retry29-gpu-samples.log: peak 79C / 258.52W, 0 Xid 79/154; fans manual 85% during the run, restored to auto and verified GPUFanControlState=0; 50C idle after).
  Weaknesses for the next fix, in priority order: (1) The qualified-name layout is the wall: given the full definition list and the full dropped list, the model reproduces the module's SHAPE but relocates Handler to module level (outputs 1-2) or writes the nesting as a dotted class name (output 3). The scaffold contract (whole-file write) forces verbatim reproduction of a 219-line module from a name list alone -- the model has never seen the module BODY. Grounding the scaffold with the module's actual source (or a structural skeleton with real signatures and bodies elided) is the missing input; alternatively a dedicated extend/insert node type would remove the reproduction burden entirely, but that is a new claw surface, not a prompt fix. (2) Output 2's hardcoded health constants (ollama_reachable: True / gpu 'N/A') never got judged on their own: the guard rejection was the only error, so the placeholder signal again rode behind a single-error rejection -- the repair prompt's steering blocks are now the only carrier; check whether the extension block should also name placeholder bindings when present. (3) Output 1's .venv file_exists criterion is a new small defect class (a directory path as a file_exists target the node does not write) -- caught cleanly by the existing unusable-criterion check; no action unless it recurs.
- Next: existing-module source/skeleton grounding for the scaffold shape, then /health retry #30.
- Planner skeleton/source grounding fix (2026-11-17, CPU-only): retry #29's weakness — extension steering worked (repair outputs adopted the in-place framing, scaffold definitions rose 9 -> 11) but the model still could not reproduce the qualified-name LAYOUT of a module it had never seen: outputs 1-2 hoisted the Handler class to module level (overwrite guard, 8/12 dropped), output 3 wrote "class HiveAPIServer.start.Handler(...)" (invalid syntax). Audit (freetalon-workspace/logs/skeleton-grounding-fix-2026-11-17.log, written before the code change; tokens measured with a byte-fallback BPE over models/tokenizer.json, validated against the recorded system-prompt count 3,029 vs 3,030): num_ctx 12288; current initial call for the /health goal 4,644 tokens; api.py full source 9,380 chars / 2,194 tokens, its signature skeleton 1,489 chars / 400 tokens; a successful full-module plan costs ~2,642 tokens to emit; worst realistic repair round (source in the shape + skeleton in the extension block + full-module reply) ~10,850, leaving ~1,400 spare. Fix (planner.py only; overwrite guard and interpreter normalization untouched): _module_skeleton reproduces every def/class header verbatim from the source (decorators, multi-line signatures) with bodies elided to a "..." line, nested definitions at their true indentation, imports verbatim; _existing_module_structure returns the FULL SOURCE at or under 10,000 chars, else the skeleton, hard-capped at 8,000 chars with an explicit truncation marker (the definition list itself is never truncated), gated by the guard's own >= 4-definition floor. _route_goal_shape's EXISTING MODULE block appends the structure (shared by the initial, repair, and replan prompts — all three pinned), and _extension_steering_block appends the skeleton beside the full dropped/existing lists. Fixture proof: retry-#29 outputs reject with identical profiles under both roots (2/1/1 errors — criterion+guard, guard, parse) and retry-#28's with 1 error each; the guard is unchanged. Suite 920 -> 932/932 green (new class TestPlannerSkeletonGroundingFix, 12 tests). Two pre-existing pins narrowed, each with an in-file comment (TestPlannerFakeAndMemoryFix replan anti-echo pins): '"run_id"' -> '"run_id":' — the prompt now legitimately carries api.py's source, whose /gates handler reads a run_id query parameter; the JSON key form the anti-echo rule targets stays absent. Demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched.
- Next: /health retry #30.
- /health retry #30 (2026-11-17, ~15:55 PST; head 1b6de40, skeleton/source grounding fix) -- FAILED at planning (DRIVER_EXIT:1; _call_llm_with_repair raised the last rejection out of plan_task_intent, no run created, judge/replan never ran). HEADLINE: structure grounding did NOT produce a module scaffold at all -- with api.py's full source in the prompt, the model opened by echoing the existing /health body back as its 'plan' ({"ok":true,"status":"ready"}), then an empty nodes array, and in all three real plans (5 nodes each) step_1's scaffold content was a 5-line COMMENT STUB that literally copies the steering text into the file as comments ('# Extend .../freetalon/api.py in place ...', '# Write the FULL module with the extended branch ...', '# Do NOT write a TODO comment -- a comment-only scaffold is rejected') with zero code and zero definitions. The placeholder check rejected every plan; the overwrite guard never fired (a 0-definition stub is not module-scale, so the guard's floor is not met -- the placeholder rule is what caught it). Interpreter errors stayed at ZERO (outputs write ./venv/bin/python and python3; normalization rewrites at parse; probe tools/probe-retry30-rejections.py, log logs/probe-retry30-rejections.log). Pre-flight FULLY recorded: 47C idle, power limit exactly 300.00W, PCIe width x16, zero Xid 79/154, Ollama tags list gemma-3-abliterated:latest, head 1b6de40 clean, baseline suite 932/932 green (retry30-baseline-suite.log). Intake succeeded cleanly (valid TaskIntent, goal verbatim). Five planner outputs (initial calls + repairs), profiles IDENTICAL under repo_root=None and the explicit root: (1) schema rejection -- output is {"ok":true,"status":"ready"}, the current /health response body, no 'nodes' array. (2) schema rejection -- {"nodes":[]}, 'must include at least one plan node'. (3) 1 error -- step_1 placeholder scaffold (comment stub, 5 lines, 0 defs); the rest of the plan was the best-formed yet (real test heredoc booting HiveAPIServer on an ephemeral port and asserting service_status/ollama_reachable/gpu_temperature, grounded CLI start flow, curl verify). (4) 2 errors -- step_1 placeholder again, plus step_2's test heredoc lost the indentation of its final line ('unittest.main()' at column 0 after the __main__ guard, parse error on line 54). (5) 1 error -- step_1 placeholder again; repair budget exhausted. No sed anywhere in the run.
  Endpoint NOT extended, no new tests, no live curl. freetalon/api.py untouched (219 lines, 0-line diff; tree clean throughout -- nothing was accepted or executed, so no restore was needed). Suite re-run 932/932 green. GPU survived (run-window samples in freetalon-workspace/logs/retry30-gpu-samples.log: peak 84C at 277.75W, zero Xid 79/154; fans manual 85% during the run, restored to AUTO after, GPUFanControlState=0 verified; post-run 54C idle). Housekeeping: the first retry-#30 worker exited after starting the baseline suite; this run is the re-dispatch. Stale self-matching pgrep watcher loops left by retries #27/#29 (PIDs 795635, 795770, 879374) were found and killed before launch; GPU samples for this run were taken inside the polling loop, no detached watcher.
  Weaknesses for the next fix, in priority order: (1) The scaffold-emission collapse: shown the full source and told to write the FULL module, the model instead emits the steering instructions themselves as the scaffold's comments -- it will not reproduce a 219-line module as one node among five, and two repairs naming exactly this defect did not move it. The grounded source needs to reach the scaffold by construction, not by asking the model to retype it (e.g. seed the scaffold content from the existing file so the model's job is the delta, or give the extension its own dedicated mechanism instead of a whole-file scaffold node). (2) The enlarged grounded prompt plausibly distracted the opening calls (outputs 1-2 were an echo and an empty plan -- a worse opening than retry #29's), worth watching if the prompt grows further. (3) Minor: one repair's test heredoc dropped the indentation of its last line (parse error) -- the heredoc indentation family again, this time in a test file rather than the scaffold.
- Next: fix the scaffold-emission collapse (grounded source must reach the scaffold by construction, not retyping), then /health retry #31.
- Planner anchored-edit fix (2026-11-17, CPU-only): retry #30's weakness — shown api.py's full source and told to write the FULL updated module, the model emitted step_1's scaffold as a 5-line comment stub copying the steering text into comments (the grounded skeleton's step-1 sketch, verbatim) and the placeholder check rejected all three real plans; a 12B model will not retype a 219-line module as one node among five. Audit (freetalon-workspace/logs/anchored-edit-fix-2026-11-17.log, written before the code change): scaffold content rides in inputs["content"] and the executor write_text()s it whole-file, so the clean seam is parse_planner_response's raw-dict stage, beside interpreter normalization. Fix (planner.py + tests; the overwrite guard, the placeholder check, interpreter normalization, and the executor untouched): a scaffold node for an existing parseable module may carry an ANCHORED EDIT — inputs "anchor" (a qualified definition name), "position" ("replace" substitutes the definition; "after"/"before" insert a sibling next to it), and "content" holding ONLY the new code at its exact file indentation (for "replace", the complete definition, def line included). _materialize_plan_edits splices each edit into the current file at the anchor's ast span (decorators included; _definition_spans mirrors repo_index.qualified_def_names exactly, descending only through def/class nodes) BEFORE normalize_plan_nodes, validates the materialized module (it parses; every existing qualified definition is still present), and rewrites the node to the legacy full-content form — the schema check, every content check, and the executor see exactly today's full-content nodes. Errors are named and steerable (position without a usable anchor, invalid position, missing target, non-Python target, nonexistent / unparseable / under-floor module, placeholder or comment-only delta, indentation mismatch, a "replace" delta that is not the one definition — fragment, whole module, or wrong name — and a splice that does not parse or loses definitions); a rejection whose only errors are placeholder deltas is a CheapRejectionError carrying the "placeholder content only" phrase, so the repair loop answers it with the grounded skeleton. Multiple edits of one module in a plan accumulate against each other's results, never the stale file. Steering teaches the edit form as PREFERRED on every surface that carried "write the FULL module": _route_goal_shape's EXISTING MODULE block (which now also names the goal's natural anchor, derived from the route branch's ast.If via the innermost containing definition — HiveAPIServer.start.Handler.do_GET for /health — while the full source/skeleton stay in the prompt for anchor choice and exact indentation, reframed as "do NOT retype it"), _extension_steering_block (the dropped definitions return by construction; the full dropped/existing lists and the skeleton are kept, the FULL updated module remains as the fallback), the _APPROACH_MARKERS replacement clause, and _grounded_skeleton's step-1 — the literal source of retry #30's stub — now an anchored edit of the handler definition with the branch sketch as the delta blank (the legacy full-content step-1 is kept when no anchor is derivable). Fixture proof: retry-#30 outputs 3/4/5 reject with identical profiles under both roots (1/2/1 errors, placeholder-led) and the retry-#28/#29 fixtures keep theirs (their classes re-run green). Suite 932 -> 959/959 green (new class TestPlannerAnchoredEditFix, 27 tests: byte-exact materialization for replace / insert-after / insert-before at method, class, and module level; decorated and multi-line definitions; same-module accumulation; the grounded /health plan with an edit step-1 and the completed grounded skeleton both accepted end-to-end; every error case; steering pins; anchor helpers). One pre-existing pin changed (TestPlannerPlaceholderCheapFix.test_grounded_skeleton_shows_the_branch_sketch): "Write the FULL module" asserted present -> asserted ABSENT, with step-1's anchor/position now pinned — the phrase is the retry-#30 defect. Demo tools/demo-framework-guard.py ALL CHECKS PASSED; M5 smoke OK. freetalon/api.py untouched.
- Next: /health retry #31.
- /health retry #31 (2026-11-17, ~16:42 PST; head 6af3ec9, anchored-edit scaffold fix) -- FAILED at planning (DRIVER_EXIT:1; _call_llm_with_repair raised the last rejection out of plan_task_intent, no run created, judge/replan never ran). HEADLINE: the anchored-edit form was USED -- output 3's step_1 carried exactly the steering's named anchor (HiveAPIServer.start.Handler.do_GET, position 'replace') -- but the model filled the edit's content with the grounded skeleton's delta-blank instruction text verbatim instead of writing the method, and materialization rejected it at the indentation gate (the content starts at 0 spaces while the anchor sits at 12 in the file). The machinery worked as designed (the derived anchor was chosen, the error is named and steerable, no module was produced from a bad delta); the failure moved one step later -- the model copies whatever prose occupies the example's content slot. The single repair then REGRESSED: output 4 abandoned the edit form for a garbled full-content Flask scaffold (framework guard + parse failure), dropped criteria from every node, and drifted off-goal (do_GET 'to include logging'). Interpreter errors stayed at ZERO (outputs write ./venv/bin/python and python3; normalization rewrites at parse; probe tools/probe-retry31-rejections.py, log logs/probe-retry31-rejections.log). Pre-flight FULLY recorded: 52C idle, power limit exactly 300.00W, PCIe width x16 (gen 4), zero Xid 79/154, Ollama tags list gemma-3-abliterated:latest, head 6af3ec9 clean, baseline suite 959/959 green (retry31-baseline-suite.log). Intake succeeded cleanly (valid TaskIntent, goal verbatim). Four planner outputs (initial + repairs), profiles IDENTICAL under repo_root=None and the explicit root: (1) schema rejection -- output is {"ok":true,"status":"ready"}, the current /health response body, no 'nodes' array. (2) schema rejection -- {"nodes":[]}, 'must include at least one plan node'. (3) 1 error -- the anchored-edit indentation rejection above; the plan's other four nodes were again well-formed (a real test heredoc booting HiveAPIServer on an ephemeral port and asserting service_status/ollama_reachable/gpu_temperature with type checks, the grounded CLI start flow with a curl wait loop, live curl verify). (4) 5 errors -- step_1/2/3 all missing criteria; step_1's full-content scaffold does not parse (unexpected unindent on line 10) and writes a Flask app (framework guard). No sed anywhere in the run.
  Endpoint NOT extended, no new tests, no live curl (port 8765 not answering post-run). freetalon/api.py untouched (219 lines, 0-line diff; tree clean throughout -- nothing was accepted or executed, so no restore was needed). Suite re-run 959/959 green. GPU survived (run-window samples in freetalon-workspace/logs/retry31-gpu-samples.log: peak 71C / 246.62W, 0 Xid; fans restored to auto and GPUFanControlState=0 verified).
  Weaknesses for the next fix, in priority order: (1) The delta-blank echo: the grounded skeleton's step-1 presents its delta as instructional comment prose, and the model copies that prose into the edit content verbatim -- retry #30's stub echo, one level down. The blank should not be copyable prose: present the delta as real skeleton CODE (the actual do_GET at its 12-space indentation with the extended branch sketched in code, so a copy is at least a parseable, correctly-indented delta for materialization and the content checks to judge), or remove the in-prompt blank and let the repair errors teach the shape. (2) Repair instability: after output 3's near-miss the repair produced the run's worst output (Flask regression, criteria dropped everywhere) -- the long error list may be drowning the anchored-edit steering in the repair prompt. (3) Outputs 1-2 (the /health echo, then an empty plan) have now opened two runs in a row -- the enlarged grounded prompt's opening-call distraction is a pattern, not noise.
- Next: fix the delta-blank echo (skeleton delta as real code, not copyable prose), then /health retry #32.
- Planner delta-blank fix (2026-11-17, CPU-only): retry #31's weakness — output 3 used the anchored edit CORRECTLY (anchor HiveAPIServer.start.Handler.do_GET, position 'replace') but filled the content slot with the grounded skeleton's delta-blank instruction prose; its re-wrap dropped the '#' markers from the prose lines, so is_placeholder_content/_is_placeholder_stub both missed the echo and materialization rejected it at the indentation gate (0 vs 12 spaces), naming the wrong defect at full price. Audit (freetalon-workspace/logs/delta-blank-fix-2026-11-17.log, written before the code change): every example/skeleton content slot in planner.py inventoried — the anchored step-1 delta was the only fill-in slot carrying instruction prose; the legacy no-anchor blank and the no-branch TODO blank are KEPT deliberately (their verbatim copies are all-comment and placeholder-rejected by design; real code there would make a copy an accepted no-op); shape blocks, extension steering, shape examples, repair/replan surfaces carry prose only AROUND real code/commands, never in a slot. Fix (planner.py + tests; the overwrite guard, the placeholder check on materialized content, interpreter normalization, and the executor untouched): (1) The delta blank is now REAL CODE — _anchored_delta_sketch slices the anchored definition's current source verbatim from the module (real signature and body at its true file indentation, decorators included) and extends the route branch in code form: the branch's real response dict bound to a payload name, one payload["<field>"] = ... line per derived response field, and the branch's real return with the dict replaced by the payload — every expression from the source segments, at the branch body's real indentation; any other branch shape (or no derivable fields) leaves the definition unchanged. The field derivation is factored into _SKELETON_ROUTE_FIELDS, shared by step-1's sketch and step-2's assertions (step-2's generated test code byte-identical). The instructions moved OUT of the slot into the step-1 guidance text. A verbatim copy now copies plausible code at the right indentation and materialization judges it on content. (2) Materialization detects comment/prose-only deltas at the placeholder check — which already sat BEFORE the indentation gate; the defect was detection, not position — via _delta_carries_code (the dedented delta parses with a real statement, or a non-comment line is code-shaped: keyword-led, decorator, assignment, call/bracket/string/number start). The echoed blank is now named "placeholder content only (comments, prose, and/or a docstring — no code)" with placeholder=True, so it is a CheapRejectionError and the repair round gets the free re-prompt WITH the grounded skeleton; real code at the wrong indentation still hits the indentation gate with both indents stated. Fixture proof (probe-retry31-rejections.py re-run): retry-#31 output 3 rejects with 1 error under both roots — the placeholder rejection, renamed from the indentation rejection (the correct name for the defect); output 4 unchanged at 5 errors; retry-#30 outputs unchanged (1/2/1), retry-#29 (2/1/1), retry-#28 (1/1/1). Suite 959 -> 967/967 green (new class TestPlannerDeltaBlankFix, 8 tests: the delta is real code — parses as the anchored definition, branch in code form, 12-space first line, all old blank phrases absent from the slot; step-2 assertions byte-identical; generic sketch shapes — no-fields route and non-dict branch stay verbatim, unknown anchor is None; _delta_carries_code cases; the old blank verbatim as a cheap placeholder rejection, never an indentation error; real code at the wrong indentation still hitting the gate; the retry-#31 fixtures). Two pre-existing pins changed, both because the behavior they pinned IS the defect: TestPlannerAnchoredEditFix.test_verbatim_skeleton_rejected_as_placeholder_delta -> test_verbatim_skeleton_materializes_as_a_real_plan (the verbatim skeleton's step-1 is real code now, so it materializes and validates — the run's tests judge the "..." sketch values), and TestPlannerPlaceholderCheapFix.test_grounded_skeleton_shows_the_branch_sketch (the old blank's phrases and is_placeholder_content(content)==True inverted: phrases gone from the slot, content is real code). Demo tools/demo-framework-guard.py ALL CHECKS PASSED. freetalon/api.py untouched. NOTED, not chased (per the fix brief): output 4's repair regression followed a non-cheap, wrongly-named rejection; with output 3's defect now cheap and correctly named, that repair round gets the skeleton-bearing free re-prompt instead — the structural mitigation already in the design. If the regression recurs after a cheap placeholder rejection, that is the next fix's evidence.
- Next: /health retry #32.
- /health retry #32 (2026-11-17, ~17:25 PST; head 38d30eb, delta-blank fix -- skeleton delta as real code) -- FAILED at planning (DRIVER_EXIT:1; _call_llm_with_repair raised the last rejection out of plan_task_intent, no run created, judge/replan never ran). HEADLINE: the model copied the new real-code skeleton delta -- output 3's step_1 again carried exactly the steering's named anchor (HiveAPIServer.start.Handler.do_GET, position 'replace') with the real do_GET as its content -- but DEDENTED: the copy stripped the uniform 12-space file indentation (def at column 0, body at 4), so materialization rejected it at the indentation gate (content starts at 0 spaces while the anchor sits at 12 in the file), and the delta still carried the sketch's '...' (Ellipsis) literals for all three new payload fields -- the model reproduced the sketch faithfully instead of completing it with the real Ollama/GPU computation. The delta-blank fix's detection worked as designed: the delta carries real code, so the placeholder gate correctly did NOT fire and the rejection named the indentation defect (in retry #31 the same slot held prose and reached this same gate under the wrong name; now prose and code are told apart). The overwrite guard never fired (materialization rejects before a module exists for the guard to judge) and no plan was accepted, so the '...' values were never judged by the run's own tests -- step_2's test asserts ollama_reachable is a bool and gpu_temperature is (int, float), which Ellipsis fails; that judgment is the design's intended path and planning never reached it. The single repair then REGRESSED exactly as in retry #31: output 4 abandoned the edit form for a garbled full-content Flask scaffold (framework guard + parse failure) and dropped criteria from every node. Interpreter errors stayed at ZERO (outputs write ./venv/bin/python and python3; normalization rewrites at parse; probe tools/probe-retry32-rejections.py, log logs/probe-retry32-rejections.log). Pre-flight FULLY recorded: 52C idle, power limit exactly 300.00W, PCIe width x16 (gen 4), zero Xid 79/154, Ollama tags list gemma-3-abliterated:latest, head 38d30eb clean, baseline suite 967/967 green (retry32-baseline-suite.log). Intake succeeded cleanly (valid TaskIntent, goal verbatim). Four planner outputs (initial + repairs), profiles IDENTICAL under repo_root=None and the explicit root: (1) schema rejection -- output is {"ok":true,"status":"ready"}, the current /health response body, no 'nodes' array. (2) schema rejection -- {"nodes":[]}, 'must include at least one plan node'. (3) 1 error -- the anchored-edit indentation rejection above; the plan's other four nodes were again well-formed (a real test heredoc booting HiveAPIServer on an ephemeral port and asserting service_status/ollama_reachable/gpu_temperature with type checks, the grounded CLI start flow with a curl wait loop, live curl verify). (4) 5 errors -- step_1/2/3 all missing criteria; step_1's full-content scaffold does not parse (unexpected unindent on line 10) and writes a Flask app (framework guard). No sed anywhere in the run.
  Endpoint NOT extended, no new tests, no live curl (port 8765 not answering post-run). freetalon/api.py untouched (219 lines, 0-line diff; tree clean throughout -- nothing was accepted or executed, so no restore was needed). Suite re-run 967/967 green (retry32-postrun-suite.log). GPU survived (run-window samples in freetalon-workspace/logs/retry32-gpu-samples.log: peak 76C / 247.07W, 0 Xid pre- and post-run; fans manual 85% for the run, restored to auto and GPUFanControlState=0 verified). Note: the GitHub Actions gpu-tests run for the 38d30eb push was executing concurrently in the runner's own work dir during pre-flight (separate tree, no interference; left running).
  Weaknesses for the next fix, in priority order: (1) The dedented copy: the model strips the uniform leading indentation when it copies the real-code delta -- the same copy-normalization as retry #31's dropped '#' markers, now applied to indentation. Two runs of evidence say the copy normalizes whatever it carries, so construction should absorb it: materialization knows the anchor's exact file indentation, and a 'replace' delta whose def line is the anchored definition at a UNIFORM offset could be re-indented to the anchor's indentation before splicing and then judged on content (parse + definition retention, as today) -- output 3 would then materialize and its '...' values would meet the run's tests, the design's intended judgment path. (2) The '...' completion gap: even a faithfully indented copy leaves the three payload fields as Ellipsis literals -- the sketch shows WHERE the values go, but the model must still write the real computation (an HTTP check of localhost:11434, an nvidia-smi read); if (1) lands, this becomes an execution/judge-stage failure with a run id and real test output to steer the replan, which is new evidence either way. (3) Repair instability is now a three-data-point pattern: after a 1-error near-miss the repair regresses to the Flask scaffold with criteria dropped everywhere (retry #31 output 4, retry #32 output 4) -- the delta-blank fix entry flagged exactly this recurrence as the next fix's evidence, and it has recurred, both times after a non-cheap single-error rejection. (4) Outputs 1-2 (the /health echo, then an empty plan) have now opened three runs in a row.
- Next: fix the dedented-delta copy (materialization re-indents a uniformly-shifted 'replace' delta to the anchor's file indentation, then judges content as today), then /health retry #33.
- Planner re-indentation fix (2026-11-17, ~18:05 PST; for retry #33) -- materialization now absorbs a uniformly-shifted anchored-edit delta, and materialization rejections get concrete repair steering. AUDIT FIRST (logs/reindent-fix-2026-11-17.log; probes tools/probe-reindent-audit.py + probe-reindent-audit2.py): (a) the indentation gate compared the delta's first-line indent to the anchor span's ast col_offset -- the same column for 'replace' and for an 'after'/'before' sibling, so one re-indent rule serves all three positions; (b) repair steering measured on retry #32 output 3's real rejection text: _extension_steering_block returned 0 chars (its trigger is the overwrite guard's phrase alone) while _shape_steering_block FIRED at 489 chars, telling the repair round "none of its code was rejected -- keep the same code; fix ONLY the named strings" about a rejection whose only defect was the delta's indentation -- the retry-#28 misfire one level down, and the plausible mechanism for the third consecutive near-miss -> Flask repair regression; (c) output 3 with a hand-applied uniform re-indent (+12 on every non-blank line): step-1 materializes (parse OK; 12/12 qualified definitions; overwrite guard, placeholder/stub rules, and framework guard all silent) and the three '...' payload lines survive verbatim into the materialized module -- by design no planning check judges them (the plan's own step-2 test asserts the fields' types, which Ellipsis fails, so an accepted plan meets the judge/replan loop at execution); the FULL output-3 plan still rejects with exactly 1 error, on step-2 -- its test heredoc does not parse (unittest.main() at column 0 under the __main__ guard), a defect the live run masked because materialization raises before the full-plan checks run. FIX (planner.py only): (1) _materialize_scaffold_edit re-indents a delta that differs from the anchor's indentation ONLY by a uniform shift -- dedented by its own base indent it is coherent (for 'replace', exactly the one anchored definition; for 'after'/'before', it parses as sibling code) -- and proceeds; non-uniform or incoherent deltas keep the indentation error, both indents stated, message text unchanged; no new rejection for '...' values (deliberate). (2) The four materialization stems ("anchors an edit", "anchors its edit", "anchors a 'replace' edit", "an anchored edit") joined _CONTENT_DEFECT_MARKERS, so the shape block stays silent on materialization rejections (mirroring the retry-#28 guard fix), and the new _materialization_steering_block mirrors _extension_steering_block for the edit-form failure: the module's FULL definition list, the anchor's own indentation, the module's skeleton, and the keep-the-edit-form pattern; the placeholder-delta cheap rejection is excluded (its repair round already gets the grounded skeleton). Guard, placeholder check on materialized content, interpreter normalization, and the executor are unchanged. TESTS: new TestPlannerReindentFix (14 tests: uniform dedent and uniform over-indent byte-match the correctly-indented splice; nested anchors; decorated defs; after/before absorbed, pinned either way; non-uniform and incoherent deltas still rejected at the gate with both indents; the retry-#32 output-3 fixture pinned to its new profile under both repo roots; steering pins including an end-to-end repair-prompt test), plus a positive re-indent test in TestPlannerAnchoredEditFix. Two pre-existing pins changed, both pinning the defect itself (a uniformly dedented real definition, now absorbed): TestPlannerAnchoredEditFix.test_indent_mismatch renamed test_non_uniform_indentation_still_rejected, and TestPlannerDeltaBlankFix.test_real_code_at_wrong_indentation_still_hits_the_gate (same name; its delta is now non-uniform). Retry #28-#31 fixtures unchanged. Suite 967 -> 982/982 green (logs/reindent-full-suite.log); demo-framework-guard ALL CHECKS PASSED. Committed locally, NOT pushed -- parent verifies, pushes, and dispatches retry #33.
- Next: /health retry #33.
- /health retry #33 (2026-11-17, ~18:20 PST; head c736d31, re-indent fix -- materialization absorbs a uniform re-indentation of a real-code delta + materialization steering block in repair prompts) -- FAILED at EXECUTION, a first (goal verdict FAIL, escalated: timed-out nodes exhausted their retries; DRIVER_EXIT:0 -- the driver exits 0 once a run completes; planning failures are what raise exit 1. Run run-20261118T022058Z-04bf5c created, elapsed 300.0s, plan plan-7885a4ef9d99 accepted on the 4th planner output). HEADLINE: the re-indent fix WORKS live. Output 3's step_1 was the anchored edit (anchor HiveAPIServer.start.Handler.do_GET, position 'replace') carrying the full do_GET DEDENTED to column 0 -- the exact uniform 12-space strip that killed retry #32 at the indentation gate -- and materialization ABSORBED it: probe replay (tools/probe-retry33.py, logs/retry33-probe.log; profiles identical under repo_root=None and the explicit root) shows step_1 produced NO error, and the plan's ONLY rejection was 1 error on step_2, the test heredoc's unindented unittest.main() under the __main__ guard ('expected an indented block after if statement on line 54') -- precisely the defect the re-indent fix audit predicted the live run would unmask once materialization stopped raising first. The materialization steering block was NOT exercised (output 3's rejection was a step_2 parse error, not a materialization rejection, so output 4's repair prompt was the ordinary one). Output 4 (repair) did NOT regress to Flask -- the first non-Flask repair in four retries -- but it abandoned the edit form for a simplified 2-node legacy full-content plan whose step_1 content is a 5-line placeholder fragment (a bare def do_GET, '# ... existing code ...', one /health branch returning {"ok":True, "status":"healthy"}, '# ... remaining code ...'), and that plan was ACCEPTED (probe: ACCEPTED, 2 nodes, both roots): the placeholder check did not fire (the fragment carries a real def + branch, so it is not comment-only) and the overwrite guard did not fire (no anchored edit to materialize, and the fragment retains none of the module's qualified definitions to count). The run EXECUTED: node-1 PASS (exit 0, 0.0s) and CLOBBERED freetalon/api.py 219 -> 5 lines (fragment preserved at logs/retry33-api-clobbered-fragment.py; restored via git checkout; post-run suite 982/982 green). Node-2 ran the full suite as a single bash node and TIMED OUT at the 300s node timeout (the suite takes ~360-385s) -- FAIL; the orphaned suite process finished on its own afterward. No live /health was ever served (the accepted plan had no server step; post-run curl to localhost:8765/health: connection refused), so no live evidence exists. Output 3's delta also still carried the sketch's '...' Ellipsis values for service_status/ollama_reachable/gpu_temperature -- never judged; execution of that plan never happened, and its own step_2 type assertions (bool, (int, float)) would have failed them. Intake clean (valid TaskIntent, goal verbatim). Four planner outputs: (1) schema rejection -- {"ok":true,"status":"ready"} echo. (2) schema rejection -- {"nodes":[]}. (3) 1 error -- step_2 heredoc parse only; step_1 absorbed by the re-indent path (its other nodes were well-formed: the grounded ephemeral-port test, CLI start with a curl wait loop -- though step_4 wrote ./venv/bin/python without the leading dot in two commands, and step_3 ran the workspace test with bare python3). (4) ACCEPTED -- the 2-node placeholder-fragment plan above. Pre-flight fully recorded (logs/health-retry-33-2026-11-17.log): 48C idle, power limit exactly 300.00W, PCIe width x16, zero Xid 79/154, Ollama tags list gemma-3-abliterated:latest, head c736d31 clean, baseline suite 982/982 green (Ran 982 in 384.7s, OK). GPU peak 77C / 251W during planning, 0 Xid for the run; fans manual 85% during the driver, auto after (GPUFanControlState=0 verified). Post-run suite 982/982 (Ran 982 in 361.2s, OK) after the api.py restore. No sed anywhere in the run.
  Weaknesses for the next fix, in priority order: (1) The full-content placeholder/elision hole -- now the top defect, with an executed clobber as evidence: a legacy full-content scaffold for an EXISTING module whose body is elision comments around one real branch passes the placeholder check (not comment-only) and the overwrite guard (nothing to count) and executes as a module overwrite. A scaffold targeting an existing module should face the same definition-retention judgment whether or not it uses the edit form, and elision markers ('# ... existing code ...' / '# ... remaining code ...') in scaffold content for an existing file should be a named rejection. (2) The suite-as-a-node timeout mismatch: a bash node running the full suite can never pass a 300s node timeout when the suite takes ~360-385s -- either planner steering should stop proposing the full suite as one bash node for this goal, or test-node timeouts must exceed suite duration; as-is, even a perfect endpoint plan ending in a suite node fails at execution. (3) The heredoc __main__-guard defect (output 3's step_2): test heredocs keep emitting unittest.main() at column 0 under the guard; the parse rejection names it precisely, but the model has now produced it in the audit fixture and live -- a heredoc-shape steering line or a targeted repair hint is the lever. (4) The '...' completion gap remains unjudged for the third consecutive retry -- the intended judgment path (the run's own tests) is still unreached, now blocked by (1)-(3) rather than by materialization.
- Next: fix the full-content placeholder/definition-retention hole, then /health retry #34.

## Update - 2026-11-17 (planner A/B refactor + Checkpoint 1 rescore)

- Planner parse/validate path refactored into composable stages
  (sanitize -> interpreter-normalize -> materialize -> validate)
  with structured errors: every rejection now carries PlanError
  records (stable code + message + node id + stage) on
  exc.plan_errors, collected by one aggregator; every existing
  message text is byte-identical. Repair steering (shape /
  extension / materialization blocks, rejected-approach seeding,
  the placeholder-skeleton branch) keys off error CODES, not
  message substrings -- the mechanism behind both historical
  steering contradictions (retries #28 and #33). Validators read
  each module file once per parse via a shared source cache.
- Defect closures (the three retry-#33 weaknesses):
  (1) Elision hole CLOSED. New 'elision' rejection: a full-content
  scaffold of an existing module carrying elision markers
  ('... existing code ...' style) is rejected by name; the
  retention guard lost its scaffold-scale floor, so fragment
  scaffolds face definition-retention judgment like everything
  else (the guard defers to the elision rule when both apply --
  one error per defect).
  (2) Suite-node timeout CLOSED executor-side: a bash node that
  runs the full suite (unittest discover / bare unittest / bare or
  tests-dir pytest) gets a 900s timeout floor in the executor
  (max of declared and floor); validator steering cannot create
  executability, and the executor guarantee covers any plan
  source.
  (3) The heredoc Python parse check is now a first-class
  validator ('heredoc_parse'), and _bash_code_writes extracts tee
  heredoc writes (cat <<M | tee T and tee T <<M) that were
  previously invisible to every payload check.
- Checkpoint 1 rescore (tools/replay-planner-corpus.py, 28 captured
  outputs x 2 repo-root variants, offline): OLD planner accepted
  4/28 (14.3%), accepted-sound 0/28, mean errors/output 1.21, wall
  16.69s. NEW: accepted 0/28, accepted-sound 0/28, mean
  errors/output 1.36 overall and 1.36 per REJECTED output (old:
  1.42). The only changed rows are the 4 unsound accepts
  (retry28-1, retry29-1, retry33-4, llama3-2), each now rejected
  with exactly 1 error (scaffold_overwrite x3, elision x1); the
  other 48 rows are identical in verdict and error count. Wall
  16.24s. Accepted-sound stays 0 because the corpus holds no
  acceptable plan (all 28 are historical failures) -- CP2 live
  runs are where sound accepts can appear. Logs:
  logs/planner-ab-replay-new.log (+ .json) vs
  logs/planner-ab-replay-old.log (+ .json).
- Pins changed (each encodes one of the three defects):
  GROUNDED_API_CONTENT fixtures (14 sites) now extend api.py via
  the anchored-edit form (GROUNDED_DO_GET_DELTA) instead of a
  fragment full-content scaffold -- the fragment is the
  retry28-1 shape; retry27 output 3 pin flipped from accepted to
  rejected-by-guard (its scaffold deletes 11 of api.py's 12
  definitions; the single-error profile still proves interpreter
  normalization); retry19 attempt 3 counts 2 -> 3 in four tests
  (step-1's Flask fragment now also faces retention); the retry25
  attempt-1 repair-prompt test flipped (its rejection is no
  longer interpreter-only, so no shape blessing -- the extension
  block fires instead); two http.server acceptance fixtures now
  scaffold api.py's own current source (the fragment form is a
  defect-1 overwrite); the Flask-isolation test counts 1 -> 2
  errors; the U+2581 sanitize fixture retargeted to a scratch
  module; steering/approach tests now pass error codes instead of
  message text; demo-framework-guard.py's GROUNDED_PLAN converted
  to the same anchored edit (46 checks, ALL CHECKS PASSED).
- Suite: 997/997 green (982 + 15 new: 9 planner refactor/closure
  tests, 6 executor suite-timeout tests). Ran 997 in 378.1s, OK.
- Next: CP2 -- 3 live /health runs on the refactored planner;
  reassess with Pat if no improvement after refactor + 2 cycles.

## Update - 2026-11-17 (Checkpoint 2: 3 live /health runs on the refactored planner)

- CP2 live A/B, new-planner arm: 3 driver runs of the /health goal
  on c334196 under full retry discipline. Pre-flight: GPU 52C idle,
  power limit exactly 300.00W, link x16, 0 Xid 79/154, Ollama tags
  list gemma-3-abliterated:latest, head c334196 clean; baseline
  suite 997/997 green (Ran 997 in 388.9s, OK). Fans manual 85%
  during each run, auto after, GPUFanControlState=0 verified after
  each run. All 3 runs DRIVER_EXIT=1, FAILED at planning; no run
  changed any file (porcelain 0 after each; nothing to restore);
  live /health never reached.
- DETERMINISM: the 3 runs are byte-identical -- same INTENT
  (md5 8abb5a6382a1687e22b7e5e4a85233b8) and all 4 raw planner
  outputs md5-identical across runs. At planner temperature 0 the
  live arm is one deterministic trace replicated 3x; CP2's live n
  is effectively 1 distinct sample.
- Per-run attempt profile (identical every run; structured codes
  from tools/probe-cp2.py replays, same verdicts under
  repo_root=None and the explicit root): output 1, the schema echo
  {"ok":true,"status":"ready"} -> missing_nodes_array@schema;
  output 2, {"nodes":[]} -> empty_plan@schema; output 3, the
  5-node anchored-edit plan (dedented full do_GET delta absorbed
  by the re-indent rule) -> rejected with exactly 1 error,
  heredoc_parse@validate[step-2] (the test heredoc leaves
  unittest.main() dedented out of the __main__ guard: "expected an
  indented block after 'if' statement on line 54"); output 4
  (repair) regresses to the 2-node elision-fragment scaffold ->
  elision@validate[step_1], rejected by name.
- Scoreboard vs the old-model live baseline (retries #28-#33,
  6 runs): accepted 1/6 old (retry #33, unsound) -> 0/3 new;
  accepted-sound 0/6 -> 0/3; end-to-end successes 0 -> 0. The two
  old-arm failure signatures are now planning-stage rejections:
  the plan class retry #33 ACCEPTED is stopped pre-execution by
  heredoc_parse, and #33's accepted fragment family is stopped by
  elision -- zero file damage across CP2, vs api.py clobbered and
  restored in #33. Accepted-sound did NOT move above zero: the
  binding defect is now the model's step-2 heredoc indentation,
  and the repair output still regresses in form (code-keyed
  steering contains the regression safely; it does not prevent
  the model from producing it).
- GPU: peak temps observed 79C (run 1), 77C (run 2), 82C (run 3),
  all under the 85C pause threshold, no thermal intervention;
  0 Xid 79/154 across CP2. Log:
  logs/health-cp2-2026-11-17.log; per-run code probes
  logs/cp2-run{1,2,3}-probe.txt; raw traces logs/cp2-run{1,2,3}.out.
- Next: reassess with Pat per the A/B framework (refactor + CP1 +
  CP2 complete; unsound-accepts are closed, accepted-sound still
  0 -- the remaining gap is model-side plan substance, not
  planner soundness).

## Update - 2026-11-18 (Planner cycle 2: two-channel envelope, payload channel, repair discipline, formatter plumbing)

- Built from the prior-art steal list (#1/#5/#7) as solutions inside
  FreeTalon's design; no new human approval gates anywhere.
  (1) Grammar-constrained envelope: planner calls on the Ollama path
  now pass a JSON Schema via the `format` parameter, derived from
  PlanNode (plan_envelope_schema): nodes array required, per-node
  id+objective required, assigned_claw enum = the claw registry,
  inputs/criteria free-form. Shape is guaranteed at decode time; the
  validators remain the semantic layer. The schema is deliberately
  NOT echoed into the prompt (house echo hazard — the model copies
  prompt JSON). Intake already constrained its TaskIntent the same
  way and is unchanged. Constrained-decoding cost (probe on
  gemma-3-abliterated, short sequential generations): ~13.0 tok/s
  schema-constrained vs ~14.4 tok/s format:"json" (~10% per-token
  overhead); the schema permits an empty nodes array by design —
  shape, not substance; the empty-plan escalation owns that case.
  (2) Payload channel — option B, harness-owned materialization,
  chosen over A (a validator ban on heredoc file writes) because
  the harness's own grounded skeleton teaches the heredoc test
  write, so A would fight the harness's own steering: a bash node
  whose ONLY effect is one plain heredoc write of a .py file,
  whose payload does not parse but is deterministically repairable
  (the anchored-edit uniform re-indent rule, then indented-block
  repair of single-statement dedents — the exact CP2 defect shape),
  is rerouted at materialization to the scaffold form carrying the
  repaired payload and judged by the full scaffold validator stack.
  Unrepairable payloads keep the heredoc_parse rejection; valid
  payloads keep the bash form untouched; appends, tees, and chained
  commands are not converted.
  (3) Repair discipline: stuck detection — the same structured
  rejection signature (code, node) three times in a row raises
  StuckRepairError (an LLMResponseError carrying the records) and
  escalates to the loop's re-plan/judge path instead of burning the
  budget (OpenHands' same-error pattern); every repair prompt now
  restates the task ("THE TASK IS UNCHANGED: ...") on top of the
  grounding it already re-included; scope lock — a would-be-accepted
  repair outside the endorsed scope is rejected with the new code
  scope_widening (35 codes total). Endorsed scope = the rejected
  plan's files + paths named in any prompt so far in the call + the
  task's workspace subtree + test files (the canonical grounded
  shape always writes one; re-plan briefs carry no workspace —
  judge.build_replan_brief — so the re-plan sequences depend on the
  test-file rule); a dependency install widens a plan that installed
  none. Framework/stack widening stays with the older framework
  validator.
  (4) Formatter plumbing (steal #7, plumbing only): an optional
  second pass re-emits the planner's response under the envelope
  schema before parsing; endpoint/model via
  FREETALON_FORMATTER_OLLAMA_BASE_URL / FREETALON_FORMATTER_OLLAMA_MODEL
  (or FREETALON_FORMATTER_ENABLED for a local two-pass on the
  planner's own endpoint); default OFF, zero behavior change; a
  formatter backend failure degrades to the original text. Pointing
  it at seeker88's llama3 later is an env change, not a code change.
  No live cross-box calls were made.
- CP2-fixture invariant, demonstrated three ways: the CP2 trace's
  output 3 (byte-exact fixture, md5 a6bf39ec4d62859086e482a3eb82d09d)
  is now ACCEPTED — 5 nodes, step-2 converted with its payload
  repaired, both written modules parse; retry-32's output 3 (the
  same defect) flips its pin from "rejected with 1 error" to
  accepted; retry-33's output 3 accepts and is sound (the full
  12-definition api.py, retention-judged — the fragment output,
  retry33-4, still rejects by name: elision).
- Corpus replay vs CP1 (28 outputs x 2 roots; wall 17.18s): OLD
  (2869fe7) accepted 4/28, accepted-sound 0/28, mean errors 1.21;
  refactor (c334196) accepted 0/28, accepted-sound 0/28, mean 1.36;
  cycle 2 accepted 2/28 (retry32-3, retry33-3 — both scored SOUND),
  accepted-sound 2/28, mean errors 1.25. accepted-unsound remains 0
  (the CP1 invariant holds), and accepted-sound moves off zero for
  the first time, on the payload channel alone.
- Pins changed, each documented in place with its reason: the A/B
  heredoc pin and the retry-32 profile pin flip to acceptance (the
  defect class they encoded is closed); retry-18 attempts 2/3 and
  retry-19 attempt 2 drop 5 -> 4 errors across 14 tests in 5 classes
  (step-2's heredoc parse error no longer fires; every other defect
  unchanged, plans still rejected; two "names_all_five_defects"
  tests renamed "..._four_defects"); retry-30 output 4 drops
  2 -> 1 error (the placeholder scaffold remains and still rejects).
  The first full-suite run with the naive scope lock produced 20
  errors in the historical repair sequences — the lock rejected the
  grounded repairs themselves (they write the workspace test file
  the rejected plans never wrote); the endorsed-scope definition
  above is the fix, and the final run is clean.
- Suite: 1014/1014 green (997 + 17 new TestPlannerCycle2 tests).
  Ran 1014 in 420.1s, OK. demo-framework-guard.py: ALL CHECKS
  PASSED (46 checks). Build log:
  logs/planner-cycle2-2026-11-17.log.
- Next: the live measurement matrix (a separate step; not run
  here): live /health runs on the cycle-2 planner with temperature
  raised for distinct samples, and the formatter A/B with
  seeker88's llama3 as the formatter node.

## Update - 2026-11-18 (Live measurement matrix: cycle-2 planner, Arms A/B; Arm C not run)

- Live matrix on 925d12c under full discipline. Pre-flight: GPU
  57C idle, power limit exactly 300.00W, link x16, 0 Xid 79/154,
  Ollama tags list gemma-3-abliterated:latest, head 925d12c
  clean; baseline suite 1014/1014 green (Ran 1014 in 426.9s,
  OK). Fans manual 85% during each arm, auto after,
  GPUFanControlState=0 verified after each arm. Log:
  logs/health-matrix-2026-11-17.log.
- ARM A (production defaults; formatter OFF, temperature as
  shipped): driver exit 0, ~5 min wall. Output 1 {"nodes":[]} ->
  empty_plan@schema. Output 2 ACCEPTED (plan-7885a4ef9d99,
  5 nodes): the PAYLOAD CHANNEL fired live -- step-2's bash
  heredoc test write (the same unittest.main() dedent that
  killed CP2) was materialized and rerouted bash -> scaffold,
  and the plan parsed. The envelope held: both outputs were
  schema-shaped; the only schema-stage rejection was
  empty_plan. No repair sequence occurred, so no stuck-detection
  or scope_widening events. BUT the run never executed: the
  loop's experiment-memory judge escalated before node-1 --
  "plan repeats a measured-failed approach for this goal:
  'bash' / 'Verify /health live' (node node-5)". All nodes
  stayed draft, verdict FAIL (escalated), zero files changed
  (porcelain 0), live /health never reached. Substance caveat:
  the accepted step-1 delta assigns literal ... placeholders
  to service_status / ollama_reachable / gpu_temperature --
  retention-sound by the corpus definition, but the endpoint
  would not have answered correctly had it run; the plan's own
  step-3 test would have caught it.
- ARM B (formatter ON: seeker88 llama3:latest via
  FREETALON_FORMATTER_OLLAMA_BASE_URL=http://100.87.140.12:11434
  and FREETALON_FORMATTER_OLLAMA_MODEL=llama3:latest): driver
  exit 1, FAILED at planning. seeker88 pre-checks: /api/tags
  lists llama3:latest; site /health HTTP 200 before AND after
  the arm; nothing on seeker88 restarted or reconfigured. All
  3 judged outputs (the post-formatter text is what is parsed
  and printed) rejected at validate: output 1, 5 errors
  (criteria x4 + route_engagement -- a stand-in plan with
  invented criterion types); output 2, 7 errors (framework x2
  -- a FastAPI rewrite -- scaffold_overwrite x2, criteria,
  command, tmp_write); output 3, 4 errors (criteria x3 --
  'exists' / 'contains' -- + route_engagement). No
  schema-stage rejections (the envelope held under the
  formatter too); no stuck detection (signatures differed
  every attempt); no scope_widening (no repair parsed); no
  payload materialization (no heredoc plan survived to it).
  Zero files changed. Observability limit: the driver log
  prints only the post-formatter text, so Gemma's original vs
  llama3's re-emission cannot be separated from this trace --
  the judged plans were consistently stand-in-shaped with
  invented criterion types (the "clean shape, hollow content"
  failure mode), but which pass introduced it is not provable
  from the log.
- ARM C (temperature sample): NOT RUN, on two independent
  grounds. (1) The arm's gate was "only if A and B both failed
  at planning" -- Arm A did not fail at planning (its plan was
  accepted; the run died at the judge's memory escalation), so
  the gate is not met. (2) Planner temperature is not
  env-configurable: LLMSettings carries no temperature field,
  from_env reads none, and _call_ollama /
  _call_openai_compatible hardcode temperature 0
  (freetalon/orchestrator/intake.py). Per the matrix brief, no
  code was changed to force it.
- Scoreboard vs CP2 (3 runs on c334196): live planning
  acceptance 0/3 -> Arm A accepted its plan (the first live
  acceptance on the sound planner that was not an elision
  fragment), but accepted-sound end-to-end remains 0 and
  end-to-end success remains 0. The blocker has moved again:
  planning is no longer the wall -- the experiment-memory
  escalation is. The memory's approach signature ('bash' +
  the node's objective text) matches node-5's "Verify /health
  live" against a historical failed attempt and refuses the
  whole plan before any node executes; any future plan that
  verifies /health with a bash node of that name hits the
  same wall regardless of planner quality. Closing it is a
  loop/judge-side fix (the memory is doing its M2 job with too
  coarse a signature), not a planner fix.
- Determinism: Arm A's trace differs from CP2's byte-identical
  trace (2 outputs vs 4, different plan and content) -- the
  envelope constraint and the payload channel changed
  generation, as intended. Within-arm replication was not
  tested (one run per arm by design).
- GPU across the matrix: poll-sample peaks 73C (Arm A), 69C
  (Arm B), all under the 85C pause threshold, no thermal
  intervention; 0 Xid 79/154 across the matrix; fans left on
  auto (GPUFanControlState=0 verified after each arm).
