"""Small read-only index of the FreeTalon repo, for planner grounding.

The LLM planner cannot see the repo unless the prompt shows it to the
model, and an ungrounded planner invents files that already exist (the
failed /health goal scaffolded ``api/health.py`` when ``/health`` already
lived in ``freetalon/api.py``). This module builds a compact, deterministic
index from the source tree on disk — no LLM calls, pure stdlib — and
answers the two questions plan validation needs:

- does a route / module already exist (so a plan must extend it, not
  duplicate it)?
- what should the planner be told about the repo before it plans?

The index is built from the checkout that contains this package
(``FREETALON_REPO_ROOT`` overrides it), so it stays correct as the code
moves; tests may point it at a synthetic tree instead.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import textwrap
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

# Route literals in the API server look like ``path == "/health"`` or
# ``path.startswith("/tasks/")``. Both spellings are collected.
_ROUTE_EQ_RE = re.compile(r"""path\s*==\s*["'](/[^"']*)["']""")
_ROUTE_STARTSWITH_RE = re.compile(r"""path\.startswith\(\s*["'](/[^"']*)["']""")
_API_PORT_RE = re.compile(r"""port:\s*int\s*=\s*Field\(default=(\d+)""")

# Web frameworks, by the import name that identifies them. A module
# that imports one of these is built on that framework; a module that
# imports none of them has no framework label. Used to ground the
# planner: freetalon/api.py imports stdlib http.server, so a plan that
# writes a Flask app for the /health route is inventing a stack the
# repo does not use (retry #7 attempts 2-3).
_WEB_FRAMEWORKS = {
    "http.server": "stdlib http.server",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "django": "Django",
    "tornado": "Tornado",
    "aiohttp": "aiohttp",
    "starlette": "Starlette",
    "bottle": "Bottle",
    "sanic": "Sanic",
    "falcon": "Falcon",
}

_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE
)


def _imported_modules(source: str) -> set[str]:
    """Module names a source file imports (``import x`` / ``from x import``)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _framework_of_imports(names: set[str]) -> str | None:
    """The web framework a set of imported module names implies, if any."""
    candidates = set(names)
    for name in names:
        candidates.add(name.split(".")[0])
    for key, label in _WEB_FRAMEWORKS.items():
        if key in candidates:
            return label
    return None


# git-HEAD sources, cached per process: build_repo_index runs on every
# plan parse, and a git show per file would dominate the suite. HEAD
# does not change inside a process (commits happen between runs), so a
# process-level cache is safe; None (untracked / not a git checkout)
# is cached too.
_HEAD_SOURCE_CACHE: dict[tuple[str, str], str | None] = {}


def _head_source(root: Path, rel: str) -> str | None:
    """Source of ``rel`` at git HEAD, or None when unavailable.

    Fail-soft by design: not a git checkout, git missing, or the file
    untracked at HEAD all return None, and the caller falls back to
    the working-tree source — so synthetic test trees and non-git
    checkouts behave exactly as before.
    """
    key = (str(root), rel)
    if key in _HEAD_SOURCE_CACHE:
        return _HEAD_SOURCE_CACHE[key]
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        _HEAD_SOURCE_CACHE[key] = None
        return None
    result = proc.stdout if proc.returncode == 0 else None
    _HEAD_SOURCE_CACHE[key] = result
    return result


def framework_in_text(text: str) -> str | None:
    """The web framework a blob of Python source imports or uses, if any."""
    names: set[str] = set()
    for match in _IMPORT_LINE_RE.finditer(text):
        names.add(match.group(1) or match.group(2))
    found = _framework_of_imports(names)
    if found is not None:
        return found
    for label, patterns in _FRAMEWORK_USAGE:
        if any(pattern.search(text) for pattern in patterns):
            return label
    return None


# Framework usage markers, for blobs of source that use a framework
# without importing it: retry #18 attempt 2's heredoc wrote a Flask app
# (@app.route + jsonify) with no import statement, so the import-only
# check never saw it. A route decorator, a framework response helper,
# or an app-object construction identifies the framework the same way
# an import does. Ordered like _WEB_FRAMEWORKS; the first match wins.
# Deliberately narrow: a bare `app.get(...)` call is not a marker (it
# is also a test-client call), and @app.get/@app.post are Flask's route
# shortcuts as well as FastAPI's, so they are claimed by neither side —
# @router.get/@router.post is FastAPI-specific, and @*.route is Flask's.
_FRAMEWORK_USAGE: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "Flask",
        (
            re.compile(r"\bjsonify\s*\("),
            re.compile(r"\bFlask\s*\("),
            re.compile(r"@\w+\.route\s*\("),
            re.compile(r"\brender_template\s*\("),
        ),
    ),
    (
        "FastAPI",
        (
            re.compile(r"\bFastAPI\s*\("),
            re.compile(r"@router\.(?:get|post|put|delete|patch)\s*\("),
        ),
    ),
    (
        "aiohttp",
        (
            re.compile(r"\bweb\.Application\s*\("),
            re.compile(r"\bapp\.router\.add_(?:get|post|put|delete|patch)\s*\("),
        ),
    ),
    (
        "Starlette",
        (
            re.compile(r"\bStarlette\s*\("),
            re.compile(r"\bRoute\s*\(\s*[\"']/"),
        ),
    ),
)


# How much of the module inventory the prompt context carries. The index
# itself is complete; only the rendered text is truncated.
_MAX_CONTEXT_FILES = 14


@dataclass
class RepoIndex:
    """Deterministic snapshot of the repo's public surface."""

    root: Path
    # rel-path (posix) -> sorted top-level def/class names in that file.
    files: dict[str, list[str]] = field(default_factory=dict)
    # HTTP route ("/health", "/tasks/…") -> rel-path of the file serving it.
    routes: dict[str, str] = field(default_factory=dict)
    # Module stem ("api", "planner") -> sorted rel-paths defining it.
    modules: dict[str, list[str]] = field(default_factory=dict)
    # FreeTalon API port from freetalon/config.py (HiveConfig.port default).
    api_port: int = 8765
    # rel-path -> web framework the module is built on ("stdlib http.server",
    # "Flask", ...), only when the module imports one.
    frameworks: dict[str, str] = field(default_factory=dict)
    # HTTP route (same keys as ``routes``) -> the request handler that
    # already serves it, e.g. "the do_GET method of the Handler class
    # (defined inside HiveAPIServer.start)". Derived from the AST: the
    # route literal's enclosing do_* method. Lets the grounded move name
    # the real dispatch instead of guessing it (retry #13: the model
    # wrote two Flask scaffolds because "extend the module" read as
    # "write a new app"). Empty for a route with no recorded handler.
    route_handlers: dict[str, str] = field(default_factory=dict)
    # HTTP route -> source text of the existing ``if`` branch dispatching
    # on that route literal, read from the real module (e.g. "/health" ->
    # 'if path == "/health":\n    return self._send(200, ...)'). Lets the
    # grounded skeleton show the model the actual handler-branch shape
    # with the route literal and the response fields named, so it extends
    # the branch instead of writing a TODO comment (retry #17: the model
    # followed the skeleton shape but filled it with placeholder
    # comments). Empty for a route with no recorded branch.
    route_branches: dict[str, str] = field(default_factory=dict)
    # Repo documents: rel-path (posix) -> size in bytes, for every
    # *.md under the root outside .git/.venv/caches. The code walks
    # below never surfaced a document, so a goal naming a spec file
    # (freetalon/pairing/CONTRACT.md) grounded nothing — the planner
    # planned from the goal's prose alone (exam re-arms #3/#4,
    # 2026-11-23). goal_named_specs matches goal tokens against
    # this inventory.
    docs: dict[str, int] = field(default_factory=dict)

    def route_names(self) -> set[str]:
        """First path segment of each route ("health", "tasks", ...)."""
        names = set()
        for route in self.routes:
            segment = route.strip("/").split("/", 1)[0]
            if segment:
                names.add(segment)
        return names


def resolve_repo_root(explicit: str | Path | None = None) -> Path:
    """Repo root: explicit argument, env override, else the checkout holding this file."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env_root = os.environ.get("FREETALON_REPO_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    # This file is <root>/freetalon/orchestrator/repo_index.py.
    return Path(__file__).resolve().parent.parent.parent


# A request-handler method: do_GET, do_POST, ... on a class built on a
# request-handler base (http.server's BaseHTTPRequestHandler) or any class
# that defines one.
_HANDLER_METHOD_RE = re.compile(r"^do_(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)$")


def _handler_spans(source: str) -> list[tuple[int, int, str, str, str]]:
    """(start_line, end_line, class, method, enclosing) for handler methods.

    ``enclosing`` is the dotted path of the scopes around the class
    ("" for a top-level class, "HiveAPIServer.start" for a Handler
    defined inside HiveAPIServer.start). A class counts as a handler
    when a base class name ends in "RequestHandler" or it defines a
    do_* method.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int, str, str, str]] = []

    def _base_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def _visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                methods = [
                    item
                    for item in child.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and _HANDLER_METHOD_RE.match(item.name)
                ]
                is_handler = any(
                    _base_name(base).endswith("RequestHandler")
                    for base in child.bases
                ) or bool(methods)
                if is_handler:
                    enclosing = ".".join(scope)
                    for method in methods:
                        spans.append(
                            (
                                method.lineno,
                                method.end_lineno or method.lineno,
                                child.name,
                                method.name,
                                enclosing,
                            )
                        )
                _visit(child, scope + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _visit(child, scope + [child.name])
            else:
                _visit(child, scope)

    _visit(tree, [])
    return spans


def _handler_for_line(
    spans: list[tuple[int, int, str, str, str]], source: str, pos: int
) -> str | None:
    """The handler description for a route literal at byte offset ``pos``.

    The route's line must fall inside a handler method's span; otherwise
    None (the route is not served by a recorded handler method).
    """
    line = source.count("\n", 0, pos) + 1
    for start, end, class_name, method_name, enclosing in spans:
        if start <= line <= end:
            description = f"the {method_name} method of the {class_name} class"
            if enclosing:
                description += f" (defined inside {enclosing})"
            return description
    return None


def _route_branch(
    source: str, spans: list[tuple[int, int, str, str, str]], route: str
) -> str | None:
    """Source text of the ``if`` branch dispatching on ``route`` in a handler.

    Walks the AST for an ``ast.If`` whose test is ``<something> == <route>``
    inside one of the handler-method spans, and returns the branch source
    dedented (so it reads as a stand-alone snippet). Read from the real
    module, never guessed: for /health this is exactly
    ``if path == "/health":\n    return self._send(200, ...)``.
    Returns None when no such branch exists in a handler span.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == route
        ):
            continue
        if not any(start <= node.lineno <= end for start, end, _, _, _ in spans):
            continue
        segment = ast.get_source_segment(source, node)
        if segment is None:
            continue
        return textwrap.dedent(segment).strip()
    return None


def _top_level_names(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    return sorted(names)


def qualified_def_names(source: str) -> list[str]:
    """Dotted names of every def/class in a module, at any nesting depth.

    ``_top_level_names`` sees only the module surface; a wholesale
    overwrite can keep a top-level name while gutting everything under
    it (retry #26: the replacement freetalon/api.py kept a class named
    HiveAPIServer but dropped HiveAPIServer.start, the Handler class
    inside it, and every handler method). Qualified names make that
    loss visible to the planner's scaffold-overwrite guard.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []

    def _visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                qualified = prefix + child.name
                names.append(qualified)
                _visit(child, qualified + ".")

    _visit(tree, "")
    return names


def build_repo_index(repo_root: str | Path | None = None) -> RepoIndex:
    """Walk ``<root>/freetalon/**/*.py`` and index routes, modules, and the API port.

    Also inventories the repo's documents (*.md, rel-path -> size)
    in ``index.docs`` — the inventory goal_named_specs matches
    against and format_repo_context lists.
    """
    root = resolve_repo_root(repo_root)
    index = RepoIndex(root=root)
    index.docs = _scan_repo_docs(root)
    package = root / "freetalon"
    if not package.is_dir():
        return index
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.endswith(".bak"):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Retry #23: when the working-tree copy does not parse, fall
        # back to the committed file for the whole per-file analysis
        # (routes, handlers, branches, top-level names, framework).
        # The framework check gates on the goal naming a route in the
        # index, so a clobbered module that no longer contains any
        # route string would disarm the guard through the route gate
        # even with the framework restored — the committed file is the
        # pristine truth for everything derived from this file. When
        # the file is untracked at HEAD (or this is not a git
        # checkout) there is no committed copy, and the working-tree
        # source is used as before.
        try:
            ast.parse(source)
        except SyntaxError:
            committed = _head_source(root, rel)
            if committed is not None:
                source = committed
        index.files[rel] = _top_level_names(source)
        # Retry #23: the serving framework comes from the committed
        # module, not the working tree. A bad scaffold write disarms
        # the guard two ways: an unparseable overwrite leaves no
        # framework recorded (the import scan returns nothing on a
        # SyntaxError), and a parseable overwrite in a different
        # framework re-labels the serving stack as the wrong one. The
        # committed module is the pristine truth — read it from HEAD
        # when it names a framework there, and fall back to the
        # working-tree source when HEAD names none (or the file is
        # untracked / this is not a git checkout).
        framework = _framework_of_imports(_imported_modules(source))
        head_source = _head_source(root, rel)
        if head_source is not None:
            head_framework = _framework_of_imports(_imported_modules(head_source))
            if head_framework is not None:
                framework = head_framework
        if framework is not None:
            index.frameworks[rel] = framework
        stem = path.stem
        index.modules.setdefault(stem, []).append(rel)
        spans = _handler_spans(source)
        for match in _ROUTE_EQ_RE.finditer(source):
            route = match.group(1)
            index.routes.setdefault(route, rel)
            handler = _handler_for_line(spans, source, match.start())
            if handler is not None:
                index.route_handlers.setdefault(route, handler)
            branch = _route_branch(source, spans, route)
            if branch is not None:
                index.route_branches.setdefault(route, branch)
        for match in _ROUTE_STARTSWITH_RE.finditer(source):
            prefix = match.group(1)
            key = prefix.rstrip("/") + "/…"
            index.routes.setdefault(key, rel)
            handler = _handler_for_line(spans, source, match.start())
            if handler is not None:
                index.route_handlers.setdefault(key, handler)
        if rel == "freetalon/config.py":
            port_match = _API_PORT_RE.search(source)
            if port_match:
                index.api_port = int(port_match.group(1))
    for stem in index.modules:
        index.modules[stem] = sorted(index.modules[stem])
    return index


def _handler_groups(index: RepoIndex, module: str) -> str:
    """One module's routes grouped by the handler that serves them.

    "the do_GET method of the Handler class (defined inside
    HiveAPIServer.start) handles /health, /status, /metrics; the do_POST
    method of the Handler class (defined inside HiveAPIServer.start)
    handles /shutdown". Empty when no route of the module has a recorded
    handler.
    """
    by_handler: dict[str, list[str]] = {}
    for route in sorted(index.routes):
        if index.routes[route] != module:
            continue
        handler = index.route_handlers.get(route)
        if handler is None:
            continue
        by_handler.setdefault(handler, []).append(route)
    return "; ".join(
        f"{handler} handles {', '.join(routes)}"
        for handler, routes in sorted(by_handler.items())
    )


def format_repo_context(index: RepoIndex, workspace: str | None = None) -> str:
    """Compact repo summary for the planner prompt (grounding context).

    ``workspace`` is the run-outputs directory from the TaskIntent
    constraints, when known. The model must not confuse it with the repo
    root: the retry-#9 plan named module paths under
    /home/seeker/freetalon-workspace/freetalon/api.py — the workspace
    used as the repo root — because the context never said where the
    repo actually lives. Both roots are named explicitly here.
    """
    lines = ["REPO CONTEXT — extend what exists; do not duplicate it:"]
    lines.append(
        f"- Repo root: {index.root} — code modules live here "
        "(freetalon/..., tests/...)."
    )
    if workspace:
        lines.append(
            f"- Workspace: {workspace} — run outputs and generated files "
            "live here; it is NOT the repo root."
        )
    if index.routes:
        routes = ", ".join(
            f"{route} ({index.routes[route]})" for route in sorted(index.routes)
        )
        lines.append(f"- FreeTalon API already serves: {routes}.")
    lines.append(
        f"- FreeTalon API port is {index.api_port} (freetalon/config.py, "
        "HiveConfig.port). Ollama is on 11434. Port 8000 is the "
        "OpenAI-compatible LLM backend, not a service to curl or verify."
    )
    serving = sorted({index.routes[route] for route in index.routes})
    for module in serving:
        framework = index.frameworks.get(module)
        if framework:
            line = (
                f"- {module} serves the API routes and is built on {framework} — "
                "extend it in place"
            )
            handled = _handler_groups(index, module)
            if handled:
                line += f"; {handled}"
            line += "; do not rewrite it in a different web framework."
            lines.append(line)
    if index.files:
        shown = sorted(index.files)[:_MAX_CONTEXT_FILES]
        for rel in shown:
            names = index.files[rel]
            detail = f": {', '.join(names)}" if names else ""
            lines.append(f"- {rel}{detail}")
        if len(index.files) > _MAX_CONTEXT_FILES:
            lines.append(f"- … and {len(index.files) - _MAX_CONTEXT_FILES} more modules.")
    if index.docs:
        shown_docs = sorted(index.docs)[:_MAX_CONTEXT_FILES]
        listing = ", ".join(
            f"{rel} ({index.docs[rel]} bytes)" for rel in shown_docs
        )
        lines.append(f"- Repo documents: {listing}.")
        if len(index.docs) > _MAX_CONTEXT_FILES:
            lines.append(
                f"- … and {len(index.docs) - _MAX_CONTEXT_FILES} more documents."
            )
    return "\n".join(lines)


# ── Existing-file inventory (campaign build #4) ────────────────────
#
# format_repo_context above lists at most _MAX_CONTEXT_FILES modules
# and only freetalon/'s — it never showed the planner the tests/ tree
# at all. Exam arm #13 (2026-11-23) priced that gap: the planner
# scaffolded the EXISTING tests/test_pairing.py — a 47-test leftover
# module from earlier arms — because nothing in its prompt said the
# file existed; the scaffold-preservation guard caught the loss
# (61 of 65 definitions dropped) only at validation, on the last
# repair attempt, and the planning call died. The inventory below is
# the deterministic counterpart: every existing Python file in the
# scopes a plan can plausibly write to, with the number of
# definitions it holds, so "this file already exists" is a prompt
# fact instead of a rejection discovered after the fact.


def existing_file_inventory(
    root: str | Path, focus_dirs: Collection[str] = ()
) -> list[tuple[str, int]]:
    """(repo-relative path, qualified definition count) for existing files.

    Scopes, in one sorted listing: the package root's own modules
    (``freetalon/*.py``, non-recursive — the classic scaffold targets
    like api.py), every ``*.py`` under each ``focus_dirs`` entry
    (repo-relative directories, walked recursively — the caller
    passes the goal-named specs' directories, i.e. the package the
    goal is about), and every ``*.py`` under ``tests/``
    (recursive). ``__pycache__`` and ``*.bak`` files are skipped, as
    in build_repo_index. A file that cannot be read still appears
    (it exists — that is the fact the planner needs) with a count
    of 0; an unparseable file likewise counts 0 definitions
    (qualified_def_names' own stance). Sorted by path and
    deduplicated, so the result is byte-stable for an unchanged
    tree.
    """
    root_path = Path(root)
    candidates: set[Path] = set()
    package = root_path / "freetalon"
    if package.is_dir():
        candidates.update(package.glob("*.py"))
    for focus in focus_dirs:
        focus_path = root_path / focus
        if focus_path.is_dir():
            candidates.update(focus_path.rglob("*.py"))
    tests_dir = root_path / "tests"
    if tests_dir.is_dir():
        candidates.update(tests_dir.rglob("*.py"))
    entries: dict[str, int] = {}
    for path in sorted(candidates):
        if "__pycache__" in path.parts or path.name.endswith(".bak"):
            continue
        try:
            rel = path.relative_to(root_path).as_posix()
        except ValueError:
            continue
        if rel in entries:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            source = ""
        entries[rel] = len(qualified_def_names(source))
    return sorted(entries.items())


def format_existing_file_inventory(entries: list[tuple[str, int]]) -> str:
    """The EXISTING FILES prompt block: the inventory + the standing rule.

    Empty for an empty inventory (a tree with none of the inventoried
    scopes prompts exactly as before). The rule is the one the
    scaffold-preservation guard enforces after the fact, stated
    before it: an existing file is modified with an anchored or
    scoped edit, or left untouched — never scaffolded; brand-new
    files are fine.
    """
    if not entries:
        return ""
    lines = [
        "EXISTING FILES — the files below already exist in the tree, "
        "with the number of definitions each one holds. NEVER scaffold "
        "an existing file: a scaffold replaces the whole file, so every "
        "definition the new content does not repeat is deleted. Modify "
        "an existing file with an anchored or scoped edit, or leave it "
        "untouched; brand-new files are fine."
    ]
    for rel, count in entries:
        lines.append(f"- {rel} ({count} definitions)")
    return "\n".join(lines)


def duplicate_scaffold_error(target_path: str, index: RepoIndex) -> str | None:
    """Return an error if scaffolding ``target_path`` duplicates existing code.

    ``None`` means the scaffold is fine: the file already exists (an edit /
    extension, not a new file), or nothing in the repo collides with it.
    A new file is a duplicate when its stem matches an existing HTTP route
    name (``api/health.py`` duplicates ``GET /health``) or, when the new
    file lives inside the repo, an existing module of the same stem.
    Package scaffolding files (``__init__.py``, ``__main__.py``) are
    exempt by exact filename — every package directory needs its own.
    """
    if not target_path:
        return None
    candidate = Path(target_path)
    # Package scaffolding is exempt by exact filename: every package
    # directory must carry its OWN __init__.py (and may carry its own
    # __main__.py), so a new package's dunder files collide with the
    # existing '__init__' / '__main__' module stems by construction —
    # "extend freetalon/__init__.py instead" cannot make
    # freetalon/pairing/ a package. The check is name-only (content
    # is never compared), so the exemption removes no real
    # protection: substantive stems keep both branches below in
    # full. (Pairing proof arm, 2026-11-19: a new standalone package
    # was unplannable because its __init__.py / __main__.py were
    # rejected as duplicates of those stems.)
    if candidate.name in ("__init__.py", "__main__.py"):
        return None
    # An existing file is an extension, not a duplicate.
    if candidate.is_absolute() and candidate.exists():
        return None
    if not candidate.is_absolute():
        if (index.root / candidate).exists() or (Path.cwd() / candidate).exists():
            return None
    stem = candidate.stem
    if not stem:
        return None
    route_names = index.route_names()
    if stem in route_names:
        routes = sorted(r for r in index.routes if r.strip("/").split("/", 1)[0] == stem)
        serving = sorted({index.routes[r] for r in routes})
        return (
            f"scaffolding {target_path!r} duplicates the existing "
            f"{', '.join(routes)} route in {', '.join(serving)} — "
            f"extend {serving[0]} instead of creating a new file"
        )
    if stem in index.modules:
        try:
            inside_repo = candidate.is_absolute() and candidate.is_relative_to(index.root)
        except AttributeError:  # pragma: no cover - Python < 3.9
            inside_repo = False
        if inside_repo:
            return (
                f"scaffolding {target_path!r} duplicates the existing module "
                f"{stem!r} ({', '.join(index.modules[stem])}) — "
                "extend that module instead of creating a new file"
            )
    return None


# ── Goal-named spec grounding ─────────────────────────────────────
#
# The index above answers "what code exists"; it never showed the
# planner a document. Exam re-arms #3/#4 (2026-11-23) measured what
# that costs: the goal named freetalon/pairing/CONTRACT.md as the
# specification, its contents reached no call, and the loop first
# implemented the goal's own restated summary instead of the spec,
# then — with the summary scrubbed — wrote NotImplementedError
# stubs deferring to the file it could not see, and invented a
# protocol on re-plan. The cross-model consultation's consensus fix
# (verbatim injection, ranked first by 7 of 8 models consulted):
# the harness loads a goal-named spec itself and puts its exact
# text in front of the planner and the code-writing calls. Matching
# is deterministic — a repo-relative path named in the goal, an
# absolute path under the root, or a basename unique in the repo.

# A file reference in goal prose: a path-shaped token ending in a
# spec extension ("freetalon/pairing/CONTRACT.md", "CONTRACT.md",
# "/abs/root/docs/DESIGN.md"). Sentence punctuation around the
# token is outside the character class, so it never sticks to the
# match. A .py file named in a goal is code — the index already
# grounds it; the gap measured above was documents.
_SPEC_FILE_RE = re.compile(r"[A-Za-z0-9_./-]+\.(?:md|txt|rst)", re.IGNORECASE)

# Total bytes of goal-named spec text one call carries. CONTRACT.md
# is 9.6 KB; the cap leaves ~3x headroom for a spec plus companions
# while keeping the block a bounded part of the prompt. Over the
# cap the planning call fails loudly (SpecContextError): a
# truncated spec is a wrong spec, so the normative text is never
# silently cut (the consultation's "stop or deliberately
# partition" rule; partitioning is future work).
_MAX_SPEC_CONTEXT_BYTES = 32768

# Directories the docs walk never enters: VCS internals, the venv
# (its vendored .md files are not repo documents), caches.
_DOC_WALK_EXCLUDED = {".git", ".venv", "__pycache__", "node_modules"}


class SpecContextError(ValueError):
    """A goal-named spec cannot be grounded as-is.

    Raised when a named file cannot be read, is not UTF-8 text, or
    the named specs together exceed _MAX_SPEC_CONTEXT_BYTES.
    Planning fails loudly rather than proceeding ungrounded or
    silently truncating the normative text.
    """


@dataclass
class SpecDocument:
    """One goal-named spec file, loaded verbatim from disk."""

    path: str  # repo-relative posix path
    sha256: str
    byte_count: int
    text: str

    def record(self) -> dict[str, str | int]:
        """The audit-trail entry: what was included, provably."""
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.byte_count,
        }


def _scan_repo_docs(root: Path) -> dict[str, int]:
    """Repo documents (rel-path -> byte size): every *.md under the
    root outside the excluded directories."""
    docs: dict[str, int] = {}
    if not root.is_dir():
        return docs
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in _DOC_WALK_EXCLUDED for part in rel.parts):
            continue
        try:
            docs[rel.as_posix()] = path.stat().st_size
        except OSError:
            continue
    return docs


def _match_spec_token(
    token: str, root: Path, docs: dict[str, int]
) -> str | None:
    """The repo-relative path a goal's file token names, or None.

    A token matches when it IS a repo-relative doc path, when it is
    an absolute path under the root, or when its basename is unique
    among the repo's docs. An ambiguous basename matches nothing —
    guessing between two same-named specs would ground the plan in
    the wrong text.
    """
    if token.startswith("/"):
        try:
            candidate = Path(token).relative_to(root).as_posix()
        except ValueError:
            return None
    else:
        candidate = token
        while candidate.startswith("./"):
            candidate = candidate[2:]
    if candidate in docs:
        return candidate
    basename = candidate.rsplit("/", 1)[-1]
    matches = [
        rel for rel in docs if rel.rsplit("/", 1)[-1] == basename
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def goal_named_specs(
    goal_text: str | None,
    root: str | Path,
    index: RepoIndex | None = None,
) -> list[SpecDocument]:
    """The spec files ``goal_text`` names, loaded verbatim.

    Deterministic: goal tokens are matched against the repo's docs
    (``index``'s inventory when given — it must be the index of the
    same root — a fresh scan otherwise), each matched file is read
    once, and the result is sorted by path. A named file that
    cannot be read or decoded, or a set whose total size exceeds
    _MAX_SPEC_CONTEXT_BYTES, raises SpecContextError: planning
    fails loudly rather than running ungrounded or on a truncated
    spec. A goal naming no existing spec file returns [] and every
    caller behaves exactly as before this grounding existed.
    """
    if not goal_text:
        return []
    root_path = Path(root).expanduser().resolve()
    docs = index.docs if index is not None else _scan_repo_docs(root_path)
    matched: list[str] = []
    for token in _SPEC_FILE_RE.findall(goal_text):
        rel = _match_spec_token(token, root_path, docs)
        if rel is not None and rel not in matched:
            matched.append(rel)
    specs: list[SpecDocument] = []
    total = 0
    for rel in sorted(matched):
        try:
            raw = (root_path / rel).read_bytes()
        except OSError as exc:
            raise SpecContextError(
                f"goal-named spec {rel!r} could not be read: {exc}"
            ) from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpecContextError(
                f"goal-named spec {rel!r} is not UTF-8 text: {exc}"
            ) from exc
        total += len(raw)
        specs.append(
            SpecDocument(
                path=rel,
                sha256=hashlib.sha256(raw).hexdigest(),
                byte_count=len(raw),
                text=text,
            )
        )
    if total > _MAX_SPEC_CONTEXT_BYTES:
        detail = ", ".join(
            f"{spec.path} ({spec.byte_count} bytes)" for spec in specs
        )
        raise SpecContextError(
            f"goal-named specs total {total} bytes, over the "
            f"{_MAX_SPEC_CONTEXT_BYTES}-byte grounding cap: {detail}. "
            "Name fewer specs, or split the spec deliberately — the "
            "normative text is never truncated silently."
        )
    return specs


def format_spec_context(specs: list[SpecDocument]) -> str:
    """The prompt block carrying goal-named specs, verbatim.

    The framing is the consultation's short form: the spec is
    authoritative, the plan decomposes it rather than replacing it,
    and a gap in the spec is named, never filled by invention. The
    file text itself is reproduced exactly as read — no summary,
    no excerpt.
    """
    if not specs:
        return ""
    lines = [
        "GOAL-NAMED SPEC — the file(s) below are the authoritative "
        "specification for this goal. The plan decomposes the spec; "
        "it never replaces it. Where the spec is silent or "
        "contradictory, name the gap instead of inventing behavior."
    ]
    for spec in specs:
        lines.append(
            f"--- {spec.path} (sha256 {spec.sha256[:12]}, "
            f"{spec.byte_count} bytes) ---"
        )
        lines.append(spec.text)
    return "\n".join(lines)
