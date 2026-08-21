"""Unit tests for the default-off OAuth 2.1 auth core (task 10.2).

Covers scope extraction across the scope/permissions/scp claim variants, the
JWKS cache TTL / reuse-on-unreachable / fetch-when-empty behavior, 401 mapping
on invalid/expired tokens, and the default-off full-scope-set guarantee.
"""

import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from cli_agent_orchestrator.security import auth

AUDIENCE = "cao-api"
ISSUER = "https://example.auth0.com/"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_token(private_key, claims: dict) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _FakeClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: ANN001
        return _FakeSigningKey(self._public_key)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
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


# --- default-off ----------------------------------------------------------


def test_default_off_returns_full_scope_set():
    assert auth.is_auth_enabled() is False
    assert auth.get_scopes_for_local_token() == auth.FULL_SCOPE_SET
    assert auth.extract_scopes_from_token("anything") == auth.FULL_SCOPE_SET


def test_jwks_uri_resolution(monkeypatch):
    assert auth.get_jwks_uri() is None
    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.auth0.com")
    assert auth.get_jwks_uri() == "https://tenant.auth0.com/.well-known/jwks.json"
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    # generic URI takes precedence
    assert auth.get_jwks_uri() == "https://idp.example/jwks"


# --- scope extraction across claim variants -------------------------------


def _base_claims(extra: dict) -> dict:
    now = datetime.now(timezone.utc)
    claims = {"aud": AUDIENCE, "iss": ISSUER, "exp": now + timedelta(hours=1), "iat": now}
    claims.update(extra)
    return claims


def test_scope_claim_space_delimited(monkeypatch, rsa_key):
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"scope": "cao:read cao:write"}))
    assert auth.extract_scopes_from_token(token) == ["cao:read", "cao:write"]


def test_permissions_list_claim(monkeypatch, rsa_key):
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"permissions": ["cao:admin"]}))
    assert auth.extract_scopes_from_token(token) == ["cao:admin"]


def test_scp_claim_variant(monkeypatch, rsa_key):
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"scp": ["cao:read", "cao:admin"]}))
    assert auth.extract_scopes_from_token(token) == ["cao:read", "cao:admin"]


# --- invalid / expired tokens -> raise (mapped to 401 by get_current_scopes) --


def test_expired_token_raises(monkeypatch, rsa_key):
    _enable_auth(monkeypatch, rsa_key)
    now = datetime.now(timezone.utc)
    token = _make_token(
        rsa_key,
        {"aud": AUDIENCE, "exp": now - timedelta(hours=1), "iat": now - timedelta(hours=2)},
    )
    with pytest.raises(jwt.PyJWTError):
        auth.extract_scopes_from_token(token)


def test_wrong_audience_raises(monkeypatch, rsa_key):
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"aud": "someone-else", "scope": "cao:read"}))
    with pytest.raises(jwt.PyJWTError):
        auth.extract_scopes_from_token(token)


# --- issuer pinning (M1) ---------------------------------------------------


def test_missing_issuer_raises(monkeypatch, rsa_key):
    """With an issuer configured, a token lacking ``iss`` is rejected."""
    _enable_auth(monkeypatch, rsa_key)
    now = datetime.now(timezone.utc)
    token = _make_token(
        rsa_key,
        {"aud": AUDIENCE, "exp": now + timedelta(hours=1), "iat": now, "scope": "cao:read"},
    )
    with pytest.raises(jwt.PyJWTError):
        auth.extract_scopes_from_token(token)


def test_wrong_issuer_raises(monkeypatch, rsa_key):
    """A token minted by a different issuer is rejected even if signed/aud-valid."""
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(
        rsa_key, _base_claims({"iss": "https://evil.example/", "scope": "cao:read"})
    )
    with pytest.raises(jwt.PyJWTError):
        auth.extract_scopes_from_token(token)


# --- audience fallback to API_BASE_URL (M2) -------------------------------


def test_expected_audience_none_when_disabled():
    assert auth.get_expected_audience() is None


def test_expected_audience_defaults_to_api_base_url_when_enabled(monkeypatch):
    """When auth is enabled but no audience env is set, the resource id is used."""
    from cli_agent_orchestrator.constants import API_BASE_URL

    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.auth0.com")
    assert auth.get_expected_audience() == API_BASE_URL


def test_audience_fallback_enforced_in_validation(monkeypatch, rsa_key):
    """Enabling via AUTH0_DOMAIN alone still verifies audience (no silent-off)."""
    from cli_agent_orchestrator.constants import API_BASE_URL

    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")  # no audience env
    fake = _FakeClient(rsa_key.public_key())
    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: fake)
    now = datetime.now(timezone.utc)

    good = _make_token(
        rsa_key,
        {
            "aud": API_BASE_URL,
            "iss": ISSUER,
            "exp": now + timedelta(hours=1),
            "iat": now,
            "scope": "cao:read",
        },
    )
    assert auth.extract_scopes_from_token(good) == ["cao:read"]

    bad = _make_token(
        rsa_key,
        {
            "aud": "other-resource",
            "iss": ISSUER,
            "exp": now + timedelta(hours=1),
            "iat": now,
            "scope": "cao:read",
        },
    )
    with pytest.raises(jwt.PyJWTError):
        auth.extract_scopes_from_token(bad)


# --- get_current_scopes FastAPI dependency --------------------------------


@pytest.mark.asyncio
async def test_get_current_scopes_default_off_ignores_header():
    assert await auth.get_current_scopes(authorization=None) == auth.FULL_SCOPE_SET


@pytest.mark.asyncio
async def test_get_current_scopes_missing_token_401(monkeypatch, rsa_key):
    from fastapi import HTTPException

    _enable_auth(monkeypatch, rsa_key)
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_scopes(authorization=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_scopes_valid_bearer(monkeypatch, rsa_key):
    _enable_auth(monkeypatch, rsa_key)
    token = _make_token(rsa_key, _base_claims({"scope": "cao:read cao:write"}))
    scopes = await auth.get_current_scopes(authorization=f"Bearer {token}")
    assert scopes == ["cao:read", "cao:write"]


@pytest.mark.asyncio
async def test_get_current_scopes_invalid_bearer_401(monkeypatch, rsa_key):
    from fastapi import HTTPException

    _enable_auth(monkeypatch, rsa_key)
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_scopes(authorization="Bearer not-a-jwt")
    assert exc.value.status_code == 401


# --- JWKS cache: TTL reuse / fetch-when-empty / reuse-on-unreachable ------


class _CountingClient:
    """Stand-in for PyJWKClient that counts get_jwk_set() (fetch) calls."""

    instances = 0

    def __init__(self, uri, *args, **kwargs) -> None:  # noqa: ANN001
        self.uri = uri
        type(self).instances += 1

    def get_jwk_set(self):
        return {"keys": []}


def test_jwks_cache_fetches_when_empty_then_reuses(monkeypatch):
    monkeypatch.setattr(auth, "PyJWKClient", _CountingClient)
    _CountingClient.instances = 0
    cache = auth._JWKSCache(ttl=timedelta(hours=1))

    c1 = cache.get_client("https://idp/jwks")
    c2 = cache.get_client("https://idp/jwks")
    # Within TTL the cached client is reused (only one construction/fetch).
    assert c1 is c2
    assert _CountingClient.instances == 1


def test_jwks_cache_refetches_after_ttl(monkeypatch):
    monkeypatch.setattr(auth, "PyJWKClient", _CountingClient)
    _CountingClient.instances = 0
    cache = auth._JWKSCache(ttl=timedelta(seconds=0))
    cache.get_client("https://idp/jwks")
    time.sleep(0.01)
    cache.get_client("https://idp/jwks")
    # TTL of 0 forces a re-fetch on every call.
    assert _CountingClient.instances == 2


def test_jwks_cache_reuses_on_unreachable(monkeypatch):
    # First call populates the cache; subsequent fetch fails -> reuse cached.
    state = {"fail": False}

    class _Flaky(_CountingClient):
        def __init__(self, uri, *a, **k):  # noqa: ANN001
            if state["fail"]:
                raise ConnectionError("jwks unreachable")
            super().__init__(uri, *a, **k)

    monkeypatch.setattr(auth, "PyJWKClient", _Flaky)
    _CountingClient.instances = 0
    cache = auth._JWKSCache(ttl=timedelta(seconds=0))  # always tries to refetch
    first = cache.get_client("https://idp/jwks")
    state["fail"] = True
    second = cache.get_client("https://idp/jwks")
    assert first is second  # reused despite the source being unreachable


def test_jwks_cache_raises_when_empty_and_unreachable(monkeypatch):
    class _Down(_CountingClient):
        def __init__(self, uri, *a, **k):  # noqa: ANN001
            raise ConnectionError("jwks unreachable")

    monkeypatch.setattr(auth, "PyJWKClient", _Down)
    cache = auth._JWKSCache()
    with pytest.raises(ConnectionError):
        cache.get_client("https://idp/jwks")


# --- require_any_scope: HTTP authorization enforcement --------------------


@pytest.mark.asyncio
async def test_require_any_scope_noop_when_auth_disabled():
    # Default-off: a read-only caller is not blocked from a write-gated dep.
    dep = auth.require_any_scope(auth.SCOPE_WRITE, auth.SCOPE_ADMIN)
    assert await dep([auth.SCOPE_READ]) == [auth.SCOPE_READ]


@pytest.mark.asyncio
async def test_require_any_scope_blocks_read_only_on_write(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    dep = auth.require_any_scope(auth.SCOPE_WRITE, auth.SCOPE_ADMIN)
    with pytest.raises(HTTPException) as ei:
        await dep([auth.SCOPE_READ])
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_require_any_scope_allows_write_token(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    dep = auth.require_any_scope(auth.SCOPE_WRITE, auth.SCOPE_ADMIN)
    assert await dep([auth.SCOPE_WRITE]) == [auth.SCOPE_WRITE]


@pytest.mark.asyncio
async def test_require_any_scope_admin_required_for_delete(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    dep = auth.require_any_scope(auth.SCOPE_ADMIN)
    with pytest.raises(HTTPException) as ei:
        await dep([auth.SCOPE_WRITE])
    assert ei.value.status_code == 403
    assert await dep([auth.SCOPE_ADMIN]) == [auth.SCOPE_ADMIN]


# --- authorization-server discovery + local-token + bearer parsing --------


def test_get_authorization_servers_issuer_precedence(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_ISSUER", "https://issuer.example/")
    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.auth0.com")
    assert auth.get_authorization_servers() == ["https://issuer.example/"]


def test_get_authorization_servers_from_jwks_uri(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/.well-known/jwks.json")
    assert auth.get_authorization_servers() == ["https://idp.example"]


def test_get_authorization_servers_empty_when_disabled():
    assert auth.get_authorization_servers() == []


def test_get_scopes_for_local_token_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "not-a-jwt")

    def _boom(uri):  # noqa: ANN001
        raise ConnectionError("unreachable")

    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", _boom)
    # Validation fails -> defensive fallback to the full set (UX pre-check only).
    assert auth.get_scopes_for_local_token() == auth.FULL_SCOPE_SET


def test_extract_bearer_variants():
    assert auth._extract_bearer(None) is None
    assert auth._extract_bearer("Bearer") is None
    assert auth._extract_bearer("Bearer   ") is None
    assert auth._extract_bearer("NotBearer tok") is None
    assert auth._extract_bearer("Bearer tok123") == "tok123"
    assert auth._extract_bearer("bearer tok123") == "tok123"


# --- H3: local bearer + misconfiguration error ----------------------------


def test_get_local_bearer_none_when_disabled():
    """Default-off: no bearer is attached to internal MCP->API calls."""
    assert auth.get_local_bearer() is None


def test_get_local_bearer_none_when_enabled_without_token(monkeypatch):
    """Auth on but no CAO_AUTH_LOCAL_TOKEN -> no bearer (caller surfaces error)."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    assert auth.get_local_bearer() is None


def test_get_local_bearer_returns_token_when_configured(monkeypatch):
    """Auth on with a local token -> that token is returned for forwarding."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "  machine-token  ")
    assert auth.get_local_bearer() == "machine-token"


def test_local_auth_misconfig_error_none_when_disabled():
    """Default-off: the internal hop is fine, no misconfiguration error."""
    assert auth.local_auth_misconfig_error() is None


def test_local_auth_misconfig_error_none_when_token_present(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "machine-token")
    assert auth.local_auth_misconfig_error() is None


def test_local_auth_misconfig_error_set_when_enabled_without_token(monkeypatch):
    """Auth on with no local token -> an actionable error string is returned."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)
    msg = auth.local_auth_misconfig_error()
    assert msg is not None
    assert "CAO_AUTH_LOCAL_TOKEN" in msg


# --- JWKS robustness: unknown-kid forced refresh + staleness bound --------


def test_extract_scopes_refreshes_on_unknown_kid(monkeypatch, rsa_key):
    """An unknown ``kid`` forces a single JWKS refresh + retry (key rotation)."""
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("CAO_AUTH_AUDIENCE", AUDIENCE)

    calls = {"n": 0}

    class _RotatingClient:
        def get_signing_key_from_jwt(self, token):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                # First lookup misses (stale cache from before the rotation).
                raise jwt.PyJWKClientError("no matching key for kid")
            return _FakeSigningKey(rsa_key.public_key())

    fake = _RotatingClient()
    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: fake)

    token = _make_token(rsa_key, _base_claims({"scope": "cao:read"}))
    assert auth.extract_scopes_from_token(token) == ["cao:read"]
    # The signing-key lookup was retried exactly once after the forced refresh.
    assert calls["n"] == 2


def test_extract_scopes_unknown_kid_still_raises_after_refresh(monkeypatch, rsa_key):
    """A genuinely unknown key still raises (mapped to 401) after the retry."""
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("CAO_AUTH_AUDIENCE", AUDIENCE)

    class _AlwaysMissing:
        def get_signing_key_from_jwt(self, token):  # noqa: ANN001
            raise jwt.PyJWKClientError("no matching key for kid")

    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: _AlwaysMissing())
    token = _make_token(rsa_key, _base_claims({"scope": "cao:read"}))
    with pytest.raises(jwt.PyJWKClientError):
        auth.extract_scopes_from_token(token)


def test_jwks_cache_fails_closed_past_max_staleness(monkeypatch):
    """Reuse-on-unreachable is bounded: past the staleness cap it fails closed."""
    state = {"fail": False}

    class _Flaky(_CountingClient):
        def __init__(self, uri, *a, **k):  # noqa: ANN001
            if state["fail"]:
                raise ConnectionError("jwks unreachable")
            super().__init__(uri, *a, **k)

    monkeypatch.setattr(auth, "PyJWKClient", _Flaky)
    _CountingClient.instances = 0
    cache = auth._JWKSCache(ttl=timedelta(seconds=0))  # always tries to refetch
    cache.get_client("https://idp/jwks")
    # Age the cached keys past the hard staleness bound.
    cache._fetched_at = datetime.now(timezone.utc) - (
        auth._JWKS_MAX_STALENESS + timedelta(minutes=1)
    )
    state["fail"] = True
    with pytest.raises(ConnectionError):
        cache.get_client("https://idp/jwks")


def test_jwks_cache_reuses_within_max_staleness(monkeypatch):
    """Within the staleness bound, unreachable refetch still reuses cached keys."""
    state = {"fail": False}

    class _Flaky(_CountingClient):
        def __init__(self, uri, *a, **k):  # noqa: ANN001
            if state["fail"]:
                raise ConnectionError("jwks unreachable")
            super().__init__(uri, *a, **k)

    monkeypatch.setattr(auth, "PyJWKClient", _Flaky)
    _CountingClient.instances = 0
    cache = auth._JWKSCache(ttl=timedelta(seconds=0))
    first = cache.get_client("https://idp/jwks")
    # Still well within the staleness bound.
    cache._fetched_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    state["fail"] = True
    assert cache.get_client("https://idp/jwks") is first
