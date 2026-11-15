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
