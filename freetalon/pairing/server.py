"""HTTP layer for the pairing endpoint (DESIGN.md section 6).

Routes, status codes, and the auth model are pinned in CONTRACT.md.
Stdlib only, mirroring freetalon/api.py's server shape. The operator
token comes from the environment and is compared via
freetalon.security.authorize (hmac.compare_digest); with no token
configured the operator API fails closed.

Deployment option (CONTRACT.md, "Static fallthrough"): when a static
root is configured, GET/HEAD requests outside the /pairing namespace
serve files from that tree, so one process can front both the pairing
API and the provisioning payloads. Off by default; the pairing routes
are matched first and are unaffected either way.

Fleet update channel (CONTRACT.md, "Fleet update channel"): when a
fleet directory is configured, the /fleet routes serve signed update
bundles to joined nodes and collect their reports. Off by default,
exactly like the static fallthrough; the pairing routes are unaffected
either way.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import unquote, urlparse

from ..security import authorize
from .updates import FleetStore, InvalidBundle as FleetInvalidBundle
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
    static_root: str | None = None
    fleet_dir: str | None = None

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
            static_root=env.get("FREETALON_PAIRING_STATIC_ROOT") or None,
            fleet_dir=env.get("FREETALON_FLEET_DIR") or None,
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


def _fleet_route_kind(path: str) -> str | None:
    """Which fleet-channel route a path names, regardless of method.

    One of "view", "updates", "report" -- or None. Same dispatch
    discipline as _route_kind: known path with the wrong method is a
    405, anything else falls through (to the static root or a 404).
    """
    if path == "/fleet":
        return "view"
    if path == "/fleet/report":
        return "report"
    if path.startswith("/fleet/updates/"):
        machine_id = path[len("/fleet/updates/"):]
        return "updates" if machine_id and "/" not in machine_id else None
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
        fkind = _fleet_route_kind(path)
        if kind == "list":
            self._handle_list()
        elif kind == "poll":
            self._handle_poll(path[len("/pairing/"):])
        elif kind is not None:
            self._send_json(405, {"error": "method not allowed"})
        elif fkind == "updates":
            self._handle_fleet_updates(path[len("/fleet/updates/"):])
        elif fkind == "view":
            self._handle_fleet_view()
        elif fkind is not None:
            self._send_json(405, {"error": "method not allowed"})
        elif self.server.pairing_static_root is not None:
            self._serve_static(head=False)
        else:
            self._send_json(404, {"error": "not found"})

    def _serve_static(self, head: bool) -> None:
        """Serve one file from the configured static root.

        The request path is percent-decoded and resolved under the
        root; anything that resolves outside the root, a missing file,
        or a directory without an index.html is the same JSON 404 the
        API returns for unknown routes. No directory listings. Only
        reached when a static root is configured (CONTRACT.md, "Static
        fallthrough").
        """
        root = Path(self.server.pairing_static_root or "").resolve()
        decoded = unquote(urlparse(self.path).path)
        parts = [p for p in decoded.split("/") if p not in ("", ".")]
        try:
            candidate = root.joinpath(*parts).resolve()
            if candidate.is_dir():
                candidate = (candidate / "index.html").resolve()
        except (OSError, ValueError):
            candidate = None
        if (
            candidate is None
            or not candidate.is_relative_to(root)
            or not candidate.is_file()
        ):
            self._send_json(404, {"error": "not found"})
            return
        size = candidate.stat().st_size
        content_type = (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if head:
            return
        try:
            with candidate.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away mid-download; nothing to answer

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        kind = _route_kind(path)
        fkind = _fleet_route_kind(path)
        if kind == "register":
            self._handle_register()
        elif kind == "confirm":
            self._handle_confirm(path[len("/pairing/"):-len("/confirm")])
        elif kind is not None:
            self._send_json(405, {"error": "method not allowed"})
        elif fkind == "report":
            self._handle_fleet_report()
        elif fkind is not None:
            self._send_json(405, {"error": "method not allowed"})
        else:
            self._send_json(404, {"error": "not found"})

    def _reject_method(self) -> None:
        path = urlparse(self.path).path
        if _route_kind(path) is not None or _fleet_route_kind(path) is not None:
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
        path = urlparse(self.path).path
        if _route_kind(path) is not None or _fleet_route_kind(path) is not None:
            self._send_json(405, {"error": "method not allowed"})
        elif self.server.pairing_static_root is not None:
            self._serve_static(head=True)
        else:
            self._send_json(404, {"error": "not found"})

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

    # -- fleet update channel (CONTRACT.md, "Fleet update channel") ------

    def _handle_fleet_updates(self, machine_id: str) -> None:
        store = self.server.fleet_store
        if store is None:
            self._send_json(404, {"error": "not found"})
            return
        bundle = store.current_bundle(machine_id)
        if bundle is None:
            self._send_json(404, {"error": "no bundle published"})
            return
        self._send_json(200, bundle)

    def _handle_fleet_report(self) -> None:
        store = self.server.fleet_store
        if store is None:
            self._send_json(404, {"error": "not found"})
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            view = store.record_report(body.get("machine_id"), body)
        except FleetInvalidBundle as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"ok": True, "node": view})

    def _handle_fleet_view(self) -> None:
        if self.server.fleet_store is None:
            self._send_json(404, {"error": "not found"})
            return
        if not self._require_operator():
            return
        self._send_json(200, self.server.fleet_store.fleet_view())


class _PairingHTTPServer(_ThreadingHTTPServer):
    pairing_store: PairingStore
    pairing_token: str | None
    pairing_static_root: str | None = None
    fleet_store: FleetStore | None = None


def make_server(
    store: PairingStore,
    token: str | None,
    host: str = "127.0.0.1",
    port: int = 0,
    static_root: str | None = None,
    fleet_dir: str | None = None,
) -> _PairingHTTPServer:
    httpd = _PairingHTTPServer((host, port), _PairingHandler)
    httpd.pairing_store = store
    httpd.pairing_token = token or None
    httpd.pairing_static_root = static_root or None
    httpd.fleet_store = FleetStore(fleet_dir) if fleet_dir else None
    return httpd


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    config = PairingServerConfig.from_env()
    store = PairingStore(config.store_path, ttl_seconds=config.ttl_seconds)
    httpd = make_server(
        store,
        config.token,
        host=config.host,
        port=config.port,
        static_root=config.static_root,
        fleet_dir=config.fleet_dir,
    )
    logger.info(
        "pairing server listening on %s:%d (store %s, operator API %s, "
        "static root %s, fleet dir %s)",
        config.host,
        config.port,
        config.store_path,
        "enabled" if config.token else "CLOSED (no FREETALON_PAIRING_TOKEN)",
        config.static_root or "off",
        config.fleet_dir or "off",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
