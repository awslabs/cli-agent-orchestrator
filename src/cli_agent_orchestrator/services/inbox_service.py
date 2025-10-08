"""Inbox service with watchdog for automatic message delivery."""

import logging
from pathlib import Path
from watchdog.events import FileSystemEventHandler, FileModifiedEvent

from cli_agent_orchestrator.clients.database import get_pending_messages, update_message_status
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)


def check_and_send_pending_messages(terminal_id: str) -> bool:
    """Check for pending messages and send if terminal is ready.
    
    Args:
        terminal_id: Terminal ID to check messages for
        
    Returns:
        bool: True if a message was sent, False otherwise
        
    Raises:
        ValueError: If provider not found for terminal
    """
    # Check for pending messages
    messages = get_pending_messages(terminal_id, limit=1)
    if not messages:
        return False
    
    message = messages[0]
    
    # Get provider and check status
    provider = provider_manager.get_provider(terminal_id)
    status = provider.get_status()
    
    if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
        logger.debug(f"Terminal {terminal_id} not ready (status={status}), message {message.id} pending")
        return False
    
    # Send message
    try:
        terminal_service.send_input(terminal_id, message.message)
        update_message_status(message.id, MessageStatus.DELIVERED)
        logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send message {message.id} to {terminal_id}: {e}")
        update_message_status(message.id, MessageStatus.FAILED)
        raise


class LogFileHandler(FileSystemEventHandler):
    """Handler for terminal log file changes."""
    
    def on_modified(self, event):
        """Handle file modification events."""
        if isinstance(event, FileModifiedEvent) and event.src_path.endswith('.log'):
            log_path = Path(event.src_path)
            terminal_id = log_path.stem
            self._handle_log_change(terminal_id)
    
    def _handle_log_change(self, terminal_id: str):
        """Handle log file change and attempt message delivery."""
        try:
            # Check for pending messages first
            messages = get_pending_messages(terminal_id, limit=1)
            if not messages:
                return
            
            # Attempt delivery
            check_and_send_pending_messages(terminal_id)
                
        except Exception as e:
            logger.error(f"Error handling log change for {terminal_id}: {e}")
