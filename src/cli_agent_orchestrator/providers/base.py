"""Base provider interface for CLI tool abstraction."""

from abc import ABC, abstractmethod
from typing import List
from cli_agent_orchestrator.models.terminal import TerminalStatus


class BaseProvider(ABC):
    """Abstract base class for CLI tool providers."""
    
    def __init__(self, terminal_id: str, session_name: str, window_name: str):
        """Initialize provider with terminal context."""
        self.terminal_id = terminal_id
        self.session_name = session_name
        self.window_name = window_name
        self._status = TerminalStatus.IDLE
    
    @property
    def status(self) -> TerminalStatus:
        """Get current provider status."""
        return self._status
    
    @abstractmethod
    def initialize(self) -> bool:
        """Initialize the provider (e.g., start CLI tool, send setup commands).
        
        Returns:
            bool: True if initialization successful, False otherwise
        """
        pass
    
    @abstractmethod
    def get_status(self, tail_lines: int = None) -> TerminalStatus:
        """Get current provider status by analyzing terminal output.
        
        Args:
            tail_lines: Number of lines to capture from terminal (default: provider-specific)
        
        Returns:
            TerminalStatus: Current status of the provider
        """
        pass
    
    @abstractmethod
    def get_idle_pattern_for_log(self) -> str:
        """Get pattern that indicates IDLE state in log file output.
        
        Used for quick detection in file watcher before calling full get_status().
        Should return a simple pattern that appears in the IDLE prompt.
        
        Returns:
            str: Pattern to search for in log file tail
        """
        pass
    
    @abstractmethod
    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last message from terminal script output.
        
        Args:
            script_output: Raw terminal output/script content
            
        Returns:
            str: Extracted last message from the provider
        """
        pass
    
    @abstractmethod
    def exit_cli(self) -> str:
        """Get the command to exit the provider CLI.
        
        Returns:
            Command string to send to terminal for exiting
        """
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Clean up provider resources."""
        pass
    
    def _update_status(self, status: TerminalStatus) -> None:
        """Update internal status."""
        self._status = status
