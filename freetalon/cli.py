from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .api import HiveAPIServer
from .config import HiveConfig
from .hive import HiveController
from .security import load_secret, redact_secret


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 4,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed: {exc}") from exc


def _pid_path(config: HiveConfig) -> Path:
    return config.workspace / "freetalon.pid"


def _base_url(config: HiveConfig) -> str:
    return f"http://{config.host}:{config.port}"


def _load_config(args: argparse.Namespace) -> HiveConfig:
    workspace = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None
    state_path = Path(args.state_path).expanduser() if getattr(args, "state_path", None) else None
    audit_log = Path(args.audit_log_path).expanduser() if getattr(args, "audit_log_path", None) else None
    token_file = Path(args.token_file).expanduser() if getattr(args, "token_file", None) else None
    kwargs: dict[str, Any] = {}
    if workspace:
        kwargs["workspace"] = workspace
    if state_path:
        kwargs["state_path"] = state_path
    if audit_log:
        kwargs["audit_log_path"] = audit_log
    if token_file:
        kwargs["api_token_file"] = token_file
    if getattr(args, "host", None):
        kwargs["host"] = args.host
    if getattr(args, "port", None):
        kwargs["port"] = args.port
    return HiveConfig(**kwargs)


def cmd_start(args: argparse.Namespace) -> int:
    config = _load_config(args)
    config.ensure_directories()
    pid_path = _pid_path(config)
    if pid_path.exists():
        try:
            os.kill(int(pid_path.read_text(encoding="utf-8").strip()), 0)
            print("already running")
            return 1
        except OSError:
            pid_path.unlink()

    cmd = [sys.executable, "-m", "freetalon.cli"]
    cmd += ["--host", config.host, "--port", str(config.port), "--workspace", str(config.workspace)]
    if config.state_path:
        cmd += ["--state-path", str(config.state_path)]
    if config.audit_log_path:
        cmd += ["--audit-log-path", str(config.audit_log_path)]
    if config.api_token_file:
        cmd += ["--token-file", str(config.api_token_file)]
    cmd += ["serve"]

    stdout_path = config.workspace / "freetalon.out.log"
    stderr_path = config.workspace / "freetalon.err.log"
    with stdout_path.open("a", encoding="utf-8") as out, stderr_path.open("a", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, start_new_session=True, stdout=out, stderr=err)  # noqa: S603
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    print(f"started pid={proc.pid} host={config.host} port={config.port}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = load_secret(
        str(config.api_token_file) if config.api_token_file else None,
        config.api_token_env,
    )
    controller = HiveController(config)
    server = HiveAPIServer(controller, token)
    controller.start()
    server.start(config.host, config.port)
    controller.audit.log("api.started", host=config.host, port=config.port, token=redact_secret(token))

    stop = False

    def _handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not stop:
            time.sleep(0.25)
    finally:
        server.stop()
        controller.stop()
        pid = _pid_path(config)
        if pid.exists():
            pid.unlink()
    return 0


def _token_from_args(config: HiveConfig, args: argparse.Namespace) -> str:
    if getattr(args, "token", None):
        return str(args.token)
    return load_secret(
        str(config.api_token_file) if config.api_token_file else None,
        config.api_token_env,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    config = _load_config(args)
    pid_path = _pid_path(config)
    if not pid_path.exists():
        print("not running")
        return 1
    try:
        os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)
    except OSError:
        pass
    for _ in range(30):
        if not pid_path.exists():
            print("stopped")
            return 0
        time.sleep(0.1)
    print("stop requested")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = _token_from_args(config, args)
    data = _request("GET", f"{_base_url(config)}/status", token)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with urllib.request.urlopen(f"{_base_url(config)}/health", timeout=3) as resp:
        print(resp.read().decode("utf-8"))
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = _token_from_args(config, args)
    payload: dict[str, Any] = {"action": args.action}
    if args.action == "echo":
        payload["text"] = args.text
    elif args.action == "sum":
        payload["values"] = [float(x) for x in args.values.split(",") if x.strip()]
    elif args.action == "sleep":
        payload["duration"] = float(args.duration)
    elif args.action == "docker_claw":
        payload["code"] = args.code
        payload["profile"] = args.profile
        payload["timeout"] = args.timeout
    payload["retries"] = args.retries
    payload["backoff_seconds"] = args.backoff
    payload["requires_gpu"] = args.requires_gpu
    data = _request("POST", f"{_base_url(config)}/tasks", token, payload=payload)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = _token_from_args(config, args)
    data = _request("POST", f"{_base_url(config)}/tasks/{args.task_id}/cancel", token, payload={})
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = _token_from_args(config, args)
    url = f"{_base_url(config)}/tasks"
    if getattr(args, "status_filter", None):
        url += f"?status={args.status_filter}"
    data = _request("GET", url, token)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = _token_from_args(config, args)
    data = _request("GET", f"{_base_url(config)}/tasks/{args.task_id}", token)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_resources(args: argparse.Namespace) -> int:
    config = _load_config(args)
    token = _token_from_args(config, args)
    data = _request("GET", f"{_base_url(config)}/resources", token)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freetalon")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--workspace", default=str(Path.home() / "freetalon-workspace"))
    p.add_argument("--state-path", default=str(Path.home() / "freetalon-workspace" / "hive-state.json"))
    p.add_argument("--audit-log-path", default=str(Path.home() / "freetalon-workspace" / "audit.log"))
    p.add_argument("--token-file", default=None)

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start")
    sub.add_parser("serve")
    sub.add_parser("stop")
    sub.add_parser("status").add_argument("--token", default=None)
    sub.add_parser("health")

    submit = sub.add_parser("submit")
    submit.add_argument("--token", default=None)
    submit.add_argument("--action", choices=["echo", "sum", "sleep", "docker_claw"], required=True)
    submit.add_argument("--text", default="")
    submit.add_argument("--values", default="")
    submit.add_argument("--duration", default="0")
    submit.add_argument("--code", default="")
    submit.add_argument("--profile", default="default")
    submit.add_argument("--timeout", type=float, default=30.0)
    submit.add_argument("--retries", type=int, default=0)
    submit.add_argument("--backoff", type=float, default=0.2)
    submit.add_argument("--requires-gpu", action="store_true")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--token", default=None)
    cancel.add_argument("task_id")

    tasks_p = sub.add_parser("tasks")
    tasks_p.add_argument("--token", default=None)
    tasks_p.add_argument("--status", dest="status_filter", default=None,
                         choices=["queued", "running", "retrying", "succeeded", "failed", "cancelled"])

    task_p = sub.add_parser("task")
    task_p.add_argument("--token", default=None)
    task_p.add_argument("task_id")

    resources_p = sub.add_parser("resources")
    resources_p.add_argument("--token", default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "start": cmd_start,
        "serve": cmd_serve,
        "stop": cmd_stop,
        "status": cmd_status,
        "health": cmd_health,
        "submit": cmd_submit,
        "cancel": cmd_cancel,
        "tasks": cmd_tasks,
        "task": cmd_task,
        "resources": cmd_resources,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
