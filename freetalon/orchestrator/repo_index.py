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
import os
import re
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


def framework_in_text(text: str) -> str | None:
    """The web framework a blob of Python source imports, if any."""
    names: set[str] = set()
    for match in _IMPORT_LINE_RE.finditer(text):
        names.add(match.group(1) or match.group(2))
    return _framework_of_imports(names)


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


def build_repo_index(repo_root: str | Path | None = None) -> RepoIndex:
    """Walk ``<root>/freetalon/**/*.py`` and index routes, modules, and the API port."""
    root = resolve_repo_root(repo_root)
    index = RepoIndex(root=root)
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
        index.files[rel] = _top_level_names(source)
        framework = _framework_of_imports(_imported_modules(source))
        if framework is not None:
            index.frameworks[rel] = framework
        stem = path.stem
        index.modules.setdefault(stem, []).append(rel)
        for match in _ROUTE_EQ_RE.finditer(source):
            index.routes.setdefault(match.group(1), rel)
        for match in _ROUTE_STARTSWITH_RE.finditer(source):
            prefix = match.group(1)
            index.routes.setdefault(prefix.rstrip("/") + "/…", rel)
        if rel == "freetalon/config.py":
            port_match = _API_PORT_RE.search(source)
            if port_match:
                index.api_port = int(port_match.group(1))
    for stem in index.modules:
        index.modules[stem] = sorted(index.modules[stem])
    return index


def format_repo_context(index: RepoIndex) -> str:
    """Compact repo summary for the planner prompt (grounding context)."""
    lines = ["REPO CONTEXT — extend what exists; do not duplicate it:"]
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
            lines.append(
                f"- {module} serves the API routes and is built on {framework} — "
                "extend it in place; do not rewrite it in a different web framework."
            )
    if index.files:
        shown = sorted(index.files)[:_MAX_CONTEXT_FILES]
        for rel in shown:
            names = index.files[rel]
            detail = f": {', '.join(names)}" if names else ""
            lines.append(f"- {rel}{detail}")
        if len(index.files) > _MAX_CONTEXT_FILES:
            lines.append(f"- … and {len(index.files) - _MAX_CONTEXT_FILES} more modules.")
    return "\n".join(lines)


def duplicate_scaffold_error(target_path: str, index: RepoIndex) -> str | None:
    """Return an error if scaffolding ``target_path`` duplicates existing code.

    ``None`` means the scaffold is fine: the file already exists (an edit /
    extension, not a new file), or nothing in the repo collides with it.
    A new file is a duplicate when its stem matches an existing HTTP route
    name (``api/health.py`` duplicates ``GET /health``) or, when the new
    file lives inside the repo, an existing module of the same stem.
    """
    if not target_path:
        return None
    candidate = Path(target_path)
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
