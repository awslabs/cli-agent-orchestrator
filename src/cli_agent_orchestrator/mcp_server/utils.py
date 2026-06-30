"""MCP server utilities.

HTTP-only: like the rest of ``mcp_server``, this module reaches Backplane state
exclusively through the FastAPI surface over HTTP (never through
``clients.database`` / ``clients.tmux``), preserving the auditable MCP boundary
enforced by ``test/test_http_only_boundary.py``.
"""

import logging
from typing import Any, Dict, Optional

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT
from cli_agent_orchestrator.security.auth import get_local_bearer

logger = logging.getLogger(__name__)


def _auth_headers() -> Dict[str, str]:
    """Return the ``Authorization`` header for the internal MCP->API hop, if any.

    Mirrors ``app_tools._auth_headers``: attaches the operator-provisioned
    ``CAO_AUTH_LOCAL_TOKEN`` when the auth layer is enabled, and returns an empty
    mapping default-off so the no-auth posture is byte-for-byte unchanged. Reads
    are not scope-gated today, but the header is attached for consistency so the
    whole MCP->API hop behaves the same with auth on.
    """

    token = get_local_bearer()
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_terminal_record(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Return the terminal record for ``terminal_id`` from the Backplane.

    Fetches the record over HTTP via ``GET /terminals/{id}`` rather than
    touching the database directly, keeping the MCP server inside its
    HTTP-only boundary.

    Args:
        terminal_id: The terminal identifier to look up.

    Returns:
        The terminal record as a dict, or ``None`` if the terminal does not
        exist (HTTP 404) or the Backplane is unreachable.
    """

    try:
        response = requests.get(
            f"{API_BASE_URL}/terminals/{terminal_id}",
            headers=_auth_headers() or None,
            timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Failed to fetch terminal record for %s: %s", terminal_id, exc)
        return None

    if response.status_code == 404:
        return None
    response.raise_for_status()
    record: Dict[str, Any] = response.json()
    return record
