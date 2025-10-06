"""Inbox service for terminal-to-terminal messaging with file-watching."""

import logging
from pathlib import Path
from typing import Dict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent

from cli_agent_orchestrator.clients.database import (
    get_pending_messages,
    update_message_status,
    get_terminal_metadata
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.constants import TERMINAL_LOG_TAIL_LINES

logger = logging.getLogger(__name__)


class TerminalLogHandler(FileSystemEventHandler):
    """Handler for terminal log file changes."""
    
    def __init__(self, terminal_id: str, provider: BaseProvider, inbox_service):
        self.terminal_id = terminal_id
        self.provider = provider
        self.inbox_service = inbox_service
    
    def on_modified(self, event: FileModifiedEvent):
        """Handle file modification events."""
        if event.is_directory:
            return
        
        try:
            self.inbox_service._on_log_change(self.terminal_id, event.src_path)
        except Exception as e:
            logger.error(f"Error handling log change for {self.terminal_id}: {e}")


class InboxService:
    """Singleton service for managing inbox message delivery via file watching."""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._watchers: Dict[str, Observer] = {}  # {terminal_id: Observer}
        self._providers: Dict[str, BaseProvider] = {}  # {terminal_id: provider}
        self._initialized = True
        logger.info("InboxService initialized")
    
    def register_terminal(self, terminal_id: str, log_path: str, provider: BaseProvider):
        """Register terminal for inbox message delivery.
        
        Args:
            terminal_id: Terminal ID
            log_path: Path to terminal log file
            provider: Provider instance for status checking
        """
        if terminal_id in self._watchers:
            logger.warning(f"Terminal {terminal_id} already registered")
            return
        
        # Store provider reference
        self._providers[terminal_id] = provider
        
        # Create file watcher
        event_handler = TerminalLogHandler(terminal_id, provider, self)
        observer = Observer()
        observer.schedule(event_handler, path=str(Path(log_path).parent), recursive=False)
        observer.start()
        
        self._watchers[terminal_id] = observer
        logger.info(f"Registered terminal {terminal_id} with inbox service")
    
    def unregister_terminal(self, terminal_id: str):
        """Unregister terminal and stop file watcher.
        
        Args:
            terminal_id: Terminal ID
        """
        if terminal_id in self._watchers:
            observer = self._watchers[terminal_id]
            observer.stop()
            observer.join(timeout=5)
            del self._watchers[terminal_id]
            logger.info(f"Unregistered terminal {terminal_id} from inbox service")
        
        if terminal_id in self._providers:
            del self._providers[terminal_id]
    
    def _on_log_change(self, terminal_id: str, log_path: str):
        """Handle log file change event.
        
        Args:
            terminal_id: Terminal ID
            log_path: Path to log file
        """
        # Read last N lines
        last_lines = self._read_last_lines(log_path, TERMINAL_LOG_TAIL_LINES)
        if not last_lines:
            return
        
        # Quick pattern check
        provider = self._providers.get(terminal_id)
        if not provider:
            return
        
        idle_patterns = provider.get_idle_patterns()
        if not any(pattern in last_lines for pattern in idle_patterns):
            return  # No IDLE patterns detected, skip expensive status check
        
        # Confirm IDLE status via provider
        try:
            status = provider.get_status()
            if status != TerminalStatus.IDLE:
                return
        except Exception as e:
            logger.error(f"Error checking status for {terminal_id}: {e}")
            return
        
        # Terminal is IDLE, try to send next message
        self._send_next_message(terminal_id)
    
    def _read_last_lines(self, file_path: str, n: int) -> str:
        """Read last N lines from file.
        
        Args:
            file_path: Path to file
            n: Number of lines to read
            
        Returns:
            Last N lines as string
        """
        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()
                return ''.join(lines[-n:])
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            return ""
    
    def _send_next_message(self, terminal_id: str):
        """Send next pending message to terminal.
        
        Args:
            terminal_id: Terminal ID (receiver)
        """
        # Get oldest pending message
        messages = get_pending_messages(terminal_id, limit=1)
        if not messages:
            return
        
        message = messages[0]
        
        # Get terminal metadata
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            logger.error(f"Terminal {terminal_id} not found in database")
            update_message_status(message.id, MessageStatus.FAILED)
            raise ValueError(f"Terminal {terminal_id} not found")
        
        # Send message via tmux
        try:
            tmux_client.send_keys(
                metadata['tmux_session'],
                metadata['tmux_window'],
                message.message
            )
            update_message_status(message.id, MessageStatus.DELIVERED)
            logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        except Exception as e:
            update_message_status(message.id, MessageStatus.FAILED)
            logger.error(f"Failed to send message {message.id} to terminal {terminal_id}: {e}")
            raise


# Module singleton
inbox_service = InboxService()
