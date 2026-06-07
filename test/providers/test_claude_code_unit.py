"""Unit tests for Claude Code provider."""

import json
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider, ProviderError

# All initialization tests need to patch _ensure_skip_bypass_prompt_setting
# to avoid writing to the real ~/.claude/settings.json.
_PATCH_SETTINGS = patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")


class TestClaudeCodeProviderInitialization:
    """Tests for ClaudeCodeProvider initialization."""

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_success(self, mock_tmux, mock_wait_status, mock_wait_shell, _):
        """Test successful initialization."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        # First call is the pre-launch snapshot, subsequent calls return Claude output
        mock_tmux.get_history.side_effect = [
            "",
            "Welcome to Claude Code v2.0",
            "Welcome to Claude Code v2.0",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True
        assert provider._initialized is True
        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test initialization with shell timeout."""
        mock_wait_shell.return_value = False

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_timeout(self, mock_tmux, mock_wait_status, mock_wait_shell, _):
        """Test initialization timeout when no Claude markers appear."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        # Snapshot and loop return the same content → no new Claude markers
        mock_tmux.get_history.return_value = "some shell output"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        with (
            patch.object(provider, "_handle_startup_prompts"),
            patch("cli_agent_orchestrator.providers.claude_code.time.time", side_effect=[0, 31]),
            patch("cli_agent_orchestrator.providers.claude_code.time.sleep"),
        ):
            with pytest.raises(TimeoutError, match="Claude Code initialization timed out"):
                await provider.initialize()

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_with_agent_profile(
        self, mock_tmux, mock_wait_status, mock_wait_shell, mock_load, _
    ):
        """Test initialization with agent profile."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.side_effect = [
            "",
            "Welcome to Claude Code v2.0",
            "Welcome to Claude Code v2.0",
        ]
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Test system prompt"
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True
        mock_load.assert_called_once_with("test-agent")

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_with_missing_profile_falls_back_to_native_agent(
        self, mock_tmux, mock_wait_status, mock_wait_shell, mock_load, _
    ):
        """Test missing CAO profile falls back to --agent <name> for native agent store."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.side_effect = FileNotFoundError("Profile not found")
        mock_tmux.get_history.side_effect = [
            "",
            "Welcome to Claude Code v2.0",
            "Welcome to Claude Code v2.0",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "my-native-agent")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True
        # Verify --agent flag was passed with the profile name
        send_keys_call = mock_tmux.send_keys.call_args
        command = (
            send_keys_call[0][2]
            if len(send_keys_call[0]) > 2
            else send_keys_call[1].get("keys", "")
        )
        assert "--agent my-native-agent" in command

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_with_broken_profile_raises_provider_error(
        self, mock_tmux, mock_load, mock_wait_shell, _
    ):
        """Test that a broken profile (parse error) raises ProviderError, not silent fallback."""
        mock_wait_shell.return_value = True
        mock_load.side_effect = RuntimeError("YAML parse error in profile")

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "broken-agent")

        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            await provider.initialize()

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_uses_native_agent_from_profile(self, mock_load):
        """Test profile with native_agent field uses --agent passthrough."""
        mock_profile = MagicMock()
        mock_profile.native_agent = "my-claude-agent"
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        command = provider._build_claude_command()

        assert "--agent my-claude-agent" in command
        assert "--append-system-prompt" not in command
        assert "--mcp-config" not in command

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_with_mcp_servers(
        self, mock_tmux, mock_wait_status, mock_wait_shell, mock_load, _
    ):
        """Test initialization with MCP servers in profile."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.side_effect = [
            "",
            "Welcome to Claude Code v2.0",
            "Welcome to Claude Code v2.0",
        ]
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"server1": {"command": "test", "args": ["--flag"]}}
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_sends_claude_command(
        self, mock_tmux, mock_wait_status, mock_wait_shell, _
    ):
        """Test that initialize sends the 'claude' command to tmux."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.side_effect = [
            "",
            "Welcome to Claude Code v2.0",
            "Welcome to Claude Code v2.0",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            await provider.initialize()

        call_args = mock_tmux.send_keys.call_args
        assert call_args[0][0] == "test-session"
        assert call_args[0][1] == "window-0"
        assert "claude --dangerously-skip-permissions" in call_args[0][2]


class TestClaudeCodeProviderStatusDetection:
    """Tests for ClaudeCodeProvider status detection."""

    def test_get_status_idle_old_prompt(self):
        """Test IDLE status detection with old '>' prompt."""
        output = "> "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_idle_new_prompt(self):
        """Test IDLE status detection with new '❯' prompt."""
        output = "❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_idle_with_ansi_codes(self):
        """Test IDLE status detection with ANSI codes around prompt."""
        output = (
            "\x1b[2m\x1b[38;2;136;136;136m────────────\n"
            '\x1b[0m❯ \x1b[7mT\x1b[0;2mry\x1b[0m \x1b[2m"hello"\x1b[0m\n'
            "\x1b[2m\x1b[38;2;136;136;136m────────────\x1b[0m"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed(self):
        """Test COMPLETED status detection."""
        output = "⏺ Here is the response\n> "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_completed_with_new_prompt(self):
        """Test COMPLETED status detection with new '❯' prompt."""
        output = "⏺ Here is the response\n❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing(self):
        """Test PROCESSING status detection."""
        output = "✶ Processing… (esc to interrupt)"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_processing_minimal_spinner(self):
        """Test PROCESSING detection with minimal spinner format (no parenthesized text)."""
        output = "✻ Orbiting…"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_processing_beats_stale_completed(self):
        """Test that PROCESSING is detected even when stale ⏺ and ❯ markers are in scrollback."""
        output = (
            "⏺ Previous response from init\n"
            "❯ user task message\n"
            "⏺ Let me read the file\n"
            "✻ Orbiting…"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_completed_despite_stale_spinner_in_scrollback(self):
        """Stale spinner in scrollback must not block COMPLETED detection (#104)."""
        output = (
            "✻ Orbiting…\n"
            "⏺ Previous response\n"
            "❯ user sent new task\n"
            "⏺ Completed response\n"
            "❯ "
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_idle_despite_stale_spinner_in_scrollback(self):
        """Stale spinner in scrollback must not block IDLE detection (#104)."""
        output = "✶ Processing… (esc to interrupt)\n" "Some previous output\n" "❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_processing_spinner_before_separator(self):
        """Spinner immediately before ──────── separator → PROCESSING (structural check)."""
        output = (
            "❯ do the task\n"
            "⏺ Let me read the file\n"
            "✢ Thinking…\n"
            "\n"
            "────────────────────────\n"
            "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_completed_no_spinner_before_separator(self):
        """Response text (no spinner) before separator → COMPLETED, not PROCESSING."""
        output = (
            "❯ do the task\n" "⏺ Here is the completed response\n" "────────────────────────\n" "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_stale_spinner_far_back_not_processing(self):
        """Stale spinner far back in scrollback + current separator with no spinner → COMPLETED."""
        output = (
            "✢ Thinking…\n"
            "⏺ Old response from first task line 1\n"
            "Old response from first task line 2\n"
            "Old response from first task line 3\n"
            "Old response from first task line 4\n"
            "────────────────────────\n"
            "❯ second task\n"
            "⏺ Completed second response\n"
            "────────────────────────\n"
            "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_processing_no_separator_yet(self):
        """Early execution with spinner but no separator yet → position fallback PROCESSING."""
        output = "✻ Orbiting…"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_processing_ansi_separator(self):
        """Spinner before separator with ANSI colour codes on separator → PROCESSING."""
        output = (
            "❯ do the task\n"
            "⏺ Reading file…\n"
            "✽ Cooking…\n"
            "\n"
            "\x1b[38;5;244m────────────────────────\x1b[0m\n"
            "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_processing_middle_dot_spinner(self):
        """New · Swirling… spinner variant → PROCESSING via structural check."""
        output = "❯ do the task\n" "· Swirling…\n" "\n" "────────────────────────\n" "❯ "
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_idle_not_false_processing_from_status_bar(self):
        """Status bar '· latest:…' must not false-positive as PROCESSING."""
        output = (
            "Claude Code v2.1.63\n"
            "────────────────────\n"
            "❯ \n"
            "────────────────────\n"
            "  current: 2.1.63 · latest:…"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_waiting_user_answer(self):
        """Test WAITING_USER_ANSWER status detection."""
        output = (
            "❯ 1. Option one\n"
            "  2. Option two\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_stale_scrollback_not_waiting_user_answer(self):
        """Stale numbered scrollback without the active footer must not block input."""
        output = "❯ 1. Option one\n" "  2. Option two\n" "⏺ Selection handled earlier\n" "❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert status == TerminalStatus.COMPLETED

    def test_get_status_error_empty(self):
        """Test UNKNOWN status with empty output."""
        output = ""

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.UNKNOWN

    def test_get_status_error_unrecognized(self):
        """Test UNKNOWN status with unrecognized output."""
        output = "Some random output without patterns"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.UNKNOWN

    def test_get_status_completed_after_compaction_not_false_processing(self):
        """Compaction spinner before its own separator, then more output; last sep has no spinner → COMPLETED."""
        output = (
            "❯ do the task\n"
            "⏺ Starting work…\n"
            "✢ Compacting conversation…\n"
            "────────────────────────\n"
            "⏺ Here is the completed response\n"
            "────────────────────────\n"
            "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_processing_after_compaction_when_still_running(self):
        """Spinner before the last separator (agent resumes after compaction) → PROCESSING."""
        output = (
            "❯ do the task\n"
            "✢ Compacting conversation…\n"
            "────────────────────────\n"
            "⏺ Resuming work…\n"
            "✻ Orbiting…\n"
            "────────────────────────\n"
            "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_completed_after_exit_not_false_processing(self):
        """Spinner → sep (task done) → /exit → second sep; spinner NOT before last sep → not PROCESSING."""
        output = (
            "❯ do the task\n"
            "⏺ Working on it…\n"
            "✻ Orbiting…\n"
            "────────────────────────\n"
            "❯ /exit\n"
            "⏺ Goodbye!\n"
            "────────────────────────\n"
            "❯ "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) != TerminalStatus.PROCESSING

    def test_get_status_new_tui_completed_box(self):
        """Newest TUI: '✻ Sautéed for Ns' summary above an empty boxed ❯ → COMPLETED.

        The box arrives with blank lines between separators and the ❯ (the form
        strip_terminal_escapes produces from in-place CUU/CHA redraws).
        """
        output = (
            "●def greet(name):\n" "✻ Sautéed for 1s\n" + "─" * 30 + "\n\n❯ \n\n" + "─" * 30 + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_new_tui_live_spinner_box(self):
        """Newest TUI: a live '…ing…' spinner above the boxed ❯ → PROCESSING.

        The spinner renders ABOVE the box top border, where the structural
        spinner-before-separator walk cannot see it; the box-gated branch must.
        """
        output = (
            "●def greet(name):\n" "✢ Cultivating…\n" + "─" * 30 + "\n\n❯ \n\n" + "─" * 30 + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_boxless_completion_summary(self):
        """Newest TUI, box rolled out of the buffer: summary + bare ❯ → COMPLETED.

        A fast turn can push the box separators out of the rolling buffer while
        the '✻ Sautéed for Ns' summary and trailing prompt survive; COMPLETED
        must still be detected without the box gate.
        """
        output = "✻ Sautéed for 1s\n❯ \n← for agents\n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_new_tui_real_raw_capture_completed(self):
        """Regression for the real raw FIFO capture of a finished newest-TUI turn.

        Drives the full pipeline get_status -> strip_terminal_escapes -> box gate
        on the actual captured bytes (escape/redraw sequences intact), unlike the
        cleaned inline literals above. See the new-TUI box-adjacency fix.
        """
        from cli_agent_orchestrator.providers.claude_code import NEW_TUI_BOX_PATTERN
        from cli_agent_orchestrator.utils.text import strip_terminal_escapes

        fixture = Path(__file__).parent / "fixtures" / "claude_code_new_tui_completed_raw.txt"
        raw = fixture.read_text(encoding="utf-8", errors="replace")

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(raw) == TerminalStatus.COMPLETED
        # Lock the gate behaviour the fix depends on: the box is detectable in the
        # cleaned buffer despite the blank lines the redraw escapes introduce.
        assert NEW_TUI_BOX_PATTERN.search(strip_terminal_escapes(raw))

    def test_get_status_asterisk_spinner_frame_is_processing(self):
        """A live spinner on its ASCII '*' animation frame → PROCESSING, not IDLE.

        The newest TUI cycles its spinner glyph through "· ✢ * ✶ ✻ ✽"; the bare
        '*' frame was previously absent from the spinner classes, so a turn whose
        captured frame landed on '*' read as IDLE.
        """
        box = "─" * 30
        output = "●working\n* Cultivating… (2s · ↓ 5 tokens)\n" + box + "\n\n❯\xa0\n\n" + box + "\n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_asterisk_spinner_not_false_completed(self):
        """An in-flight '*' spinner above the box wins over a completion-shaped
        line embedded in the streamed answer → PROCESSING, never a false COMPLETED.
        """
        box = "─" * 30
        output = (
            "●Here is the expected render:\n✻ Sautéed for 1s\n...done.\n"
            "* Cultivating… (2s · ↓ 5 tokens)\n" + box + "\n\n❯\xa0\n\n" + box + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_stale_spinner_above_response_in_box_not_processing(self):
        """A stale spinner left ABOVE a response (empty box, no summary) is not the
        line directly above the box → COMPLETED, not a false PROCESSING.
        """
        box = "─" * 24
        output = "✢ Cultivating…\n⏺ Old response\n" + box + "\n❯ \n" + box + "\n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_mid_buffer_blockquote_box_not_processing(self):
        """A separator-framed markdown blockquote in the response is NOT the input
        box (it does not contain the last ❯), so a spinner-shaped bullet near it
        must not trigger PROCESSING on a finished legacy ⏺ turn.
        """
        box = "─" * 24
        output = (
            "⏺ Done. Here is the markdown:\n· Refactoring…\n" + box + "\n\n> \n\n" + box + "\n❯ \n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_completed_survives_version_footer(self):
        """A finished new-TUI turn whose footer shows the "· latest:…" version
        notice must stay COMPLETED (the gerund-anchored spinner guard ignores the
        status bar), not collapse to a timeout-inducing IDLE.
        """
        box = "─" * 30
        output = (
            "●done\n✻ Sautéed for 1s\n"
            + box
            + "\n\n❯\xa0\n\n"
            + box
            + "\n  current: 2.1.63 · latest:…"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_response_bullet_above_box_not_processing(self):
        """A response bullet ending in '…' directly above the box is NOT a spinner.

        The line-above-box check requires the gerund to be the FIRST word after the
        glyph, so a markdown bullet like "* Remember to deploy…" cannot be mistaken
        for a live "* Cultivating…" spinner and flip a finished turn to PROCESSING.
        """
        box = "─" * 30
        output = (
            "⏺ I updated the config and verified the tests pass.\n"
            "* Remember to restart the service after deploying…\n" + box + "\n❯ \n" + box + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_version_notice_above_box_not_processing(self):
        """A "· latest: … update…" version notice directly above the box is not a
        spinner (no first-word gerund) → COMPLETED, not a false PROCESSING.
        """
        box = "─" * 30
        output = (
            "⏺ All done. Anything else?\n"
            "· latest: v2.1.50 available, run /upgrade to update…\n" + box + "\n❯ \n" + box + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_multiword_compaction_spinner_above_box(self):
        """The MULTI-WORD live spinner "✢ Compacting conversation…" directly above
        the box → PROCESSING. The gerund need only be the FIRST word; the ellipsis
        may follow later, so a real compaction frame is not misread as COMPLETED.
        """
        box = "─" * 75
        output = (
            "⏺ Starting work on the task…\n│ reading files\n\n"
            "❯ refactor the auth module\n\n✢ Compacting conversation…\n\n\n"
            + box
            + "\n\n❯ \n\n"
            + box
            + "\n\n⏵⏵ bypass permissions on · esc to interrupt · high · /effort\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_column_positioned_completion_summary(self):
        """COMPLETED when the completion summary is laid out with column-move
        escapes instead of literal spaces.

        The newest TUI sometimes redraws the summary as
        "✻\\x1b[3GWorked\\x1b[10Gfor\\x1b[14G3s" (each word positioned with CHA),
        which has NO literal spaces. get_status -> strip_terminal_escapes must
        re-insert spaces so "Worked for 3s" matches the completion pattern; a raw
        capture from a real handoff otherwise stuck at IDLE forever.
        """
        box = "─" * 40
        output = (
            '●def greet(name):\n    return f"Hello, {name}!"\n\n\n'
            "\x1b[38;5;246m✻\x1b[3GWorked\x1b[10Gfor\x1b[14G3s\x1b[39m\n\n\n"
            + box
            + "\n\x1b[3G❯\xa0\n"
            + box
            + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED


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

    def test_extract_message_preserves_mid_line_angle_bracket(self):
        """Test that > in mid-line content (Java generics, git diffs, HTML) is not a stop."""
        output = """⏺ Here is the code:
List<String> items = new ArrayList<>();
Map<String, List<Integer>> nested = getMap();
> """
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "List<String>" in result
        assert "Map<String, List<Integer>>" in result

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

        assert "claude --dangerously-skip-permissions" in command
        assert "--permission-mode" not in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_claude_command_with_system_prompt(self, mock_load):
        """Test building Claude command with system prompt."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Test prompt\nwith newlines"
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        command = provider._build_claude_command()

        assert "claude" in command
        assert "--append-system-prompt" in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_mcp_injects_terminal_id(self, mock_load):
        """Test that _build_claude_command injects CAO_TERMINAL_ID into MCP server env."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "cao-mcp-server", "args": ["--port", "8080"]}
        }
        mock_profile.permissionMode = None
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

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_mcp_preserves_existing_env(self, mock_load):
        """Test that existing env vars in MCP config are preserved when injecting CAO_TERMINAL_ID."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "my-server": {
                "command": "my-server",
                "env": {"MY_VAR": "my_value", "OTHER": "other_value"},
            }
        }
        mock_profile.permissionMode = None
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

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_mcp_does_not_override_existing_terminal_id(self, mock_load):
        """Test that an existing CAO_TERMINAL_ID in MCP env is NOT overwritten."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "my-server": {
                "command": "my-server",
                "env": {"CAO_TERMINAL_ID": "user-provided-id"},
            }
        }
        mock_profile.permissionMode = None
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


class TestClaudeCodeProviderModelFlag:
    """Tests that profile.model is forwarded to Claude Code via --model."""

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_appends_model_when_set(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = "sonnet"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent")
        command = provider._build_claude_command()

        assert "--model sonnet" in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_omits_model_when_unset(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent")
        command = provider._build_claude_command()

        assert "--model" not in command


class TestClaudeCodeProviderPermissionMode:

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_uses_permission_mode_when_set_and_not_yolo(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = "auto"
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent")
        command = provider._build_claude_command()

        assert "--permission-mode auto" in command
        assert "--dangerously-skip-permissions" not in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_yolo_overrides_permission_mode(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = "auto"
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent", allowed_tools=["*"])
        command = provider._build_claude_command()

        assert "--dangerously-skip-permissions" in command
        assert "--permission-mode" not in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_legacy_profile_without_permission_mode(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent")
        command = provider._build_claude_command()

        assert "--dangerously-skip-permissions" in command
        assert "--permission-mode" not in command


class TestClaudeCodeProviderStartupPrompts:
    """Tests for Claude Code startup prompt handling (trust + bypass)."""

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_startup_prompts_detected_and_accepted(self, mock_tmux):
        """Test that trust prompt is detected and auto-accepted."""
        mock_tmux.get_history.return_value = (
            "\x1b[1m❯\x1b[0m 1. Yes, I trust this folder\n  2. No, don't trust\n"
        )
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(timeout=2.0)

        mock_pane.send_keys.assert_called_once_with("", enter=True)

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_startup_prompts_not_needed(self, mock_tmux):
        """Test early return when Claude Code starts without prompts."""
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.1.0"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(timeout=2.0)

        mock_tmux.server.sessions.get.assert_not_called()

    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_startup_prompts_timeout(self, mock_tmux, mock_time):
        """Test startup prompt handler times out gracefully."""
        mock_tmux.get_history.return_value = "Loading..."
        mock_time.time.side_effect = [0.0, 0.0, 25.0]
        mock_time.sleep = MagicMock()

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(timeout=20.0)

        mock_tmux.server.sessions.get.assert_not_called()

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_startup_prompts_empty_output_then_detected(self, mock_tmux):
        """Test trust prompt detection after initially empty output."""
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
        provider._handle_startup_prompts(timeout=5.0)

        mock_pane.send_keys.assert_called_once_with("", enter=True)

    @patch("cli_agent_orchestrator.providers.claude_code.subprocess")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_bypass_prompt_detected_and_accepted(self, mock_tmux, mock_subprocess):
        """Test that bypass permissions prompt is detected and auto-accepted."""
        # First poll: bypass prompt; second poll: welcome banner (after dismissal)
        mock_tmux.get_history.side_effect = [
            "WARNING: Claude Code running in Bypass Permissions mode\n"
            "❯ 1. No, exit\n  2. Yes, I accept\n",
            "Welcome to Claude Code v2.1.74",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(timeout=5.0)

        # Verify raw Down arrow escape sequence + Enter was sent via subprocess
        calls = mock_subprocess.run.call_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == [
            "tmux",
            "send-keys",
            "-t",
            "test-session:window-0",
            "-l",
            "\x1b[B",
        ]
        assert calls[1].args[0] == ["tmux", "send-keys", "-t", "test-session:window-0", "Enter"]

    @patch("cli_agent_orchestrator.providers.claude_code.subprocess")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_handle_bypass_then_trust_prompt(self, mock_tmux, mock_subprocess):
        """Test that bypass prompt is handled, then trust prompt follows."""
        # Poll 1: bypass prompt; Poll 2: trust prompt (after bypass dismissed)
        mock_tmux.get_history.side_effect = [
            "WARNING: Bypass Permissions mode\n❯ 1. No, exit\n  2. Yes, I accept\n",
            "❯ 1. Yes, I trust this folder\n  2. No",
        ]
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(timeout=5.0)

        # Bypass: 2 subprocess calls (Down + Enter), then trust: 1 pane.send_keys call
        sub_calls = mock_subprocess.run.call_args_list
        assert len(sub_calls) == 2
        assert sub_calls[0].args[0] == [
            "tmux",
            "send-keys",
            "-t",
            "test-session:window-0",
            "-l",
            "\x1b[B",
        ]
        pane_calls = mock_pane.send_keys.call_args_list
        assert len(pane_calls) == 1
        assert pane_calls[0].args == ("",)
        assert pane_calls[0].kwargs == {"enter": True}

    def test_get_status_trust_prompt_not_waiting_user_answer(self):
        """Test that trust prompt is NOT detected as WAITING_USER_ANSWER."""
        output = (
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, don't trust this folder\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status != TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_bypass_prompt_not_waiting_user_answer(self):
        """Test that bypass prompt is NOT detected as WAITING_USER_ANSWER."""
        output = (
            "WARNING: Bypass Permissions mode\n"
            "❯ 1. No, exit\n"
            "  2. Yes, I accept\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)

        assert status != TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    async def test_initialize_calls_handle_startup_prompts(
        self, mock_tmux, mock_wait_status, mock_wait_shell, _
    ):
        """Test that initialize calls _handle_startup_prompts."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        trust_output = "❯ 1. Yes, I trust this folder\n  2. No"
        mock_tmux.get_history.side_effect = ["", trust_output, trust_output]
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True
        mock_pane.send_keys.assert_called_with("", enter=True)


class TestClaudeCodeProviderSettings:
    """Tests for Claude Code settings management."""

    @patch("cli_agent_orchestrator.providers.claude_code.Path")
    def test_ensure_skip_bypass_prompt_already_set(self, mock_path_cls):
        """Test no-op when setting is already present."""
        mock_settings_path = MagicMock()
        mock_settings_path.exists.return_value = True
        mock_path_cls.home.return_value.__truediv__ = MagicMock(
            side_effect=lambda _: mock_settings_path
        )
        # Chain .home() / ".claude" / "settings.json"
        mock_home = MagicMock()
        mock_claude_dir = MagicMock()
        mock_path_cls.home.return_value = mock_home
        mock_home.__truediv__ = MagicMock(return_value=mock_claude_dir)
        mock_claude_dir.__truediv__ = MagicMock(return_value=mock_settings_path)

        existing = json.dumps({"skipDangerousModePermissionPrompt": True})
        with patch("builtins.open", mock_open(read_data=existing)):
            ClaudeCodeProvider._ensure_skip_bypass_prompt_setting()

        # Should not write (file handle's write not called)
        mock_settings_path.parent.mkdir.assert_not_called()

    def test_ensure_skip_bypass_prompt_writes_setting(self, tmp_path):
        """Test that setting is written when missing."""
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"permissions": {"allow": []}}))

        with patch("cli_agent_orchestrator.providers.claude_code.Path") as mock_path_cls:
            mock_home = MagicMock()
            mock_path_cls.home.return_value = mock_home
            mock_home.__truediv__ = MagicMock(
                return_value=MagicMock(__truediv__=MagicMock(return_value=settings_file))
            )

            ClaudeCodeProvider._ensure_skip_bypass_prompt_setting()

        result = json.loads(settings_file.read_text())
        assert result["skipDangerousModePermissionPrompt"] is True
        # Original settings preserved
        assert result["permissions"] == {"allow": []}

    def test_ensure_skip_bypass_prompt_creates_file(self, tmp_path):
        """Test that settings file is created when it doesn't exist."""
        settings_file = tmp_path / ".claude" / "settings.json"

        with patch("cli_agent_orchestrator.providers.claude_code.Path") as mock_path_cls:
            mock_home = MagicMock()
            mock_path_cls.home.return_value = mock_home
            mock_home.__truediv__ = MagicMock(
                return_value=MagicMock(__truediv__=MagicMock(return_value=settings_file))
            )

            ClaudeCodeProvider._ensure_skip_bypass_prompt_setting()

        result = json.loads(settings_file.read_text())
        assert result["skipDangerousModePermissionPrompt"] is True
