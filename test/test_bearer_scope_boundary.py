"""The local API bearer is paired only with URLs built on ``API_BASE_URL`` (#822).

``CAO_AUTH_LOCAL_TOKEN`` authenticates the client->API hop on THIS node. There
are several ``_auth_headers()`` helpers (one per module, so each module's tests
patch its own ``get_local_bearer``) and one shared decision, ``_is_local_api``,
behind the ``_auth_headers_for(base_url)`` companions. That is a convention,
and a convention does not stop the next remote-capable call site from writing
``requests.get(f"{base_url}...", headers=_auth_headers() or None)`` with a
``base_url`` that came from ``target_host``, which is exactly how the bearer
reached other hosts before.

So this test walks every ``requests.<verb>(...)`` call under ``src/`` with an
AST, the same way ``test_http_only_boundary.py`` polices imports, and fails
when an UNSCOPED helper is paired with a URL whose first component is not the
``API_BASE_URL`` name. A URL that starts with anything else must use
``_auth_headers_for(...)`` (any module's), send no ``headers``, or send headers
that do not come from a bearer helper.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"
VERBS = {"get", "post", "put", "patch", "delete", "head", "request"}
UNSCOPED = {"_auth_headers"}
SCOPED = {"_auth_headers_for", "_inbox_headers"}


def _is_requests_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in VERBS
        and isinstance(func.value, ast.Name)
        and func.value.id == "requests"
    )


def _called_names(expr: ast.AST):
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                yield f.id
            elif isinstance(f, ast.Attribute):
                yield f.attr


def _url_starts_with_api_base(url: ast.AST) -> bool:
    """True when the URL literally begins with ``API_BASE_URL`` (f-string or concat)."""
    if isinstance(url, ast.JoinedStr) and url.values:
        first = url.values[0]
        return isinstance(first, ast.FormattedValue) and _is_api_base_name(first.value)
    if isinstance(url, ast.BinOp) and isinstance(url.op, ast.Add):
        return _url_starts_with_api_base(url.left) or _is_api_base_name(url.left)
    return _is_api_base_name(url)


def _is_api_base_name(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "API_BASE_URL") or (
        isinstance(node, ast.Attribute) and node.attr == "API_BASE_URL"
    )


def _url_argument(node: ast.Call):
    """The URL expression: ``requests.<verb>(url, ...)`` or ``requests.request(method, url, ...)``."""
    for kw in node.keywords:
        if kw.arg == "url":
            return kw.value
    index = 1 if isinstance(node.func, ast.Attribute) and node.func.attr == "request" else 0
    return node.args[index] if len(node.args) > index else None


def _violations():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_requests_call(node)):
                continue
            headers = next((kw.value for kw in node.keywords if kw.arg == "headers"), None)
            if headers is None:
                continue
            names = set(_called_names(headers))
            if not (names & UNSCOPED) or (names & SCOPED):
                continue
            url = _url_argument(node)
            if url is None or not _url_starts_with_api_base(url):
                rel = path.relative_to(SRC.parent.parent)
                yield f"{rel}:{node.lineno}: unscoped bearer helper on a URL not built on API_BASE_URL"


def test_unscoped_bearer_helpers_only_reach_the_local_api():
    violations = list(_violations())
    assert violations == [], "\n" + "\n".join(violations)


def test_the_guard_sees_the_known_remote_capable_sites():
    """Sanity: the walker must find the sites this rule exists for, or a refactor
    that renamed ``requests`` (``import requests as r``) would silence it."""
    seen = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_requests_call(node):
                headers = next((kw.value for kw in node.keywords if kw.arg == "headers"), None)
                if headers is not None and set(_called_names(headers)) & SCOPED:
                    seen.add(path.name)
    assert {"orchestration.py", "server.py"} <= seen, seen
