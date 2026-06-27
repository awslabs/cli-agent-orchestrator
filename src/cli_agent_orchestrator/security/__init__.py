"""CAO security package — default-off OAuth 2.1 / RFC 9728 auth layer.

Re-exports the scope taxonomy and the auth helpers so callers can do
``from cli_agent_orchestrator.security import get_scopes_for_local_token,
SCOPE_WRITE`` (mirrors how ``mcp_server/app_tools.py`` imports them).

This package must NOT import ``clients.tmux`` / ``clients.database`` so that the
HTTP-only boundary guard (which scans modules importing the app tools) stays
satisfiable.
"""

from cli_agent_orchestrator.security.auth import (
    FULL_SCOPE_SET,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    SCOPES_SUPPORTED,
    extract_scopes_from_token,
    get_authorization_servers,
    get_current_scopes,
    get_expected_audience,
    get_jwks_cache,
    get_jwks_uri,
    get_scopes_for_local_token,
    is_auth_enabled,
)

__all__ = [
    "SCOPE_READ",
    "SCOPE_WRITE",
    "SCOPE_ADMIN",
    "FULL_SCOPE_SET",
    "SCOPES_SUPPORTED",
    "extract_scopes_from_token",
    "get_authorization_servers",
    "get_current_scopes",
    "get_expected_audience",
    "get_jwks_cache",
    "get_jwks_uri",
    "get_scopes_for_local_token",
    "is_auth_enabled",
]
