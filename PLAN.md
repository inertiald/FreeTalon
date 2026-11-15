# FreeTalon — Plan

Living plan. Updated at the end of every work session, alongside STATUS.md.
Last updated: 2026-11-14. Owner: Pat. Working node: seeker7 (RTX 3090, 24 GB).

## 1. Goal

FreeTalon is a general autonomous operator, not a script runner. You give it a
goal and it plans the work, gathers data, runs tests, makes research bets,
trains models, measures what happened, and iterates — stopping for a human
only at irreversible steps.

Two worked examples define what "done" looks like:

- **Financial automation.** "Automate a mean-reversion strategy": research
  the approach, gather market data, backtest, train a model, paper-trade,
  measure, iterate — and invest only after human approval, inside position
  limits.
- **ECU reverse-engineering.** Rack, power, meter and cable an ECU; capture
  CAN bus streams and live inputs/outputs with inductive clamps; research
  brick risks from forums; find a tune file and analyse it in a secure
  environment first; build tooling; iterate toward a product. Human approval
  only at dangerous steps (e.g. flashing firmware).

The fleet (30+ nodes, 48-port switch, isolated compute VLAN) is the muscle
for this loop, not a substitute for it. The loop has to work on one node
before the fleet is worth cabling. See section 6.

## 2. Where we are (verified 2026-11-14)

- Single node seeker7: Ubuntu 24.04, kernel 7.0.0-43, Nvidia 595.91.07,
  fully patched, zero packages upgradable. Tailscale + SSH access works;
  RDP restricted to Tailscale only; Ollama bound to localhost.
- Gemma 3 12B (gemma-3-abliterated:latest) runs in Docker via Ollama,
  43/49 layers on GPU, ~10 tok/s. FreeTalon intake via Ollama returns a
  validated TaskIntent.
- Test suite: 270/270 green (229 on 2026-11-14; +41 in the M0 loop
  build). Fixed: JSON marshaling (freetalon/jsonutil.py + patched
  hive.py / audit.py / api.py), flat distributed config, strict tool
  registry, planner normalisation, prompt contracts, executor audit events.
- M4 DONE 2026-11-14 (see section 4): approval gates (packets, API,
  dashboard). Live smoke verified: a gated flash pauses, an approval
  over HTTP unblocks it, the run completes. 346/346 tests green.
- M3 DONE 2026-11-14 (see section 4): research + data claws and
  sandbox analysis. Live ECU flow smoke-verified on seeker7 (run
  run-20261115T043909Z-504ac2, goal PASS). 320/320 tests green.
- M2 DONE 2026-11-14 (see section 4): experiment memory (SQLite,
  append-only, goal+domain scoped); a second run of the same goal does
  not repeat a measured-failed approach. 300/300 tests green.
- M1 DONE 2026-11-14 (see section 4): judge (continue/retry/re-plan/
  escalate), delta re-plans from structured briefs, budget accounting,
  progress rule, duplicate-approach rejection. 289/289 tests green.
- M0 DONE 2026-11-14 (see section 4): executor measures every node
  (freetalon/orchestrator/measure.py), run directories under
  freetalon-workspace/runs/<run-id>/, GoalContract with conservative
  budgets, input sanitizer fixed, vLLM path decided deferred (Ollama is
  the working path).
- What works on one node: Ollama intake, echo submit, hive-state
  persistence, dashboard pipeline (non-strict registry, demo behaviour).
- What does not work yet: the autonomous loop (section 3). The vLLM
  inference path still raises NotImplementedError; the working LLM path is
  Ollama via orchestrator/intake.py.

## 3. The loop — phases and current state

| Phase | What it means | State today |
|---|---|---|
| Intake | Turn a request into a validated TaskIntent, with budgets, risk limits and forbidden actions captured as constraints | Partial: goal parsing works but is loose ("check the GPU" became "summarise the directory"); constraints are not captured |
| Plan | Produce a DAG with acceptance criteria per node | One-shot only: a single DAG, never re-planned |
| Execute | Run nodes through the tool registry | Works, but dishonest: a node is COMPLETED if its handler returns, even when a bash script exits non-zero; exit_code is returned and never read; acceptance is stored and never checked |
| Measure | Record what actually happened (exit codes, outputs, metrics) | DONE (M0): measure.py evaluates criteria deterministically; verdicts PASS/FAIL/UNMEASURABLE; measurements.jsonl per run |
| Judge | Decide pass/fail against acceptance criteria | DONE (M1): judge.py — CONTINUE/RETRY/REPLAN/ESCALATE from measurements; budget accounting; progress rule (stall after 2 re-plans without a newly passing criterion); repeated UNMEASURABLE escalates |
| Re-plan | On failure, plan again from the measurements | DONE (M1): structured re-plan briefs; delta plans validated against repeating a failed approach; plan versions numbered and kept in the run directory |
| Remember | Keep hypothesis, measurement, next try across runs | DONE (M2): ExperimentStore (SQLite, same file as the plan store); lessons written at judge time from measurements; planner retrieves past attempts; repeats refused before running |
| Research | Gather data, research risks, fetch files — with provenance | DONE (M3): research claw (DuckDuckGo + Stack Exchange + Wikipedia, sourced/dated/trust-noted artifacts); failures recorded, never hidden |
| Gate | Pause for human approval before irreversible actions (invest, flash) | DONE (M4): gate packets bound to a hash, expiring (deny on timeout), denial never re-asked; dashboard + API approve/deny |

The full loop design (phases, data model, safety, worked examples) lives in
the design document in the home-lab goal docs, "FreeTalon Autonomous Loop
Design" (written 2026-11-14). This plan is the execution schedule for it.

## 4. Milestones

### M0 — Run and measure (DONE 2026-11-14)
Executor judges every node result: non-zero exit_code fails the node,
acceptance criteria are checked, measurements are recorded on the node.
Done when: a script that exits 1 reports FAILED with its output attached;
a script that passes its acceptance reports COMPLETED with measurements;
tests cover both. New module orchestrator/measure.py.
Evidence: 270/270 tests green (was 229); live smoke run
run-20261115T041349Z-bc5c66 (exit-1 node FAILED with verdict, file-writing
node COMPLETED with PASS, measurements.jsonl + summary.md on disk).
Small fixes done: echo sanitizer accepts ordinary text ("don't",
"hello!", quotes); vLLM decided DEFERRED — Ollama is the working path,
the module is kept as the documented integration point (see inference.py).

### M1 — Re-plan from measurements (DONE 2026-11-14)
Planner takes the failed plan + measurements and produces a revised DAG.
Iteration budget (max re-plans per run) and stop conditions defined.
Done when: a failing plan is automatically revised and re-run up to the
budget, then fails honestly with the full attempt history.
Evidence: 289/289 tests green. Seeded failing task re-plans once from its
measurement, does something demonstrably different, and succeeds (v2);
with re-plans exhausted it escalates with the full attempt history in
summary.md; a duplicate re-plan (same approach, no differs_by) is
rejected; a timed-out node retries once and succeeds; repeated
UNMEASURABLE escalates. LLM re-planning is wired (replan_from_brief via
Ollama); tests inject deterministic re-plan functions so the suite needs
no LLM.

### M2 — Experiment memory (DONE 2026-11-14)
Persistent record of experiments: hypothesis, attempts, measurements,
outcome. Stored in SQLite next to ExecutionPlanStateStore. The planner
reads past attempts before planning. Done when: a second run of the same
goal does not repeat a measured-failed approach.
Evidence: 300/300 tests green. A second run of the same goal with the
same approach escalates before executing anything (citing the recorded
failure); the same approach with a stated differs_by is allowed and can
succeed; a re-plan that repeats a recorded failure is rejected; a
different goal is not blocked. Lessons are generated deterministically
from measurements (no invented content). Memory is also mirrored to the
run's memory.jsonl and fed into re-plan briefs.

### M3 — Research and data tools (DONE 2026-11-14)
Research claw (web/forum search with source capture), data claw (fetch
and store datasets with provenance), sandbox for analysing untrusted
files. Done when: the ECU flow can research brick risks and analyse a
tune file in the sandbox without touching the live ECU.
Evidence: 320/320 tests green. Live smoke on seeker7
(run-20261115T043909Z-504ac2): research node (3 sources, 2 pages fetched),
dataset fetch (143,832 bytes with provenance sidecar), sandbox analysis
(file type + sha256 + strings) — goal PASS, no live ECU touched.
Engines: DuckDuckGo (best-effort — its HTML endpoint returns HTTP 202 to
this VM's urllib, recorded as a failure in the artifact, never hidden),
Stack Exchange API (forum, keyless, works), Wikipedia full-text search
(reference, works). The executor now injects run_dir into node inputs so
artifacts are shared across nodes in a run.

### M4 — Approval gates (DONE 2026-11-14)
AWAITING_APPROVAL plan state, approval requests (what, why, risk, cost,
reversible?), dashboard/API approve/deny, deny-on-timeout, full audit
trail. Done when: a plan that would invest or flash pauses and only
proceeds after an explicit approval.
Evidence: 346/346 tests green. Live smoke on seeker7
(run-20261115T044604Z-1a2ab1): the flash node paused at AWAITING_APPROVAL
with the handler never called; GET /gates listed the packet; POST approve
over HTTP unblocked it; resume completed the run with goal PASS. Deny and
expiry both fail the node honestly (deny on timeout), and a denial is
never re-asked — in the same run the node fails, and in a later run M2
memory refuses the approach before it reaches the gate. Approvals are
bound to a hash of (action, node, inputs) only: evidence is excluded
because it grows during the run and would otherwise void approvals.

### M5 — Verticals (finance + ECU) (next)
Finance: backtest claw, train claw, paper-trade mode (dry-run default),
position caps, kill switch. ECU: CAN capture claw, tune-surface analysis,
flash gate. Done when: each worked example runs end-to-end on one node
with a human gate before the irreversible step.

### M6 — Fleet (deferred)
Only after M0-M2 are green on one node: switch configuration, isolated
compute VLAN, node enrolment, ADR 0002 distributed work (still Proposed).
Blocked on: switch model (unknown), VLAN design, and the loop itself.
See section 6.

## 5. Version control rules

- main is the stable line. Work happens on dev/* branches; merge to
  main only with CI green.
- CI: .github/workflows/tests.yml runs the unit suite (229 tests) on
  every push to main / dev/* and every PR. A red build blocks the merge.
  The existing trusted-dependency-policy.yml runs only on pushes to
  main and on PRs — not on dev/* pushes, so it never runs on a dev
  branch until a PR targets main. (Corrected 2026-11-14: an earlier
  version of this plan wrongly said it ran on dev/* too. Verified
  2026-11-14: zero workflow runs exist on dev/seeker7-single-node.)
- Commit in logical chunks with messages that say what and why. Never
  commit local-only artefacts: *.bak, *.db, .env, Modelfile,
  models/, generated/, .venv, logs. (See .gitignore.)
- This branch (dev/seeker7-single-node) folds in local dev that had
  diverged from mainline 2085f6cd for some time, including the 2026-11-14
  JSON and test-suite fixes. The diff against mainline is the honest
  record; review it before merging, and split it further if it is too big
  to review in one pass.
- Remote branches to triage (exist on origin, unmerged):
  - copilot/freetalonorchestratorclawshypervisorpy — hypervisor claw
    (430 lines) + tests. Decide: merge, rework, or drop.
  - copilot/task-4-1-dag-visualization — DAG depth visualisation
    (dag.py) + tests. Decide: merge, rework, or drop.

## 6. Fleet track (separate, deferred)

- Switch: model and managed/unmanaged status still unknown — needed before
  any switch configuration.
- Compute VLAN: isolated, no internet for nodes, no DHCP except a trusted
  provisioning source enabled only during imaging; network boot disabled
  on nodes by default (unpatched PXE RCE in node BIOS); only seeker7
  talks out and to the nodes.
- Provisioning: Tailscale-first enrolment preferred; PXE only for
  re-imaging. No standing PXE network.
- Hardware still unverified: whether a separate RTX 4090 machine exists
  (seeker7 is the RTX 3090); 3060 server, NUCs, EliteDesks.

## 7. Standup

STATUS.md is updated at the end of every work session in this format:
Done / Doing / Blocked / Next, plus health (tests, OS, services). That file
is the standup update — no separate meeting notes.

## 8. Open questions for Pat

- Finance: which broker/data source, and what position/risk limits?
- Approvals: approve in the dashboard, or in chat with Muse?
- The two Copilot branches (section 5): merge, rework, or drop?
- Switch model, when known.

## 9. Design document alignment (2026-11-14)

The full loop design is "FreeTalon Autonomous Loop Design" (12-page PDF,
home-lab goal docs, 2026-11-14). This plan is its execution schedule.
Alignment notes, read from the finished document:

- Milestones M0-M4 map to the design's five phases: 1 Run and measure,
  2 Judge and re-plan, 3 Experiment memory, 4 Research tools,
  5 Approval gates. M5 (verticals) and M6 (fleet) sit on top of those.
- Phase 1 also includes the small fixes: input sanitizer (fixed in M0 —
  echo now accepts ordinary text) and a decision on the vLLM path's fate
  (decided in M0: DEFERRED, Ollama is the working path).
- Measurement contract: verdicts are PASS / FAIL / UNMEASURABLE, from
  deterministic evaluators (exit_code, file_exists, file_matches, metric,
  tests_pass, custom_check). A node is SUCCEEDED only when execution
  completed AND measurement passed - the false-success bug becomes
  structurally impossible. The judge may not lower a threshold or edit a
  criterion to keep a run alive; changing the contract is a human act.
- Runs live under freetalon-workspace/runs/<run-id>/ with contract.json,
  numbered plan.json versions, per-node stdout/stderr/artifacts,
  measurements.jsonl, memory.jsonl, gates/, summary.md.
- Gate packets carry the exact action, evidence, risks and undoability;
  approvals are per-action, expiring, and bound to the packet hash - a
  modified packet invalidates the approval. Denial is recorded data, and
  the loop never re-asks the same gate in the same run.
- Fleet boundary (design doc): no compute connection until the loop
  passes its done tests on one node; fleet work returns after Phase 2.
  This plan's M6 says the same thing (after M0-M2).

Decisions for Pat before Phase 1 (from the design doc, defaults assumed
until Pat says otherwise):

- Default budgets: conservative caps for time, attempts, re-plans per run.
  Set in code (RunBudget, M0): 3600s wall clock, 3 attempts per hypothesis,
  3 re-plans, 50 nodes, $0 real money (paper/dry-run only).
- Gate delivery: gate packets in the FreeTalon interface (assumed).
- Data sources: which market/data providers and research sources are
  in bounds for Phase 4.
- Risk limits: default loss/drawdown language for finance contracts.
- Memory retention: keep everything indefinitely on seeker7 (assumed).
