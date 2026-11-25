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

Campaign build #15 (2026-11-24) — attribute CHAINS. Arm #24's
landed repair referenced ``self.server.pairing_store`` in a file
whose handler reaches the store as ``self.store``: ``self.server``
itself is grounded (the stdlib handler table above), so the
first-level check passed, and the second level — an attribute of
the server object, not of the handler — was never judged. The
AttributeError turned the hang the repair targeted into a crash.
``attribute_grounding_violations(old_source, new_source)`` is that
judgment, and ``edit_grounding_violations`` folds it in:

- For each attribute chain of length >= 2 rooted at ``self`` that
  the edit INTRODUCES (the chain does not appear in the old text),
  each deeper attribute must have a home in the file. The object a
  chain steps onto is resolved only when the file itself says what
  it is: an annotation naming an in-file class, a constructor
  assignment of an in-file class, or — the arm #24 case — the
  ``self.server`` of a request handler, whose object is the server
  this file constructs. Anything else (imported types, unknown
  provenance) is UNVERIFIABLE and exempt, as before.
- Evidence for the stepped-onto attribute: the resolved class's
  own assigned/defined attributes (through in-file bases), class
  level annotations, attributes attached to instances of that
  class anywhere in the file (``httpd.pairing_store = store``),
  the stdlib surface of the object where one is known (the server
  table below), and the chain's own presence in the old text.
  In the repo's pairing server, ``self.server.pairing_store`` is
  grounded by exactly this evidence (the server class declares
  it and make_server assigns it); in arm #24's file the leaf had
  no evidence anywhere, and the check declines with the class's
  established ``self.*`` vocabulary in the message so the re-plan
  can spell the reference the file's way.
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
    violations.extend(_chain_violations(old_tree, new_tree))
    violations.extend(_bare_call_violations(old_tree, new_tree))
    return violations


def attribute_grounding_violations(old_source: str, new_source: str) -> list[str]:
    """Only the attribute-CHAIN violations (build #15), as messages.

    The repair seams consult this first so a chain failure declines
    under its own reason (``ungrounded_attribute``) instead of the
    first-level ``ungrounded_reference``; the plan-form call sites
    keep folding chains into ``edit_grounding_violations``.
    """
    try:
        old_tree = ast.parse(old_source)
        new_tree = ast.parse(new_source)
    except (SyntaxError, ValueError):
        return []
    return _chain_violations(old_tree, new_tree)


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


def _resolvable_attrs(
    class_node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    cache: dict[str, set[str] | None],
    seen: frozenset[str] = frozenset(),
) -> set[str] | None:
    """The self-attrs this class can resolve, or None when any base
    is unverifiable (the class is then exempt). Extracted from
    _attribute_violations (build #15) so the chain check shares the
    exact same resolution semantics."""
    if class_node.name in cache and class_node.name not in seen:
        return cache[class_node.name]
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
            inherited = _resolvable_attrs(
                classes[name], classes, cache, seen | {class_node.name}
            )
            if inherited is None:
                return None
            resolved |= inherited
        elif name == class_node.name:
            continue
        else:
            return None  # unknown base: cannot verify
    if class_node.name not in seen:
        cache[class_node.name] = resolved
    return resolved


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

    violations: list[str] = []
    for name, class_node in classes.items():
        resolvable_attrs = _resolvable_attrs(class_node, classes, resolved_cache)
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


# ── attribute chains: self.<a>.<b> (build #15) ──────────────────────

# Bases that make an in-file class a plausible holder of a request
# handler's ``self.server``: the stdlib server classes.
_KNOWN_SERVER_BASES = frozenset(
    {
        "BaseServer",
        "TCPServer",
        "UDPServer",
        "ThreadingTCPServer",
        "ForkingTCPServer",
        "ThreadingUDPServer",
        "ForkingUDPServer",
        "HTTPServer",
        "ThreadingHTTPServer",
        "ForkingHTTPServer",
    }
)

# Attributes the stdlib assigns or defines on its server objects
# (socketserver.BaseServer / TCPServer, http.server.HTTPServer,
# ThreadingMixIn state): the surface a handler's ``self.server``
# legitimately carries when the file constructs a stdlib server
# directly. Generous on purpose, like _KNOWN_BASE_ATTRS.
_KNOWN_SERVER_ATTRS = frozenset(
    {
        "server_address",
        "socket",
        "RequestHandlerClass",
        "timeout",
        "serve_forever",
        "shutdown",
        "handle_request",
        "service_actions",
        "server_bind",
        "server_activate",
        "server_close",
        "handle_error",
        "handle_timeout",
        "verify_request",
        "get_request",
        "process_request",
        "finish_request",
        "shutdown_request",
        "close_request",
        "fileno",
        "address_family",
        "socket_type",
        "request_queue_size",
        "allow_reuse_address",
        "allow_reuse_port",
        "daemon_threads",
        "block_on_close",
        "process_request_thread",
        "_threads",
        "server_name",
        "server_port",
    }
)


def _class_self_chains(class_node: ast.ClassDef) -> set[tuple[str, ...]]:
    """Every attribute chain of length >= 2 rooted at ``self`` and
    LOADED in the class's own methods, as attribute tuples —
    ``self.server.store`` yields ("server", "store"). Every prefix
    is included: each prefix is itself a load the runtime resolves.
    Nested class bodies are excluded, as in _class_self_loads."""
    chains: set[tuple[str, ...]] = set()
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in _walk_pruning_nested_classes(stmt):
                if not (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.ctx, ast.Load)
                ):
                    continue
                attrs: list[str] = []
                node: ast.expr = inner
                while isinstance(node, ast.Attribute):
                    attrs.append(node.attr)
                    node = node.value
                if (
                    isinstance(node, ast.Name)
                    and node.id == "self"
                    and len(attrs) >= 2
                ):
                    attrs.reverse()
                    for end in range(2, len(attrs) + 1):
                        chains.add(tuple(attrs[:end]))
    return chains


def _annotation_name(annotation: ast.expr) -> str | None:
    """The final component of an annotation's type name, resolving
    string forward references and subscripts; None when the shape
    is not a simple name."""
    if isinstance(annotation, ast.Constant):
        if isinstance(annotation.value, str):
            text = annotation.value.strip().split(".")[-1].strip()
            return text or None
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _annotation_name(annotation.value)
    return None


def _class_attr_annotations(class_node: ast.ClassDef) -> dict[str, str]:
    """attr name -> annotated type name, for class-level attribute
    declarations (``server: "_PairingHTTPServer"``) and annotated
    ``self.x: T`` assignments in the class's methods."""
    out: dict[str, str] = {}

    def note(target: ast.expr, annotation: ast.expr) -> None:
        attr: str | None = None
        if isinstance(target, ast.Name):
            attr = target.id
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            attr = target.attr
        if attr is None:
            return
        type_name = _annotation_name(annotation)
        if type_name:
            out.setdefault(attr, type_name)

    for stmt in class_node.body:
        if isinstance(stmt, ast.AnnAssign):
            note(stmt.target, stmt.annotation)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in _walk_pruning_nested_classes(stmt):
                if isinstance(inner, ast.AnnAssign):
                    note(inner.target, inner.annotation)
    return out


def _class_attr_constructors(class_node: ast.ClassDef) -> dict[str, str]:
    """attr name -> constructed class name, for ``self.x = C(...)``
    assignments in the class's methods."""
    out: dict[str, str] = {}
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in _walk_pruning_nested_classes(stmt):
                if isinstance(inner, ast.Assign) and isinstance(
                    inner.value, ast.Call
                ):
                    ctor = _base_name(inner.value.func)
                    if ctor is None:
                        continue
                    for target in inner.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            out.setdefault(target.attr, ctor)
    return out


def _inherited_lookup(
    class_node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    extractor: Any,
) -> dict[str, str]:
    """Merge an attr->name mapping over a class and its in-file
    ancestors, nearest class winning."""
    merged: dict[str, str] = {}
    queue = [class_node]
    seen: set[str] = set()
    while queue:
        node = queue.pop(0)
        if node.name in seen:
            continue
        seen.add(node.name)
        for attr, value in extractor(node).items():
            merged.setdefault(attr, value)
        for base in node.bases:
            base_name = _base_name(base)
            if base_name in classes and base_name != node.name:
                queue.append(classes[base_name])
    return merged


def _lenient_class_attrs(
    class_node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> set[str] | None:
    """The attributes a class's instances carry per THIS file:
    its own assigned/defined names plus in-file ancestors',
    leniently — an unknown base contributes nothing instead of
    exempting the class (the question here is only whether the
    file gives an attribute a home). None when any class in the
    in-file ancestry defines __getattr__ (dynamic resolution)."""
    assigned, defined = _class_own_facts(class_node)
    if "__getattr__" in defined:
        return None
    attrs = assigned | defined
    for base in class_node.bases:
        name = _base_name(base)
        if name in _KNOWN_BASE_ATTRS:
            attrs |= _KNOWN_BASE_ATTRS[name]
        elif (
            name in classes
            and name != class_node.name
            and name not in seen
        ):
            inherited = _lenient_class_attrs(
                classes[name], classes, seen | {class_node.name}
            )
            if inherited is None:
                return None
            attrs |= inherited
    return attrs


def _instance_attachments(
    tree: ast.Module, ctor_names: frozenset[str] | set[str]
) -> set[str]:
    """Attribute names assigned onto instances of the named
    classes, anywhere in the file: variables bound to a constructor
    call (``httpd = _PairingHTTPServer(...)`` then
    ``httpd.pairing_store = store``), and direct attachments
    through a ``.server`` hop (``self.server.x = ...``)."""
    variables: set[str] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        if isinstance(value, ast.Call) and _base_name(value.func) in ctor_names:
            for target in targets:
                if isinstance(target, ast.Name):
                    variables.add(target.id)
    attached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            holder = node.value
            if isinstance(holder, ast.Name) and holder.id in variables:
                attached.add(node.attr)
            elif isinstance(holder, ast.Attribute) and holder.attr == "server":
                attached.add(node.attr)
    return attached


def _is_server_class(
    class_node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """True when the class's ancestry, followed leniently through
    in-file classes, reaches a stdlib server base."""
    for base in class_node.bases:
        name = _base_name(base)
        if name in _KNOWN_SERVER_BASES:
            return True
        if (
            name in classes
            and name != class_node.name
            and name not in seen
            and _is_server_class(classes[name], classes, seen | {class_node.name})
        ):
            return True
    return False


def _has_handler_base(
    class_node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """True when the class's ancestry reaches a known request
    handler base (so ``self.server`` is the stdlib-assigned server
    object, not a coincidence of naming)."""
    for base in class_node.bases:
        name = _base_name(base)
        if name in _KNOWN_BASE_ATTRS:
            return True
        if (
            name in classes
            and name != class_node.name
            and name not in seen
            and _has_handler_base(classes[name], classes, seen | {class_node.name})
        ):
            return True
    return False


def _constructed_with(tree: ast.Module, arg_name: str) -> set[str]:
    """Class names constructed anywhere in the file with
    ``arg_name`` passed as an argument — the make_server shape:
    ``ThreadingHTTPServer((host, port), Handler)``."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            ctor = _base_name(node.func)
            if ctor is None:
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            for arg in args:
                if isinstance(arg, ast.Name) and arg.id == arg_name:
                    out.add(ctor)
                elif isinstance(arg, ast.Attribute) and arg.attr == arg_name:
                    out.add(ctor)
    return out


def _server_evidence(
    handler_node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    new_tree: ast.Module,
    old_server_attrs: set[str],
) -> set[str] | None:
    """The attributes a handler's ``self.server`` can resolve in
    this file, or None when the server's provenance is not in the
    file at all (nothing here constructs one) or an in-file server
    class resolves attributes dynamically."""
    server_class_names = {
        name for name, node in classes.items() if _is_server_class(node, classes)
    }
    constructed = _constructed_with(new_tree, handler_node.name)
    if not server_class_names and not constructed:
        return None
    evidence: set[str] = set(old_server_attrs) | set(_KNOWN_SERVER_ATTRS)
    for name in server_class_names:
        attrs = _lenient_class_attrs(classes[name], classes)
        if attrs is None:
            return None  # a dynamic server class: cannot verify
        evidence |= attrs
    evidence |= _instance_attachments(
        new_tree, server_class_names | constructed
    )
    return evidence


def _chain_violations(
    old_tree: ast.Module, new_tree: ast.Module
) -> list[str]:
    classes: dict[str, ast.ClassDef] = {}
    for node in ast.walk(new_tree):
        if isinstance(node, ast.ClassDef):
            classes.setdefault(node.name, node)
    if not classes:
        return []
    old_classes: dict[str, ast.ClassDef] = {}
    for node in ast.walk(old_tree):
        if isinstance(node, ast.ClassDef):
            old_classes.setdefault(node.name, node)
    old_chains: dict[str, set[tuple[str, ...]]] = {
        name: _class_self_chains(node) for name, node in old_classes.items()
    }
    old_loads: dict[str, set[str]] = {
        name: _class_self_loads(node) for name, node in old_classes.items()
    }

    resolved_cache: dict[str, set[str] | None] = {}
    violations: list[str] = []
    reported: set[tuple[str, tuple[str, ...]]] = set()

    for name, class_node in classes.items():
        resolvable_attrs = _resolvable_attrs(class_node, classes, resolved_cache)
        if resolvable_attrs is None:
            continue
        introduced = _class_self_chains(class_node) - old_chains.get(name, set())
        if not introduced:
            continue
        annotations = _inherited_lookup(
            class_node, classes, _class_attr_annotations
        )
        constructors = _inherited_lookup(
            class_node, classes, _class_attr_constructors
        )
        vocabulary = sorted(
            old_loads.get(name, set())
            | _class_own_facts(class_node)[0]
        )
        server_evidence: set[str] | None | str = "unset"
        for chain in sorted(introduced):
            first = chain[0]
            if first not in resolvable_attrs:
                continue  # the first-level check owns self.<a>
            # Resolve the object self.<first> denotes.
            current: str | None = None  # in-file class name
            on_server = False
            type_name = annotations.get(first) or constructors.get(first)
            if type_name in classes:
                current = type_name
            elif first == "server" and _has_handler_base(class_node, classes):
                on_server = True
            else:
                continue  # provenance not in this file: cannot verify
            for depth in range(1, len(chain)):
                leaf = chain[depth]
                prefix = chain[: depth + 1]
                if on_server:
                    if server_evidence == "unset":
                        old_server_attrs = {
                            c[1]
                            for c in old_chains.get(name, set())
                            if c[0] == "server" and len(c) >= 2
                        }
                        server_evidence = _server_evidence(
                            class_node, classes, new_tree, old_server_attrs
                        )
                    evidence = server_evidence
                    holder = "the server object"
                elif current is not None:
                    attrs = _lenient_class_attrs(classes[current], classes)
                    if attrs is None:
                        evidence = None
                    else:
                        evidence = attrs | _instance_attachments(
                            new_tree, {current}
                        )
                    holder = current
                else:
                    break  # deeper provenance unknown: stop judging
                if evidence is None:
                    break  # cannot verify this step
                if leaf in evidence:
                    # Step through: the next object is whatever this
                    # attribute's type is, when the file says.
                    if on_server:
                        on_server = False
                        current = None
                    elif current is not None:
                        next_type = _class_attr_annotations(
                            classes[current]
                        ).get(leaf) or _class_attr_constructors(
                            classes[current]
                        ).get(leaf)
                        current = next_type if next_type in classes else None
                    continue
                if (name, prefix) not in reported:
                    reported.add((name, prefix))
                    chain_text = "self." + ".".join(prefix)
                    hint = ""
                    matches = [
                        v
                        for v in vocabulary
                        if leaf == v
                        or leaf.endswith("_" + v)
                        or v.endswith("_" + leaf)
                    ]
                    if len(matches) == 1:
                        hint = (
                            f" — this class already reaches that role "
                            f"as self.{matches[0]}"
                        )
                    vocab_text = ", ".join(vocabulary[:12])
                    if len(vocabulary) > 12:
                        vocab_text += ", ..."
                    violations.append(
                        f"{chain_text} is referenced in class {name} "
                        f"but {leaf!r} has no home on {holder} in "
                        f"this file: it is not assigned on it, not "
                        f"defined by the class that provides it, and "
                        f"not part of its known surface — the edit "
                        f"introduces an attribute chain the file "
                        f"cannot resolve (this class's established "
                        f"self.* attributes include: {vocab_text})"
                        f"{hint}; spell the reference the way this "
                        f"file does, or give the attribute a home in "
                        f"this file as part of the edit"
                    )
                break  # one report per failing prefix is enough
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
