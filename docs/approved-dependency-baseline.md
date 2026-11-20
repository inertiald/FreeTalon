# Approved dependency baseline

FreeTalon uses an allowlist model for runtime dependencies and container bases.

## Python package baseline

Only the packages listed in `requirements.txt` are approved runtime dependencies.

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
- `ollama/ollama:0.40.16@sha256:910d0d8107b538fb0894e63163eb195de0cc7aefcbc666dc62d3b18f71124592`
- `ollama/ollama:0.40.16-rocm@sha256:85b3c895222b1ddc9d63c40b20fc74f37a37593dcbbdfbf28910a925e43ead00`

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
