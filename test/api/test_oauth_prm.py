"""Integration tests for the OAuth 2.1 / RFC 9728 layer (task 10.5).

Covers the Protected Resource Metadata endpoint (schema when enabled, 404 when
disabled), the enabled-auth 401 flow on a routed mutation endpoint, a valid
token passing the FastAPI boundary, and the ``@requires_scopes`` RBAC matrix.

All tests are default-safe: the auth-enabling env vars are cleared by an
autouse fixture so an enabled test never leaks into the default-off suite.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.security import auth, decorators


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
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


def _enable(monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.auth0.com")
    monkeypatch.setenv("CAO_AUTH_AUDIENCE", "cao-api")


# --- PRM endpoint ---------------------------------------------------------


@pytest.mark.integration
def test_prm_returns_404_when_disabled(client) -> None:
    """Default-off: the PRM endpoint is absent (404), posture unchanged."""

    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 404


@pytest.mark.integration
def test_prm_schema_when_enabled(client, monkeypatch) -> None:
    """When enabled, PRM advertises resource, auth servers, scopes, bearer methods."""

    _enable(monkeypatch)
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "cao-api"
    assert body["authorization_servers"] == ["https://tenant.auth0.com/"]
    assert body["scopes_supported"] == ["cao:read", "cao:write", "cao:admin"]
    assert body["bearer_methods_supported"] == ["header"]


# --- enabled-auth 401 / valid-token flow on a routed mutation endpoint ----


@pytest.mark.integration
def test_mutation_requires_token_when_enabled_401(client, monkeypatch) -> None:
    """With auth enabled, a routed mutation with no bearer token yields 401."""

    _enable(monkeypatch)
    # DELETE /sessions/{name} carries Depends(get_current_scopes).
    response = client.delete("/sessions/cao-x")
    assert response.status_code == 401


@pytest.mark.integration
def test_mutation_accepts_valid_token_when_enabled(client, monkeypatch) -> None:
    """A valid bearer token passes the FastAPI boundary and reaches the handler."""

    _enable(monkeypatch)
    # Bypass JWKS by stubbing scope extraction for this token.
    monkeypatch.setattr(auth, "extract_scopes_from_token", lambda t: ["cao:admin"])
    with patch(
        "cli_agent_orchestrator.api.main.session_service.delete_session",
        return_value={"deleted": True},
    ):
        response = client.delete("/sessions/cao-x", headers={"Authorization": "Bearer good-token"})
    assert response.status_code == 200
    assert response.json()["success"] is True


# --- @requires_scopes RBAC matrix -----------------------------------------


@pytest.mark.integration
def test_requires_scopes_default_off_always_passes() -> None:
    """Default-off (full scope set) -> the decorator never blocks."""

    @decorators.requires_scopes("cao:admin")
    def _impl() -> dict:
        return {"success": True, "ran": True}

    # autouse fixture cleared env => auth disabled => full set granted.
    assert _impl() == {"success": True, "ran": True}


@pytest.mark.integration
def test_requires_scopes_rbac_matrix() -> None:
    """The decorator allows when the scope is granted and blocks when absent."""

    @decorators.requires_scopes("cao:admin")
    def _admin_impl() -> dict:
        return {"success": True}

    # granted set lacks cao:admin -> blocked
    with patch.object(decorators, "get_scopes_for_local_token", return_value=["cao:read"]):
        denied = _admin_impl()
    assert denied == {"success": False, "error": "scope cao:admin required"}

    # granted set includes cao:admin -> allowed
    with patch.object(
        decorators, "get_scopes_for_local_token", return_value=["cao:write", "cao:admin"]
    ):
        allowed = _admin_impl()
    assert allowed == {"success": True}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_requires_scopes_async_impl() -> None:
    """The decorator supports async tool impls too."""

    @decorators.requires_scopes("cao:write")
    async def _impl() -> dict:
        return {"success": True}

    with patch.object(decorators, "get_scopes_for_local_token", return_value=["cao:read"]):
        assert await _impl() == {"success": False, "error": "scope cao:write required"}
    with patch.object(decorators, "get_scopes_for_local_token", return_value=["cao:write"]):
        assert await _impl() == {"success": True}
