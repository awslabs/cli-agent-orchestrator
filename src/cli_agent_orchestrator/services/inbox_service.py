"""Inbox service for terminal-to-terminal messaging with file-watching."""

import asyncio
import logging
from pathlib import Path
from typing import Dict
from watchfiles import awatch

from cli_agent_orchestrator.clients.database import (
    get_pending_messages,
    update_message_status,
    get_terminal_metadata
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.constants import TERMINAL_LOG_TAIL_LINES, LOG_DIR

# Setup dedicated inbox logger
logger = logging.getLogger(__name__)
inbox_log_file = LOG_DIR / "inbox.log"
inbox_handler = logging.FileHandler(inbox_log_file)
inbox_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(inbox_handler)
logger.setLevel(logging.INFO)

# Module-level state
_watch_tasks: Dict[str, asyncio.Task] = {}  # {terminal_id: Task}
_providers: Dict[str, BaseProvider] = {}  # {terminal_id: provider}


def register_terminal(terminal_id: str, log_path: str, provider: BaseProvider):
    """Register terminal for inbox message delivery.
    
    Args:
        terminal_id: Terminal ID
        log_path: Path to terminal log file
        provider: Provider instance for status checking
    """
    logger.info(f"[register_terminal] Starting registration for terminal {terminal_id}")
    logger.info(f"[register_terminal] Log path: {log_path}")
    
    if terminal_id in _watch_tasks:
        logger.warning(f"[register_terminal] Terminal {terminal_id} already registered, skipping")
        return
    
    # Store provider reference
    _providers[terminal_id] = provider
    logger.info(f"[register_terminal] Stored provider for {terminal_id}")
    
    # Spawn async watch task
    loop = asyncio.get_event_loop()
    task = loop.create_task(_watch_log_file(terminal_id, log_path))
    _watch_tasks[terminal_id] = task
    
    logger.info(f"[register_terminal] Successfully registered terminal {terminal_id} with inbox service")
    logger.info(f"[register_terminal] Active watchers: {len(_watch_tasks)}")


def unregister_terminal(terminal_id: str):
    """Unregister terminal and stop file watcher.
    
    Args:
        terminal_id: Terminal ID
    """
    if terminal_id in _watch_tasks:
        task = _watch_tasks[terminal_id]
        task.cancel()
        del _watch_tasks[terminal_id]
        logger.info(f"Unregistered terminal {terminal_id} from inbox service")
    
    if terminal_id in _providers:
        del _providers[terminal_id]


async def shutdown_all():
    """Shutdown all watchers gracefully."""
    logger.info(f"Shutting down {len(_watch_tasks)} inbox watchers...")
    for terminal_id, task in list(_watch_tasks.items()):
        task.cancel()
    _watch_tasks.clear()
    _providers.clear()
    logger.info("All inbox watchers shut down")


def check_and_send_pending_messages(terminal_id: str):
    """Check if terminal is ready and send pending messages immediately.
    
    Called when a new message is sent to check if receiver is already ready.
    
    Args:
        terminal_id: Terminal ID (receiver)
    """
    logger.info(f"[check_and_send] Called for terminal {terminal_id}")
    
    provider = _providers.get(terminal_id)
    if not provider:
        logger.info(f"[check_and_send] Terminal {terminal_id} not registered with inbox service, skipping immediate send")
        logger.info(f"[check_and_send] Registered terminals: {list(_providers.keys())}")
        return
    
    logger.info(f"[check_and_send] Provider found for {terminal_id}, checking status")
    
    # Check if terminal is ready (IDLE or COMPLETED)
    if _is_terminal_ready(provider):
        logger.info(f"[check_and_send] Terminal {terminal_id} is ready, sending message")
        _send_next_message(terminal_id)
    else:
        status = provider.get_status()
        logger.info(f"[check_and_send] Terminal {terminal_id} not ready (status: {status}), will wait for file watcher")


def _is_terminal_ready(provider: BaseProvider) -> bool:
    """Check if terminal is in a ready state (IDLE or COMPLETED).
    
    Args:
        provider: Provider instance
        
    Returns:
        True if terminal is ready to receive messages
    """
    status = provider.get_status()
    is_ready = status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
    logger.info(f"[_is_terminal_ready] Status: {status}, Ready: {is_ready}")
    return is_ready


async def _watch_log_file(terminal_id: str, log_path: str):
    """Watch log file for changes and trigger message delivery.
    
    Args:
        terminal_id: Terminal ID
        log_path: Path to log file
    """
    logger.info(f"[_watch_log_file] Starting file watcher for terminal {terminal_id}")
    logger.info(f"[_watch_log_file] Watching: {log_path}")
    try:
        async for changes in awatch(log_path):
            logger.info(f"[_watch_log_file] File change detected for {terminal_id}: {len(changes)} changes")
            for change_type, changed_path in changes:
                if str(changed_path) == log_path:
                    logger.info(f"[_watch_log_file] Processing change for {terminal_id}")
                    _on_log_change(terminal_id, log_path)
    except asyncio.CancelledError:
        logger.info(f"[_watch_log_file] Watch task cancelled for terminal {terminal_id}")


def _on_log_change(terminal_id: str, log_path: str):
    """Handle log file change event.
    
    Args:
        terminal_id: Terminal ID
        log_path: Path to log file
    """
    logger.info(f"[_on_log_change] Handling log change for terminal {terminal_id}")
    
    # Read last N lines
    last_lines = _read_last_lines(log_path, TERMINAL_LOG_TAIL_LINES)
    if not last_lines:
        logger.info(f"[_on_log_change] No lines read from {log_path}")
        return
    
    logger.info(f"[_on_log_change] Read {len(last_lines)} characters from last {TERMINAL_LOG_TAIL_LINES} lines")
    
    # Quick pattern check
    provider = _providers.get(terminal_id)
    if not provider:
        logger.warning(f"[_on_log_change] No provider found for {terminal_id}")
        return
    
    idle_patterns = provider.get_idle_patterns()
    logger.info(f"[_on_log_change] Checking for idle patterns in output")
    
    if not any(pattern in last_lines for pattern in idle_patterns):
        logger.info(f"[_on_log_change] No IDLE patterns detected, skipping status check")
        return  # No IDLE patterns detected, skip expensive status check
    
    logger.info(f"[_on_log_change] IDLE pattern detected, checking terminal status")
    
    # Check if terminal is ready (IDLE or COMPLETED)
    if _is_terminal_ready(provider):
        logger.info(f"[_on_log_change] Terminal ready, sending next message")
        _send_next_message(terminal_id)
    else:
        logger.info(f"[_on_log_change] Terminal not ready yet")


def _read_last_lines(file_path: str, n: int) -> str:
    """Read last N lines from file.
    
    Args:
        file_path: Path to file
        n: Number of lines to read
        
    Returns:
        Last N lines as string
    """
    with open(file_path, 'r') as f:
        lines = f.readlines()
        return ''.join(lines[-n:])


def _send_next_message(terminal_id: str):
    """Send next pending message to terminal.
    
    Args:
        terminal_id: Terminal ID (receiver)
    """
    logger.info(f"[_send_next_message] Called for terminal {terminal_id}")
    
    # Get oldest pending message
    messages = get_pending_messages(terminal_id, limit=1)
    if not messages:
        logger.info(f"[_send_next_message] No pending messages for {terminal_id}")
        return
    
    message = messages[0]
    logger.info(f"[_send_next_message] Found pending message {message.id}")
    logger.info(f"[_send_next_message] Message content: {message.message}")
    logger.info(f"[_send_next_message] From: {message.sender_id} To: {message.receiver_id}")
    
    # Get terminal metadata
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        logger.error(f"[_send_next_message] Terminal {terminal_id} not found in database")
        update_message_status(message.id, MessageStatus.FAILED)
        raise ValueError(f"Terminal {terminal_id} not found")
    
    logger.info(f"[_send_next_message] Sending to tmux session: {metadata['tmux_session']}, window: {metadata['tmux_window']}")
    
    # Send message via tmux
    try:
        tmux_client.send_keys(
            metadata['tmux_session'],
            metadata['tmux_window'],
            message.message
        )
        update_message_status(message.id, MessageStatus.DELIVERED)
        logger.info(f"[_send_next_message] Successfully delivered message {message.id} to terminal {terminal_id}")
    except Exception as e:
        update_message_status(message.id, MessageStatus.FAILED)
        logger.error(f"[_send_next_message] Failed to send message {message.id} to terminal {terminal_id}: {e}")
        raise
