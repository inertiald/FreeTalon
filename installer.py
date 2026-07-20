#!/usr/bin/env python3
"""FreeTalon installer and local UI bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

GPU_NVIDIA = "nvidia"
GPU_AMD = "amd"
GPU_NONE = "none"
NODE_ROLE_ORCHESTRATOR = "orchestrator"
NODE_ROLE_WORKER = "worker"
DEFAULT_NODE_ROLE = NODE_ROLE_ORCHESTRATOR
PLAYWRIGHT_VERSION = "1.40.0"
UI_HOST = "127.0.0.1"
UI_PORT = 7860
API_HOST = "127.0.0.1"
API_PORT = 8765
REPO_ROOT = Path(__file__).resolve().parent
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"
DEFAULT_VENV_PATH = REPO_ROOT / ".venv"
OLLAMA_IMAGE_DEFAULT = (
    "ollama/ollama:latest@sha256:f1a705f2bd113fb8d15f85f7c217f0dc5f6bebda6b0cc42b82c3ad165ffcb9dc"
)
OLLAMA_IMAGE_AMD = (
    "ollama/ollama:rocm@sha256:c2d5755f1cc3777d2616014516dfe08fa9da214add9fe76f399ffd6a45661f1a"
)


@dataclass(slots=True)
class DockerStatus:
    cli_available: bool
    daemon_reachable: bool
    compose_available: bool
    runtimes: tuple[str, ...] = ()
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def _status(prefix: str, message: str) -> None:
    print(f"{prefix} {message}")


def _ok(message: str) -> None:
    _status("[ok]", message)


def _warn(message: str) -> None:
    _status("[warn]", message)


def _fail(message: str) -> None:
    _status("[fail]", message)


def _run(
    command: list[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd or REPO_ROOT),
        check=check,
        text=True,
        capture_output=capture_output,
    )


def check_linux() -> bool:
    return platform.system() == "Linux"


def check_python_version() -> bool:
    return sys.version_info >= (3, 10)


def detect_gpu() -> str:
    if shutil.which("nvidia-smi") is not None:
        return GPU_NVIDIA
    if os.path.exists("/dev/kfd"):
        return GPU_AMD
    return GPU_NONE


def detect_docker_status() -> DockerStatus:
    if shutil.which("docker") is None:
        return DockerStatus(False, False, False)

    status = DockerStatus(True, False, False)
    try:
        _run(["docker", "compose", "version"], capture_output=True)
        status.compose_available = True
    except subprocess.CalledProcessError:
        status.warnings.append(
            "Docker Compose plugin not detected; install it before running 'docker compose up -d'."
        )

    try:
        info = _run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
        )
        status.daemon_reachable = True
        runtimes = json.loads(info.stdout.strip() or "{}")
        if isinstance(runtimes, dict):
            status.runtimes = tuple(sorted(str(name) for name in runtimes))
    except subprocess.CalledProcessError as exc:
        status.error = (exc.stderr or exc.stdout or str(exc)).strip()
        return status
    except json.JSONDecodeError as exc:
        status.error = f"Unable to parse docker runtime information: {exc}"
        return status

    return status


def select_compose_gpu(detected_gpu: str, docker_status: DockerStatus) -> tuple[str, list[str]]:
    warnings: list[str] = []

    if not docker_status.cli_available:
        if detected_gpu != GPU_NONE:
            warnings.append("Docker is not installed; generating CPU-only compose defaults.")
        return GPU_NONE, warnings

    if not docker_status.daemon_reachable:
        if detected_gpu != GPU_NONE:
            warnings.append("Docker daemon is not reachable; generating CPU-only compose defaults.")
        return GPU_NONE, warnings

    if detected_gpu == GPU_NVIDIA and "nvidia" not in docker_status.runtimes:
        warnings.append(
            "NVIDIA GPU detected but Docker runtime 'nvidia' is unavailable; falling back to CPU mode."
        )
        return GPU_NONE, warnings

    if detected_gpu == GPU_AMD:
        missing = [path for path in ("/dev/kfd", "/dev/dri") if not os.path.exists(path)]
        if missing:
            warnings.append(
                "AMD GPU detected but required device nodes are missing; falling back to CPU mode."
            )
            return GPU_NONE, warnings

    return detected_gpu, warnings


def venv_python_path(venv_path: Path) -> Path:
    bindir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv_path / bindir / executable


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        _ok(f"{path.name} already up to date")
        return
    path.write_text(content, encoding="utf-8")
    _ok(f"Wrote {path}")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _yaml_dump(data: Any, indent: int = 0) -> str:
    """Serialize a small subset of YAML when PyYAML is unavailable.

    The installer prefers PyYAML when it is importable. This fallback keeps
    ``python3 installer.py`` usable on a fresh machine before the project
    dependencies have been installed into ``.venv``.
    """
    pad = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(_yaml_dump(value, indent + 2))
            elif isinstance(value, dict):
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(value, list):
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
        return "\n".join(lines)

    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict) and item:
                nested = _yaml_dump(item, indent + 2).splitlines()
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            elif isinstance(item, list) and item:
                nested = _yaml_dump(item, indent + 2).splitlines()
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)

    return f"{pad}{_yaml_scalar(data)}"


_CLAW_SERVICES: dict[str, Any] = {
    "media-claw": {
        "image": "trusted-python-base:1.0.0",
        "networks": ["freetalon-claw-net"],
        "read_only": False,
        "volumes": ["${LOCAL_WORKSPACE}/output:/workspace/output:rw"],
        "deploy": {"resources": {"limits": {"memory": "8g", "cpus": "4.0"}}},
        "profiles": ["media"],
    },
    "upload-claw": {
        "image": "trusted-python-base:1.0.0",
        "networks": ["freetalon-upload-net"],
        "read_only": False,
        "volumes": ["${LOCAL_WORKSPACE}/output:/workspace/output:rw"],
        "deploy": {"resources": {"limits": {"memory": "8g", "cpus": "4.0"}}},
        "profiles": ["upload"],
    },
    "browser-claw": {
        "image": "freetalon-claw-browser:1.0.0",
        "networks": ["freetalon-browser-net"],
        "volumes": ["${LOCAL_WORKSPACE}/screenshots:/screenshots:rw"],
        "environment": {"SCREENSHOTS_DIR": "/screenshots", "CLAW_PORT": "8080"},
        "deploy": {"resources": {"limits": {"memory": "512m", "cpus": "0.5"}}},
        "profiles": ["browser"],
    },
}


def _ollama_service(image: str) -> dict[str, Any]:
    return {
        "image": image,
        "ports": ["11434:11434"],
        "volumes": ["ollama-models:/root/.ollama", "${LOCAL_WORKSPACE}:/workspace"],
        "networks": ["back-tier", "freetalon-claw-net"],
    }


def _compose_template(gpu: str) -> dict[str, Any]:
    ollama = _ollama_service(OLLAMA_IMAGE_AMD if gpu == GPU_AMD else OLLAMA_IMAGE_DEFAULT)
    if gpu == GPU_NVIDIA:
        # Prefer the legacy NVIDIA runtime here because it avoids the CDI/runtime
        # mismatch seen on some hosts and gives a deterministic compose file for
        # end-user installs.
        ollama["runtime"] = "nvidia"
        ollama["environment"] = {
            "NVIDIA_VISIBLE_DEVICES": "all",
            "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        }
    elif gpu == GPU_AMD:
        ollama["devices"] = ["/dev/kfd", "/dev/dri"]
        ollama["group_add"] = ["video"]

    compose: dict[str, Any] = {
        "name": "freetalon",
        "services": {"ollama": ollama},
        "networks": {
            "back-tier": {"driver": "bridge", "labels": {"freetalon.tier": "back"}},
            "freetalon-claw-net": {"driver": "bridge", "internal": True},
            "freetalon-browser-net": {"driver": "bridge"},
            "freetalon-upload-net": {"driver": "bridge"},
        },
        "volumes": {"ollama-models": {"name": "freetalon-ollama-models"}},
    }
    compose["services"].update(_CLAW_SERVICES)
    return compose


def generate_compose(gpu: str, path: Path) -> None:
    template = _compose_template(gpu)
    if yaml is not None:
        content = yaml.safe_dump(template, sort_keys=False)
    else:
        content = _yaml_dump(template).rstrip() + "\n"
    _write_if_changed(path, content)


def generate_env(
    workspace: str,
    install_mode: str,
    docker_profile: str,
    browser_enabled: bool,
    path: Path,
    node_role: str = DEFAULT_NODE_ROLE,
) -> None:
    content = "\n".join(
        [
            "# Generated by installer.py",
            f"LOCAL_WORKSPACE={workspace}",
            f"FREETALON_INSTALL_MODE={install_mode}",
            f"FREETALON_NODE_ROLE={node_role}",
            f"FREETALON_UI_HOST={UI_HOST}",
            f"FREETALON_UI_PORT={UI_PORT}",
            f"FREETALON_API_HOST={API_HOST}",
            f"FREETALON_API_PORT={API_PORT}",
            f"FREETALON_DOCKER_PROFILE={docker_profile}",
            f"FREETALON_BROWSER_ENABLED={1 if browser_enabled else 0}",
            "",
        ]
    )
    _write_if_changed(path, content)


def ensure_workspace_dirs(workspace: Path) -> None:
    for subdir in ("output", "screenshots"):
        (workspace / subdir).mkdir(parents=True, exist_ok=True)
    _ok(f"Workspace ready at {workspace}")


def ensure_virtualenv(venv_path: Path) -> Path:
    python_path = venv_python_path(venv_path)
    if python_path.exists():
        _ok(f"Reusing virtual environment at {venv_path}")
        return python_path

    _status("[..]", f"Creating virtual environment at {venv_path}")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(venv_path)
    python_path = venv_python_path(venv_path)
    _ok(f"Created virtual environment at {venv_path}")
    return python_path


def install_requirements(python_executable: Path) -> None:
    _status("[..]", "Installing Python dependencies into the project virtual environment")
    _run(
        [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "-r",
            str(REQUIREMENTS_PATH),
        ]
    )
    _ok("Installed Python dependencies")


def python_has_module(python_executable: Path, module_name: str) -> bool:
    result = _run(
        [
            str(python_executable),
            "-c",
            "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)",
            module_name,
        ],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def ensure_playwright(python_executable: Path) -> None:
    if not python_has_module(python_executable, "playwright"):
        _status("[..]", f"Installing Playwright {PLAYWRIGHT_VERSION} for local browser automation")
        _run(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"playwright=={PLAYWRIGHT_VERSION}",
            ]
        )
        _ok("Installed Playwright Python package")
    else:
        _ok("Playwright Python package already installed")

    _status("[..]", "Installing Chromium browser binaries for Playwright")
    _run([str(python_executable), "-m", "playwright", "install", "chromium"])
    _ok("Playwright browser binaries ready")


def build_local_images(browser_enabled: bool) -> None:
    _status("[..]", "Building trusted local Docker image")
    _run(
        [
            "docker",
            "build",
            "-t",
            "trusted-python-base:1.0.0",
            "-f",
            str(REPO_ROOT / "Dockerfile.trusted-base"),
            str(REPO_ROOT),
        ]
    )
    _ok("Built trusted-python-base:1.0.0")

    if browser_enabled:
        _status("[..]", "Building browser claw Docker image")
        _run(
            [
                "docker",
                "build",
                "-t",
                "freetalon-claw-browser:1.0.0",
                "-f",
                str(REPO_ROOT / "Dockerfile.claw-browser"),
                str(REPO_ROOT),
            ]
        )
        _ok("Built freetalon-claw-browser:1.0.0")


def validate_compose(path: Path, docker_status: DockerStatus) -> None:
    if not docker_status.compose_available:
        return
    _run(["docker", "compose", "-f", str(path), "config"], capture_output=True)
    _ok("Validated docker-compose.yml with docker compose config")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap FreeTalon for local UI, API, and optional Docker/browser features."
    )
    parser.add_argument("--yes", action="store_true", help="Use defaults without prompting.")
    parser.add_argument(
        "--mode",
        choices=("ui", "api", "docker", "full"),
        default="full",
        help="Choose the setup path to prepare. Default: full.",
    )
    parser.add_argument(
        "--node-role",
        choices=(NODE_ROLE_ORCHESTRATOR, NODE_ROLE_WORKER),
        default=None,
        help=(
            "Role of this node in a multi-node mesh: "
            f"'{NODE_ROLE_ORCHESTRATOR}' (Primary Orchestrator) or "
            f"'{NODE_ROLE_WORKER}' (Worker Node). "
            f"Default: {NODE_ROLE_ORCHESTRATOR} (prompted when run without --yes)."
        ),
    )
    parser.add_argument(
        "--workspace",
        default=os.path.expanduser("~/freetalon-workspace"),
        help="Workspace directory for outputs, screenshots, and runtime state.",
    )
    parser.add_argument(
        "--venv",
        default=str(DEFAULT_VENV_PATH),
        help="Project virtual environment path. Default: %(default)s",
    )
    parser.add_argument(
        "--enable-browser",
        action="store_true",
        help="Install Playwright and Chromium for local browser automation.",
    )
    parser.add_argument(
        "--start-ui",
        action="store_true",
        help="Launch the dashboard when setup completes.",
    )
    parser.add_argument(
        "--build-images",
        action="store_true",
        help="Build local Docker images for trusted-python-base and optional browser claw.",
    )
    parser.add_argument("--skip-python-deps", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-docker-validation", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-playwright-install", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _prompt(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def resolve_options(args: argparse.Namespace) -> tuple[Path, Path, bool, str]:
    """Return ``(workspace_path, venv_path, browser_enabled, node_role)`` for this run."""
    workspace = Path(args.workspace).expanduser()
    venv_path = Path(args.venv).expanduser()
    browser_enabled = args.enable_browser
    node_role = args.node_role

    if args.yes:
        return workspace, venv_path, browser_enabled, node_role or DEFAULT_NODE_ROLE

    answer = _prompt(f"Workspace path [{workspace}]: ").strip()
    if answer:
        workspace = Path(answer).expanduser()

    if not browser_enabled:
        browser_answer = _prompt("Prepare optional Playwright browser automation? [y/N]: ").strip().lower()
        browser_enabled = browser_answer in {"y", "yes"}

    if node_role is None:
        role_answer = _prompt(
            "Node role: [1] Primary Orchestrator, [2] Worker Node [1]: "
        ).strip().lower()
        if role_answer in {"2", "worker", "worker node", NODE_ROLE_WORKER}:
            node_role = NODE_ROLE_WORKER
        else:
            node_role = NODE_ROLE_ORCHESTRATOR

    return workspace, venv_path, browser_enabled, node_role


def print_summary(
    *,
    mode: str,
    workspace: Path,
    venv_path: Path,
    docker_status: DockerStatus,
    compose_gpu: str,
    browser_enabled: bool,
    python_ready: bool,
    node_role: str = DEFAULT_NODE_ROLE,
) -> None:
    print()
    print("FreeTalon setup complete.")
    print()
    print(f"Mode: {mode}")
    role_label = "Primary Orchestrator" if node_role == NODE_ROLE_ORCHESTRATOR else "Worker Node"
    print(f"Node role: {role_label}")
    print(f"Workspace: {workspace}")
    print(f"Virtualenv: {venv_path}")
    print(f"Dashboard URL: http://{UI_HOST}:{UI_PORT}")
    print()
    if python_ready:
        print("Golden path:")
        print(f"  1. python3 {REPO_ROOT / 'dashboard.py'}")
        print(f"  2. Open http://{UI_HOST}:{UI_PORT}")
        print()
        print("Other supported commands:")
        print(f"  Local API: {venv_python_path(venv_path)} -m freetalon.cli start")
    else:
        print("Local UI/API dependencies were not installed in this mode.")
        print(f"Run: python3 {REPO_ROOT / 'installer.py'} --yes --mode full")
    if docker_status.cli_available and docker_status.compose_available:
        print("  Docker services: docker compose up -d")
    else:
        print("  Docker services: unavailable until Docker + Docker Compose are installed")

    if compose_gpu == GPU_NONE:
        print("  Docker profile: CPU-safe defaults")
    elif compose_gpu == GPU_NVIDIA:
        print("  Docker profile: NVIDIA runtime")
    else:
        print("  Docker profile: AMD ROCm")

    if browser_enabled:
        print("  Browser automation: local Playwright + Chromium installed")
    else:
        print(f"  Browser automation: optional, rerun with 'python3 {REPO_ROOT / 'installer.py'} --yes --enable-browser'")


def launch_ui(venv_path: Path, workspace: Path) -> None:
    python_executable = venv_python_path(venv_path)
    _status("[..]", "Starting dashboard in the project virtual environment")
    # Inherit stdout/stderr so dashboard startup errors stay visible instead of
    # being swallowed by the installer.
    process = subprocess.Popen(
        [str(python_executable), str(REPO_ROOT / "dashboard.py")],
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
    )
    pid_path = workspace / "freetalon-dashboard.pid"
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    _ok(f"Dashboard launch requested; open http://{UI_HOST}:{UI_PORT}")
    _ok(f"Dashboard PID {process.pid} recorded in {pid_path}")
    if os.name == "nt":
        _ok(f"Stop it with: taskkill /PID {process.pid} /F")
    else:
        _ok(f"Stop it with: kill $(cat {pid_path})")


def main() -> int:
    args = parse_args()
    workspace, venv_path, browser_enabled, node_role = resolve_options(args)

    print("FreeTalon installer")
    print("===================")

    if not check_python_version():
        _fail(f"Python 3.10+ is required; found {platform.python_version()}")
        return 1
    _ok(f"Python {platform.python_version()} detected")

    if check_linux():
        _ok("Linux host detected")
    else:
        _warn(f"Detected {platform.system()}; Linux is recommended for Docker GPU support")

    ensure_workspace_dirs(workspace)

    python_executable = venv_python_path(venv_path)
    python_ready = args.mode in {"ui", "api", "full"}
    if python_ready:
        python_executable = ensure_virtualenv(venv_path)
        if not args.skip_python_deps:
            install_requirements(python_executable)
        if browser_enabled and not args.skip_playwright_install:
            ensure_playwright(python_executable)
        elif browser_enabled:
            _warn("Skipped Playwright installation by request")

    detected_gpu = detect_gpu()
    docker_status = DockerStatus(False, False, False)
    compose_gpu = GPU_NONE
    if args.mode in {"docker", "full"}:
        if args.skip_docker_validation:
            docker_status = DockerStatus(
                cli_available=shutil.which("docker") is not None,
                daemon_reachable=False,
                compose_available=False,
            )
        else:
            docker_status = detect_docker_status()

        if not docker_status.cli_available:
            message = "Docker is not installed."
            if args.mode == "docker":
                _fail(message)
                return 1
            _warn(message + " Continuing with local UI/API setup only.")
        elif not docker_status.daemon_reachable:
            message = "Docker daemon is not reachable."
            if args.mode == "docker":
                _fail(message if docker_status.error is None else f"{message} {docker_status.error}")
                return 1
            _warn(message if docker_status.error is None else f"{message} {docker_status.error}")

        compose_gpu, warnings = select_compose_gpu(detected_gpu, docker_status)
        for message in docker_status.warnings + warnings:
            _warn(message)

        generate_compose(compose_gpu, REPO_ROOT / "docker-compose.yml")
        if docker_status.compose_available and not args.skip_docker_validation:
            validate_compose(REPO_ROOT / "docker-compose.yml", docker_status)
        if args.build_images and docker_status.daemon_reachable:
            build_local_images(browser_enabled)
        elif args.build_images:
            _warn("Skipped local Docker image builds because the Docker daemon is not reachable")
    else:
        _ok("Docker compose generation skipped for selected mode")

    generate_env(
        workspace=str(workspace),
        install_mode=args.mode,
        docker_profile=compose_gpu,
        browser_enabled=browser_enabled,
        path=REPO_ROOT / ".env",
        node_role=node_role,
    )

    print_summary(
        mode=args.mode,
        workspace=workspace,
        venv_path=venv_path,
        docker_status=docker_status,
        compose_gpu=compose_gpu,
        browser_enabled=browser_enabled,
        python_ready=python_ready,
        node_role=node_role,
    )

    if args.start_ui:
        if not python_ready:
            _fail("--start-ui requires --mode ui, --mode api, or --mode full")
            return 1
        launch_ui(venv_path, workspace)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
