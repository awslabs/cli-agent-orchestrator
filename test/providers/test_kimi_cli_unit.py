"""Tests for Kimi CLI provider.

Covers initialization, status detection, message extraction, command building,
pattern matching, and cleanup — targeting >90% code coverage.
"""

import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kimi_cli import (
    ANSI_CODE_PATTERN,
    ERROR_PATTERN,
    IDLE_PROMPT_PATTERN,
    IDLE_PROMPT_PATTERN_LOG,
    IDLE_PROMPT_TAIL_LINES,
    RESPONSE_BULLET_PATTERN,
    STATUS_BAR_PATTERN,
    THINKING_BULLET_RAW_PATTERN,
    USER_INPUT_BOX_END_PATTERN,
    USER_INPUT_BOX_START_PATTERN,
    WELCOME_BANNER_PATTERN,
    KimiCliProvider,
    ProviderError,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    """Read a test fixture file."""
    return (FIXTURES_DIR / name).read_text()


# =============================================================================
# Initialization tests
# =============================================================================


class TestKimiCliProviderInitialization:
    """Tests for KimiCliProvider initialization flow."""

    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test successful initialization sends kimi command and reaches IDLE."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        result = provider.initialize()

        assert result is True
        assert provider._initialized is True
        mock_tmux.send_keys.assert_called_once()
        mock_wait_shell.assert_called_once()
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell", return_value=False)
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test shell init timeout raises TimeoutError."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        with pytest.raises(TimeoutError, match="Shell initialization"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status", return_value=False)
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_initialize_kimi_timeout(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test Kimi CLI init timeout raises TimeoutError."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        with pytest.raises(TimeoutError, match="Kimi CLI initialization"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_initialize_with_agent_profile(
        self, mock_load, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Test initialization with agent profile creates temp files."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a helpful assistant"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="developer")
        result = provider.initialize()
        assert result is True

        # Verify kimi command includes --agent-file
        call_args = mock_tmux.send_keys.call_args
        command = call_args[0][2]
        assert "--agent-file" in command
        assert "--yolo" in command

        # Cleanup temp files
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_initialize_with_invalid_profile(self, mock_load):
        """Test initialization with invalid agent profile raises ProviderError."""
        mock_load.side_effect = FileNotFoundError("Profile not found")

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="nonexistent")
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_kimi_command()

    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_initialize_with_mcp_servers(
        self, mock_load, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Test initialization with MCP servers in profile adds --mcp-config."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "command": "npx",
                "args": ["-y", "cao-mcp-server"],
            }
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="developer")
        result = provider.initialize()
        assert result is True

        call_args = mock_tmux.send_keys.call_args
        command = call_args[0][2]
        assert "--mcp-config" in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_initialize_sends_kimi_command(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test that initialize sends the correct kimi --yolo command."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider.initialize()

        call_args = mock_tmux.send_keys.call_args
        command = call_args[0][2]
        assert command == "kimi --yolo"


# =============================================================================
# Status detection tests
# =============================================================================


class TestKimiCliProviderStatusDetection:
    """Tests for KimiCliProvider.get_status()."""

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_idle(self, mock_tmux):
        """Test IDLE detection from fresh startup output."""
        mock_tmux.get_history.return_value = _read_fixture("kimi_cli_idle_output.txt")
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_idle_no_thinking(self, mock_tmux):
        """Test IDLE detection with ✨ prompt (no-thinking mode)."""
        output = (
            "Welcome to Kimi Code CLI!\n"
            "user@my-app✨\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding)  ctrl-x: toggle mode  context: 0.0%"
        )
        mock_tmux.get_history.return_value = output
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        """Test COMPLETED detection when response is present with prompt."""
        mock_tmux.get_history.return_value = _read_fixture("kimi_cli_completed_output.txt")
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_completed_complex(self, mock_tmux):
        """Test COMPLETED detection with multi-line code response."""
        mock_tmux.get_history.return_value = _read_fixture("kimi_cli_complex_response.txt")
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        """Test PROCESSING detection when no prompt at bottom."""
        mock_tmux.get_history.return_value = _read_fixture("kimi_cli_processing_output.txt")
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_error_empty(self, mock_tmux):
        """Test ERROR on empty output."""
        mock_tmux.get_history.return_value = ""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_error_none(self, mock_tmux):
        """Test ERROR on None output."""
        mock_tmux.get_history.return_value = None
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_error_pattern(self, mock_tmux):
        """Test ERROR detection from error output fixture."""
        mock_tmux.get_history.return_value = _read_fixture("kimi_cli_error_output.txt")
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_idle_with_ansi_codes(self, mock_tmux):
        """Test IDLE detection with ANSI escape codes in output."""
        # Simulate raw ANSI output: bold prompt with color codes
        output = (
            "\x1b[38;5;33mWelcome to Kimi Code CLI!\x1b[0m\n"
            "\x1b[1muser@my-app💫\x1b[0m\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 0.0%"
        )
        mock_tmux.get_history.return_value = output
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_with_tail_lines(self, mock_tmux):
        """Test status detection with tail_lines parameter passed through."""
        mock_tmux.get_history.return_value = _read_fixture("kimi_cli_idle_output.txt")
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider.get_status(tail_lines=20)
        mock_tmux.get_history.assert_called_once_with("session-1", "window-1", tail_lines=20)

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_idle_tall_terminal(self, mock_tmux):
        """Test IDLE detection in tall terminals (46+ rows) where prompt is far from bottom.

        In a 46-row terminal, the welcome banner takes ~12 lines, the prompt is at
        line ~14, and there are ~32 empty padding lines before the status bar. The
        IDLE_PROMPT_TAIL_LINES must be large enough to reach the prompt.
        """
        # Simulate a 46-row terminal: welcome banner + prompt + 32 empty lines + status bar
        output = (
            "╭───────────────────────────────────╮\n"
            "│ Welcome to Kimi Code CLI!          │\n"
            "│ Send /help for help information.   │\n"
            "╰───────────────────────────────────╯\n"
            "user@project💫\n"
            + "\n" * 32  # 32 empty padding lines (typical for 46-row terminal)
            + "00:05  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 0.0%\n"
        )
        mock_tmux.get_history.return_value = output
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_get_status_processing_streaming(self, mock_tmux):
        """Test PROCESSING when response is mid-stream (no prompt, no error)."""
        output = (
            "╭──────────────────╮\n"
            "│ write a function  │\n"
            "╰──────────────────╯\n"
            "• Here's the function:\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )
        mock_tmux.get_history.return_value = output
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING


# =============================================================================
# Message extraction tests
# =============================================================================


class TestKimiCliProviderMessageExtraction:
    """Tests for KimiCliProvider.extract_last_message_from_script()."""

    def test_extract_message_success(self):
        """Test successful message extraction from completed output."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = _read_fixture("kimi_cli_completed_output.txt")
        result = provider.extract_last_message_from_script(output)

        assert len(result) > 0
        assert "greet" in result.lower() or "function" in result.lower()

    def test_extract_message_complex_response(self):
        """Test extraction of multi-line response with code block."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = _read_fixture("kimi_cli_complex_response.txt")
        result = provider.extract_last_message_from_script(output)

        assert len(result) > 0
        assert "Calculator" in result or "calculator" in result

    def test_extract_message_no_input(self):
        """Test ValueError when no user input box is found."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = "Some random text without input box"
        with pytest.raises(ValueError, match="No Kimi CLI user input found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_empty_response(self):
        """Test ValueError on empty response after input box."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ test message      │\n"
            "╰──────────────────╯\n"
            "user@my-app💫\n"
        )
        with pytest.raises(ValueError, match="Empty Kimi CLI response"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_filters_thinking(self):
        """Test that thinking bullets (gray ANSI) are filtered from output."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        # Simulate raw output with thinking and response bullets
        output = (
            "╭──────────────────╮\n"
            "│ say hello          │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m•\x1b[39m \x1b[3m\x1b[38;5;244mThe user wants a greeting.\x1b[0m\n"
            "• Hello! \U0001f44b\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)

        assert "Hello!" in result
        # Thinking text should be filtered out
        assert "The user wants" not in result

    def test_extract_message_multiple_responses(self):
        """Test extraction picks content from last user input box."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ first question     │\n"
            "╰──────────────────╯\n"
            "• First answer\n"
            "user@my-app💫\n"
            "╭──────────────────╮\n"
            "│ second question    │\n"
            "╰──────────────────╯\n"
            "• Second answer\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Second answer" in result

    def test_extract_message_no_trailing_prompt(self):
        """Test extraction works when there's no trailing prompt."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ what is python?    │\n"
            "╰──────────────────╯\n"
            "• Python is a programming language.\n"
            "• It supports multiple paradigms.\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Python" in result
        assert "paradigm" in result.lower()

    def test_extract_message_all_thinking_falls_back(self):
        """Test fallback when all lines are filtered as thinking."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        # All bullets are thinking (gray ANSI) — should fall back to returning all content
        output = (
            "╭──────────────────╮\n"
            "│ analyze this       │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m• \x1b[39m\x1b[3m\x1b[38;5;244mLet me analyze the code.\x1b[0m\n"
            "\x1b[38;5;244m• \x1b[39m\x1b[3m\x1b[38;5;244mI see several patterns.\x1b[0m\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        # Should return the thinking content as fallback
        assert "analyze" in result.lower() or "pattern" in result.lower()

    def test_extract_message_with_status_bar_filtered(self):
        """Test that status bar lines are filtered from extracted content."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ hello               │\n"
            "╰──────────────────╯\n"
            "• Hi there!\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Hi there!" in result
        assert "yolo" not in result
        assert "ctrl-x" not in result


# =============================================================================
# Command building tests
# =============================================================================


class TestKimiCliProviderBuildCommand:
    """Tests for KimiCliProvider._build_kimi_command()."""

    def test_build_command_no_profile(self):
        """Test command without agent profile is just 'kimi --yolo'."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        command = provider._build_kimi_command()
        assert command == "kimi --yolo"

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_with_system_prompt(self, mock_load):
        """Test command with agent profile containing system prompt."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a developer"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert "kimi" in command
        assert "--yolo" in command
        assert "--agent-file" in command
        # Temp directory should be created
        assert provider._temp_dir is not None

        # Cleanup
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_with_mcp_config(self, mock_load):
        """Test command with MCP server configuration including CAO_TERMINAL_ID injection."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"test-server": {"command": "npx", "args": ["test"]}}
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert "--mcp-config" in command
        assert "test-server" in command
        # CAO_TERMINAL_ID should be injected into MCP server env
        assert "CAO_TERMINAL_ID" in command
        assert "term-1" in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_creates_agent_yaml(self, mock_load):
        """Test that agent YAML and system prompt files are created correctly."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "Custom system prompt"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        provider._build_kimi_command()

        # Check temp files were created
        assert provider._temp_dir is not None
        assert os.path.exists(os.path.join(provider._temp_dir, "agent.yaml"))
        assert os.path.exists(os.path.join(provider._temp_dir, "system.md"))

        # Check system prompt content
        with open(os.path.join(provider._temp_dir, "system.md")) as f:
            assert f.read() == "Custom system prompt"

        # Check agent YAML content
        with open(os.path.join(provider._temp_dir, "agent.yaml")) as f:
            content = f.read()
            assert "extend: default" in content
            assert "system_prompt_path: ./system.md" in content

        # Cleanup
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_with_pydantic_mcp_config(self, mock_load):
        """Test command with MCP servers as Pydantic model objects."""
        mock_server = MagicMock()
        mock_server.model_dump.return_value = {"command": "node", "args": ["server.js"]}
        # Not a dict, triggers model_dump branch
        type(mock_server).__instancecheck__ = lambda cls, inst: False

        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"my-server": mock_server}
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert "--mcp-config" in command
        assert "my-server" in command
        # CAO_TERMINAL_ID should be injected into MCP server env
        assert "CAO_TERMINAL_ID" in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_mcp_preserves_existing_env(self, mock_load):
        """Test that CAO_TERMINAL_ID injection preserves existing env vars."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "test-server": {
                "command": "npx",
                "args": ["test"],
                "env": {"MY_VAR": "my_value"},
            }
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("abc123", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        import json

        # Extract the JSON config from the command
        parts = command.split("--mcp-config ")
        mcp_json = parts[1].strip().strip("'")
        config = json.loads(mcp_json)

        assert config["test-server"]["env"]["MY_VAR"] == "my_value"
        assert config["test-server"]["env"]["CAO_TERMINAL_ID"] == "abc123"

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_mcp_does_not_override_existing_terminal_id(self, mock_load):
        """Test that existing CAO_TERMINAL_ID in env is not overwritten."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "test-server": {
                "command": "npx",
                "args": ["test"],
                "env": {"CAO_TERMINAL_ID": "existing-id"},
            }
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("new-id", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        import json

        parts = command.split("--mcp-config ")
        mcp_json = parts[1].strip().strip("'")
        config = json.loads(mcp_json)

        # Should keep the existing value, not override
        assert config["test-server"]["env"]["CAO_TERMINAL_ID"] == "existing-id"

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_profile_no_system_prompt(self, mock_load):
        """Test command with profile that has no system prompt (no temp files)."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert command == "kimi --yolo"
        assert provider._temp_dir is None

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_profile_empty_system_prompt(self, mock_load):
        """Test command with profile that has empty string system prompt."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert command == "kimi --yolo"
        assert provider._temp_dir is None


# =============================================================================
# Misc / lifecycle tests
# =============================================================================


class TestKimiCliProviderMisc:
    """Tests for miscellaneous KimiCliProvider methods and lifecycle."""

    def test_exit_cli(self):
        """Test exit command returns /exit."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.exit_cli() == "/exit"

    def test_get_idle_pattern_for_log(self):
        """Test idle pattern for log monitoring matches both emoji markers."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        pattern = provider.get_idle_pattern_for_log()
        assert pattern == IDLE_PROMPT_PATTERN_LOG
        # Should match both emoji markers
        assert re.search(pattern, "user@app✨")
        assert re.search(pattern, "user@app💫")

    def test_cleanup(self):
        """Test cleanup resets initialized state."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_cleanup_removes_temp_dir(self):
        """Test cleanup removes temporary directory and its contents."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider._temp_dir = tempfile.mkdtemp(prefix="cao_kimi_test_")
        temp_path = provider._temp_dir  # Save path before cleanup resets it

        # Create a file in temp dir to verify it's removed
        test_file = os.path.join(temp_path, "test.txt")
        with open(test_file, "w") as f:
            f.write("test")

        provider.cleanup()
        assert provider._temp_dir is None
        assert not os.path.exists(temp_path)

    def test_cleanup_nonexistent_temp_dir(self):
        """Test cleanup handles already-removed temp directory gracefully."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider._temp_dir = "/tmp/cao_kimi_nonexistent_12345"
        provider.cleanup()
        assert provider._temp_dir is None

    def test_provider_inherits_base(self):
        """Test provider inherits from BaseProvider."""
        from cli_agent_orchestrator.providers.base import BaseProvider

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert isinstance(provider, BaseProvider)

    def test_provider_default_state(self):
        """Test provider default initialization state."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider._initialized is False
        assert provider._agent_profile is None
        assert provider._temp_dir is None
        assert provider.terminal_id == "term-1"
        assert provider.session_name == "session-1"
        assert provider.window_name == "window-1"

    def test_provider_with_agent_profile(self):
        """Test provider stores agent profile."""
        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        assert provider._agent_profile == "dev"


# =============================================================================
# Pattern tests
# =============================================================================


class TestKimiCliProviderPatterns:
    """Tests for Kimi CLI regex patterns — validates correctness of all patterns."""

    def test_idle_prompt_pattern_thinking(self):
        """Test idle prompt pattern matches thinking mode prompt (💫)."""
        assert re.search(IDLE_PROMPT_PATTERN, "user@my-app💫")
        assert re.search(IDLE_PROMPT_PATTERN, "haofeif@cli-agent-orchestrator💫")

    def test_idle_prompt_pattern_no_thinking(self):
        """Test idle prompt pattern matches no-thinking mode prompt (✨)."""
        assert re.search(IDLE_PROMPT_PATTERN, "user@my-app✨")
        assert re.search(IDLE_PROMPT_PATTERN, "haofeif@project✨")

    def test_idle_prompt_pattern_with_dots_in_hostname(self):
        """Test idle prompt pattern matches hostnames with dots."""
        assert re.search(IDLE_PROMPT_PATTERN, "user@host.domain.com💫")

    def test_idle_prompt_pattern_does_not_match_random_text(self):
        """Test idle prompt pattern doesn't match arbitrary text."""
        assert not re.search(IDLE_PROMPT_PATTERN, "Hello world")
        assert not re.search(IDLE_PROMPT_PATTERN, "some random text")
        assert not re.search(IDLE_PROMPT_PATTERN, "💫 alone")

    def test_welcome_banner_pattern(self):
        """Test welcome banner detection."""
        assert re.search(WELCOME_BANNER_PATTERN, "Welcome to Kimi Code CLI!")
        assert not re.search(WELCOME_BANNER_PATTERN, "Welcome to Claude Code")

    def test_user_input_box_patterns(self):
        """Test user input box boundary detection."""
        assert re.search(USER_INPUT_BOX_START_PATTERN, "╭──────────────╮")
        assert re.search(USER_INPUT_BOX_END_PATTERN, "╰──────────────╯")
        assert not re.search(USER_INPUT_BOX_START_PATTERN, "│ text │")

    def test_response_bullet_pattern(self):
        """Test response bullet detection."""
        assert re.search(RESPONSE_BULLET_PATTERN, "• Hello world!")
        assert re.search(RESPONSE_BULLET_PATTERN, "• Here is the code")
        assert not re.search(RESPONSE_BULLET_PATTERN, "Hello world")
        assert not re.search(RESPONSE_BULLET_PATTERN, "  • indented bullet")

    def test_thinking_bullet_raw_pattern(self):
        """Test thinking bullet detection in raw ANSI output."""
        # Gray-colored bullet (thinking mode)
        raw = "\x1b[38;5;244m•\x1b[39m \x1b[3m\x1b[38;5;244mThinking...\x1b[0m"
        assert re.search(THINKING_BULLET_RAW_PATTERN, raw)
        # Gray bullet with space before •
        raw_space = "\x1b[38;5;244m •\x1b[39m"
        assert re.search(THINKING_BULLET_RAW_PATTERN, raw_space)
        # Regular bullet (response mode) — should NOT match
        assert not re.search(THINKING_BULLET_RAW_PATTERN, "• Hello world")

    def test_error_pattern(self):
        """Test error pattern detection."""
        assert re.search(ERROR_PATTERN, "Error: connection failed", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "ERROR: something went wrong", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "ConnectionError: timeout", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "APIError: rate limited", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "Traceback (most recent call last):", re.MULTILINE)
        assert not re.search(ERROR_PATTERN, "No errors found", re.MULTILINE)

    def test_status_bar_pattern(self):
        """Test status bar detection."""
        assert re.search(STATUS_BAR_PATTERN, "23:14  yolo  agent (kimi-for-coding, thinking)")
        assert re.search(STATUS_BAR_PATTERN, "10:30  agent (kimi-for-coding)")
        assert not re.search(STATUS_BAR_PATTERN, "Hello world")

    def test_ansi_code_stripping(self):
        """Test ANSI code pattern strips all escape sequences."""
        raw = "\x1b[1muser@app💫\x1b[0m"
        clean = re.sub(ANSI_CODE_PATTERN, "", raw)
        assert clean == "user@app💫"

        raw2 = "\x1b[38;5;244m•\x1b[39m \x1b[3m\x1b[38;5;244mThinking\x1b[0m"
        clean2 = re.sub(ANSI_CODE_PATTERN, "", raw2)
        assert clean2 == "• Thinking"

    def test_idle_prompt_tail_lines(self):
        """Test tail lines constant is reasonable for Kimi's TUI layout."""
        assert IDLE_PROMPT_TAIL_LINES >= 40  # Must cover tall terminals (46+ rows)
        assert IDLE_PROMPT_TAIL_LINES <= 100  # Not unreasonably large
