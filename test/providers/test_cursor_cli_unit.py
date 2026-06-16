"""Unit tests for the Cursor CLI provider."""

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cursor_cli import (
    ANSI_CODE_PATTERN,
    IDLE_PROMPT_PATTERN,
    IDLE_PROMPT_PATTERN_LOG,
    PERMISSION_PROMPT_PATTERN,
    PROCESSING_PATTERN,
    SEPARATOR_PATTERN,
    TRUST_PROMPT_PATTERN,
    TUI_PLACEHOLDER_PATTERN,
    TUI_PROCESSING_INDICATOR_PATTERN,
    TUI_STATUS_BAR_PATTERN,
    WAITING_USER_ANSWER_PATTERN,
    CursorCliProvider,
    ProviderError,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Load a plain-text fixture file."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _stub_cursor_binary():
    """Make ``shutil.which('agent')`` succeed for the duration of every
    test in this module so the build_command / initialize paths don't
    raise ``ProviderError('Cursor CLI not found')``.

    Tests that need to exercise the legacy-alias fallback override
    this via ``mock_which``.
    """
    with patch(
        "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
        return_value="/usr/bin/agent",
    ):
        yield


def make_provider(
    agent_profile: str | None = None,
    allowed_tools: list | None = None,
    model: str | None = None,
    skill_prompt: str | None = None,
) -> CursorCliProvider:
    """Build a CursorCliProvider with the given configuration."""
    return CursorCliProvider(
        terminal_id="test-tid",
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        allowed_tools=allowed_tools,
        model=model,
        skill_prompt=skill_prompt,
    )


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


class TestRegexPatterns:
    def test_idle_prompt_matches_unicode_arrow(self):
        assert re.search(IDLE_PROMPT_PATTERN, "\u276f ")

    def test_idle_prompt_matches_ascii_arrow(self):
        assert re.search(IDLE_PROMPT_PATTERN, "> ")

    def test_idle_prompt_matches_non_breaking_space(self):
        assert re.search(IDLE_PROMPT_PATTERN, "\u276f\xa0")

    def test_idle_prompt_rejects_other_text(self):
        assert not re.search(IDLE_PROMPT_PATTERN, "hello world")

    def test_processing_pattern_matches_braille_spinner(self):
        assert re.search(PROCESSING_PATTERN, "\u2807 Thinking\u2026")

    def test_processing_pattern_matches_unicode_spinner(self):
        assert re.search(PROCESSING_PATTERN, "\u2736 Reasoning\u2026")

    def test_processing_pattern_matches_claude_spinner(self):
        assert re.search(PROCESSING_PATTERN, "\u2733 Cooking\u2026 (esc to interrupt)")

    def test_processing_pattern_rejects_plain_text(self):
        assert not re.search(PROCESSING_PATTERN, "just some plain text")

    def test_waiting_user_answer_pattern_matches_navigation_footer(self):
        assert re.search(WAITING_USER_ANSWER_PATTERN, "\u2191/\u2193 to navigate")

    def test_trust_prompt_pattern_matches(self):
        assert re.search(
            TRUST_PROMPT_PATTERN, "Do you trust the files in this folder?", re.IGNORECASE
        )

    def test_permission_prompt_pattern_matches(self):
        assert re.search(PERMISSION_PROMPT_PATTERN, "Do you want to allow this?", re.IGNORECASE)

    def test_ansi_strips_truecolor(self):
        text = "\x1b[38;2;255;100;50mHello\x1b[0m"
        assert re.sub(ANSI_CODE_PATTERN, "", text) == "Hello"

    def test_idle_prompt_is_start_of_line_anchored(self):
        # Copilot review #3411781807: IDLE_PROMPT_PATTERN must be
        # anchored to start-of-line so it does NOT match the
        # leading "❯ " on echoed user input lines (e.g.
        # "❯ Summarize…") or any "> " inside response content.
        # The pattern is also what the regex module anchors; we
        # verify by passing multi-line input and inspecting the
        # match positions.
        ip = re.compile(IDLE_PROMPT_PATTERN, re.MULTILINE)
        # A line-anchored prompt: only one match at offset 0.
        text = "\u276f Summarize this file"
        matches = list(ip.finditer(text))
        assert len(matches) == 1
        assert matches[0].start() == 0

    def test_idle_prompt_rejects_arrow_in_response_content(self):
        # A ">" or "❯" character in the middle of a response
        # body (e.g. "use > to redirect" or "return > 0") must
        # NOT be matched as an idle prompt.
        ip = re.compile(IDLE_PROMPT_PATTERN, re.MULTILINE)
        # "use > to redirect" — the ">" is preceded by " " (a
        # space), so the pattern's `^\s*` anchor fails to match
        # at that position; MULTILINE `^` only matches at line
        # start.
        text = "use > to redirect output"
        assert list(ip.finditer(text)) == []
        # Same for an in-line "❯" surrounded by text.
        text2 = "the answer is \u276f 42"
        assert list(ip.finditer(text2)) == []

    def test_idle_prompt_log_is_start_of_line_anchored(self):
        # Copilot review #3411781846: IDLE_PROMPT_PATTERN_LOG has
        # the same over-broad matching problem as IDLE_PROMPT_PATTERN.
        ip = re.compile(IDLE_PROMPT_PATTERN_LOG, re.MULTILINE)
        text = "use > to redirect"
        assert list(ip.finditer(text)) == []


# ---------------------------------------------------------------------------
# SEPARATOR_PATTERN
# ---------------------------------------------------------------------------


class TestSeparatorPattern:
    def test_matches_plain_separator(self):
        # Baseline: a plain ──…── line.
        sep = "\u2500" * 22
        assert re.search(SEPARATOR_PATTERN, sep, re.MULTILINE)

    def test_matches_csi_before_dash_run(self):
        # The original case: a single CSI before the entire dash run.
        sep = "\x1b[38;5;245m" + ("\u2500" * 22) + "\x1b[0m"
        assert re.search(SEPARATOR_PATTERN, sep, re.MULTILINE)

    def test_matches_csi_between_dashes(self):
        # Copilot review #3411781900 / #3411781914: the separator
        # regex must tolerate CSI sequences *between* the ─
        # characters, not just before the entire run. Cursor
        # re-renders the separator in place with new colour escapes
        # on every prompt, so the byte stream looks like
        # `\x1b[38;5;245m──\x1b[0m──\x1b[38;5;245m──` (CSIs
        # interleaved between dashes). Build a 22-dash line with
        # CSIs after every two dashes; the regex must still
        # consume the full line.
        dash_run = "\u2500" * 22
        # Insert a CSI every 2 dashes
        interleaved = ""
        for i, ch in enumerate(dash_run):
            interleaved += ch
            if (i + 1) % 2 == 0 and i < len(dash_run) - 1:
                interleaved += "\x1b[0m"
        # Wrap with leading SGR to mimic a TUI re-render
        sep = "\x1b[38;5;245m" + interleaved
        assert re.search(SEPARATOR_PATTERN, sep, re.MULTILINE)

    def test_does_not_match_dash_sequence_inside_content(self):
        # Copilot review: the regex must not match a stray dash
        # sequence inside response content. The pattern is anchored
        # to a full line so a 20+-dash substring embedded in a
        # longer line is not matched.
        bad = "Here is some code: " + ("\u2500" * 22) + " done"
        assert not re.search(SEPARATOR_PATTERN, bad, re.MULTILINE)


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Verify get_status() returns the correct enum for each fixture.

    New event-driven contract: get_status(output) receives the buffer
    string directly from the StatusMonitor; the provider no longer
    reads tmux internally.
    """

    def test_idle_fixture_returns_completed(self):
        output = load_fixture("cursor_cli_idle_output.txt")
        provider = make_provider()
        # Fresh spawn, no input received yet -> IDLE, not COMPLETED.
        # We mark a turn to simulate the post-first-turn state so
        # the same fixture exercises the COMPLETED branch as well.
        provider.mark_input_received()
        # Provider reports COMPLETED on idle prompt to match other
        # providers' "ready" signal convention.
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_idle_fixture_without_input_returns_idle(self):
        # Fresh spawn (no user input ever delivered): the TUI
        # looks the same as the post-turn idle state but the
        # turn counter is zero, so the detector must report IDLE
        # (not COMPLETED) — distinguishes "just spawned, waiting
        # for first prompt" from "last turn delivered, ready
        # for next".
        output = load_fixture("cursor_cli_idle_output.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_completed_fixture_returns_completed(self):
        output = load_fixture("cursor_cli_completed_output.txt")
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_processing_spinner_before_separator_returns_processing(self):
        output = load_fixture("cursor_cli_processing_output.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_stale_spinner_ignored_returns_completed(self):
        # A spinner from a previous turn followed by another separator
        # is a completed task, not active processing.
        sep = "\u2500" * 30
        stale_output = (
            sep
            + "\nFirst task done\n"
            + sep
            + "\nOld spinner text\u2026 lingering\n"
            + sep
            + "\nLatest response done\n"
            + sep
            + "\n\u276f "
        )
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(stale_output) == TerminalStatus.COMPLETED

    def test_processing_no_separator_yet_returns_processing(self):
        output = "Welcome to Cursor Agent\n\u2807 Thinking\u2026\n"
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_trust_prompt_returns_waiting_user_answer(self):
        output = load_fixture("cursor_cli_permission_output.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_tui_widget_footer_returns_waiting_user_answer(self):
        sep = "\u2500" * 30
        output = (
            sep
            + "\nPick a model:\n"
            + "gpt-5\nsonnet-4\n"
            + "\u2191/\u2193 to navigate, enter to select\n"
        )
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_empty_output_returns_unknown(self):
        provider = make_provider()
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    def test_none_output_returns_unknown(self):
        provider = make_provider()
        assert provider.get_status(None) == TerminalStatus.UNKNOWN

    def test_unrecognizable_output_returns_unknown(self):
        provider = make_provider()
        assert provider.get_status("random text without any markers") == TerminalStatus.UNKNOWN

    def test_idle_after_input_received_returns_completed(self):
        # Long response with multiple separators, ending at the idle prompt.
        sep = "\u2500" * 30
        output = sep + "\n\u276f What is 2+2?\n" + sep + "\nThe answer is 4.\n" + sep + "\n\u276f "
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(output) == TerminalStatus.COMPLETED


# ---------------------------------------------------------------------------
# get_status() — Cursor CLI v2026+ TUI detection (issue #299)
# ---------------------------------------------------------------------------


class TestGetStatusV2026Tui:
    """Status detection for the Ink/TUI Cursor CLI ships in v2026+.

    The pre-v2026 regex suite (looking for `❯` and `─────`) no longer
    matches the v2026 output because those markers are TUI widgets and
    never reach the pipe-pane buffer (issue #299). The provider now
    relies on the input-box placeholder "Plan, search, build anything":
    present in the tail of the buffer = idle / completed, absent =
    the user has submitted and the agent is working.
    """

    def test_v2026_idle_fixture_returns_completed(self):
        output = load_fixture("cursor_cli_v2026_idle_output.txt")
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_v2026_idle_fixture_fresh_spawn_returns_idle(self):
        # Without a turn, the same TUI buffer is IDLE (not
        # COMPLETED). The turn counter is the split.
        output = load_fixture("cursor_cli_v2026_idle_output.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_v2026_post_turn_idle_fixture_returns_completed(self):
        # v2026 swaps the input-box placeholder from "Plan,
        # search, build anything" (fresh launch) to "Add a
        # follow-up" after the first user turn. The status
        # detector must still classify this post-turn idle
        # state as COMPLETED so the supervisor inbox can pick
        # up the response on the next turn.
        output = load_fixture("cursor_cli_v2026_post_turn_idle_output.txt")
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_v2026_processing_fixture_returns_processing(self):
        # The processing fixture has the placeholder replaced by
        # user-typed text ("say hello in 3 words"). No `❯`, no
        # `─────`, no spinner — the TUI-marker fallback is the
        # only thing that can classify this as PROCESSING.
        output = load_fixture("cursor_cli_v2026_processing_output.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_synthetic_v2026_idle_with_tui_markers(self):
        # Minimal hand-crafted buffer: header + status bar +
        # placeholder. No `❯`, no `─────`. Pre-fix this would
        # have returned UNKNOWN because every regex was looking
        # for an older-Build marker.
        output = (
            "  Cursor Agent\n"
            "  v2026.06.15-03-48-54-da23e37\n"
            "  \x1b[2mUse /config to customize.\x1b[0m\n"
            "\n"
            "  \x1b[48;5;233m \x1b[2m→ \x1b[0;7mP\x1b[0;2m"
            "lan, search, build anything\x1b[0m"
            "\x1b[48;5;233m                                              \x1b[49m\n"
            "  \x1b[48;5;233m                                                                              \x1b[49m\n"
            "\n"
            "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_synthetic_v2026_processing_placeholder_replaced(self):
        # The v2026 TUI marks active processing with "ctrl+c to
        # stop" on the input-box line, not by removing the
        # placeholder. The placeholder is ALWAYS present in v2026
        # regardless of agent state, so the previous test (which
        # stripped the placeholder to fake "processing") was
        # modelling the wrong v2026 behaviour. The correct v2026
        # processing state keeps the placeholder AND adds
        # "ctrl+c to stop" on the same line.
        output = (
            "  Cursor Agent\n"
            "  v2026.06.15-03-48-54-da23e37\n"
            "\n"
            "  \x1b[48;5;233m \x1b[2m→ \x1b[0;7m"
            "Add a follow-up\x1b[0m"
            "                                                    "
            "    \x1b[2mctrl+c to stop\x1b[0m"
            " \x1b[49m\n"
            "  \x1b[48;5;233m                                                                       \x1b[49m\n"
            "\n"
            "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_processing_window_only_checks_tail_of_buffer(self):
        # A 4KB buffer where the input-box line was rendered with
        # the "ctrl+c to stop" indicator early in the buffer (so it
        # has scrolled out of the 1KB TUI TAIL WINDOW) but the
        # agent is no longer working. The tail of the buffer
        # therefore has the status bar and the placeholder but no
        # processing indicator, and we must classify the state as
        # COMPLETED (post-turn idle). This is the inverse of the
        # long-response-in-head test below.
        padding = "x" * 3500
        output = (
            "  \x1b[48;5;233m \x1b[2m→ \x1b[0;7m"
            "Add a follow-up\x1b[0m"
            "                                                    "
            "    \x1b[2mctrl+c to stop\x1b[0m"
            " \x1b[49m\n" + padding + "\n" * 50 + "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        # Sanity: the indicator is in the head, not in the tail.
        assert "ctrl+c to stop" in output
        assert "ctrl+c to stop" not in output[-1024:]
        provider = make_provider()
        provider.mark_input_received()
        # The tail has the status bar and the placeholder but no
        # "ctrl+c to stop" — this is the post-turn idle state.
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_idle_placeholder_with_long_response_in_head(self):
        # Inverse of the previous test: a long response was
        # processed (no placeholder in head) but the agent has
        # finished and redrawn the placeholder, which is now
        # present in the tail. Must classify as COMPLETED.
        padding = "y" * 3500
        # Place the placeholder at the end of the buffer (the
        # natural TUI redraw position) so it lands inside the
        # 1024-byte TUI TAIL WINDOW.
        output = (
            "  \x1b[2mComposer 2.5 Fast\x1b[0m\n"
            + padding
            + "\n"
            + "  \x1b[48;5;233m \x1b[2m→ \x1b[0;7mP\x1b[0;2m"
            "lan, search, build anything\x1b[0m"
            "\x1b[48;5;233m                                              \x1b[49m\n"
            "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        provider = make_provider()
        provider.mark_input_received()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_processing_indicator_in_tail_returns_processing(self):
        # The new v2026+ TUI signal: "ctrl+c to stop" on the
        # input-box line. The placeholder is still present in
        # v2026 regardless of state, so it is the presence /
        # absence of this indicator that distinguishes
        # processing from idle.
        output = (
            "  Cursor Agent\n"
            "  v2026.06.15-03-48-54-da23e37\n"
            "\n"
            "  ⠠⠛ Composing  23 tokens\n"
            "\n"
            "  \x1b[48;5;233m \x1b[2m→ \x1b[0;7m"
            "Add a follow-up\x1b[0m"
            "                                                    "
            "    \x1b[2mctrl+c to stop\x1b[0m"
            " \x1b[49m\n"
            "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_processing_indicator_pattern_documented(self):
        # Pattern sanity check: TUI_PROCESSING_INDICATOR_PATTERN
        # matches the v2026+ TUI hint, and does not spuriously
        # match idle / completed buffers.
        assert re.search(TUI_PROCESSING_INDICATOR_PATTERN, "ctrl+c to stop", re.IGNORECASE)
        assert re.search(
            TUI_PROCESSING_INDICATOR_PATTERN,
            "  ctrl+c to stop  ",
            re.IGNORECASE,
        )
        # Negative: the indicator must NOT match an idle input
        # box (the v2026 placeholder alone, no "ctrl+c to stop").
        assert not re.search(TUI_PROCESSING_INDICATOR_PATTERN, "Add a follow-up", re.IGNORECASE)
        assert not re.search(
            TUI_PROCESSING_INDICATOR_PATTERN, "Plan, search, build anything", re.IGNORECASE
        )

    def test_v2026_placeholder_pattern_documented(self):
        # Pattern sanity check: TUI_PLACEHOLDER_PATTERN must match
        # both placeholder strings Cursor v2026 uses, and
        # TUI_STATUS_BAR_PATTERN must match the status bar
        # fragments we use as a "TUI is fully rendered" guard.
        assert re.search(TUI_PLACEHOLDER_PATTERN, "Plan, search, build anything")
        assert re.search(TUI_PLACEHOLDER_PATTERN, "  plan, search, build anything  ", re.IGNORECASE)
        # v2026 swaps the placeholder to "Add a follow-up" after
        # the first turn — the detection must classify that as
        # idle too.
        assert re.search(TUI_PLACEHOLDER_PATTERN, "Add a follow-up")
        assert re.search(TUI_STATUS_BAR_PATTERN, "Run Everything")
        assert re.search(TUI_STATUS_BAR_PATTERN, "Composer 2.5 Fast")
        # Negative: these patterns must NOT spuriously match a
        # "no markers" buffer that we want to classify as UNKNOWN.
        assert not re.search(TUI_PLACEHOLDER_PATTERN, "say hello world")
        assert not re.search(TUI_STATUS_BAR_PATTERN, "random text")


# ---------------------------------------------------------------------------
# extract_last_message_from_script()
# ---------------------------------------------------------------------------


class TestExtractLastMessage:
    def test_extracts_response_from_completed_fixture(self):
        provider = make_provider()
        output = load_fixture("cursor_cli_completed_output.txt")
        result = provider.extract_last_message_from_script(output)
        assert "comprehensive response" in result
        assert "multiple paragraphs" in result

    def test_extracts_response_strips_ansi(self):
        sep = "\u2500" * 30
        provider = make_provider()
        output = (
            sep
            + "\n\u276f say hello\n"
            + sep
            + "\n\x1b[32mHello world\x1b[0m\n"
            + sep
            + "\n\u276f "
        )
        result = provider.extract_last_message_from_script(output)
        assert "Hello world" in result
        assert "\x1b[" not in result

    def test_raises_when_no_separator(self):
        provider = make_provider()
        with pytest.raises(ValueError, match="No Cursor CLI response found"):
            provider.extract_last_message_from_script("\u276f hello")

    def test_raises_when_no_idle_prompt(self):
        provider = make_provider()
        output = ("\u2500" * 30) + "\nSome response without trailing prompt"
        with pytest.raises(ValueError, match="No Cursor CLI response found"):
            provider.extract_last_message_from_script(output)

    def test_raises_when_response_is_empty(self):
        sep = "\u2500" * 30
        provider = make_provider()
        # Two separators back to back, then idle prompt. No content between.
        output = sep + "\n\u276f user\n" + sep + "\n   \n" + sep + "\n\u276f "
        with pytest.raises(ValueError, match="Empty Cursor CLI response"):
            provider.extract_last_message_from_script(output)

    def test_extracts_with_only_one_separator(self):
        # Single-separator buffers occur when the response start
        # separator has scrolled out of the 8KB rolling buffer but
        # the end separator is still present. In that case the
        # start_sep is None and we fall back to the buffer start.
        sep = "\u2500" * 30
        provider = make_provider()
        output = sep + "\nThe answer is 42.\n" + sep + "\n\u276f "
        result = provider.extract_last_message_from_script(output)
        assert "The answer is 42." in result

    def test_separator_matching_tolerates_interleaved_csi_escapes(self):
        # Cursor re-renders the separator with new colour escapes
        # on every prompt: the box-drawing line may contain
        # multiple SGR segments interleaved between the ─ chars.
        # The regex must still match so status detection and
        # extraction both work.
        sep_with_color = "\x1b[38;5;245m" + ("\u2500" * 30) + "\x1b[0m"
        provider = make_provider()
        provider.mark_input_received()
        output = (
            sep_with_color
            + "\n\u276f question\n"
            + sep_with_color
            + "\nHello world\n"
            + sep_with_color
            + "\n\u276f "
        )
        # Status detection also uses the separator regex.
        assert provider.get_status(output) == TerminalStatus.COMPLETED
        # Extraction must find the response between the second
        # and third separators.
        result = provider.extract_last_message_from_script(output)
        assert "Hello world" in result

    def test_extraction_strips_cursor_positioning_sequences(self):
        # Long generations cause Cursor to emit cursor-positioning
        # sequences inside the response area (e.g. \x1b[2K erase
        # line, \x1b[H cursor home). The extracted text must not
        # contain these.
        sep = "\u2500" * 30
        provider = make_provider()
        output = (
            sep
            + "\n\u276f say hello\n"
            + sep
            + "\nHello \x1b[2Kworld\x1b[H with cursor moves\n"
            + sep
            + "\n\u276f "
        )
        result = provider.extract_last_message_from_script(output)
        assert "Hello world with cursor moves" in result
        assert "\x1b[" not in result

    def test_extraction_strips_osc_title_sequences(self):
        # OSC sequences (e.g. terminal title updates) can leak into
        # the captured text. The extraction must strip them.
        sep = "\u2500" * 30
        provider = make_provider()
        osc = "\x1b]0;Cursor Agent\x07"  # set window title
        output = (
            sep
            + "\n\u276f say hello\n"
            + sep
            + "\n"
            + osc
            + "Response text after title update\n"
            + sep
            + "\n\u276f "
        )
        result = provider.extract_last_message_from_script(output)
        assert "Response text after title update" in result
        assert "\x1b]" not in result

    def test_uses_last_response_when_multiple(self):
        sep = "\u2500" * 30
        provider = make_provider()
        output = (
            sep
            + "\n\u276f First question\n"
            + sep
            + "\nFirst response\n"
            + sep
            + "\n\u276f Second question\n"
            + sep
            + "\nSecond response\n"
            + sep
            + "\n\u276f Third question\n"
            + sep
            + "\nThird (latest) response\n"
            + sep
            + "\n\u276f "
        )
        result = provider.extract_last_message_from_script(output)
        assert "Third" in result
        assert "Second" not in result
        assert "First" not in result


# ---------------------------------------------------------------------------
# _build_cursor_command()
# ---------------------------------------------------------------------------


class TestBuildCommand:
    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_no_profile_bare_command(self, mock_load):
        mock_load.side_effect = FileNotFoundError("no profile")
        provider = make_provider()
        cmd = provider._build_cursor_command()
        # v2026+ rejects --trust in interactive REPL mode (it is only
        # valid with --print/headless). v2026 also dropped the
        # --agent flag, so the launch command is now "cursor-agent --force"
        # (or "agent --force" when only the primary name is on PATH;
        # we prefer the unambiguous cursor-agent alias first — see
        # issues #299 and #300).
        assert cmd == "cursor-agent --force"

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_constructor_model_forwarded(self, mock_load):
        mock_load.side_effect = FileNotFoundError("no profile")
        provider = make_provider(model="gpt-5")
        cmd = provider._build_cursor_command()
        assert "--model gpt-5" in cmd

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_profile_model_overrides_constructor(self, mock_load):
        profile = MagicMock()
        profile.model = "sonnet-4"
        profile.system_prompt = None
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer", model="gpt-5")
        cmd = provider._build_cursor_command()
        assert "--model sonnet-4" in cmd
        assert "gpt-5" not in cmd

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_agent_profile_loaded_but_not_passed_as_flag(self, mock_load):
        # v2026.06.15 has a confirmed bug where the backend rejects
        # any ``--system-prompt <file>`` request with
        # ``[invalid_argument] unknown option '--system-prompt'``,
        # so the provider deliberately does not pass that flag
        # even when a profile has a system prompt. The CAO role
        # context still reaches the agent via the cao-mcp-server
        # inbox (handoff / assign payloads include role + prompt
        # on the wire), so dropping the flag is the right
        # operational choice.
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "DEVELOPER_AGENT_BODY"
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        cmd = provider._build_cursor_command()
        assert "--agent" not in cmd
        assert "--system-prompt" not in cmd

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_no_system_prompt_flag_even_with_prompt_content(self, mock_load):
        # Sanity: even a 3-character system prompt does not get
        # written to a file. The provider's _write_system_prompt_file
        # helper is preserved (for the day Cursor ships a fixed
        # v2026.x) but no longer wired into the launch command.
        from cli_agent_orchestrator.providers.cursor_cli import (
            CursorCliProvider,
        )

        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "hi"
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        cmd = provider._build_cursor_command()
        assert "--system-prompt" not in cmd

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_mcp_servers_forwarded_via_plugin_dir(self, mock_load):
        # v2026 removed ``--mcp <json>``. The replacement is
        # ``--plugin-dir <path>`` pointing at a directory holding a
        # plugin manifest. We synthesise that directory at build
        # time; the test asserts the flag is present, points at an
        # existing directory, and that the manifest's mcpServers
        # map carries CAO_TERMINAL_ID.
        import json
        from pathlib import Path

        profile = MagicMock()
        profile.model = None
        profile.system_prompt = None
        profile.mcpServers = {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}}
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        cmd = provider._build_cursor_command()
        assert "--mcp" not in cmd
        assert "--plugin-dir" in cmd
        assert "--approve-mcps" in cmd
        m = re.search(r"--plugin-dir\s+(\S+)", cmd)
        assert m is not None, f"--plugin-dir <path> not found in: {cmd}"
        plugin_dir = Path(m.group(1))
        assert plugin_dir.is_dir()
        # The synthesised manifest must include the server with the
        # terminal id forwarded into its env.
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        servers = manifest["mcpServers"]
        assert "cao-mcp-server" in servers
        assert servers["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test-tid"

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_mcp_resolves_bundled_command_in_manifest(self, mock_load):
        # Wiring guard: the bare cao-mcp-server command must be rewritten to
        # a PATH-independent invocation in the written plugin manifest. A
        # refactor that drops the resolve_mcp_server_config call fails this.
        import json
        from pathlib import Path

        profile = MagicMock()
        profile.model = None
        profile.system_prompt = None
        profile.mcpServers = {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}}
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        MOD = "cli_agent_orchestrator.utils.mcp_resolution"
        # NOTE: mcp_resolution and cursor_cli import the SAME shutil module
        # object, so a blanket which->None would break the provider's own
        # cursor-binary lookup (stubbed by the autouse fixture). Only the
        # cao-mcp-server lookup may miss.
        which_cursor_keeps_working = lambda name: (
            None if name == "cao-mcp-server" else "/usr/local/bin/cursor-agent"
        )
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", side_effect=which_cursor_keeps_working),
        ):
            cmd = provider._build_cursor_command()
        m = re.search(r"--plugin-dir\s+(\S+)", cmd)
        assert m is not None
        manifest = json.loads((Path(m.group(1)) / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["mcpServers"]["cao-mcp-server"]["command"] == "/venv/bin/cao-mcp-server"

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_mcp_preserves_existing_cao_terminal_id(self, mock_load):
        # The constructor's terminal_id must NOT override an
        # explicit preset (matches the prior --mcp behaviour).
        import json
        from pathlib import Path

        profile = MagicMock()
        profile.model = None
        profile.system_prompt = None
        profile.mcpServers = {
            "cao-mcp-server": {
                "command": "cao-mcp-server",
                "args": [],
                "env": {"CAO_TERMINAL_ID": "preset"},
            }
        }
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        cmd = provider._build_cursor_command()
        m = re.search(r"--plugin-dir\s+(\S+)", cmd)
        assert m is not None
        plugin_dir = Path(m.group(1))
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "preset"

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_tool_restrictions_skip_system_prompt(self, mock_load):
        # Soft tool-restriction enforcement used to prepend
        # SECURITY_PROMPT and the tool list to the system prompt.
        # v2026.06.15 forces us to drop --system-prompt entirely,
        # so this enforcement path is not available. The test
        # asserts the command does NOT carry the security prompt
        # (it is no longer injected) and does NOT carry the
        # --system-prompt flag.
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "Base prompt."
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(
            agent_profile="developer",
            allowed_tools=["fs_read", "fs_list"],
        )
        cmd = provider._build_cursor_command()
        assert "--system-prompt" not in cmd
        assert "SECURITY CONSTRAINTS" not in cmd
        assert "fs_read" not in cmd
        assert "fs_list" not in cmd

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_wildcard_allowed_tools_no_system_prompt(self, mock_load):
        # Unrestricted yolo mode: nothing special happens — there
        # is no system-prompt injection at all in v2026.06.15.
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "Base prompt."
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(
            agent_profile="developer",
            allowed_tools=["*"],
        )
        cmd = provider._build_cursor_command()
        assert "--system-prompt" not in cmd

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_missing_profile_raises_provider_error(self, mock_load):
        mock_load.side_effect = FileNotFoundError("missing")
        provider = make_provider(agent_profile="developer")
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_cursor_command()


# ---------------------------------------------------------------------------
# _build_cursor_command() — binary resolution
# ---------------------------------------------------------------------------


class TestBuildCommandBinaryResolution:
    """Copilot review #3411781886: ``_build_cursor_command`` must
    fall back to the legacy ``cursor-agent`` binary when the
    primary ``agent`` binary is missing on the host.

    The provider prefers the unambiguous ``cursor-agent`` alias
    first because ``agent`` is a common binary name shared with
    unrelated tools (Linux ``gpg-agent``, the OS X
    ``com.apple.security.agent``-style launch daemons, various
    language server entry points, etc.). When the primary
    ``agent`` name is selected the provider also probes
    ``agent --version`` to confirm the resolved binary is the
    Cursor CLI before launching (see
    :meth:`TestBuildCommandBinaryResolution.test_agent_validation`
    and the related tests).
    """

    def test_prefers_cursor_agent_when_both_available(self):
        # ``cursor-agent`` is unambiguous (only the Cursor CLI
        # ships it) so the provider picks it even when ``agent``
        # is also on PATH.
        def fake_which(name):
            return {
                "agent": "/usr/bin/agent",
                "cursor-agent": "/usr/local/bin/cursor-agent",
            }.get(name)

        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            side_effect=fake_which,
        ):
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                side_effect=FileNotFoundError("no profile"),
            ):
                provider = make_provider()
                cmd = provider._build_cursor_command()
        assert cmd.startswith("cursor-agent ")

    def test_agent_validation_passes_for_cursor_binary(self):
        # When only ``agent`` is on PATH, the provider probes
        # ``agent --version`` and accepts a banner that matches
        # ``agent <4-digit year>.<...>`` (Cursor's semver
        # convention). The version probe runs via subprocess so
        # we mock it; the test asserts the launch proceeds.
        probe_result = MagicMock()
        probe_result.stdout = "agent 2026.06.15-03-48-54-da23e37\n"
        probe_result.stderr = ""

        def fake_which(name):
            return "/usr/bin/agent" if name == "agent" else None

        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            side_effect=fake_which,
        ):
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.subprocess.run",
                return_value=probe_result,
            ) as mock_run:
                with patch(
                    "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                    side_effect=FileNotFoundError("no profile"),
                ):
                    provider = make_provider()
                    cmd = provider._build_cursor_command()
        assert cmd.startswith("agent ")
        # The probe must have been called.
        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0] == ["agent", "--version"]

    def test_agent_validation_rejects_non_cursor_binary(self):
        # The probe returns a banner that does not match the
        # Cursor semver convention (e.g. the resolved ``agent``
        # is some other tool). The provider must raise
        # ProviderError rather than launching a non-Cursor
        # binary with Cursor-only flags and producing a 500.
        probe_result = MagicMock()
        probe_result.stdout = "agent (gpg-agent) 2.4.0\n"
        probe_result.stderr = ""

        def fake_which(name):
            return "/usr/bin/agent" if name == "agent" else None

        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            side_effect=fake_which,
        ):
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.subprocess.run",
                return_value=probe_result,
            ):
                with patch(
                    "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                    side_effect=FileNotFoundError("no profile"),
                ):
                    provider = make_provider()
                    with pytest.raises(ProviderError, match="does not identify as Cursor CLI"):
                        provider._build_cursor_command()

    def test_agent_validation_handles_probe_timeout(self):
        # If the resolved ``agent`` binary hangs the probe
        # (subprocess.TimeoutExpired), the provider must surface
        # a clear error rather than blocking ``_build_cursor_command``
        # for the operator.
        def fake_which(name):
            return "/usr/bin/agent" if name == "agent" else None

        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            side_effect=fake_which,
        ):
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["agent", "--version"], timeout=3.0),
            ):
                with patch(
                    "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                    side_effect=FileNotFoundError("no profile"),
                ):
                    provider = make_provider()
                    with pytest.raises(ProviderError, match="Could not probe"):
                        provider._build_cursor_command()

    def test_cursor_agent_skips_validation(self):
        # ``cursor-agent`` is unambiguous; no version probe is
        # needed. The provider should pick it without ever
        # touching subprocess.
        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            return_value="/usr/local/bin/cursor-agent",
        ):
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.subprocess.run",
            ) as mock_run:
                with patch(
                    "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                    side_effect=FileNotFoundError("no profile"),
                ):
                    provider = make_provider()
                    cmd = provider._build_cursor_command()
        assert cmd.startswith("cursor-agent ")
        mock_run.assert_not_called()

    def test_falls_back_to_cursor_agent_when_agent_missing(self):
        # When only the legacy alias is on PATH, the command must
        # invoke it so the launch does not hard-fail on older
        # installations pinned to the historical name.
        def fake_which(name):
            if name == "agent":
                return None
            if name == "cursor-agent":
                return "/usr/local/bin/cursor-agent"
            return None

        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            side_effect=fake_which,
        ):
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                side_effect=FileNotFoundError("no profile"),
            ):
                provider = make_provider()
                cmd = provider._build_cursor_command()
        assert cmd.startswith("cursor-agent ")
        assert "--force" in cmd

    def test_raises_when_neither_binary_installed(self):
        # Both binaries missing: a clear ProviderError with an
        # install-from message.
        with patch(
            "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
            return_value=None,
        ):
            provider = make_provider()
            with pytest.raises(ProviderError, match="Cursor CLI not found"):
                provider._build_cursor_command()


# ---------------------------------------------------------------------------
# initialize() — async with new get_backend pattern
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.cursor_cli.get_backend")
    async def test_initialize_success(self, mock_backend, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        assert await provider.initialize() is True
        assert provider._initialized is True
        mock_backend.return_value.send_keys.assert_called_once()
        sent = mock_backend.return_value.send_keys.call_args.args[2]
        # v2026+ rejects --trust in interactive REPL mode. See
        # issue #299. The launch command starts with the resolved
        # binary (cursor-agent preferred, agent fallback).
        assert sent.startswith("cursor-agent ") or sent.startswith("agent ")
        assert "--force" in sent
        assert "--trust" not in sent

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.cursor_cli.get_backend")
    async def test_initialize_shell_timeout(self, mock_backend, mock_shell):
        mock_shell.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()
        mock_backend.return_value.send_keys.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.cursor_cli.get_backend")
    async def test_initialize_cursor_timeout(self, mock_backend, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="Cursor CLI initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.cursor_cli.get_backend")
    async def test_initialize_does_not_send_system_prompt_flag(
        self, mock_backend, mock_shell, mock_wait
    ):
        # v2026.06.15 has a confirmed bug where any request that
        # carries --system-prompt is rejected by the backend, so
        # the provider deliberately omits the flag.
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(agent_profile="developer")
        await provider.initialize()
        sent = mock_backend.return_value.send_keys.call_args.args[2]
        assert "--agent" not in sent
        assert "--system-prompt" not in sent

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.cursor_cli.get_backend")
    async def test_initialize_sends_model_flag(self, mock_backend, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(model="gpt-5")
        await provider.initialize()
        sent = mock_backend.return_value.send_keys.call_args.args[2]
        assert "--model gpt-5" in sent

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.cursor_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.cursor_cli.get_backend")
    async def test_initialize_arms_stickiness_gate(self, mock_backend, mock_shell, mock_wait):
        # Copilot review #3411781865: initialize() must call
        # status_monitor.notify_input_sent() before send_keys so
        # the launching command can drive a fresh PROCESSING
        # transition past any stale ready latch. Without this,
        # a previously-latched IDLE/COMPLETED would suppress the
        # genuine PROCESSING transition that follows.
        #
        # The status_monitor module is imported lazily inside
        # initialize() to break a circular import
        # (status_monitor imports provider_manager which imports
        # cursor_cli), so we install a sentinel module into
        # sys.modules with a status_monitor attribute. The lazy
        # ``from cli_agent_orchestrator.services.status_monitor
        # import status_monitor`` inside initialize() resolves
        # through sys.modules and binds the sentinel
        # ``status_monitor`` name in the cursor_cli module's
        # namespace.
        sentinel_status_monitor = MagicMock()
        sentinel_module = type(sys)("fake_status_monitor")
        sentinel_module.status_monitor = sentinel_status_monitor

        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()

        with patch.dict(
            "sys.modules",
            {"cli_agent_orchestrator.services.status_monitor": sentinel_module},
            clear=False,
        ):
            await provider.initialize()

        sentinel_status_monitor.notify_input_sent.assert_called_once_with(provider.terminal_id)
        # And send_keys must have been called.
        assert mock_backend.return_value.send_keys.call_count == 1


# ---------------------------------------------------------------------------
# Misc interface methods
# ---------------------------------------------------------------------------


class TestMiscInterface:
    def test_exit_cli_returns_slash_exit(self):
        assert make_provider().exit_cli() == "/exit"

    def test_get_idle_pattern_for_log(self):
        assert make_provider().get_idle_pattern_for_log() == IDLE_PROMPT_PATTERN_LOG

    def test_cleanup_resets_initialized(self):
        provider = make_provider()
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_cleanup_removes_tracked_tmp_paths(self, tmp_path, monkeypatch):
        # Copilot review #3412413702 (P1): cleanup() must delete
        # the per-session temp files the provider wrote (system
        # prompt + plugin dir). We stub _cao_tmp_dir to redirect
        # to a temp dir and assert the files are removed on
        # cleanup().
        monkeypatch.setenv("CAO_TMP_DIR", str(tmp_path))
        provider = make_provider()
        # Materialise a fake system-prompt file and a fake plugin
        # dir as if a launch had run.
        prompt_path = tmp_path / f"{provider.terminal_id}-system-prompt.md"
        prompt_path.write_text("dummy")
        plugin_dir = tmp_path / f"{provider.terminal_id}-cursor-plugins"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text("{}")
        provider._tmp_paths = [prompt_path, plugin_dir]
        # Sanity: the files are there before cleanup.
        assert prompt_path.exists()
        assert plugin_dir.is_dir()
        provider.cleanup()
        # And gone after.
        assert not prompt_path.exists()
        assert not plugin_dir.exists()
        # The registry is also drained so a second cleanup is a
        # no-op (idempotent).
        assert provider._tmp_paths == []
        provider.cleanup()  # should not raise

    def test_paste_enter_count_is_one(self):
        assert make_provider().paste_enter_count == 1

    def test_terminal_attributes_stored(self):
        provider = make_provider(
            agent_profile="developer", allowed_tools=["fs_read"], model="gpt-5"
        )
        assert provider.terminal_id == "test-tid"
        assert provider.session_name == "test-session"
        assert provider.window_name == "window-0"
        assert provider._agent_profile == "developer"
        assert provider._model == "gpt-5"


# ---------------------------------------------------------------------------
# ProviderManager registration
# ---------------------------------------------------------------------------


class TestProviderManagerRegistration:
    def test_create_provider_cursor_cli_stores_mapping(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        manager = ProviderManager()
        provider = manager.create_provider(
            provider_type="cursor_cli",
            terminal_id="tid",
            tmux_session="s",
            tmux_window="w",
            agent_profile="developer",
        )
        assert isinstance(provider, CursorCliProvider)
        assert manager.get_provider("tid") is provider
        assert manager.list_providers()["tid"] == "CursorCliProvider"

    def test_create_provider_unknown_type_raises(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        manager = ProviderManager()
        with pytest.raises(ValueError, match="Unknown provider type"):
            manager.create_provider(
                provider_type="nonexistent",
                terminal_id="tid",
                tmux_session="s",
                tmux_window="w",
            )


# ---------------------------------------------------------------------------
# launch.py PROVIDERS_REQUIRING_WORKSPACE_ACCESS
# ---------------------------------------------------------------------------


class TestWorkspaceAccess:
    def test_cursor_cli_in_workspace_access_set(self):
        from cli_agent_orchestrator.cli.commands.launch import (
            PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
        )

        assert "cursor_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS
