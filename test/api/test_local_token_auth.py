"""Standalone ``CAO_AUTH_LOCAL_TOKEN`` enforcement at the HTTP boundary (issue #706).

With no IdP configured the auth layer used to be inert regardless of
``CAO_AUTH_LOCAL_TOKEN`` — the variable was only read once ``AUTH0_DOMAIN`` /
``CAO_AUTH_JWKS_URI`` had already switched auth on — while the reference docs
described it as a working local bearer token. Setting it on its own now enables
a local-token mode in which every scope-gated route requires that exact value as
a bearer and anything else is 401.

These tests drive the real FastAPI app through ``TestClient`` so the dependency
wiring (``Depends(require_any_scope(...))`` -> ``get_current_scopes``) is what is
under test, not the auth module in isolation (``test/security/test_auth.py``
covers that). Admitted requests may still 404/500 downstream depending on the
environment; the assertion that matters is that they are not refused at the
auth boundary.
"""

from contextlib import nullcontext
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.security import auth

LOCAL_TOKEN = "s3cret-local-token"

# A terminal id that passes the TerminalId path pattern (^[a-f0-9]{8}$).
TERMINAL_ID = "abcdef12"

# One read-gated route per surface family plus one POST that is read-gated
# (``/workflows/validate`` writes nothing). Same sample set as
# ``test_auth_read_gating.py`` so the two suites stay comparable.
_ROUTES = [
    ("GET", "/sessions", {}),
    ("GET", f"/terminals/{TERMINAL_ID}", {}),
    ("GET", f"/terminals/{TERMINAL_ID}/output", {}),
    ("GET", "/flows", {}),
    ("GET", "/workflows", {}),
    ("POST", "/workflows/validate", {"json": {"path": "sample.yaml"}}),
    ("GET", "/memory", {}),
]


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    """Default-safe: clear every auth variable + the JWKS cache between tests."""
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


@pytest.fixture
def local_token_mode(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", LOCAL_TOKEN)
    assert auth.is_local_token_mode()
    return LOCAL_TOKEN


# --- enforcement ------------------------------------------------------------


@pytest.mark.parametrize("method,url,kwargs", _ROUTES)
def test_missing_token_is_401(client, local_token_mode, method, url, kwargs):
    """No bearer → 401 with a ``WWW-Authenticate: Bearer`` challenge."""
    resp = getattr(client, method.lower())(url, **kwargs)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("method,url,kwargs", _ROUTES)
def test_wrong_token_is_401(client, local_token_mode, method, url, kwargs):
    """A bearer that is not the configured value fails closed → 401."""
    resp = getattr(client, method.lower())(
        url, headers={"Authorization": "Bearer not-the-token"}, **kwargs
    )
    assert resp.status_code == 401
    assert LOCAL_TOKEN not in resp.text


@pytest.mark.parametrize("method,url,kwargs", _ROUTES)
def test_matching_token_is_admitted(client, local_token_mode, method, url, kwargs):
    """The configured value passes the boundary (never 401/403).

    ``TestClient`` re-raises a handler exception instead of turning it into a
    response, so the one sampled route whose handler needs a populated database
    (``GET /workflows`` reads the ``workflow_index`` table) is stubbed at the
    service it calls: this test is about the auth boundary, not the index.
    """
    downstream = (
        patch(
            "cli_agent_orchestrator.services.workflow_spec_service.list_workflows", return_value=[]
        )
        if url == "/workflows"
        else nullcontext()
    )
    with downstream:
        resp = getattr(client, method.lower())(
            url, headers={"Authorization": f"Bearer {LOCAL_TOKEN}"}, **kwargs
        )
    assert resp.status_code not in (401, 403)


def test_query_parameter_is_not_a_bearer_for_http(client, local_token_mode):
    """Only the WebSocket / SSE handshakes accept a query-string token (browsers
    cannot set headers there). Plain HTTP routes take the header only, so a
    token in the URL must not authenticate — and must not end up in access logs
    as if it did."""
    resp = client.get(f"/sessions?token={LOCAL_TOKEN}")
    assert resp.status_code == 401


def test_prm_endpoint_is_404_in_local_token_mode(client, local_token_mode):
    """RFC 9728 metadata advertises an OAuth authorization server. Local-token
    mode has none, so the endpoint stays 404; the detail names what is missing
    rather than claiming auth is off, because it is not."""
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no OAuth authorization server configured"}


def test_prm_endpoint_default_off_response_is_unchanged(client):
    """Default-off must stay byte-for-byte what it was before local-token mode
    existed: 404 with the original detail text."""
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "auth disabled"}


# --- AG-UI stream -----------------------------------------------------------


class TestAguiStreamLocalTokenMode:
    """The SSE surface reads ``?access_token=`` (EventSource cannot set headers)
    and must enforce the local token like every other boundary. A mutation that
    skipped the AG-UI check in local-token mode only survived every other test in
    this PR (found by the #706 review), so these pin it directly. ``since=invalid``
    is the tell: a 400 for the bad timestamp proves the request got PAST auth."""

    @pytest.fixture(autouse=True)
    def _agui_on(self, monkeypatch):
        monkeypatch.setenv("CAO_AGUI_ENABLED", "true")

    def test_missing_access_token_is_401(self, client, local_token_mode):
        resp = client.get("/agui/v1/stream", params={"since": "invalid"})
        assert resp.status_code == 401

    def test_wrong_access_token_is_401(self, client, local_token_mode):
        resp = client.get("/agui/v1/stream", params={"since": "invalid", "access_token": "nope"})
        assert resp.status_code == 401

    def test_header_bearer_does_not_substitute_for_the_query_token(self, client, local_token_mode):
        resp = client.get(
            "/agui/v1/stream",
            params={"since": "invalid"},
            headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
        )
        assert resp.status_code == 401

    def test_matching_access_token_passes_auth(self, client, local_token_mode):
        resp = client.get(
            "/agui/v1/stream", params={"since": "invalid", "access_token": LOCAL_TOKEN}
        )
        assert resp.status_code == 400  # past auth; rejected on the timestamp, not the token


# --- default-off unchanged ---------------------------------------------------


def test_default_off_still_admits_without_token(client):
    """With none of the auth variables set the posture is unchanged: no token,
    no challenge."""
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.list_sessions.return_value = []
        resp = client.get("/sessions")
    assert resp.status_code == 200
    assert "WWW-Authenticate" not in resp.headers


def test_default_off_ignores_a_presented_token(client):
    """Default-off never inspects the request, so an arbitrary bearer is harmless."""
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.list_sessions.return_value = []
        resp = client.get("/sessions", headers={"Authorization": "Bearer whatever"})
    assert resp.status_code == 200
