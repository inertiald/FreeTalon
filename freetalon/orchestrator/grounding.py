"""Edit grounding: references an edit introduces must resolve.

Campaign build #3 (2026-11-23), from exam arm #11: build #2's
modify rule forced the re-plan's writes into anchored edit form,
and the edits APPLIED cleanly — but their content referenced
symbols that do not exist in the target (``self.store`` for
``self.server.store``; a body re-read on a handler that never
stored one) and the repaired server hung. Edit-form success,
content-grounding failure. The scoped-edit machinery checks that
an edit's SEARCH text matches the file; nothing checked that the
REPLACE text's own references resolve against the file it lands
in. This module is that check.

``edit_grounding_violations(old_source, new_source)`` compares
the two texts and flags only references the edit INTRODUCES — a
name or attribute the new text uses that the old text never did —
when the new file cannot resolve it:

- ``self.<attr>`` loads inside a class: the attribute must be
  assigned somewhere in the enclosing class (any method —
  ``self.x = ...``, annotated or augmented assignment), defined
  in the class as a method/property, assigned at class level, or
  inherited. In-file bases contribute their own assigned/defined
  sets (recursively); a small table of known instance attributes
  covers the stdlib handler bases (``BaseHTTPRequestHandler`` /
  ``StreamRequestHandler``). A class defining ``__getattr__`` is
  exempt — any attribute may resolve dynamically. A class with
  ANY base that is neither defined in the file nor in the known
  table is UNVERIFIABLE and exempt: doubt resolves to no flag.
- Bare calls (``ast.Call`` whose func is a bare ``Name``): the
  name must resolve to a module-level definition (def / class /
  assignment), an import, a name bound in an enclosing function
  scope (parameters, local assignments, nested defs, imports), or
  a builtin. A star import anywhere in the module disables this
  half — the names it binds are unknowable statically. Names
  starting with an underscore are exempt: a call to a
  module-private helper the file does not (yet) define is this
  codebase's forward-reference shape — the cycle-7 precedent
  (TestPlannerPlaceholderValueFix's REAL_DELTA, accepted at both
  the parse and the repair-seam surface) is a delta whose real
  expressions call ``_ollama_reachable()`` / ``_gpu_temperature()``
  helpers the same work defines elsewhere; a new grounding rule
  composes with that pinned behavior instead of overriding it.
  What remains flagged is the clear case: a call to a public name
  that exists nowhere — a hallucinated API, not a forward
  reference.

Conservative by design: it is better to miss an exotic violation
than to decline a sound repair. Unparseable input yields no
violations — the syntax machinery owns that failure class, and a
grounding opinion about text that does not parse would be noise.
Stdlib only.
"""

from __future__ import annotations

import ast
import builtins
from typing import Any

# Instance attributes (and methods — a ``self.method()`` call is an
# attribute load too) the stdlib sets or defines on its request
# handlers, so a subclass referencing them is grounded even though
# the definition lives in the stdlib, not in the file under edit.
# The set is the base classes' documented public surface plus the
# instance attributes they assign in setup()/parse_request() —
# generous on purpose: for a fixed stdlib surface, over-listing
# costs nothing and under-listing would decline sound repairs.
_KNOWN_BASE_ATTRS: dict[str, frozenset[str]] = {
    "BaseHTTPRequestHandler": frozenset(
        {
            # assigned instance attributes
            "server",
            "request",
            "client_address",
            "headers",
            "rfile",
            "wfile",
            "path",
            "command",
            "request_version",
            "requestline",
            "close_connection",
            "connection",
            # class attributes read through self
            "server_version",
            "sys_version",
            "error_message_format",
            "error_content_type",
            "protocol_version",
            "default_request_version",
            "responses",
            # public methods
            "send_response",
            "send_response_only",
            "send_error",
            "send_error_response",
            "send_header",
            "end_headers",
            "flush_headers",
            "log_request",
            "log_error",
            "log_message",
            "version_string",
            "date_time_string",
            "address_string",
            "parse_request",
            "handle_expect_100",
            "handle",
            "setup",
            "finish",
        }
    ),
    "StreamRequestHandler": frozenset(
        {"connection", "rfile", "wfile", "setup", "handle", "finish"}
    ),
    "DatagramRequestHandler": frozenset(
        {"connection", "rfile", "wfile", "setup", "handle", "finish"}
    ),
}

_BUILTIN_NAMES = frozenset(dir(builtins))


def edit_grounding_violations(old_source: str, new_source: str) -> list[str]:
    """Grounding violations an edit introduces, as message strings.

    Empty when the edit introduces no unresolvable reference, when
    either text does not parse, or when the check cannot verify
    (the conservative exits documented in the module docstring).
    """
    try:
        old_tree = ast.parse(old_source)
        new_tree = ast.parse(new_source)
    except (SyntaxError, ValueError):
        return []
    violations: list[str] = []
    violations.extend(_attribute_violations(old_tree, new_tree))
    violations.extend(_bare_call_violations(old_tree, new_tree))
    return violations


# ── self.<attr> references ──────────────────────────────────────────


def _base_name(base: ast.expr) -> str | None:
    """The final component of a base-class expression, if simple."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return None


def _walk_pruning_nested_classes(root: ast.AST) -> Any:
    """Yield ``root``'s descendants without entering nested class
    bodies — a nested class's ``self`` is a different object, so its
    assignments and loads belong to it, not to the enclosing class."""
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _class_own_facts(class_node: ast.ClassDef) -> tuple[set[str], set[str]]:
    """(assigned self attrs, defined names) of ONE class body.

    Assigned: ``self.x`` targets of Assign/AnnAssign/AugAssign in
    any method of the class, plus class-level Name assignments
    (reachable through ``self``). Defined: method names and
    class-level def/class names.
    """
    assigned: set[str] = set()
    defined: set[str] = set()
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(stmt.name)
            for inner in _walk_pruning_nested_classes(stmt):
                if isinstance(inner, ast.Attribute) and isinstance(
                    inner.value, ast.Name
                ) and inner.value.id == "self":
                    if isinstance(inner.ctx, ast.Store):
                        assigned.add(inner.attr)
        elif isinstance(stmt, ast.ClassDef):
            defined.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(
            stmt.target, ast.Name
        ):
            defined.add(stmt.target.id)
    return assigned, defined


def _class_self_loads(class_node: ast.ClassDef) -> set[str]:
    """Every ``self.<attr>`` LOAD inside the class's own methods.

    Nested class bodies are excluded — their ``self`` is a
    different object; they are judged as classes of their own.
    """
    loads: set[str] = set()
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in _walk_pruning_nested_classes(stmt):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "self"
                    and isinstance(inner.ctx, ast.Load)
                ):
                    loads.add(inner.attr)
    return loads


def _attribute_violations(
    old_tree: ast.Module, new_tree: ast.Module
) -> list[str]:
    classes: dict[str, ast.ClassDef] = {}
    for node in ast.walk(new_tree):
        if isinstance(node, ast.ClassDef):
            classes.setdefault(node.name, node)
    old_loads: dict[str, set[str]] = {}
    for node in ast.walk(old_tree):
        if isinstance(node, ast.ClassDef):
            old_loads.setdefault(node.name, set()).update(
                _class_self_loads(node)
            )
    if not classes:
        return []

    resolved_cache: dict[str, set[str] | None] = {}

    def resolvable(class_node: ast.ClassDef, seen: frozenset[str]) -> set[str] | None:
        """The self-attrs this class can resolve, or None when any
        base is unverifiable (the class is then exempt)."""
        if class_node.name in resolved_cache and class_node.name not in seen:
            return resolved_cache[class_node.name]
        assigned, defined = _class_own_facts(class_node)
        if "__getattr__" in defined:
            return None  # dynamic resolution: exempt by design
        resolved = assigned | defined
        for base in class_node.bases:
            name = _base_name(base)
            if name is None:
                return None
            if name in _KNOWN_BASE_ATTRS:
                resolved |= _KNOWN_BASE_ATTRS[name]
            elif name in classes and name != class_node.name:
                if name in seen:
                    return None  # inheritance cycle: cannot verify
                inherited = resolvable(classes[name], seen | {class_node.name})
                if inherited is None:
                    return None
                resolved |= inherited
            elif name == class_node.name:
                continue
            else:
                return None  # unknown base: cannot verify
        if class_node.name not in seen:
            resolved_cache[class_node.name] = resolved
        return resolved

    violations: list[str] = []
    for name, class_node in classes.items():
        resolvable_attrs = resolvable(class_node, frozenset())
        if resolvable_attrs is None:
            continue
        introduced = _class_self_loads(class_node) - old_loads.get(name, set())
        for attr in sorted(introduced):
            if attr not in resolvable_attrs:
                violations.append(
                    f"self.{attr} is referenced in class {name} but is "
                    f"never assigned in that class (nor inherited from "
                    f"a base defined in this file) — the edit "
                    f"introduces a reference the file cannot resolve"
                )
    return violations


# ── bare calls ──────────────────────────────────────────────────────


def _module_level_names(tree: ast.Module) -> set[str]:
    """Names a module binds at top level: defs, classes, imports,
    and assignment targets."""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for inner in ast.walk(target):
                    if isinstance(inner, ast.Name) and isinstance(
                        inner.ctx, ast.Store
                    ):
                        names.add(inner.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(
            stmt.target, ast.Name
        ):
            names.add(stmt.target.id)
    return names


def _has_star_import(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return True
    return False


def _bound_names_in_scope(body: list[ast.stmt]) -> set[str]:
    """Names bound directly in one function/class scope's body:
    parameters are the caller's addition; here — assignments,
    nested defs/classes, imports, for/with targets, walrus names,
    and exception-handler names, without descending into nested
    function/class bodies (their bindings are their own)."""
    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            return  # do not descend: nested scope
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in body:
        visit(stmt)
    return names


def _param_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _unresolved_bare_calls(tree: ast.Module) -> set[str]:
    """Bare-call names in this tree that no visible scope resolves."""
    module_names = _module_level_names(tree)
    unresolved: set[str] = set()

    def visit(node: ast.AST, scopes: tuple[set[str], ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local = _param_names(node.args) | _bound_names_in_scope(node.body)
            for child in node.body:
                visit(child, scopes + (local,))
            for decorator in node.decorator_list:
                visit(decorator, scopes)
            return
        if isinstance(node, ast.Lambda):
            local = _param_names(node.args)
            visit(node.body, scopes + (local,))
            return
        if isinstance(node, ast.ClassDef):
            local = _bound_names_in_scope(node.body)
            for child in node.body:
                visit(child, scopes + (local,))
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if (
                name not in module_names
                and name not in _BUILTIN_NAMES
                and not any(name in scope for scope in scopes)
            ):
                unresolved.add(name)
        for child in ast.iter_child_nodes(node):
            visit(child, scopes)

    for stmt in tree.body:
        visit(stmt, ())
    return unresolved


def _all_bare_call_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def _bare_call_violations(
    old_tree: ast.Module, new_tree: ast.Module
) -> list[str]:
    if _has_star_import(new_tree):
        return []
    introduced = _unresolved_bare_calls(new_tree) - _all_bare_call_names(old_tree)
    return [
        f"the edit introduces a call to {name}(...), which is not "
        f"defined in this module, imported, or a builtin"
        for name in sorted(introduced)
        if not name.startswith("_")
    ]
