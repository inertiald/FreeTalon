from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

from .hive import HiveController
from .security import authorize


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HiveAPIServer:
    def __init__(self, controller: HiveController, token: str) -> None:
        self.controller = controller
        self.token = token
        self._httpd: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str, port: int) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/health":
                    return self._send(200, {"ok": True, "status": "ready"})
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
                encoded = json.dumps(data).encode("utf-8")
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
