"""H4 — scope coverage across mutating routes.

Two layers of assurance:

* a **guard test** that enumerates the live FastAPI route table and asserts every
  mutating route (POST/PUT/PATCH/DELETE) carries a ``require_any_scope``
  dependency, so a future route cannot silently regress the coverage;
* **enforcement tests** that, with auth enabled, a ``cao:read`` token is 403'd on
  a write route and a ``cao:write`` token is 403'd on an admin (delete) route,
  while the matching scope is admitted past the dependency.

Default-off behavior (the dependency returns the full scope set and enforces
nothing) is covered by the existing endpoint suites, which exercise these routes
with no auth configured.
"""

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth

# Mutating HTTP methods that must be scope-gated when present on a route.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that use a mutating verb but perform no state change, so they are
# intentionally not scope-gated. ``POST /workflows/validate`` only parses and
# validates a spec file (read-only), mirroring a GET.
_EXEMPT = {("POST", "/workflows/validate")}


def _has_scope_dependency(route) -> bool:
    """True if ``route`` has a ``require_any_scope`` dependency anywhere in its tree."""
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None and "require_any_scope" in getattr(call, "__qualname__", ""):
            return True
        stack.extend(getattr(dep, "dependencies", []))
    return False


def _mutating_routes():
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        mutating = methods & _MUTATING_METHODS
        if not mutating:
            continue
        yield route, mutating


def test_every_mutating_route_is_scope_gated():
    """No mutating route may be missing a scope dependency (regression guard)."""
    missing = []
    for route, mutating in _mutating_routes():
        if any((m, route.path) in _EXEMPT for m in mutating):
            continue
        if not _has_scope_dependency(route):
            missing.append(f"{sorted(mutating)} {route.path}")
    assert not missing, "mutating routes missing a require_any_scope dependency: " + ", ".join(
        missing
    )


def _override_scopes(scopes):
    async def _dep():
        return list(scopes)

    return _dep


@pytest.fixture
def auth_on(monkeypatch):
    """Enable the auth layer for enforcement tests."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)


def test_read_token_forbidden_on_write_route(client, auth_on):
    """A cao:read token is 403'd on a write-gated route (POST /settings/skill-dirs)."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_READ])
    resp = client.post("/settings/skill-dirs", json={"extra_dirs": []})
    assert resp.status_code == 403


def test_write_token_admitted_on_write_route(client, auth_on):
    """A cao:write token passes the dependency on a write-gated route (not 403)."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_WRITE])
    resp = client.post("/settings/skill-dirs", json={"extra_dirs": []})
    assert resp.status_code != 403


def test_write_token_forbidden_on_admin_route(client, auth_on):
    """A cao:write token is 403'd on an admin (delete) route (DELETE /memory/{key})."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_WRITE])
    resp = client.delete("/memory/some-key")
    assert resp.status_code == 403


def test_admin_token_admitted_on_admin_route(client, auth_on):
    """A cao:admin token passes the admin-gated dependency (not 403)."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_ADMIN])
    resp = client.delete("/memory/some-key")
    assert resp.status_code != 403
