from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

from .audit import clear_request_id, current_request_id, set_request_id
from .docker_manager import resource_summary_safe
from .hive import HiveController
from .security import authorize

_TASK_ID_RE = __import__("re").compile(r"^[0-9a-f]{12}$")
_MAX_BODY_DEFAULT = 65536


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "message": message}


class HiveAPIServer:
    def __init__(self, controller: HiveController, token: str) -> None:
        self.controller = controller
        self.token = token
        self._httpd: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str, port: int) -> None:
        api = self
        max_body = getattr(api.controller.config, "max_request_body_bytes", _MAX_BODY_DEFAULT)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                rid = uuid.uuid4().hex[:12]
                set_request_id(rid)
                try:
                    self._handle_get()
                finally:
                    clear_request_id()

            def _handle_get(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if path == "/health":
                    return self._send(200, {"ok": True, "status": "alive"})

                if path == "/health/ready":
                    status = api.controller.status()
                    workers_healthy = status["workers"]["healthy"]
                    if workers_healthy > 0 or status["workers"]["configured"] > 0:
                        return self._send(200, {"ok": True, "status": "ready"})
                    return self._send(503, _error("NOT_READY", "No healthy workers available"))

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

                parts = path.split("/")
                if len(parts) == 3 and parts[1] == "tasks":
                    task_id = parts[2]
                    if not _TASK_ID_RE.fullmatch(task_id):
                        return self._send(404, _error("NOT_FOUND", "Task not found"))
                    if not self._authorized():
                        return
                    task = api.controller.get_task(task_id)
                    if task is None:
                        return self._send(404, _error("NOT_FOUND", "Task not found"))
                    return self._send(200, {"ok": True, "task": task})

                if path == "/resources":
                    if not self._authorized():
                        return
                    return self._send(200, {"ok": True, "resources": resource_summary_safe()})

                return self._send(404, _error("NOT_FOUND", "Not Found"))

            def do_POST(self) -> None:  # noqa: N802
                rid = uuid.uuid4().hex[:12]
                set_request_id(rid)
                try:
                    self._handle_post()
                finally:
                    clear_request_id()

            def _handle_post(self) -> None:
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
                        msg = str(exc)
                        code = "QUEUE_FULL" if "Queue is full" in msg else "INVALID_PAYLOAD"
                        return self._send(400, _error(code, msg))
                    return self._send(201, {"ok": True, "task_id": task.task_id})

                parts = path.split("/")
                if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "cancel":
                    task_id = parts[2]
                    if not _TASK_ID_RE.fullmatch(task_id):
                        return self._send(404, _error("NOT_FOUND", "Task not found"))
                    if not self._authorized():
                        return
                    ok = api.controller.cancel_task(task_id)
                    if ok:
                        return self._send(200, {"ok": True, "task_id": task_id})
                    return self._send(404, _error("NOT_FOUND", "Task not found"))

                return self._send(404, _error("NOT_FOUND", "Not Found"))

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                token = ""
                if header.startswith("Bearer "):
                    token = header[7:].strip()
                if authorize(token, api.token):
                    return True
                api.controller.audit.log("auth.denied", path=self.path, source=self.client_address[0])
                self._send(401, _error("UNAUTHORIZED", "Unauthorized"))
                return False

            def _json_body(self) -> dict[str, Any] | None:
                length_str = self.headers.get("Content-Length", "0")
                try:
                    length = int(length_str)
                except ValueError:
                    self._send(400, _error("INVALID_REQUEST", "Invalid Content-Length"))
                    return None
                if length > max_body:
                    self._send(413, _error("PAYLOAD_TOO_LARGE", f"Request body exceeds {max_body} bytes"))
                    return None
                body = self.rfile.read(length)
                try:
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("JSON body must be an object")
                    return data
                except (json.JSONDecodeError, ValueError) as exc:
                    self._send(400, _error("INVALID_JSON", str(exc)))
                    return None

            def _send(self, code: int, data: dict[str, Any]) -> None:
                encoded = json.dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                rid = current_request_id()
                if rid:
                    self.send_header("X-Request-ID", rid)
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
