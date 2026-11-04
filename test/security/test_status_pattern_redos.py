"""Load-independent ReDoS guards + behaviour guards for the #771 status regexes.

Provider status detection runs untrusted text: everything an agent CLI paints on
its terminal reaches these patterns through ``strip_terminal_escapes``, and an
agent decides what it paints. Six of them held a quantifier that could be
re-entered at every position of a repeated prefix, so a screenful of the right
filler cost quadratic time in the buffer length instead of linear (CWE-1333,
py/polynomial-redos). #771 fixed them:

    minimax _COMPLETION_PATTERN    (?:^|\\n)\\s*      -> (?:^|\\n)[^\\S\\n]*
    minimax _ERROR_PATTERN         the same
    minimax _PROCESSING_PATTERN    [^\\n]* gap       -> head + hint, once per line
    kimi PROMPT_WITH_INPUT_PATTERN optional \\w+@ prefix dropped
    kimi NEW_TUI_STATUS_PATTERN    [^)]*            -> [^)●]{0,80}
    codex TUI_FOOTER_PATTERN       \\d+%            -> \\d{1,3}%

Four more patterns were reported by the same rule but were never slow in
CPython: ``re.match`` anchored them, or the ambiguity existed only under an
ASCII-only reading of \\s/\\S. They were tightened for precision and are guarded
here too, so a later rewrite into a genuinely unanchored form is caught.

Three kinds of test live here, and all are load-bearing:

* the structure tests run ``structural_issues`` (test/security/redos_analysis.py)
  over each pattern's parse tree: no nested or ambiguous repeats, no two repeats
  that can split the same characters, and no repeat that rescans characters a
  later search attempt starts from;
* the growth tests count backtracking steps at n, 4n and 16n repetitions of
  inputs generated from the pattern's own structure. The current form must grow
  linearly on every one of them, and the pre-#771 form must grow superlinearly
  on at least one: that is what shows the test can fail. Nothing here measures
  seconds, so a loaded CI box cannot flake it (the budget test it replaces used
  a 2 s wall-clock ceiling);
* the recognition tests pin the strings each pattern EXISTS to match. Every fix
  here narrowed a class or bounded a repetition, and the way to "fix" a ReDoS
  while silently breaking a provider is to narrow it too far — status detection
  would then read IDLE forever and the provider would time out at init with no
  regex test to show why.
"""

import re
from dataclasses import dataclass
from test.security.redos_analysis import (
    first_superlinear,
    input_shapes,
    instrumented_steps,
    nonlinear_shapes,
    pattern_probe,
    structural_issues,
)
from typing import Optional, Union

import pytest

from cli_agent_orchestrator.providers import codex, kimi_cli, minimax_code
from cli_agent_orchestrator.utils import text as text_utils
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

# The forms before #771 (3fadf383^), so every growth check is shown to fail on them.
OLD_COMPLETION = r"(?:^|\n)\s*└\s+Completed in\s+\d"
OLD_ERROR = r"Sign in required|Authentication failed|Not authenticated|(?:^|\n)\s*(?:Fatal|Error):"
OLD_PROCESSING = r"[⠁-⣿◇◆]\s+(?:Loading|Running)(?:\s+\d+s)?[^\n]*(?:Enter queue|Esc stop)"
OLD_PROMPT_WITH_INPUT = r"(?:\w+@[\w.-]+)?[✨💫][^\S\n]+\S"
OLD_NEW_TUI_STATUS = r"context:\s*\d+(?:\.\d+)?%|agent\s*\([^)]*●"
OLD_TUI_FOOTER = r"(?:\?\s+for shortcuts|context left|\d+%\s+left|·\s+[~/])"


@dataclass(frozen=True)
class Case:
    """One pattern, how callers run it, and its pre-#771 form if it was ever slow."""

    label: str
    pattern: Union[str, "re.Pattern[str]"]
    mode: str
    flags: int = 0
    old: Optional[str] = None
    old_flags: int = 0

    def __str__(self) -> str:
        return self.label


CASES = [
    Case(
        "minimax _COMPLETION_PATTERN",
        minimax_code._COMPLETION_PATTERN,
        "finditer",
        old=OLD_COMPLETION,
        old_flags=re.IGNORECASE,
    ),
    Case(
        "minimax _ERROR_PATTERN",
        minimax_code._ERROR_PATTERN,
        "finditer",
        old=OLD_ERROR,
        old_flags=re.IGNORECASE,
    ),
    Case("minimax _PROCESSING_HEAD_PATTERN", minimax_code._PROCESSING_HEAD_PATTERN, "search"),
    Case("minimax _PROCESSING_HINT_PATTERN", minimax_code._PROCESSING_HINT_PATTERN, "search"),
    Case(
        "kimi PROMPT_WITH_INPUT_PATTERN",
        kimi_cli.PROMPT_WITH_INPUT_PATTERN,
        "search",
        old=OLD_PROMPT_WITH_INPUT,
    ),
    Case(
        "kimi NEW_TUI_STATUS_PATTERN",
        kimi_cli.NEW_TUI_STATUS_PATTERN,
        "search",
        old=OLD_NEW_TUI_STATUS,
    ),
    Case("codex TUI_FOOTER_PATTERN", codex.TUI_FOOTER_PATTERN, "search", old=OLD_TUI_FOOTER),
    # Never slow in CPython; guarded so a rewrite into a slow form is caught.
    Case("codex UPDATE_DIALOG_PATTERN", codex.UPDATE_DIALOG_PATTERN, "search"),
    Case("kimi BULLET_LINE_PATTERN", kimi_cli.BULLET_LINE_PATTERN, "match"),
]
SLOW_BEFORE = [case for case in CASES if case.old is not None]


def _shapes(case: Case) -> list[tuple[str, str]]:
    shapes = set(input_shapes(case.pattern, case.flags))
    if case.old is not None:
        shapes |= set(input_shapes(case.old, case.old_flags))
    return sorted(shapes)


class TestStructure:
    """No pattern holds a shape that makes backtracking superlinear."""

    @pytest.mark.parametrize("case", CASES, ids=str)
    def test_current_form_is_clean(self, case):
        assert structural_issues(case.pattern, case.mode, case.flags) == []

    @pytest.mark.parametrize("case", SLOW_BEFORE, ids=str)
    def test_pre_fix_form_is_flagged(self, case):
        assert structural_issues(case.old, case.mode, case.old_flags)


class TestStepGrowth:
    """Backtracking work grows linearly, measured in steps rather than seconds."""

    @pytest.mark.parametrize("case", CASES, ids=str)
    def test_current_form_grows_linearly(self, case):
        probe = pattern_probe(case.pattern, case.mode, case.flags)
        assert nonlinear_shapes(probe, _shapes(case)) == []

    @pytest.mark.parametrize("case", SLOW_BEFORE, ids=str)
    def test_pre_fix_form_grows_superlinearly(self, case):
        probe = pattern_probe(case.old, case.mode, case.old_flags)
        assert first_superlinear(probe, input_shapes(case.old, case.old_flags)) is not None

    def test_processing_scan_grows_linearly(self):
        # _last_processing_start runs a head search and a hint search per line;
        # count the steps of both, on inputs grown from the old combined pattern.
        def probe(text: str) -> int:
            return instrumented_steps(
                minimax_code._last_processing_start,
                text,
                {minimax_code: ["_PROCESSING_HEAD_PATTERN", "_PROCESSING_HINT_PATTERN"]},
            )

        shapes = set(input_shapes(OLD_PROCESSING, re.IGNORECASE))
        shapes |= set(input_shapes(minimax_code._PROCESSING_HEAD_PATTERN))
        assert nonlinear_shapes(probe, sorted(shapes)) == []

    def test_pre_fix_processing_pattern_grows_superlinearly(self):
        probe = pattern_probe(OLD_PROCESSING, "finditer", re.IGNORECASE)
        assert first_superlinear(probe, input_shapes(OLD_PROCESSING, re.IGNORECASE)) is not None

    def test_osc_strip_grows_linearly(self):
        def probe(text: str) -> int:
            return instrumented_steps(
                strip_terminal_escapes,
                text,
                {
                    text_utils: [
                        "_LINE_START_CSI",
                        "_FORWARD_CURSOR_CSI",
                        "_CSI_PATTERN",
                        "_OSC_PATTERN",
                        "_CONTROL_CHARS_PATTERN",
                    ]
                },
            )

        assert nonlinear_shapes(probe, input_shapes(text_utils._OSC_PATTERN)) == []


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
