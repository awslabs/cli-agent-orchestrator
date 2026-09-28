"""``is_service_principal`` verifies CAO_AUTH_LOCAL_TOKEN like a request does (#745).

The service identity is derived by verifying the local token through the same
``extract_principal_from_token`` path requests use, so there is one notion of who
the token is. Uses real RS256 tokens and the auth suite's fake JWKS client.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cli_agent_orchestrator.security import auth, service_principal
from cli_agent_orchestrator.security.principal import Principal

AUDIENCE = "cao-api"
ISSUER = "https://example.auth0.com/"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


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


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
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
    with service_principal._lock:
        service_principal._verified_ids.clear()


def _enable(monkeypatch, rsa_key):
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("CAO_AUTH_AUDIENCE", AUDIENCE)
    fake = _FakeClient(rsa_key.public_key())
    monkeypatch.setattr(auth.get_jwks_cache(), "get_client", lambda uri: fake)


def test_true_for_the_verified_service_principal(monkeypatch, rsa_key):
    _enable(monkeypatch, rsa_key)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", _make_token(rsa_key, "svc"))

    assert service_principal.is_service_principal(Principal(subject="svc", issuer=ISSUER)) is True


def test_false_for_a_different_principal(monkeypatch, rsa_key):
    _enable(monkeypatch, rsa_key)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", _make_token(rsa_key, "svc"))

    assert (
        service_principal.is_service_principal(Principal(subject="human", issuer=ISSUER)) is False
    )


def test_false_when_auth_disabled(monkeypatch):
    # No IdP configured: there is no service principal at all.
    assert service_principal.is_service_principal(Principal(subject="svc", issuer=ISSUER)) is False


def test_false_when_no_local_token(monkeypatch, rsa_key):
    _enable(monkeypatch, rsa_key)
    # Auth on but no CAO_AUTH_LOCAL_TOKEN.
    assert service_principal.is_service_principal(Principal(subject="svc", issuer=ISSUER)) is False


def test_false_when_token_unverifiable(monkeypatch, rsa_key):
    _enable(monkeypatch, rsa_key)

    def _boom(token):
        raise jwt.InvalidTokenError("bad")

    monkeypatch.setattr(auth, "extract_principal_from_token", _boom)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", _make_token(rsa_key, "svc"))

    assert service_principal.is_service_principal(Principal(subject="svc", issuer=ISSUER)) is False
