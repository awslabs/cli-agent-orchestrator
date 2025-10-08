"""Session utilities for CLI Agent Orchestrator."""

import time
import uuid
import logging
import httpx
from cli_agent_orchestrator.constants import SESSION_PREFIX, API_BASE_URL
from cli_agent_orchestrator.models.terminal import TerminalStatus

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


def wait_for_shell(tmux_client, session_name: str, window_name: str, timeout: float = 10.0, polling_interval: float = 0.5) -> bool:
    """Wait for shell to be ready by checking if output is stable (2 consecutive reads are the same and non-empty)."""
    logger.info(f"Waiting for shell to be ready in {session_name}:{window_name}...")
    start_time = time.time()
    previous_output = None
    
    while time.time() - start_time < timeout:
        output = tmux_client.get_history(session_name, window_name)
        
        if output and output.strip() and previous_output is not None and output == previous_output:
            logger.info(f"Shell ready")
            return True
        
        previous_output = output
        time.sleep(polling_interval)
    
    logger.warning(f"Timeout waiting for shell to be ready")
    return False


def wait_until_status(
    provider_instance,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0
) -> bool:
    """Wait until provider reaches target status or timeout."""
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        status = provider_instance.get_status()
        logger.info(f"Waiting for {target_status}, current status: {status}")
        if status == target_status:
            return True
        time.sleep(polling_interval)
    
    return False


def wait_until_terminal_status(
    terminal_id: str,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0
) -> bool:
    """Wait until terminal reaches target status using API endpoint."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = httpx.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=10.0)
            logger.info(response)
            if response.status_code == 200:
                terminal_data = response.json()
                if terminal_data["status"] == target_status.value:
                    return True
        except Exception:
            pass
        time.sleep(polling_interval)
    return False
