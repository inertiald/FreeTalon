"""HTTP client for the inertiald.com content pipeline internal API.

The site side (src/app/api/internal/content) is gated by a shared
bearer token (CONTENT_PIPELINE_TOKEN), not the admin session: this
client is a machine client. Every failure mode raises
ContentQueueError so the generator can fail one entry back to
'received' and keep moving.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# transport(method, url, headers, body) -> (status, response body bytes)
Transport = Callable[[str, str, dict[str, str], "bytes | None"], "tuple[int, bytes]"]


class ContentQueueError(RuntimeError):
    """Raised when the content queue API cannot serve a request."""


def _urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # An HTTP status is still an answer: hand it to the caller,
        # which turns it into a ContentQueueError with the server's
        # own message when there is one.
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ContentQueueError(f"content queue unreachable: {exc.reason}") from exc


class ContentQueueClient:
    """Thin wrapper over the internal content API's operations."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: Transport | None = None,
        timeout_seconds: float = 60.0,
        video_timeout_seconds: float = 300.0,
    ) -> None:
        if not token:
            raise ContentQueueError("a pipeline token is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        if transport is None:

            def transport(method, url, headers, body):  # noqa: F811
                return _urllib_transport(method, url, headers, body, timeout_seconds)

            def video_transport(method, url, headers, body):
                return _urllib_transport(
                    method, url, headers, body, video_timeout_seconds
                )

            # The video upload gets its own, longer leash (a video
            # is tens of MB over the tailnet); every other call
            # keeps the standard timeout.
            self._video_transport = video_transport
        else:
            self._video_transport = transport
        self._transport = transport

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        status, raw = self._transport(method, f"{self._base_url}{path}", headers, body)
        if status == 401:
            raise ContentQueueError("content queue rejected the pipeline token (401)")
        if not 200 <= status < 300:
            detail = ""
            try:
                detail = str(json.loads(raw.decode("utf-8")).get("error", ""))
            except (ValueError, UnicodeDecodeError, AttributeError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise ContentQueueError(f"content queue {method} {path} -> {status}{suffix}")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContentQueueError(f"content queue returned non-JSON for {path}") from exc
        if not isinstance(decoded, dict):
            raise ContentQueueError(f"content queue returned a non-object for {path}")
        return decoded

    def list_received(self) -> list[dict[str, Any]]:
        """Entries waiting at 'received', oldest first."""
        data = self._request("GET", "/api/internal/content")
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise ContentQueueError("content queue list response has no entries list")
        return entries

    def claim(self, entry_id: str) -> dict[str, Any]:
        """Move an entry received -> processing."""
        return self._request(
            "PATCH", f"/api/internal/content/{entry_id}", {"action": "claim"}
        )

    def fail(self, entry_id: str, error: str) -> dict[str, Any]:
        """Return a claimed entry to 'received' with an error note."""
        return self._request(
            "PATCH",
            f"/api/internal/content/{entry_id}",
            {"action": "fail", "error": error},
        )

    def submit_drafts(self, entry_id: str, drafts: dict[str, Any]) -> dict[str, Any]:
        """Attach drafts to a claimed entry, landing it at drafts-ready."""
        return self._request(
            "PATCH",
            f"/api/internal/content/{entry_id}",
            {"action": "submit", "drafts": drafts},
        )

    def _put_bytes(
        self,
        transport: Transport,
        path: str,
        content_type: str,
        data: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One raw-body PUT with bearer auth (treated media, video):
        the response discipline is shared so the two uploads can
        never drift apart."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": content_type,
        }
        if extra_headers:
            headers.update(extra_headers)
        status, raw = transport("PUT", f"{self._base_url}{path}", headers, data)
        if status == 401:
            raise ContentQueueError("content queue rejected the pipeline token (401)")
        if not 200 <= status < 300:
            detail = ""
            try:
                detail = str(json.loads(raw.decode("utf-8")).get("error", ""))
            except (ValueError, UnicodeDecodeError, AttributeError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise ContentQueueError(f"content queue PUT {path} -> {status}{suffix}")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContentQueueError(
                f"content queue returned non-JSON for {path}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ContentQueueError(f"content queue returned a non-object for {path}")
        return decoded

    def submit_treated_media(
        self, entry_id: str, look: str, filename: str, data: bytes
    ) -> dict[str, Any]:
        """Upload one treated version of an entry photo (slice 1d):
        the PLATE standard (look 'standard') or one alternate look.
        The entry must be claimed ('processing'); the site stores the
        bytes under the entry's treated/ or alternates/<look>/ folder
        and records them on the media item."""
        path = (
            f"/api/internal/content/{entry_id}/treated/"
            f"{urllib.parse.quote(look, safe='')}/"
            f"{urllib.parse.quote(filename, safe='')}"
        )
        return self._put_bytes(self._transport, path, "image/jpeg", data)

    def submit_video(
        self,
        entry_id: str,
        filename: str,
        data: bytes,
        *,
        duration_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        """Upload the entry's generated video (YouTube V1). The
        entry must be claimed ('processing'); the site stores the
        bytes under the entry's video/ folder and records them on
        the entry. Render metadata rides as headers so the site
        can record it without probing the file itself. Sent over
        the video transport: the longer timeout scoped to this
        call only."""
        path = (
            f"/api/internal/content/{entry_id}/video/"
            f"{urllib.parse.quote(filename, safe='')}"
        )
        extra: dict[str, str] = {}
        if duration_seconds is not None:
            extra["X-Video-Duration-Seconds"] = f"{duration_seconds:.2f}"
        if width is not None:
            extra["X-Video-Width"] = str(width)
        if height is not None:
            extra["X-Video-Height"] = str(height)
        return self._put_bytes(
            self._video_transport, path, "video/mp4", data, extra
        )

    def fetch_media(self, entry_id: str, filename: str) -> bytes:
        """Raw bytes of one media file stored on an entry."""
        path = (
            f"/api/internal/content/{entry_id}/media/"
            f"{urllib.parse.quote(filename, safe='')}"
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        status, raw = self._transport("GET", f"{self._base_url}{path}", headers, None)
        if status != 200:
            raise ContentQueueError(
                f"content queue media fetch -> {status} for {filename}"
            )
        return raw
