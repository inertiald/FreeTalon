# FreeTalon — Plan

Living plan. Updated at the end of every work session, alongside STATUS.md.
Last updated: 2026-11-20. Owner: Pat. Working node: seeker7 (RTX 3090, 24 GB).

## 1. Goal

FreeTalon is a general autonomous operator, not a script runner. You give it a
goal and it plans the work, gathers data, runs tests, makes research bets,
trains models, measures what happened, and iterates — stopping for a human
only at irreversible steps.

North Star (2026-11-17): the finance example, taken all the way. FreeTalon collects its own market data, applies real ML methods to it, develops a model, and trades the real market for profit - paper first, real money only at the human gate. Every other vertical is judged against the same bar: scary capable on its own, stopped only at cliff edges (publish, pay, flash, send).

Expansion (2026-11-18): the daily-bar stack is a foundation, not the
destination — intraday trading is an eventual goal. The longer arc is
event-driven: incorporate mass disparate OSINT and find non-spurious
correlations among the spurious ones. Event alpha decays in minutes to
hours, so sector selection follows data-feed quality (faster,
structured, machine-readable feeds first). The filter is discipline:
pre-registered hypotheses, out-of-sample validation, false-discovery
control, and a plausible mechanism required before a correlation is
tradeable.

Model development — open work item: every validated strategy so far is
a hand-written rule (two equity rules, one crypto rule). No learned ML
model exists yet. Developing one on the self-collected data — trained,
validated out-of-sample under the same discipline, then paper-tracked —
is a tracked step of the North Star, not yet started.

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

The fleet (~14 nodes, 48-port switch, isolated compute VLAN) is the muscle
for this loop, not a substitute for it. The loop has to work on one node
before the fleet is worth cabling. See section 6.

## 2. Where we are (verified 2026-11-20)

- The autonomous loop works end-to-end on one node. The /health proving
  goal succeeded 2026-11-18 (planner qwen3.8:27b, critic-gated): plan,
  execute, measure, judge — and the extended check held (live endpoint
  vs reality, code computes its values, new tests pass, suite green).
  M0–M5 detail is in section 4.
- Planner: the model emits only the work contract — system-owned fields
  (differs_by / responds_to / status) are stripped before it can write
  them, prose is capped, repetition loops are detected. Contract
  pinning (2026-11-20): a plan declares a module contract for each new
  module others import from; a deterministic AST validator checks it,
  the validated contract is pinned into plan metadata, and re-plans
  may not change it.
- Critic: merged and production-enabled (2026-11-18). The judge is
  qwen2.5-coder:7b on seeker88; a verdict must quote verbatim evidence
  or it is void, and a deterministic rule voids code that confesses
  its own hollowness.
- Pairing endpoint shipped (freetalon/pairing): contract-first store +
  server, 35 reference tests; the shipped Node Console's poll client
  matches the server exactly. Exercised live in the UEFI netboot
  proof (register/poll against the provisioning-segment address).
- Content pipeline complete through video V1: photo/video + blurb
  becomes blog, Instagram and YouTube drafts plus a rendered MP4, with
  the PLATE photo standard applied to stills. Drafts only — nothing
  auto-posts.
- North Star legs: self-collected market data (Nasdaq public API, 12
  symbols; daily closes authoritative) and OSINT V1 collectors run on
  seeker7. Equities: two strategies validated out-of-sample and
  paper-tracked. Crypto: the BTC 200DMA filter validated and
  paper-tracked. Robinhood is connected — the agent reads all accounts
  and trades only in the agentic account. The $43 demo trade closed
  2026-11-20 at a $2.82 loss; under the ladder, larger sums stay
  locked until a model validates and live profit is demonstrated.
- Test suite: 1419/1419 green. Hosted CI (tests + secrets scan) is
  green on every push; the GPU workflow runs on seeker7's self-hosted
  runner.
- seeker7 is currently down: the RTX 3090 fell off the bus on
  2026-11-20 and the directed reboot hung; the box waits on Pat's
  power cycle. Its data legs (collectors, paper trackers, dashboard
  pusher, content generator) pause with it — all backfillable.
  Development continues on seeker88.

## 3. The loop — phases and current state

| Phase | What it means | State today |
|---|---|---|
| Intake | Turn a request into a validated TaskIntent, with budgets, risk limits and forbidden actions captured as constraints | Works: validated TaskIntent via Ollama intake; budgets, risk limits and forbidden actions captured as constraints |
| Plan | Produce a DAG with acceptance criteria per node | Works: DAG with per-node acceptance criteria; delta re-plans from measurements (M1); module contract declared, validated and pinned (2026-11-20) |
| Execute | Run nodes through the tool registry | DONE (M0): exit codes are read and judged; a node is COMPLETED only when execution completed AND its acceptance measurement passed |
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

### M5 — Verticals (finance + ECU) (DONE 2026-11-14)
Finance: backtest claw, train claw, paper-trade mode (dry-run default),
position caps, kill switch. ECU: CAN capture claw, tune-surface analysis,
flash gate. Done when: each worked example runs end-to-end on one node
with a human gate before the irreversible step.
Evidence: 368/368 tests green. Live smoke on seeker7: finance
(run-20261115T045041Z-f529e6) — backtest, train, paper-trade (2 trades,
max position 10.0% at the cap) then paused at the invest gate; ECU
(run-20261115T045041Z-666823) — real research, verified backup, simulated
CAN capture (200 frames), sandbox analysis, tune surface (5 map terms)
then paused at the flash gate. Approving either gated node fails it
honestly: the invest/flash handlers are stubs that refuse, so no real
money can move and no ECU can be flashed even after approval. Live CAN
capture (source="live") is refused in code. Known behaviour: approving a
gated stub records a failed experiment, so re-running the same goal needs
a stated differs_by (the smoke script does this).

### M6 — Fleet (prep proven 2026-11-20; live session pending)
Provisioning prep is done and proven end-to-end in QEMU on both boot
paths: BIOS/PXE, and UEFI with Secure Boot on (shim -> Canonical-signed
grubnet -> autoinstall -> Node Console on tty1, pairing poll exercised
against the shipped server). The pairing server endpoint is shipped
(freetalon/pairing) and the Node Console is built. Remaining blockers
are session inputs only: real EdgeSwitch port numbers, and per-unit
inventory (RAM/SSD read at first boot).
Fleet: ~14 CPU-only nodes — 4x Intel NUC 11 Extreme, 10x Intel NUC 9
Extreme — most carrying a dual-port 100G QSFP NIC in the PCIe slot.
Fabric: node-to-node 1m DAC in a ring, no 100G switch (the EdgeSwitch
cannot terminate 100G); provisioning and management ride the onboard
1G LAN. The provisioning segment is an isolated unrouted VLAN 40
(10.40.0.0/24): ports normally shut, opened as a window, no secrets in
the boot chain — pairing credentials move over the encrypted channel
after the console appears. Desktop is a flag: compute|desktop
(default compute; desktop = Budgie + GUI console). See section 6.

## 5. Version control rules

- main is the stable line. Development happens on dev/* and pushes to
  dev/seeker7-single-node only; no PR or merge to main unless Pat asks
  (Pat, 2026-11-19).
- CI: .github/workflows/tests.yml runs the unit suite (1419 tests) on
  every push to main / dev/* and every PR; secrets-scan.yml (gitleaks)
  runs on push and PR. A red build blocks promotion to main. The
  existing trusted-dependency-policy.yml runs only on pushes to
  main and on PRs — not on dev/* pushes, so it never runs on a dev
  branch until a PR targets main. (Corrected 2026-11-14: an earlier
  version of this plan wrongly said it ran on dev/* too.)
- Commit in logical chunks with messages that say what and why. Never
  commit local-only artefacts: *.bak, *.db, .env, Modelfile,
  models/, generated/, .venv, logs. (See .gitignore.)

## 6. Fleet track

- Switch: Ubiquiti EdgeSwitch 48 Lite (ES-48-LITE) — managed L2/L3,
  48x 1G + 2x SFP + 2x SFP+ (resolved 2026-11-16). It cannot terminate
  the nodes' 100G fabric; see section 4 (M6).
- seeker7 (primary node): ASUS TUF GAMING X570-PLUS (WI-FI), Ryzen 9
  5900X, 64 GB, EVGA RTX 3090 24 GB (persistent 300 W limit), Ubuntu
  Budgie 24.04, BIOS 5021, legacy/CSM boot. UEFI conversion is a
  planned later job (workspace/data backup first).
- seeker88 (auxiliary node): Ryzen 5 2600, 32 GB, RTX 3060 Ti 8 GB,
  Ubuntu 24.04. Runs inertiald.com and serves as the second model
  node (critic judge qwen2.5-coder:7b; the site's own llama3).
- Fleet nodes: ~14 Intel NUCs (4x NUC 11 Extreme, 10x NUC 9 Extreme),
  CPU-only, most with dual-port 100G NICs; RAM/SSD per unit is read at
  first-boot inventory. There is no RTX 4090 machine (resolved
  2026-11-15).
- Provisioning: Ubuntu Server 24.04, netboot-installed fresh (iPXE +
  autoinstall + cloud-init) on the isolated VLAN 40 segment; no
  standing PXE network and no secrets in the boot chain. The Node
  Console (self-launching TUI on tty1) is the join point; desktop is
  a compute|desktop flag.

## 7. Standup

STATUS.md is updated at the end of every work session in this format:
Done / Doing / Blocked / Next, plus health (tests, OS, services). That file
is the standup update — no separate meeting notes.

## 8. Questions for Pat (all answered)

- Finance: which broker/data source, and what position/risk limits?
  ANSWERED: Robinhood (connected 2026-11-18; the agent trades only in
  the agentic account — the primary account is read-only to it). Data:
  self-collected (Nasdaq public API for equities, Coinbase for
  crypto). Limits: the ladder — a model validates first; the $43 seed
  trades ungated at that scale; larger sums unlock only on
  demonstrated live profit, with trade approvals arriving at that
  scale (2026-11-18).
- Approvals: approve in the dashboard, or in chat with Muse?
  ANSWERED: FreeTalon gate packets live in the FreeTalon interface
  (dashboard/API approve/deny, M4). Trading approvals are off at the
  seed scale by Pat's decision (2026-11-18) and arrive with larger
  sums.
- The two Copilot branches (section 5): merge, rework, or drop?
  ANSWERED 2026-11-19: triaged and removed in the branch cleanup;
  origin now holds only main, dev/seeker7-single-node and
  dev/critic-experiment.
- Switch model, when known. ANSWERED 2026-11-16: Ubiquiti EdgeSwitch
  48 Lite (ES-48-LITE).

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
