"""Every request to the local API carries the local bearer (#990).

``test_bearer_scope_boundary.py`` (#822) polices one half of the bearer rule:
an UNSCOPED ``_auth_headers()`` must never be paired with a URL that is not
built on ``API_BASE_URL``, so this node's token cannot leak to another node.
It deliberately skips any call with no ``headers=`` kwarg at all
(``if headers is None: continue``), because a call that sends nothing cannot
leak anything.

This module polices the other half, which that skip left open: a call to the
local API that sends NO bearer. Nearly every route the MCP server talks to
carries a ``require_any_scope`` dependency, so on an auth-enabled server a
bearer-less request gets ``401 missing bearer token``. Two of the call sites
#990 found were fail-closed authorization lookups
(``_require_discovery_marker`` and the ``_target_binding_denial`` fetch in
``_send_user_prompt_answer``), where a 401 is swallowed by the surrounding
``except`` and re-emitted as a refusal -- so auth turned into a denial of
function for exactly the callers the gate exists to admit, with no visible
transport error.

A convention does not stop the next tool from being added without
``headers=``; that is how twenty-plus sites accumulated before #822 and how
fourteen more survived it. So this walks the tree and fails on any
``requests.<verb>()`` whose URL is built on a base-URL name and whose headers
do not come from a bearer helper -- directly, or through a local variable
assigned from one. New exemptions go in ``ALLOWLIST`` with a reason; it is
expected to stay empty.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"

VERBS = {"get", "post", "put", "patch", "delete", "head", "request"}

#: Seed names that resolve to an ``Authorization: Bearer`` header dict.
#: ``_inbox_headers`` is here because it composes ``_auth_headers_for`` rather
#: than writing the header itself. The rest of the set is DISCOVERED from the
#: tree by :func:`_discover_bearer_helpers` -- there is one such helper per
#: module by design (each module's tests patch its own ``get_local_bearer``),
#: so a hardcoded list would go stale the moment a fifth module appears.
BEARER_HELPER_SEEDS = {"_inbox_headers"}

#: Names a URL expression may be built on that denote a CAO node's API root.
BASE_URL_NAMES = {"API_BASE_URL", "base_url", "base", "target_base_url"}

#: ``{"path:line": "reason"}`` for a local-API call that legitimately sends no
#: bearer. Expected empty: an unauthenticated route still accepts the header,
#: so "the route has no scope guard" is not a reason to omit it.
ALLOWLIST: dict[str, str] = {}


def _discover_bearer_helpers() -> set[str]:
    """Every function under ``src/`` that produces an ``Authorization`` header.

    A helper qualifies if its body mentions the literal ``"Authorization"``
    (``headers["Authorization"] = ...`` or a dict literal) or if it returns the
    result of another discovered helper. Two passes settle the second case
    (``_auth_headers_for`` -> ``_auth_headers``), which is as deep as the real
    chains go.
    """
    bodies: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Body only: the ``def <name>`` line would let a helper match itself
                # by substring (``elastic_worker_gateway_headers`` contains ``_headers``).
                bodies[node.name] = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
    found = {name for name, body in bodies.items() if "'Authorization'" in body}
    for _ in range(2):
        found |= {
            name
            for name, body in bodies.items()
            if any(helper in body for helper in found) and "header" in name.lower()
        }
    return found | BEARER_HELPER_SEEDS


BEARER_HELPERS = _discover_bearer_helpers()


def _is_requests_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in VERBS
        and isinstance(func.value, ast.Name)
        and func.value.id == "requests"
    )


def _url_argument(node: ast.Call):
    """``requests.<verb>(url, ...)`` or ``requests.request(method, url, ...)``."""
    for kw in node.keywords:
        if kw.arg == "url":
            return kw.value
    index = 1 if isinstance(node.func, ast.Attribute) and node.func.attr == "request" else 0
    return node.args[index] if len(node.args) > index else None


def _base_url_name(url: ast.AST) -> str | None:
    """The base-URL name this URL expression is built on, if any."""
    for sub in ast.walk(url):
        if isinstance(sub, ast.Name) and sub.id in BASE_URL_NAMES:
            return sub.id
        if isinstance(sub, ast.Attribute) and sub.attr in BASE_URL_NAMES:
            return sub.attr
    return None


def _called_names(expr: ast.AST):
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                yield f.id
            elif isinstance(f, ast.Attribute):
                yield f.attr


def _header_sources(path: Path, tree: ast.AST) -> dict[str, dict[str, set[str]]]:
    """Per enclosing function, ``name -> helper names its assignments call``.

    Several call sites hoist the headers into a local
    (``auth_headers = mcp_utils._auth_headers() or None``) and pass
    ``headers=auth_headers``; without this the walker would read that as a
    missing bearer.
    """
    scopes: dict[str, dict[str, set[str]]] = {}

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{scope}.{child.name}")
                continue
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if child.value is not None:
                    names = set(_called_names(child.value))
                    for target in targets:
                        if isinstance(target, ast.Name):
                            scopes.setdefault(scope, {}).setdefault(target.id, set()).update(names)
            visit(child, scope)

    visit(tree, "<module>")
    return scopes


def _enclosing_scopes(tree: ast.AST) -> dict[int, str]:
    """``id(node) -> dotted scope name`` for every node, so a call can find its locals."""
    out: dict[int, str] = {}

    def visit(node: ast.AST, scope: str) -> None:
        out[id(node)] = scope
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{scope}.{child.name}")
            else:
                visit(child, scope)

    visit(tree, "<module>")
    return out


def _violations():
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assigns = _header_sources(path, tree)
        scopes = _enclosing_scopes(tree)
        rel = path.relative_to(SRC.parent.parent)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_requests_call(node)):
                continue
            url = _url_argument(node)
            if url is None:
                continue
            base = _base_url_name(url)
            if base is None:
                continue
            key = f"{rel}:{node.lineno}"
            if key in ALLOWLIST:
                continue
            headers = next((kw.value for kw in node.keywords if kw.arg == "headers"), None)
            if headers is None:
                yield f"{key}: request to {{{base}}} sends no headers -- 401 when auth is enabled"
                continue
            names = set(_called_names(headers))
            # One hop of indirection: ``headers=auth_headers`` where
            # ``auth_headers = _auth_headers() or None`` earlier in the function.
            scope = scopes.get(id(node), "<module>")
            while scope:
                local = assigns.get(scope, {})
                for sub in ast.walk(headers):
                    if isinstance(sub, ast.Name) and sub.id in local:
                        names |= local[sub.id]
                scope = scope.rpartition(".")[0]
            if not (names & BEARER_HELPERS):
                yield (
                    f"{key}: request to {{{base}}} sends headers that do not come from a "
                    f"bearer helper ({sorted(names) or 'no helper call'}) -- "
                    f"401 when auth is enabled"
                )


def test_every_base_url_request_carries_a_bearer_helper():
    violations = list(_violations())
    assert violations == [], "\n" + "\n".join(violations)


def test_the_known_bearer_helpers_are_discovered():
    """Guard the guard: if discovery silently returned an empty set, every call
    site would look unauthenticated (noisy) -- but if it over-matched, every
    call site would look fine (silent). Pin the exact set of real helpers:
    every one of these writes or composes an ``Authorization`` header, and an
    extra name here (e.g. ``elastic_worker_gateway_headers``, which sends no
    bearer) would let a bearer-less call site pass the census."""
    assert BEARER_HELPERS == {
        "_auth_headers",
        "_auth_headers_for",
        "_build_headers",
        "_headers",
        "_inbox_headers",
        "auth_headers_for",
    }
    assert "_mcp_timeout" not in BEARER_HELPERS


def test_the_allowlist_is_empty():
    """An exemption needs a reason AND a maintainer; keep the list honest."""
    assert ALLOWLIST == {}, f"unexpected bearer exemptions: {ALLOWLIST}"


def test_the_census_finds_the_sites_it_polices():
    """Sanity: if a refactor renamed ``requests`` or ``API_BASE_URL``, the walker
    would pass by finding nothing. Assert it still sees the known population."""
    seen = 0
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_requests_call(node):
                url = _url_argument(node)
                if url is not None and _base_url_name(url) is not None:
                    seen += 1
    assert seen >= 50, f"census found only {seen} base-URL call sites; the walker is likely blind"


def test_the_census_catches_a_stripped_header():
    """Prove the guard bites: synthesise the shape #990 found and re-run the rule."""
    snippet = (
        "import requests\n"
        "API_BASE_URL = 'http://x'\n"
        "def f(terminal_id):\n"
        "    return requests.get(f'{API_BASE_URL}/terminals/{terminal_id}', timeout=5)\n"
    )
    tree = ast.parse(snippet)
    offenders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_requests_call(node)
        and _url_argument(node) is not None
        and _base_url_name(_url_argument(node)) is not None
        and not any(kw.arg == "headers" for kw in node.keywords)
    ]
    assert len(offenders) == 1, "the rule no longer recognises a bearer-less local API call"
