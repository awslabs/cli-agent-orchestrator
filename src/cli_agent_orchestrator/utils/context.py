"""Context file injection for agent terminals."""

import logging
from typing import List

from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)


def inject_context_files(terminal_id: str, files: List[str]) -> bool:
    """Inject context files into an agent terminal via /context add.

    Sends the Claude Code '/context add' command to load files into
    the agent's context window. Used by assign-agent and orchestrator.

    Args:
        terminal_id: Terminal to inject into
        files: List of file paths to inject

    Returns:
        True if successful, False on error
    """
    if not files:
        return True
    quoted = " ".join(f'"{f}"' for f in files)
    command = f"/context add {quoted}"
    try:
        terminal_service.send_input(terminal_id, command)
        return True
    except Exception as e:
        logger.error(f"Failed to inject context files into {terminal_id}: {e}")
        return False
