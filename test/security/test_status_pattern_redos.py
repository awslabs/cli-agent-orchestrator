"""Backtracking budget + behaviour guards for the provider status regexes.

Provider status detection runs untrusted text: everything an agent CLI paints on
its terminal reaches these patterns through ``strip_terminal_escapes``, and an
agent decides what it paints. Six of them held a quantifier that could be
re-entered at every position of a repeated prefix, so a screenful of the right
filler cost quadratic time in the buffer length instead of linear (CWE-1333,
py/polynomial-redos). Measured on this box, at 50k characters:

    _COMPLETION_PATTERN            3451 ms -> 2 ms
    _ERROR_PATTERN                24481 ms -> 4 ms
    _PROCESSING_PATTERN            1738 ms -> 0.5 ms
    PROMPT_WITH_INPUT_PATTERN      4815 ms -> 0.4 ms
    NEW_TUI_STATUS_PATTERN          234 ms -> 6 ms
    TUI_FOOTER_PATTERN             4753 ms -> 2 ms

Four more patterns were reported by the same rule but were never slow in CPython
— `re.match` anchored them, or the ambiguity existed only under an ASCII-only
reading of \\s/\\S. They were tightened for precision and they get budget tests
too, so a later rewrite into a genuinely unanchored form is caught.

Two kinds of test live here, and both are load-bearing:

* the budget tests below feed each pattern its own worst case at a size where the
  old form took seconds and the new form takes milliseconds. The threshold is
  deliberately ~100x the observed cost so a busy CI box cannot flake it; a
  genuine regression to quadratic misses it by two orders of magnitude, not by a
  margin.
* the recognition tests pin the strings each pattern EXISTS to match. Every fix
  here narrowed a class or bounded a repetition, and the way to "fix" a ReDoS
  while silently breaking a provider is to narrow it too far — status detection
  would then read IDLE forever and the provider would time out at init with no
  regex test to show why.
"""

import re
import time

import pytest

from cli_agent_orchestrator.providers import codex, kimi_cli, minimax_code
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

# Large enough that the pre-fix patterns took seconds, small enough to stay
# instant when the matching is linear.
PATHOLOGICAL_LENGTH = 200_000

# ~100x the post-fix cost of the slowest case measured above.
BUDGET_SECONDS = 2.0


def assert_within_budget(label: str, probe, text: str) -> None:
    """Fail if ``probe(text)`` does not finish inside the backtracking budget."""
    start = time.perf_counter()
    probe(text)
    elapsed = time.perf_counter() - start
    assert elapsed < BUDGET_SECONDS, (
        f"{label} took {elapsed:.2f}s on {len(text)} characters of its own worst-case "
        f"filler (budget {BUDGET_SECONDS}s). That is the signature of a quantifier that "
        f"can be re-entered at every position — see CWE-1333 / py/polynomial-redos."
    )


class TestBacktrackingBudget:
    """Each pattern's own worst-case filler must stay linear."""

    def test_completion_pattern_on_blank_lines(self):
        assert_within_budget(
            "minimax _COMPLETION_PATTERN",
            minimax_code._COMPLETION_PATTERN.search,
            "\n" * PATHOLOGICAL_LENGTH,
        )

    def test_error_pattern_on_blank_lines(self):
        assert_within_budget(
            "minimax _ERROR_PATTERN",
            minimax_code._ERROR_PATTERN.search,
            "\n" * PATHOLOGICAL_LENGTH,
        )

    def test_processing_scan_on_spinner_flood(self):
        # A single line carrying nothing but spinner heads: the old combined
        # pattern re-walked the rest of the line from every one of them.
        assert_within_budget(
            "minimax _last_processing_start",
            minimax_code._last_processing_start,
            "◆ Loading 0s " * (PATHOLOGICAL_LENGTH // 13),
        )

    def test_prompt_with_input_on_word_characters(self):
        assert_within_budget(
            "kimi PROMPT_WITH_INPUT_PATTERN",
            lambda text: re.search(kimi_cli.PROMPT_WITH_INPUT_PATTERN, text),
            "0" * PATHOLOGICAL_LENGTH,
        )

    def test_new_tui_status_on_agent_paren_flood(self):
        assert_within_budget(
            "kimi NEW_TUI_STATUS_PATTERN",
            lambda text: re.search(kimi_cli.NEW_TUI_STATUS_PATTERN, text),
            "agent(" * (PATHOLOGICAL_LENGTH // 6),
        )

    def test_tui_footer_on_digits(self):
        assert_within_budget(
            "codex TUI_FOOTER_PATTERN",
            lambda text: re.search(codex.TUI_FOOTER_PATTERN, text),
            "0" * PATHOLOGICAL_LENGTH,
        )

    def test_update_dialog_on_padding_whitespace(self):
        assert_within_budget(
            "codex UPDATE_DIALOG_PATTERN",
            lambda text: re.search(codex.UPDATE_DIALOG_PATTERN, text),
            "Update available!\t" + "\xa0" * PATHOLOGICAL_LENGTH,
        )

    def test_bullet_line_on_indent_whitespace(self):
        assert_within_budget(
            "kimi BULLET_LINE_PATTERN",
            kimi_cli.BULLET_LINE_PATTERN.match,
            "\t" * PATHOLOGICAL_LENGTH,
        )

    def test_osc_strip_on_escape_flood(self):
        assert_within_budget(
            "strip_terminal_escapes (OSC)",
            strip_terminal_escapes,
            "\x1b]" * (PATHOLOGICAL_LENGTH // 2),
        )


class TestStillRecognisesRealOutput:
    """The narrowed patterns must still match what providers actually render."""

    @pytest.mark.parametrize(
        "line",
        [
            "└ Completed in 3s",
            "  └ Completed in 12.4s",
            "\n\n   └ completed in 1s",
        ],
    )
    def test_completion_pattern_matches(self, line):
        assert minimax_code._COMPLETION_PATTERN.search(line) is not None

    def test_completion_pattern_still_requires_line_start(self):
        assert minimax_code._COMPLETION_PATTERN.search("tail └ Completed in 3s") is None

    @pytest.mark.parametrize(
        "text",
        ["Error: boom", "  Fatal: boom", "prior line\n  Error: boom", "Sign in required"],
    )
    def test_error_pattern_matches(self, text):
        assert minimax_code._ERROR_PATTERN.search(text) is not None

    def test_error_pattern_ignores_mid_line_error_label(self):
        assert minimax_code._ERROR_PATTERN.search("see Error: in the log") is None

    @pytest.mark.parametrize(
        "text",
        [
            "◆ Loading  Enter queue · Esc stop",
            "◇ Running 12s ──────────  Enter queue",
            "⠹ Loading 3s   Esc stop",
            "banner\n◆ Running 1s  Enter queue\ntail",
        ],
    )
    def test_processing_scan_finds_spinner(self, text):
        assert minimax_code._last_processing_start(text) >= 0

    @pytest.mark.parametrize(
        "text",
        [
            "◆ Running 12s",  # spinner without the footer hint
            "Enter queue · Esc stop",  # footer hint without a spinner
            "◆ Running 12s\nEnter queue",  # hint on a different line
        ],
    )
    def test_processing_scan_rejects_partial_frames(self, text):
        assert minimax_code._last_processing_start(text) == -1

    def test_processing_scan_reports_the_newest_frame(self):
        text = "◆ Loading 1s Enter queue\nfiller\n◆ Running 2s Esc stop"
        assert minimax_code._last_processing_start(text) == text.rindex("◆")

    @pytest.mark.parametrize("text", ["💫 hello", "✨ do the thing", "user@host:~$ 💫 hello"])
    def test_prompt_with_input_matches(self, text):
        assert re.search(kimi_cli.PROMPT_WITH_INPUT_PATTERN, text) is not None

    @pytest.mark.parametrize("text", ["💫 ", "💫", "💫\nnext line"])
    def test_prompt_with_input_rejects_empty_prompt(self, text):
        assert re.search(kimi_cli.PROMPT_WITH_INPUT_PATTERN, text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "yolo  agent (kimi-k2-thinking ●)  ctrl-x: toggle mode",
            "agent(kimi-k2 ●)",
            "context: 12.3%",
            "context:0%",
        ],
    )
    def test_new_tui_status_matches(self, text):
        assert re.search(kimi_cli.NEW_TUI_STATUS_PATTERN, text) is not None

    def test_new_tui_status_rejects_agent_without_the_dot(self):
        assert re.search(kimi_cli.NEW_TUI_STATUS_PATTERN, "agent (kimi-k2)") is None

    @pytest.mark.parametrize("line", ["• thinking", "  • response", "\t• response"])
    def test_bullet_line_matches(self, line):
        assert kimi_cli.BULLET_LINE_PATTERN.match(line) is not None

    def test_bullet_line_rejects_mid_line_bullet(self):
        assert kimi_cli.BULLET_LINE_PATTERN.match("text • more") is None

    @pytest.mark.parametrize(
        "line",
        [
            "? for shortcuts",
            "23% left",
            "100% left",
            "0% left",
            "gpt-6-astra · 41% left · ~/code",
            "context left",
            "gpt-6-astra · ~/code",
        ],
    )
    def test_tui_footer_matches(self, line):
        assert re.search(codex.TUI_FOOTER_PATTERN, line) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "Update available! 0.142.5 -> 0.144.5",
            "✨ Update available! 0.142.5 -> 0.144.5",
            "Update available! v1.2.3-beta.1 -> v1.2.4",
            "Update available!\xa00.142.5\xa0->\xa00.144.5",
        ],
    )
    def test_update_dialog_matches(self, text):
        assert re.search(codex.UPDATE_DIALOG_PATTERN, text) is not None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("\x1b]0;window title\x07visible", "visible"),
            ("\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\", "link"),
            ("plain", "plain"),
        ],
    )
    def test_osc_sequences_are_stripped(self, raw, expected):
        assert strip_terminal_escapes(raw) == expected
