# Approved dependency baseline

FreeTalon uses an allowlist model for runtime dependencies and container bases.

## Python package baseline

Only the packages listed in `requirements.txt` are approved runtime dependencies.

Policy:

- Runtime dependencies must be pinned to exact versions (`==`).
- Every pinned dependency must include at least one SHA256 hash.
- New runtime dependencies must be explicitly reviewed and added to the baseline in the same change.

Current approved runtime packages — this list mirrors `requirements.txt`
exactly (names and pinned versions, in file order):

- `aiofiles==25.1.0`
- `aiohappyeyeballs==2.7.1`
- `aiohttp==3.14.1`
- `aiosignal==1.4.0`
- `annotated-types==0.7.0`
- `anyio==4.14.1`
- `attrs==26.1.0`
- `bidict==0.23.1`
- `certifi==2026.6.17`
- `charset-normalizer==3.4.7`
- `click==8.4.2`
- `docker==7.1.0`
- `docutils==0.19`
- `fastapi==0.109.2`
- `frozenlist==1.8.0`
- `h11==0.16.0`
- `httpcore==1.0.9`
- `httptools==0.8.0`
- `httpx==0.28.1`
- `idna==3.18`
- `ifaddr==0.2.0`
- `itsdangerous==2.2.0`
- `jinja2==3.1.6`
- `markdown-it-py==4.2.0`
- `markdown2==2.5.5`
- `markupsafe==3.0.3`
- `mdurl==0.1.2`
- `multidict==6.7.1`
- `nicegui==1.4.26`
- `orjson==3.11.9`
- `propcache==0.5.2`
- `pillow==10.2.0`
- `pscript==0.7.7`
- `pydantic==2.8.2`
- `pydantic-core==2.20.1`
- `pygments==2.20.0`
- `python-dotenv==1.2.2`
- `python-engineio==4.13.3`
- `python-multipart==0.0.32`
- `python-socketio[asyncio-client]==5.16.3`
- `pyyaml==6.0.2`
- `requests==2.34.2`
- `rich==13.9.4`
- `simple-websocket==1.1.0`
- `starlette==0.36.3`
- `typing-extensions==4.16.0`
- `urllib3==2.7.0`
- `uvicorn[standard]==0.49.0`
- `uvloop==0.22.1`
- `vbuild==0.8.2`
- `watchfiles==1.2.0`
- `websockets==16.0`
- `wsproto==1.3.2`
- `yarl==1.24.2`

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
