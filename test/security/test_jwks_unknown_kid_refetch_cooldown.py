"""Unknown kids do not amplify JWKS refetches.

``_verify_token`` used to ``clear()`` the whole JWKS cache and refetch on any
``PyJWKClientError``. The ``kid`` comes from the unverified header, so an
unauthenticated caller could force a JWKS fetch per request. A refetch is now
bounded to one per JWKS URI per cooldown window, with unknown kids
negative-cached, so two tokens with different bogus kids in the window trigger
at most one refetch.
"""

import jwt
import pytest
from jwt import PyJWKClientError

from cli_agent_orchestrator.security import auth


class _CountingClient:
    """Stand-in PyJWKClient that counts JWKS fetches and knows no signing keys."""

    fetches = 0

    def __init__(self, uri):
        self._uri = uri

    def get_jwk_set(self):
        type(self).fetches += 1

    def get_signing_key_from_jwt(self, token):
        raise PyJWKClientError("no matching kid")


def _token_with_kid(kid: str) -> str:
    return jwt.encode({"sub": "x"}, "x" * 32, algorithm="HS256", headers={"kid": kid})


@pytest.fixture
def counting_jwks(monkeypatch):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.test/.well-known/jwks.json")
    _CountingClient.fetches = 0
    monkeypatch.setattr(auth, "PyJWKClient", _CountingClient)
    monkeypatch.setattr(auth, "_jwks_cache", auth._JWKSCache())


def test_two_unknown_kids_in_window_cause_at_most_one_refetch(counting_jwks):
    for kid in ("bogus-kid-1", "bogus-kid-2"):
        with pytest.raises(PyJWKClientError):
            auth._verify_token(_token_with_kid(kid))

    # One initial cache fill + at most one refetch for the whole window.
    assert (
        _CountingClient.fetches <= 2
    ), f"unknown kids amplified fetches: {_CountingClient.fetches}"
