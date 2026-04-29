"""Unit tests for the OpenCode CLI provider."""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.opencode_cli import (
    ANSI_CODE_PATTERN,
    COMPLETION_MARKER_PATTERN,
    IDLE_FOOTER_PATTERN,
    PERMISSION_PROMPT_PATTERN,
    PROCESSING_FOOTER_PATTERN,
    TOOL_CALL_IN_FLIGHT_PATTERN,
    USER_MESSAGE_PATTERN,
    OpenCodeCliProvider,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Load a plain-text fixture file."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_ansi_fixture(name: str) -> str:
    """Load an ANSI-encoded fixture file."""
    return (FIXTURES_DIR / name).read_bytes().decode("utf-8", errors="replace")


def make_provider(
    agent_profile: str = "developer", model: str | None = None
) -> OpenCodeCliProvider:
    return OpenCodeCliProvider(
        terminal_id="test-tid",
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        model=model,
    )


# ---------------------------------------------------------------------------
# (g) Regex patterns individually match expected substrings
# ---------------------------------------------------------------------------


class TestRegexPatterns:
    def test_user_message_pattern_matches_bar_indent(self):
        assert re.search(USER_MESSAGE_PATTERN, "┃  some user message", re.MULTILINE)

    def test_user_message_pattern_rejects_plain_text(self):
        assert not re.search(USER_MESSAGE_PATTERN, "  some agent message", re.MULTILINE)

    def test_completion_marker_pattern_matches_full_marker(self):
        assert re.search(COMPLETION_MARKER_PATTERN, "▣  Build · Big Pickle · 7.2s")

    def test_completion_marker_pattern_matches_minute_second_duration(self):
        # Responses > 60 s are formatted as "Nm Ns" by OpenCode.
        assert re.search(COMPLETION_MARKER_PATTERN, "▣  Data_analyst · Big Pickle · 1m 8s")
        assert re.search(COMPLETION_MARKER_PATTERN, "▣  Data_analyst · Big Pickle · 2m 30.5s")

    def test_completion_marker_pattern_rejects_incomplete(self):
        # No duration suffix → not a full completion marker
        assert not re.search(COMPLETION_MARKER_PATTERN, "▣  Build · Big Pickle")

    def test_processing_footer_pattern_matches_esc_interrupt(self):
        assert re.search(PROCESSING_FOOTER_PATTERN, "⬝⬝⬝⬝  esc interrupt   ctrl+p commands")

    def test_idle_footer_pattern_matches_ctrl_p(self):
        assert re.search(IDLE_FOOTER_PATTERN, "tab agents  ctrl+p commands  • OpenCode")

    def test_permission_prompt_matches_permission_required(self):
        assert re.search(PERMISSION_PROMPT_PATTERN, "△  Permission required")

    def test_permission_prompt_matches_always_allow(self):
        assert re.search(PERMISSION_PROMPT_PATTERN, "△  Always allow")

    def test_tool_call_in_flight_matches_braille_spinner(self):
        assert re.search(TOOL_CALL_IN_FLIGHT_PATTERN, "  ⠋ Read /etc/hostname", re.MULTILINE)

    def test_ansi_code_pattern_strips_truecolor(self):
        # 24-bit truecolor sequence — must be stripped
        seq = "\x1b[38;2;255;100;50mHello\x1b[0m"
        clean = re.sub(ANSI_CODE_PATTERN, "", seq)
        assert clean == "Hello"


# ---------------------------------------------------------------------------
# (a) + (b)  get_status() per fixture
# ---------------------------------------------------------------------------


class TestGetStatusFromFixtures:
    """Verify get_status() returns the correct enum for each Phase 1 fixture."""

    def _mock_provider(self, fixture_content: str) -> OpenCodeCliProvider:
        provider = make_provider()
        mock_tmux = MagicMock()
        mock_tmux.get_history.return_value = fixture_content
        provider._tmux_client = mock_tmux  # not used directly — patch below
        return provider

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_idle_splash_returns_idle(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("opencode_cli_idle_splash.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_idle_splash_ansi_returns_idle(self, mock_tmux):
        mock_tmux.get_history.return_value = load_ansi_fixture("opencode_cli_idle_splash.ansi.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_processing_returns_processing(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("opencode_cli_processing.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_processing_ansi_returns_processing(self, mock_tmux):
        mock_tmux.get_history.return_value = load_ansi_fixture("opencode_cli_processing.ansi.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_completed_returns_completed(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("opencode_cli_completed.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_completed_ansi_returns_completed(self, mock_tmux):
        mock_tmux.get_history.return_value = load_ansi_fixture("opencode_cli_completed.ansi.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_permission_ansi_returns_waiting_user_answer(self, mock_tmux):
        # ANSI fixture required: plain text loses the △ Permission required overlay.
        mock_tmux.get_history.return_value = load_ansi_fixture("opencode_cli_permission.ansi.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_idle_post_completion_returns_idle(self, mock_tmux):
        # Use plain fixture — ANSI variant reuses the completed frame (see OPENCODE_FIXTURES.md).
        mock_tmux.get_history.return_value = load_fixture("opencode_cli_idle_post_completion.txt")
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_empty_output_returns_error(self, mock_tmux):
        mock_tmux.get_history.return_value = ""
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_none_output_returns_error(self, mock_tmux):
        mock_tmux.get_history.return_value = None
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_unknown_output_returns_error_fallback(self, mock_tmux):
        """Non-empty output with no recognizable pattern → ERROR fallback.

        Exercises the final ``return TerminalStatus.ERROR`` at the end of
        get_status(), distinct from the early return on empty/None output.
        """
        mock_tmux.get_history.return_value = "random text without any tui markers"
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.ERROR


# ---------------------------------------------------------------------------
# (c)  Stale ``esc interrupt`` + idle footer on later line → IDLE
# ---------------------------------------------------------------------------


class TestStaleEscInterruptGuard:
    """Verify position-aware guard: esc interrupt on earlier line + idle footer on later line → IDLE."""

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_stale_esc_interrupt_returns_idle(self, mock_tmux):
        # Synthetic: esc interrupt on line 0 (old frame), ctrl+p commands on line 2 (new frame).
        stale_output = (
            "   ⬝⬝⬝⬝  esc interrupt\n"
            "\n"
            "                  ctrl+p commands    • OpenCode 1.14.19"
        )
        mock_tmux.get_history.return_value = stale_output
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_same_line_esc_and_idle_returns_processing(self, mock_tmux):
        # esc interrupt and ctrl+p commands on the same footer line → PROCESSING (normal case).
        same_line_output = (
            "   some content here\n"
            "   ⬝⬝⬝⬝  esc interrupt                     tab agents  ctrl+p commands    • OpenCode"
        )
        mock_tmux.get_history.return_value = same_line_output
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_stale_esc_followed_by_full_completion_returns_completed(self, mock_tmux):
        # Stale esc interrupt line, then full completion marker + idle footer → COMPLETED.
        stale_to_completed = (
            "   ⬝⬝⬝⬝  esc interrupt\n"
            "\n"
            "   Hello there, friend!\n"
            "\n"
            "   ▣  Build · Big Pickle · 7.2s\n"
            "\n"
            "                  ctrl+p commands    • OpenCode 1.14.19"
        )
        mock_tmux.get_history.return_value = stale_to_completed
        provider = make_provider()
        assert provider.get_status() == TerminalStatus.COMPLETED


# ---------------------------------------------------------------------------
# (d)  extract_last_message_from_script()
# ---------------------------------------------------------------------------


class TestExtractLastMessage:
    """Verify message extraction from the completed fixture and synthetic output."""

    def test_extracts_response_from_completed_fixture(self):
        provider = make_provider()
        output = load_fixture("opencode_cli_completed.txt")
        result = provider.extract_last_message_from_script(output)
        assert "Hello" in result

    def test_strips_thinking_preamble(self):
        provider = make_provider()
        # Synthetic: Thinking: block before the actual response
        output = (
            "┃  say hello\n"
            "┃\n"
            "\n"
            "┃  Thinking: The user wants a greeting. Simple.\n"
            "\n"
            "     Hi there!\n"
            "\n"
            "     ▣  Build · Big Pickle · 3.1s\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Thinking:" not in result
        assert "Hi there!" in result

    def test_raises_if_no_completion_marker(self):
        provider = make_provider()
        # No completion marker at all → first thing checked after ANSI strip.
        with pytest.raises(ValueError, match="No completion marker"):
            provider.extract_last_message_from_script("some random output without any markers")

    def test_raises_if_no_user_message_before_completion(self):
        provider = make_provider()
        # Completion marker present but no ┃  before it.
        output = "     ▣  Build · Big Pickle · 3.1s\n"
        with pytest.raises(ValueError, match="No user message"):
            provider.extract_last_message_from_script(output)

    def test_fallback_extracts_when_user_message_scrolled_off(self):
        provider = make_provider()
        # Simulate the 41-line TUI viewport where the user message has scrolled off the top.
        # Only the tail of the agent's response is visible, plus the completion marker and
        # the input box (┃ lines) below it. No ┃  appears before the completion marker.
        output = (
            "\n"
            "➜  cli-agent-orchestrator\n"  # shell prompt from 2-line tmux scrollback
            "     Response line 1.\n"
            "     Response line 2.\n"
            "\n"
            "     ▣  Developer · Big Pickle · 8s\n"
            "\n"
            "  ┃\n"
            "  ┃  Developer · Big Pickle OpenCode Zen\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Response line 1." in result
        assert "Response line 2." in result
        # Shell prompt must not leak into extracted content
        assert "cli-agent-orchestrator" not in result

    def test_strips_ansi_before_extraction(self):
        provider = make_provider()
        ansi_output = load_ansi_fixture("opencode_cli_completed.ansi.txt")
        result = provider.extract_last_message_from_script(ansi_output)
        # Should not contain raw ANSI sequences
        assert "\x1b[" not in result
        assert result.strip()

    def test_extract_skips_residual_bar_lines(self):
        """Residual ┃ lines inside raw_response are stripped by the agent-block loop.

        Constructs a raw_response slice whose first ``┃\\s{2}`` match anchors at the
        top but contains a bare ┃ line (followed by non-whitespace so the regex
        doesn't consume it) and a trailing blank line before the agent response.
        Exercises both the ``^\\s*┃`` branch and the ``not past_user_block and not
        line.strip()`` branch of the extraction loop.
        """
        provider = make_provider()
        output = (
            "┃  user msg\n"
            "  ┃Xstray-residual\n"  # residual ┃ line; does not match ┃\\s{2}
            "\n"  # blank after ┃-strip → past_user_block=False branch
            "     agent reply here\n"
            "\n"
            "     ▣  Build · Big Pickle · 3.1s\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "agent reply here" in result
        assert "stray-residual" not in result

    def test_extract_raises_when_response_is_empty(self):
        """Empty content between user bar and ▣ → ValueError.

        Exercises the ``if not result:`` guard after dedent/strip.
        """
        provider = make_provider()
        # User bar with nothing after it before the ▣ marker.
        output = "┃  \n     ▣  Build · Big Pickle · 3.1s\n"
        with pytest.raises(ValueError, match="Empty OpenCode response"):
            provider.extract_last_message_from_script(output)


# ---------------------------------------------------------------------------
# (e)  initialize() calls wait_until_status with timeout=120.0
# ---------------------------------------------------------------------------


class TestInitialize:
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_success_returns_true(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        assert provider.initialize() is True

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_uses_120s_timeout(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        provider.initialize()
        _args, kwargs = mock_wait.call_args
        assert kwargs.get("timeout") == 120.0 or _args[2] == 120.0

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_sends_agent_flag(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(agent_profile="developer")
        provider.initialize()
        sent_cmd = mock_tmux.send_keys.call_args[0][2]
        assert "--agent developer" in sent_cmd

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_includes_model_when_set(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(model="anthropic/claude-sonnet-4-6")
        provider.initialize()
        sent_cmd = mock_tmux.send_keys.call_args[0][2]
        assert "--model anthropic/claude-sonnet-4-6" in sent_cmd

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_no_model_flag_when_unset(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(model=None)
        provider.initialize()
        sent_cmd = mock_tmux.send_keys.call_args[0][2]
        assert "--model" not in sent_cmd

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_raises_on_shell_timeout(self, mock_tmux, mock_shell):
        mock_shell.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_raises_on_opencode_timeout(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="OpenCode CLI initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.tmux_client")
    def test_initialize_sets_env_vars_in_command(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        provider.initialize()
        sent_cmd = mock_tmux.send_keys.call_args[0][2]
        # Platform-agnostic: POSIX uses KEY=VAL prefix; PowerShell uses $env:KEY = 'VAL'
        assert "OPENCODE_DISABLE_AUTOUPDATE" in sent_cmd
        assert "OPENCODE_DISABLE_MOUSE" in sent_cmd
        assert "OPENCODE_CLIENT" in sent_cmd
        assert "xterm-256color" in sent_cmd
        assert "OPENCODE_CONFIG" in sent_cmd
        assert "OPENCODE_CONFIG_DIR" in sent_cmd


# ---------------------------------------------------------------------------
# (f)  exit_cli() returns "/exit"
# ---------------------------------------------------------------------------


class TestExitCli:
    def test_exit_cli_returns_slash_exit(self):
        assert make_provider().exit_cli() == "/exit"


# ---------------------------------------------------------------------------
# Miscellaneous interface methods
# ---------------------------------------------------------------------------


class TestMiscInterface:
    def test_cleanup_sets_initialized_false(self):
        provider = make_provider()
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_get_idle_pattern_for_log_returns_ctrl_p_pattern(self):
        pattern = make_provider().get_idle_pattern_for_log()
        assert re.search(pattern, "tab agents  ctrl+p commands  • OpenCode")

    def test_paste_enter_count_is_one(self):
        assert make_provider().paste_enter_count == 1

    def test_extraction_tail_lines_is_2000(self):
        """extraction_tail_lines must be large enough for long-response agents."""
        assert make_provider().extraction_tail_lines == 2000


# ---------------------------------------------------------------------------
# Provider manager registration
# ---------------------------------------------------------------------------


class TestProviderManagerRegistration:
    """ProviderManager.create_provider('opencode_cli', …) returns an OpenCodeCliProvider."""

    def test_create_provider_returns_opencode_instance(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        pm = ProviderManager()
        provider = pm.create_provider(
            provider_type="opencode_cli",
            terminal_id="oc-test-tid",
            tmux_session="test-session",
            tmux_window="window-0",
            agent_profile="developer",
        )
        assert isinstance(provider, OpenCodeCliProvider)

    def test_unknown_provider_type_raises(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        pm = ProviderManager()
        with pytest.raises(ValueError, match="Unknown provider type"):
            pm.create_provider(
                provider_type="nonexistent_provider",
                terminal_id="tid",
                tmux_session="s",
                tmux_window="w",
            )


# ---------------------------------------------------------------------------
# launch.py PROVIDERS_REQUIRING_WORKSPACE_ACCESS
# ---------------------------------------------------------------------------


class TestWorkspaceAccess:
    def test_opencode_cli_in_workspace_access_set(self):
        from cli_agent_orchestrator.cli.commands.launch import (
            PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
        )

        assert "opencode_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS
