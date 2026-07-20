from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import yaml

import installer
from freetalon.bootstrap import missing_module_message, venv_python_path as bootstrap_venv_python_path


class InstallerTests(unittest.TestCase):
    def test_select_compose_gpu_falls_back_without_nvidia_runtime(self) -> None:
        status = installer.DockerStatus(
            cli_available=True,
            daemon_reachable=True,
            compose_available=True,
            runtimes=(),
        )
        gpu, warnings = installer.select_compose_gpu(installer.GPU_NVIDIA, status)
        self.assertEqual(gpu, installer.GPU_NONE)
        self.assertTrue(warnings)
        self.assertIn("runtime 'nvidia'", warnings[0])

    def test_generate_compose_nvidia_uses_runtime_nvidia(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "docker-compose.yml"
            installer.generate_compose(installer.GPU_NVIDIA, path)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        ollama = data["services"]["ollama"]
        self.assertEqual(ollama["runtime"], "nvidia")
        self.assertEqual(ollama["environment"]["NVIDIA_VISIBLE_DEVICES"], "all")
        self.assertNotIn("deploy", ollama)

    def test_generate_env_includes_launch_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            installer.generate_env(
                workspace="/tmp/free-talon-workspace",
                install_mode="full",
                docker_profile=installer.GPU_NONE,
                browser_enabled=True,
                path=path,
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("LOCAL_WORKSPACE=/tmp/free-talon-workspace", content)
        self.assertIn("FREETALON_INSTALL_MODE=full", content)
        self.assertIn("FREETALON_UI_PORT=7860", content)
        self.assertIn("FREETALON_BROWSER_ENABLED=1", content)
        self.assertIn("FREETALON_NODE_ROLE=orchestrator", content)

    def test_generate_env_records_worker_node_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            installer.generate_env(
                workspace="/tmp/free-talon-workspace",
                install_mode="full",
                docker_profile=installer.GPU_NONE,
                browser_enabled=False,
                path=path,
                node_role=installer.NODE_ROLE_WORKER,
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("FREETALON_NODE_ROLE=worker", content)

    def test_resolve_options_yes_defaults_to_orchestrator_role(self) -> None:
        args = installer.parse_args(["--yes"])
        _, _, _, node_role = installer.resolve_options(args)
        self.assertEqual(node_role, installer.NODE_ROLE_ORCHESTRATOR)

    def test_resolve_options_honors_node_role_flag(self) -> None:
        args = installer.parse_args(["--yes", "--node-role", "worker"])
        _, _, _, node_role = installer.resolve_options(args)
        self.assertEqual(node_role, installer.NODE_ROLE_WORKER)

    def test_resolve_options_prompts_for_node_role(self) -> None:
        args = installer.parse_args([])
        answers = iter(["", "n", "2"])
        original_prompt = installer._prompt
        installer._prompt = lambda prompt: next(answers)
        try:
            _, _, _, node_role = installer.resolve_options(args)
        finally:
            installer._prompt = original_prompt
        self.assertEqual(node_role, installer.NODE_ROLE_WORKER)

    def test_bootstrap_venv_python_path_matches_platform(self) -> None:
        root = Path("/tmp/freetalon-example")
        bindir = "Scripts" if os.name == "nt" else "bin"
        executable = "python.exe" if os.name == "nt" else "python"
        self.assertEqual(
            bootstrap_venv_python_path(root),
            root / ".venv" / bindir / executable,
        )

    def test_missing_module_message_points_to_installer(self) -> None:
        message = missing_module_message("nicegui", "python3 installer.py --yes")
        self.assertIn("nicegui", message)
        self.assertIn("python3 installer.py --yes", message)
        self.assertIn("python3 dashboard.py", message)


if __name__ == "__main__":
    unittest.main()
