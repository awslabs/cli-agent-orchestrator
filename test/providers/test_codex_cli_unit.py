"""Unit tests for Codex CLI provider."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

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
        assert mock_tmux.send_keys.call_args_list == [
            call("session", "window", "export CAO_TERMINAL_ID=abcd1234"),
            call("session", "window", "codex"),
        ]
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.codex_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        mock_wait_shell.return_value = False
        provider = CodexCliProvider("abcd1234", "session", "window")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.codex_cli.subprocess.run")
    @patch("cli_agent_orchestrator.providers.codex_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.codex_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex_cli.tmux_client")
    def test_initialize_registers_mcp_servers(
        self,
        mock_tmux,
        mock_wait_status,
        mock_wait_shell,
        mock_load_profile,
        mock_subprocess,
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load_profile.return_value = SimpleNamespace(
            mcpServers={
                "cao-mcp-server": {
                    "command": "uvx",
                    "args": ["--from", "git+https://example", "cao-mcp-server"],
                    "env": {"OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"},
                }
            }
        )

        provider = CodexCliProvider("abcd1234", "session", "window", agent_profile="product_supervisor")
        provider.initialize()

        mock_subprocess.assert_called_once()
        cmd_args = mock_subprocess.call_args[0][0]
        assert cmd_args[:3] == ["codex", "mcp", "add"]
        assert cmd_args[3:5] == ["--env", "OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES"]
        assert cmd_args[5:7] == ["cao-mcp-server", "uvx"]
        assert "--from" in cmd_args
        assert cmd_args[-1] == "cao-mcp-server"
        # First send_keys exports env, second launches Codex
        assert mock_tmux.send_keys.call_args_list[0].args[2] == "export CAO_TERMINAL_ID=abcd1234"
        assert mock_tmux.send_keys.call_args_list[1].args[2].startswith("export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=")
        assert mock_tmux.send_keys.call_args_list[-1] == call("session", "window", "codex")


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
