# Secret hygiene

FreeTalon's repo is public. Anything committed is published forever;
treat a committed secret as burned.

## The three layers

- (a) PUBLIC PARAMETERS — versioned in the repo: defaults in code and
  every variable name in `.env.example`. If publishing a value would
  be harmless, it is a parameter and it belongs here.
- (b) RUNTIME SECRETS — only in env files in the freetalon-workspace
  on the boxes (mode 0600), or in GitHub Actions secrets for CI.
  Never in the repo, in issues or PRs, or in logs.
- (c) HUMAN CREDENTIALS — password manager / vault only. Never in any
  file the repo or the workspace can see.

The rule: a value that would need rotation if it leaked is a secret.
Secrets live in layer (b) or (c), never in layer (a).

Secrets by that rule today: `FREETALON_API_TOKEN`,
`CONTENT_PIPELINE_TOKEN`, `FREETALON_OPENAI_API_KEY` (optional
backend), `FREETALON_DASHBOARD_STORAGE_SECRET`. Everything else in
`.env.example` is a parameter.

One rotation has already been done under this rule: the dashboard
storage secret, hardcoded in dashboard.py until 2026-11-19, now
lives only in `<workspace>/dashboard.env` (0600). dashboard.py
resolves `FREETALON_DASHBOARD_STORAGE_SECRET` from the process
environment, then that file, then an ephemeral per-process fallback
with a startup warning. The pre-rotation value remains in git
history and is useless.

## Enforcement

- CI: `.github/workflows/secrets-scan.yml` runs gitleaks on every
  push and pull request. It is read-only and runs no repo code, so —
  unlike the GPU workflow — it is safe on fork PRs.
- Local: a pre-commit hook runs the same scan on staged changes.
  Install it once per clone:

      git config core.hooksPath scripts/git-hooks

  The hook uses gitleaks when available and falls back to a pattern
  scan of the staged diff when it is not.
- `.gitignore` blocks env files, key material, and snapshot archives
  by default. `.env.example` is the one env-shaped file that is
  versioned; its values are placeholders.

## If a secret lands in history anyway

Stop. Do not rewrite history and do not rotate on your own — report
what was found (type and location, never the value) and let Pat
decide. The usual response is a rotation: the old value stays in
history but becomes useless.
