"""Fleet update channel — signed console-carried updates (DESIGN.md §13).

The pairing-join (section 6) deliberately leaves a node with no root
channel: the fleet SSH account is unprivileged and the console offers
no shell. This module is the server half of the channel that closes
that gap without weakening the posture:

  * The operator builds a bundle for one machine-id: a manifest
    (version, target files, SHA-256 per file) plus the file bytes,
    signed with the fleet Ed25519 key (the same keypair whose public
    half the pairing bundle installs as the node's SSH credential).
    Signing is OpenSSH's SSHSIG via ``ssh-keygen -Y sign`` under the
    namespace ``freetalon-update``; nodes verify with the public key
    at /etc/freetalon/fleet-pubkey. No new secret class: the private
    key never leaves seeker7 and is never printed or logged.
  * Nodes poll ``GET /fleet/updates/<machine_id>`` and verify the
    signature and every hash before touching anything; an unsigned or
    mismatched bundle is ignored. Apply/rollback outcomes come back
    as ``POST /fleet/report`` and land in the fleet state file, which
    is the dashboard's view of fleet versions.
  * Publishing is a CLI on the control side (this module's ``main``),
    writing bundle files into the fleet directory directly — there is
    no HTTP publish route, so the node's segment can only ever read.

The fleet directory layout (FREETALON_FLEET_DIR):

    bundles/<machine_id>.json   one published bundle per node (CLI-written)
    state.json                   node reports (server-written)

Both are plain files; the server reads bundles per request so a
publish takes effect without a restart.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

SIGN_NAMESPACE = "freetalon-update"
SIGN_PRINCIPAL = "freetalon-fleet"
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_REPORT_HISTORY = 20


class FleetError(Exception):
    """Base class for fleet update channel errors."""


class FleetStateCorrupt(FleetError):
    """The fleet state file exists but cannot be read. Never silently
    reset: the state is the fleet's version record."""


class InvalidBundle(FleetError, ValueError):
    """A bundle offered to the publisher failed validation."""


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """The exact bytes that get signed: one canonical JSON encoding,
    produced identically by the publisher and checked by the node."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sign_manifest(key_path: str | Path, canonical: bytes) -> str:
    """SSHSIG-armored signature over the canonical manifest bytes.

    Shells out to ssh-keygen (the fleet key is an OpenSSH Ed25519 key;
    the stdlib has no Ed25519). The key file is passed by path only —
    its contents are never read by this process, printed, or logged.
    """
    with tempfile.TemporaryDirectory() as tmp:
        msg = Path(tmp) / "manifest.json"
        msg.write_bytes(canonical)
        proc = subprocess.run(
            [
                "ssh-keygen", "-Y", "sign",
                "-f", str(key_path),
                "-n", SIGN_NAMESPACE,
                str(msg),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise FleetError(
                "ssh-keygen -Y sign failed: %s" % (proc.stderr.strip() or "unknown")
            )
        sig = Path(str(msg) + ".sig")
        if not sig.exists():
            raise FleetError("ssh-keygen -Y sign produced no signature file")
        return sig.read_text(encoding="utf-8")


def build_bundle(
    *,
    machine_id: str,
    version: str,
    files: list[tuple[str, str, Path]],
    key_path: str | Path,
    reboot_after_apply: bool = False,
    notes: str = "",
    console_version: str | None = None,
    now_fn: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Assemble + sign one bundle record (the shape the server serves).

    ``files`` is a list of (name, target_path, source_path): the name
    keys the bundle's file table, the target path is where the file
    lands on the node (the node re-checks it against its allowlist).
    """
    if not _MACHINE_ID_RE.fullmatch(machine_id or ""):
        raise InvalidBundle("machine_id must be 32 lowercase hex characters")
    if not _VERSION_RE.fullmatch(version or ""):
        raise InvalidBundle("version must be a short [A-Za-z0-9._-] token")
    entries: list[dict[str, Any]] = []
    blobs: dict[str, str] = {}
    for name, target, source in files:
        data = Path(source).read_bytes()
        entries.append(
            {
                "name": name,
                "path": target,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
        blobs[name] = base64.b64encode(data).decode("ascii")
    manifest: dict[str, Any] = {
        "version": version,
        "machine_id": machine_id,
        "created_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_fn())
        ),
        "reboot_after_apply": bool(reboot_after_apply),
        "files": entries,
        "notes": notes,
    }
    if console_version is not None:
        manifest["console_version"] = console_version
    canonical = canonical_manifest_bytes(manifest)
    return {
        "manifest": manifest,
        "manifest_canonical": canonical.decode("utf-8"),
        "signature": sign_manifest(key_path, canonical),
        "files": blobs,
    }


class FleetStore:
    """Bundle files + node report state under one fleet directory.

    Bundles are written by the publisher CLI and only read here, per
    request, so publishing never needs a server restart. Reports are
    written by the server process alone (this class), with the same
    atomic-save discipline as the pairing store.
    """

    def __init__(
        self,
        root: str | Path,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(root)
        self._bundles = self._root / "bundles"
        self._state_path = self._root / "state.json"
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._load_state()

    # -- persistence ---------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"nodes": {}}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FleetStateCorrupt(
                f"fleet state at {self._state_path} is unreadable: {exc}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), dict):
            raise FleetStateCorrupt(
                f"fleet state at {self._state_path} has an unexpected shape"
            )
        return raw

    def _save_state(self) -> None:
        text = json.dumps(self._state, indent=2, sort_keys=True)
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._state_path)
        dir_fd = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # -- bundles (read-only here; the publisher writes) -----------------

    def current_bundle(self, machine_id: str) -> dict[str, Any] | None:
        """The published bundle record for a node, or None. A bundle
        file that cannot be parsed is treated as absent (the publisher
        writes atomically, so this only happens on manual damage; the
        node would reject a malformed bundle anyway)."""
        if not _MACHINE_ID_RE.fullmatch(machine_id or ""):
            return None
        path = self._bundles / ("%s.json" % machine_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        manifest = raw.get("manifest")
        if not isinstance(manifest, dict):
            return None
        if manifest.get("machine_id") != machine_id:
            return None
        return raw

    def published_versions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._bundles.is_dir():
            for path in sorted(self._bundles.glob("*.json")):
                bundle = self.current_bundle(path.stem)
                if bundle is not None:
                    out[path.stem] = str(bundle["manifest"].get("version"))
        return out

    # -- reports ---------------------------------------------------------

    def record_report(self, machine_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Store one node report; returns the node's public view.

        The report is the node's own account of itself (console and
        bundle versions, last apply outcome). It is informational —
        the segment is sealed and pairing's node routes follow the
        same unauthenticated posture (CONTRACT.md)."""
        if not _MACHINE_ID_RE.fullmatch(machine_id or ""):
            raise InvalidBundle("machine_id must be 32 lowercase hex characters")
        entry = {
            "machine_id": machine_id,
            "hostname": report.get("hostname"),
            "mac": report.get("mac"),
            "console_version": report.get("console_version"),
            "bundle_version": report.get("bundle_version"),
            "last_result": report.get("last_result"),
            "last_detail": report.get("last_detail"),
            "reported_at": report.get("at"),
            "received_at": self._now_fn(),
        }
        with self._lock:
            node = self._state["nodes"].get(machine_id, {})
            history = list(node.get("history", []))
            history.append(entry)
            node.update(entry)
            node["history"] = history[-_MAX_REPORT_HISTORY:]
            self._state["nodes"][machine_id] = node
            self._save_state()
            return self._public_node(node)

    @staticmethod
    def _public_node(node: dict[str, Any]) -> dict[str, Any]:
        return {
            key: node.get(key)
            for key in (
                "machine_id", "hostname", "mac", "console_version",
                "bundle_version", "last_result", "last_detail",
                "reported_at", "received_at",
            )
        }

    def fleet_view(self) -> dict[str, Any]:
        with self._lock:
            nodes = [
                self._public_node(self._state["nodes"][mid])
                for mid in sorted(self._state["nodes"])
            ]
        return {"nodes": nodes, "published": self.published_versions()}


# ----------------------------------------------------------------------
# Publisher CLI (control side only): python -m freetalon.pairing.updates
# ----------------------------------------------------------------------

def _probe_console_version(source: Path) -> str | None:
    """Run a candidate console file's --version probe, sandboxed, and
    return what it reports. The node runs the identical probe against
    the staged file before swapping, and compares against the value
    recorded in the manifest."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["FREETALON_ETC"] = str(Path(tmp) / "etc")
        env["FREETALON_VAR"] = str(Path(tmp) / "var")
        env["FREETALON_APPLY"] = "0"
        proc = subprocess.run(
            [sys.executable, str(source), "--version"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None


def _publish(args: argparse.Namespace) -> int:
    files: list[tuple[str, str, Path]] = []
    console_version: str | None = None
    for spec in args.file:
        parts = spec.split("=", 2)
        if len(parts) != 3:
            raise InvalidBundle(
                "--file must be NAME=TARGET_PATH=SOURCE_PATH, got %r" % spec
            )
        name, target, source = parts
        files.append((name, target, Path(source)))
        if target.endswith("node_console.py"):
            console_version = _probe_console_version(Path(source))
            if console_version is None:
                raise InvalidBundle(
                    "console probe failed for %s (--version did not answer)"
                    % source
                )
    bundle = build_bundle(
        machine_id=args.machine_id,
        version=args.version,
        files=files,
        key_path=args.key,
        reboot_after_apply=args.reboot,
        notes=args.notes or "",
        console_version=console_version,
    )
    root = Path(args.fleet_dir)
    bundles = root / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    dest = bundles / ("%s.json" % args.machine_id)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    print(
        "published bundle %s for %s (%d files, reboot=%s) -> %s"
        % (args.version, args.machine_id, len(files), args.reboot, dest)
    )
    return 0


def _show(args: argparse.Namespace) -> int:
    store = FleetStore(args.fleet_dir)
    print(json.dumps(store.fleet_view(), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FreeTalon fleet update channel — publisher + state viewer"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("publish", help="build, sign and publish one bundle")
    pub.add_argument("--fleet-dir", required=True)
    pub.add_argument("--key", required=True, help="path to the fleet private key")
    pub.add_argument("--machine-id", required=True)
    pub.add_argument("--version", required=True)
    pub.add_argument("--reboot", action="store_true",
                     help="flag the bundle reboot-after-apply")
    pub.add_argument("--notes", default="")
    pub.add_argument(
        "--file", action="append", required=True,
        metavar="NAME=TARGET=SOURCE",
        help="one payload file; repeat per file",
    )
    pub.set_defaults(func=_publish)
    show = sub.add_parser("show", help="print published bundles + node reports")
    show.add_argument("--fleet-dir", required=True)
    show.set_defaults(func=_show)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FleetError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
