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


# ---------------------------------------------------------------------------
# PR 526 review — SHOULD-FIX: the diagnostics bundle must be scope-gated.
#
# GET /workflows/runs/{id}/diagnostics returns the run's `inputs` (its raw
# inputs_json, passed through sanitize_output — which is transport hygiene, NOT
# secret redaction, so a credential passed as a workflow input comes back
# verbatim) plus capture-gated output excerpts. It had NO require_any_scope
# dependency, so with auth enabled ANY valid token could export it.
# ---------------------------------------------------------------------------
def test_diagnostics_route_is_scope_gated():
    """The wiring guard: the diagnostics route carries a require_any_scope dep."""
    routes = [
        r for r in app.routes if getattr(r, "path", None) == "/workflows/runs/{run_id}/diagnostics"
    ]
    assert routes, "diagnostics route not found in the route table"
    for route in routes:
        assert _has_scope_dependency(route), "diagnostics route lost its scope gate"


def test_unscoped_token_forbidden_on_diagnostics(client, auth_on):
    """A token carrying NO recognized scope is 403'd on the diagnostics export."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([])
    resp = client.get("/workflows/runs/r1/diagnostics")
    assert resp.status_code == 403


def test_read_token_admitted_on_diagnostics(client, auth_on):
    """A cao:read token passes the dependency (404 for an unknown run, not 403)."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_READ])
    resp = client.get("/workflows/runs/r1/diagnostics")
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# PR 526 human review — BLOCKING: every payload-bearing run READ route must be
# scope-gated, not just /diagnostics.
#
# The review's point: GET /workflows/runs/{run_id} (inspect) returns every step's
# full `output_json` and `error` text — strictly MORE payload than /diagnostics,
# whose excerpts are capture-gated — and GET .../events and GET .../compare carry
# error_kind / reason / validation_result / output_ref and terminal-offset
# coordinates. All three shipped with no require_any_scope dependency while
# /diagnostics was deliberately gated, so the most payload-bearing read route
# escaped the PR's own scope model.
#
# Table-driven (not one test per route) so a future run read route added without
# a gate fails here. The expected paths are hard-coded literals rather than
# derived from the route table — a fixture sourced from the value under test
# would stay green if a route were renamed or dropped.
#
# SCOPE OF THIS CLAIM (PR #526 review fix cycle 1): this list is the set of
# payload-bearing read routes #504 OWNS, not an exhaustive inventory of every
# route that can return captured content. Specifically it does NOT include
# ``GET /terminals/{id}/output`` — that route predates #504 and the wider
# ``/terminals/*`` surface is uniformly ungated, so gating one pre-existing member
# of it is a separate, deliberate decision about that whole surface rather than
# part of this PR. ``GET /terminals/{id}/output/range`` IS included: #504 added it.
# Do not read a passing run here as "every content-returning route is gated."
# ---------------------------------------------------------------------------
_GATED_RUN_READ_ROUTES = [
    "/workflows/runs/{run_id}",
    "/workflows/runs/{run_id}/events",
    "/workflows/runs/{run_id}/compare",
    "/workflows/runs/{run_id}/diagnostics",
    "/terminals/{terminal_id}/output/range",
]


def _get_routes_for_path(path: str):
    """Every GET route registered at exactly ``path``."""
    return [
        r
        for r in app.routes
        if getattr(r, "path", None) == path and "GET" in (getattr(r, "methods", None) or set())
    ]


@pytest.mark.parametrize("path", _GATED_RUN_READ_ROUTES)
def test_payload_bearing_run_read_route_is_scope_gated(path):
    """Each payload-bearing run read route carries a require_any_scope dependency."""
    routes = _get_routes_for_path(path)
    assert routes, f"GET {path} not found in the route table"
    for route in routes:
        assert _has_scope_dependency(route), f"GET {path} is missing its scope gate"


@pytest.mark.parametrize("url", ["/workflows/runs/r1", "/workflows/runs/r1/events"])
def test_unscoped_token_forbidden_on_run_read_routes(client, auth_on, url):
    """A token carrying NO recognized scope is 403'd on inspect and events."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([])
    resp = client.get(url)
    assert resp.status_code == 403


def test_unscoped_token_forbidden_on_compare(client, auth_on):
    """A token carrying NO recognized scope is 403'd on compare.

    Separate from the parametrized pair because ``?against=`` is required: without
    it FastAPI would 422 on validation and never reach the scope dependency, so
    the 403 would prove nothing about the gate.
    """
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([])
    resp = client.get("/workflows/runs/r1/compare", params={"against": "r2"})
    assert resp.status_code == 403


@pytest.mark.parametrize("url", ["/workflows/runs/r1", "/workflows/runs/r1/events"])
def test_read_token_admitted_on_run_read_routes(client, auth_on, url):
    """A cao:read token passes the gate on inspect and events (404/200, never 403)."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_READ])
    resp = client.get(url)
    assert resp.status_code != 403
