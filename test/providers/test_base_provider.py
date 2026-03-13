"""Tests for base provider."""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider


class ConcreteProvider(BaseProvider):
    """Concrete implementation of BaseProvider for testing."""

    async def initialize(self) -> bool:
        return True

    def get_status(self, buffer: str) -> TerminalStatus:
        if not buffer:
            return TerminalStatus.UNKNOWN
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        return "extracted message"

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        pass


class TestBaseProvider:
    """Tests for BaseProvider abstract class."""

    def test_init(self):
        """Test provider initialization."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.terminal_id == "term-123"
        assert provider.session_name == "session-1"
        assert provider.window_name == "window-0"

    def test_abstract_methods_implemented(self):
        """Test that concrete implementation works."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.get_status("some output") == TerminalStatus.IDLE
        assert provider.extract_last_message_from_script("test") == "extracted message"
        assert provider.exit_cli() == "/exit"
        provider.cleanup()  # Should not raise
