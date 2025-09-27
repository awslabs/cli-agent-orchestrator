"""Session utilities for CLI Agent Orchestrator."""

import uuid
from pathlib import Path
from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR


def generate_session_name(base_name: str = "cao") -> str:
    """Generate a unique session name with format base-uuid."""
    session_uuid = uuid.uuid4().hex[:8]
    return f"{base_name}-{session_uuid}"


def get_terminal_log_path(terminal_id: str) -> Path:
    """Get the log file path for a terminal."""
    TERMINAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return TERMINAL_LOG_DIR / f"terminal-{terminal_id}.log"

# TODO: Move the generate terminal name logic here.