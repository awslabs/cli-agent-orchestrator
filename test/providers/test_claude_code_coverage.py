"""Additional tests for ClaudeCodeProvider to cover uncovered branches.

Covers: McpServer model_dump path, bypass permissions prompt handling,
and idle prompt early return in _handle_startup_prompts.
"""

import re
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def provider():
    """Create a ClaudeCodeProvider with mocked dependencies."""
    with patch("cli_agent_orchestrator.providers.claude_code.tmux_client"):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        p = ClaudeCodeProvider("tid1", "ses", "win", "test-agent")
        yield p


class TestBuildCommandMcpServerModelDump:
    """Test the model_dump branch in _build_claude_command (line 93)."""

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_mcp_server_with_model_dump(self, mock_load, provider):
        """When mcpServers contains a Pydantic model (not dict), model_dump is called."""
        mock_mcp = MagicMock()
        mock_mcp.model_dump.return_value = {
            "command": "node",
            "args": ["server.js"],
            "env": {},
        }
        # isinstance(mock_mcp, dict) returns False, so the model_dump branch triggers
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Test prompt"
        mock_profile.mcpServers = {"my-mcp": mock_mcp}
        mock_profile.allowedTools = None
        mock_load.return_value = mock_profile

        cmd = provider._build_claude_command()

        assert "--mcp-config" in cmd
        mock_mcp.model_dump.assert_called_once_with(exclude_none=True)


class TestHandleStartupPromptsBranches:
    """Test _handle_startup_prompts branches."""

    @patch("cli_agent_orchestrator.providers.claude_code.subprocess")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_bypass_permissions_prompt(self, mock_tmux, mock_subprocess, provider):
        """Detects bypass permissions prompt and sends Down + Enter."""
        mock_tmux.get_history.return_value = (
            "⚠ Bypass Permissions mode\n" "1. No, exit\n" "2. Yes, I accept\n"
        )

        provider._handle_startup_prompts(timeout=1.0)

        # Should have called subprocess.run twice (Down arrow + Enter)
        assert mock_subprocess.run.call_count == 2

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_idle_prompt_detected_early_return(self, mock_tmux, provider):
        """When idle prompt is visible, returns immediately without sending keys."""
        from cli_agent_orchestrator.providers.claude_code import IDLE_PROMPT_PATTERN

        mock_tmux.get_history.return_value = "❯ "

        provider._handle_startup_prompts(timeout=1.0)

        # No exception means early return worked

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_welcome_banner_detected_early_return(self, mock_tmux, provider):
        """When welcome banner is visible, returns immediately."""
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.5.0"

        provider._handle_startup_prompts(timeout=1.0)

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_trust_prompt_detected(self, mock_tmux, provider):
        """Trust prompt sends Enter to accept."""
        mock_tmux.get_history.return_value = (
            "Do you trust the files in this folder?\n" "❯ Yes, I trust this folder"
        )
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        mock_tmux.server.sessions.get.return_value = mock_session

        provider._handle_startup_prompts(timeout=1.0)

        mock_pane.send_keys.assert_called_once_with("", enter=True)


class TestDatabaseListAllTerminals:
    """Test list_all_terminals database function (lines 149-151)."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_all_terminals(self, mock_session_class):
        from cli_agent_orchestrator.clients.database import list_all_terminals

        mock_terminal = MagicMock()
        mock_terminal.id = "tid1"
        mock_terminal.tmux_session = "ses"
        mock_terminal.tmux_window = "win"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "dev"
        mock_terminal.last_active = None

        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = [mock_terminal]
        mock_session_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_session_class.return_value.__exit__ = MagicMock(return_value=False)

        result = list_all_terminals()

        assert len(result) == 1
        assert result[0]["id"] == "tid1"
        assert result[0]["provider"] == "kiro_cli"
