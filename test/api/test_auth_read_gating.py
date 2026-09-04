"""Auth read-gating coverage for sensitive READ endpoints.

The auth layer is default-off (``is_auth_enabled()`` is True only when
``AUTH0_DOMAIN`` or ``CAO_AUTH_JWKS_URI`` is set), and ``require_any_scope``
returns the full scope set — never raising — while auth is disabled. That made
it easy for a previously-ungated READ endpoint to ship without a gate: behavior
is unchanged in the default config, so no test could catch the omission.

These tests pin BOTH halves of the fix:

* **Structural guard** — every sensitive read route now carries a
  ``require_any_scope(READ, WRITE, ADMIN)`` dependency in the live route table
  (the half that CANNOT be faked by the default-off posture), and
* **Enforcement** — with auth enabled (JWKS stubbed to an in-process key, the
  same hermetic pattern as ``test/security/test_auth.py``), a missing token is
  401, a token holding none of the cao: scopes is 403, and a ``cao:read`` token
  is admitted past the gate; with auth disabled every one of these endpoints
  still behaves exactly as before the change.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth

AUDIENCE = "cao-api"
ISSUER = "https://example.auth0.com/"

# A terminal id that passes the TerminalId path pattern (^[a-f0-9]{8}$).
TERMINAL_ID = "abcdef12"

# Every sensitive read endpoint gated by this change, as (method, route path).
# Literal paths (not derived from the route table) so a renamed/removed route
# fails the guard instead of silently passing it.
_GATED_ROUTES = [
    ("GET", "/agents/profiles"),
    ("GET", "/agents/profiles/{name}"),
    ("GET", "/agents/profiles/{name}/source"),
    ("GET", "/sessions"),
    ("GET", "/sessions/{session_name}"),
    ("GET", "/terminals/{terminal_id}"),
    ("GET", "/terminals/{terminal_id}/output"),
    ("GET", "/terminals/{terminal_id}/memory-context"),
    ("GET", "/terminals/{terminal_id}/inbox/messages"),
    ("GET", "/skills/{name}"),
    ("GET", "/flows"),
    ("GET", "/flows/{name}"),
    ("GET", "/workflows"),
    ("GET", "/workflows/{name}"),
    ("POST", "/workflows/validate"),
    ("GET", "/memory"),
    ("GET", "/memory/{key}"),
]


def _sample_requests():
    """Example (method, url, request-kwargs) for each gated route's HTTP tests."""
    return [
        ("GET", "/agents/profiles", {}),
        ("GET", "/agents/profiles/sample", {}),
        ("GET", "/agents/profiles/sample/source", {}),
        ("GET", "/sessions", {}),
        ("GET", "/sessions/sample-session", {}),
        ("GET", f"/terminals/{TERMINAL_ID}", {}),
        ("GET", f"/terminals/{TERMINAL_ID}/output", {}),
        ("GET", f"/terminals/{TERMINAL_ID}/memory-context", {}),
        ("GET", f"/terminals/{TERMINAL_ID}/inbox/messages", {}),
        ("GET", "/skills/sample", {}),
        ("GET", "/flows", {}),
        ("GET", "/flows/sample", {}),
        ("GET", "/workflows", {}),
        ("GET", "/workflows/sample", {}),
        ("POST", "/workflows/validate", {"json": {"path": "sample.yaml"}}),
        ("GET", "/memory", {}),
        ("GET", "/memory/sample-key", {}),
    ]


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_token(private_key, claims):
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def _base_claims(extra):
    now = datetime.now(timezone.utc)
    claims = {"aud": AUDIENCE, "iss": ISSUER, "exp": now + timedelta(hours=1), "iat": now}
    claims.update(extra)
    return claims


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._public_key)


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    """Default-safe: clear auth env vars + the JWKS cache between tests."""
    for var in (
        "AUTH0_DOMAIN",
        "CAO_AUTH_JWKS_URI",
        "CAO_AUTH_AUDIENCE",
        "AUTH0_AUDIENCE",
        "CAO_AUTH_LOCAL_TOKEN",
        "CAO_AUTH_ISSUER",
    ):
        monkeypatch.delenv(var, raising=False)
    auth.get_jwks_cache().clear()


def _enable_auth(monkeypatch, rsa_key):
    """Enable auth and route the JWKS cache to a fake client for rsa_key."""
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("CAO_AUTH_AUDIENCE", AUDIENCE)
    fake = _FakeClient(rsa_key.public_key())
    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: fake)


def _has_scope_dependency(route):
    """True if ``route`` carries a ``require_any_scope`` dependency in its tree."""
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None and "require_any_scope" in getattr(call, "__qualname__", ""):
            return True
        stack.extend(getattr(dep, "dependencies", []))
    return False


# --- structural guard ------------------------------------------------------


@pytest.mark.parametrize("method,path", _GATED_ROUTES)
def test_sensitive_read_route_is_scope_gated(method, path):
    """Each sensitive read route carries a require_any_scope dependency.

    This is the half that cannot be faked by the default-off posture: with auth
    disabled ``require_any_scope`` passes everyone, so a plain "still returns
    200" test stays green even if the dependency is missing. Asserting on the
    route table makes the guard real.
    """
    matches = [
        r
        for r in app.routes
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set())
    ]
    assert matches, f"{method} {path} is not registered"
    for route in matches:
        assert _has_scope_dependency(route), f"{method} {path} has no require_any_scope dependency"


# --- enforcement: auth enabled --------------------------------------------


@pytest.mark.parametrize("method,url,kwargs", _sample_requests())
def test_no_token_is_401_when_auth_enabled(client, monkeypatch, rsa_key, method, url, kwargs):
    """With auth enabled, a request with no bearer token is 401 on every gated read."""
    _enable_auth(monkeypatch, rsa_key)
    resp = getattr(client, method.lower())(url, **kwargs)
    assert resp.status_code == 401


@pytest.mark.parametrize("method,url,kwargs", _sample_requests())
def test_scopeless_token_is_403_when_auth_enabled(
    client, monkeypatch, rsa_key, method, url, kwargs
):
    """A token holding none of cao:read/write/admin is 403 on every gated read.

    403 (not 401) is the correct expectation: the token authenticates fine, but
    ``require_any_scope`` denies the missing authorization.
    """
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"scope": "cao:metrics"}))
    resp = getattr(client, method.lower())(
        url, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("method,url,kwargs", _sample_requests())
def test_read_token_admitted_when_auth_enabled(client, monkeypatch, rsa_key, method, url, kwargs):
    """A valid cao:read token passes the gate (never 401/403).

    The underlying service may return empty lists, 404s, or 500s depending on
    the test environment — the assertion that matters is that the read-scoped
    token is admitted past the auth boundary.
    """
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"scope": "cao:read"}))
    resp = getattr(client, method.lower())(
        url, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    assert resp.status_code not in (401, 403)


def test_wrong_audience_token_is_401_when_auth_enabled(client, monkeypatch, rsa_key):
    """A token for a different audience is rejected at the boundary (401)."""
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"aud": "someone-else", "scope": "cao:read"}))
    resp = client.get("/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_malformed_token_is_401_when_auth_enabled(client, monkeypatch, rsa_key):
    """A garbage bearer token is rejected at the boundary (401)."""
    _enable_auth(monkeypatch, rsa_key)
    resp = client.get("/sessions", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


# --- enforcement: auth disabled (behavior preserved) -----------------------


@pytest.mark.parametrize(
    "url", ["/sessions", "/memory", "/flows", f"/terminals/{TERMINAL_ID}/output"]
)
def test_auth_disabled_returns_200(client, monkeypatch, url):
    """With auth disabled (the default), the gated reads still return 200.

    Services are stubbed at the same seams the existing endpoint suites use so
    the response is deterministic (empty data) rather than environment-dependent.
    """
    if url == "/sessions":
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.list_sessions.return_value = []
            resp = client.get(url)
    elif url == "/flows":
        with patch("cli_agent_orchestrator.api.main.flow_service") as svc:
            svc.list_flows.return_value = []
            resp = client.get(url)
    elif url == "/memory":
        svc = MagicMock()
        svc.base_dir = "/tmp/memory"
        svc.recall = AsyncMock(return_value=[])
        with (
            patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=svc),
            patch(
                "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
                return_value=True,
            ),
        ):
            resp = client.get(url)
    else:
        with patch("cli_agent_orchestrator.api.main.terminal_service") as svc:
            svc.get_output.return_value = "hello"
            resp = client.get(url)
    assert resp.status_code == 200
