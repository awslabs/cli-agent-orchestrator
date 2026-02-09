"""Terminal service with workflow functions.

This module provides high-level terminal management operations that orchestrate
multiple components (database, tmux, providers) to create a unified terminal
abstraction for CLI agents.

Key Responsibilities:
- Terminal lifecycle management (create, get, delete)
- Provider initialization and cleanup
- Tmux session/window management
- Terminal output capture and message extraction

Terminal Workflow:
1. create_terminal() → Creates tmux window, initializes provider, starts logging
2. send_input() → Sends user message to the agent via tmux
3. get_output() → Retrieves agent response from terminal history
4. delete_terminal() → Cleans up provider, database record, and logging
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    get_terminal_metadata,
    update_last_active,
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import SESSION_PREFIX, TERMINAL_LOG_DIR
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
)

logger = logging.getLogger(__name__)


class OutputMode(str, Enum):
    """Output mode for terminal history retrieval.

    FULL: Returns complete terminal output (scrollback buffer)
    LAST: Returns only the last agent response (extracted by provider)
    """

    FULL = "full"
    LAST = "last"


def create_terminal(
    provider: str,
    agent_profile: str,
    session_name: Optional[str] = None,
    new_session: bool = False,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create a new terminal with an initialized CLI agent.

    This function orchestrates the complete terminal creation workflow:
    1. Generate unique terminal ID and window name
    2. Create tmux session/window (new or existing)
    3. Save terminal metadata to database
    4. Initialize the CLI provider (starts the agent)
    5. Set up terminal logging via tmux pipe-pane

    Args:
        provider: Provider type string (e.g., "kiro_cli", "claude_code")
        agent_profile: Name of the agent profile to use
        session_name: Optional custom session name. If not provided, auto-generated.
        new_session: If True, creates a new tmux session. If False, adds to existing.
        working_directory: Optional working directory for the terminal shell.

    Returns:
        Terminal object with all metadata populated

    Raises:
        ValueError: If session already exists (new_session=True) or not found (new_session=False)
        TimeoutError: If provider initialization times out
    """
    try:
        # Step 1: Generate unique identifiers
        terminal_id = generate_terminal_id()

        if not session_name:
            session_name = generate_session_name()

        window_name = generate_window_name(agent_profile)

        # Step 2: Create tmux session or window
        if new_session:
            # Ensure session name has the CAO prefix for identification
            if not session_name.startswith(SESSION_PREFIX):
                session_name = f"{SESSION_PREFIX}{session_name}"

            # Prevent duplicate sessions
            if tmux_client.session_exists(session_name):
                raise ValueError(f"Session '{session_name}' already exists")

            # Create new tmux session with initial window
            tmux_client.create_session(session_name, window_name, terminal_id, working_directory)
        else:
            # Add window to existing session
            if not tmux_client.session_exists(session_name):
                raise ValueError(f"Session '{session_name}' not found")
            window_name = tmux_client.create_window(
                session_name, window_name, terminal_id, working_directory
            )

        # Step 3: Persist terminal metadata to database
        db_create_terminal(terminal_id, session_name, window_name, provider, agent_profile)

        # Step 4: Create and initialize the CLI provider
        # This starts the agent (e.g., runs "kiro-cli chat --agent developer")
        provider_instance = provider_manager.create_provider(
            provider, terminal_id, session_name, window_name, agent_profile
        )
        provider_instance.initialize()

        # Step 5: Set up terminal logging via tmux pipe-pane
        # This captures all terminal output to a log file for inbox monitoring
        log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
        log_path.touch()  # Ensure file exists before watching
        tmux_client.pipe_pane(session_name, window_name, str(log_path))

        # Build and return the Terminal object
        terminal = Terminal(
            id=terminal_id,
            name=window_name,
            provider=ProviderType(provider),
            session_name=session_name,
            agent_profile=agent_profile,
            status=TerminalStatus.IDLE,
            last_active=datetime.now(),
        )

        logger.info(
            f"Created terminal: {terminal_id} in session: {session_name} (new_session={new_session})"
        )
        return terminal

    except Exception as e:
        # Cleanup on failure: kill the session if we created one
        logger.error(f"Failed to create terminal: {e}")
        if new_session and session_name:
            try:
                tmux_client.kill_session(session_name)
            except:
                pass  # Ignore cleanup errors
        raise


def get_terminal(terminal_id: str) -> Dict:
    """Get terminal data."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Get status from provider
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            raise ValueError(f"Provider not found for terminal {terminal_id}")
        status = provider.get_status().value

        return {
            "id": metadata["id"],
            "name": metadata["tmux_window"],
            "provider": metadata["provider"],
            "session_name": metadata["tmux_session"],
            "agent_profile": metadata["agent_profile"],
            "status": status,
            "last_active": metadata["last_active"],
        }

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


def get_working_directory(terminal_id: str) -> Optional[str]:
    """Get the current working directory of a terminal's pane.

    Args:
        terminal_id: The terminal identifier

    Returns:
        Working directory path, or None if pane has no directory

    Raises:
        ValueError: If terminal not found
        Exception: If unable to query working directory
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        working_dir = tmux_client.get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]
        )
        return working_dir

    except Exception as e:
        logger.error(f"Failed to get working directory for terminal {terminal_id}: {e}")
        raise


def send_input(terminal_id: str, message: str) -> bool:
    """Send input to terminal via bracketed paste.

    Uses tmux paste buffer with bracketed paste mode (-p) to bypass TUI hotkey
    handling. Without this, characters like '!' in user messages can trigger
    Gemini CLI's shell mode toggle and similar TUI hotkeys.
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        tmux_client.send_keys(metadata["tmux_session"], metadata["tmux_window"], message)

        update_last_active(terminal_id)
        logger.info(f"Sent input to terminal: {terminal_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send input to terminal {terminal_id}: {e}")
        raise


def send_special_key(terminal_id: str, key: str) -> bool:
    """Send a tmux special key sequence (e.g., C-d, C-c) to terminal.

    Unlike send_input(), this sends the key as a tmux key name (not literal text)
    and does not append a carriage return. Used for control signals like Ctrl+D (EOF).

    Args:
        terminal_id: Target terminal identifier
        key: Tmux key name (e.g., "C-d", "C-c", "Escape")

    Returns:
        True if the key was sent successfully

    Raises:
        ValueError: If terminal not found
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        tmux_client.send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)

        update_last_active(terminal_id)
        logger.info(f"Sent special key '{key}' to terminal: {terminal_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send special key to terminal {terminal_id}: {e}")
        raise


def get_output(terminal_id: str, mode: OutputMode = OutputMode.FULL) -> str:
    """Get terminal output."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        full_output = tmux_client.get_history(metadata["tmux_session"], metadata["tmux_window"])

        if mode == OutputMode.FULL:
            return full_output
        elif mode == OutputMode.LAST:
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                raise ValueError(f"Provider not found for terminal {terminal_id}")
            return provider.extract_last_message_from_script(full_output)

    except Exception as e:
        logger.error(f"Failed to get output from terminal {terminal_id}: {e}")
        raise


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal."""
    try:
        # Get metadata before deletion
        metadata = get_terminal_metadata(terminal_id)

        # Stop pipe-pane
        if metadata:
            try:
                tmux_client.stop_pipe_pane(metadata["tmux_session"], metadata["tmux_window"])
            except Exception as e:
                logger.warning(f"Failed to stop pipe-pane for {terminal_id}: {e}")

        # Existing cleanup
        provider_manager.cleanup_provider(terminal_id)
        deleted = db_delete_terminal(terminal_id)
        logger.info(f"Deleted terminal: {terminal_id}")
        return deleted

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise
