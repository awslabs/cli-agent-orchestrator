"""Tests for Gemini CLI provider.

Covers initialization, status detection, message extraction, command building,
pattern matching, and cleanup — targeting >90% code coverage.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.gemini_cli import (
    ANSI_CODE_PATTERN,
    ERROR_PATTERN,
    IDLE_PROMPT_PATTERN,
    IDLE_PROMPT_PATTERN_LOG,
    IDLE_PROMPT_TAIL_LINES,
    INPUT_BOX_BOTTOM_PATTERN,
    INPUT_BOX_TOP_PATTERN,
    MODEL_INDICATOR_PATTERN,
    PROCESSING_SPINNER_PATTERN,
    QUERY_BOX_PREFIX_PATTERN,
    RESPONDING_WITH_PATTERN,
    RESPONSE_PREFIX_PATTERN,
    STATUS_BAR_PATTERN,
    TOOL_CALL_BOX_PATTERN,
    WELCOME_BANNER_PATTERN,
    YOLO_INDICATOR_PATTERN,
    GeminiCliProvider,
    ProviderError,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    """Read a test fixture file."""
    return (FIXTURES_DIR / name).read_text()


# =============================================================================
# Initialization tests
# =============================================================================


class TestGeminiCliProviderInitialization:
    """Tests for GeminiCliProvider initialization flow."""

    @patch("cli_agent_orchestrator.providers.gemini_cli.time")
    @patch("cli_agent_orchestrator.providers.gemini_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_shell, mock_time):
        """Test successful initialization sends warm-up + gemini command and reaches IDLE."""
        # Configure time mock: first call returns 0 (warm-up start), subsequent calls
        # for the init loop need to return 0 then trigger the IDLE status check.
        mock_time.time.side_effect = [0, 0, 0, 0, 0]
        mock_time.sleep = MagicMock()
        # Simulate warm-up marker appearing in shell output, then IDLE status
        idle_output = " *   Type your message or @path/to/file\n"
        mock_tmux.get_history.side_effect = ["CAO_SHELL_READY", idle_output]
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        result = provider.initialize()

        assert result is True
        assert provider._initialized is True
        assert mock_tmux.send_keys.call_count == 2  # warm-up echo + gemini command
        mock_tmux.send_keys.assert_any_call("session-1", "window-1", "echo CAO_SHELL_READY")
        mock_wait_shell.assert_called_once()

    @patch("cli_agent_orchestrator.providers.gemini_cli.wait_for_shell", return_value=False)
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test shell init timeout raises TimeoutError."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        with pytest.raises(TimeoutError, match="Shell initialization"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.gemini_cli.time")
    @patch("cli_agent_orchestrator.providers.gemini_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_initialize_gemini_timeout(self, mock_tmux, mock_wait_shell, mock_time):
        """Test Gemini CLI init timeout raises TimeoutError."""
        # Simulate time progressing past timeout (120s)
        call_count = [0]

        def advancing_time():
            call_count[0] += 1
            return call_count[0] * 10.0  # each call advances 10s

        mock_time.time.side_effect = advancing_time
        mock_time.sleep = MagicMock()
        # Warm-up succeeds, but CLI never reaches IDLE (always returns PROCESSING)
        mock_tmux.get_history.return_value = "CAO_SHELL_READY"
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        with pytest.raises(TimeoutError, match="Gemini CLI initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.gemini_cli.time")
    @patch("cli_agent_orchestrator.providers.gemini_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_initialize_with_mcp_servers(
        self, mock_load, mock_tmux, mock_wait_shell, mock_time, tmp_path
    ):
        """Test initialization with MCP servers writes to settings.json."""
        mock_time.time.side_effect = [0, 0, 0, 0, 0]
        mock_time.sleep = MagicMock()
        idle_output = " *   Type your message or @path/to/file\n"
        mock_tmux.get_history.side_effect = ["CAO_SHELL_READY", idle_output]
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "command": "npx",
                "args": ["-y", "cao-mcp-server"],
            }
        }
        mock_load.return_value = mock_profile

        # Use tmp_path as fake home so we don't touch real ~/.gemini/settings.json
        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"

        with patch("cli_agent_orchestrator.providers.gemini_cli.Path.home", return_value=tmp_path):
            provider = GeminiCliProvider(
                "term-1", "session-1", "window-1", agent_profile="developer"
            )
            result = provider.initialize()

        assert result is True
        # MCP server should be registered in settings.json, not via gemini mcp add
        import json

        settings = json.loads(settings_file.read_text())
        assert "cao-mcp-server" in settings["mcpServers"]
        assert settings["mcpServers"]["cao-mcp-server"]["command"] == "npx"
        assert settings["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "term-1"
        # Command should be plain gemini launch (no chained mcp add)
        call_args = mock_tmux.send_keys.call_args_list[1]
        command = call_args[0][2]
        assert command == "gemini --yolo --sandbox false"
        assert "cao-mcp-server" in provider._mcp_server_names

    @patch("cli_agent_orchestrator.providers.gemini_cli.time")
    @patch("cli_agent_orchestrator.providers.gemini_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_initialize_sends_gemini_command(self, mock_tmux, mock_wait_shell, mock_time):
        """Test that initialize sends warm-up echo then the correct gemini --yolo command."""
        mock_time.time.side_effect = [0, 0, 0, 0, 0]
        mock_time.sleep = MagicMock()
        idle_output = " *   Type your message or @path/to/file\n"
        mock_tmux.get_history.side_effect = ["CAO_SHELL_READY", idle_output]
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider.initialize()

        # First call: warm-up echo
        assert mock_tmux.send_keys.call_args_list[0][0][2] == "echo CAO_SHELL_READY"
        # Second call: gemini command
        assert mock_tmux.send_keys.call_args_list[1][0][2] == "gemini --yolo --sandbox false"

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_initialize_with_invalid_profile(self, mock_load):
        """Test initialization with invalid agent profile raises ProviderError."""
        mock_load.side_effect = FileNotFoundError("Profile not found")

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="nonexistent")
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_gemini_command()

    @patch("cli_agent_orchestrator.providers.gemini_cli.time")
    @patch("cli_agent_orchestrator.providers.gemini_cli.wait_for_shell", return_value=True)
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_initialize_with_prompt_interactive_waits_for_completed(
        self, mock_load, mock_tmux, mock_wait_shell, mock_time
    ):
        """Test that -i flag makes initialize() wait for COMPLETED, not IDLE.

        When -i is used, Gemini processes the system prompt as the first user
        message and produces a response. IDLE alone is premature because the
        Ink TUI shows the idle prompt before -i processing finishes (lesson #18).
        """
        mock_time.time.side_effect = [0, 0, 0, 0, 0, 0, 0]
        mock_time.sleep = MagicMock()
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = {}
        mock_load.return_value = mock_profile

        # First get_history: warm-up marker. Second: idle prompt (should NOT
        # be accepted when -i is used). Third: completed state (response + idle).
        idle_output = " *   Type your message or @path/to/file\n"
        completed_output = (
            "> You are a supervisor.\n"
            "✦ I understand. I am a supervisor.\n"
            " *   Type your message or @path/to/file\n"
        )
        mock_tmux.get_history.side_effect = [
            "CAO_SHELL_READY",
            idle_output,  # 1st status check: IDLE — skipped because -i requires COMPLETED
            completed_output,  # 2nd status check: COMPLETED — accepted
        ]
        mock_tmux.get_pane_working_directory.return_value = None

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="supervisor")
        result = provider.initialize()

        assert result is True
        assert provider._uses_prompt_interactive is True
        assert provider._initialized is True

    def test_uses_prompt_interactive_flag_default(self):
        """Test _uses_prompt_interactive defaults to False."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider._uses_prompt_interactive is False

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_build_command_sets_prompt_interactive_flag(self, mock_tmux, mock_load):
        """Test _build_gemini_command sets _uses_prompt_interactive when -i is used."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = {}
        mock_load.return_value = mock_profile
        mock_tmux.get_pane_working_directory.return_value = None

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="supervisor")
        command = provider._build_gemini_command()

        assert provider._uses_prompt_interactive is True
        assert "-i" in command

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_build_command_no_prompt_interactive_without_system_prompt(self, mock_tmux, mock_load):
        """Test _uses_prompt_interactive stays False when profile has no system prompt."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {}
        mock_load.return_value = mock_profile

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="worker")
        command = provider._build_gemini_command()

        assert provider._uses_prompt_interactive is False
        assert "-i" not in command


# =============================================================================
# Status detection tests
# =============================================================================


class TestGeminiCliProviderStatusDetection:
    """Tests for GeminiCliProvider.get_status()."""

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_idle(self, mock_tmux):
        """Test IDLE detection from fresh startup output."""
        mock_tmux.get_history.return_value = _read_fixture("gemini_cli_idle_output.txt")
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        """Test COMPLETED detection when response is present with prompt."""
        mock_tmux.get_history.return_value = _read_fixture("gemini_cli_completed_output.txt")
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_completed_complex(self, mock_tmux):
        """Test COMPLETED detection with tool call response."""
        mock_tmux.get_history.return_value = _read_fixture("gemini_cli_complex_response.txt")
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        """Test PROCESSING detection when user query is in input box."""
        mock_tmux.get_history.return_value = _read_fixture("gemini_cli_processing_output.txt")
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_error_empty(self, mock_tmux):
        """Test ERROR on empty output."""
        mock_tmux.get_history.return_value = ""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_error_none(self, mock_tmux):
        """Test ERROR on None output."""
        mock_tmux.get_history.return_value = None
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_error_pattern(self, mock_tmux):
        """Test ERROR detection from error output fixture."""
        mock_tmux.get_history.return_value = _read_fixture("gemini_cli_error_output.txt")
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_idle_with_ansi_codes(self, mock_tmux):
        """Test IDLE detection with ANSI escape codes in output."""
        output = (
            "\x1b[38;2;71;150;228m ███ GEMINI BANNER \x1b[0m\n"
            "\n"
            "\x1b[30m\x1b[48;2;11;14;20m▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            "\x1b[39m \x1b[38;2;243;139;168m*\x1b[39m   Type your message or @path\n"
            "\x1b[30m▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "\x1b[39m ~/dir (main)   sandbox   Auto\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_with_tail_lines(self, mock_tmux):
        """Test status detection with tail_lines parameter passed through."""
        mock_tmux.get_history.return_value = _read_fixture("gemini_cli_idle_output.txt")
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider.get_status(tail_lines=20)
        mock_tmux.get_history.assert_called_once_with("session-1", "window-1", tail_lines=20)

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_idle_tall_terminal(self, mock_tmux):
        """Test IDLE detection in tall terminals (46+ rows) where prompt is far from bottom.

        In a tall terminal, the welcome banner and input box may be far from the
        bottom due to Ink's cursor-based rendering and empty padding lines.
        IDLE_PROMPT_TAIL_LINES must be large enough to reach the prompt.
        """
        output = (
            " ███ GEMINI BANNER\n"
            "\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " *   Type your message or @path/to/file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            + "\n" * 32  # 32 empty padding lines (typical for tall terminal)
            + " .../project (main*)   sandbox   Auto (Gemini 3) /model | 200 MB\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_processing_no_idle_prompt(self, mock_tmux):
        """Test PROCESSING when response is mid-stream (no idle prompt, no error)."""
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > write a function\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "✦ Here's the function:\n"
            "\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_not_error_when_response_mentions_error(self, mock_tmux):
        """Test COMPLETED (not ERROR) when response text discusses errors.

        The ✦ response may contain text like 'Error: you need to fix...' which
        matches ERROR_PATTERN. Since the idle prompt is visible, the error check
        is never reached — idle prompt detection takes priority.
        """
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > how to fix this error\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "✦ Error: you need to add a return statement at line 42.\n"
            "✦ Here is the fixed version:\n"
            "\n"
            "                                YOLO mode (ctrl + y to toggle)\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " *   Type your message or @path/to/file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 100 MB\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_processing_spinner_with_idle_prompt(self, mock_tmux):
        """Test PROCESSING when spinner is visible despite idle prompt being shown.

        Gemini's Ink TUI keeps the idle input box visible at the bottom at ALL
        times, even during active processing (tool calls, model thinking).
        The processing spinner (Braille dots + 'esc to cancel') appears above
        the idle prompt. Without spinner detection, get_status() would return
        COMPLETED prematurely (lesson #16).
        """
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > Use the handoff tool to delegate this task\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "╭──────────────────────────────╮\n"
            "│ ✓  handoff (cao-mcp-server)   │\n"
            "╰──────────────────────────────╯\n"
            "⠴ Refining Delegation Parameters (esc to cancel, 50s)\n"
            "\n"
            " 1 MCP server                  YOLO mode (ctrl + y to toggle)\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " *   Type your message or @path/to/file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 234 MB\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_processing_spinner_retry(self, mock_tmux):
        """Test PROCESSING when model is retrying API call (Attempt N/M spinner)."""
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > create a report\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "⠼ Trying to reach gemini-3-flash-preview (Attempt 2/3) (esc to cancel, 2s)\n"
            "\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " *   Type your message or @path/to/file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 100 MB\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_completed_no_spinner(self, mock_tmux):
        """Test COMPLETED when response finished and no spinner is present.

        After the model finishes processing (no spinner), idle prompt visible,
        and response with ✦ prefix visible → COMPLETED.
        """
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > Use the handoff tool to delegate this task\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "╭──────────────────────────────╮\n"
            "│ ✓  handoff (cao-mcp-server)   │\n"
            "╰──────────────────────────────╯\n"
            "✦ Here is the report template from the worker:\n"
            "\n"
            " 1 MCP server                  YOLO mode (ctrl + y to toggle)\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " *   Type your message or @path/to/file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 234 MB\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_get_status_processing_multi_turn_old_response(self, mock_tmux):
        """Test PROCESSING on second query when old ✦ response is in scrollback.

        In a multi-turn conversation, the scrollback contains ✦ from the first
        response. When the second query is processing (no idle prompt at bottom),
        the status should be PROCESSING despite the old ✦ in scrollback.
        """
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > first question\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "✦ First answer from turn 1.\n"
            "\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > second question\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
        )
        mock_tmux.get_history.return_value = output
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status() == TerminalStatus.PROCESSING


# =============================================================================
# Message extraction tests
# =============================================================================


class TestGeminiCliProviderMessageExtraction:
    """Tests for GeminiCliProvider.extract_last_message_from_script()."""

    def test_extract_message_success(self):
        """Test successful message extraction from completed output."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = _read_fixture("gemini_cli_completed_output.txt")
        result = provider.extract_last_message_from_script(output)

        assert len(result) > 0
        assert "Hi" in result or "help" in result

    def test_extract_message_complex_response(self):
        """Test extraction of response with tool calls."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = _read_fixture("gemini_cli_complex_response.txt")
        result = provider.extract_last_message_from_script(output)

        assert len(result) > 0
        assert "test" in result.lower() or "file" in result.lower()

    def test_extract_message_no_query(self):
        """Test ValueError when no user query is found."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = "Some random text without query box"
        with pytest.raises(ValueError, match="No Gemini CLI user query found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_empty_response(self):
        """Test ValueError on empty response after query."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > test message\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " *   Type your message or @path/to/file\n"
        )
        with pytest.raises(ValueError, match="Empty Gemini CLI response"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_filters_chrome(self):
        """Test that input box borders, status bar, YOLO indicator are filtered."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > say hello\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "✦ Hello! How can I help?\n"
            "\n"
            "                                YOLO mode (ctrl + y to toggle)\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " *   Type your message or @path/to/file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 100 MB\n"
        )
        result = provider.extract_last_message_from_script(output)

        assert "Hello! How can I help?" in result
        # Filtered out:
        assert "YOLO mode" not in result
        assert "Responding with" not in result
        assert "sandbox" not in result
        assert "▀" not in result
        assert "▄" not in result

    def test_extract_message_multiple_responses(self):
        """Test extraction picks content from last user query."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > first question\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "✦ First answer\n"
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > second question\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "✦ Second answer\n"
            " *   Type your message or @path/to/file\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Second answer" in result

    def test_extract_message_no_trailing_prompt(self):
        """Test extraction works when there's no trailing idle prompt."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > what is python?\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "✦ Python is a programming language.\n"
            "✦ It supports multiple paradigms.\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Python" in result
        assert "paradigm" in result.lower()

    def test_extract_message_with_tool_call(self):
        """Test extraction includes tool call box content."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > read the file\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "✦ Let me read the file.\n"
            "╭──────────────────────────────╮\n"
            "│ ✓  ReadFile test.txt          │\n"
            "╰──────────────────────────────╯\n"
            "✦ The file contains test data.\n"
            " *   Type your message or @path/to/file\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "read the file" in result.lower() or "file contains" in result.lower()

    def test_extract_message_filters_status_bar_in_response(self):
        """Test that status bar lines within the response window are filtered out.

        In some terminal captures, the status bar (e.g. 'dir (branch) sandbox Auto ...')
        appears between the response and the next idle prompt.
        """
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > hello\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "✦ Hello there!\n"
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 100 MB\n"
            " *   Type your message or @path/to/file\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Hello there!" in result
        assert "sandbox" not in result
        assert "/model" not in result

    def test_extract_message_filters_spinner_lines(self):
        """Test that processing spinner lines are filtered from extracted response."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " > create a report\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "  Responding with gemini-3-flash-preview\n"
            "✦ Here is the report:\n"
            "✦ Summary section content.\n"
            "⠼ I'm Feeling Lucky (esc to cancel, 1s)\n"
            " *   Type your message or @path/to/file\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "report" in result.lower()
        assert "Summary section" in result
        assert "esc to cancel" not in result
        assert "Feeling Lucky" not in result

    def test_extract_message_with_ansi_codes(self):
        """Test extraction strips ANSI codes correctly."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        output = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            " \x1b[38;2;203;166;247m> \x1b[38;2;108;112;134mhello\x1b[39m\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "\x1b[38;2;203;166;247m✦ \x1b[39mHi there!\n"
            " \x1b[38;2;243;139;168m*\x1b[39m   Type your message\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Hi there!" in result


# =============================================================================
# Command building tests
# =============================================================================


class TestGeminiCliProviderBuildCommand:
    """Tests for GeminiCliProvider._build_gemini_command()."""

    def test_build_command_no_profile(self):
        """Test command without agent profile is 'gemini --yolo --sandbox false'."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        command = provider._build_gemini_command()
        assert command == "gemini --yolo --sandbox false"

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_with_mcp_config(self, mock_load, tmp_path):
        """Test command with MCP server writes to settings.json, not gemini mcp add."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"test-server": {"command": "npx", "args": ["test-pkg"]}}
        mock_load.return_value = mock_profile

        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir()

        with patch("cli_agent_orchestrator.providers.gemini_cli.Path.home", return_value=tmp_path):
            provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
            command = provider._build_gemini_command()

        # Command should be plain gemini launch (MCP configured via settings.json)
        assert command == "gemini --yolo --sandbox false"
        # MCP server should be tracked for cleanup
        assert "test-server" in provider._mcp_server_names
        # Verify settings.json was written
        import json

        settings = json.loads((settings_dir / "settings.json").read_text())
        assert settings["mcpServers"]["test-server"]["command"] == "npx"
        assert settings["mcpServers"]["test-server"]["args"] == ["test-pkg"]
        assert settings["mcpServers"]["test-server"]["env"]["CAO_TERMINAL_ID"] == "term-1"

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_with_pydantic_mcp_config(self, mock_load, tmp_path):
        """Test command with MCP servers as Pydantic model objects."""
        mock_server = MagicMock()
        mock_server.model_dump.return_value = {"command": "node", "args": ["server.js"]}

        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"my-server": mock_server}
        mock_load.return_value = mock_profile

        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir()

        with patch("cli_agent_orchestrator.providers.gemini_cli.Path.home", return_value=tmp_path):
            provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
            command = provider._build_gemini_command()

        assert command == "gemini --yolo --sandbox false"
        import json

        settings = json.loads((settings_dir / "settings.json").read_text())
        assert settings["mcpServers"]["my-server"]["command"] == "node"
        assert settings["mcpServers"]["my-server"]["args"] == ["server.js"]
        assert settings["mcpServers"]["my-server"]["env"]["CAO_TERMINAL_ID"] == "term-1"

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_profile_no_mcp(self, mock_load, mock_tmux, tmp_path):
        """Test command with profile uses -i flag for system prompt + writes GEMINI.md."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a developer"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_tmux.get_pane_working_directory.return_value = str(tmp_path)

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_gemini_command()

        # Primary injection: -i flag sends system prompt as first user message
        assert "gemini --yolo --sandbox false -i" in command
        assert "You are a developer" in command
        # Supplementary: GEMINI.md also written for persistent context
        gemini_md = tmp_path / "GEMINI.md"
        assert gemini_md.exists()
        assert gemini_md.read_text() == "You are a developer"
        assert provider._gemini_md_path == str(gemini_md)

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_system_prompt_backs_up_existing_gemini_md(
        self, mock_load, mock_tmux, tmp_path
    ):
        """Test GEMINI.md backup when user already has one in the working directory."""
        # Create an existing GEMINI.md
        existing_md = tmp_path / "GEMINI.md"
        existing_md.write_text("User's existing instructions")

        mock_profile = MagicMock()
        mock_profile.system_prompt = "Supervisor agent prompt"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_tmux.get_pane_working_directory.return_value = str(tmp_path)

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_gemini_command()

        # Primary: -i flag
        assert "-i" in command
        assert "Supervisor agent prompt" in command
        # Supplementary: GEMINI.md backed up and overwritten
        assert existing_md.read_text() == "Supervisor agent prompt"
        backup = tmp_path / "GEMINI.md.cao_backup"
        assert backup.exists()
        assert backup.read_text() == "User's existing instructions"
        assert provider._gemini_md_backup_path == str(backup)

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_system_prompt_no_working_dir(self, mock_load, mock_tmux):
        """Test -i flag still used when working dir unavailable (GEMINI.md skipped)."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a developer"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_tmux.get_pane_working_directory.return_value = None

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_gemini_command()

        # -i flag is always added (primary injection), GEMINI.md is optional
        assert "-i" in command
        assert "You are a developer" in command
        assert provider._gemini_md_path is None

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_profile_error(self, mock_load):
        """Test command raises ProviderError when profile loading fails."""
        mock_load.side_effect = FileNotFoundError("not found")

        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="bad")
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_gemini_command()

    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_build_command_multiple_mcp_servers(self, mock_load, tmp_path):
        """Test multiple MCP servers are all written to settings.json."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "server-a": {"command": "npx", "args": ["-y", "server-a"]},
            "server-b": {"command": "node", "args": ["b.js"]},
        }
        mock_load.return_value = mock_profile

        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir()

        with patch("cli_agent_orchestrator.providers.gemini_cli.Path.home", return_value=tmp_path):
            provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
            command = provider._build_gemini_command()

        # Command should be plain gemini launch (no && chaining)
        assert command == "gemini --yolo --sandbox false"
        assert " && " not in command
        assert len(provider._mcp_server_names) == 2
        # Both servers written to settings.json
        import json

        settings = json.loads((settings_dir / "settings.json").read_text())
        assert "server-a" in settings["mcpServers"]
        assert "server-b" in settings["mcpServers"]


# =============================================================================
# Misc / lifecycle tests
# =============================================================================


class TestGeminiCliProviderMisc:
    """Tests for miscellaneous GeminiCliProvider methods and lifecycle."""

    def test_exit_cli(self):
        """Test exit command returns C-d (Ctrl+D)."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider.exit_cli() == "C-d"

    def test_get_idle_pattern_for_log(self):
        """Test idle pattern for log monitoring matches idle prompt."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        pattern = provider.get_idle_pattern_for_log()
        assert pattern == IDLE_PROMPT_PATTERN_LOG
        assert re.search(pattern, " *   Type your message or @path/to/file")

    def test_cleanup(self):
        """Test cleanup resets initialized state."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider._initialized = True
        provider._mcp_server_names = []
        provider.cleanup()
        assert provider._initialized is False

    def test_cleanup_removes_mcp_servers(self, tmp_path):
        """Test cleanup removes MCP servers from settings.json."""
        import json

        # Pre-populate settings.json with MCP servers
        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "server-a": {"command": "npx", "args": ["-y", "a"], "env": {}},
                        "server-b": {"command": "node", "args": ["b.js"], "env": {}},
                        "unrelated": {"command": "other", "args": [], "env": {}},
                    }
                }
            )
        )

        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider._mcp_server_names = ["server-a", "server-b"]

        with patch("cli_agent_orchestrator.providers.gemini_cli.Path.home", return_value=tmp_path):
            provider.cleanup()

        assert provider._mcp_server_names == []
        # server-a and server-b removed, unrelated preserved
        settings = json.loads(settings_file.read_text())
        assert "server-a" not in settings["mcpServers"]
        assert "server-b" not in settings["mcpServers"]
        assert "unrelated" in settings["mcpServers"]

    def test_cleanup_handles_mcp_removal_error(self, tmp_path):
        """Test cleanup handles errors when settings.json is malformed."""
        # Write invalid JSON to settings.json
        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text("not valid json{{{")

        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider._mcp_server_names = ["server-a"]

        with patch("cli_agent_orchestrator.providers.gemini_cli.Path.home", return_value=tmp_path):
            # Should not raise
            provider.cleanup()
        assert provider._mcp_server_names == []
        assert provider._initialized is False

    def test_cleanup_removes_gemini_md(self, tmp_path):
        """Test cleanup removes GEMINI.md file created for system prompt."""
        gemini_md = tmp_path / "GEMINI.md"
        gemini_md.write_text("Supervisor agent prompt")

        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider._gemini_md_path = str(gemini_md)
        provider.cleanup()

        assert not gemini_md.exists()
        assert provider._gemini_md_path is None

    def test_cleanup_restores_backup_gemini_md(self, tmp_path):
        """Test cleanup restores user's original GEMINI.md from backup."""
        gemini_md = tmp_path / "GEMINI.md"
        gemini_md.write_text("CAO injected prompt")
        backup = tmp_path / "GEMINI.md.cao_backup"
        backup.write_text("User's original instructions")

        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        provider._gemini_md_path = str(gemini_md)
        provider._gemini_md_backup_path = str(backup)
        provider.cleanup()

        # Original restored, backup removed
        assert gemini_md.exists()
        assert gemini_md.read_text() == "User's original instructions"
        assert not backup.exists()
        assert provider._gemini_md_path is None
        assert provider._gemini_md_backup_path is None

    def test_provider_inherits_base(self):
        """Test provider inherits from BaseProvider."""
        from cli_agent_orchestrator.providers.base import BaseProvider

        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert isinstance(provider, BaseProvider)

    def test_provider_default_state(self):
        """Test provider default initialization state."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1")
        assert provider._initialized is False
        assert provider._agent_profile is None
        assert provider._mcp_server_names == []
        assert provider._gemini_md_path is None
        assert provider._gemini_md_backup_path is None
        assert provider.terminal_id == "term-1"
        assert provider.session_name == "session-1"
        assert provider.window_name == "window-1"

    def test_provider_with_agent_profile(self):
        """Test provider stores agent profile."""
        provider = GeminiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        assert provider._agent_profile == "dev"


# =============================================================================
# Pattern tests
# =============================================================================


class TestGeminiCliProviderPatterns:
    """Tests for Gemini CLI regex patterns — validates correctness of all patterns."""

    def test_idle_prompt_pattern(self):
        """Test idle prompt pattern matches asterisk + placeholder text."""
        assert re.search(IDLE_PROMPT_PATTERN, " *   Type your message or @path/to/file")
        assert re.search(IDLE_PROMPT_PATTERN, " * Type your message")

    def test_idle_prompt_pattern_does_not_match_user_input(self):
        """Test idle prompt pattern doesn't match user-typed text with * prefix."""
        assert not re.search(IDLE_PROMPT_PATTERN, " * hello world")
        assert not re.search(IDLE_PROMPT_PATTERN, " * reply with exactly")

    def test_idle_prompt_pattern_does_not_match_random_text(self):
        """Test idle prompt pattern doesn't match arbitrary text."""
        assert not re.search(IDLE_PROMPT_PATTERN, "Hello world")
        assert not re.search(IDLE_PROMPT_PATTERN, "✦ response text")

    def test_welcome_banner_pattern(self):
        """Test welcome banner detection with block characters."""
        assert re.search(WELCOME_BANNER_PATTERN, " ███ █████████  ██████████ ██████")
        assert not re.search(WELCOME_BANNER_PATTERN, "Welcome to Kimi Code CLI!")

    def test_query_box_prefix_pattern(self):
        """Test query box prefix (>) detection."""
        assert re.search(QUERY_BOX_PREFIX_PATTERN, " > say hi")
        assert re.search(QUERY_BOX_PREFIX_PATTERN, " > test")
        assert not re.search(QUERY_BOX_PREFIX_PATTERN, " >  ")  # > with just spaces
        assert not re.search(QUERY_BOX_PREFIX_PATTERN, "✦ response")

    def test_response_prefix_pattern(self):
        """Test response prefix (✦) detection."""
        assert re.search(RESPONSE_PREFIX_PATTERN, "✦ Hi! How can I help?")
        assert re.search(RESPONSE_PREFIX_PATTERN, "✦ The file contains test data.")
        assert not re.search(RESPONSE_PREFIX_PATTERN, "Hello world")
        assert not re.search(RESPONSE_PREFIX_PATTERN, "> query text")

    def test_model_indicator_pattern(self):
        """Test model indicator line detection."""
        assert re.search(MODEL_INDICATOR_PATTERN, "  Responding with gemini-3-flash-preview")
        assert re.search(MODEL_INDICATOR_PATTERN, "Responding with gemini-2.5-flash")
        assert not re.search(MODEL_INDICATOR_PATTERN, "Hello world")

    def test_tool_call_box_pattern(self):
        """Test tool call box border detection."""
        assert re.search(TOOL_CALL_BOX_PATTERN, "╭──────────────╮")
        assert re.search(TOOL_CALL_BOX_PATTERN, "╰──────────────╯")
        assert not re.search(TOOL_CALL_BOX_PATTERN, "│ ✓ ReadFile │")

    def test_input_box_border_patterns(self):
        """Test input box border detection."""
        assert re.search(INPUT_BOX_TOP_PATTERN, "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀")
        assert re.search(INPUT_BOX_BOTTOM_PATTERN, "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄")
        assert not re.search(INPUT_BOX_TOP_PATTERN, "▀▀▀")  # Too short

    def test_status_bar_pattern(self):
        """Test status bar detection."""
        assert re.search(
            STATUS_BAR_PATTERN,
            " .../dir (main)   no sandbox   Auto (Gemini 3) /model | 100 MB",
        )
        assert re.search(
            STATUS_BAR_PATTERN,
            " .../gemini-test-dir (master*)   sandbox   Auto (Gemini 3) /model | 240.3 MB",
        )
        assert not re.search(STATUS_BAR_PATTERN, "Hello world")

    def test_yolo_indicator_pattern(self):
        """Test YOLO mode indicator detection."""
        assert re.search(YOLO_INDICATOR_PATTERN, "YOLO mode (ctrl + y to toggle)")
        assert re.search(YOLO_INDICATOR_PATTERN, "  YOLO mode")
        assert not re.search(YOLO_INDICATOR_PATTERN, "normal mode")

    def test_error_pattern(self):
        """Test error pattern detection."""
        assert re.search(ERROR_PATTERN, "Error: connection failed", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "ERROR: something went wrong", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "ConnectionError: timeout", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "APIError: rate limited", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "Traceback (most recent call last):", re.MULTILINE)
        assert not re.search(ERROR_PATTERN, "No errors found", re.MULTILINE)

    def test_ansi_code_stripping(self):
        """Test ANSI code pattern strips all escape sequences."""
        raw = "\x1b[38;2;203;166;247m✦ \x1b[39mHi there!"
        clean = re.sub(ANSI_CODE_PATTERN, "", raw)
        assert clean == "✦ Hi there!"

        raw2 = "\x1b[38;2;243;139;168m*\x1b[39m   Type your message"
        clean2 = re.sub(ANSI_CODE_PATTERN, "", raw2)
        assert clean2 == "*   Type your message"

    def test_processing_spinner_pattern(self):
        """Test processing spinner detection (Braille dots + esc to cancel)."""
        assert re.search(
            PROCESSING_SPINNER_PATTERN, "⠴ Refining Delegation Parameters (esc to cancel, 50s)"
        )
        assert re.search(
            PROCESSING_SPINNER_PATTERN,
            "⠧ Clarifying the Template Retrieval (esc to cancel, 1m 55s)",
        )
        assert re.search(
            PROCESSING_SPINNER_PATTERN,
            "⠼ Trying to reach gemini-3-flash-preview (Attempt 2/3) (esc to cancel, 2s)",
        )
        assert re.search(PROCESSING_SPINNER_PATTERN, "⠋ I'm Feeling Lucky (esc to cancel, 1s)")
        assert not re.search(PROCESSING_SPINNER_PATTERN, "Hello world")
        assert not re.search(PROCESSING_SPINNER_PATTERN, "✦ Here is the response")
        assert not re.search(PROCESSING_SPINNER_PATTERN, " *   Type your message")

    def test_responding_with_pattern(self):
        """Test 'Responding with' model indicator detection."""
        assert re.search(RESPONDING_WITH_PATTERN, "  Responding with gemini-3-flash-preview")
        assert re.search(RESPONDING_WITH_PATTERN, "Responding with gemini-2.5-flash")
        assert not re.search(RESPONDING_WITH_PATTERN, "Hello world")

    def test_idle_prompt_tail_lines(self):
        """Test tail lines constant is reasonable for Gemini's TUI layout."""
        assert IDLE_PROMPT_TAIL_LINES >= 40  # Must cover tall terminals
        assert IDLE_PROMPT_TAIL_LINES <= 100  # Not unreasonably large
