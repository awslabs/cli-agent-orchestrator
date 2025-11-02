"""Unit tests for Codex CLI provider."""

import pytest
from pathlib import Path
from unittest.mock import patch

from cli_agent_orchestrator.providers.codex_cli import CodexCliProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus

# Fixtures are tmux capture logs recorded from real Codex CLI sessions.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as fh:
        return fh.read()


class TestCodexCliInitialization:
    """Initialization scenarios."""

    @patch("cli_agent_orchestrator.providers.codex_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_status, mock_wait_shell):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = CodexCliProvider("abcd1234", "session", "window")
        assert provider.initialize() is True

        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once_with("session", "window", "codex")
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.codex_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        mock_wait_shell.return_value = False
        provider = CodexCliProvider("abcd1234", "session", "window")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()


class TestCodexCliStatusDetection:
    """Status detection against captured fixtures."""

    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_status_idle(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_idle_output.txt")
        provider = CodexCliProvider("abcd1234", "session", "window")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_status_processing(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_processing_output.txt")
        provider = CodexCliProvider("abcd1234", "session", "window")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_status_completed(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_completed_output.txt")
        provider = CodexCliProvider("abcd1234", "session", "window")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_status_error_on_empty_output(self, mock_tmux):
        mock_tmux.get_history.return_value = ""
        provider = CodexCliProvider("abcd1234", "session", "window")
        assert provider.get_status() == TerminalStatus.ERROR


class TestCodexCliMessageExtraction:
    """Message extraction from history."""

    def test_extract_last_message_success(self):
        output = load_fixture("codex_completed_output.txt")
        provider = CodexCliProvider("abcd1234", "session", "window")
        message = provider.extract_last_message_from_script(output)
        assert "Morning codebirds sing" in message
        assert "Tests bloom into green" in message

    def test_extract_while_processing(self):
        output = load_fixture("codex_processing_output.txt")
        provider = CodexCliProvider("abcd1234", "session", "window")
        with pytest.raises(ValueError, match="still processing"):
            provider.extract_last_message_from_script(output)
