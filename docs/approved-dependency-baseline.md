# Approved dependency baseline

FreeTalon uses an allowlist model for runtime dependencies and container bases.

## Python package baseline

Only the packages listed in `/home/runner/work/FreeTalon/FreeTalon/requirements.txt` are approved runtime dependencies.

Policy:

- Runtime dependencies must be pinned to exact versions (`==`).
- Every pinned dependency must include at least one SHA256 hash.
- New runtime dependencies must be explicitly reviewed and added to the baseline in the same change.

Current approved runtime packages:

- `rich==13.9.4`
- `pyyaml==6.0.2`
- `nicegui==1.4.26`
- `docker==7.1.0`
- `pydantic==2.8.2`

## Container image baseline

Approved external image references:

- `python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf`
- `mcr.microsoft.com/playwright/python:v1.40.0-jammy@sha256:5656288def8576b31903becabac255b19bc01ee961ddb499de3418d52f55ed2a`
- `ollama/ollama:latest@sha256:f1a705f2bd113fb8d15f85f7c217f0dc5f6bebda6b0cc42b82c3ad165ffcb9dc`
- `ollama/ollama:rocm@sha256:c2d5755f1cc3777d2616014516dfe08fa9da214add9fe76f399ffd6a45661f1a`

Approved local images built from this repository:

- `trusted-python-base:1.0.0`
- `freetalon-claw-browser:1.0.0`

Policy:

- External images must be pinned with immutable digests (`@sha256:`).
- Local images must use explicit version tags.
- Floating references (for example `:latest` without digest) are disallowed.

## Allowed dependency sources

Allowed package and image sources:

- PyPI (Python runtime packages).
- Docker Hub (`docker.io`) for Docker library and `ollama/ollama` images.
- Microsoft Container Registry (`mcr.microsoft.com`) for Playwright base image.

Any new source registry or package index requires explicit review and policy update in this document.
