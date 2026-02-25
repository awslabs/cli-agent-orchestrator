"""Session service for session-level operations."""

import logging
from typing import Dict, List

from cli_agent_orchestrator.clients.database import (
    delete_terminals_by_session,
    list_terminals_by_session,
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.providers.manager import provider_manager

logger = logging.getLogger(__name__)


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        tmux_sessions = tmux_client.list_sessions()
        return [s for s in tmux_sessions if s["id"].startswith(SESSION_PREFIX)]
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals.

    Automatically cleans up database records for terminals whose tmux windows no longer exist.
    If the session itself doesn't exist in tmux, cleans up all associated terminal records.
    """
    try:
        # Get session from tmux
        session_data = tmux_client.get_session(session_name)
        if not session_data:
            # Session doesn't exist - clean up any orphaned terminal records
            terminals = list_terminals_by_session(session_name)
            if terminals:
                logger.info(
                    f"Cleaning up {len(terminals)} orphaned terminals for non-existent session {session_name}"
                )
                delete_terminals_by_session(session_name)
            raise ValueError(f"Session '{session_name}' not found")

        # Get terminals and filter out those whose tmux windows no longer exist
        from cli_agent_orchestrator.clients.database import delete_terminal

        terminals = list_terminals_by_session(session_name)
        active_terminals = []

        for terminal in terminals:
            if tmux_client.window_exists(terminal["tmux_session"], terminal["tmux_window"]):
                active_terminals.append(terminal)
            else:
                logger.info(f"Cleaning up terminal {terminal['id']} - tmux window no longer exists")
                delete_terminal(terminal["id"])

        return {"session": session_data, "terminals": active_terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(session_name: str) -> bool:
    """Delete session and cleanup with verification."""
    try:
        if not tmux_client.session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)

        # Cleanup providers with error tracking
        cleanup_errors = []
        for terminal in terminals:
            try:
                provider_manager.cleanup_provider(terminal["id"])
            except Exception as e:
                cleanup_errors.append(f"Provider cleanup failed for {terminal['id']}: {e}")
                logger.error(cleanup_errors[-1])

        # Kill tmux session and verify
        kill_success = tmux_client.kill_session(session_name)
        if not kill_success:
            logger.warning(f"kill_session returned False for {session_name}")

        # Verify session is actually gone
        if tmux_client.session_exists(session_name):
            raise RuntimeError(f"Tmux session {session_name} still exists after kill attempt")

        # Delete terminal metadata
        deleted_count = delete_terminals_by_session(session_name)
        logger.info(f"Deleted {deleted_count} terminal records for session {session_name}")

        # Delete log files for all terminals
        from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR

        for terminal in terminals:
            log_path = TERMINAL_LOG_DIR / f"{terminal['id']}.log"
            try:
                log_path.unlink(missing_ok=True)
                logger.debug(f"Deleted log file for terminal {terminal['id']}")
            except Exception as e:
                logger.warning(f"Failed to delete log file for {terminal['id']}: {e}")

        if cleanup_errors:
            logger.warning(f"Session deleted but with {len(cleanup_errors)} cleanup errors")

        logger.info(f"Deleted session: {session_name}")
        return True

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
