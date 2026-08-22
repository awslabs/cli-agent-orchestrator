"""Bearer-token authentication for the terminal WebSocket attach endpoint.

Covers the security fix that closes the auth bypass on
``/terminals/{terminal_id}/ws``: when the HTTP auth layer is enabled the
handshake must carry a valid JWT (``Authorization: Bearer`` header or
``?token=`` query parameter) granting at least ``cao:read``. Default-off —
auth disabled — the attach must behave exactly as before (no token needed).

The tests drive ``terminal_ws`` directly with a mock ``WebSocket`` (the same
pattern the existing Origin/IP-guard tests in ``test_terminals.py`` use) so
the exact ``close`` codes are observable. The admitted paths stop at the
terminal-metadata lookup (mocked to return ``None``), so no real PTY is
spawned.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.security import auth

AUTH_DOMAIN = "test.local"
AUTH_AUDIENCE = "cao://test"


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _FakeClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: ANN001
        return _FakeSigningKey(self._public_key)


def _enable_auth(monkeypatch, jwt_factory):
    """Enable the auth layer and route the JWKS cache to ``jwt_factory``.

    Mirrors ``test/security/test_auth.py``: env-var enable + a fake JWKS
    client so no network is involved. ``jwt_factory.mint(...)`` tokens are
    issued against the same keypair, so they validate.
    """
    from cryptography.hazmat.primitives import serialization

    monkeypatch.setenv("AUTH0_DOMAIN", AUTH_DOMAIN)
    monkeypatch.setenv("AUTH0_AUDIENCE", AUTH_AUDIENCE)
    public_key = serialization.load_pem_private_key(
        jwt_factory.private_pem, password=None
    ).public_key()
    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: _FakeClient(public_key))


def _make_ws(**overrides):
    """Build a mock WebSocket that passes the loopback IP + Origin gates."""
    ws = MagicMock()
    ws.client = MagicMock(host="127.0.0.1")
    ws.headers = {}
    ws.query_params = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    for key, value in overrides.items():
        setattr(ws, key, value)
    return ws


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    for var in (
        "AUTH0_DOMAIN",
        "CAO_AUTH_JWKS_URI",
        "AUTH0_AUDIENCE",
        "CAO_AUTH_AUDIENCE",
        "CAO_AUTH_ISSUER",
    ):
        monkeypatch.delenv(var, raising=False)
    auth.get_jwks_cache().clear()
    yield
    auth.get_jwks_cache().clear()


@contextmanager
def _admitted_patches():
    with (
        patch("cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS", ["127.0.0.1"]),
        patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=None),
    ):
        yield


# --- default-off (no auth configured) --------------------------------------


@pytest.mark.asyncio
async def test_ws_auth_disabled_no_token_attaches():
    """Default-off: a token-less attach proceeds past the gate and reaches the
    terminal lookup (closes 4004 terminal-not-found, never 4401)."""
    from cli_agent_orchestrator.api.main import terminal_ws

    ws = _make_ws()
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.call_args.kwargs.get("code") == 4004


@pytest.mark.asyncio
async def test_ws_auth_disabled_ignores_present_token(jwt_factory):
    """Default-off: even a provided token does not gate the attach."""
    from cli_agent_orchestrator.api.main import terminal_ws

    ws = _make_ws(headers={"authorization": f"Bearer {jwt_factory.mint_viewer()}"})
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_awaited_once()
    assert ws.close.call_args.kwargs.get("code") == 4004


# --- auth enabled: no / invalid token --------------------------------------


@pytest.mark.asyncio
async def test_ws_auth_enabled_missing_token_rejected(monkeypatch, jwt_factory):
    """Auth on + no token → handshake closed 4401 before accept."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws()
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_not_called()
    ws.close.assert_awaited_once()
    kwargs = ws.close.call_args.kwargs
    assert kwargs.get("code") == 4401
    assert kwargs.get("reason") == "Unauthorized"


@pytest.mark.asyncio
async def test_ws_auth_enabled_invalid_token_rejected(monkeypatch, jwt_factory):
    """Auth on + a garbage bearer token → closed 4401."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws(headers={"authorization": "Bearer not-a-jwt"})
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_not_called()
    assert ws.close.call_args.kwargs.get("code") == 4401


# --- auth enabled: valid token ---------------------------------------------


@pytest.mark.asyncio
async def test_ws_auth_enabled_valid_bearer_header_attaches(monkeypatch, jwt_factory):
    """Auth on + valid ``Authorization: Bearer`` token → proceeds past the gate
    (closes 4004 terminal-not-found, never 4401)."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws(headers={"authorization": f"Bearer {jwt_factory.mint_viewer()}"})
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.call_args.kwargs.get("code") == 4004


@pytest.mark.asyncio
async def test_ws_auth_enabled_valid_token_query_param_attaches(monkeypatch, jwt_factory):
    """Auth on + valid ``?token=`` query param (browser clients cannot set
    headers on a WebSocket handshake) → proceeds past the gate."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws(query_params={"token": jwt_factory.mint_viewer()})
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_awaited_once()
    assert ws.close.call_args.kwargs.get("code") == 4004


@pytest.mark.asyncio
async def test_ws_auth_enabled_write_only_token_rejected(monkeypatch, jwt_factory):
    """Auth on + a valid token WITHOUT ``cao:read`` → closed 4401 (attach
    requires at least the read scope)."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws(headers={"authorization": f"Bearer {jwt_factory.mint(scopes='cao:write')}"})
    with _admitted_patches():
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_not_called()
    assert ws.close.call_args.kwargs.get("code") == 4401


# --- existing IP + Origin checks still run (and precede auth) ---------------


@pytest.mark.asyncio
async def test_ws_auth_ip_check_still_precedes_auth(monkeypatch, jwt_factory):
    """A valid token does NOT bypass the loopback IP allowlist: an
    out-of-allowlist peer is still closed 4003 before any auth evaluation."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws(headers={"authorization": f"Bearer {jwt_factory.mint_viewer()}"})
    ws.client = MagicMock(host="10.0.0.9")
    with patch("cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS", ["127.0.0.1"]):
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_not_called()
    assert ws.close.call_args.kwargs.get("code") == 4003


@pytest.mark.asyncio
async def test_ws_auth_origin_check_still_precedes_auth(monkeypatch, jwt_factory):
    """A valid token does NOT bypass the Origin guard: a cross-site Origin is
    still closed 4403 before any auth evaluation."""
    from cli_agent_orchestrator.api.main import terminal_ws

    _enable_auth(monkeypatch, jwt_factory)
    ws = _make_ws(
        headers={
            "origin": "http://evil.example.com",
            "host": "localhost:9889",
            "authorization": f"Bearer {jwt_factory.mint_viewer()}",
        }
    )
    with (
        patch(
            "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
            ["127.0.0.1", "::1", "localhost"],
        ),
        patch("cli_agent_orchestrator.constants.CORS_ORIGINS", []),
        patch("cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS", []),
    ):
        await terminal_ws(ws, "abcd1234")

    ws.accept.assert_not_called()
    assert ws.close.call_args.kwargs.get("code") == 4403
