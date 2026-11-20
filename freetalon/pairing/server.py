"""HTTP layer for the pairing endpoint (DESIGN.md section 6).

Routes, status codes, and the auth model are pinned in CONTRACT.md.
Stdlib only, mirroring freetalon/api.py's server shape. The operator
token comes from the environment and is compared via
freetalon.security.authorize (hmac.compare_digest); with no token
configured the operator API fails closed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

from ..security import authorize
from .store import (
    CodeAlreadyClaimed,
    CodeAlreadyConfirmed,
    CodeConflict,
    CodeExpired,
    CodeNotFound,
    InvalidBundle,
    InvalidRegistration,
    PairingStore,
    STATUS_CLAIMED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_PENDING,
)

logger = logging.getLogger("freetalon.pairing")

_MAX_BODY_BYTES = 65536
_DEFAULT_STORE_PATH = "/home/seeker/freetalon-workspace/pairing-store.json"


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


@dataclass(frozen=True)
class PairingServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    store_path: str = _DEFAULT_STORE_PATH
    ttl_seconds: float = 1800.0
    token: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "PairingServerConfig":
        env = os.environ if env is None else env
        token = env.get("FREETALON_PAIRING_TOKEN") or None
        return cls(
            host=env.get("FREETALON_PAIRING_HOST") or "0.0.0.0",
            port=int(env.get("FREETALON_PAIRING_PORT") or 8080),
            store_path=env.get("FREETALON_PAIRING_STORE_PATH")
            or _DEFAULT_STORE_PATH,
            ttl_seconds=float(env.get("FREETALON_PAIRING_TTL_SECONDS") or 1800),
            token=token,
        )


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _route_kind(path: str) -> str | None:
    """Which contract route a path names, regardless of method.

    One of "list", "register", "poll", "confirm" -- or None for a path
    no route owns. The method handlers dispatch on this so CONTRACT.md's
    rule -- unknown route 404, known path with the wrong method 405 --
    holds uniformly for every method, instead of BaseHTTPRequestHandler's
    default 501 for methods with no do_* handler.
    """
    if path == "/pairing":
        return "list"
    if path == "/pairing/register":
        return "register"
    if path.startswith("/pairing/"):
        rest = path[len("/pairing/"):]
        if rest.endswith("/confirm"):
            code = rest[: -len("/confirm")]
            return "confirm" if code and "/" not in code else None
        return "poll" if rest and "/" not in rest else None
    return None


class _PairingHandler(BaseHTTPRequestHandler):
    # Set by make_server on the server instance.
    server: "_PairingHTTPServer"

    # -- plumbing ------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        # Method/path/status only, via logging. Never headers or bodies:
        # a claiming poll's response carries credentials, and the
        # operator token must never reach a log.
        logger.info("%s %s", self.command, self.path)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":  # a HEAD response carries no body
            self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any] | None:
        """Parsed JSON object body, or None after an error response."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(400, {"error": "invalid Content-Length"})
            return None
        if length > _MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            self._send_json(400, {"error": "request body must be a JSON object"})
            return None
        return parsed

    def _operator_authorized(self) -> bool:
        configured = self.server.pairing_token
        if not configured:
            return False  # fail closed: no token configured, no operator API
        try:
            return authorize(
                _bearer_token(self.headers.get("Authorization")), configured
            )
        except TypeError:
            # hmac.compare_digest refuses non-ASCII str input. A token
            # carrying such characters can never match the configured
            # one -- refuse it like any other bad token instead of
            # killing the handler and dropping the connection
            # (CONTRACT.md: one indistinguishable 401 per refusal).
            return False

    def _require_operator(self) -> bool:
        if self._operator_authorized():
            return True
        # One indistinguishable refusal for missing header, wrong
        # token, and no token configured (CONTRACT.md).
        self._send_json(401, {"error": "unauthorized"})
        return False

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        kind = _route_kind(path)
        if kind == "list":
            self._handle_list()
        elif kind == "poll":
            self._handle_poll(path[len("/pairing/"):])
        elif kind is not None:
            self._send_json(405, {"error": "method not allowed"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        kind = _route_kind(path)
        if kind == "register":
            self._handle_register()
        elif kind == "confirm":
            self._handle_confirm(path[len("/pairing/"):-len("/confirm")])
        elif kind is not None:
            self._send_json(405, {"error": "method not allowed"})
        else:
            self._send_json(404, {"error": "not found"})

    def _reject_method(self) -> None:
        if _route_kind(urlparse(self.path).path) is not None:
            self._send_json(405, {"error": "method not allowed"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_HEAD(self) -> None:
        self._reject_method()

    def do_OPTIONS(self) -> None:
        self._reject_method()

    def do_TRACE(self) -> None:
        self._reject_method()

    # -- handlers --------------------------------------------------------

    def _handle_register(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        # 201 when this call creates the record (absent, or an expired
        # record being revived per CONTRACT.md), 200 for an idempotent
        # re-register of a live record.
        code = body.get("code")
        creates = True
        if isinstance(code, str):
            for record in self.server.pairing_store.list_records():
                if record["code"] == code and record["status"] != STATUS_EXPIRED:
                    creates = False
        try:
            view = self.server.pairing_store.register(
                code,
                body.get("nonce"),
                body.get("machine_id"),
                hostname=body.get("hostname"),
                mac=body.get("mac"),
            )
        except InvalidRegistration as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except CodeConflict as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(201 if creates else 200, view)

    def _handle_poll(self, code: str) -> None:
        if not code or "/" in code:
            self._send_json(404, {"error": "not found"})
            return
        try:
            outcome = self.server.pairing_store.poll(code)
        except CodeNotFound:
            # Indistinguishable from pending: no existence oracle.
            self._send_json(404, {"code": code, "status": STATUS_PENDING})
            return
        if outcome.status == STATUS_PENDING:
            self._send_json(404, {"code": code, "status": STATUS_PENDING})
        elif outcome.status == STATUS_CONFIRMED:
            self._send_json(
                200,
                {
                    "code": code,
                    "status": STATUS_CONFIRMED,
                    "credentials": outcome.credentials,
                },
            )
        elif outcome.status == STATUS_CLAIMED:
            self._send_json(410, {"code": code, "status": STATUS_CLAIMED})
        elif outcome.status == STATUS_EXPIRED:
            self._send_json(410, {"code": code, "status": STATUS_EXPIRED})
        else:  # pragma: no cover - the store cannot produce this
            self._send_json(500, {"error": "unexpected pairing status"})

    def _handle_confirm(self, code: str) -> None:
        if not code or "/" in code:
            self._send_json(404, {"error": "not found"})
            return
        if not self._require_operator():
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            view = self.server.pairing_store.confirm(code, body.get("credentials"))
        except InvalidBundle as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except CodeNotFound:
            self._send_json(404, {"error": "unknown pairing code"})
            return
        except CodeAlreadyConfirmed as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except (CodeExpired, CodeAlreadyClaimed) as exc:
            status = (
                STATUS_EXPIRED
                if isinstance(exc, CodeExpired)
                else STATUS_CLAIMED
            )
            self._send_json(410, {"code": code, "status": status})
            return
        self._send_json(200, view)

    def _handle_list(self) -> None:
        if not self._require_operator():
            return
        records = self.server.pairing_store.list_records()
        self._send_json(200, {"records": records})


class _PairingHTTPServer(_ThreadingHTTPServer):
    pairing_store: PairingStore
    pairing_token: str | None


def make_server(
    store: PairingStore,
    token: str | None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> _PairingHTTPServer:
    httpd = _PairingHTTPServer((host, port), _PairingHandler)
    httpd.pairing_store = store
    httpd.pairing_token = token or None
    return httpd


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    config = PairingServerConfig.from_env()
    store = PairingStore(config.store_path, ttl_seconds=config.ttl_seconds)
    httpd = make_server(store, config.token, host=config.host, port=config.port)
    logger.info(
        "pairing server listening on %s:%d (store %s, operator API %s)",
        config.host,
        config.port,
        config.store_path,
        "enabled" if config.token else "CLOSED (no FREETALON_PAIRING_TOKEN)",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
