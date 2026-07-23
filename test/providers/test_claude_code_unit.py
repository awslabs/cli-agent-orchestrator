"""Unit tests for Claude Code provider."""

import json
import shlex
import time
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import (
    AgentProfile,
    ContainerConfig,
    ContainerPathMap,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider, ProviderError


@pytest.fixture(autouse=True)
def cleanup_tmp_files():
    """Remove temp prompt/mcp files created by _build_claude_command during tests."""
    yield
    tmp_dir = Path.home() / ".aws" / "cli-agent-orchestrator" / "tmp"
    if tmp_dir.exists():
        for f in tmp_dir.glob("test*.prompt"):
            f.unlink(missing_ok=True)
        for f in tmp_dir.glob("test*.mcp.json"):
            f.unlink(missing_ok=True)
        for f in tmp_dir.glob("term-*.prompt"):
            f.unlink(missing_ok=True)
        for f in tmp_dir.glob("term-*.mcp.json"):
            f.unlink(missing_ok=True)


# All initialization tests need to patch _ensure_skip_bypass_prompt_setting
# to avoid writing to the real ~/.claude/settings.json.
_PATCH_SETTINGS = patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")


def _extract_mcp_config(command: str) -> dict:
    args = shlex.split(command)
    assert "--strict-mcp-config" in args
    mcp_file = Path(args[args.index("--mcp-config") + 1])
    try:
        return json.loads(mcp_file.read_text())
    finally:
        mcp_file.unlink(missing_ok=True)


class TestClaudeCodeProviderInitialization:
    """Tests for ClaudeCodeProvider initialization."""

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
        mock_profile.provider_init_timeout = None
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
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
        assert "--append-system-prompt-file" not in command
        assert "--mcp-config" not in command

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
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
        mock_profile.provider_init_timeout = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("test123", "test-session", "window-0", "test-agent")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True

    @pytest.mark.asyncio
    @_PATCH_SETTINGS
    @patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
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

    def test_get_status_empty_buffer_returns_unknown(self):
        """Empty buffer -> UNKNOWN (native=None always falls through to buffer analysis).

        The backend's get_native_status() returns None (tmux always; herdr
        'unknown'); this always falls through to buffer analysis -- no
        dispatch-timing guess. On tmux, BaseProvider._resolve_buffer() is a
        pass-through, so the empty buffer hits Claude Code's own no-output
        default (UNKNOWN) directly.
        """
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

    def test_get_status_boxless_completion_after_stale_spinner(self):
        """COMPLETED when the finished turn is repainted BOXLESS below a stale
        spinner + separator.

        The newest TUI sometimes leaves the prior frame's spinner ("· …ing…") and
        its box separator in the buffer, then paints "✻ <Verb>ed for Ns" + ❯
        afterwards with no fresh separators. The spinner-before-separator walk must
        NOT report PROCESSING off the stale spinner — the completion summary after
        the last separator is the freshest state. (Real handoff capture otherwise
        stuck at PROCESSING until timeout.)
        """
        box = "─" * 30
        output = (
            "· Whatchamacalliting… (1s · ↓ 13 tokens)\n❯ \n"
            + box
            + "\n✻ Cogitated for 1s\n❯ \n← for agents\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_completed_via_response_when_summary_clipped(self):
        """COMPLETED via the ● response marker when the completion summary is
        clipped to "✻ Crunched for " (no duration) by the redraw.

        The newest TUI sometimes writes the summary's duration with a separate
        cursor-positioned write that the raw stream splits off, leaving
        "✻ Crunched for " (no "Ns") which COMPLETION_SUMMARY_PATTERN can't match.
        A start-of-line ● response above the prompt is the robust completion
        signal (real handoff capture otherwise stuck at IDLE until timeout).
        """
        box = "─" * 30
        output = (
            "● def multiply(a, b):\n    return a * b\n"
            "· Multiplying…\n" + box + "\n❯ \n" + box + "\n"
            "  ⏵⏵ bypass permissions on · esc to interrupt ● high · /effort\n"
            "✻ Crunched for \n❯ \n ← for agents\n You've used 94% of your session limit\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_clipped_completion_after_separator_beats_stale_spinner(self):
        """COMPLETED when a CLIPPED completion ("✻ Crunched for ") is repainted
        boxless after the last separator, above a stale spinner.

        The spinner-before-separator walk would otherwise report PROCESSING off the
        stale "✽ Deciphering…"; the clipped completion after the last separator is
        the boxless-redraw signature and must take precedence. This is the
        intermittent real-handoff failure (the settle frame randomly landed here).
        """
        box = "─" * 40
        output = (
            "● def greet(name):\n"
            "✽ Deciphering… (2s · ↓ 57 tokens)\n\n❯ \n\n" + box + "\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt ● high\n"
            "✻ Crunched for \n❯ \n ← for agents\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_own_line_effort_footer_only_is_idle(self):
        """GH #459: on Claude Code v2.1.212 the effort footer can render on its
        OWN line at column 0 ("● high · /effort"), not just mid-line after
        "esc to interrupt". A fresh terminal whose only "●" content is this
        footer must read IDLE, not COMPLETED — there is no response yet.
        """
        output = "● high · /effort\n❯ \n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_post_paste_stale_own_line_effort_footer_not_completed(self):
        """GH #459 exact premature-exit trigger: a task was just pasted (echoed
        by the ❯ line), the worker has not produced a spinner or response yet,
        and the only "●" content is a stale own-line effort footer. This must
        NOT read COMPLETED — a false COMPLETED here is what drove `handoff` to
        paste `/exit` into a still-running worker.
        """
        box = "─" * 24
        output = (
            "❯ Delegate to developer: create fizzbuzz.py\n"
            "● high · /effort\n" + box + "\n❯ \n" + box + "\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) != TerminalStatus.COMPLETED

    def test_get_status_live_spinner_above_own_line_effort_footer_is_processing(self):
        """GH #459 box-walk: a live spinner renders above the input box with an
        own-line effort footer sitting BETWEEN the spinner and the box's top
        border. The box-walk must skip the footer line (like it already skips
        blank lines and "⎿" hints) to still see the spinner → PROCESSING.
        """
        box = "─" * 30
        output = "✢ Cultivating…\n● high · /effort\n" + box + "\n\n❯ \n\n" + box + "\n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_own_line_effort_footer_medium_level_is_idle(self):
        """The effort footer's level varies by setting ("medium", "low", etc.),
        not just "high" — the exclusion must not be hardcoded to one level."""
        output = "● medium · /effort\n❯ \n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE


class TestClaudeCodeDialogDetection:
    """Tests for dialog detection anchoring and plan-approval (issue #405)."""

    def test_plan_dialog_many_options_detected_as_waiting(self):
        """Plan dialog with ~9 options (scrolled beyond old 10-line window) → WAITING."""
        output = (
            "⏺ I've analyzed the codebase and prepared a plan.\n"
            "Would you like to proceed?\n"
            "❯ 1. Yes, and bypass permissions\n"
            "  2. Yes, manually approve edits\n"
            "  3. No, refine with Ultraplan\n"
            "  4. Tell Claude what to change\n"
            "  5. Save plan and exit\n"
            "  6. Show plan details\n"
            "  7. Edit plan in editor\n"
            "  8. Run with different model\n"
            "  9. Run with constraints\n"
            "     shift+tab to approve with this feedback\n"
            "ctrl+g to edit in  Nvim  · ~/.claude/plans/my-plan.md"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_plan_dialog_dismissed_with_response_marker_not_waiting(self):
        """Dismissed plan dialog + response marker (⏺) after options → COMPLETED."""
        output = (
            "Would you like to proceed?\n"
            "❯ 1. Yes, and bypass permissions\n"
            "  2. Yes, manually approve edits\n"
            "⏺ Done implementing the feature.\n"
            "❯ "
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert status == TerminalStatus.COMPLETED

    def test_plan_dialog_dismissed_with_new_tui_marker_not_waiting(self):
        """Dismissed plan dialog + newest-TUI response marker (●) → not WAITING.

        The newest TUI renders responses with ● (U+25CF) instead of ⏺; the
        dismissal guard must accept it as dismissal evidence or the terminal
        stays falsely WAITING and inbox delivery stalls.
        """
        output = (
            "Would you like to proceed?\n"
            "❯ 1. Yes, and bypass permissions\n"
            "  2. Yes, manually approve edits\n"
            "● Done implementing the feature.\n"
            "❯ "
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status != TerminalStatus.WAITING_USER_ANSWER

    def test_plan_dialog_effort_footer_is_not_dismissal_evidence(self):
        """A '● high · /effort' footer below a LIVE plan dialog must not count
        as a response marker — the dialog is still open → WAITING."""
        output = (
            "Would you like to proceed?\n"
            "❯ 1. Yes, and bypass permissions\n"
            "  2. Yes, manually approve edits\n"
            "  3. No, tell Claude what to change\n"
            "● high · /effort"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_plan_dialog_dismissed_with_separator_not_waiting(self):
        """Dismissed plan dialog + separator after options → COMPLETED."""
        output = (
            "Would you like to proceed?\n"
            "❯ 1. Yes, and bypass permissions\n"
            "  2. Yes, manually approve edits\n"
            "⏺ Proceeding with option 1\n"
            "⏺ Done implementing the changes.\n"
            "────────────────────────\n"
            "❯ Ask a question or describe a task"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert status == TerminalStatus.COMPLETED

    def test_nav_footer_in_scrollback_with_idle_at_bottom_not_waiting(self):
        """'↑/↓ to navigate' in scrollback but NOT in bottom 6 lines → not WAITING."""
        scrollback_lines = ["line %d of output" % i for i in range(25)]
        scrollback_lines[5] = "Enter to select · ↑/↓ to navigate · Esc to cancel"
        scrollback_lines.append("⏺ Here is the result")
        scrollback_lines.append("────────────────────────")
        scrollback_lines.append("❯ ")
        output = "\n".join(scrollback_lines)

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert status == TerminalStatus.COMPLETED

    def test_ask_user_question_with_notes_hint_and_error_banner(self):
        """AskUserQuestion footer pushed down by notes-hint + error → still WAITING."""
        output = (
            "❯ 1. Option one\n"
            "  2. Option two\n"
            "  3. Option three\n"
            "n to add notes\n"
            "⚠ Please select a valid option\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_plan_approval_active_with_option_markers_is_waiting(self):
        """Active plan-approval dialog (option markers present) → WAITING."""
        output = (
            "Claude has a plan. Would you like to proceed?\n"
            "❯ 1. Yes, and bypass permissions\n"
            "  2. Yes, manually approve edits\n"
            "  3. No, refine\n"
            "  4. Tell Claude what to change\n"
            "     shift+tab to approve with feedback"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_plan_text_far_in_scrollback_no_option_markers_in_bottom(self):
        """Plan text far in scrollback, no option markers in bottom → not WAITING."""
        lines = ["line %d" % i for i in range(20)]
        lines[2] = "Would you like to proceed?"
        lines[3] = "❯ 1. Yes"
        lines[4] = "  2. No"
        lines.extend(
            [
                "⏺ Completed the work",
                "────────────────────────",
                "❯ ",
            ]
        )
        output = "\n".join(lines)

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert status == TerminalStatus.COMPLETED

    def test_dismissed_plan_response_marker_no_separator(self):
        """Response marker after options WITHOUT separator still dismisses the plan."""
        output = (
            "Would you like to proceed?\n" "  1. Yes\n" "  2. No\n" "⏺ Here is the response\n" "> "
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        assert status == TerminalStatus.COMPLETED

    @pytest.mark.xfail(
        reason="Known limitation: agent prose containing '↑/↓ to navigate' "
        "in the 6-line footer window causes false WAITING. Full fix needs "
        "structural composer detection.",
        strict=True,
    )
    def test_agent_prose_with_nav_text_in_footer_false_waiting(self):
        """KNOWN LIMITATION: agent prose echoing '↑/↓ to navigate' in the
        bottom 6 lines of an idle prompt false-positives as WAITING."""
        output = (
            "⏺ The dialog shows:\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
            "────────────────────────\n"
            "❯ "
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        status = provider.get_status(output)
        # This SHOULD be COMPLETED but will be WAITING due to the known limitation
        assert status == TerminalStatus.COMPLETED


class TestClaudeCodeProviderNativeStatus:
    """Tests for get_status() native-first path (herdr backend)."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_processing_skips_buffer(self, mock_backend):
        """When native returns PROCESSING, get_history is not called."""
        mock_backend.get_native_status.return_value = TerminalStatus.PROCESSING

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.get_status("")

        assert result == TerminalStatus.PROCESSING
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_processing_resets_flush_wait_timers(self, mock_backend):
        """native=PROCESSING must reset flush-wait timestamps stamped during a pre-work idle gap.

        Scenario: send_input() fires, herdr briefly shows idle before transitioning
        to processing. _idle_first_detected gets stamped at T0. Then native=processing
        arrives. If we don't reset, the timer keeps accumulating. When herdr finishes
        and shows idle again 112s later, waited >= 10s already -> COMPLETED fires before
        the buffer is flushed.
        """
        mock_backend.get_native_status.return_value = TerminalStatus.PROCESSING

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._task_dispatched = True
        # Simulate timestamps stamped during the pre-work idle gap
        provider._done_first_detected = time.time() - 5.0
        provider._idle_first_detected = time.time() - 5.0

        provider.get_status("")

        assert provider._done_first_detected == 0.0
        assert provider._idle_first_detected == 0.0

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_waiting_user_answer_skips_buffer(self, mock_backend):
        """When native returns WAITING_USER_ANSWER, get_history is not called."""
        mock_backend.get_native_status.return_value = TerminalStatus.WAITING_USER_ANSWER

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.get_status("")

        assert result == TerminalStatus.WAITING_USER_ANSWER
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_completed_no_task_dispatched_returns_completed(self, mock_backend):
        """Native COMPLETED with no task dispatched returns COMPLETED directly (no buffer read)."""
        mock_backend.get_native_status.return_value = TerminalStatus.COMPLETED

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        # _task_dispatched is False by default
        result = provider.get_status("")

        assert result == TerminalStatus.COMPLETED
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_done_task_dispatched_within_10s_returns_processing(self, mock_backend):
        """Native 'done' + task dispatched: <10s since first detection -> PROCESSING."""
        mock_backend.get_native_status.return_value = TerminalStatus.COMPLETED

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._task_dispatched = True
        provider._last_dispatch_time = time.time()
        # _done_first_detected=0.0: first detection happens now, elapsed < 10s

        result = provider.get_status("")

        assert result == TerminalStatus.PROCESSING
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_done_task_dispatched_after_10s_returns_completed(self, mock_backend):
        """Native 'done' + task dispatched: >=10s since first detection -> COMPLETED."""
        mock_backend.get_native_status.return_value = TerminalStatus.COMPLETED

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._task_dispatched = True
        provider._last_dispatch_time = time.time()
        provider._done_first_detected = time.time() - 11.0

        result = provider.get_status("")

        assert result == TerminalStatus.COMPLETED
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_error_skips_buffer(self, mock_backend):
        """When native returns ERROR (herdr 'unknown'), get_history is not called."""
        mock_backend.get_native_status.return_value = TerminalStatus.ERROR

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.get_status("")

        assert result == TerminalStatus.ERROR
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_idle_no_task_dispatched_returns_idle(self, mock_backend):
        """Native 'idle' with _task_dispatched=False returns IDLE."""
        mock_backend.get_native_status.return_value = TerminalStatus.IDLE

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.get_status("")

        assert result == TerminalStatus.IDLE
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_idle_task_dispatched_within_10s_returns_processing(self, mock_backend):
        """Native idle + task dispatched: <10s since first detection -> PROCESSING."""
        mock_backend.get_native_status.return_value = TerminalStatus.IDLE

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._task_dispatched = True
        provider._last_dispatch_time = time.time()
        # _idle_first_detected=0.0: first detection happens now, elapsed < 10s

        result = provider.get_status("")

        assert result == TerminalStatus.PROCESSING
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_idle_task_dispatched_after_10s_returns_completed(self, mock_backend):
        """Native idle + task dispatched: >=10s since first detection -> COMPLETED."""
        mock_backend.get_native_status.return_value = TerminalStatus.IDLE

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._task_dispatched = True
        provider._last_dispatch_time = time.time()
        provider._idle_first_detected = time.time() - 11.0

        result = provider.get_status("")

        assert result == TerminalStatus.COMPLETED
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_idle_task_dispatched_5min_timeout_returns_completed(self, mock_backend):
        """Native idle + task dispatched: >5 min since dispatch -> COMPLETED (give up)."""
        mock_backend.get_native_status.return_value = TerminalStatus.IDLE

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._task_dispatched = True
        provider._last_dispatch_time = time.time() - 301.0
        provider._idle_first_detected = time.time() - 301.0

        result = provider.get_status("")

        assert result == TerminalStatus.COMPLETED
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_mark_input_received_resets_detection_flags(self, mock_backend):
        """mark_input_received() sets _task_dispatched=True and resets detection flags."""
        mock_backend.get_history.return_value = "❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._done_first_detected = 999.0
        provider._idle_first_detected = 999.0

        provider.mark_input_received()

        assert provider._task_dispatched is True
        assert provider._done_first_detected == 0.0
        assert provider._idle_first_detected == 0.0

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_falls_through_to_buffer(self, mock_backend):
        """When native returns None (tmux backend), buffer analysis runs."""
        mock_backend.get_native_status.return_value = None

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.get_status("❯ ")

        assert result == TerminalStatus.IDLE
        # Buffer path reads the passed-in arg, not get_history (no polling).
        mock_backend.get_history.assert_not_called()


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
        """Test extraction stops at Claude's full-width UI separator (20+ dashes, no box chars)."""
        # Claude's turn separator spans the full terminal width — always 20+ dashes
        output = "⏺ Response content\n" + "─" * 80 + "\nMore content\n> "
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "Response content" in result
        assert "More content" not in result

    def test_extract_message_new_tui_circle_glyph(self):
        """Newest TUI uses '●' (U+25CF) as the response marker instead of '⏺'.

        Extraction must recognize '●', trim the '✻ Worked for Ns' completion stat,
        and NOT mistake the footer's mid-line effort indicator '● high' for a
        response marker.
        """
        output = (
            "❯ Create a greet function\n\n"
            "● def greet(name):\n"
            '    return f"Hello, {name}!"\n\n'
            "✻ Worked for 3s\n\n"
            "────────────────────────────────\n"
            "❯ \n"
            "────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · esc to interrupt ● high · /effort\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "def greet(name):" in result
        assert 'return f"Hello, {name}!"' in result
        # completion stat + footer chrome must not leak
        assert "Worked for 3s" not in result
        assert "esc to interrupt" not in result
        assert "high" not in result

    def test_extract_message_circle_mid_line_not_a_marker(self):
        """A '●' that is NOT at the start of a line (e.g. inside footer chrome with
        no real response) yields no response, not a spurious extraction."""
        output = "  ⏵⏵ bypass permissions on · esc to interrupt ● high · /effort\n❯ \n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        with pytest.raises(ValueError, match="No Claude Code response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_own_line_effort_footer_not_a_marker(self):
        """GH #459: on v2.1.212 the effort footer can render on its OWN line at
        column 0 ("● high · /effort"), where the start-of-line anchor alone no
        longer excludes it. A buffer whose only "●"-prefixed line is this
        footer must still yield no response, not the footer text itself
        mistaken for a message."""
        output = "● high · /effort\n❯ \n"
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        with pytest.raises(ValueError, match="No Claude Code response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_styled_own_line_effort_footer_not_a_marker(self):
        """GH #459 follow-up: real ``tmux capture-pane -e`` output re-renders
        the pane's SGR color state, so the own-line effort footer arrives
        wrapped in ANSI codes with a trailing reset directly after "/effort"
        (e.g. "\\x1b[38;5;246m● high · /effort\\x1b[39m"). That reset must not
        defeat the exclusion lookahead in EXTRACTION_RESPONSE_PATTERN — a real
        answer followed by this styled footer must still extract the answer,
        not the footer's own text."""
        output = (
            "● def greet(name):\n"
            '    return f"Hello, {name}!"\n\n'
            "\x1b[38;5;246m●  high  ·  /effort\x1b[39m\n"
            "❯ \n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "def greet(name):" in result
        assert 'return f"Hello, {name}!"' in result
        assert "effort" not in result
        assert "high" not in result

    def test_extract_message_own_line_effort_footer_not_leaked_into_response(self):
        """GH #459 follow-up: the own-line effort footer can render directly
        below a real response, before the idle prompt or any other stop
        condition. The line-collection loop must treat it as a stop boundary
        like the separator and completion-summary lines — not append its text
        onto the extracted answer as trailing garbage."""
        output = (
            "● def greet(name):\n" '    return f"Hello, {name}!"\n\n' "● high · /effort\n" "❯ \n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "def greet(name):" in result
        assert 'return f"Hello, {name}!"' in result
        assert "effort" not in result
        assert "high" not in result

    def test_extract_message_with_table_not_truncated(self):
        """Extraction must NOT stop at table borders containing ─ runs inside │ box chars."""
        output = (
            "⏺ Here is a table:\n"
            "┌──────────────┬──────────────┐\n"
            "│ Col A        │ Col B        │\n"
            "├──────────────┼──────────────┤\n"
            "│ value 1      │ value 2      │\n"
            "└──────────────┴──────────────┘\n"
            "End of response\n"
            "> "
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)

        assert "Here is a table" in result
        assert "Col A" in result
        assert "value 1" in result
        assert "End of response" in result

    def test_extract_message_bullet_marker(self):
        """● (U+25CF) is accepted as a response marker — newer Claude versions use this."""
        output = "● Here is the bullet response\nthat spans lines\n> "
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)
        assert "Here is the bullet response" in result
        assert "that spans lines" in result

    def test_extract_message_last_of_mixed_markers(self):
        """When both ⏺ and ● appear, the last one wins."""
        output = "⏺ Old response\n> \n● New response\n> "
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(output)
        assert "New response" in result
        assert "Old response" not in result


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
        assert "--append-system-prompt-file" in command

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
        mcp_data = _extract_mcp_config(command)
        server_env = mcp_data["mcpServers"]["cao-mcp-server"]["env"]
        assert server_env["CAO_TERMINAL_ID"] == "term-42"

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_build_command_resolves_bundled_mcp_command(self, mock_load):
        """The bare cao-mcp-server command is resolved to a PATH-independent
        invocation in the written MCP config (wiring guard: a refactor that
        drops the resolve_mcp_server_config call must fail this test)."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
        }
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("term-43", "test-session", "window-0", "test-agent")
        MOD = "cli_agent_orchestrator.utils.mcp_resolution"
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            command = provider._build_claude_command()

        mcp_data = _extract_mcp_config(command)
        assert mcp_data["mcpServers"]["cao-mcp-server"]["command"] == "/venv/bin/cao-mcp-server"

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

        mcp_data = _extract_mcp_config(command)
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

        mcp_data = _extract_mcp_config(command)
        server_env = mcp_data["mcpServers"]["my-server"]["env"]
        # Should keep the user-provided value, NOT overwrite with term-99
        assert server_env["CAO_TERMINAL_ID"] == "user-provided-id"


class TestClaudeCodeProviderContainerPathTranslation:
    """_build_claude_command must translate temp-file paths for container profiles.

    Unit coverage of _translate_path itself lives in test_base_provider.py. These
    tests cover the INTEGRATION point: that _build_claude_command routes the temp
    prompt-file and MCP-config paths through _translate_path so the guest paths —
    not the host paths — reach the emitted --append-system-prompt-file and
    --mcp-config arguments.
    """

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_temp_file_paths_translated_to_guest(self, mock_load, tmp_path):
        """host CAO_HOME_DIR prefix -> guest path in both temp-file CLI args."""
        # Map the (patched) CAO_HOME_DIR host prefix to a container guest path.
        mock_load.return_value = AgentProfile(
            name="test-agent",
            description="d",
            system_prompt="You are a container agent.",
            mcpServers={"test-mcp": {"command": "echo", "args": []}},
            container=ContainerConfig(
                path_maps=[ContainerPathMap(host=str(tmp_path), guest="/app/config")]
            ),
        )

        provider = ClaudeCodeProvider("test-container", "sess", "win", "test-agent")
        with patch("cli_agent_orchestrator.providers.claude_code.CAO_HOME_DIR", tmp_path):
            command = provider._build_claude_command()

        args = shlex.split(command)
        prompt_arg = args[args.index("--append-system-prompt-file") + 1]
        mcp_arg = args[args.index("--mcp-config") + 1]

        # Both args carry the translated guest path, not the host path.
        assert prompt_arg == "/app/config/tmp/test-container.prompt"
        assert mcp_arg == "/app/config/tmp/test-container.mcp.json"
        # The host prefix must not leak into either arg (translation happened).
        assert str(tmp_path) not in prompt_arg
        assert str(tmp_path) not in mcp_arg

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_temp_file_paths_unchanged_without_container(self, mock_load, tmp_path):
        """No container -> paths are the real host paths (translation is a no-op).

        Guards against the assertions above passing for the wrong reason: the
        guest prefix only appears when a container maps it.
        """
        mock_load.return_value = AgentProfile(
            name="test-agent",
            description="d",
            system_prompt="You are a host agent.",
            mcpServers={"test-mcp": {"command": "echo", "args": []}},
        )

        provider = ClaudeCodeProvider("test-host", "sess", "win", "test-agent")
        with patch("cli_agent_orchestrator.providers.claude_code.CAO_HOME_DIR", tmp_path):
            command = provider._build_claude_command()

        args = shlex.split(command)
        prompt_arg = args[args.index("--append-system-prompt-file") + 1]
        mcp_arg = args[args.index("--mcp-config") + 1]

        assert prompt_arg == str(tmp_path / "tmp" / "test-host.prompt")
        assert mcp_arg == str(tmp_path / "tmp" / "test-host.mcp.json")
        assert "/app/config" not in prompt_arg
        assert "/app/config" not in mcp_arg


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
    def test_permission_mode_takes_priority_over_yolo(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = "auto"
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent", allowed_tools=["*"])
        command = provider._build_claude_command()

        assert "--permission-mode auto" in command
        assert "--dangerously-skip-permissions" not in command

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


class TestClaudeCodeProviderYoloRootRegression:
    """Regression tests for yolo + root/non-root --dangerously-skip-permissions logic.

    Ensures that the root-user guard (PR #322) only omits --dangerously-skip-permissions
    when running as root, and does not break normal non-root yolo launches.
    """

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.os")
    def test_yolo_non_root_includes_dangerously_skip_permissions(self, mock_os, mock_load):
        """yolo + no permissionMode + non-root => includes --dangerously-skip-permissions."""
        mock_os.geteuid.return_value = 1000  # non-root
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent", allowed_tools=["*"])
        command = provider._build_claude_command()

        assert "--dangerously-skip-permissions" in command
        assert "--permission-mode" not in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.os")
    def test_yolo_root_omits_dangerously_skip_permissions(self, mock_os, mock_load):
        """yolo + no permissionMode + root => omits --dangerously-skip-permissions."""
        mock_os.geteuid.return_value = 0  # root
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = None
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent", allowed_tools=["*"])
        command = provider._build_claude_command()

        assert "--dangerously-skip-permissions" not in command
        assert "--permission-mode" not in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.claude_code.os")
    def test_yolo_with_permission_mode_uses_permission_mode_flag(self, mock_os, mock_load):
        """yolo + permissionMode => uses --permission-mode <value>, omits --dangerously-skip-permissions."""
        mock_os.geteuid.return_value = 1000  # non-root; permissionMode should still win
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.permissionMode = "auto"
        mock_load.return_value = mock_profile

        provider = ClaudeCodeProvider("tid", "sess", "win", "agent", allowed_tools=["*"])
        command = provider._build_claude_command()

        assert "--permission-mode auto" in command
        assert "--dangerously-skip-permissions" not in command


class TestClaudeCodeProviderStartupPrompts:
    """Tests for Claude Code startup prompt handling (trust + bypass)."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_handle_startup_prompts_detected_and_accepted(self, mock_tmux):
        """Test that trust prompt is detected and auto-accepted."""
        mock_tmux.get_history.return_value = (
            "\x1b[1m❯\x1b[0m 1. Yes, I trust this folder\n  2. No, don't trust\n"
        )

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(idle_gap=2.0)

        mock_tmux.send_special_key.assert_called_once_with("test-session", "window-0", "Enter")

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_handle_startup_prompts_not_needed(self, mock_tmux):
        """Test early return when Claude Code starts without prompts."""
        mock_tmux.get_history.return_value = "Welcome to Claude Code v2.1.0"

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(idle_gap=2.0)

        mock_tmux.send_special_key.assert_not_called()

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings")
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_handle_startup_prompts_timeout(self, mock_tmux, mock_time, mock_settings):
        """Handler gives up gracefully at the outer cap when no prompt ever appears.

        should-fix-3: the idle-gap exit does not apply until a first prompt has
        been handled, so with only "Loading..." ever showing, the loop runs
        until the outer cap (provider_init_timeout=60) rather than the old
        20s idle-gap boundary.
        """
        mock_settings.return_value = {
            "provider_init_timeout": 60,
            "startup_prompt_handler_timeout": 20,
        }
        mock_tmux.get_history.return_value = "Loading..."
        # monotonic() calls: outer_deadline, last_prompt_time, iter-1 now (no
        # prompt handled yet -> idle-gap check skipped, polls "Loading..."),
        # iter-2 now (still no prompt -> idle-gap check skipped), iter-3 now
        # (61s >= 60s outer cap -> return).
        mock_time.monotonic.side_effect = [0.0, 0.0, 0.0, 25.0, 61.0]
        mock_time.sleep = MagicMock()

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(idle_gap=20.0)

        mock_tmux.send_special_key.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_handle_startup_prompts_empty_output_then_detected(self, mock_tmux):
        """Test trust prompt detection after initially empty output."""
        mock_tmux.get_history.side_effect = [
            "",
            "❯ 1. Yes, I trust this folder\n  2. No",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(idle_gap=5.0)

        mock_tmux.send_special_key.assert_called_once_with("test-session", "window-0", "Enter")

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_handle_bypass_prompt_detected_and_accepted(self, mock_tmux):
        """Test that bypass permissions prompt is detected and auto-accepted."""
        # First poll: bypass prompt; second poll: welcome banner (after dismissal)
        mock_tmux.get_history.side_effect = [
            "WARNING: Claude Code running in Bypass Permissions mode\n"
            "❯ 1. No, exit\n  2. Yes, I accept\n",
            "Welcome to Claude Code v2.1.74",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(idle_gap=5.0)

        # Verify Down arrow sent via send_keys and Enter via send_special_key
        mock_tmux.send_keys.assert_called_once()
        mock_tmux.send_special_key.assert_called_once_with("test-session", "window-0", "Enter")

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_handle_bypass_then_trust_prompt(self, mock_tmux):
        """Test that bypass prompt is handled, then trust prompt follows."""
        # Poll 1: bypass prompt; Poll 2: trust prompt (after bypass dismissed)
        mock_tmux.get_history.side_effect = [
            "WARNING: Bypass Permissions mode\n❯ 1. No, exit\n  2. Yes, I accept\n",
            "❯ 1. Yes, I trust this folder\n  2. No",
        ]

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider._handle_startup_prompts(idle_gap=5.0)

        # Bypass: send_keys (Down) + send_special_key (Enter)
        # Trust: send_special_key (Enter) — called twice total
        assert mock_tmux.send_keys.call_count == 1  # Down arrow for bypass
        assert mock_tmux.send_special_key.call_count == 2  # Enter for bypass + Enter for trust

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
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_initialize_calls_handle_startup_prompts(
        self, mock_tmux, mock_wait_status, mock_wait_shell, _
    ):
        """Test that initialize calls _handle_startup_prompts."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        trust_output = "❯ 1. Yes, I trust this folder\n  2. No"
        mock_tmux.get_history.side_effect = ["", trust_output, trust_output]
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            result = await provider.initialize()

        assert result is True
        mock_tmux.send_special_key.assert_called_with("test-session", "window-0", "Enter")


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


class TestClaudeCodeMcpCallNotCompleted:
    """A live MCP call must not read as COMPLETED.

    Real failure (supervisor handoff e2e): the supervisor shows an interim
    completion summary from an earlier thinking phase, then keeps working —
    "● Calling cao-mcp-server… (ctrl+o to expand)" with a live
    "✢ Misting… (33s · ↑ 332 tokens)" spinner and a "⎿ Tip: …" hint line
    between the spinner and the input box. get_status() returned COMPLETED
    mid-call; with the StatusMonitor ready-latch that false COMPLETED is
    pinned until the next input, so the test extracted mid-flight output.
    """

    def test_live_spinner_above_tip_line_is_processing(self):
        """Spinner above a ⎿ Tip line above the input box → PROCESSING."""
        box = "─" * 30
        output = (
            "✻ Pondered for 8s\n"
            "● Calling cao-mcp-server… (ctrl+o to expand)\n"
            "✢ Misting… (33s · ↑ 332 tokens)\n"
            "⎿  Tip: Use /btw to ask a quick side question\n"
            + box
            + "\n❯ \n"
            + box
            + "\n  ⏵⏵ bypass permissions on · esc to interrupt\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_live_spinner_after_interim_summary_in_tail_is_processing(self):
        """A live spinner AFTER an interim summary in the post-separator tail
        keeps the turn PROCESSING (the summary is interim, not final)."""
        box = "─" * 30
        output = (
            "● Working on the report…\n"
            "✢ Misting… (10s · ↑ 12 tokens)\n" + box + "\n✻ Pondered for 8s\n"
            "✢ Churning… (2s · ↑ 4 tokens)\n❯ \n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_summary_without_following_spinner_still_completed(self):
        """No live spinner after the summary → boxless completion still wins."""
        box = "─" * 30
        output = (
            "· Whatchamacalliting… (1s · ↓ 13 tokens)\n❯ \n"
            + box
            + "\n✻ Cogitated for 1s\n❯ \n← for agents\n"
        )
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED


class TestClaudeCodeScreenDetection:
    """Viewport detector (get_status_from_screen) — pyte-composited screens.

    These fixtures replicate the LIVE failures the screen path hit during
    validation, so they are the regression net for the pyte detection mode.
    """

    def _p(self):
        return ClaudeCodeProvider("test123", "test-session", "window-0")

    def test_ready_screen_with_boxed_prompt_is_idle(self):
        screen = [
            "─" * 60,
            '❯ Try "fix typecheck errors"',
            "─" * 60,
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.IDLE

    def test_launch_echo_is_not_idle(self):
        """During launch the echoed command (system prompt contains '> ') must
        NOT read as an idle prompt — observed live as premature IDLE that made
        the first task hit a not-ready agent."""
        screen = [
            "rkram@host:/tmp$ claude --append-system-prompt '...",
            "> `memory_store` and `memory_recall` are CAO tools",
            "- **cao-session-management**: ...",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.UNKNOWN

    def test_launch_echo_with_nearby_separator_is_not_idle(self):
        """A launch-echo frame where a single early-painted ──── rule lands near
        an echoed '> ' system-prompt quote must NOT read as a ready box. Only one
        rail is present; the real box has a rail both above AND below the prompt.
        A one-sided separator adjacency misread this as IDLE, breaking init."""
        screen = [
            "rkram@host:/tmp$ claude --append-system-prompt '...",
            "─" * 40,
            "> `memory_store` and `memory_recall` are CAO tools",
            "- **cao-session-management**: ...",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.UNKNOWN

    def test_live_spinner_is_processing(self):
        screen = [
            "● Working on the task",
            "✻ Cultivating… (12s · ↓ 1.2k tokens)",
            "─" * 60,
            "❯ ",
            "─" * 60,
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.PROCESSING

    def test_response_plus_prompt_is_completed(self):
        screen = [
            "● Done — fib.py created and tests pass.",
            "✻ Crunched for 12s",
            "─" * 60,
            "❯ ",
            "─" * 60,
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.COMPLETED

    def test_response_bullet_with_gerund_is_not_false_spinner(self):
        """A settled COMPLETED turn whose "*"/"·" response bullet ends in a
        gerund + ellipsis must NOT read as a live spinner. The loose
        NEW_TUI_SPINNER_PATTERN (glyph class includes "·"/"*") matches such a
        bullet; the gerund-first NEW_TUI_BOX_SPINNER_PATTERN the detector now
        uses does not. Without the fix this screen latches a false PROCESSING
        that starves InboxService."""
        screen = [
            "● Done — see notes.",
            "* Remember to restart the service after deploying…",
            "─" * 60,
            "❯ ",
            "─" * 60,
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.COMPLETED

    def test_selection_widget_is_waiting_user_answer(self):
        screen = [
            "Do you want to proceed?",
            "  1. Yes",
            "  2. No",
            "  ↑/↓ to navigate · enter to select",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER

    def test_empty_screen_is_unknown(self):
        assert self._p().get_status_from_screen(["", "", ""]) == TerminalStatus.UNKNOWN


class TestClaudeCodeBackgroundTaskNotCompleted:
    """A backgrounded task must not read as COMPLETED (GH #392).

    Real failure (first observed live on the Runs dashboard): a code_supervisor
    launched its own backgrounded Workflow. The TUI printed the turn's text
    response, showed an EMPTY idle ❯ box, and rendered
    "✻ Waiting for 1 dynamic workflow to finish" above it — while the status
    bar read "2/3 agents done". That line has no spinner ellipsis (invisible to
    every PROCESSING check) and even matches the lenient completion pattern
    ("✻ Waiting *for* 1 …"), so the frame read COMPLETED, the ready-latch
    pinned it, and the dashboard showed the run as Done mid-execution.
    """

    BOX = "─" * 30

    def _p(self):
        return ClaudeCodeProvider("test123", "test-session", "window-0")

    def _wait_frame(self) -> str:
        """The exact live-frame shape from the GH #392 report."""
        return (
            "● Workflow(Developer agent builds a todo app; Reviewer verifies)\n"
            "  ⎿  Running in background · /workflows to monitor and save\n"
            "● The build is now running in the background. Here's what's happening:\n"
            "  1. Developer agent is building a single-file todo app\n"
            "  2. Code Reviewer agent then verifies every criterion\n"
            "✻ Waiting for 1 dynamic workflow to finish\n"
            + self.BOX
            + "\n❯ \n"
            + self.BOX
            + "\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )

    def test_background_wait_frame_is_processing(self):
        assert self._p().get_status(self._wait_frame()) == TerminalStatus.PROCESSING

    def test_finished_after_background_wait_is_completed(self):
        """Once the workflow finishes, a fresh response + real completion
        summary render BELOW the (now stale, still-in-rolling-buffer) wait
        line — the wait line must not pin PROCESSING."""
        output = (
            self._wait_frame()
            + "● All 12 acceptance criteria pass — here is the artifact link.\n"
            + "✻ Baked for 7m 48s\n"
            + self.BOX
            + "\n❯ \n"
            + self.BOX
            + "\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        assert self._p().get_status(output) == TerminalStatus.COMPLETED

    def test_wait_line_after_last_separator_is_not_a_completion_summary(self):
        """Boxless-repaint variant: the wait line lands AFTER the last
        separator, where the lenient glyph+"for" completion match would
        otherwise count it as a finished-turn summary."""
        output = (
            "● Kicking the workflow off now.\n"
            + self.BOX
            + "\n❯ \n"
            + self.BOX
            + "\n✻ Waiting for 1 dynamic workflow to finish\n"
        )
        assert self._p().get_status(output) == TerminalStatus.PROCESSING

    def test_screen_background_wait_is_processing(self):
        screen = [
            "● The build is now running in the background.",
            "✻ Waiting for 1 dynamic workflow to finish",
            "─" * 60,
            "❯",
            "─" * 60,
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.PROCESSING

    def test_screen_finished_frame_is_completed(self):
        """The finished repaint no longer shows the wait line — composited
        screens carry only live content, so COMPLETED resumes normally."""
        screen = [
            "● All 12 acceptance criteria pass — artifact link below.",
            "✻ Baked for 7m 48s",
            "─" * 60,
            "❯",
            "─" * 60,
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.COMPLETED


class TestBackgroundWaitReviewHardening:
    """Round-2 review of PR #393: the wait-line match must not over-reach.

    The original pattern's glyph class included the markdown bullets ·/* with
    no line anchor or tail restriction, so a settled response containing
    "* Waiting for review" pinned the terminal at PROCESSING (denial of
    progress via the ready-latch), and the screen path checked the wait line
    BEFORE the permission-prompt footer, masking a security-relevant
    WAITING_USER_ANSWER as "working". Both were confirmed by probe before the
    fix; these tests pin the hardened behavior.
    """

    BOX = "─" * 30

    def _p(self):
        return ClaudeCodeProvider("test123", "test-session", "window-0")

    def test_markdown_bullet_wait_text_is_completed_not_pinned(self):
        """Reviewer probe 1: '* Waiting for review' in a settled response body
        must read COMPLETED — no tail keyword, so the pattern cannot match."""
        output = (
            "● Review checklist posted.\n"
            "* Waiting for review\n"
            "· Waiting for approval\n"
            + self.BOX
            + "\n❯ \n"
            + self.BOX
            + "\n  ⏵⏵ bypass permissions on\n"
        )
        assert self._p().get_status(output) == TerminalStatus.COMPLETED

    def test_screen_markdown_bullet_is_completed(self):
        screen = [
            "● Review checklist posted.",
            "* Waiting for review",
            "─" * 60,
            "❯",
            "─" * 60,
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.COMPLETED

    def test_middle_dot_glyph_wait_frame_is_processing(self):
        """The TUI cycles the glyph through '· ✢ * ✶ ✻ ✽' — a ·-glyph frame of
        the REAL wait line (tail keyword present) must still read PROCESSING,
        or one missed frame would false-COMPLETE and re-latch GH #392."""
        output = (
            "● Kicking the workflow off now.\n"
            "· Waiting for 1 dynamic workflow to finish\n"
            + self.BOX
            + "\n❯ \n"
            + self.BOX
            + "\n  ⏵⏵ bypass permissions on\n"
        )
        assert self._p().get_status(output) == TerminalStatus.PROCESSING

    def test_screen_permission_prompt_wins_over_background_wait(self):
        """Reviewer probe 2: a permission prompt co-rendering with the wait
        line is a security gate — WAITING_USER_ANSWER must win."""
        screen = [
            "✻ Waiting for 1 dynamic workflow to finish",
            "Do you want to allow this tool?",
            "❯ 1. Yes",
            "  2. No, and tell Claude what to do differently",
            "  ↑/↓ to navigate · Enter to select",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER

    def test_wait_text_outside_tail_region_is_completed(self):
        """A wait-shaped line buried >20 lines above the buffer end (old
        response text) is outside the live-region restriction."""
        filler = "\n".join(f"  step {i} done" for i in range(25))
        output = (
            "✻ Waiting for 1 dynamic workflow to finish\n"
            + filler
            + "\n● All finished, results above.\n"
            + self.BOX
            + "\n❯ \n"
            + self.BOX
            + "\n  ⏵⏵ bypass permissions on\n"
        )
        assert self._p().get_status(output) == TerminalStatus.COMPLETED


class TestWaitUntilInputReady:
    """Settle-check gate: 'box rendered' is not 'box accepting input'.

    The gate requires the rendered pane to be stable across two consecutive
    captures AND still showing the input box before the first paste is sent.
    """

    BOX = "─" * 40 + "\n> \n" + "─" * 40

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_ready_when_pane_stable_with_box(self, mock_tmux):
        mock_tmux.get_history.side_effect = [self.BOX, self.BOX]
        provider = ClaudeCodeProvider("t1", "sess", "win")
        assert await provider.wait_until_input_ready(timeout=3.0) is True
        assert mock_tmux.get_history.call_count == 2

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_waits_out_still_painting_pane(self, mock_tmux):
        # Startup content still changing between captures (banner, tips, MCP
        # status lines) — exactly the window where Ink drops keystrokes. The
        # gate must NOT pass until two identical box-bearing captures.
        mock_tmux.get_history.side_effect = [
            "Welcome to Claude Code",
            "Welcome to Claude Code\ntips...",
            self.BOX,
            self.BOX,
        ]
        provider = ClaudeCodeProvider("t2", "sess", "win")
        assert await provider.wait_until_input_ready(timeout=5.0) is True
        assert mock_tmux.get_history.call_count == 4

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_stable_pane_without_box_does_not_pass(self, mock_tmux):
        # A stable pane that never shows the input box (e.g. stuck on an
        # error screen) must time out with False, not report ready.
        mock_tmux.get_history.return_value = "some stable non-box content"
        provider = ClaudeCodeProvider("t3", "sess", "win")
        assert await provider.wait_until_input_ready(timeout=1.2) is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_capture_failure_returns_false_not_raise(self, mock_tmux):
        # Backend hiccups must never fail initialization via the gate.
        mock_tmux.get_history.side_effect = RuntimeError("pane gone")
        provider = ClaudeCodeProvider("t4", "sess", "win")
        assert await provider.wait_until_input_ready(timeout=2.0) is False

    @pytest.mark.asyncio
    async def test_base_provider_default_is_immediate_true(self):
        # Non-TUI providers keep the old behavior: no extra gate.
        from cli_agent_orchestrator.providers.base import BaseProvider

        provider = ClaudeCodeProvider("t5", "sess", "win")
        assert await BaseProvider.wait_until_input_ready(provider) is True
