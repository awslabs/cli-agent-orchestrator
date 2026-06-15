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

    def test_apply_skill_prompt_appends(self):
        """Test _apply_skill_prompt appends skill text to base prompt."""
        provider = ConcreteProvider(
            "term-123", "session-1", "window-0", skill_prompt="## Skills\n- skill1"
        )
        result = provider._apply_skill_prompt("Base prompt")
        assert result == "Base prompt\n\n## Skills\n- skill1"

    def test_apply_skill_prompt_no_skill(self):
        """Test _apply_skill_prompt returns original when no skill_prompt."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")
        result = provider._apply_skill_prompt("Base prompt")
        assert result == "Base prompt"

    def test_apply_skill_prompt_empty_base(self):
        """Test _apply_skill_prompt with empty base and skill_prompt present."""
        provider = ConcreteProvider("term-123", "session-1", "window-0", skill_prompt="## Skills")
        result = provider._apply_skill_prompt("")
        assert result == "## Skills"

    def test_abstract_methods_implemented(self):
        """Test that concrete implementation works."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.get_status("some output") == TerminalStatus.IDLE
        assert provider.extract_last_message_from_script("test") == "extracted message"
        assert provider.exit_cli() == "/exit"
        provider.cleanup()  # Should not raise
