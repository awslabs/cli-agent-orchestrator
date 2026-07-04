"""Default-off OAuth 2.1 / RFC 9728 auth core for the CAO MCP App.

This module provides scope extraction, JWKS caching, and the FastAPI
``get_current_scopes`` dependency. It is **default-off**: with ``AUTH0_DOMAIN``
unset *and* ``CAO_AUTH_JWKS_URI`` unset, every authorization path returns the
full scope taxonomy and no enforcement happens — behavior is byte-for-byte
identical to a build with no auth layer.

The implementation is a **generic OAuth 2.1** one:
all load-bearing standards (RFC 9728 Protected Resource Metadata, RFC 8693 Token
Exchange, RFC 8707 Resource Indicators) are established IETF RFCs. Auth0 is
treated as just one configurable IdP — ``AUTH0_DOMAIN`` is a convenience that
derives the JWKS URI, while ``CAO_AUTH_JWKS_URI`` points at any generic IdP. No
Auth0-specific behavior is hard-depended on.

**Boundary note:** this module must NOT import ``clients.tmux`` or
``clients.database`` (it is imported by ``mcp_server/app_tools.py``, which sits
behind the HTTP-only guard). It depends only on ``constants`` and ``pyjwt``.

**Config note (issue #357):** ``AUTH0_DOMAIN``/``CAO_AUTH_JWKS_URI``/
``CAO_AUTH_AUDIENCE``/``CAO_AUTH_ISSUER`` are **env-var only** here — the
``auth.*`` settings.json keys are not read on this path. This module is the
actual authentication *enforcement* boundary (not a UX gate), so it is kept on
direct ``os.getenv`` reads rather than routed through ``ConfigService`` to
avoid changing security-critical resolution behavior in this PR. See
docs/configuration.md for the full rationale.
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional, cast

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient, PyJWKClientError

from cli_agent_orchestrator.constants import API_BASE_URL

logger = logging.getLogger(__name__)

# --- scope taxonomy -------------------------------------------------------
SCOPE_READ = "cao:read"
SCOPE_WRITE = "cao:write"
SCOPE_ADMIN = "cao:admin"
FULL_SCOPE_SET: List[str] = [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN]
SCOPES_SUPPORTED: List[str] = list(FULL_SCOPE_SET)

# Accepted RS256 only (RFC 9728 protected resources use asymmetric signatures).
_ALGORITHMS = ["RS256"]
_JWKS_TTL = timedelta(hours=1)

# Hard upper bound on how long the cache will keep serving keys it could not
# refresh because the JWKS source was unreachable. Within ``_JWKS_TTL`` keys are
# served without re-fetching; past the TTL a refresh is attempted on every call
# and, on failure, the *stale* keys are reused so a transient outage does not
# lock out valid callers — but only up to this bound. Past it the keys are
# considered too stale to trust (an IdP that rotated keys during a long outage
# would otherwise let revoked keys validate indefinitely), so validation fails
# closed instead.
_JWKS_MAX_STALENESS = timedelta(hours=24)


# --- configuration (default-off) -----------------------------------------


def _auth0_domain() -> str:
    return os.getenv("AUTH0_DOMAIN", "").strip()


def _generic_jwks_uri() -> str:
    return os.getenv("CAO_AUTH_JWKS_URI", "").strip()


def is_auth_enabled() -> bool:
    """Return whether the auth layer is active.

    Default-off: auth is enabled only when an IdP is configured — either
    ``AUTH0_DOMAIN`` (Auth0 convenience) or ``CAO_AUTH_JWKS_URI`` (generic IdP).
    With neither set, the layer is off and every path returns
    the full scope set.
    """

    return bool(_auth0_domain()) or bool(_generic_jwks_uri())


def get_jwks_uri() -> Optional[str]:
    """Resolve the JWKS endpoint URI, or ``None`` when auth is disabled.

    ``CAO_AUTH_JWKS_URI`` (generic IdP) takes precedence; otherwise the URI is
    derived from ``AUTH0_DOMAIN`` per Auth0's well-known convention.
    """

    generic = _generic_jwks_uri()
    if generic:
        return generic
    domain = _auth0_domain()
    if domain:
        return f"https://{domain}/.well-known/jwks.json"
    return None


def get_expected_audience() -> Optional[str]:
    """Return the expected token audience.

    When auth is enabled this never returns ``None``: it falls back to
    ``API_BASE_URL`` — the resource identifier advertised by the RFC 9728 PRM
    endpoint (``/.well-known/oauth-protected-resource``) — so audience
    verification is never silently disabled. An explicit ``CAO_AUTH_AUDIENCE`` /
    ``AUTH0_AUDIENCE`` takes precedence. Returns ``None`` only when auth is
    disabled (default-off), so nothing changes in the no-auth posture.
    """

    if not is_auth_enabled():
        return None
    return (
        os.getenv("CAO_AUTH_AUDIENCE", "").strip()
        or os.getenv("AUTH0_AUDIENCE", "").strip()
        or API_BASE_URL
    )


def get_authorization_servers() -> List[str]:
    """Return the issuer(s) advertised by the PRM endpoint."""

    issuer = os.getenv("CAO_AUTH_ISSUER", "").strip()
    if issuer:
        return [issuer]
    domain = _auth0_domain()
    if domain:
        return [f"https://{domain}/"]
    # Generic IdP: best-effort derive the issuer from the JWKS URI origin.
    generic = _generic_jwks_uri()
    if generic:
        return [generic.rsplit("/.well-known/", 1)[0]]
    return []


# --- JWKS cache (1 h TTL; reuse-on-unreachable, fetch-when-empty) ---------


class _JWKSCache:
    """Thread-safe JWKS client cache with a 1 h TTL.

    Behavior:

    * within the TTL, the cached client is reused without re-fetching;
    * when the cache is empty (or stale) AND the source is reachable, fresh keys
      are fetched before validating;
    * when the source is unreachable, the cached client is reused if present
      (so a transient outage does not lock out valid callers).
    """

    def __init__(self, ttl: timedelta = _JWKS_TTL) -> None:
        self._ttl = ttl
        self._client: Optional[PyJWKClient] = None
        self._uri: Optional[str] = None
        self._fetched_at: Optional[datetime] = None
        self._lock = threading.Lock()

    def get_client(self, uri: str) -> PyJWKClient:
        with self._lock:
            now = datetime.now(timezone.utc)
            fresh = (
                self._client is not None
                and self._uri == uri
                and self._fetched_at is not None
                and (now - self._fetched_at) < self._ttl
            )
            if fresh:
                assert self._client is not None
                return self._client

            try:
                client = PyJWKClient(uri)
                # Force a fetch so reachability + key presence are validated now.
                client.get_jwk_set()
                self._client = client
                self._uri = uri
                self._fetched_at = now
                return client
            except Exception:  # source unreachable / fetch failed
                # Reuse cached keys across a transient outage, but only up to a
                # hard staleness bound: if the source has been unreachable past
                # ``_JWKS_MAX_STALENESS`` we fail closed rather than keep trusting
                # keys an IdP may have rotated away during the outage.
                if self._client is not None and self._uri == uri and self._fetched_at is not None:
                    if (now - self._fetched_at) <= _JWKS_MAX_STALENESS:
                        logger.warning("JWKS source unreachable; reusing cached keys for %s", uri)
                        return self._client
                    logger.warning(
                        "JWKS source unreachable and cached keys exceed the %s staleness "
                        "bound for %s; failing closed",
                        _JWKS_MAX_STALENESS,
                        uri,
                    )
                raise

    def clear(self) -> None:
        with self._lock:
            self._client = None
            self._uri = None
            self._fetched_at = None


_jwks_cache = _JWKSCache()


def get_jwks_cache() -> _JWKSCache:
    """Return the process-wide JWKS cache (exposed for tests)."""

    return _jwks_cache


# --- scope extraction -----------------------------------------------------


def _scopes_from_claims(claims: dict) -> List[str]:
    """Extract granted scopes, accepting the ``scope`` / ``permissions`` / ``scp``
    claim variants."""

    out: List[str] = []
    scope = claims.get("scope")
    if isinstance(scope, str):
        out.extend(scope.split())
    elif isinstance(scope, list):
        out.extend(str(s) for s in scope)

    for key in ("permissions", "scp"):
        value = claims.get(key)
        if isinstance(value, str):
            out.extend(value.split())
        elif isinstance(value, list):
            out.extend(str(s) for s in value)

    # De-duplicate while preserving order.
    seen: set = set()
    deduped: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def extract_scopes_from_token(token: str) -> List[str]:
    """Validate ``token`` (RS256 + issuer + audience + expiry via JWKS) and
    return scopes.

    Default-off: when auth is disabled, returns the full scope set without
    touching the token. When enabled, raises (``jwt.PyJWTError`` subclasses or
    a generic ``Exception`` from the JWKS fetch) on any validation failure so the
    caller can map it to HTTP 401.

    Validation pins the token to the configured authorization server (``iss``)
    and audience (``aud``) in addition to the RS256 signature and ``exp`` so a
    token minted by a different IdP — or for a different resource on the same
    IdP — is rejected rather than accepted on signature alone.
    """

    if not is_auth_enabled():
        return list(FULL_SCOPE_SET)

    uri = get_jwks_uri()
    if not uri:  # pragma: no cover - guarded by is_auth_enabled
        return list(FULL_SCOPE_SET)

    client = _jwks_cache.get_client(uri)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
    except PyJWKClientError:
        # Unknown ``kid`` — almost always an IdP key rotation the 1 h TTL has not
        # picked up yet. Force a single refresh and retry rather than rejecting
        # valid tokens (and re-issued keys) for up to an hour. A genuinely
        # unknown key still raises on the retry and maps to 401.
        _jwks_cache.clear()
        client = _jwks_cache.get_client(uri)
        signing_key = client.get_signing_key_from_jwt(token)

    audience = get_expected_audience()
    # Pin to the first advertised authorization server (the PRM endpoint
    # advertises the same list). When an issuer is known we also require the
    # ``iss`` claim to be present so it cannot simply be omitted.
    issuers = get_authorization_servers()
    expected_issuer = issuers[0] if issuers else None
    required_claims = ["exp"]
    if expected_issuer is not None:
        required_claims.append("iss")
    options = {"require": required_claims, "verify_aud": audience is not None}
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=_ALGORITHMS,
        audience=audience,
        issuer=expected_issuer,
        options=cast("Any", options),
    )
    return _scopes_from_claims(claims)


def get_scopes_for_local_token() -> List[str]:
    """Return the granted scope set for the local (loopback) caller.

    Used by the ``submit_command`` choke-point pre-check (a UX gate; the FastAPI
    layer is the real security boundary). Default-off returns the full taxonomy.
    When auth is enabled, an optional ``CAO_AUTH_LOCAL_TOKEN`` is validated if
    present; otherwise the full set is returned and enforcement defers to the
    FastAPI ``Depends(get_current_scopes)`` boundary.
    """

    if not is_auth_enabled():
        return list(FULL_SCOPE_SET)

    token = os.getenv("CAO_AUTH_LOCAL_TOKEN", "").strip()
    if not token:
        return list(FULL_SCOPE_SET)
    try:
        return extract_scopes_from_token(token)
    except Exception:
        logger.warning(
            "Local token validation failed; deferring to FastAPI boundary", exc_info=True
        )
        return list(FULL_SCOPE_SET)


# --- internal MCP -> API service credential (H3) --------------------------


def get_local_bearer() -> Optional[str]:
    """Return the bearer token for internal MCP->API (loopback) calls, or ``None``.

    The MCP server reaches Backplane state over loopback HTTP to the FastAPI
    app. When the auth layer enforces scope on the mutation endpoints, those
    internal calls must carry a credential or they are rejected at the boundary
    with a raw ``401``. This returns the operator-provisioned
    ``CAO_AUTH_LOCAL_TOKEN`` (a machine token from the same IdP, holding the
    scopes the surface needs) so callers can attach
    ``Authorization: Bearer <token>``.

    Default-off: returns ``None`` when auth is disabled so no header is attached
    and the no-auth posture is byte-for-byte unchanged. When auth is enabled but
    no local token is configured it also returns ``None`` — the caller is
    responsible for surfacing the actionable misconfiguration
    (:func:`local_auth_misconfig_error`) rather than letting the bare ``401``
    leak out.
    """

    if not is_auth_enabled():
        return None
    return os.getenv("CAO_AUTH_LOCAL_TOKEN", "").strip() or None


def local_auth_misconfig_error() -> Optional[str]:
    """Return an actionable error string when auth is on but no local token is set.

    Returns ``None`` when the internal hop is properly configured (auth disabled,
    or auth enabled with a ``CAO_AUTH_LOCAL_TOKEN`` present). When auth is enabled
    but the token is missing, returns a message the MCP App surface can hand back
    as ``{"success": false, "error": ...}`` instead of a confusing raw ``401``.
    """

    if not is_auth_enabled():
        return None
    if get_local_bearer():
        return None
    return (
        "auth enabled but CAO_AUTH_LOCAL_TOKEN is not set: the MCP Apps surface "
        "cannot authorize its internal call to the CAO API. Provision a machine "
        "token (with the scopes the surface needs) and set CAO_AUTH_LOCAL_TOKEN."
    )


# --- FastAPI dependency ---------------------------------------------------


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """Pull the bearer token out of an ``Authorization`` header value."""

    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


async def get_current_scopes(
    authorization: Optional[str] = Header(default=None),
) -> List[str]:
    """FastAPI dependency returning the caller's granted scope set.

    Default-off: when auth is disabled, returns the full scope set and never
    inspects the request. When enabled, a missing/invalid/
    expired bearer token yields HTTP 401.

    ``authorization`` is bound to the ``Authorization`` request header via
    FastAPI's ``Header`` so the function can be used directly as
    ``Depends(get_current_scopes)``.
    """

    if not is_auth_enabled():
        return list(FULL_SCOPE_SET)

    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return extract_scopes_from_token(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_any_scope(*required: str) -> Callable[..., Any]:
    """FastAPI dependency factory enforcing the caller holds >=1 of ``required``.

    Returns a dependency that resolves the caller's scopes via
    ``get_current_scopes`` and, when auth is enabled, raises HTTP 403 unless the
    granted set contains at least one of ``required``. ``get_current_scopes``
    already returns 401 on a missing/invalid token, so this adds the missing
    *authorization* layer on top of authentication.

    Default-off: when auth is disabled the granted set is the full taxonomy, so
    the check always passes and behavior is byte-for-byte unchanged.
    """

    async def _dep(scopes: List[str] = Depends(get_current_scopes)) -> List[str]:
        if is_auth_enabled() and not any(scope in scopes for scope in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: requires one of {list(required)}",
            )
        return scopes

    return _dep
