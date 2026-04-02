"""Session service for session-level operations."""

import logging
from typing import Dict, List, Callable, Optional

from cli_agent_orchestrator.clients.database import (
    delete_terminals_by_session,
    list_terminals_by_session,
    get_children_sessions,
    set_terminal_bead,
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
    """Get session with terminals."""
    try:
        if not tmux_client.session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        tmux_sessions = tmux_client.list_sessions()
        session_data = next((s for s in tmux_sessions if s["id"] == session_name), None)

        if not session_data:
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def get_session_children(session_name: str) -> List[str]:
    """Get child session IDs for a session."""
    return get_children_sessions(session_name)


def delete_session(session_name: str, on_progress: Optional[Callable[[str, str], None]] = None) -> Dict:
    """Delete session and cleanup. Returns dict with deleted sessions and any errors."""
    result = {"deleted": [], "errors": []}
    
    def report(action: str, target: str):
        if on_progress:
            on_progress(action, target)
    
    try:
        if not tmux_client.session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        # First, recursively delete children
        children = get_children_sessions(session_name)
        for child_session in children:
            report("deleting_child", child_session)
            try:
                child_result = delete_session(child_session, on_progress)
                result["deleted"].extend(child_result["deleted"])
                result["errors"].extend(child_result["errors"])
            except Exception as e:
                result["errors"].append({"session": child_session, "error": str(e)})

        # Now delete this session
        report("deleting", session_name)
        terminals = list_terminals_by_session(session_name)

        # Clear bead_id on any terminals that have one (release the bead)
        for terminal in terminals:
            if terminal.get("bead_id"):
                set_terminal_bead(terminal["id"], None)

        # Cleanup providers (don't let failures block)
        for terminal in terminals:
            try:
                provider_manager.cleanup_provider(terminal["id"])
            except Exception:
                pass

        # Kill tmux session
        tmux_client.kill_session(session_name)

        # Delete terminal metadata
        delete_terminals_by_session(session_name)

        result["deleted"].append(session_name)
        logger.info(f"Deleted session: {session_name}")
        return result

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
