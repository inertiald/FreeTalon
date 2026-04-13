#!/usr/bin/env python3
"""Browser Claw — headless Chromium command server for FreeTalon.

Exposes a minimal HTTP API on ``$CLAW_PORT`` (default 8080) that accepts
newline-safe JSON commands and drives a persistent Playwright/Chromium session.
Screenshots are written to ``$SCREENSHOTS_DIR`` (default ``/screenshots``), which
should be bind-mounted to a host path so the NiceGUI Hub can display them.

HTTP endpoints
--------------
GET  /health
    Returns ``{"ok": true, "status": "ready"}`` when the browser is up.

POST /command
    Body: a JSON object with at minimum a ``"cmd"`` key.
    Returns a JSON object with at minimum an ``"ok"`` boolean key.

Supported commands
------------------
navigate          {"cmd": "navigate", "url": "<url>"}
screenshot        {"cmd": "screenshot", "filename": "<optional>.png",
                   "full_page": false}
click             {"cmd": "click", "selector": "<css>"}
type              {"cmd": "type", "selector": "<css>", "text": "<text>"}
scroll            {"cmd": "scroll", "x": 0, "y": 300}
evaluate          {"cmd": "evaluate", "js": "<expression>"}
wait_for_selector {"cmd": "wait_for_selector", "selector": "<css>",
                   "timeout": 10000}
get_text          {"cmd": "get_text", "selector": "<css>"}
get_url           {"cmd": "get_url"}
list_screenshots  {"cmd": "list_screenshots"}
close             {"cmd": "close"}   — shuts the server down gracefully

Architecture
------------
A single persistent Playwright ``BrowserContext`` lives on the *main* thread.
A ``ThreadingHTTPServer`` dispatches each request on a worker thread; commands
are passed to the main thread via a ``queue.Queue`` and the result is sent back
on a per-request ``queue.Queue``, keeping all Playwright calls on one thread
(required by the sync API).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCREENSHOTS_DIR = Path(os.environ.get("SCREENSHOTS_DIR", "/screenshots"))
CLAW_PORT = int(os.environ.get("CLAW_PORT", "8080"))
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [browser-claw] [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thread-safe command bus
# ---------------------------------------------------------------------------

# Each item is (cmd_dict, result_queue).  The Playwright main loop drains this.
_cmd_bus: queue.Queue[tuple[dict, queue.Queue]] = queue.Queue()

# Set to True by the /command handler when cmd == "close".
_shutdown_event = threading.Event()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _handle_navigate(page: Page, cmd: dict) -> dict:
    url = cmd.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "'url' is required"}
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    return {"ok": True, "url": page.url}


def _handle_screenshot(page: Page, cmd: dict) -> dict:
    filename = (cmd.get("filename") or f"screenshot_{int(time.time() * 1000)}.png")
    filename = Path(filename).name  # strip any directory component
    if not filename.lower().endswith(".png"):
        filename += ".png"
    dest = SCREENSHOTS_DIR / filename
    page.screenshot(path=str(dest), full_page=bool(cmd.get("full_page", False)))
    return {"ok": True, "path": str(dest), "filename": filename}


def _handle_click(page: Page, cmd: dict) -> dict:
    selector = cmd.get("selector", "")
    if not selector:
        return {"ok": False, "error": "'selector' is required"}
    page.click(selector, timeout=10_000)
    return {"ok": True}


def _handle_type(page: Page, cmd: dict) -> dict:
    selector = cmd.get("selector", "")
    if not selector:
        return {"ok": False, "error": "'selector' is required"}
    page.fill(selector, cmd.get("text", ""), timeout=10_000)
    return {"ok": True}


def _handle_scroll(page: Page, cmd: dict) -> dict:
    x = int(cmd.get("x", 0))
    y = int(cmd.get("y", 0))
    page.evaluate(f"window.scrollBy({x}, {y})")
    return {"ok": True}


def _handle_evaluate(page: Page, cmd: dict) -> dict:
    js = cmd.get("js", "")
    if not js:
        return {"ok": False, "error": "'js' is required"}
    result = page.evaluate(js)
    return {"ok": True, "result": result}


def _handle_wait_for_selector(page: Page, cmd: dict) -> dict:
    selector = cmd.get("selector", "")
    if not selector:
        return {"ok": False, "error": "'selector' is required"}
    timeout = int(cmd.get("timeout", 10_000))
    page.wait_for_selector(selector, timeout=timeout)
    return {"ok": True}


def _handle_get_text(page: Page, cmd: dict) -> dict:
    selector = cmd.get("selector", "")
    if not selector:
        return {"ok": False, "error": "'selector' is required"}
    element = page.query_selector(selector)
    if element is None:
        return {"ok": False, "error": f"Selector not found: {selector!r}"}
    return {"ok": True, "text": element.inner_text()}


def _handle_get_url(page: Page, _cmd: dict) -> dict:
    return {"ok": True, "url": page.url}


def _handle_list_screenshots(_page: Page, _cmd: dict) -> dict:
    files = sorted(SCREENSHOTS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    return {"ok": True, "screenshots": [f.name for f in files]}


_HANDLERS: dict[str, object] = {
    "navigate": _handle_navigate,
    "screenshot": _handle_screenshot,
    "click": _handle_click,
    "type": _handle_type,
    "scroll": _handle_scroll,
    "evaluate": _handle_evaluate,
    "wait_for_selector": _handle_wait_for_selector,
    "get_text": _handle_get_text,
    "get_url": _handle_get_url,
    "list_screenshots": _handle_list_screenshots,
}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a dedicated thread."""

    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "status": "ready", "port": CLAW_PORT})
        else:
            self._send(404, {"ok": False, "error": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/command":
            self._send(404, {"ok": False, "error": "Not Found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            cmd = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send(400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return

        if not isinstance(cmd, dict):
            self._send(400, {"ok": False, "error": "Command must be a JSON object"})
            return

        result_q: queue.Queue = queue.Queue(maxsize=1)
        _cmd_bus.put((cmd, result_q))
        try:
            result = result_q.get(timeout=60)
        except queue.Empty:
            result = {"ok": False, "error": "Command timed out after 60 s"}

        self._send(200, result)

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # silence access log
        logger.debug("HTTP " + fmt, *args)


# ---------------------------------------------------------------------------
# Playwright main loop (runs on the main thread)
# ---------------------------------------------------------------------------


def _playwright_loop(page: Page) -> None:
    """Drain *_cmd_bus* and execute commands against *page* until 'close'."""
    logger.info("Browser Claw ready. Awaiting commands on HTTP :%d/command …", CLAW_PORT)

    while not _shutdown_event.is_set():
        try:
            cmd, result_q = _cmd_bus.get(timeout=0.5)
        except queue.Empty:
            continue

        cmd_name = cmd.get("cmd", "")

        if cmd_name == "close":
            result_q.put({"ok": True, "closed": True})
            _shutdown_event.set()
            break

        handler = _HANDLERS.get(cmd_name)
        if handler is None:
            result_q.put({
                "ok": False,
                "error": f"Unknown command: {cmd_name!r}",
                "available": sorted(_HANDLERS) + ["close"],
            })
            continue

        try:
            response = handler(page, cmd)  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Command %r raised an exception", cmd_name)
            response = {"ok": False, "error": str(exc)}

        result_q.put(response)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------


def run() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Screenshots directory: %s", SCREENSHOTS_DIR)

    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,800",
            ],
        )
        context: BrowserContext = browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page: Page = context.new_page()

        # Start the HTTP server on a background thread.
        server = _ThreadingHTTPServer(("0.0.0.0", CLAW_PORT), _Handler)  # noqa: S104
        server_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="browser-claw-http",
        )
        server_thread.start()
        logger.info("HTTP command server listening on 0.0.0.0:%d", CLAW_PORT)

        try:
            _playwright_loop(page)
        finally:
            server.shutdown()
            context.close()
            browser.close()

    logger.info("Browser Claw exiting.")


if __name__ == "__main__":
    run()
