"""Terminal utility functions."""

import asyncio
import time

from cli_agent_orchestrator.models.terminal import TerminalStatus


async def wait_until_status(
    provider_instance,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0
) -> bool:
    """
    Wait until terminal reaches target status or timeout.
    
    Args:
        provider_instance: Provider instance with get_status() method
        target_status: The TerminalStatus to wait for
        timeout: Maximum time to wait in seconds
        polling_interval: Time between status checks in seconds
        
    Returns:
        True if target status reached, False if timeout
    """
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        status = await provider_instance.get_status()
        if status == target_status:
            return True
        await asyncio.sleep(polling_interval)
    
    return False


async def wait_until_terminal_status(
    session_manager,
    terminal_id: str,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0
) -> bool:
    """
    Wait until terminal reaches target status or timeout using session manager.
    
    Args:
        session_manager: Session manager instance with get_terminal() method
        terminal_id: ID of terminal to check
        target_status: The TerminalStatus to wait for
        timeout: Maximum time to wait in seconds
        polling_interval: Time between status checks in seconds
        
    Returns:
        True if target status reached, False if timeout
    """
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        terminal = await session_manager.get_terminal(terminal_id)
        if terminal.status == target_status:
            return True
        await asyncio.sleep(polling_interval)
    
    return False
