"""Session utilities for CLI Agent Orchestrator."""

import logging
import time
import uuid
from typing import TYPE_CHECKING, Union

import httpx
import requests

from cli_agent_orchestrator.constants import API_BASE_URL, SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import TerminalStatus

if TYPE_CHECKING:
    from cli_agent_orchestrator.clients.tmux import TmuxClient
    from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)


def generate_session_name() -> str:
    """Generate a unique session name with SESSION_PREFIX."""
    session_uuid = uuid.uuid4().hex[:8]
    return f"{SESSION_PREFIX}{session_uuid}"


def generate_terminal_id() -> str:
    """Generate terminal ID without prefix."""
    return uuid.uuid4().hex[:8]


def generate_window_name(agent_profile: str) -> str:
    """Generate window name from agent profile with unique suffix."""
    return f"{agent_profile}-{uuid.uuid4().hex[:4]}"


def wait_for_shell(
    tmux_client: "TmuxClient",
    session_name: str,
    window_name: str,
    timeout: float = 10.0,
    polling_interval: float = 0.5,
) -> bool:
    """Wait for shell to be ready by checking if output is stable (2 consecutive reads are the same and non-empty).

    On Windows, once the shell is detected ready, sends a one-shot
    ``Set-PSReadLineOption -AddToHistoryHandler { 'SkipAdding' }`` so that
    subsequent CAO-injected commands (warm-ups, agent launch) don't pollute
    the user's persistent PSReadLine history file.
    """
    from cli_agent_orchestrator.clients.tmux import disable_pwsh_history

    logger.info(f"Waiting for shell to be ready in {session_name}:{window_name}...")
    start_time = time.time()
    previous_output = None

    while time.time() - start_time < timeout:
        output = tmux_client.get_history(session_name, window_name)

        if output and output.strip() and previous_output is not None and output == previous_output:
            logger.info(f"Shell ready")
            disable_pwsh_history(tmux_client, session_name, window_name)
            return True

        previous_output = output
        time.sleep(polling_interval)

    logger.warning(f"Timeout waiting for shell to be ready")
    return False


def wait_until_status(
    provider_instance: "BaseProvider",
    target_status: "TerminalStatus | set[TerminalStatus]",
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until provider reaches target status or timeout."""
    targets = target_status if isinstance(target_status, set) else {target_status}
    start_time = time.time()

    while time.time() - start_time < timeout:
        status = provider_instance.get_status()
        target_str = ", ".join(s.value for s in targets)
        logger.info(f"Waiting for {{{target_str}}}, current status: {status}")
        if status in targets:
            return True
        time.sleep(polling_interval)

    return False


def poll_until_done(terminal_id: str, timeout: float, polling_interval: float = 1.0) -> None:
    """Poll terminal status until completed/error or timeout.

    Raises click.ClickException on error, timeout, or request failure.
    """
    import click

    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            raise click.ClickException(
                f"Timed out after {int(elapsed)}s waiting for terminal {terminal_id}"
            )
        try:
            resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
            resp.raise_for_status()
            status = resp.json().get("status")
            if status == TerminalStatus.COMPLETED.value:
                return
            if status == TerminalStatus.ERROR.value:
                raise click.ClickException("Terminal reached ERROR status")
        except requests.exceptions.RequestException as e:
            raise click.ClickException(f"Failed to poll terminal status: {e}")
        time.sleep(polling_interval)


def wait_until_terminal_status(
    terminal_id: str,
    target_status: Union[TerminalStatus, set],
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status using API endpoint.

    Args:
        terminal_id: Terminal to poll.
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
            response = httpx.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=10.0)
            logger.info(response)
            if response.status_code == 200:
                terminal_data = response.json()
                if terminal_data["status"] in target_values:
                    return True
        except Exception:
            pass
        time.sleep(polling_interval)
    return False
