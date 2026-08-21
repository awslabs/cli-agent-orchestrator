"""JWTFactory — canonical JWT minting fixture for the CAO test suite.

Why this module exists
----------------------
The RSA-2048 keypair generation and RS256 JWT minting logic was previously
duplicated in three places:

- ``test/conftest.py`` (``rsa_keys`` + ``mint_test_token`` — function-scoped)
- ``test/fixtures/cao_server.py`` (``_session_rsa_keys`` — session-scoped)
- Any future test module that needs tokens (e.g. Playwright helpers, MCP
  iframe scripts)

``JWTFactory`` is the single-source-of-truth for this logic. Both the Python
test suite and the TypeScript helpers (``web/e2e/helpers/jwt.ts``,
``cao_mcp_apps/e2e/helpers/jwt.ts``) document the same RS256 wire shape so
tokens minted in any language are accepted by the same server.

Fixture scopes available
------------------------
- ``jwt_factory`` (session-scoped) — one keypair per pytest session; matches
  the ``cao_server_with_auth`` lifetime so tokens are valid for the session.
- ``jwt_factory_fn`` (function-scoped) — fresh keypair per test; use when
  isolation between tests matters more than startup speed.

The ``AuthCaoServer`` bundle in ``test/fixtures/cao_server.py`` already
generates its own session keypair internally; this module does not replace
that path. Instead it exposes the same capability to tests that need to mint
tokens *without* spinning up a subprocess (unit tests, security tests,
alternative e2e drivers).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

# ---------------------------------------------------------------------------
# Constants (match test/conftest.py and test/fixtures/cao_server.py)
# ---------------------------------------------------------------------------

DEFAULT_DOMAIN = "test.local"
DEFAULT_AUDIENCE = "cao://test"
DEFAULT_KID = "test-kid"
DEFAULT_SCOPES = "cao:read cao:write cao:admin"


# ---------------------------------------------------------------------------
# JWTFactory
# ---------------------------------------------------------------------------


@dataclass
class JWTFactory:
    """Manages an RSA-2048 keypair and mints RS256 JWTs against it.

    Usage::

        factory = JWTFactory.generate()
        token = factory.mint(scopes="cao:read")
        jwks = factory.jwks()   # dict ready to serve as /.well-known/jwks.json body

    The ``domain`` and ``audience`` fields match the ``AUTH0_DOMAIN`` /
    ``AUTH0_AUDIENCE`` env vars that cao-server reads; tests must set those
    vars (or use ``cao_server_with_auth`` which does it automatically).
    """

    private_pem: bytes
    public_jwk: Any  # authlib.jose.JsonWebKey — typed Any to keep import lazy
    domain: str = DEFAULT_DOMAIN
    audience: str = DEFAULT_AUDIENCE
    kid: str = DEFAULT_KID

    # Cached JWKS dict — built once on first call to jwks().
    _jwks_cache: Optional[dict[str, Any]] = field(default=None, repr=False, compare=False)

    @classmethod
    def generate(
        cls,
        *,
        domain: str = DEFAULT_DOMAIN,
        audience: str = DEFAULT_AUDIENCE,
        kid: str = DEFAULT_KID,
    ) -> "JWTFactory":
        """Generate a fresh RSA-2048 keypair and return a ``JWTFactory`` wrapping it."""
        from authlib.jose import JsonWebKey
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_jwk = JsonWebKey.import_key(
            public_pem,
            {"kty": "RSA", "use": "sig", "kid": kid},
        )
        return cls(
            private_pem=private_pem,
            public_jwk=public_jwk,
            domain=domain,
            audience=audience,
            kid=kid,
        )

    def mint(
        self,
        *,
        scopes: str = DEFAULT_SCOPES,
        audience: Optional[str] = None,
        exp_offset: int = 300,
        iat_offset: int = 0,
        extra_claims: Optional[dict[str, Any]] = None,
    ) -> str:
        """Mint an RS256 JWT signed with this factory's private key.

        Args:
            scopes: Space-separated scope string (e.g. ``"cao:read cao:write"``).
            audience: Override the factory's default audience.
            exp_offset: Seconds from now until expiry (default 5 min).
            iat_offset: Seconds to add/subtract from ``iat`` (useful for
                testing clock-skew scenarios; negative values produce tokens
                issued "in the future" from the server's perspective).
            extra_claims: Additional claims merged into the payload.

        Returns:
            The compact JWT string (``header.payload.signature``).
        """
        from authlib.jose import JsonWebToken

        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": f"https://{self.domain}/",
            "aud": audience if audience is not None else self.audience,
            "iat": now + iat_offset,
            "exp": now + exp_offset,
            "scope": scopes,
        }
        if extra_claims:
            claims.update(extra_claims)

        header = {"alg": "RS256", "kid": self.kid}
        token = jwt.encode(header, claims, self.private_pem)
        return token.decode("utf-8") if isinstance(token, bytes) else token

    def jwks(self) -> dict[str, Any]:
        """Return a JWKS dict suitable for serving as ``/.well-known/jwks.json``.

        The dict is cached after the first call — the public key is immutable.
        """
        if self._jwks_cache is None:
            self._jwks_cache = {"keys": [self.public_jwk.as_dict()]}
        return self._jwks_cache

    # Convenience aliases matching the old conftest API surface.

    def mint_admin(self) -> str:
        """Mint a full admin token (``cao:read cao:write cao:admin``)."""
        return self.mint(scopes="cao:read cao:write cao:admin")

    def mint_operator(self) -> str:
        """Mint an operator token (``cao:read cao:write``)."""
        return self.mint(scopes="cao:read cao:write")

    def mint_viewer(self) -> str:
        """Mint a read-only viewer token (``cao:read`` only)."""
        return self.mint(scopes="cao:read")

    def mint_expired(self) -> str:
        """Mint a token that is already expired (``exp`` 10 s in the past)."""
        return self.mint(exp_offset=-10)


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def jwt_factory() -> JWTFactory:
    """Session-scoped JWTFactory.

    One keypair per pytest session. The matching JWKS document is accessible
    via ``jwt_factory.jwks()`` — pass it to a ``JWKSServer`` or
    ``cao_server_with_auth`` when you need an authed subprocess.

    Use ``jwt_factory_fn`` when test isolation requires a fresh keypair.
    """
    return JWTFactory.generate()


@pytest.fixture
def jwt_factory_fn() -> JWTFactory:
    """Function-scoped JWTFactory — fresh RSA keypair per test.

    Slower than ``jwt_factory`` (RSA-2048 keygen per test) but guarantees
    complete key isolation between tests. Prefer this for security-focused
    tests where sharing a keypair across tests would obscure bugs.
    """
    return JWTFactory.generate()


__all__ = [
    "JWTFactory",
    "DEFAULT_DOMAIN",
    "DEFAULT_AUDIENCE",
    "DEFAULT_KID",
    "DEFAULT_SCOPES",
    "jwt_factory",
    "jwt_factory_fn",
]
