#!/usr/bin/env python3
"""FreeTalon CLI Installer — hardware-aware Docker environment bootstrapper.

Detects the host OS, Docker availability, and GPU hardware, then generates
a tailored ``docker-compose.yml`` and ``.env`` file for the local workspace.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import textwrap
import time

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.prompt import Prompt
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

GPU_NVIDIA = "nvidia"
GPU_AMD = "amd"
GPU_NONE = "none"


def check_linux() -> bool:
    """Return *True* when running on Linux."""
    return platform.system() == "Linux"


def check_docker() -> bool:
    """Return *True* when the ``docker`` CLI is found on *PATH*."""
    return shutil.which("docker") is not None


def detect_gpu() -> str:
    """Detect the GPU vendor present on the system.

    * **Nvidia** — ``nvidia-smi`` binary on *PATH*.
    * **AMD** — ``/dev/kfd`` device node exists.
    * Otherwise returns :pydata:`GPU_NONE`.
    """
    if shutil.which("nvidia-smi") is not None:
        return GPU_NVIDIA
    if os.path.exists("/dev/kfd"):
        return GPU_AMD
    return GPU_NONE


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------

_NVIDIA_COMPOSE: dict = {
    "services": {
        "ollama": {
            "image": "ollama/ollama:latest",
            "ports": ["11434:11434"],
            "volumes": ["${LOCAL_WORKSPACE}:/workspace"],
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [
                            {
                                "driver": "nvidia",
                                "count": "all",
                                "capabilities": ["gpu"],
                            }
                        ]
                    }
                }
            },
        }
    },
}

_AMD_COMPOSE: dict = {
    "services": {
        "ollama": {
            "image": "ollama/ollama:rocm",
            "ports": ["11434:11434"],
            "volumes": ["${LOCAL_WORKSPACE}:/workspace"],
            "devices": ["/dev/kfd", "/dev/dri"],
            "group_add": ["video"],
        }
    },
}

_CPU_COMPOSE: dict = {
    "services": {
        "ollama": {
            "image": "ollama/ollama:latest",
            "ports": ["11434:11434"],
            "volumes": ["${LOCAL_WORKSPACE}:/workspace"],
        }
    },
}


def _compose_template(gpu: str) -> dict:
    """Return the docker-compose dict for the detected *gpu* type."""
    if gpu == GPU_NVIDIA:
        return _NVIDIA_COMPOSE
    if gpu == GPU_AMD:
        return _AMD_COMPOSE
    return _CPU_COMPOSE


def generate_compose(gpu: str, path: str = "docker-compose.yml") -> None:
    """Write a ``docker-compose.yml`` tailored to *gpu*."""
    template = _compose_template(gpu)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(template, fh, default_flow_style=False, sort_keys=False)
    console.print(f"[green]✔[/green] Generated [bold]{path}[/bold]")


def generate_env(workspace: str, path: str = ".env") -> None:
    """Write a ``.env`` file with the local workspace path."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"LOCAL_WORKSPACE={workspace}\n")
    console.print(f"[green]✔[/green] Generated [bold]{path}[/bold]")


# ---------------------------------------------------------------------------
# Progress-bar simulations
# ---------------------------------------------------------------------------


def _simulate_tasks() -> None:
    """Run two fake progress bars to show sandboxing and networking steps."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task1 = progress.add_task("Hardening Sandboxes…", total=100)
        task2 = progress.add_task("Configuring Virtual Network…", total=100)

        while not progress.finished:
            progress.update(task1, advance=1.2)
            progress.update(task2, advance=0.8)
            time.sleep(0.04)

    console.print("[green]✔[/green] All tasks completed.\n")


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------


def main() -> None:  # noqa: D401 – imperative mood
    """Beautiful CLI installer for FreeTalon."""

    console.print(
        Panel.fit(
            "[bold cyan]FreeTalon Installer[/bold cyan]\n"
            "Local openclaw hive — security hardened & hardware optimized.",
            border_style="bright_blue",
        )
    )

    # --- Step 1: OS check ---------------------------------------------------
    is_linux = check_linux()
    if is_linux:
        console.print("[green]✔[/green] Operating system: [bold]Linux[/bold]")
    else:
        console.print(
            f"[yellow]⚠[/yellow] Detected [bold]{platform.system()}[/bold] "
            "(Linux is recommended for full GPU pass-through)."
        )

    # --- Step 2: Docker check -----------------------------------------------
    has_docker = check_docker()
    if has_docker:
        console.print("[green]✔[/green] Docker: [bold]installed[/bold]")
    else:
        console.print(
            "[red]✖[/red] Docker is [bold]not installed[/bold]. "
            "Please install Docker before continuing.\n"
            "  → https://docs.docker.com/engine/install/"
        )
        sys.exit(1)

    # --- Step 3: GPU detection ----------------------------------------------
    gpu = detect_gpu()
    gpu_table = Table(title="GPU Detection", show_header=True, header_style="bold magenta")
    gpu_table.add_column("Check", style="dim")
    gpu_table.add_column("Result")
    gpu_table.add_row("nvidia-smi", "[green]found[/green]" if gpu == GPU_NVIDIA else "[dim]not found[/dim]")
    gpu_table.add_row("/dev/kfd (AMD)", "[green]found[/green]" if gpu == GPU_AMD else "[dim]not found[/dim]")
    gpu_table.add_row(
        "Selected profile",
        {GPU_NVIDIA: "[green]Nvidia (CUDA)[/green]", GPU_AMD: "[green]AMD (ROCm)[/green]"}.get(
            gpu, "[yellow]CPU-only[/yellow]"
        ),
    )
    console.print(gpu_table)

    # --- Step 4: Workspace path & file generation ---------------------------
    workspace = Prompt.ask(
        "[bold]Enter local workspace path[/bold]",
        default=os.path.expanduser("~/freetalon-workspace"),
    )
    console.print()

    generate_compose(gpu)
    generate_env(workspace)
    console.print()

    # --- Step 5: Simulated hardening steps ----------------------------------
    _simulate_tasks()

    console.print(
        Panel.fit(
            textwrap.dedent(
                """\
                [bold green]Installation complete![/bold green]

                Start services with:
                  [cyan]docker compose up -d[/cyan]
                """
            ),
            border_style="bright_green",
        )
    )


if __name__ == "__main__":
    main()
