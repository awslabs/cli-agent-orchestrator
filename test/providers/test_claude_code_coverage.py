"""Additional tests for ClaudeCodeProvider to cover uncovered branches.

Covers: McpServer model_dump path, bypass permissions prompt handling,
and the workspace-trust handling in _handle_startup_prompts (including the
regression where the echoed launch command false-matched the idle prompt).
"""

import re
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def provider():
    """Create a ClaudeCodeProvider with mocked dependencies."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
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
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        cmd = provider._build_claude_command()

        assert "--mcp-config" in cmd
        mock_mcp.model_dump.assert_called_once_with(exclude_none=True)


class TestHandleStartupPromptsBranches:
    """Test _handle_startup_prompts branches."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_bypass_permissions_prompt(self, mock_backend, provider):
        """Detects bypass permissions prompt and sends Down arrow + Enter via backend."""
        mock_backend.get_history.return_value = (
            "⚠ Bypass Permissions mode\n" "1. No, exit\n" "2. Yes, I accept\n"
        )

        provider._handle_startup_prompts(timeout=1.0)

        # Down arrow sent via send_keys, Enter via send_special_key
        mock_backend.send_keys.assert_called_once()
        mock_backend.send_special_key.assert_called_once_with(
            provider.session_name, provider.window_name, "Enter"
        )

    @patch("cli_agent_orchestrator.providers.claude_code.time.sleep", lambda *a, **k: None)
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_echoed_prompt_does_not_short_circuit_trust(self, mock_backend, provider):
        """Regression: the injected --append-system-prompt contains a line that
        starts with "> `memory_store`". The shell echoes the launch command into
        the capture buffer ~300ms before the workspace-trust dialog renders, so
        that "> " must NOT be treated as the idle prompt and end startup handling
        early — otherwise the trust dialog is left unaccepted and initialize()
        blocks on {IDLE, COMPLETED} until it times out and the session is killed.

        First poll returns the echoed command (with the "> memory_store" marker
        but no dialog yet); second poll returns the trust dialog. The handler must
        accept the trust dialog (Enter) rather than returning on the first frame.
        """
        echoed_launch_cmd = (
            "user@host:/tmp/proj$ claude --dangerously-skip-permissions "
            "--append-system-prompt '## Memory\n"
            "> `memory_store` and `memory_recall` are CAO's memory tools'"
        )
        trust_frame = (
            "Quick safety check: Is this a project you created or one you trust?\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit\n"
        )
        mock_backend.get_history.side_effect = [echoed_launch_cmd, trust_frame]

        provider._handle_startup_prompts(timeout=5.0)

        # Trust dialog accepted via Enter — proves we did not early-return on the
        # echoed "> memory_store" marker.
        mock_backend.send_special_key.assert_called_once_with(
            provider.session_name, provider.window_name, "Enter"
        )

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_welcome_banner_detected_early_return(self, mock_backend, provider):
        """When welcome banner is visible, returns immediately."""
        mock_backend.get_history.return_value = "Welcome to Claude Code v2.5.0"

        provider._handle_startup_prompts(timeout=1.0)

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_trust_prompt_detected(self, mock_backend, provider):
        """Trust prompt sends Enter to accept via backend send_special_key."""
        mock_backend.get_history.return_value = (
            "Do you trust the files in this folder?\n" "❯ Yes, I trust this folder"
        )

        provider._handle_startup_prompts(timeout=1.0)

        mock_backend.send_special_key.assert_called_once_with(
            provider.session_name, provider.window_name, "Enter"
        )


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
