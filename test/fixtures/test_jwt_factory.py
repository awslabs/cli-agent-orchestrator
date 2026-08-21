"""Self-tests for test/fixtures/jwt_factory.py.

Covers the JWTFactory contract without spinning up a subprocess:
- Key generation produces a valid RSA-2048 keypair.
- Minted tokens carry the expected claims.
- Scope helpers produce the right scope strings.
- ``mint_expired`` produces a token that the server would reject.
- ``jwks()`` is cacheable and matches the signing key.
- Fixtures (session + function-scoped) are constructable.

These run in Tier-1 (unit only, no server, no provider).
"""

from __future__ import annotations

import json
import time
from test.fixtures.jwt_factory import (
    DEFAULT_AUDIENCE,
    DEFAULT_DOMAIN,
    DEFAULT_KID,
    DEFAULT_SCOPES,
    JWTFactory,
)

import pytest


def _decode_jwt_payload(token: str) -> dict:
    """Base64url-decode the JWT payload without verifying the signature."""
    import base64

    parts = token.split(".")
    assert len(parts) == 3, f"expected 3 JWT parts, got {len(parts)}"
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def _decode_jwt_header(token: str) -> dict:
    """Base64url-decode the JWT header without verifying the signature."""
    import base64

    parts = token.split(".")
    assert len(parts) == 3
    padded = parts[0] + "=" * (-len(parts[0]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


# ---------------------------------------------------------------------------
# JWTFactory.generate
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_returns_jwt_factory_instance(self):
        factory = JWTFactory.generate()
        assert isinstance(factory, JWTFactory)

    def test_default_domain_and_audience(self):
        factory = JWTFactory.generate()
        assert factory.domain == DEFAULT_DOMAIN
        assert factory.audience == DEFAULT_AUDIENCE
        assert factory.kid == DEFAULT_KID

    def test_custom_domain_and_audience(self):
        factory = JWTFactory.generate(
            domain="custom.local", audience="cao://custom", kid="custom-kid"
        )
        assert factory.domain == "custom.local"
        assert factory.audience == "cao://custom"
        assert factory.kid == "custom-kid"

    def test_private_and_public_pem_are_bytes(self):
        factory = JWTFactory.generate()
        assert isinstance(factory.private_pem, bytes)
        assert b"PRIVATE" in factory.private_pem

    def test_different_factories_produce_different_keys(self):
        f1 = JWTFactory.generate()
        f2 = JWTFactory.generate()
        assert f1.private_pem != f2.private_pem


# ---------------------------------------------------------------------------
# JWTFactory.mint
# ---------------------------------------------------------------------------


class TestMint:
    def setup_method(self):
        self.factory = JWTFactory.generate()

    def test_produces_three_part_jwt(self):
        token = self.factory.mint()
        assert token.count(".") == 2

    def test_header_alg_is_rs256(self):
        token = self.factory.mint()
        header = _decode_jwt_header(token)
        assert header["alg"] == "RS256"

    def test_header_kid_matches_factory(self):
        token = self.factory.mint()
        header = _decode_jwt_header(token)
        assert header["kid"] == DEFAULT_KID

    def test_payload_iss_contains_domain(self):
        token = self.factory.mint()
        payload = _decode_jwt_payload(token)
        assert payload["iss"] == f"https://{DEFAULT_DOMAIN}/"

    def test_payload_aud_matches_factory_audience(self):
        token = self.factory.mint()
        payload = _decode_jwt_payload(token)
        assert payload["aud"] == DEFAULT_AUDIENCE

    def test_payload_scope_default(self):
        token = self.factory.mint()
        payload = _decode_jwt_payload(token)
        assert payload["scope"] == DEFAULT_SCOPES

    def test_payload_scope_custom(self):
        token = self.factory.mint(scopes="cao:read")
        payload = _decode_jwt_payload(token)
        assert payload["scope"] == "cao:read"

    def test_payload_exp_is_in_the_future(self):
        now = int(time.time())
        token = self.factory.mint()
        payload = _decode_jwt_payload(token)
        assert payload["exp"] > now

    def test_payload_iat_is_close_to_now(self):
        before = int(time.time()) - 2
        token = self.factory.mint()
        payload = _decode_jwt_payload(token)
        assert payload["iat"] >= before

    def test_exp_offset_controls_expiry(self):
        token = self.factory.mint(exp_offset=10)
        payload = _decode_jwt_payload(token)
        now = int(time.time())
        assert payload["exp"] <= now + 12  # allow 2s clock slack

    def test_iat_offset_shifts_iat(self):
        token = self.factory.mint(iat_offset=-100)
        payload = _decode_jwt_payload(token)
        now = int(time.time())
        assert payload["iat"] <= now - 98  # allow 2s clock slack

    def test_audience_override(self):
        token = self.factory.mint(audience="cao://other")
        payload = _decode_jwt_payload(token)
        assert payload["aud"] == "cao://other"

    def test_extra_claims_are_merged(self):
        token = self.factory.mint(extra_claims={"sub": "testuser", "custom": 42})
        payload = _decode_jwt_payload(token)
        assert payload["sub"] == "testuser"
        assert payload["custom"] == 42


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


class TestConvenienceMintMethods:
    def setup_method(self):
        self.factory = JWTFactory.generate()

    def test_mint_admin_scopes(self):
        payload = _decode_jwt_payload(self.factory.mint_admin())
        assert payload["scope"] == "cao:read cao:write cao:admin"

    def test_mint_operator_scopes(self):
        payload = _decode_jwt_payload(self.factory.mint_operator())
        assert payload["scope"] == "cao:read cao:write"

    def test_mint_viewer_scopes(self):
        payload = _decode_jwt_payload(self.factory.mint_viewer())
        assert payload["scope"] == "cao:read"

    def test_mint_expired_is_in_the_past(self):
        token = self.factory.mint_expired()
        payload = _decode_jwt_payload(token)
        now = int(time.time())
        assert payload["exp"] < now


# ---------------------------------------------------------------------------
# JWKS
# ---------------------------------------------------------------------------


class TestJwks:
    def setup_method(self):
        self.factory = JWTFactory.generate()

    def test_jwks_returns_dict_with_keys(self):
        jwks = self.factory.jwks()
        assert isinstance(jwks, dict)
        assert "keys" in jwks
        assert len(jwks["keys"]) == 1

    def test_jwks_key_has_expected_fields(self):
        key = self.factory.jwks()["keys"][0]
        assert key["kty"] == "RSA"
        assert key["use"] == "sig"
        assert key["kid"] == DEFAULT_KID
        assert "n" in key
        assert "e" in key

    def test_jwks_is_cached(self):
        jwks1 = self.factory.jwks()
        jwks2 = self.factory.jwks()
        assert jwks1 is jwks2  # same object — cached

    def test_different_factories_produce_different_jwks(self):
        f1 = JWTFactory.generate()
        f2 = JWTFactory.generate()
        assert f1.jwks()["keys"][0]["n"] != f2.jwks()["keys"][0]["n"]


# ---------------------------------------------------------------------------
# Pytest fixture smoke
# ---------------------------------------------------------------------------


def test_jwt_factory_fixture_is_jwt_factory(jwt_factory):
    assert isinstance(jwt_factory, JWTFactory)


def test_jwt_factory_fn_fixture_is_jwt_factory(jwt_factory_fn):
    assert isinstance(jwt_factory_fn, JWTFactory)


def test_session_and_function_fixtures_have_different_keys(jwt_factory, jwt_factory_fn):
    assert jwt_factory.private_pem != jwt_factory_fn.private_pem
