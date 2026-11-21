"""A visible permission dialog wins over working text drawn above it.

The traces are scrubbed from two live captures of a Kiro 2.x TUI terminal whose "shell requires approval"
dialog was open: ``GET /terminals/{id}/output?mode=full`` and ``tmux capture-pane -p -e`` of the viewport.
CAO reported ``idle`` while the dialog was open. The full buffer held no OSC 7501 report and no idle prompt
below the working text: the agent's own transcript quoted the footer phrase "Kiro is working", and Check 2
of ``get_status`` returned PROCESSING for it before it ever looked at the dialog. The status monitor's
sticky latch then refused PROCESSING over the latched IDLE, so the terminal kept reading idle.

The dialog block below is the capture's viewport, byte for byte except for the horizontal rules, which are
shortened. The transcript and tool line above it are rewritten.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

RULE = "\x1b[38;2;238;238;238m" + "─" * 40 + "\n\x1b[39m"
DIALOG = (
    RULE
    + " shell requires approval\n"
    + " \x1b[1m\x1b[38;2;255;0;255m❯\x1b[0m \x1b[1m\x1b[38;2;255;0;255m"
    + "Yes, single permission             \x1b[0m\n"
    + "   Trust, always allow in this session\n"
    + "   No (Tab to edit)\n"
    + "\x1b[38;2;238;238;238m"
    + "─" * 40
    + "\n\x1b[39m esc \x1b[38;2;128;128;128mto close · \x1b[39m↑↓ \x1b[38;2;128;128;128mto navigate · "
    + "\x1b[39m↵ \x1b[38;2;128;128;128mto select · \x1b[39mTab \x1b[38;2;128;128;128mto edit\x1b[39m"
)
SHELL_LINE = (
    "\x1b[38;2;128;128;128m↓\x1b[39m Shell\x1b[38;2;0;95;255m zsh -c 'sleep 1; ls /tmp/example/'"
    "\x1b[39m\n\n"
)
STEER = "Kiro is working · 12s · Type to steer · Ctrl+S to queue"
QUEUE = "Kiro is working · 12s · Type to queue · Ctrl+S to steer"
IDLE_PROMPT = "›  \x1b[38;2;98;98;98mask a question or describe a task ↵\x1b[39m\n"


def transcript(quote: str) -> str:
    """Agent prose that mentions the footer phrase, as the captured transcript did."""
    return (
        "  Both phrases are drawn while a turn runs. The line\n"
        f'  "{quote}" can still be visible\n'
        "  above an approval dialog, so working wins.\n\n"
    )


def footer_line(text: str) -> str:
    return f"\x1b[39m› \x1b[39m\x1b[7m\x1b[27m \x1b[7m\x1b[0m\x1b[38;2;98;98;98m{text}\x1b[39m\n"


def provider() -> KiroCliProvider:
    return KiroCliProvider("dialog0", "cao-dialog", "window-0", "supervisor")


@patch("cli_agent_orchestrator.providers.kiro_cli.get_backend")
class TestDialogWinsOverWorkingText:
    @pytest.mark.parametrize("quote", ["Kiro is working", "Thinking..."])
    def test_quoted_working_text_above_the_dialog_is_waiting(self, mock_backend, quote):
        """The captured shape: no report, no idle prompt, working phrase in the transcript above."""
        output = transcript(quote) + SHELL_LINE + DIALOG
        assert provider().get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.parametrize("footer", [STEER, QUEUE], ids=["steer", "queue"])
    def test_working_footer_above_the_dialog_is_waiting(self, mock_backend, footer):
        output = footer_line(footer) + "\n" + SHELL_LINE + DIALOG
        assert provider().get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.parametrize("footer", [STEER, QUEUE], ids=["steer", "queue"])
    def test_spinner_and_footer_above_the_dialog_is_waiting(self, mock_backend, footer):
        spinner = "\x1b[38;2;128;128;128m⠋\x1b[39m Thinking... (esc to cancel)\n"
        output = spinner + footer_line(footer) + SHELL_LINE + DIALOG
        assert provider().get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_dialog_on_a_running_terminal(self, mock_backend):
        mock_backend.return_value.get_pane_current_command.return_value = "kiro-cli"
        p = provider()
        p.shell_baseline = "zsh"
        p._initialized = True
        output = transcript("Kiro is working") + SHELL_LINE + DIALOG
        assert p.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_viewport_alone_is_waiting(self, mock_backend):
        assert provider().get_status(SHELL_LINE + DIALOG) == TerminalStatus.WAITING_USER_ANSWER

    def test_later_unblocked_report_outranks_the_dialog_text(self, mock_backend):
        """Kiro's own report after the dialog says the prompt ended: the dialog text is stale."""
        output = (
            SHELL_LINE + DIALOG + "\n\x1b]7501;state=working:app=kiro-cli\x07" + footer_line(STEER)
        )
        assert provider().get_status(output) == TerminalStatus.PROCESSING


@patch("cli_agent_orchestrator.providers.kiro_cli.get_backend")
class TestAnsweredDialogStillReleases:
    @pytest.mark.parametrize("footer", [STEER, QUEUE], ids=["steer", "queue"])
    def test_working_footer_after_the_dialog_is_processing(self, mock_backend, footer):
        output = transcript("Kiro is working") + SHELL_LINE + DIALOG + "\n" + footer_line(footer)
        assert provider().get_status(output) == TerminalStatus.PROCESSING

    def test_spinner_after_the_dialog_is_processing(self, mock_backend):
        spinner = "\x1b[38;2;128;128;128m⠋\x1b[39m Thinking... (esc to cancel)\n"
        output = footer_line(QUEUE) + SHELL_LINE + DIALOG + "\n" + spinner
        assert provider().get_status(output) == TerminalStatus.PROCESSING

    def test_dialog_scrolled_out_of_the_bottom_lines_is_processing(self, mock_backend):
        """Dialog text far above the live screen is scrollback, not an open dialog."""
        output = SHELL_LINE + DIALOG + "\n" + "agent output line\n" * 30 + footer_line(QUEUE)
        assert provider().get_status(output) == TerminalStatus.PROCESSING

    def test_idle_prompt_after_working_text_is_unchanged(self, mock_backend):
        output = transcript("Kiro is working") + IDLE_PROMPT
        assert provider().get_status(output) in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}


@patch("cli_agent_orchestrator.providers.kiro_cli.get_backend")
class TestMonitorWithTheCapturedBuffer:
    """The latched IDLE must give way to the dialog."""

    def test_idle_then_dialog_is_waiting(self, mock_backend):
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        p = provider()
        p.supports_screen_detection = False
        sm = StatusMonitor()
        with (
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm,
            patch("cli_agent_orchestrator.services.status_monitor.bus", MagicMock()),
            patch("cli_agent_orchestrator.services.status_monitor.get_server_settings") as settings,
        ):
            settings.return_value = {"state_buffer_max": 32768}
            pm.get_provider.return_value = p
            sm._process_chunk("dialog0", IDLE_PROMPT)
            assert sm._last_status["dialog0"] in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
            sm._process_chunk("dialog0", transcript("Kiro is working") + SHELL_LINE + DIALOG)
            assert sm._last_status["dialog0"] == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.parametrize("footer", [STEER, QUEUE], ids=["steer", "queue"])
    def test_dialog_answered_in_the_pane_is_processing(self, mock_backend, footer):
        """No CAO input arms the latch; kiro_cli's opt-in lets the release through."""
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        p = provider()
        p.supports_screen_detection = False
        sm = StatusMonitor()
        with (
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm,
            patch("cli_agent_orchestrator.services.status_monitor.bus", MagicMock()),
            patch("cli_agent_orchestrator.services.status_monitor.get_server_settings") as settings,
        ):
            settings.return_value = {"state_buffer_max": 32768}
            pm.get_provider.return_value = p
            sm._process_chunk("dialog0", IDLE_PROMPT)
            sm._process_chunk("dialog0", transcript("Kiro is working") + SHELL_LINE + DIALOG)
            assert sm._last_status["dialog0"] == TerminalStatus.WAITING_USER_ANSWER
            sm._process_chunk("dialog0", "\n" + footer_line(footer))
            assert sm._last_status["dialog0"] == TerminalStatus.PROCESSING
