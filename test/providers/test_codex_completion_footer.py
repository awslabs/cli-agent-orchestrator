"""Codex 0.155+ ends a turn with a completion footer ("Worked for 32s • 4:19 PM").

Codex streams the final answer with no spinner. Before the footer is drawn, a
frame of a streaming answer looked like a finished turn: the handoff returned a
partial reply, or, when the user message had scrolled away, a spinner line. The
frames below are trimmed from live codex-cli 0.160.0 panes (2026-10-11).
"""

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import (
    COMPLETION_FOOTER_PATTERN,
    EXIT_CHECK_INTERVAL_S,
    CodexProvider,
    _banner_version,
    _dispatched_echoes,
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

    @pytest.mark.parametrize("with_status_bar", [False, True])
    def test_attributed_long_reply_completes_after_echo_and_message_scroll_away(
        self, with_status_bar
    ):
        provider = TestEmptyDispatchedTurn()._dispatched_provider("a very long task " * 8)
        frame = [
            *(f"• Reply row {n}" for n in range(40)),
            "  Worked for 55s • 4:15 PM",
            COMPOSER,
            *([STATUS_BAR] if with_status_bar else []),
        ]
        assert provider.get_status_from_screen(frame) == TerminalStatus.COMPLETED

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


class TestEmptyDispatchedTurn:
    MESSAGE = (
        "ACK: agente disponible\n\n"
        "[Message from terminal worker. Use send_message MCP tool for any follow-up work.]"
    )

    def _dispatched_provider(
        self,
        message=None,
        baseline="› Earlier task.\n" + COMPOSER + "\n" + STATUS_BAR,
    ):
        provider = footer_provider()
        with patch("cli_agent_orchestrator.providers.codex.get_backend") as get_backend:
            get_backend.return_value.get_history.return_value = baseline
            provider.notify_status_buffer_reset(1)
            provider.mark_input_received()
            provider.record_dispatched_message(message or self.MESSAGE)
        return provider

    @pytest.mark.parametrize("with_status_bar", [False, True])
    def test_current_full_echo_and_short_footer_complete_and_extract_empty(self, with_status_bar):
        provider = self._dispatched_provider()
        fixture = Path(__file__).parent / "fixtures/codex_empty_turn_completed.txt"
        capture = fixture.read_text()
        if not with_status_bar:
            capture = capture.replace("\n" + STATUS_BAR, "")
        frame = capture.splitlines()
        assert provider.get_status_from_screen(frame) == TerminalStatus.COMPLETED
        assert provider.extract_last_message_from_script(capture) == ""

    def test_footer_in_user_text_without_actual_footer_does_not_complete(self):
        provider = self._dispatched_provider("Worked for 5s • 4:19 PM")
        frame = screen("› Worked for 5s • 4:19 PM")
        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    def test_identical_baseline_frame_is_not_current_turn_evidence(self):
        frame = (
            Path(__file__)
            .parent.joinpath("fixtures/codex_empty_turn_completed.txt")
            .read_text()
            .splitlines()
        )
        provider = self._dispatched_provider("\n".join(frame))
        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    @pytest.mark.parametrize("with_status_bar", [False, True])
    def test_previous_turn_footer_cannot_complete_a_new_dispatch(self, with_status_bar):
        previous = "\n".join(
            [
                "› Previous task",
                "• Previous answer",
                "  Worked for 2s • 4:19 PM",
                COMPOSER,
                STATUS_BAR,
            ]
        )
        provider = self._dispatched_provider("new task", baseline=previous)
        frame = [
            "› Previous task",
            "• Previous answer",
            "  Worked for 2s • 4:19 PM",
            COMPOSER,
            *([STATUS_BAR] if with_status_bar else []),
        ]

        detected = provider.get_status_from_screen(frame)
        assert detected != TerminalStatus.COMPLETED
        if not with_status_bar:
            assert detected == TerminalStatus.UNKNOWN

    @pytest.mark.parametrize("with_status_bar", [False, True])
    @pytest.mark.parametrize("include_previous_user", [False, True])
    def test_previous_reply_and_footer_outside_viewport_cannot_complete_dispatch(
        self, include_previous_user, with_status_bar
    ):
        previous_rows = ["• Previous answer", "  Worked for 2s • 4:19 PM"]
        if include_previous_user:
            previous_rows.insert(0, "› Previous task")
        previous = "\n".join([*previous_rows, COMPOSER, STATUS_BAR])
        provider = self._dispatched_provider("new task", baseline=previous)
        frame = [
            "• Previous answer",
            "  Worked for 2s • 4:19 PM",
            COMPOSER,
            *([STATUS_BAR] if with_status_bar else []),
        ]

        detected = provider.get_status_from_screen(frame)
        assert detected != TerminalStatus.COMPLETED
        if not with_status_bar:
            assert detected == TerminalStatus.UNKNOWN

    @pytest.mark.parametrize("with_status_bar", [False, True])
    def test_new_reply_after_previous_footer_completes_without_echo(self, with_status_bar):
        previous = "\n".join(
            [
                "› Previous task",
                "• Previous answer",
                "  Worked for 2s • 4:19 PM",
                COMPOSER,
                STATUS_BAR,
            ]
        )
        provider = self._dispatched_provider("new task", baseline=previous)
        frame = [
            "• Current answer after the previous turn",
            "  Worked for 5s • 4:20 PM",
            COMPOSER,
            *([STATUS_BAR] if with_status_bar else []),
        ]
        capture = "\n".join(frame)

        assert provider.get_status_from_screen(frame) == TerminalStatus.COMPLETED
        assert not provider._empty_current_dispatch(capture)

    def test_previous_footer_without_visible_user_echo_cannot_complete_dispatch(self):
        provider = self._dispatched_provider("new task whose echo is not visible")
        frame = screen("  Worked for 2s • 4:19 PM")

        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    def test_stale_footer_without_echo_cannot_complete_without_status_bar(self):
        provider = self._dispatched_provider("new task whose echo is not visible")
        frame = ["  Worked for 2s • 4:19 PM", COMPOSER]

        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    @pytest.mark.parametrize(
        "rows",
        [
            ["› abc extra", "  Worked for 1s • 4:19 PM"],
            ["› abc", "  continuation extra", "  Worked for 1s • 4:19 PM"],
        ],
    )
    def test_text_after_dispatch_echo_on_final_physical_row_is_not_discarded(self, rows):
        provider = self._dispatched_provider("abc")
        frame = screen(*rows)

        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    def test_echo_matcher_rejects_same_row_suffix_and_accepts_exact_wrapping(self):
        assert _dispatched_echoes(["› abc extra"], "abc") == []
        assert _dispatched_echoes(["› abc", "  def"], "abcdef") == [(0, 2)]

    def test_captured_word_wrapped_initial_dispatch_completes(self):
        """Sanitized 226-column capture from the e8948af1 inbox stall."""
        fixture = Path(__file__).parent / "fixtures/codex_word_wrapped_completed.json"
        captured = json.loads(fixture.read_text())
        provider = self._dispatched_provider(captured["message"])

        assert provider.get_status_from_screen(captured["screen"]) == TerminalStatus.COMPLETED

    @pytest.mark.parametrize("with_status_bar", [False, True])
    @pytest.mark.parametrize("reply", [[], ["• Acknowledged."]])
    def test_word_wrapped_multiline_dispatch_completes(self, with_status_bar, reply):
        message = (
            "ACK: agent available\n\n"
            "[Message from terminal worker01. Use send_message MCP tool for any follow-up work.]"
        )
        provider = self._dispatched_provider(message)
        frame = [
            "› ACK: agent",
            "  available",
            "",
            "  [Message from terminal worker01. Use send_message MCP",
            "  tool for any follow-up work.]",
            "",
            *reply,
            "  Worked for <1s • 7:46 AM",
            COMPOSER,
        ]
        if with_status_bar:
            frame.append(STATUS_BAR)

        assert provider.get_status_from_screen(frame) == TerminalStatus.COMPLETED
        if not reply:
            assert provider.extract_last_message_from_script("\n".join(frame)) == ""

    def test_word_wrapped_echo_with_footer_in_user_text_does_not_complete(self):
        provider = self._dispatched_provider("Meeting notes\nWorked for 5s • 4:19 PM")
        frame = screen("› Meeting", "  notes", "  Worked for 5s • 4:19 PM")

        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    def test_word_wrapped_echo_already_in_baseline_does_not_complete_new_dispatch(self):
        frame = screen("› abc", "  def", "  Worked for 5s • 4:19 PM")
        provider = self._dispatched_provider("abc def", baseline="\n".join(frame))

        assert provider.get_status_from_screen(frame) != TerminalStatus.COMPLETED

    def test_empty_current_turn_completes_without_status_bar(self):
        provider = self._dispatched_provider()
        frame = [
            "› ACK: agente disponible",
            "",
            "  [Message from terminal worker. Use send_message MCP tool for any follow-up work.]",
            "",
            "  Worked for <1s • 7:46 AM",
            COMPOSER,
        ]

        assert provider.get_status_from_screen(frame) == TerminalStatus.COMPLETED
        assert provider.extract_last_message_from_script("\n".join(frame)) == ""

    def test_identical_message_needs_a_new_echo_after_the_previous_footer(self):
        fixture = Path(__file__).parent / "fixtures/codex_empty_turn_completed.txt"
        baseline = fixture.read_text()
        provider = self._dispatched_provider(baseline=baseline)
        frame = screen(
            "› ACK: agente disponible",
            "",
            "  [Message from terminal worker. Use send_message MCP tool for any follow-up work.]",
            "",
            "  Worked for <1s • 7:46 AM",
            "› ACK: agente disponible",
            "",
            "  [Message from terminal worker. Use send_message MCP tool for any follow-up work.]",
            "",
            "  Worked for <1s • 7:47 AM",
        )

        assert provider.get_status_from_screen(frame) == TerminalStatus.COMPLETED

    def test_full_38_row_pane_with_chrome_maps_the_repeated_empty_turn(self):
        notice = "ACK: agent available"
        baseline = [f"history {i}" for i in range(32)] + [
            "› " + notice,
            "",
            "  Worked for <1s • 7:46 AM",
            COMPOSER,
            "",
            STATUS_BAR,
        ]
        current = [f"history {i}" for i in range(3, 32)] + [
            "› " + notice,
            "",
            "  Worked for <1s • 7:46 AM",
            "› " + notice,
            "",
            "  Worked for <1s • 7:47 AM",
            COMPOSER,
            "",
            STATUS_BAR,
        ]
        assert len(baseline) == len(current) == 38
        provider = self._dispatched_provider(notice, baseline="\n".join(baseline))

        capture = "\n".join(current)
        assert provider.get_status_from_screen(current) == TerminalStatus.COMPLETED
        assert provider._empty_current_dispatch(capture)
        assert provider.extract_last_message_from_script(capture) == ""

    def test_prepended_old_echo_is_not_a_fresh_repeat(self):
        notice = "ACK: agent available"
        baseline = [
            "› " + notice,
            "",
            "  Worked for <1s • 7:46 AM",
            COMPOSER,
            "",
            STATUS_BAR,
        ]
        provider = self._dispatched_provider(notice, baseline="\n".join(baseline))
        current = [f"newly visible history {i}" for i in range(10)] + [
            "› " + notice,
            "",
            "  Worked for <1s • 7:46 AM",
        ]

        assert provider._current_dispatch_echo_end("\n".join(current)) is None
        assert not provider._empty_current_dispatch("\n".join(current))
        assert provider.get_status_from_screen(screen(*current)) != TerminalStatus.COMPLETED

    def test_ambiguous_exact_alignment_is_rejected(self):
        provider = self._dispatched_provider("message")
        baseline = ("before", "  Worked for <1s • 7:46 AM", "after")
        current = [*baseline, *baseline]

        assert provider._map_baseline_row(baseline, current, 1) is None

    @pytest.mark.parametrize(
        ("message", "rendered_echo"),
        [
            ("Run:\\tmake test", "› Run:    make test"),
            ("Done.  \\nNext step", "› Done.\n  Next step"),
        ],
    )
    def test_tab_and_trailing_spaces_in_sent_message_do_not_prove_empty(
        self, message, rendered_echo
    ):
        provider = self._dispatched_provider(message)
        capture = rendered_echo + "\n  Worked for <1s • 7:46 AM"

        assert not provider._empty_current_dispatch(capture)

    def test_repeated_message_in_full_pane_uses_aligned_scroll_boundary(self):
        msg = "repeat"
        prior = [f"context {i}" for i in range(31)] + ["› repeat", "  Worked for 2s • 4:18 PM"]
        baseline = "\n".join(prior)
        provider = self._dispatched_provider(msg, baseline=baseline)
        # The pane stays at 38 rows: two old rows leave at the top, while a new
        # identical echo and footer arrive at the bottom.
        current = [f"context {i}" for i in range(2, 31)] + [
            "› repeat",
            "  Worked for 2s • 4:18 PM",
            "› repeat",
            "  Worked for <1s • 4:19 PM",
        ]
        capture = "\n".join(current)
        assert provider._current_dispatch_echo_end(capture) == 30
        assert provider.get_status_from_screen(current) == TerminalStatus.COMPLETED
        assert provider.extract_last_message_from_script(capture) == ""

    def test_echo_matcher_keeps_tabs_and_trailing_spaces_significant(self):
        assert _dispatched_echoes(["› abc\tdef"], "abc def") == []
        assert _dispatched_echoes(["› abc def  "], "abc def") == []


class TestDispatchedEchoWrapping:
    @pytest.mark.parametrize(
        ("message", "rows"),
        [
            ("abc def", ["› abc", "  def"]),
            ("abc   def", ["› abc", "  def"]),
            ("abc def ghi", ["› abc", "  def", "  ghi"]),
            ("abc def", ["› abc", "   def"]),
            ("abc def", ["› abc de", "  f"]),
            ("acción 😀 漢字", ["› acción", "  😀", "  漢字"]),
            ("abc def\n\nghi jkl", ["› abc", "  def", "", "  ghi", "  jkl"]),
        ],
    )
    def test_spaces_omitted_only_at_soft_wrap_boundaries(self, message, rows):
        assert _dispatched_echoes(rows, message) == [(0, len(rows))]

    @pytest.mark.parametrize(
        ("message", "rows"),
        [
            ("abc def", ["› abcdef"]),
            ("abc  def", ["› abc def"]),
            ("abc def", ["› abc", "  def extra"]),
            ("abc def", ["› abc", "    def"]),
            ("abc\tdef", ["› abc", "  def"]),
            ("abc\u00a0def", ["› abc", "  def"]),
            ("abc\ndef", ["› abc def"]),
            ("abc\n\ndef", ["› abc", "  def"]),
            ("abc\n def", ["› abc", "  def"]),
        ],
    )
    def test_other_whitespace_and_row_suffixes_remain_significant(self, message, rows):
        assert _dispatched_echoes(rows, message) == []


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
        # The shell at the baseline read, codex at every exit check after the launch (#1018).
        pane_commands = iter(["zsh"])
        backend.get_pane_current_command.side_effect = lambda *_a, **_k: next(
            pane_commands, "codex"
        )
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
