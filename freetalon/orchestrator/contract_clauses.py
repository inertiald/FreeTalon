"""Contract clauses: a frozen requirement list, bound to plans.

Campaign build #3 (2026-11-23). The frontier panel (Gemini /
GPT-6 Luna / ChatGPT, unanimous) and ten exam arms agree on the
missing object: the loop must not control the evidence for its
own PASS. A goal-named contract (e.g. freetalon/pairing/
CONTRACT.md) sat in the prompts verbatim for ten arms while the
same three spec corners were missed every time — presence is not
enforcement. This module compiles the contract into obligations:

1. EXTRACTION — one clause per ``## `` section of the goal-named
   spec, with a stable ID (``C<nn>-<slug>``). Deterministic for a
   fixed contract text: same input, same list, byte for byte. The
   list is stamped into plan metadata by the planner under
   ``contract_clauses`` (system-owned, like ``module_contract``);
   the loop's models cannot edit it.
2. MAPPING — the planner declares, per node, ``inputs["clauses"]
   = {"implements": [...], "verifies": [...]}``. Validation
   (planner, gated like build #2's authorship rules) requires
   every clause to be implemented by at least one node and
   verified by at least one node, with no invented IDs.
3. PROBES — deterministic checks run by the loop after each
   version, outside the model's self-written tests: declared
   files exist; a contract requiring defined behavior for
   non-GET/POST methods (it pins ``405``) has a handler surface
   that provides it. Build #6 adds LIVE probes: a contract
   family's registered runner may exercise the produced code
   for real (the pairing runner boots the shipped server in a
   subprocess and runs its pinned HTTP flows) under a hard
   deadline, recording outcomes in the same record shape — the
   static probes cannot see a server that hangs or answers 400.
4. STATES — per clause: verified / failed / unverified / waived,
   computed from the coverage declarations, node outcomes, and
   probe results. The judge's re-plan brief carries the
   unverified ones; the loop's verdict gate refuses a PASS while
   any clause is neither verified nor explicitly waived.

Stdlib only. Everything here is a pure function of its inputs —
the extraction, the probes, and the state computation never call
a model and never write.
"""

from __future__ import annotations

import ast
import http.client
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import PlanNode
    from .repo_index import SpecDocument

CLAUSES_METADATA_KEY = "contract_clauses"
PROBE_RESULTS_METADATA_KEY = "contract_probe_results"
WAIVERS_METADATA_KEY = "contract_clause_waivers"

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PY_FILE_RE = re.compile(r"[A-Za-z0-9_./-]+\.py\b")
_PYTHON_M_RE = re.compile(r"python\s+-m\s+([A-Za-z_][A-Za-z0-9_.]*)")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# HTTP methods beyond GET/POST whose handler (or an equivalent
# dispatch) proves the method surface is deliberate, not the
# BaseHTTPRequestHandler 501 default.
_EXTRA_METHOD_HANDLERS = ("do_PUT", "do_DELETE", "do_PATCH", "do_HEAD", "do_OPTIONS")


def _slugify(heading: str) -> str:
    slug = _SLUG_STRIP_RE.sub("-", heading.lower()).strip("-")
    return slug[:48].strip("-") or "section"


def _declared_files(section_text: str, spec_path: str) -> list[str]:
    """The .py files a contract section declares, repo-relative.

    Sources: backtick spans naming a ``.py`` path (a token with a
    "/" is repo-relative as written; a bare filename resolves
    against the spec's directory), and the entry-point rule — a
    ``python -m <dotted.package>`` invocation declares the
    package's ``__main__.py`` (the file the invocation requires,
    whether or not the contract names it literally).
    """
    spec_dir = PurePosixPath(spec_path).parent.as_posix()
    found: list[str] = []
    for span in _BACKTICK_RE.findall(section_text):
        for token in _PY_FILE_RE.findall(span):
            token = token.lstrip("./") if token.startswith("./") else token
            if "/" in token:
                candidate = token
            elif spec_dir in ("", "."):
                candidate = token
            else:
                candidate = f"{spec_dir}/{token}"
            if candidate not in found:
                found.append(candidate)
    for dotted in _PYTHON_M_RE.findall(section_text):
        candidate = dotted.replace(".", "/") + "/__main__.py"
        if candidate not in found:
            found.append(candidate)
    return found


def extract_clauses(spec: "SpecDocument", *, id_prefix: str = "") -> list[dict[str, Any]]:
    """The frozen clause list for one goal-named spec document.

    One clause per ``## `` section, in document order. The
    preamble before the first section heading is framing, not an
    obligation list, and yields no clause. Deterministic: the
    same spec text always yields the same records.
    """
    sections: list[tuple[str, list[str]]] = []
    heading: str | None = None
    body: list[str] = []
    for line in spec.text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if heading is not None:
                sections.append((heading, body))
            heading = match.group(1)
            body = [line]
        elif heading is not None:
            body.append(line)
    if heading is not None:
        sections.append((heading, body))
    clauses: list[dict[str, Any]] = []
    for position, (section_heading, lines) in enumerate(sections, start=1):
        text = "\n".join(lines).strip()
        clause_id = f"{id_prefix}C{position:02d}-{_slugify(section_heading)}"
        clauses.append(
            {
                "id": clause_id,
                "spec": spec.path,
                "heading": section_heading,
                "text": text,
                "declared_files": _declared_files(text, spec.path),
                "method_surface": "405" in text,
            }
        )
    return clauses


def clauses_for_specs(specs: list["SpecDocument"]) -> list[dict[str, Any]]:
    """The combined frozen list for every goal-named spec.

    With a single spec (the common case) IDs are bare. With
    several, each spec's IDs are prefixed with its file stem so
    IDs stay unique across documents.
    """
    clauses: list[dict[str, Any]] = []
    multi = len(specs) > 1
    for spec in specs:
        prefix = ""
        if multi:
            stem = PurePosixPath(spec.path).stem
            prefix = f"{_slugify(stem)}-"
        clauses.extend(extract_clauses(spec, id_prefix=prefix))
    return clauses


def clauses_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The frozen clause list a plan's metadata carries, or [].

    Defensive: metadata is caller input on hand-built plans, so a
    malformed value means no clauses, never a crash.
    """
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get(CLAUSES_METADATA_KEY)
    if not isinstance(raw, list):
        return []
    clauses = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            clauses.append(item)
    return clauses


# ── Coverage mapping ────────────────────────────────────────────────


def node_clause_declaration(node: "PlanNode") -> tuple[list[str], list[str], str | None]:
    """One node's (implements, verifies, error) clause declaration.

    The declaration rides ``inputs["clauses"]`` (the build #2
    "edits" precedent: planner-declared structured data in node
    inputs). Absent is a valid empty declaration; a present but
    malformed value is an error the author must fix.
    """
    inputs = node.inputs or {}
    raw = inputs.get("clauses")
    if raw is None:
        return [], [], None
    if not isinstance(raw, dict):
        return [], [], (
            f"node '{node.id}': inputs.clauses must be an object "
            '{"implements": [clause ids], "verifies": [clause ids]}'
        )
    parsed: dict[str, list[str]] = {}
    for role in ("implements", "verifies"):
        value = raw.get(role) or []
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            return [], [], (
                f"node '{node.id}': inputs.clauses.{role} must be an "
                "array of clause id strings"
            )
        parsed[role] = list(dict.fromkeys(value))
    return parsed["implements"], parsed["verifies"], None


def coverage_map(nodes: list["PlanNode"]) -> dict[str, dict[str, list[str]]]:
    """clause id -> {"implement": [node ids], "verify": [node ids]}."""
    coverage: dict[str, dict[str, list[str]]] = {}
    for node in nodes:
        implements, verifies, _error = node_clause_declaration(node)
        for clause_id in implements:
            coverage.setdefault(clause_id, {"implement": [], "verify": []})[
                "implement"
            ].append(node.id)
        for clause_id in verifies:
            coverage.setdefault(clause_id, {"implement": [], "verify": []})[
                "verify"
            ].append(node.id)
    return coverage


def clause_coverage_errors(
    nodes: list["PlanNode"], clauses: list[dict[str, Any]]
) -> list[str]:
    """Plan errors for the clause-mapping rule (campaign build #3).

    Every frozen clause must be implemented by at least one node
    and verified by at least one (other or same) node; invented
    clause IDs and malformed declarations are errors. Runs only
    under build #2's authored gating in the planner's collector.
    """
    errors: list[str] = []
    known = {str(clause["id"]) for clause in clauses}
    for node in nodes:
        implements, verifies, error = node_clause_declaration(node)
        if error is not None:
            errors.append(error)
            continue
        for clause_id in implements + verifies:
            if clause_id not in known:
                errors.append(
                    f"node '{node.id}' declares contract clause "
                    f"{clause_id!r}, which is not in the frozen clause "
                    f"list ({', '.join(sorted(known))}) — clause ids "
                    "are system-extracted from the contract and cannot "
                    "be invented"
                )
    coverage = coverage_map(nodes)
    for clause in clauses:
        clause_id = str(clause["id"])
        entry = coverage.get(clause_id) or {"implement": [], "verify": []}
        if not entry["implement"]:
            errors.append(
                f"contract clause {clause_id} ({clause.get('heading')}) "
                "has no implementation node — declare it in the "
                '"implements" list of the node that implements it '
                '(inputs.clauses)'
            )
        if not entry["verify"]:
            errors.append(
                f"contract clause {clause_id} ({clause.get('heading')}) "
                "has no verification node — declare it in the "
                '"verifies" list of the node whose tests or checks '
                "prove it (inputs.clauses)"
            )
    return errors


def format_clauses_block(clauses: list[dict[str, Any]]) -> str:
    """The CONTRACT CLAUSES prompt block: the frozen list + the
    declaration rule, rendered for planner and re-plan prompts."""
    lines = [
        "CONTRACT CLAUSES — the goal-named contract, compiled by the "
        "system into a frozen requirement list. You cannot add, "
        "remove, or rename clauses. EVERY clause below must be "
        "implemented by at least one node and verified by at least "
        "one node: declare coverage on each node with "
        'inputs.clauses = {"implements": ["<clause id>", ...], '
        '"verifies": ["<clause id>", ...]}. A plan that leaves a '
        "clause unimplemented or unverified is rejected, and a run "
        "cannot PASS while a clause is unverified — the system's "
        "own probes check the mechanically checkable clauses "
        "(declared files exist; the HTTP method surface) outside "
        "your tests.",
    ]
    for clause in clauses:
        lines.append(f"- {clause['id']}: {clause.get('heading')}")
    return "\n".join(lines)


# ── Deterministic probes ────────────────────────────────────────────


def _handler_surface_ok(root: Path, spec_path: str) -> tuple[bool, str]:
    """True when the spec package's handler surface is deliberate.

    Scans the spec's directory for classes subclassing a
    *RequestHandler base. The surface is deliberate when some
    handler defines a method handler beyond do_GET/do_POST, a
    __getattr__ fallback, or a handle_one_request override — any
    of these means non-GET/POST methods get defined behavior
    instead of the stdlib's default 501.
    """
    spec_dir = root / PurePosixPath(spec_path).parent.as_posix()
    if not spec_dir.is_dir():
        return False, f"package directory {spec_dir} does not exist"
    handler_found = False
    for path in sorted(spec_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = set()
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.add(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.add(base.attr)
            if not any(name.endswith("RequestHandler") for name in base_names):
                continue
            handler_found = True
            methods = {
                stmt.name
                for stmt in node.body
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if (
                methods & set(_EXTRA_METHOD_HANDLERS)
                or "__getattr__" in methods
                or "handle_one_request" in methods
            ):
                return True, (
                    f"{path.name}:{node.name} defines method handling "
                    "beyond do_GET/do_POST"
                )
    if handler_found:
        return False, (
            "request handler class(es) define only do_GET/do_POST — "
            "every other method falls through to the stdlib 501 "
            "default the contract forbids"
        )
    return False, "no request handler class found in the package"


def run_contract_probes(
    clauses: list[dict[str, Any]], repo_root: str | Path
) -> list[dict[str, Any]]:
    """Run every clause's deterministic probes against the tree.

    One record per (clause, probe): {clause_id, probe, ok, detail}.
    A clause with no mechanically checkable content produces no
    records — its verification rides its verification node alone.
    Live-probe runners (below) append their records after the
    static ones; a runner that does not recognize the clause set
    returns nothing.
    """
    root = Path(repo_root)
    results: list[dict[str, Any]] = []
    for clause in clauses:
        clause_id = str(clause["id"])
        declared = clause.get("declared_files") or []
        if declared:
            missing = [
                name for name in declared if not (root / str(name)).is_file()
            ]
            results.append(
                {
                    "clause_id": clause_id,
                    "probe": "declared_files",
                    "ok": not missing,
                    "detail": (
                        "declared files present: " + ", ".join(map(str, declared))
                        if not missing
                        else "declared files missing: " + ", ".join(missing)
                    ),
                }
            )
        if clause.get("method_surface"):
            ok, detail = _handler_surface_ok(root, str(clause.get("spec") or ""))
            results.append(
                {
                    "clause_id": clause_id,
                    "probe": "method_surface",
                    "ok": ok,
                    "detail": detail,
                }
            )
    for runner in _LIVE_PROBE_RUNNERS:
        results.extend(runner(clauses, root))
    return results


def probe_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in results if not record.get("ok")]


# ── Live probes (campaign build #6) ────────────────────────────────
#
# The static probes read the tree; they cannot see behavior that
# exists only at runtime. Exam arm #15 shipped a pairing server
# whose register flow answered valid POSTs with 400 and whose
# confirm/poll flows hung — invisible to declared_files and
# method_surface, fatal to the plan's own test node, which timed
# out twice and ended the run. A live probe exercises the produced
# code for real and records per-flow outcomes in the same record
# shape, so a live failure forces re-plans through exactly the
# channel a static failure uses.
#
# Live probes are registered runners: each takes (clauses, root)
# and returns records, or [] when the clause set is not its
# contract family. A runner is family-specific by nature — it must
# know how to boot the family's server and which flows its
# contract pins — so the pairing runner below is the first entry,
# and a new contract family adds a runner here; the loop and the
# verdict gate need no changes.

# Hard bounds. The overall deadline is the probe's contract with
# the loop: a probe run completes or is killed within it, and a
# probe-internal timeout is a probe FAILURE record, never a loop
# stall.
_SMOKE_OVERALL_TIMEOUT_S = 20.0
_SMOKE_BOOT_TIMEOUT_S = 5.0
_SMOKE_REQUEST_TIMEOUT_S = 3.0

# The probe's fixed identity: same tree, same requests, same
# outcome. The code matches the contract's ^[A-Z]{2,}-\d{4}$ rule
# and the nonce its 32-hex rule.
_SMOKE_CODE = "SMOKE-0001"
_SMOKE_NONCE = "0123456789abcdef" * 2
_SMOKE_MACHINE_ID = "smoke-probe-machine"
_SMOKE_TOKEN = "<redacted>"
_SMOKE_CREDENTIALS = {"smoke_probe": "bundle"}

_PAIRING_SERVER_PATH = "freetalon/pairing/server.py"
_PAIRING_MAIN_PATH = "freetalon/pairing/__main__.py"

# The boot program: the contract's own construction surface
# (server.py's make_server over store.py's PairingStore), bound to
# an ephemeral port whose number is the program's first stdout
# line. Booting through make_server exercises the construction
# clause for real and learns the bound port with no race and no
# fixed port; the entry-point module itself (python -m
# freetalon.pairing) stays covered by the declared_files probe.
_SMOKE_BOOTSTRAP = """\
import os

from freetalon.pairing.server import make_server
from freetalon.pairing.store import PairingStore

store = PairingStore(os.environ["FREETALON_PAIRING_STORE_PATH"])
server = make_server(
    store,
    os.environ.get("FREETALON_PAIRING_TOKEN"),
    host="127.0.0.1",
    port=0,
)
print(f"SMOKE_PORT {server.server_address[1]}", flush=True)
server.serve_forever()
"""


def _smoke_http(
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float,
) -> tuple[int, bytes]:
    """One HTTP request against the probe server (loopback only).

    Raises OSError / http.client.HTTPException on transport
    failure, timeouts included — the caller records those as flow
    failures, never as probe crashes.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers: dict[str, str] = {}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _pairing_smoke_flows(
    port: int, deadline: float
) -> list[tuple[str, bool, str]]:
    """The pairing contract's core flows, exercised in order.

    Each entry: (flow, ok, detail). The expectations are the
    contract's pinned ones (CONTRACT.md, HTTP API + poll table):
    a valid fresh registration answers a success status (201);
    a pending code polls 404; confirm with the operator token
    answers 200; the claiming poll answers 200 with the bundle;
    a wrong method on a known path answers the contract's 405 —
    never the stdlib's 501 default. A flow that cannot be
    attempted inside the overall deadline fails as unattempted.
    """

    def request(name, method, path, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, (
                f"{name}: not attempted — the probe's overall "
                f"{_SMOKE_OVERALL_TIMEOUT_S:.0f}s deadline was spent "
                "by earlier flows"
            )
        timeout = min(_SMOKE_REQUEST_TIMEOUT_S, remaining)
        try:
            outcome = _smoke_http(port, method, path, timeout=timeout, **kwargs)
        except TimeoutError:
            return None, (
                f"{name}: {method} {path} timed out after "
                f"{timeout:.1f}s — the server did not answer "
                "(a hang, not an error response)"
            )
        except (OSError, http.client.HTTPException) as exc:
            return None, f"{name}: {method} {path} failed at transport: {exc}"
        return outcome, None

    flows: list[tuple[str, bool, str]] = []

    outcome, error = request(
        "register",
        "POST",
        "/pairing/register",
        body={
            "code": _SMOKE_CODE,
            "nonce": _SMOKE_NONCE,
            "machine_id": _SMOKE_MACHINE_ID,
            "hostname": "smoke-probe",
            "mac": "02:00:00:00:00:01",
        },
    )
    if error is not None:
        flows.append(("register", False, error))
    else:
        status, _raw = outcome
        flows.append(
            (
                "register",
                status in (200, 201),
                f"register: POST /pairing/register -> {status} "
                "(expected a success status — 201 for a fresh code; "
                "a 400/5xx means the server rejects or breaks on a "
                "valid registration)",
            )
        )

    outcome, error = request("poll_pending", "GET", f"/pairing/{_SMOKE_CODE}")
    if error is not None:
        flows.append(("poll_pending", False, error))
    else:
        status, _raw = outcome
        flows.append(
            (
                "poll_pending",
                status == 404,
                f"poll_pending: GET /pairing/{_SMOKE_CODE} -> {status} "
                "(expected 404 — the contract's pending poll)",
            )
        )

    outcome, error = request(
        "confirm",
        "POST",
        f"/pairing/{_SMOKE_CODE}/confirm",
        body={"credentials": _SMOKE_CREDENTIALS},
        token=_SMOKE_TOKEN,
    )
    if error is not None:
        flows.append(("confirm", False, error))
    else:
        status, _raw = outcome
        flows.append(
            (
                "confirm",
                status == 200,
                f"confirm: POST /pairing/{_SMOKE_CODE}/confirm -> "
                f"{status} (expected 200 with the operator token)",
            )
        )

    outcome, error = request("poll_claim", "GET", f"/pairing/{_SMOKE_CODE}")
    if error is not None:
        flows.append(("poll_claim", False, error))
    else:
        status, raw = outcome
        bundle_present = False
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            bundle_present = payload.get("credentials") == _SMOKE_CREDENTIALS
        flows.append(
            (
                "poll_claim",
                status == 200 and bundle_present,
                f"poll_claim: GET /pairing/{_SMOKE_CODE} -> {status} "
                "(expected 200 with the confirmed credential bundle "
                "— the one-time claim)",
            )
        )

    outcome, error = request("wrong_method", "PUT", "/pairing/register")
    if error is not None:
        flows.append(("wrong_method", False, error))
    else:
        status, _raw = outcome
        flows.append(
            (
                "wrong_method",
                status == 405,
                f"wrong_method: PUT /pairing/register -> {status} "
                "(expected the contract's 405 for a known path with "
                "the wrong method"
                + (
                    " — 501 is the stdlib default the contract forbids"
                    if status == 501
                    else ""
                )
                + ")",
            )
        )
    return flows


def _boot_pairing_server(
    root: Path, work_dir: Path
) -> tuple[subprocess.Popen[bytes] | None, int | None, str]:
    """Boot the shipped pairing server in a subprocess.

    Returns (process, port, detail): on success the process is
    serving on the port and the caller owns killing it; on
    failure the process may still be returned (hung at boot) and
    the caller kills it the same way. Configuration rides the
    contract's own env surface, pointed at a throwaway store in
    the probe's temp dir; the only network the child can see is
    the loopback it binds.
    """
    stderr_path = work_dir / "smoke-server.stderr"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["FREETALON_PAIRING_STORE_PATH"] = str(work_dir / "smoke-store.json")
    env["FREETALON_PAIRING_TOKEN"] = _SMOKE_TOKEN
    with stderr_path.open("wb") as stderr_file:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, "-c", _SMOKE_BOOTSTRAP],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            start_new_session=True,
        )
    deadline = time.monotonic() + _SMOKE_BOOT_TIMEOUT_S
    port: int | None = None
    buffer = b""
    while time.monotonic() < deadline and port is None:
        if proc.poll() is not None:
            break
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if not ready:
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            break
        buffer += chunk
        for line in buffer.split(b"\n"):
            if line.startswith(b"SMOKE_PORT "):
                try:
                    port = int(line.split()[1])
                except (IndexError, ValueError):
                    port = None
    if port is not None:
        return proc, port, (
            "server booted via make_server on an ephemeral port "
            "and reported it ready"
        )
    tail = ""
    try:
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[
            -400:
        ].strip()
    except OSError:
        pass
    if proc.poll() is None:
        detail = (
            f"server did not report a port within "
            f"{_SMOKE_BOOT_TIMEOUT_S:.0f}s (boot hang)"
        )
    else:
        detail = f"server exited during boot (status {proc.returncode})"
    if tail:
        detail += f"; stderr tail: {tail}"
    return proc, None, detail


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the probe subprocess and its process group, always.

    The probe's safety rests on this running in a finally block:
    the child gets SIGKILL to its whole group (it was started in
    its own session), so no server a probe booted can outlive the
    probe — including one whose handler threads are hung.
    """
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass


def _pairing_http_smoke_records(
    clauses: list[dict[str, Any]], root: Path
) -> list[dict[str, Any]]:
    """Live HTTP smoke probe for the pairing contract (build #6).

    Scoping is the contract's own: the runner engages when a
    clause declares the pairing server module. The boot record
    rides the clause(s) declaring the package entry point
    (__main__.py — the server-construction clause); the flow
    records ride the method-surface clause(s) (the HTTP API
    clause), falling back to the clauses declaring the server
    module. When the server module itself is missing there are
    no records — the declared_files probe already owns that
    failure. The whole run is bounded by _SMOKE_OVERALL_TIMEOUT_S
    and the child is killed unconditionally.
    """
    if not any(
        _PAIRING_SERVER_PATH in (clause.get("declared_files") or [])
        for clause in clauses
    ):
        return []
    if not (root / _PAIRING_SERVER_PATH).is_file():
        return []
    flow_clauses = [c for c in clauses if c.get("method_surface")]
    if not flow_clauses:
        flow_clauses = [
            c
            for c in clauses
            if _PAIRING_SERVER_PATH in (c.get("declared_files") or [])
        ]
    boot_clauses = [
        c
        for c in clauses
        if _PAIRING_MAIN_PATH in (c.get("declared_files") or [])
    ] or flow_clauses
    if not flow_clauses and not boot_clauses:
        return []
    deadline = time.monotonic() + _SMOKE_OVERALL_TIMEOUT_S
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="freetalon-smoke-") as tmp:
        proc, port, boot_detail = _boot_pairing_server(root, Path(tmp))
        try:
            for clause in boot_clauses:
                records.append(
                    {
                        "clause_id": str(clause["id"]),
                        "probe": "http_smoke_boot",
                        "ok": port is not None,
                        "detail": boot_detail,
                    }
                )
            if port is None:
                flows = [
                    (
                        "flows",
                        False,
                        f"no flow could run: {boot_detail}",
                    )
                ]
            else:
                flows = _pairing_smoke_flows(port, deadline)
            for clause in flow_clauses:
                for _name, ok, detail in flows:
                    records.append(
                        {
                            "clause_id": str(clause["id"]),
                            "probe": "http_smoke",
                            "ok": ok,
                            "detail": detail,
                        }
                    )
        finally:
            if proc is not None:
                _kill_process_tree(proc)
    return records


# The registered live-probe runners, in run order. A new contract
# family appends its runner here (see the section header).
_LIVE_PROBE_RUNNERS = (_pairing_http_smoke_records,)


# ── Clause states ───────────────────────────────────────────────────


def clause_states(
    clauses: list[dict[str, Any]],
    nodes: list["PlanNode"],
    probe_results: list[dict[str, Any]],
    waivers: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-clause verification state for the brief and the verdict.

    verified — every probe bound to the clause passed, or (when
      the clause has no probe) a verification node completed with
      a non-FAIL measurement. A clause WITH probes is verified by
      its probes plus, when it also has verification nodes, at
      least one of them passing — a probe pass does not excuse a
      failed verification node.
    failed — a probe failed, or verification nodes exist and every
      one of them failed.
    waived — the waivers map carries a non-empty reason (an
      operator/system record; the loop never writes one).
    unverified — everything else: no coverage, or coverage whose
      verification has not (yet) passed.
    """
    from .models import PlanStatus

    waivers = waivers or {}
    coverage = coverage_map(nodes)
    by_id = {node.id: node for node in nodes}
    probes_by_clause: dict[str, list[dict[str, Any]]] = {}
    for record in probe_results:
        probes_by_clause.setdefault(str(record.get("clause_id")), []).append(record)

    def node_passed(node_id: str) -> bool:
        node = by_id.get(node_id)
        if node is None or node.status != PlanStatus.COMPLETED:
            return False
        measurement = node.measurement or {}
        return measurement.get("verdict") != "FAIL"

    def node_failed(node_id: str) -> bool:
        node = by_id.get(node_id)
        return node is not None and node.status == PlanStatus.FAILED

    states: dict[str, dict[str, Any]] = {}
    for clause in clauses:
        clause_id = str(clause["id"])
        reasons: list[str] = []
        waiver = waivers.get(clause_id)
        if isinstance(waiver, str) and waiver.strip():
            states[clause_id] = {"state": "waived", "reasons": [f"waived: {waiver}"]}
            continue
        entry = coverage.get(clause_id) or {"implement": [], "verify": []}
        if not entry["implement"]:
            reasons.append("no implementation node")
        if not entry["verify"]:
            reasons.append("no verification node")
        probes = probes_by_clause.get(clause_id) or []
        failed_probes = [p for p in probes if not p.get("ok")]
        for probe in failed_probes:
            reasons.append(f"probe {probe.get('probe')} failing: {probe.get('detail')}")
        verify_ids = entry["verify"]
        any_verify_passed = any(node_passed(nid) for nid in verify_ids)
        all_verify_failed = bool(verify_ids) and all(
            node_failed(nid) for nid in verify_ids
        )
        if failed_probes or all_verify_failed:
            if all_verify_failed:
                reasons.append("every verification node failed")
            state = "failed"
        elif probes and not failed_probes:
            if verify_ids and not any_verify_passed:
                reasons.append("verification node has not passed")
                state = "unverified"
            else:
                state = "verified"
        elif any_verify_passed:
            state = "verified"
        else:
            if verify_ids:
                reasons.append("verification node has not passed")
            state = "unverified"
        states[clause_id] = {"state": state, "reasons": reasons}
    return states


def uncovered_clauses(
    clauses: list[dict[str, Any]],
    nodes: list["PlanNode"],
    probe_results: list[dict[str, Any]],
    waivers: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The brief's uncovered-clause list: every clause not verified
    or waived, with the reasons it is still open."""
    states = clause_states(clauses, nodes, probe_results, waivers)
    headings = {str(c["id"]): c.get("heading") for c in clauses}
    return [
        {"id": clause_id, "heading": headings.get(clause_id), "reasons": info["reasons"]}
        for clause_id, info in states.items()
        if info["state"] in ("unverified", "failed")
    ]
