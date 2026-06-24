"""Session service for session-level operations.

This module provides session management functionality for CAO, where a "session"
corresponds to a tmux session that may contain multiple terminal windows (agents).

Session Hierarchy:
- Session: A tmux session (e.g., "cao-my-project")
  - Terminal: A tmux window within the session (e.g., "developer-abc123")
    - Provider: The CLI agent running in the terminal (e.g., KiroCliProvider)

Key Operations:
- list_sessions(): Get all CAO-managed sessions (filtered by SESSION_PREFIX)
- get_session(): Get session details including all terminal metadata
- delete_session(): Clean up session, providers, database records, and tmux session

Session Lifecycle:
1. create_terminal() with new_session=True creates a new tmux session
2. Additional terminals are added via create_terminal() with new_session=False
3. delete_session() removes the entire session and all contained terminals
"""

import logging
from typing import Dict, List

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateSessionEvent,
    PostKillSessionEvent,
)
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import clear_session_env
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

logger = logging.getLogger(__name__)


async def create_session(
    provider: str | None,
    agent_profile: str,
    session_name: str | None = None,
    working_directory: str | None = None,
    allowed_tools: list[str] | None = None,
    registry: PluginRegistry | None = None,
    env_vars: dict[str, str] | None = None,
) -> Terminal:
    """Create a new session by creating its initial terminal.

    ``env_vars`` are operator-forwarded env vars from ``cao launch --env``.
    They are persisted on the session record so every worker spawned later
    in the same session inherits them. See issue #248.
    """
    if provider is None:
        resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
    else:
        resolved_provider = provider

    terminal = await create_terminal(
        provider=resolved_provider,
        agent_profile=agent_profile,
        session_name=session_name,
        new_session=True,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
        registry=registry,
        env_vars=env_vars,
    )
    dispatch_plugin_event(
        registry,
        "post_create_session",
        PostCreateSessionEvent(
            session_id=terminal.session_name,
            session_name=terminal.session_name,
        ),
    )
    return terminal


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        tmux_sessions = get_backend().list_sessions()
        return [s for s in tmux_sessions if s["id"].startswith(SESSION_PREFIX)]
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals."""
    try:
        if not get_backend().session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        tmux_sessions = get_backend().list_sessions()
        session_data = next((s for s in tmux_sessions if s["id"] == session_name), None)

        if not session_data:
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)
        # Enrich each terminal with its live status. list_terminals_by_session
        # reads only the DB row (no status column), but callers monitoring an
        # orchestration — the web UI, and the cao-ops-mcp get_session_info tool
        # an external supervisor polls — need to distinguish
        # IDLE/PROCESSING/COMPLETED/ERROR per terminal. status_monitor is the
        # single source of truth and is backend-aware (tmux push vs herdr
        # native), so derive it here rather than persisting a stale column.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        for terminal in terminals:
            terminal["status"] = status_monitor.get_status(terminal["id"]).value
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(session_name: str, registry: PluginRegistry | None = None) -> Dict:
    """Delete session and cleanup.

    Returns:
        Dict with 'deleted' (list of deleted session names) and 'errors' (list of error dicts).
    """
    result: Dict = {"deleted": [], "errors": []}
    try:
        session_alive = get_backend().session_exists(session_name)

        from cli_agent_orchestrator.services import terminal_service

        terminals = list_terminals_by_session(session_name)

        # Clean up each terminal (snapshot, kill window, FIFO reader,
        # status buffer, provider, DB) via the event-driven teardown path.
        for terminal in terminals:
            try:
                terminal_service.delete_terminal(terminal["id"], registry=registry)
            except Exception as e:
                logger.warning(f"Failed to cleanup terminal {terminal['id']}: {e}")

        # Kill backend session only if it still exists
        if session_alive:
            get_backend().kill_session(session_name)

        # Drop the per-session forwarded-env mapping (issue #248). Safe
        # even when no vars were forwarded — the helper is a no-op then.
        clear_session_env(session_name)

        result["deleted"].append(session_name)
        logger.info(f"Deleted session: {session_name}")
        dispatch_plugin_event(
            registry,
            "post_kill_session",
            PostKillSessionEvent(session_id=session_name, session_name=session_name),
        )
        return result

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
