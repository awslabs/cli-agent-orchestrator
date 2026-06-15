"""Unit tests for the Cursor CLI provider."""

import re
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
    TUI_PLACEHOLDER_PATTERN,
    TUI_STATUS_BAR_PATTERN,
    TRUST_PROMPT_PATTERN,
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
        # Provider reports COMPLETED on idle prompt to match other
        # providers' "ready" signal convention.
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_completed_fixture_returns_completed(self):
        output = load_fixture("cursor_cli_completed_output.txt")
        provider = make_provider()
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
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_synthetic_v2026_processing_placeholder_replaced(self):
        # Same buffer as the idle case, but the placeholder has
        # been replaced by the user's submitted text. The status
        # bar is still visible so the buffer looks fully rendered,
        # not half-initialised.
        output = (
            "  Cursor Agent\n"
            "  v2026.06.15-03-48-54-da23e37\n"
            "\n"
            "  \x1b[48;5;233m \x1b[2m→ \x1b[0;7msay hello in 3 words\x1b[0m"
            "\x1b[48;5;233m                                                  \x1b[49m\n"
            "  \x1b[48;5;233m                                                                       \x1b[49m\n"
            "\n"
            "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_processing_window_only_checks_tail_of_buffer(self):
        # A 4KB buffer that contains the placeholder in the first
        # 3KB but NOT in the last 1KB must classify as PROCESSING
        # — long responses evict the placeholder from the visible
        # tail even though it is still in the scrollback. The
        # status bar is also absent from the tail so the
        # processing-positive branch (which requires the status
        # bar) does not fire; the absence of the placeholder in
        # the tail combined with no `❯` separator matches means
        # the status is UNKNOWN. We assert the specific contract:
        # the TUI TAIL WINDOW matters, not the full buffer.
        padding = "x" * 3500
        output = (
            "Plan, search, build anything\n"  # placeholder in head
            + padding
            + "\n" * 50
            + "  \x1b[2mComposer 2.5 Fast\x1b[0m"
            "                                             \x1b[35mRun Everything\x1b[39m\n"
        )
        # Sanity: the placeholder is in the head, not in the tail.
        assert "Plan, search, build anything" in output
        assert "Plan, search, build anything" not in output[-1024:]
        provider = make_provider()
        # The tail has the status bar but no placeholder, so the
        # processing-positive branch fires.
        assert provider.get_status(output) == TerminalStatus.PROCESSING

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
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_v2026_placeholder_pattern_documented(self):
        # Pattern sanity check: TUI_PLACEHOLDER_PATTERN must match
        # the literal placeholder text Cursor renders, and
        # TUI_STATUS_BAR_PATTERN must match the status bar
        # fragments we use as a "TUI is fully rendered" guard.
        assert re.search(TUI_PLACEHOLDER_PATTERN, "Plan, search, build anything")
        assert re.search(TUI_PLACEHOLDER_PATTERN, "  plan, search, build anything  ", re.IGNORECASE)
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
        # --agent flag, so the launch command is now "agent --force"
        # when no profile / model / system-prompt is configured.
        # See issues #299 and #300.
        assert cmd == "agent --force"

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
    def test_agent_profile_injected_via_system_prompt_file(self, mock_load):
        # v2026 dropped the ``--agent`` flag. The CAO agent profile's
        # body is now injected via ``--system-prompt <file>`` so the
        # same multi-agent orchestration still works without
        # selecting a Cursor-side agent. This test asserts the
        # command contains a ``--system-prompt <path>`` pair (the
        # path is the per-session temp file the provider writes).
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "DEVELOPER_AGENT_BODY"
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        cmd = provider._build_cursor_command()
        assert "--agent" not in cmd
        assert "--system-prompt" in cmd
        # The value after --system-prompt should be a path to a
        # file containing the profile body.
        m = re.search(r"--system-prompt\s+(\S+)", cmd)
        assert m is not None, f"--system-prompt <path> not found in: {cmd}"
        prompt_path = m.group(1)
        # File exists, was written by the provider during
        # _build_cursor_command.
        from pathlib import Path
        assert Path(prompt_path).exists()
        contents = Path(prompt_path).read_text(encoding="utf-8")
        assert "DEVELOPER_AGENT_BODY" in contents

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_system_prompt_preserves_newlines_in_file(self, mock_load):
        # v2026 reads the system prompt from a *file*, so we no
        # longer have to escape newlines into ``\\n`` for tmux
        # transport. The file should contain the original multi-
        # line string verbatim.
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "Line 1\nLine 2"
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(agent_profile="developer")
        cmd = provider._build_cursor_command()
        m = re.search(r"--system-prompt\s+(\S+)", cmd)
        assert m is not None
        from pathlib import Path
        assert Path(m.group(1)).read_text(encoding="utf-8") == "Line 1\nLine 2"

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_skill_prompt_appended(self, mock_load):
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = "Base prompt."
        profile.mcpServers = None
        mock_load.return_value = profile
        provider = make_provider(
            agent_profile="developer",
            skill_prompt="<skills>...</skills>",
        )
        cmd = provider._build_cursor_command()
        # Skill catalog is appended to the system prompt before it
        # is written to the file. The command only references the
        # file path; the catalog is the responsibility of
        # _apply_skill_prompt, which is exercised by the base
        # provider's own test suite.
        m = re.search(r"--system-prompt\s+(\S+)", cmd)
        assert m is not None

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
        assert (
            manifest["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"]
            == "preset"
        )

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_tool_restrictions_prepend_security_prompt(self, mock_load):
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
        # v2026: SECURITY_PROMPT and the tool list are written into
        # the system-prompt file, not the command line. The command
        # only references the file path; we read the file to assert
        # the security prompt and the tool list are there.
        m = re.search(r"--system-prompt\s+(\S+)", cmd)
        assert m is not None
        from pathlib import Path
        contents = Path(m.group(1)).read_text(encoding="utf-8")
        assert "SECURITY CONSTRAINTS" in contents
        assert "fs_read" in contents
        assert "fs_list" in contents

    @patch("cli_agent_orchestrator.providers.cursor_cli.load_agent_profile")
    def test_wildcard_allowed_tools_skips_security_prompt(self, mock_load):
        # Unrestricted yolo mode: SECURITY_PROMPT is not prepended.
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
        # Wildcard → no security prompt in the system-prompt file.
        m = re.search(r"--system-prompt\s+(\S+)", cmd)
        assert m is not None
        from pathlib import Path
        contents = Path(m.group(1)).read_text(encoding="utf-8")
        assert "SECURITY CONSTRAINTS" not in contents

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
    """

    def test_prefers_agent_when_both_available(self):
        # The autouse fixture already returns '/usr/bin/agent' for
        # shutil.which. Confirm the resulting command starts with
        # the primary name.
        with patch("cli_agent_orchestrator.providers.cursor_cli.shutil.which") as mock_which:
            mock_which.side_effect = lambda name: ("/usr/bin/agent" if name == "agent" else None)
            with patch(
                "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                side_effect=FileNotFoundError("no profile"),
            ):
                provider = make_provider()
                cmd = provider._build_cursor_command()
        assert cmd.startswith("agent ")

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
        assert sent.startswith("agent ")
        assert "--force" in sent
        # v2026+ rejects --trust in interactive REPL mode. See
        # issue #299. The launch command is "agent --force" when
        # no profile / model / system-prompt is configured.
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
    async def test_initialize_sends_system_prompt_file(self, mock_backend, mock_shell, mock_wait):
        # v2026 dropped ``--agent``; the CAO profile is now carried
        # in the file passed to ``--system-prompt``. Assert the
        # launched command references a system-prompt file path.
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(agent_profile="developer")
        await provider.initialize()
        sent = mock_backend.return_value.send_keys.call_args.args[2]
        assert "--agent" not in sent
        assert "--system-prompt" in sent

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
