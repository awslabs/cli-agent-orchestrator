"""ReDoS guards for Claude Code's status and extraction patterns (CWE-1333), tier 1.

Four patterns ran over the whole status buffer on every poll, or over the whole
capture during extraction, and backtracked superlinearly:

* ``NEW_TUI_BOX_PATTERN`` restarted at every "─" of a rule row and tried every
  split of the row between ``─{8,}`` and ``[^\\n]*``: cubic in one long rule,
  and ~0.2 s per poll on an ordinary buffer of full-width rules;
* ``GET_STATUS_COMPLETION_PATTERN`` and ``PROCESSING_PATTERN`` restarted at
  every spinner glyph and re-scanned the rest of the line: quadratic;
* ``EXTRACTION_RESPONSE_PATTERN`` re-ran its effort-footer lookahead once per
  SGR code the run after the marker gave back: quadratic in the run.

Each fix is checked without a clock: the structure check, step growth at n, 4n
and 16n on inputs generated from the OLD pattern's structure (the old form must
grow superlinearly, the new one linearly), and a differential test that the new
form reports exactly the old matches -- spans, text and existence.
"""

import random
import re
from test.security.redos_analysis import (
    first_superlinear,
    input_shapes,
    instrumented_steps,
    nonlinear_shapes,
    pattern_probe,
    structural_issues,
)

import pytest

from cli_agent_orchestrator.providers import claude_code as cc

OLD_PROCESSING = r"[✶✢✽✻✳·*].*…"
OLD_GET_STATUS_COMPLETION = r"[✶✢✽✻✳][^\n…]*\bfor\b"
OLD_NEW_TUI_BOX = (
    r"─{8,}[^\n]*\n(?:[ \t\xa0]*\n){0,2}[ \t]*[>❯][ \t\xa0]*\n(?:[ \t\xa0]*\n){0,2}[ \t]*─{8,}"
)
_OLD_ANSI = r"(?:\x1b\[[0-9;]*m)*"
_OLD_TAIL = (
    _OLD_ANSI + r"\w+" + _OLD_ANSI + r"[ \t]*·[ \t]*" + _OLD_ANSI + r"/effort" + _OLD_ANSI
) + r"[ \t]*$"
OLD_EXTRACTION_RESPONSE = (
    r"^[ \t]*(?:\x1b\[[0-9;]*m)*[⏺●](?:\x1b\[[0-9;]*m)*(?![ \t]*" + _OLD_TAIL + r")\s+"
)
OLD_EFFORT_FOOTER_LINE = r"^[ \t]*[⏺●][ \t]*" + _OLD_TAIL
OLD_COMPLETION_SUMMARY = r"[✶✢✽✻✳][^\n…]*\bfor\s+\d+(?:\.\d+)?\s*s\b"
OLD_AFTER_SUMMARY_SPINNER = r"[✶✢✽✻✳·*][ \t]+" + cc._SPINNER_VERB + r"[^\n]*…"
OLD_SEPARATOR_WALK_SPINNER = r"[✶✢✽✻✳·][^\n]*\u2026"
OLD_REDUCED_MOTION_SPINNER = (
    r"^[ \t]*●[ \t]+[^\n]*…[ \t]+\("
    r"(?:running[ \t][^)\n]*\bhooks?\b"
    r"|\d+(?:\.\d+)?[smhd]\b"
    r"|(?:[↓↑][ \t]*)?[\d.,]+[kmb]?[ \t]+tokens\b"
    r"|thinking\b|thought[ \t]+for\b"
    r"|esc[ \t]+to[ \t]+interrupt\b)"
)
OLD_COMPOSER_PROMPT = r"^[ \t]*[>❯](?:[ \t\xa0]+(.*)|[ \t\xa0]*)$"

# (label, current pattern, old pattern, old flags, how callers run it)
PATTERNS = [
    ("PROCESSING_PATTERN", cc.PROCESSING_PATTERN, OLD_PROCESSING, 0, "finditer"),
    (
        "GET_STATUS_COMPLETION_PATTERN",
        cc.GET_STATUS_COMPLETION_PATTERN,
        OLD_GET_STATUS_COMPLETION,
        0,
        "finditer",
    ),
    ("NEW_TUI_BOX_PATTERN", cc.NEW_TUI_BOX_PATTERN, OLD_NEW_TUI_BOX, re.MULTILINE, "search"),
    (
        "EXTRACTION_RESPONSE_PATTERN",
        cc.EXTRACTION_RESPONSE_PATTERN,
        OLD_EXTRACTION_RESPONSE,
        re.MULTILINE,
        "finditer",
    ),
    (
        "COMPLETION_SUMMARY_PATTERN",
        cc.COMPLETION_SUMMARY_PATTERN,
        OLD_COMPLETION_SUMMARY,
        0,
        "search",
    ),
    (
        "AFTER_SUMMARY_SPINNER_PATTERN",
        cc.AFTER_SUMMARY_SPINNER_PATTERN,
        OLD_AFTER_SUMMARY_SPINNER,
        0,
        "search",
    ),
    (
        "SEPARATOR_WALK_SPINNER_PATTERN",
        cc.SEPARATOR_WALK_SPINNER_PATTERN,
        OLD_SEPARATOR_WALK_SPINNER,
        0,
        "search",
    ),
    (
        "REDUCED_MOTION_SPINNER_PATTERN",
        cc.REDUCED_MOTION_SPINNER_PATTERN,
        OLD_REDUCED_MOTION_SPINNER,
        re.MULTILINE,
        "search",
    ),
]
IDS = [p[0] for p in PATTERNS]


def _random_texts(seed: int, tokens: list[str], count: int, length: int) -> list[str]:
    rng = random.Random(seed)
    return [
        "".join(rng.choice(tokens) for _ in range(rng.randrange(0, length))) for _ in range(count)
    ]


class TestStructureAndGrowth:
    @pytest.mark.parametrize("label,new,old,old_flags,mode", PATTERNS, ids=IDS)
    def test_current_form_is_clean(self, label, new, old, old_flags, mode):
        assert structural_issues(new, mode) == []

    @pytest.mark.parametrize("label,new,old,old_flags,mode", PATTERNS, ids=IDS)
    def test_old_form_is_flagged_and_grows_superlinearly(self, label, new, old, old_flags, mode):
        assert structural_issues(old, mode, old_flags)
        probe = pattern_probe(old, mode, old_flags)
        assert first_superlinear(probe, input_shapes(old, old_flags)) is not None

    @pytest.mark.parametrize("label,new,old,old_flags,mode", PATTERNS, ids=IDS)
    def test_current_form_grows_linearly(self, label, new, old, old_flags, mode):
        shapes = sorted(set(input_shapes(old, old_flags)) | set(input_shapes(new)))
        assert nonlinear_shapes(pattern_probe(new, mode), shapes) == []

    def test_effort_footer_line_pattern_is_clean(self):
        # Shares the possessive _EFFORT_FOOTER_TAIL with EXTRACTION_RESPONSE_PATTERN.
        assert structural_issues(cc.EFFORT_FOOTER_LINE_PATTERN, "finditer") == []

    def test_box_scan_grows_linearly(self):
        def probe(text: str) -> int:
            return instrumented_steps(
                lambda t: list(cc._iter_new_tui_boxes(t)),
                text,
                {cc: ["_NEW_TUI_BOX_RULE_RUN", "_NEW_TUI_BOX_BELOW_TOP_RE"]},
            )

        shapes = sorted(
            set(input_shapes(OLD_NEW_TUI_BOX, re.MULTILINE))
            | set(input_shapes(cc._NEW_TUI_BOX_BELOW_TOP_RE))
        )
        assert nonlinear_shapes(probe, shapes) == []


class TestSameMatchesAsBefore:
    SPINNER_TOKENS = ["✶", "✻", "✳", "·", "*", "a", " ", "…", "\n", "for", " for ", "Waiting"]

    def test_processing_spinner_spans_match_the_old_pattern(self):
        for text in _random_texts(974, self.SPINNER_TOKENS, 5000, 30):
            old = [m.span() for m in re.finditer(OLD_PROCESSING, text)]
            new = [
                (m.start(), m.end()) for m in cc._iter_spinner_matches(cc.PROCESSING_PATTERN, text)
            ]
            assert new == old, text
            # The busy-pattern guard and existence checks see the same verdict.
            flags = re.MULTILINE | re.IGNORECASE
            assert bool(re.search(cc.PROCESSING_PATTERN, text, flags)) == bool(
                re.search(OLD_PROCESSING, text, flags)
            )

    def test_completion_summary_spans_and_text_match_the_old_pattern(self):
        for text in _random_texts(392, self.SPINNER_TOKENS, 5000, 30):
            old = [(m.span(), m.group(0)) for m in re.finditer(OLD_GET_STATUS_COMPLETION, text)]
            new = [
                ((m.start(), m.end()), m.group(0))
                for m in cc._iter_spinner_matches(cc.GET_STATUS_COMPLETION_PATTERN, text)
            ]
            assert new == old, text
            assert bool(re.search(cc.GET_STATUS_COMPLETION_PATTERN, text)) == bool(
                re.search(OLD_GET_STATUS_COMPLETION, text)
            )

    def test_input_box_spans_and_existence_match_the_old_pattern(self):
        old_re = re.compile(OLD_NEW_TUI_BOX, re.MULTILINE)
        tokens = ["─" * 8, "─" * 20, "─" * 3, "─", " ", "\t", "\xa0", "\n", "❯", ">", "x"]
        for text in _random_texts(459, tokens, 20000, 30):
            assert list(cc._iter_new_tui_boxes(text)) == [m.span() for m in old_re.finditer(text)]
            assert bool(cc.NEW_TUI_BOX_PATTERN.search(text)) == bool(old_re.search(text))

    def test_response_marker_spans_match_the_old_patterns(self):
        old_response = re.compile(OLD_EXTRACTION_RESPONSE, re.MULTILINE)
        old_footer = re.compile(OLD_EFFORT_FOOTER_LINE, re.MULTILINE)
        tokens = [
            "⏺",
            "●",
            " ",
            "\t",
            "\n",
            "\x1b[m",
            "\x1b[38;5;246m",
            "\x1b[39m",
            "high",
            "·",
            "/effort",
            "x",
        ]
        for text in _random_texts(908, tokens, 20000, 25):
            assert [m.span() for m in cc.EXTRACTION_RESPONSE_PATTERN.finditer(text)] == [
                m.span() for m in old_response.finditer(text)
            ], repr(text)
            assert [m.span() for m in cc.EFFORT_FOOTER_LINE_PATTERN.finditer(text)] == [
                m.span() for m in old_footer.finditer(text)
            ], repr(text)


_RULE = "─" * 12
#: Input-box layouts with the spans the old ``NEW_TUI_BOX_PATTERN.finditer`` gave.
#: Pinned literally (not only compared with the old pattern) so a broken resume
#: position or start offset in ``_iter_new_tui_boxes`` fails here by name.
BOX_LAYOUTS = {
    "single box": (f"{_RULE}\n❯ \n{_RULE}\n", [(0, 28)]),
    "box filling the buffer, no trailing newline": (f"{_RULE}\n❯ \n{_RULE}", [(0, 28)]),
    "box after reply text": (f"reply text\n{_RULE}\n❯ \n{_RULE}\n", [(11, 39)]),
    "stacked boxes sharing a rule": (f"{_RULE}\n❯ \n{_RULE}\n❯ \n{_RULE}\n", [(0, 28)]),
    "adjacent boxes": (f"{_RULE}\n❯ \n{_RULE}\n{_RULE}\n❯ \n{_RULE}\n", [(0, 28), (29, 57)]),
    "two blank rows above the prompt": (f"{_RULE}\n\n\n❯ \n{_RULE}\n", [(0, 30)]),
    "three blank rows above the prompt": (f"{_RULE}\n\n\n\n❯ \n{_RULE}\n", []),
    "two blank rows below the prompt": (f"{_RULE}\n❯ \n\n\n{_RULE}\n", [(0, 30)]),
    "three blank rows below the prompt": (f"{_RULE}\n❯ \n\n\n\n{_RULE}\n", []),
    "NBSP and tab blank rows": (f"{_RULE}\n\xa0\n \t\n❯\xa0\n\xa0\n{_RULE}\n", [(0, 35)]),
    "CRLF rows": (f"{_RULE}\r\n❯ \r\n{_RULE}\r\n", []),
    "second rule run on the bottom line": (
        f"{_RULE}\n❯ \n{_RULE}  {_RULE}\n❯ \n{_RULE}\n",
        [(0, 28), (30, 58)],
    ),
    "rule with text around it": (f"text {_RULE} more\n❯ \n  {_RULE}\n", [(5, 40)]),
    "top rule with no newline": (f"text\n{_RULE}", []),
    "partial box, no bottom rule": (f"{_RULE}\n❯ \nreply\n", []),
    "seven-wide rule is not a rule": ("─" * 7 + f"\n❯ \n{_RULE}\n", []),
    "prompt holding text is not an empty box": (f"{_RULE}\n❯ hello\n{_RULE}\n", []),
}


class TestInputBoxSpans:
    @pytest.mark.parametrize("text,expected", BOX_LAYOUTS.values(), ids=BOX_LAYOUTS.keys())
    def test_scan_reports_the_pinned_spans(self, text, expected):
        assert list(cc._iter_new_tui_boxes(text)) == expected
        assert [m.span() for m in re.finditer(OLD_NEW_TUI_BOX, text, re.MULTILINE)] == expected

    @pytest.mark.parametrize("text,expected", BOX_LAYOUTS.values(), ids=BOX_LAYOUTS.keys())
    def test_search_group_is_the_first_box(self, text, expected):
        match = cc.NEW_TUI_BOX_PATTERN.search(text)
        assert (match.span(1) if match else None) == (expected[0] if expected else None)

    def test_spans_match_the_old_pattern_on_random_screens(self):
        # Built from whole rows, so boxes, stacked boxes and near-misses are common.
        old_re = re.compile(OLD_NEW_TUI_BOX, re.MULTILINE)
        rows = [
            "─" * 7,
            "─" * 8,
            "─" * 9,
            _RULE,
            f"{_RULE}  {_RULE}",
            f"x {_RULE}",
            "",
            " ",
            "\xa0",
            " \t",
            "❯ ",
            "> ",
            "❯\xa0",
            "  ❯",
            "❯ hi",
            "reply",
        ]
        rng = random.Random(1325)
        for _ in range(20000):
            sep = "\r\n" if rng.random() < 0.1 else "\n"
            text = sep.join(rng.choice(rows) for _ in range(rng.randrange(0, 12)))
            if rng.random() < 0.5:
                text += sep
            spans = [m.span() for m in old_re.finditer(text)]
            assert list(cc._iter_new_tui_boxes(text)) == spans, repr(text)
            match = cc.NEW_TUI_BOX_PATTERN.search(text)
            assert (match.span(1) if match else None) == (spans[0] if spans else None), repr(text)


def _same_verdicts(new, old: str, old_flags: int, texts: list[str]) -> None:
    old_re = re.compile(old, old_flags)
    for text in texts:
        assert bool(new.search(text)) == bool(old_re.search(text)), repr(text)


class TestSpinnerAndSummaryVerdicts:
    """These four are only ever asked whether they match: the verdict must not move."""

    GLYPHS = ["✶", "✢", "✽", "✻", "✳", "·", "*"]

    def test_completion_summary(self):
        tokens = self.GLYPHS + ["for", " for ", "3", "3.5", "s", " ", "\t", "…", "\n", "x"]
        new = re.compile(cc.COMPLETION_SUMMARY_PATTERN)
        _same_verdicts(new, OLD_COMPLETION_SUMMARY, 0, _random_texts(1886, tokens, 20000, 25))

    def test_after_summary_spinner(self):
        tokens = self.GLYPHS + [" ", "\t", "Thinking", "ing", "Dilly-dallying", "Beboppin'"]
        tokens += ["in’", "…", "\n", "x", "-", "'"]
        _same_verdicts(
            cc.AFTER_SUMMARY_SPINNER_PATTERN,
            OLD_AFTER_SUMMARY_SPINNER,
            0,
            _random_texts(975, tokens, 20000, 20),
        )

    def test_separator_walk_spinner(self):
        tokens = self.GLYPHS + ["…", "a", " ", "\n"]
        _same_verdicts(
            cc.SEPARATOR_WALK_SPINNER_PATTERN,
            OLD_SEPARATOR_WALK_SPINNER,
            0,
            _random_texts(1218, tokens, 20000, 25),
        )

    def test_reduced_motion_spinner(self):
        tokens = ["●", " ", "\t", "…", " (", "(", "running ", "running\t", "Stop", " hook"]
        tokens += ["hooks", ")", "12s", "1.5m", "↓", " 300 tokens", "300k tokens", "thinking"]
        tokens += ["thought for", "esc to interrupt", "x", "\n"]
        _same_verdicts(
            cc.REDUCED_MOTION_SPINNER_PATTERN,
            OLD_REDUCED_MOTION_SPINNER,
            re.MULTILINE,
            _random_texts(908, tokens, 20000, 25),
        )

    def test_real_reduced_motion_rows_still_match(self):
        for row in (
            "● Leavening… (running UserPromptSubmit hook)",
            "● Sublimating… (12s · ↓ 300 tokens)",
            "● Running the test suite… (1m 3s · thinking)",
            "● Beboppin'… (esc to interrupt)",
            "● Waiting… (running Stop… hooks)",
        ):
            assert cc.REDUCED_MOTION_SPINNER_PATTERN.search(row), row
        for row in (
            "● Analyzing… the diff is fine",
            "● Calling cao-mcp-server… (ctrl+o to expand)",
        ):
            assert not cc.REDUCED_MOTION_SPINNER_PATTERN.search(row), row


class TestComposerPromptRow:
    def test_current_form_is_clean(self):
        assert structural_issues(cc.ClaudeCodeProvider._COMPOSER_PROMPT_RE, "match") == []

    def test_match_and_text_are_unchanged(self):
        old = re.compile(OLD_COMPOSER_PROMPT)
        new = cc.ClaudeCodeProvider._COMPOSER_PROMPT_RE
        tokens = [" ", "\t", "\xa0", "❯", ">", "x", "hi", "\n"]
        for row in _random_texts(1842, tokens, 20000, 12):
            a, b = old.match(row), new.match(row)
            assert (a and (a.span(), a.group(1))) == (b and (b.span(), b.group(1))), repr(row)
