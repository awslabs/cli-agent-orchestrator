"""Codex 0.155+ ends a turn with a completion footer ("Worked for 32s • 4:19 PM").

Codex streams the final answer with no spinner. Before the footer is drawn, a
frame of a streaming answer looked like a finished turn: the handoff returned a
partial reply, or, when the user message had scrolled away, a spinner line. The
frames below are trimmed from live codex-cli 0.160.0 panes (2026-10-11).
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import (
    COMPLETION_FOOTER_PATTERN,
    EXIT_CHECK_INTERVAL_S,
    CodexProvider,
    _banner_version,
    _is_completion_footer,
)

STATUS_BAR = "  gpt-6 max · /tmp/project                       ⚠ 1 warning · f2 to view"
COMPOSER = "› Ask Codex to do anything"
BANNER = [
    "╭──────────────────────────────────────────╮",
    "│ >_ OpenAI Codex (v0.160.0)               │",
    "│     /tmp/project                         │",
    "╰──────────────────────────────────────────╯",
    "  To get started, describe a task or try one of these commands:",
    "  /init - create an AGENTS.md file with instructions for Codex",
]
TASK = [
    "› Analyze Dataset A: [1, 2, 3, 4, 5].",
    "  Calculate mean, median, and standard deviation.",
]
V0160 = (0, 160, 0)


def footer_provider(version=V0160) -> CodexProvider:
    provider = CodexProvider("test1234", "test-session", "window-0")
    provider._codex_version = version
    provider._ends_turns_with_footer = True
    return provider


def screen(*rows: str) -> list[str]:
    return [*rows, COMPOSER, STATUS_BAR]


class TestCompletionFooterPattern:
    @pytest.mark.parametrize(
        "line",
        [
            "  Worked for 32s • 4:19 PM",
            "  Worked for 2s • 16:19",
            "  Worked for <1s • 4:19 PM",
            "  Worked for 1h 2m 3s • Oct 3 4:19 PM",
            "  Worked for 5s • 4:19 PM • 3 tool calls (2s)",
            "  Worked for 1m 5s · done 4:19 PM",
            "  done 4:19 PM",
            "  done Oct 3 at 4:19 PM",
            "  4:19 PM",
            "  16:19",
        ],
    )
    def test_footer_forms_of_0_155_and_later(self, line):
        assert re.search(COMPLETION_FOOTER_PATTERN, line, re.MULTILINE), line

    @pytest.mark.parametrize(
        "line",
        [
            # 0.154 and earlier: a rule ABOVE the final message, not a turn end.
            "─ Worked for 1m 5s ────────────────────────────",
            "  The meeting is at 4:19 PM today.",
            "  12:30 and 13:00",
            # A live footer always has the time; without it, this is reply text.
            "  Worked for 1m 5s",
            "  Worked for 2s",
            STATUS_BAR,
            "",
        ],
    )
    def test_other_lines_are_not_a_footer(self, line):
        assert not re.search(COMPLETION_FOOTER_PATTERN, line, re.MULTILINE), line

    @pytest.mark.parametrize(
        ("line", "version", "expected"),
        [
            ("  Worked for 32s • 4:19 PM", V0160, True),
            ("  done 4:19 PM", (0, 155, 0), True),
            # Only 0.156 to 0.158 draw a bare time; a reply row can look the same.
            ("  4:19 PM", (0, 158, 0), True),
            ("  4:19 PM", V0160, False),
            ("  09:00 · Standup", V0160, False),
        ],
    )
    def test_bare_time_is_a_footer_only_where_codex_draws_one(self, line, version, expected):
        assert _is_completion_footer(line, version) is expected


class TestStatusWithCompletionFooter:
    def test_fresh_terminal_is_idle(self):
        assert footer_provider().get_status_from_screen(screen(*BANNER)) == TerminalStatus.IDLE

    def test_streaming_answer_without_footer_is_processing(self):
        """No spinner while the answer streams; the old rules read COMPLETED here."""
        frame = screen(
            *BANNER,
            *TASK,
            "• The mean and median of [1, 2, 3, 4, 5] are both 3, because the values",
            "  are evenly spaced around the middle value.",
        )
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.PROCESSING

    def test_footer_after_the_reply_is_completed(self):
        frame = screen(*BANNER, *TASK, "• Mean 3, median 3.", "", "  Worked for 32s • 4:19 PM")
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.COMPLETED

    def test_streaming_reply_whose_newest_row_reads_worked_for_is_processing(self):
        """A reply row such as "Worked for 2s" is not the footer (no time)."""
        frame = screen(*TASK, "• Summary:", "  Worked for 2s")
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.PROCESSING
        assert footer_provider()._strip_completion_footer("• Summary:\n  Worked for 2s") == (
            "• Summary:\n  Worked for 2s"
        )

    def test_streaming_reply_whose_newest_row_is_a_time_is_processing(self):
        frame = screen(*TASK, "• Agenda:", "  09:00 · Standup")
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.PROCESSING

    def test_user_message_with_nothing_under_it_is_unknown(self):
        """A torn redraw, or a turn that has not drawn its first row: no
        evidence either way, so StatusMonitor keeps the last status."""
        frame = screen(*BANNER, *TASK)
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.UNKNOWN

    def test_footer_like_line_in_the_user_message_does_not_complete(self):
        frame = screen("› Plan the day:", "  Worked for 5s • 4:19 PM")
        assert footer_provider().get_status_from_screen(frame) != TerminalStatus.COMPLETED

    def test_long_answer_whose_user_message_scrolled_away_is_processing(self):
        frame = screen(*(f"  Row {n} of a long template." for n in range(40)))
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.PROCESSING

    def test_long_answer_whose_user_message_scrolled_away_completes_on_footer(self):
        frame = screen(
            *(f"  Row {n} of a long template." for n in range(40)),
            "",
            "  Worked for 55s • 4:15 PM",
        )
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.COMPLETED

    def test_previous_turn_footer_does_not_complete_the_next_turn(self):
        frame = screen(
            *TASK,
            "• Mean 3, median 3.",
            "  Worked for 32s • 4:19 PM",
            "› Now analyze Dataset B.",
            "• Working (3s • esc to interrupt)",
        )
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.PROCESSING

    def test_frame_without_status_bar_is_unknown(self):
        """A redraw in progress: UNKNOWN keeps the last status in StatusMonitor."""
        frame = [*TASK, "• Mean 3, median 3.", COMPOSER]
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.UNKNOWN

    def test_finished_turn_without_status_bar_is_completed(self):
        """The bar can stop fitting the pane (an attach resize); the footer of
        the turn still ends it."""
        frame = [*TASK, "• Mean 3, median 3.", "  Worked for 32s • 4:19 PM", COMPOSER]
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.COMPLETED

    def test_blank_rows_at_the_end_of_a_pane_read(self):
        frame = "\n".join([*screen(*BANNER), "", "   ", ""])
        assert footer_provider().get_status(frame) == TerminalStatus.IDLE

    def test_error_cell_ends_the_turn_without_footer(self):
        frame = screen(*TASK, "■ stream disconnected before completion")
        assert footer_provider().get_status_from_screen(frame) != TerminalStatus.PROCESSING

    def test_raw_stream_keeps_the_older_rules(self):
        """With CAO_PYTE_STATUS=false the raw stream is parsed. It keeps every
        redraw, so a stale status bar or footer can follow the live rows."""
        frame = "\n".join(screen(*TASK, "• Mean 3, median 3."))
        assert footer_provider().get_status(frame) == TerminalStatus.COMPLETED

    def test_release_without_footer_keeps_the_older_rules(self):
        """origin/main reads IDLE here; a footer-like line in the user
        message must not change that on a release without the footer."""
        provider = CodexProvider("test1234", "test-session", "window-0")
        frame = screen("› Plan the day:", "  09:00 · Standup")
        assert provider.get_status_from_screen(frame) == TerminalStatus.IDLE


class TestSpinnerQuotedByAToolResult:
    def test_spinner_above_the_footer_is_history(self):
        """A handoff result quoted a worker's spinner line, and the supervisor
        then read PROCESSING after its turn had ended, so its inbox never
        delivered."""
        frame = screen(
            *TASK,
            "• Called cao-mcp-server.handoff({...})",
            '    {"success": true, "output": "• Working (12s • esc to interrupt)"}',
            "• I've assigned the analyses and requested the report template.",
            "  Worked for 1m 36s • 4:10 PM",
        )
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.COMPLETED

    def test_live_spinner_is_processing(self):
        frame = screen(*TASK, "• Working (12s • esc to interrupt)")
        assert footer_provider().get_status_from_screen(frame) == TerminalStatus.PROCESSING

    def test_torn_frame_with_a_live_spinner_is_processing(self):
        """The mid-burst probe sees mostly half-drawn frames; a drawn spinner
        is still evidence of a working turn."""
        frame = [*TASK, "• Working (5s • esc to interrupt)", COMPOSER]
        provider = footer_provider()
        assert provider.get_status_from_screen(frame) == TerminalStatus.PROCESSING
        assert provider.probe_processing_from_screen(frame) is True


class TestExitCheck:
    @staticmethod
    def _provider(mock_backend, pane_command):
        mock_backend.return_value.get_native_status.return_value = None
        mock_backend.return_value.get_pane_current_command.return_value = pane_command
        provider = footer_provider()
        provider._initialized = True
        provider.shell_baseline = "zsh"
        return provider

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_busy_frames_read_the_pane_at_most_once_per_interval(self, mock_backend):
        """The lookup costs about 0.1 s of tmux calls on StatusMonitor's one
        consumer; reading it for every frame put the status of five Codex
        terminals one to two minutes behind."""
        provider = self._provider(mock_backend, "codex")
        busy = screen(*TASK, "• Working (5s • esc to interrupt)")

        for _ in range(5):
            assert provider.get_status_from_screen(busy) == TerminalStatus.PROCESSING

        assert mock_backend.return_value.get_pane_current_command.call_count == 1

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_a_ready_verdict_always_reads_the_pane(self, mock_backend):
        provider = self._provider(mock_backend, "codex")
        ready = screen(*TASK, "• Mean 3.", "  Worked for 2s • 4:19 PM")

        for _ in range(3):
            assert provider.get_status_from_screen(ready) == TerminalStatus.COMPLETED

        assert mock_backend.return_value.get_pane_current_command.call_count == 3

    @patch("cli_agent_orchestrator.providers.codex.time")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_busy_frames_read_the_pane_again_after_the_interval(self, mock_backend, mock_time):
        provider = self._provider(mock_backend, "codex")
        busy = screen(*TASK, "• Working (5s • esc to interrupt)")
        mock_time.monotonic.side_effect = [100.0, 100.5, 100.0 + EXIT_CHECK_INTERVAL_S]

        for _ in range(3):
            provider.get_status_from_screen(busy)

        assert mock_backend.return_value.get_pane_current_command.call_count == 2

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_a_shell_prompt_under_a_stale_codex_footer_is_error(self, mock_backend):
        """Codex killed without cleanup leaves its status bar on the screen,
        and a "❯" prompt can take the composer row."""
        provider = self._provider(mock_backend, "zsh")
        frame = [*TASK, "• Mean 3.", "❯ ", STATUS_BAR]

        assert provider.get_status_from_screen(frame) == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_exit_to_the_shell_is_error(self, mock_backend):
        provider = self._provider(mock_backend, "zsh")
        frame = [*TASK, "To continue this session, run: codex resume 01a1", "me · ~/project %"]

        assert provider.get_status_from_screen(frame) == TerminalStatus.ERROR


class TestFooterHelpers:
    @pytest.mark.parametrize(
        ("text", "version"),
        [
            (">_ OpenAI Codex (v0.160.0)", (0, 160, 0)),
            ("OpenAI Codex (v0.98.0)", (0, 98, 0)),
            ("no banner", (0, 0, 0)),
        ],
    )
    def test_banner_version(self, text, version):
        assert _banner_version(text) == version

    def test_footer_is_stripped_from_a_reply(self):
        reply = "OK\n\n  Worked for 2s • 4:19 PM"
        assert footer_provider()._strip_completion_footer(reply) == "OK"

    def test_time_rows_of_a_reply_are_kept(self):
        reply = "• Agenda:\n  09:00 · Standup\n  10:00 · Review\n\n  Worked for 2s • 4:19 PM"
        assert footer_provider()._strip_completion_footer(reply) == (
            "• Agenda:\n  09:00 · Standup\n  10:00 · Review"
        )

    def test_release_without_footer_strips_nothing(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        reply = "• Agenda:\n  09:00 · Standup"
        assert provider._strip_completion_footer(reply) == reply

    def test_reply_that_is_only_a_time_is_kept(self):
        assert footer_provider()._strip_completion_footer("16:19") == "16:19"

    def test_extraction_drops_the_footer(self):
        output = "\n".join([*TASK, "• OK", "", "  Worked for 2s • 4:19 PM", COMPOSER, STATUS_BAR])
        assert footer_provider().extract_last_message_from_script(output) == "• OK"


class TestFooterReleaseDetection:
    @staticmethod
    async def _initialize(history: str) -> CodexProvider:
        provider = CodexProvider("test1234", "test-session", "window-0")
        backend = MagicMock()
        backend.get_history.return_value = history
        backend.get_pane_current_command.return_value = "zsh"
        with (
            patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.codex.wait_for_shell", return_value=True),
            patch("cli_agent_orchestrator.providers.codex.wait_until_status", return_value=True),
            patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock),
            patch.object(provider, "_build_codex_command", return_value="codex"),
            patch.object(provider, "_handle_trust_prompt", new_callable=AsyncMock),
            patch.object(provider, "_current_startup_state", new_callable=AsyncMock),
        ):
            await provider.initialize()
        return provider

    @pytest.mark.asyncio
    async def test_release_with_footer_and_status_bar(self):
        provider = await self._initialize("\n".join(screen(*BANNER)))
        assert provider._ends_turns_with_footer is True
        assert provider._codex_version == V0160

    @pytest.mark.asyncio
    async def test_blank_rows_at_the_end_of_the_capture(self):
        """A capture of the pane ends with its blank rows."""
        provider = await self._initialize("\n".join([*screen(*BANNER), "", "", "   ", ""]))
        assert provider._ends_turns_with_footer is True

    @pytest.mark.asyncio
    async def test_release_before_the_footer(self):
        history = "\n".join(screen(*[row.replace("0.160.0", "0.154.0") for row in BANNER]))
        provider = await self._initialize(history)
        assert provider._ends_turns_with_footer is False

    @pytest.mark.asyncio
    async def test_status_bar_that_the_pattern_does_not_know(self):
        provider = await self._initialize("\n".join([*BANNER, COMPOSER, "  my custom line"]))
        assert provider._ends_turns_with_footer is False


class TestCaoToolsAreNotDeferred:
    @patch("cli_agent_orchestrator.providers.codex.agent_profiles.load_agent_profile")
    def test_only_the_cao_server_skips_tool_search(self, mock_load_profile):
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = ""
        profile.codexProfile = None
        profile.codexConfig = None
        profile.mcpServers = {
            "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []},
            "other": {"type": "stdio", "command": "other-server", "args": []},
        }
        mock_load_profile.return_value = profile

        command = CodexProvider(
            "test1234", "test-session", "window-0", "supervisor"
        )._build_codex_command()

        assert 'mcp_servers.cao-mcp-server.omit_tools_from=["deferred"]' in command
        assert "mcp_servers.other.omit_tools_from" not in command

    def test_poller_recheck_reads_the_screen(self):
        assert CodexProvider.status_recheck_uses_screen is True
