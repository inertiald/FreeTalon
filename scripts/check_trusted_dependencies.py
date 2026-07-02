#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
COMPOSE = ROOT / "docker-compose.yml"
INSTALLER = ROOT / "installer.py"
ORCHESTRATOR = ROOT / "orchestrator.py"
DOCKERFILES = [
    ROOT / "Dockerfile.trusted-base",
    ROOT / "Dockerfile.claw-browser",
]

LOCAL_IMAGE_PREFIXES = ("trusted-python-base:", "freetalon-claw-browser:")


def _error(msg: str) -> None:
    print(f"ERROR: {msg}")


def _check_requirements(errors: list[str]) -> None:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    logical: list[str] = []
    current: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current.append(line[:-1].strip())
            continue
        current.append(line)
        logical.append(" ".join(current))
        current = []
    if current:
        logical.append(" ".join(current))

    if not logical:
        errors.append("requirements.txt has no dependencies")
        return

    pat = re.compile(
        r"^[a-zA-Z0-9_.-]+==[a-zA-Z0-9_.+!-]+(?:\s+--hash=sha256:[a-f0-9]{64})+$"
    )
    for entry in logical:
        if any(op in entry for op in (">=", "<=", "~=", ">", "<")):
            errors.append(f"requirements entry is not strictly pinned: {entry}")
        if not pat.fullmatch(entry):
            errors.append(f"requirements entry missing exact format/hash: {entry}")


def _image_is_allowed(image: str) -> bool:
    if "@sha256:" in image:
        return True
    return any(image.startswith(prefix) and not image.endswith(":latest") for prefix in LOCAL_IMAGE_PREFIXES)


def _check_compose(errors: list[str]) -> None:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = data.get("services", {})
    for name, spec in services.items():
        image = spec.get("image")
        if not image:
            errors.append(f"service {name} has no image field")
            continue
        if ":latest" in image and "@sha256:" not in image:
            errors.append(f"service {name} uses floating latest tag: {image}")
        if not _image_is_allowed(image):
            errors.append(f"service {name} image is not digest/version pinned: {image}")


def _check_dockerfiles(errors: list[str]) -> None:
    for dockerfile in DOCKERFILES:
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                ref = line.removeprefix("FROM ").strip()
                if "@sha256:" not in ref:
                    errors.append(f"{dockerfile.name} has unpinned FROM reference: {ref}")


def _check_source_files(errors: list[str]) -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    orchestrator = ORCHESTRATOR.read_text(encoding="utf-8")

    banned = ('"ollama/ollama:latest"', '"ollama/ollama:rocm"', '"trusted-python-base"', '"freetalon-claw-browser"')
    for token in banned:
        if token in installer:
            errors.append(f"installer.py still contains unpinned token: {token}")

    if 'TRUSTED_IMAGE = "trusted-python-base"' in orchestrator:
        errors.append("orchestrator.py uses unversioned trusted-python-base image")
    if 'BROWSER_CLAW_IMAGE = "freetalon-claw-browser"' in orchestrator:
        errors.append("orchestrator.py uses unversioned freetalon-claw-browser image")


def main() -> int:
    errors: list[str] = []
    _check_requirements(errors)
    _check_compose(errors)
    _check_dockerfiles(errors)
    _check_source_files(errors)
    if errors:
        for err in errors:
            _error(err)
        return 1
    print("Trusted dependency policy checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
