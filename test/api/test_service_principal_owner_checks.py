"""Service-token MCP callbacks pass the owner checks; other principals don't (#745).

The MCP->API hop authenticates with the server's own service token, whose
principal is not the human owner of the terminal a callback is about. The owner
checks on ``create_inbox_message_endpoint`` (sender owner) and
``create_terminal_in_session`` (caller owner) used to compare owner to the
request principal and 403 every such legitimate callback.
They now allow the verified service principal to name an existing terminal, while
still rejecting any other principal that names a terminal owned by someone else.

Uses real RS256 tokens and the auth suite's fake JWKS client.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.security import auth, service_principal
from cli_agent_orchestrator.security.principal import Principal

AUDIENCE = "cao-api"
ISSUER = "https://example.auth0.com/"

HUMAN1 = Principal(subject="human1", issuer=ISSUER)
HUMAN2 = Principal(subject="human2", issuer=ISSUER)
SERVICE = Principal(subject="svc", issuer=ISSUER)


def _make_token(private_key, sub: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "sub": sub,
        "exp": now + timedelta(hours=1),
        "iat": now,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _FakeClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: ANN001
        return _FakeSigningKey(self._public_key)


class _Msg:
    id = "m1"
    sender_id = "sender-term"
    receiver_id = "recv-term"
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeRequest:
    pass


class _FakeBackgroundTasks:
    def add_task(self, *a, **k):
        pass


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch, rsa_key):
    for var in (
        "CAO_AUTH_JWKS_URI",
        "AUTH0_AUDIENCE",
        "CAO_AUTH_ISSUER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("CAO_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", _make_token(rsa_key, "svc"))
    fake = _FakeClient(rsa_key.public_key())
    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: fake)
    auth.get_jwks_cache().clear()
    with service_principal._lock:
        service_principal._verified_ids.clear()
    yield
    auth.get_jwks_cache().clear()


# --- inbox route: sender owner check --------------------------------------


def _call_inbox(principal):
    return asyncio.run(
        main.create_inbox_message_endpoint(
            _FakeRequest(),
            receiver_id="recv-term",
            sender_id="sender-term",
            message="hi",
            _scopes=["cao:write"],
            principal=principal,
        )
    )


def _patch_inbox(monkeypatch):
    # Sender exists and is owned by human1.
    monkeypatch.setattr(main, "get_terminal_metadata", lambda tid: {"id": tid, "owner": HUMAN1.id})
    monkeypatch.setattr(main, "create_inbox_message", lambda *a, **k: _Msg())
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)
    monkeypatch.setattr(main.inbox_service, "deliver_pending", lambda *a, **k: None)


def test_inbox_owner_token_allowed(monkeypatch):
    _patch_inbox(monkeypatch)
    result = _call_inbox(HUMAN1)
    assert result["success"] is True


def test_inbox_service_token_allowed(monkeypatch):
    # The service principal names a sender owned by a human — a legitimate MCP
    # callback. Fails first (403), passes after the fix (200).
    _patch_inbox(monkeypatch)
    result = _call_inbox(SERVICE)
    assert result["success"] is True


def test_inbox_other_human_forbidden(monkeypatch):
    _patch_inbox(monkeypatch)
    with pytest.raises(main.HTTPException) as ei:
        _call_inbox(HUMAN2)
    assert ei.value.status_code == 403


# --- create_terminal_in_session: caller owner check -----------------------


def _call_create_terminal(principal):
    return asyncio.run(
        main.create_terminal_in_session(
            _FakeRequest(),
            session_name="cao-x",
            agent_profile="developer",
            provider="kiro_cli",
            caller_id="caller-term",
            working_directory="/tmp/project",
            principal=principal,
        )
    )


def _patch_create_terminal(monkeypatch):
    # Caller exists and is owned by human1; caller is local (no runtime).
    monkeypatch.setattr(main, "caller_owner_id", lambda caller_id: HUMAN1.id)
    monkeypatch.setattr(main, "get_terminal_metadata", lambda tid: {"id": tid, "owner": HUMAN1.id})
    monkeypatch.setattr(main.runtime_registry, "placement", lambda tid: (False, None))
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    async def fake_create_terminal(**kwargs):
        return {"id": "worker-term", "owner": kwargs.get("owner")}

    monkeypatch.setattr(main.terminal_service, "create_terminal", fake_create_terminal)


def test_create_terminal_service_token_allowed(monkeypatch):
    # Service principal creating a worker for a caller owned by a human. Fails
    # first (403), passes after the fix.
    _patch_create_terminal(monkeypatch)
    result = _call_create_terminal(SERVICE)
    assert result["id"] == "worker-term"


def test_create_terminal_other_human_forbidden(monkeypatch):
    _patch_create_terminal(monkeypatch)
    with pytest.raises(main.HTTPException) as ei:
        _call_create_terminal(HUMAN2)
    assert ei.value.status_code == 403
