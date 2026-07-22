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
import time
from typing import Dict, List

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    delete_terminals_by_session,
    list_terminals_by_session,
)
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


# Bounded confirmation that ``kill_session`` actually reaped the tmux session.
# ``session.kill()`` can return before tmux has finished tearing the session
# down (libtmux's session list is a cached view of ``list-sessions``), so a
# single post-kill ``session_exists`` check can still see the dying session.
# Poll a few times before declaring the kill a failure. Total worst-case wait
# is _KILL_CONFIRM_ATTEMPTS * _KILL_CONFIRM_INTERVAL seconds.
_KILL_CONFIRM_ATTEMPTS = 10
_KILL_CONFIRM_INTERVAL = 0.2


def _confirm_session_gone(session_name: str) -> bool:
    """Return True once the backend reports the tmux session no longer exists.

    Polls ``session_exists`` up to ``_KILL_CONFIRM_ATTEMPTS`` times because a
    freshly-issued kill can race the tmux server's own teardown. Returns False
    if the session is still visible after the last attempt.
    """
    backend = get_backend()
    for _ in range(_KILL_CONFIRM_ATTEMPTS):
        if not backend.session_exists(session_name):
            return True
        time.sleep(_KILL_CONFIRM_INTERVAL)
    return not backend.session_exists(session_name)


def delete_session(session_name: str, registry: PluginRegistry | None = None) -> Dict:
    """Delete session and cleanup, reconciling tmux and the registry atomically.

    Teardown order is deliberately tmux-first, registry-second, and every step
    is idempotent so a re-run reconciles a partially-torn-down session rather
    than erroring (issue caom-9k8):

    1. Tear down each known terminal (kills its tmux window, FIFO reader,
       status buffer, provider) via the event-driven ``delete_terminal`` path.
       That path also deletes the terminal's DB row.
    2. Re-check ``session_exists`` AFTER the terminal loop — killing the last
       window can make tmux drop the whole session, so a pre-loop snapshot of
       "was it alive" is stale by the time we would act on it.
    3. If the tmux session survives, kill it and CONFIRM it is gone (bounded
       poll). ``kill_session``'s success is verified, not assumed: a swallowed
       failure or stale libtmux cache used to leave an orphaned tmux session
       with no registry entry (observation A).
    4. Only once the tmux session is provably gone do we drop any leftover
       registry rows for the session and the forwarded-env mapping. Sweeping
       ``delete_terminals_by_session`` reconciles rows that ``delete_terminal``
       missed (e.g. a row added concurrently, or a terminal whose per-window
       teardown raised) so a caller can never observe a lingering registry
       entry for a dead session (observation B).

    If the tmux session cannot be confirmed dead, we raise WITHOUT deleting the
    remaining registry rows — leaving a re-runnable state (rows still point at
    the surviving session) instead of a permanent orphan, and surfacing the
    failure to the caller rather than reporting a false success.

    Returns:
        Dict with 'deleted' (list of deleted session names) and 'errors' (list of error dicts).
    """
    result: Dict = {"deleted": [], "errors": []}
    try:
        from cli_agent_orchestrator.services import terminal_service

        terminals = list_terminals_by_session(session_name)

        # Step 1: Clean up each terminal (snapshot, kill window, FIFO reader,
        # status buffer, provider, DB) via the event-driven teardown path.
        for terminal in terminals:
            try:
                terminal_service.delete_terminal(terminal["id"], registry=registry)
            except Exception as e:
                logger.warning(f"Failed to cleanup terminal {terminal['id']}: {e}")

        # Step 2/3: Re-check liveness AFTER the loop (killing the last window
        # can drop the session), then kill and CONFIRM the tmux session is
        # gone. Do not trust kill_session's return alone — verify.
        if get_backend().session_exists(session_name):
            get_backend().kill_session(session_name)
            if not _confirm_session_gone(session_name):
                # Leave registry rows in place so a re-run reconciles rather
                # than orphaning the survivor. Surface the failure — never
                # report success while a tmux session lives on.
                raise RuntimeError(
                    f"tmux session '{session_name}' still exists after kill_session; "
                    "registry left intact for reconciliation on re-run"
                )

        # Step 4: tmux session is provably gone. Reconcile any leftover
        # registry rows (idempotent — a no-op when the loop already deleted
        # them all) so no record outlives the dead session.
        delete_terminals_by_session(session_name)

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
