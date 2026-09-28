"""Recognise the server's own MCP->API service identity (#745).

The interactive owner checks on the create and inbox routes compare a terminal's
recorded owner to the request principal. But the internal MCP->API hop
authenticates with ``CAO_AUTH_LOCAL_TOKEN``, whose principal is a SERVICE
identity, not the human who owns the terminal the callback is about. Without a
way to tell that service principal apart, every legitimate MCP callback naming a
terminal owned by a human would be rejected with 403.

``is_service_principal`` answers "is this the verified service principal?" so
those checks can let the service token name an existing terminal it did not own,
while still rejecting any OTHER principal that names a terminal owned by someone
else. The service identity is derived by verifying ``CAO_AUTH_LOCAL_TOKEN``
through the SAME code path requests use (``extract_principal_from_token``), so
there is one notion of who that token is.

The remaining gap — anyone holding the shared service token can name any
terminal — is the shared-credential limit tracked by #774.

Boundary: standard library plus ``security.auth`` / ``security.principal`` only,
matching those modules.
"""

import logging
import threading
from typing import Optional

from cli_agent_orchestrator.security import auth

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# token -> the verified principal id it carries. Cached so the RS256/JWKS
# verification does not run on every owner check; keyed by the token itself so a
# rotated ``CAO_AUTH_LOCAL_TOKEN`` re-verifies rather than serving a stale id.
_verified_ids: dict = {}


def _service_principal_id() -> Optional[str]:
    """The canonical id of the configured service token, or ``None``.

    ``None`` when auth is off, no local token is set, or the token cannot be
    verified — any of which means there is no service principal to match against.
    """
    token = auth.get_local_bearer()
    if not token:
        return None
    with _lock:
        cached = _verified_ids.get(token)
        if cached is not None:
            return str(cached)
    try:
        principal = auth.extract_principal_from_token(token)
    except Exception:
        # A misconfigured or unverifiable local token has no service identity we
        # can trust; log it and treat it as "no service principal" (fail closed).
        logger.warning(
            "could not verify CAO_AUTH_LOCAL_TOKEN to derive the service principal",
            exc_info=True,
        )
        return None
    with _lock:
        _verified_ids[token] = principal.id
    return principal.id


def is_service_principal(principal) -> bool:
    """Whether ``principal`` is the verified MCP->API service identity.

    True only when auth is enabled, ``CAO_AUTH_LOCAL_TOKEN`` is set, and
    ``principal.id`` equals the id obtained by verifying that token. Any
    verification error yields False (logged in ``_service_principal_id``).
    """
    if not auth.is_auth_enabled():
        return False
    service_id = _service_principal_id()
    if service_id is None:
        return False
    return getattr(principal, "id", None) == service_id
