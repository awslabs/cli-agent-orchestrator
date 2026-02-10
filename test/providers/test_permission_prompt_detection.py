"""Unit tests for permission prompt detection fix.

Tests all cases from real terminal logs (605 logs analyzed).
See: ~/kb/cao/bugs/inbox_delivers_during_permission_prompt.md

Permission cases (P1-P8):
  P1: Empty prompt, unanswered
  P2: Trailing text ("What would you like to do next?"), unanswered
  P3: CAO injection delivered during active prompt
  P4: CAO injection delivered during active prompt (different text)
  P5: User answered y, agent continued
  P6: User typed long response instead of y/n/t
  P7: kiro-cli re-renders [y/n/t]: for each keystroke
  P8: User typing partial text, hasn't pressed enter

Non-permission cases (N1-N9):
  N1: Plain idle
  N2: Idle with trailing text
  N3: Idle with "What would you like to do next?"
  N4: Tool running
  N5: Thinking spinner
  N6: Completed response
  N7: Initializing (MCP loading)
  N8: Exited (back to shell)
  N9: Message received via inbox
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


def make_provider(agent_profile="developer"):
    return KiroCliProvider("test1234", "test-session", "window-0", agent_profile)


class TestPermissionPromptActive:
    """Cases where permission prompt is active — should return WAITING_USER_ANSWER."""

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p1_active_empty_prompt(self, mock_tmux):
        """P1: Permission prompt shown, empty idle prompt on next line, unanswered."""
        mock_tmux.get_history.return_value = load_fixture("kiro_cli_permission_active_empty.txt")
        provider = make_provider("cao-internal-docs-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p2_active_trailing_text(self, mock_tmux):
        """P2: Permission prompt + idle prompt with trailing text, unanswered."""
        mock_tmux.get_history.return_value = load_fixture(
            "kiro_cli_permission_active_trailing_text.txt"
        )
        provider = make_provider("cao-jira-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p3_active_injection_delivered(self, mock_tmux):
        """P3: Permission prompt + CAO injection message delivered during prompt."""
        mock_tmux.get_history.return_value = load_fixture(
            "kiro_cli_permission_active_injection.txt"
        )
        provider = make_provider("cao-code-explorer-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p4_active_different_injection_text(self, mock_tmux):
        """P4: Permission prompt + different injected text on idle prompt."""
        mock_tmux.get_history.return_value = (
            "Allow this action? Use 't' to trust (always allow) this tool "
            "for the session. [y/n/t]:\n\n"
            "[cao-workspace-expert] 22% λ > don't you have the internal search?"
        )
        provider = make_provider("cao-workspace-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p8_active_partial_typing(self, mock_tmux):
        """P8: User typing partial text during permission prompt, no enter."""
        mock_tmux.get_history.return_value = load_fixture(
            "kiro_cli_permission_active_partial_typing.txt"
        )
        provider = make_provider("cao-internal-docs-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p1_active_zero_idle_prompts_after(self, mock_tmux):
        """Permission prompt with no idle prompt after it at all."""
        mock_tmux.get_history.return_value = (
            "Allow this action? Use 't' to trust (always allow) this tool "
            "for the session. [y/n/t]:\n"
        )
        # No idle prompt → PROCESSING (no idle prompt detected at all)
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.PROCESSING


class TestPermissionPromptStale:
    """Cases where permission prompt was answered — should NOT return WAITING_USER_ANSWER."""

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p5_answered_y_agent_idle(self, mock_tmux):
        """P5: User answered y, agent ran tool, now idle again."""
        mock_tmux.get_history.return_value = load_fixture("kiro_cli_permission_stale_answered.txt")
        provider = make_provider("cao-workspace-expert")
        status = provider.get_status()
        assert status != TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p6_long_response_instead_of_ynt(self, mock_tmux):
        """P6: User typed long response instead of y/n/t, agent continued."""
        mock_tmux.get_history.return_value = load_fixture(
            "kiro_cli_permission_stale_long_response.txt"
        )
        provider = make_provider("cao-query-decomposer-supervisor")
        status = provider.get_status()
        assert status != TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_p7_rerendered_prompts_then_answered(self, mock_tmux):
        """P7: Multiple [y/n/t]: re-renders during typing, then answered."""
        mock_tmux.get_history.return_value = (
            "Allow this action? [y/n/t]:\n\n"
            "[developer] 16% λ > \n"
            "Allow this action? [y/n/t]:\n\n"
            "[developer] 16% λ > \n"
            "Allow this action? [y/n/t]:\n\n"
            "[developer] 16% λ > y\n\n"
            " - Completed in 0.3s\n\n"
            "> Done!\n\n"
            "[developer] 18% λ > "
        )
        provider = make_provider("developer")
        status = provider.get_status()
        assert status != TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_stale_single_prompt_answered(self, mock_tmux):
        """Single permission prompt answered, 2 idle prompts after."""
        mock_tmux.get_history.return_value = (
            "Allow this action? [y/n/t]:\n\n"
            "[developer] 10% λ > y\n\n"
            " - Completed in 1.5s\n\n"
            "> Response here\n\n"
            "[developer] 12% λ > "
        )
        provider = make_provider("developer")
        status = provider.get_status()
        assert status != TerminalStatus.WAITING_USER_ANSWER


class TestNonPermissionCases:
    """Cases without permission prompts — existing detection should work."""

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_n1_plain_idle(self, mock_tmux):
        """N1: Plain idle, no permission prompt."""
        mock_tmux.get_history.return_value = "[developer] 22% λ > "
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_n2_idle_trailing_text(self, mock_tmux):
        """N2: Idle with trailing text after prompt."""
        mock_tmux.get_history.return_value = "[developer] 24% λ > send message back to supervisor?"
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_n3_idle_what_would_you_like(self, mock_tmux):
        """N3: Idle with 'What would you like to do next?' trailing text."""
        mock_tmux.get_history.return_value = "[developer] 11% > What would you like to do next?"
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_n4_running_tool(self, mock_tmux):
        """N4: Tool is executing, no idle prompt."""
        mock_tmux.get_history.return_value = (
            "Searching for: system-privileges (*.toml) (using tool: grep)"
        )
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_n6_completed_response(self, mock_tmux):
        """N6: Agent completed response, prompt shown after green arrow."""
        mock_tmux.get_history.return_value = (
            "[developer] 20% λ > user question\n"
            "> Complete response here\n"
            "[developer] 22% λ > "
        )
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_n9_message_received(self, mock_tmux):
        """N9: Inbox message delivered, agent idle."""
        mock_tmux.get_history.return_value = (
            "[developer] 12% > [Message from terminal 9445aa60] " "Hello from supervisor"
        )
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_empty_output(self, mock_tmux):
        """Empty output returns ERROR."""
        mock_tmux.get_history.return_value = ""
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.ERROR


class TestPermissionPromptEdgeCases:
    """Edge cases for permission prompt detection."""

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_permission_same_line_as_idle(self, mock_tmux):
        """Original fixture format: [y/n/t]: and idle prompt on same line."""
        mock_tmux.get_history.return_value = "Allow this action? [y/n/t]: [developer] 10% λ > "
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_multiple_active_prompts_last_unanswered(self, mock_tmux):
        """Multiple permission prompts, last one unanswered."""
        mock_tmux.get_history.return_value = (
            "Allow this action? [y/n/t]:\n\n"
            "[developer] 10% λ > y\n\n"
            " - Completed in 1s\n\n"
            "> Running next tool\n"
            "Allow this action? [y/n/t]:\n\n"
            "[developer] 12% λ > "
        )
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_permission_with_ansi_codes(self, mock_tmux):
        """Permission prompt with ANSI color codes (real terminal output)."""
        mock_tmux.get_history.return_value = (
            "\x1b[38;5;244mAllow this action? Use '\x1b[38;5;13mt\x1b[38;5;244m' "
            "to trust (always allow) this tool for the session. "
            "[\x1b[38;5;13my\x1b[38;5;244m/\x1b[38;5;13mn\x1b[38;5;244m/"
            "\x1b[38;5;13mt\x1b[38;5;244m]:\n\n"
            "\x1b[38;5;6m[developer] \x1b[32m16% \x1b[38;5;39mλ \x1b[38;5;93m> \x1b[0m"
        )
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_no_permission_prompt_in_output(self, mock_tmux):
        """No permission prompt at all — should not affect idle detection."""
        mock_tmux.get_history.return_value = "> Here is my response\n\n" "[developer] 22% λ > "
        provider = make_provider("developer")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_real_ansi_active_trailing_text(self, mock_tmux):
        """Real ANSI output: active prompt with trailing text and \\r redraw.

        From 00ce37f3.log: kiro-cli shows [y/n/t]: then redraws idle prompt
        with "What would you like to do next?" via \\r (carriage return).
        The \\r redraw creates two idle prompt matches on the same line.
        Line-based counting correctly treats this as 1 line = active.
        """
        mock_tmux.get_history.return_value = (
            "\x1b[38;5;244mAllow this action? Use '\x1b[38;5;13mt\x1b[38;5;244m' "
            "to trust (always allow) this tool for the session. "
            "[\x1b[38;5;13my\x1b[38;5;244m/\x1b[38;5;13mn\x1b[38;5;244m/"
            "\x1b[38;5;13mt\x1b[38;5;244m]:\r\n"
            "\r\n"
            "\x1b[0m\x1b[0m\x1b[0m\x1b[?2004h\r\x1b[K"
            "\x1b[38;5;6m[cao-jira-expert] \x1b[0m\x1b[32m16% \x1b[0m"
            "\x1b[38;5;93m> \x1b[0m\x1b[38;5;240mWhat would you like to do next?"
            "\r\x1b[24C\r\x1b[K"
            "\x1b[38;5;6m[cao-jira-expert] \x1b[0m\x1b[32m16% \x1b[0m"
            "\x1b[38;5;93m> \x1b[0m"
        )
        provider = make_provider("cao-jira-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_real_ansi_active_injection(self, mock_tmux):
        """Real ANSI output: active prompt with CAO injection delivered.

        From 0895b67b.log: injection message delivered during permission prompt
        via \\r redraw on same line.
        """
        mock_tmux.get_history.return_value = (
            "\x1b[38;5;244mAllow this action? Use '\x1b[38;5;13mt\x1b[38;5;244m' "
            "to trust (always allow) this tool for the session. "
            "[\x1b[38;5;13my\x1b[38;5;244m/\x1b[38;5;13mn\x1b[38;5;244m/"
            "\x1b[38;5;13mt\x1b[38;5;244m]:\r\n"
            "\r\n"
            "\x1b[0m\x1b[0m\x1b[0m\x1b[?2004h\r\x1b[K"
            "\x1b[38;5;6m[cao-code-explorer-expert] \x1b[0m\x1b[32m15% \x1b[0m"
            "\x1b[38;5;39m\u03bb \x1b[0m\x1b[38;5;93m> \x1b[0m"
            "\x1b[38;5;240mWhat would you like to do next?"
            "\r\x1b[35C\r\x1b[K"
            "\x1b[38;5;6m[cao-code-explorer-expert] \x1b[0m\x1b[32m15% \x1b[0m"
            "\x1b[38;5;39m\u03bb \x1b[0m\x1b[38;5;93m> \x1b[0m"
            "[Assigned by terminal 63878fc7. When done, send results back to "
            "terminal 63878fc7 using send_message]"
        )
        provider = make_provider("cao-code-explorer-expert")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_real_ansi_stale_answered_y(self, mock_tmux):
        """Real ANSI output: permission answered with y, agent continued.

        From 4d9d97cf.log: user typed y via \\r redraw, tool completed,
        new prompt on separate \\n line.
        """
        mock_tmux.get_history.return_value = (
            "\x1b[38;5;244mAllow this action? Use '\x1b[38;5;13mt\x1b[38;5;244m' "
            "to trust (always allow) this tool for the session. "
            "[\x1b[38;5;13my\x1b[38;5;244m/\x1b[38;5;13mn\x1b[38;5;244m/"
            "\x1b[38;5;13mt\x1b[38;5;244m]:\r\n"
            "\r\n"
            "\x1b[0m\x1b[0m\x1b[0m\x1b[?2004h\r\x1b[K"
            "\x1b[38;5;6m[cao-workspace-expert] \x1b[0m\x1b[32m20% \x1b[0m"
            "\x1b[38;5;39m\u03bb \x1b[0m\x1b[38;5;93m> \x1b[0m"
            "\r\x1b[28Cy\x1b[?2004l\r\n"
            "\r\n"
            "Updating: FTVChannelsUI/src/video/GlobalVideoPlayer.tsx\r\n"
            " - Completed in 0.3s\r\n"
            "\r\n"
            "> Done!\r\n"
            "\r\n"
            "\x1b[38;5;6m[cao-workspace-expert] \x1b[0m\x1b[32m22% \x1b[0m"
            "\x1b[38;5;39m\u03bb \x1b[0m\x1b[38;5;93m> \x1b[0m"
        )
        provider = make_provider("cao-workspace-expert")
        status = provider.get_status()
        assert status != TerminalStatus.WAITING_USER_ANSWER
