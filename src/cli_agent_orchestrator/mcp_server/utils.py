"""MCP server utilities.

HTTP-only: like the rest of ``mcp_server``, this module reaches Backplane state
exclusively through the FastAPI surface over HTTP (never through
``clients.database`` / ``clients.tmux``), preserving the auditable MCP boundary
enforced by ``test/test_http_only_boundary.py`` (Requirement 7).
"""

import logging
from typing import Any, Dict, Optional

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


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
            f"{API_BASE_URL}/terminals/{terminal_id}", timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        logger.warning("Failed to fetch terminal record for %s: %s", terminal_id, exc)
        return None

    if response.status_code == 404:
        return None
    response.raise_for_status()
    record: Dict[str, Any] = response.json()
    return record
