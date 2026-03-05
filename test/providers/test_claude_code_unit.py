"""Unit tests for Claude Code provider."""

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider, ProviderError


class TestClaudeCodeProviderInitialization:
    """Tests for ClaudeCodeProvider initialization."""

    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test successful initialization."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        # _handle_trust_prompt needs get_history to return a string
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.0"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.initialize()

        assert result is True
        assert provider._initialized is True
        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once()
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test initialization with shell timeout."""
        mock_wait_shell.return_value = False

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_timeout(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test initialization timeout."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.0"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Claude Code initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_with_agent_profile(
        self, mock_tmux, mock_wait_status, mock_wait_shell, mock_load, mock_load_claude
    ):
        """Test initialization with agent profile (found via CAO store fallback)."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.0"
        # Global Claude dir misses → falls through to CAO store
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "test-agent"
        mock_profile.system_prompt = "Test system prompt"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        result = provider.initialize()

        assert result is True
        mock_load.assert_called_once_with("test-agent")

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_with_invalid_agent_profile(
        self, mock_tmux, mock_load, mock_wait_shell, mock_load_claude
    ):
        """Test initialization with invalid agent profile not found in global dir or CAO store."""
        mock_wait_shell.return_value = True
        # Global Claude directory lookup returns None
        mock_load_claude.return_value = None
        # CAO store also raises → ProviderError
        mock_load.side_effect = FileNotFoundError("Profile not found")

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "invalid-agent")

        with pytest.raises(
            ProviderError, match="not found in global Claude Code agent directory or CAO store"
        ):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_with_mcp_servers(
        self, mock_tmux, mock_wait_status, mock_wait_shell, mock_load, mock_load_claude
    ):
        """Test initialization with MCP servers in profile (CAO store fallback)."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.0"
        # Global Claude dir misses → falls through to CAO store
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "test-agent"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"server1": {"command": "test", "args": ["--flag"]}}
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        result = provider.initialize()

        assert result is True

    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_sends_claude_command(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test that initialize sends the 'claude' command to tmux."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.0"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.initialize()

        mock_tmux.send_keys.assert_called_once_with(
            "test-session", "window-0", "claude --dangerously-skip-permissions"
        )


class TestClaudeCodeProviderStatusDetection:
    """Tests for ClaudeCodeProvider status detection."""

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_idle_old_prompt(self, mock_tmux):
        """Test IDLE status detection with old '>' prompt."""
        mock_tmux.get_history.return_value = "> "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_idle_new_prompt(self, mock_tmux):
        """Test IDLE status detection with new '❯' prompt."""
        mock_tmux.get_history.return_value = "❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_idle_with_ansi_codes(self, mock_tmux):
        """Test IDLE status detection with ANSI codes around prompt."""
        mock_tmux.get_history.return_value = (
            "\x1b[2m\x1b[38;2;136;136;136m────────────\n"
            '\x1b[0m❯ \x1b[7mT\x1b[0;2mry\x1b[0m \x1b[2m"hello"\x1b[0m\n'
            "\x1b[2m\x1b[38;2;136;136;136m────────────\x1b[0m"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        """Test COMPLETED status detection."""
        mock_tmux.get_history.return_value = "⏺ Here is the response\n> "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_completed_with_new_prompt(self, mock_tmux):
        """Test COMPLETED status detection with new '❯' prompt."""
        mock_tmux.get_history.return_value = "⏺ Here is the response\n❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        """Test PROCESSING status detection."""
        mock_tmux.get_history.return_value = "✶ Processing… (esc to interrupt)"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_processing_minimal_spinner(self, mock_tmux):
        """Test PROCESSING detection with minimal spinner format (no parenthesized text)."""
        mock_tmux.get_history.return_value = "✻ Orbiting…"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_processing_beats_stale_completed(self, mock_tmux):
        """Test that PROCESSING is detected even when stale ⏺ and ❯ markers are in scrollback."""
        mock_tmux.get_history.return_value = (
            "⏺ Previous response from init\n"
            "❯ user task message\n"
            "⏺ Let me read the file\n"
            "✻ Orbiting…"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_idle_not_false_processing_from_status_bar(self, mock_tmux):
        """Status bar '· latest:…' must not false-positive as PROCESSING."""
        mock_tmux.get_history.return_value = (
            "Claude Code v2.1.63\n"
            "────────────────────\n"
            "❯ \n"
            "────────────────────\n"
            "  current: 2.1.63 · latest:…"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        """Test WAITING_USER_ANSWER status detection."""
        mock_tmux.get_history.return_value = "❯ 1. Option one\n  2. Option two"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_error_empty(self, mock_tmux):
        """Test ERROR status with empty output."""
        mock_tmux.get_history.return_value = ""

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_error_unrecognized(self, mock_tmux):
        """Test ERROR status with unrecognized output."""
        mock_tmux.get_history.return_value = "Some random output without patterns"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_with_tail_lines(self, mock_tmux):
        """Test status detection with tail_lines parameter."""
        mock_tmux.get_history.return_value = "> "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.get_status(tail_lines=50)

        mock_tmux.get_history.assert_called_with("test-session", "window-0", tail_lines=50)


class TestClaudeCodeProviderMessageExtraction:
    """Tests for ClaudeCodeProvider message extraction."""

    def test_extract_message_success(self):
        """Test successful message extraction."""
        output = """Some initial content
⏺ Here is the response message
that spans multiple lines
> """
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "Here is the response message" in result
        assert "that spans multiple lines" in result

    def test_extract_message_no_response(self):
        """Test extraction with no response pattern."""
        output = """Some content without response
> """
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        with pytest.raises(ValueError, match="No Claude Code response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_empty_response(self):
        """Test extraction with empty response."""
        output = """⏺
> """
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        with pytest.raises(ValueError, match="Empty Claude Code response"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_multiple_responses(self):
        """Test extraction with multiple responses (uses last)."""
        output = """⏺ First response
>
⏺ Second response
> """
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "Second response" in result

    def test_extract_message_with_separator(self):
        """Test extraction stops at separator."""
        output = """⏺ Response content
────────
More content
> """
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "Response content" in result
        assert "More content" not in result


class TestClaudeCodeProviderMisc:
    """Tests for miscellaneous ClaudeCodeProvider methods."""

    def test_exit_cli(self):
        """Test exit command."""
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"

    def test_get_idle_pattern_for_log(self):
        """Test idle pattern for log files."""
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        pattern = provider.get_idle_pattern_for_log()

        assert pattern is not None
        assert ">" in pattern
        assert "❯" in pattern

    def test_cleanup(self):
        """Test cleanup resets initialized state."""
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._initialized = True

        provider.cleanup()

        assert provider._initialized is False

    def test_build_claude_command_no_profile(self):
        """Test building Claude command without profile."""
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        command = provider._build_claude_command()

        assert command == "claude --dangerously-skip-permissions"

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_claude_command_with_system_prompt(self, mock_load, mock_load_claude):
        """Test building Claude command with system prompt (CAO store fallback)."""
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "test-agent"
        mock_profile.system_prompt = "Test prompt\nwith newlines"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        command = provider._build_claude_command()

        assert "claude" in command
        assert "--agent" in command
        assert "test-agent" in command

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_mcp_injects_terminal_id(self, mock_load, mock_load_claude):
        """Test that _build_claude_command injects CAO_TERMINAL_ID into MCP server env."""
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "test-agent"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "cao-mcp-server", "args": ["--port", "8080"]}
        }
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("term-42", "test-session", "window-0", "test-agent")
        command = provider._build_claude_command()

        assert "--mcp-config" in command
        # Extract the JSON arg after --mcp-config
        parts = command.split("--mcp-config ")
        mcp_json_str = parts[1].strip()
        # shlex.join wraps the JSON in single quotes; strip them
        if mcp_json_str.startswith("'") and mcp_json_str.endswith("'"):
            mcp_json_str = mcp_json_str[1:-1]
        mcp_data = json.loads(mcp_json_str)
        server_env = mcp_data["mcpServers"]["cao-mcp-server"]["env"]
        assert server_env["CAO_TERMINAL_ID"] == "term-42"

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_mcp_preserves_existing_env(self, mock_load, mock_load_claude):
        """Test that existing env vars in MCP config are preserved when injecting CAO_TERMINAL_ID."""
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "test-agent"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "my-server": {
                "command": "my-server",
                "env": {"MY_VAR": "my_value", "OTHER": "other_value"},
            }
        }
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("term-99", "test-session", "window-0", "test-agent")
        command = provider._build_claude_command()

        parts = command.split("--mcp-config ")
        mcp_json_str = parts[1].strip()
        if mcp_json_str.startswith("'") and mcp_json_str.endswith("'"):
            mcp_json_str = mcp_json_str[1:-1]
        mcp_data = json.loads(mcp_json_str)
        server_env = mcp_data["mcpServers"]["my-server"]["env"]
        # Original vars preserved
        assert server_env["MY_VAR"] == "my_value"
        assert server_env["OTHER"] == "other_value"
        # CAO_TERMINAL_ID added
        assert server_env["CAO_TERMINAL_ID"] == "term-99"

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_mcp_does_not_override_existing_terminal_id(
        self, mock_load, mock_load_claude
    ):
        """Test that an existing CAO_TERMINAL_ID in MCP env is NOT overwritten."""
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "test-agent"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "my-server": {
                "command": "my-server",
                "env": {"CAO_TERMINAL_ID": "user-provided-id"},
            }
        }
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("term-99", "test-session", "window-0", "test-agent")
        command = provider._build_claude_command()

        parts = command.split("--mcp-config ")
        mcp_json_str = parts[1].strip()
        if mcp_json_str.startswith("'") and mcp_json_str.endswith("'"):
            mcp_json_str = mcp_json_str[1:-1]
        mcp_data = json.loads(mcp_json_str)
        server_env = mcp_data["mcpServers"]["my-server"]["env"]
        # Should keep the user-provided value, NOT overwrite with term-99
        assert server_env["CAO_TERMINAL_ID"] == "user-provided-id"


class TestClaudeCodeProviderTrustPrompt:
    """Tests for Claude Code workspace trust prompt handling."""

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_trust_prompt_detected_and_accepted(self, mock_tmux):
        """Test that trust prompt is detected and auto-accepted."""
        # Simulate trust prompt appearing in terminal output
        mock_tmux.get_history.return_value = (
            "\x1b[1m❯\x1b[0m 1. Yes, I trust this folder\n" "  2. No, don't trust\n"
        )
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_trust_prompt(timeout=2.0)

        # Verify Enter was sent to accept the trust prompt
        mock_pane.send_keys.assert_called_once_with("", enter=True)

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_trust_prompt_not_needed(self, mock_tmux):
        """Test early return when Claude Code starts without trust prompt."""
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.1.0"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_trust_prompt(timeout=2.0)

        # No session/pane access should happen
        mock_tmux.server.sessions.get.assert_not_called()

    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_trust_prompt_timeout(self, mock_tmux, mock_time):
        """Test trust prompt handler times out gracefully."""
        # Return output that doesn't match trust prompt or welcome banner
        mock_tmux.get_history.return_value = "Loading..."
        # Simulate time passing past the timeout
        mock_time.time.side_effect = [0.0, 0.0, 25.0]
        mock_time.sleep = MagicMock()

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        # Should not raise, just log a warning and return
        provider._handle_trust_prompt(timeout=20.0)

        mock_tmux.server.sessions.get.assert_not_called()

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_trust_prompt_empty_output_then_detected(self, mock_tmux):
        """Test trust prompt detection after initially empty output."""
        # First call returns empty, second returns trust prompt
        mock_tmux.get_history.side_effect = [
            "",
            "❯ 1. Yes, I trust this folder\n  2. No",
        ]
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_trust_prompt(timeout=5.0)

        mock_pane.send_keys.assert_called_once_with("", enter=True)

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_get_status_trust_prompt_not_waiting_user_answer(self, mock_tmux):
        """Test that trust prompt is NOT detected as WAITING_USER_ANSWER."""
        # This output has both WAITING_USER_ANSWER pattern AND trust prompt pattern
        mock_tmux.get_history.return_value = (
            "❯ 1. Yes, I trust this folder\n" "  2. No, don't trust this folder"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status()

        # Should NOT be WAITING_USER_ANSWER since trust prompt is excluded
        assert status != TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_initialize_calls_handle_trust_prompt(
        self, mock_tmux, mock_wait_status, mock_wait_shell
    ):
        """Test that initialize calls _handle_trust_prompt."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        # Trust prompt appears, then gets auto-accepted
        mock_tmux.get_history.return_value = "❯ 1. Yes, I trust this folder\n  2. No"
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.initialize()

        assert result is True
        # Verify trust prompt was auto-accepted (Enter sent)
        mock_pane.send_keys.assert_called_with("", enter=True)


class TestLoadClaudeAgentProfile:
    """Tests for the module-level _load_claude_agent_profile helper."""

    def test_finds_global_agent(self, tmp_path):
        """Returns agent profile from the global ~/.claude/agents/ directory."""
        from cli_agent_orchestrator.providers.claude_code import _load_claude_agent_profile

        global_dir = tmp_path / "agents"
        global_dir.mkdir(parents=True)
        (global_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: test\n---\nDo stuff."
        )

        with patch(
            "cli_agent_orchestrator.providers.claude_code.CLAUDE_AGENTS_DIR",
            global_dir,
        ):
            result = _load_claude_agent_profile("my-agent")

        assert result is not None
        assert result["name"] == "my-agent"
        assert "mcpServers" not in result

    def test_returns_none_when_not_found(self, tmp_path):
        """Returns None when the agent file does not exist in the global directory."""
        from cli_agent_orchestrator.providers.claude_code import _load_claude_agent_profile

        empty_dir = tmp_path / "agents"
        empty_dir.mkdir()

        with patch(
            "cli_agent_orchestrator.providers.claude_code.CLAUDE_AGENTS_DIR",
            empty_dir,
        ):
            result = _load_claude_agent_profile("nonexistent")

        assert result is None

    def test_returns_none_on_parse_error(self, tmp_path):
        """Returns None when the frontmatter cannot be parsed (logs a warning)."""
        from cli_agent_orchestrator.providers.claude_code import _load_claude_agent_profile

        global_dir = tmp_path / "agents"
        global_dir.mkdir(parents=True)
        (global_dir / "bad-agent.md").write_text("---\nname: [invalid yaml\n---\nBody.")

        with patch(
            "cli_agent_orchestrator.providers.claude_code.CLAUDE_AGENTS_DIR",
            global_dir,
        ):
            result = _load_claude_agent_profile("bad-agent")

        assert result is None

    def test_includes_mcp_servers_when_present(self, tmp_path):
        """Returns mcpServers from frontmatter when present."""
        from cli_agent_orchestrator.providers.claude_code import _load_claude_agent_profile

        global_dir = tmp_path / "agents"
        global_dir.mkdir(parents=True)
        (global_dir / "mcp-agent.md").write_text(
            "---\nname: mcp-agent\ndescription: mcp\n"
            "mcpServers:\n  my-server:\n    command: my-cmd\n---\nBody."
        )

        with patch(
            "cli_agent_orchestrator.providers.claude_code.CLAUDE_AGENTS_DIR",
            global_dir,
        ):
            result = _load_claude_agent_profile("mcp-agent")

        assert result is not None
        assert "mcpServers" in result
        assert "my-server" in result["mcpServers"]

    def test_uses_filename_as_name_fallback(self, tmp_path):
        """Falls back to agent_name when 'name' key is absent from frontmatter."""
        from cli_agent_orchestrator.providers.claude_code import _load_claude_agent_profile

        global_dir = tmp_path / "agents"
        global_dir.mkdir(parents=True)
        # Frontmatter exists but has no 'name' key
        (global_dir / "unnamed-agent.md").write_text("---\ndescription: no name here\n---\nBody.")

        with patch(
            "cli_agent_orchestrator.providers.claude_code.CLAUDE_AGENTS_DIR",
            global_dir,
        ):
            result = _load_claude_agent_profile("unnamed-agent")

        assert result is not None
        assert result["name"] == "unnamed-agent"


class TestBuildClaudeCommandResolutionOrder:
    """Tests for agent resolution order: global Claude dir first, CAO store fallback."""

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    def test_global_claude_dir_checked_first(self, mock_load_claude):
        """When agent exists in global Claude dir, it is used without touching CAO store."""
        mock_load_claude.return_value = {"name": "claude-native-agent"}

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "claude-native-agent")
        command = provider._build_claude_command()

        assert "--agent" in command
        assert "claude-native-agent" in command
        assert "--mcp-config" not in command
        mock_load_claude.assert_called_once_with("claude-native-agent")

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    def test_global_claude_dir_with_mcp_servers(self, mock_load_claude):
        """Global Claude dir path injects CAO_TERMINAL_ID when profile has mcpServers."""
        mock_load_claude.return_value = {
            "name": "claude-mcp-agent",
            "mcpServers": {"my-server": {"command": "my-cmd", "args": []}},
        }

        provider = ClaudeCodeProvider("term-77", "test-session", "window-0", "claude-mcp-agent")
        command = provider._build_claude_command()

        assert "--agent" in command
        assert "--mcp-config" in command
        import json as _json
        import shlex as _shlex

        parts = _shlex.split(command)
        mcp_json = parts[parts.index("--mcp-config") + 1]
        mcp_data = _json.loads(mcp_json)
        assert mcp_data["mcpServers"]["my-server"]["env"]["CAO_TERMINAL_ID"] == "term-77"

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_falls_back_to_cao_store(self, mock_load, mock_load_claude):
        """When global Claude dir misses, falls back to CAO agent store."""
        mock_load_claude.return_value = None
        mock_profile = MagicMock()
        mock_profile.name = "cao-agent"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "cao-agent")
        command = provider._build_claude_command()

        assert "--agent" in command
        assert "cao-agent" in command
        mock_load_claude.assert_called_once_with("cao-agent")
        mock_load.assert_called_once_with("cao-agent")

    @patch("cli_agent_orchestrator.providers.claude_code._load_claude_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_raises_provider_error_when_not_found_anywhere(self, mock_load, mock_load_claude):
        """Raises ProviderError when agent is not found in global dir or CAO store."""
        mock_load_claude.return_value = None
        mock_load.side_effect = FileNotFoundError("not in CAO store")

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "missing-agent")

        with pytest.raises(
            ProviderError, match="not found in global Claude Code agent directory or CAO store"
        ):
            provider._build_claude_command()
