"""Tests for base provider."""

from typing import Optional

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider


class ConcreteProvider(BaseProvider):
    """Concrete implementation of BaseProvider for testing."""

    def initialize(self) -> bool:
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        return self._status

    def get_idle_pattern_for_log(self) -> str:
        return r"\[test\]>"

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
        assert provider._status == TerminalStatus.IDLE

    def test_status_property(self):
        """Test status property getter."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.status == TerminalStatus.IDLE

    def test_update_status(self):
        """Test _update_status method."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        provider._update_status(TerminalStatus.PROCESSING)

        assert provider._status == TerminalStatus.PROCESSING
        assert provider.status == TerminalStatus.PROCESSING

    def test_update_status_all_values(self):
        """Test _update_status with all status values."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        for status in TerminalStatus:
            provider._update_status(status)
            assert provider.status == status

    def test_abstract_methods_implemented(self):
        """Test that concrete implementation works."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.initialize() is True
        assert provider.get_status() == TerminalStatus.IDLE
        assert provider.get_idle_pattern_for_log() == r"\[test\]>"
        assert provider.extract_last_message_from_script("test") == "extracted message"
        assert provider.exit_cli() == "/exit"
        provider.cleanup()  # Should not raise
