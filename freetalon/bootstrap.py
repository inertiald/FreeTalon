from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from freetalon.orchestrator.tool_registry import ToolRegistry, register_default_claws
from freetalon.orchestrator.research import register_research_claws
from freetalon.orchestrator.sandbox import register_sandbox_claws
from freetalon.orchestrator.tool_scaffolder import ToolScaffolder
from freetalon.orchestrator.intake import call_llm

_BOOTSTRAP_ENV_VAR = "FREETALON_BOOTSTRAPPED"


def initialize_orchestrator():
    registry = ToolRegistry(strict=False)
    
    # 1. Register foundational claws
    register_default_claws(registry)

    # 1b. Research, data, and sandbox claws (M3): sourced research, dataset
    # fetch with provenance, and read-only analysis of untrusted files.
    register_research_claws(registry)
    register_sandbox_claws(registry)

    # 2. Configure LLM codegen backend for dynamic capability generation
    def llm_codegen_backend(capability: str, node_inputs: dict) -> str:
        prompt = f"""Write a Python handler function `handle(inputs: dict) -> dict` for the capability '{capability}'.
Inputs schema context: {node_inputs}
Return ONLY valid Python code containing the function definition."""
        return call_llm("You are a Python code generator.", prompt)

    # Absolute quarantine dir under the workspace: the relative default
    # ("generated/proposed_tools") always resolves under the cwd, and the
    # cwd is always on sys.path, so the scaffolder's security guard
    # rejected every call. The workspace path is never on sys.path.
    scaffolder = ToolScaffolder(
        base_dir=Path.home() / "freetalon-workspace" / "generated" / "proposed_tools",
        codegen_backend=llm_codegen_backend,
    )

    return registry, scaffolder




def venv_python_path(project_root: Path, venv_dirname: str = ".venv") -> Path:
    bindir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return project_root / venv_dirname / bindir / executable


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def python_can_import(python_executable: Path, module_name: str) -> bool:
    if not python_executable.exists():
        return False
    result = subprocess.run(
        [
            str(python_executable),
            "-c",
            "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)",
            module_name,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def missing_module_message(module_name: str, install_command: str) -> str:
    return textwrap.dedent(
        f"""\
        Missing required Python module: {module_name}

        Supported setup:
          {install_command}

        Then start the local UI with:
          python3 dashboard.py
        """
    ).strip()


def ensure_module(module_name: str, project_root: Path, install_command: str) -> None:
    """Ensure *module_name* is importable for the current entry-point.

    If the current interpreter cannot import the module but the repository
    virtualenv can, this function replaces the current process with the
    virtualenv interpreter so user-facing commands like ``python3 dashboard.py``
    continue to work without manual activation. This replacement happens
    before the rest of the module imports run, so the original process does
    not continue executing and any in-memory state from the original process
    is intentionally discarded.
    """
    if module_available(module_name):
        return

    project_python = venv_python_path(project_root)
    if (
        os.environ.get(_BOOTSTRAP_ENV_VAR) != "1"
        and python_can_import(project_python, module_name)
    ):
        env = os.environ.copy()
        env[_BOOTSTRAP_ENV_VAR] = "1"
        # Replace the current process so "python3 dashboard.py" transparently
        # runs inside the prepared project virtualenv without requiring shell
        # activation or a wrapper script.
        os.execvpe(str(project_python), [str(project_python), *sys.argv], env)

    raise SystemExit(missing_module_message(module_name, install_command))
