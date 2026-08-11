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
# validates a spec file (read-only), mirroring a GET. ``/agents/profiles/templates/validate``
# and ``/agents/profiles/templates/preview`` are POSTs for the same reason — their config
# travels in a JSON body — and mutate nothing (schema check / template render).
# ``/agents/profiles/validate`` is the same shape: the profile content travels in
# a JSON body and is checked against the profile schema without being persisted.
_EXEMPT = {
    ("POST", "/workflows/validate"),
    ("POST", "/agents/profiles/templates/validate"),
    ("POST", "/agents/profiles/templates/preview"),
    ("POST", "/agents/profiles/validate"),
}


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


# --------------------------------------------------------------------------- #
# Disclosure-bearing GET routes.
#
# The mutating-route guard above cannot see the failure mode the agent-plugins
# adoption audit found (R2): `GET /plugins` shipped with no scope dependency while
# disclosing every plugin's source path plus the terminal IDs, session names,
# profile names and skill names of running work. Nothing enumerated GETs, so
# nothing caught it.
#
# Gating all 28 pre-existing ungated reads is NOT the fix — it would change the
# auth posture of shipped routes and could break existing unauthenticated readers,
# the same trade-off recorded for the `/workflows` reads below. So the guard
# inverts the default for GETs and pins today's state as data: a GET route must
# either carry a scope dependency or appear in `_OPEN_READS`. A new route is
# gated by default, and opening one becomes a visible, reviewable diff to this
# list rather than an omission nobody sees.
#
# `/plugins` is deliberately ABSENT from this list: it is gated.
# --------------------------------------------------------------------------- #
_OPEN_READS = {
    # Protocol/discovery surfaces that must answer before a caller can hold a
    # token at all, and CAO's liveness probe.
    "/.well-known/oauth-protected-resource",
    "/health",
    # Agent profile and provider catalogs. Names and descriptions of installable
    # profiles, plus which provider binaries are present.
    "/agents/profiles",
    "/agents/profiles/schema",
    "/agents/profiles/search",
    "/agents/profiles/templates",
    "/agents/profiles/templates/{category}/{name}/schema",
    "/agents/profiles/{name}",
    "/agents/providers",
    # AG-UI event stream; carries its own auth story.
    "/agui/v1/stream",
    # Flow definitions.
    "/flows",
    "/flows/{name}",
    # Memory reads. Gated writes, open reads — pre-existing asymmetry.
    "/memory",
    "/memory/{key}",
    "/settings/memory",
    "/settings/skill-dirs",
    # Skill content.
    "/skills/{name}",
    # Live session and terminal state. The closest siblings to `/plugins`, and the
    # strongest candidates for a future gate: they disclose exactly the live
    # operational detail that motivated gating `/plugins`. Left open here only
    # because they are shipped routes with existing readers.
    "/sessions",
    "/sessions/{session_name}",
    "/sessions/{session_name}/terminals",
    "/terminals/{terminal_id}",
    "/terminals/{terminal_id}/inbox/messages",
    "/terminals/{terminal_id}/memory-context",
    "/terminals/{terminal_id}/output",
    "/terminals/{terminal_id}/working-directory",
    # Workflow reads. `/workflows/runs` and `/workflows/runs/{run_id}/result` are
    # gated (see the #505 block below); these three siblings are not, deliberately.
    "/workflows",
    "/workflows/runs/{run_id}",
    "/workflows/{name}",
}


def _api_get_routes():
    """Every GET route that FastAPI resolved a dependency tree for.

    Skips the routes Starlette mounts itself — ``/docs``, ``/redoc``,
    ``/openapi.json``, ``/docs/oauth2-redirect`` — which have no ``dependant`` and
    are not application endpoints.
    """
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        if getattr(route, "dependant", None) is None:
            continue
        yield route


def test_every_disclosure_bearing_get_route_is_gated_or_explicitly_open():
    """A GET route is scope-gated unless it is listed as deliberately open.

    The assertion is one-directional on purpose: it fails for a *new* ungated GET,
    not for one that becomes gated. Tightening a route should never require
    editing a test to permit it.
    """
    unlisted = [
        route.path
        for route in _api_get_routes()
        if not _has_scope_dependency(route) and route.path not in _OPEN_READS
    ]
    assert not unlisted, (
        "ungated GET route(s) not listed in _OPEN_READS: "
        + ", ".join(sorted(unlisted))
        + ". Add a scope dependency, or add the path to _OPEN_READS with a comment "
        "saying what it discloses and why that is acceptable."
    )


def test_the_open_reads_list_has_no_stale_entries():
    """Keeps `_OPEN_READS` honest in the other direction.

    Without this, a path that was gated (or deleted) would linger in the list and
    silently pre-authorize a *future* route that happened to reuse the path. This
    test is why gating a route requires removing it from the list — which is the
    reviewable diff the list exists to produce.
    """
    registered_ungated = {
        route.path for route in _api_get_routes() if not _has_scope_dependency(route)
    }
    stale = sorted(_OPEN_READS - registered_ungated)
    assert not stale, (
        "_OPEN_READS lists path(s) that are no longer ungated GET routes: "
        + ", ".join(stale)
        + ". Remove them — a stale entry would pre-authorize a future route reusing the path."
    )


def test_plugins_list_is_gated_and_not_exempted():
    """`GET /plugins` specifically — the route the audit found ungated (R2).

    Named rather than left to the generic guard because the generic guard would
    also pass if someone added `/plugins` to `_OPEN_READS`, and that would be
    exactly the regression. This asserts the route carries the dependency AND that
    the exemption list does not mention it.
    """
    matches = [route for route in _api_get_routes() if route.path == "/plugins"]
    assert matches, "GET /plugins is not registered"
    assert _has_scope_dependency(matches[0]), "GET /plugins lost its scope dependency"
    assert "/plugins" not in _OPEN_READS, "GET /plugins must not be exempted from the read floor"


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


# --------------------------------------------------------------------------- #
# PR #525 review — the two NEW #505 run-read routes carry a read-scope gate.
#
# Scoped deliberately to the two routes issue #505 ADDED. The three pre-existing
# sibling reads (``GET /workflows``, ``GET /workflows/{name}``,
# ``GET /workflows/runs/{run_id}``) are equally ungated and are left alone: gating
# them would change the auth posture of shipped routes and could break an existing
# unauthenticated reader, which is a bigger risk than the residual asymmetry.
# --------------------------------------------------------------------------- #
_NEW_505_READ_ROUTES = [
    ("GET", "/workflows/runs"),
    ("GET", "/workflows/runs/{run_id}/result"),
]


@pytest.mark.parametrize("method,path", _NEW_505_READ_ROUTES)
def test_new_505_read_routes_declare_a_scope_dependency(method, path):
    """Structural guard: the dependency is present on the route object.

    This is the half that CANNOT be faked by ambient config. ``is_auth_enabled()`` is
    default-off (true only when ``AUTH0_DOMAIN`` or ``CAO_AUTH_JWKS_URI`` is set) and
    ``require_any_scope`` hands back the full scope set when auth is off — so a plain
    "the route still returns 200" test passes whether or not the dependency exists at
    all. Asserting on the route table instead makes the guard real.
    """
    matches = [
        r
        for r in app.routes
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set())
    ]
    assert matches, f"{method} {path} is not registered"
    assert _has_scope_dependency(matches[0]), f"{method} {path} has no require_any_scope dependency"


def test_scopeless_token_forbidden_on_run_list(client, auth_on):
    """Enforcement: a token holding none of read/write/admin is 403'd on the run list.

    403 (not 401) is the correct expectation here: ``require_any_scope`` itself raises
    403 for a token that authenticated but lacks the scope, while 401 comes from
    ``get_current_scopes`` upstream on a missing/invalid token.
    """
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([])
    resp = client.get("/workflows/runs")
    assert resp.status_code == 403


def test_scopeless_token_forbidden_on_run_result(client, auth_on):
    """Enforcement: same for the result route, which exposes per-step output blobs."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([])
    resp = client.get("/workflows/runs/whatever/result")
    assert resp.status_code == 403


def test_read_token_admitted_on_run_list(client, auth_on):
    """A cao:read token PASSES the gate (not 403) — the point of including SCOPE_READ.

    Guards the over-restriction failure mode: gating these reads on write/admin only
    would lock out exactly the read-only callers they exist for.
    """
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_READ])
    resp = client.get("/workflows/runs")
    assert resp.status_code != 403


def test_write_token_still_admitted_on_run_list(client, auth_on):
    """A cao:write token keeps working — existing write-scoped callers must not break."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_WRITE])
    resp = client.get("/workflows/runs")
    assert resp.status_code != 403
