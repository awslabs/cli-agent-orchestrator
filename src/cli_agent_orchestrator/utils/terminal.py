"""Session utilities for CLI Agent Orchestrator."""

import logging
import time
import uuid
from typing import Union

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)


def generate_session_name() -> str:
    """Generate a unique session name with SESSION_PREFIX."""
    return f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"


def generate_terminal_id() -> str:
    """Generate terminal ID without prefix."""
    return uuid.uuid4().hex[:8]


def generate_window_name(agent_profile: str) -> str:
    """Generate window name from agent profile with unique suffix."""
    return f"{agent_profile}-{uuid.uuid4().hex[:4]}"


def wait_for_shell(terminal_id: str, timeout: float = 10.0, polling_interval: float = 0.5) -> bool:
    """Wait for shell to be ready by polling status_monitor."""
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    start = time.time()
    while time.time() - start < timeout:
        if status_monitor.get_status(terminal_id) == TerminalStatus.IDLE:
            return True
        time.sleep(polling_interval)
    logger.warning(f"Timeout waiting for shell to be ready for {terminal_id}")
    return False


def wait_until_status(
    terminal_id: str,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status by polling status_monitor."""
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    start = time.time()
    while time.time() - start < timeout:
        if status_monitor.get_status(terminal_id) == target_status:
            return True
        time.sleep(polling_interval)
    return False


def wait_until_terminal_status(
    terminal_id: str,
    target_status: Union[TerminalStatus, set],
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status by polling GET /terminals/{id}.

    Args:
        terminal_id: Terminal to poll status for.
        target_status: A single TerminalStatus or a set of acceptable statuses.
        timeout: Maximum wait time in seconds.
        polling_interval: Seconds between polls.

    Returns:
        True if the terminal reached one of the target statuses within timeout.
    """
    if isinstance(target_status, TerminalStatus):
        target_values = {target_status.value}
    else:
        target_values = {s.value for s in target_status}

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=5.0)
            if response.status_code == 200:
                current_status = response.json().get("status")
                if current_status in target_values:
                    return True
        except Exception:
            pass
        time.sleep(polling_interval)
    return False
