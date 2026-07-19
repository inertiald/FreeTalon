from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path


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
    if module_available(module_name):
        return

    project_python = venv_python_path(project_root)
    if (
        os.environ.get("FREETALON_BOOTSTRAPPED") != "1"
        and python_can_import(project_python, module_name)
    ):
        env = os.environ.copy()
        env["FREETALON_BOOTSTRAPPED"] = "1"
        os.execvpe(str(project_python), [str(project_python), *sys.argv], env)

    raise SystemExit(missing_module_message(module_name, install_command))
