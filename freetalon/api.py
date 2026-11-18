from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

from .docker_manager import resource_summary_safe
from .hive import HiveController
from .orchestrator.gates import GateStore
from .security import authorize
from .jsonutil import json_default


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HiveAPIServer:
    def __init__(
        self,
        controller: HiveController,
        token: str,
        gate_store: GateStore | None = None,
    ) -> None:
        self.controller = controller
        self.token = token
        # M4: gate packets share the plan store's SQLite file by default.
        self.gate_store = gate_store if gate_store is not None else GateStore()
        self._httpd: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str, port: int) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if path == "/health":
                    import socket
                    import subprocess

                    ollama_reachable = False
                    try:
                        sock = socket.create_connection(("localhost", 11434), timeout=2)
                        sock.close()
                        ollama_reachable = True
                    except OSError:
                        pass

                    gpu_temp = None
                    try:
                        result = subprocess.run(
                            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            gpu_temp = int(result.stdout.strip().split("\n")[0])
                    except (OSError, ValueError):
                        pass

                    return self._send(200, {
                        "ok": True,
                        "status": "ready",
                        "ollama_reachable": ollama_reachable,
                        "gpu_temperature": gpu_temp,
                    })

                if path == "/status":
                    if not self._authorized():
                        return
                    return self._send(200, {"ok": True, "status": api.controller.status()})

                if path == "/metrics":
                    if not self._authorized():
                        return
                    status = api.controller.status()
                    metrics = {
                        "workers_healthy": status["workers"]["healthy"],
                        "workers_configured": status["workers"]["configured"],
                        "tasks_running": status["queue"]["running"],
                        "tasks_queued": status["queue"]["queued"],
                        "queue_max": status["queue"]["max"],
                    }
                    return self._send(200, {"ok": True, "metrics": metrics})

                if path == "/tasks":
                    if not self._authorized():
                        return
                    status_filter = qs.get("status", [None])[0]
                    tasks = api.controller.list_tasks(status=status_filter)
                    return self._send(200, {"ok": True, "tasks": tasks, "count": len(tasks)})

                if path.startswith("/tasks/") and len(path.split("/")) == 3:
                    if not self._authorized():
                        return
                    task_id = path.split("/")[2]
                    task = api.controller.get_task(task_id)
                    if task is None:
                        return self._send(404, {"ok": False, "error": "Task not found"})
                    return self._send(200, {"ok": True, "task": task})

                if path == "/resources":
                    if not self._authorized():
                        return
                    return self._send(200, {"ok": True, "resources": resource_summary_safe()})

                # M4: approval gates — list packets, optionally filtered.
                if path == "/gates":
                    if not self._authorized():
                        return
                    status_filter = qs.get("status", [None])[0]
                    run_filter = qs.get("run_id", [None])[0]
                    if run_filter:
                        packets = api.gate_store.list_for_run(run_filter)
                    elif status_filter == "pending":
                        packets = api.gate_store.pending()
                    else:
                        packets = api.gate_store.all()
                    return self._send(
                        200,
                        {
                            "ok": True,
                            "gates": [pkt.model_dump(mode="json") for pkt in packets],
                            "count": len(packets),
                        },
                    )

                if path.startswith("/gates/") and len(path.split("/")) == 3:
                    if not self._authorized():
                        return
                    packet_id = path.split("/")[2]
                    packet = api.gate_store.get(packet_id)
                    if packet is None:
                        return self._send(404, {"ok": False, "error": "Gate not found"})
                    return self._send(200, {"ok": True, "gate": packet.model_dump(mode="json")})

                return self._send(404, {"ok": False, "error": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/shutdown":
                    if not self._authorized():
                        return
                    self._send(200, {"ok": True, "stopping": True})
                    threading.Thread(target=api.stop, daemon=True).start()
                    return

                if path == "/tasks":
                    if not self._authorized():
                        return
                    body = self._json_body()
                    if body is None:
                        return
                    try:
                        task = api.controller.submit_task(body)
                    except ValueError as exc:
                        return self._send(400, {"ok": False, "error": str(exc)})
                    return self._send(201, {"ok": True, "task_id": task.task_id})

                if path.startswith("/tasks/") and path.endswith("/cancel"):
                    if not self._authorized():
                        return
                    task_id = path.split("/")[2]
                    ok = api.controller.cancel_task(task_id)
                    return self._send(200 if ok else 404, {"ok": ok, "task_id": task_id})

                # M4: approve / deny a gate packet. Body: {decided_by, note?}.
                if path.startswith("/gates/") and (
                    path.endswith("/approve") or path.endswith("/deny")
                ):
                    if not self._authorized():
                        return
                    parts = path.split("/")
                    packet_id, action = parts[2], parts[3]
                    body = self._json_body()
                    if body is None:
                        return
                    decided_by = str(body.get("decided_by") or "api")
                    try:
                        packet = api.gate_store.decide(
                            packet_id, action, decided_by, note=body.get("note")
                        )
                    except ValueError as exc:
                        return self._send(400, {"ok": False, "error": str(exc)})
                    return self._send(
                        200,
                        {
                            "ok": True,
                            "gate": packet.model_dump(mode="json"),
                        },
                    )

                return self._send(404, {"ok": False, "error": "Not Found"})

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                token = ""
                if header.startswith("Bearer "):
                    token = header[7:].strip()
                if authorize(token, api.token):
                    return True
                api.controller.audit.log("auth.denied", path=self.path, source=self.client_address[0])
                self._send(401, {"ok": False, "error": "Unauthorized"})
                return False

            def _json_body(self) -> dict[str, Any] | None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("JSON body must be an object")
                    return data
                except (json.JSONDecodeError, ValueError) as exc:
                    self._send(400, {"ok": False, "error": str(exc)})
                    return None

            def _send(self, code: int, data: dict[str, Any]) -> None:
                encoded = json.dumps(data, default=json_default).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        self._httpd = _ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
