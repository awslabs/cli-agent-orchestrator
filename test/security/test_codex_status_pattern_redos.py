"""ReDoS guards for Codex's status and extraction patterns (CWE-1333), tier 1.

Two patterns backtracked superlinearly on agent-controlled text:

* ``IDLE_PROMPT_STRICT_PATTERN`` (``^\\s*(?:❯|›|»|codex>)\\s*$``) is searched
  with re.MULTILINE over the response during extraction. Every line start in a
  run of blank lines began an attempt whose ``\\s*`` re-scanned the rest of the
  run: quadratic in the run. The indent is now horizontal whitespace only, and
  ``_strict_idle_prompt_start`` recovers the old start offset;
* ``TUI_PROGRESS_PATTERN`` restarted at every bullet glyph on a line and
  re-scanned the rest of the line for the spinner status: quadratic. It is now
  anchored at the line start and skips to the line's first glyph.

Each fix is checked without a clock: the structure check, step growth at n, 4n
and 16n on inputs generated from the OLD pattern's structure (the old form must
grow superlinearly, the new one linearly), and a differential test against the
old pattern.
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

from cli_agent_orchestrator.providers import codex

OLD_IDLE_PROMPT_STRICT = r"^\s*(?:❯|›|»|codex>)\s*$"
OLD_TUI_PROGRESS = r"[•◦][^\n]*\((?:(?:\d+h\s+)?\d+m\s+)?\d+s\s*•\s*esc to interrupt\)"


def _random_texts(seed: int, tokens: list[str], count: int, length: int) -> list[str]:
    rng = random.Random(seed)
    return [
        "".join(rng.choice(tokens) for _ in range(rng.randrange(0, length))) for _ in range(count)
    ]


class TestIdlePromptStrict:
    TOKENS = ["\n", " ", "\t", "\xa0", "\r", "❯", "›", "»", "codex>", "x", "› hi"]

    def test_current_form_is_clean(self):
        assert structural_issues(codex.IDLE_PROMPT_STRICT_PATTERN, "search", re.MULTILINE) == []

    def test_old_form_is_flagged_and_grows_superlinearly(self):
        assert structural_issues(OLD_IDLE_PROMPT_STRICT, "search", re.MULTILINE)
        probe = pattern_probe(OLD_IDLE_PROMPT_STRICT, "search", re.MULTILINE)
        assert first_superlinear(probe, input_shapes(OLD_IDLE_PROMPT_STRICT, re.MULTILINE))

    def test_start_offset_grows_linearly(self):
        # The regex's steps are counted; the walk back is one rstrip() pass.
        def probe(text: str) -> int:
            return instrumented_steps(
                codex._strict_idle_prompt_start, text, {codex: ["_IDLE_PROMPT_STRICT_RE"]}
            )

        shapes = sorted(
            set(input_shapes(OLD_IDLE_PROMPT_STRICT, re.MULTILINE))
            | set(input_shapes(codex.IDLE_PROMPT_STRICT_PATTERN, re.MULTILINE))
        )
        assert nonlinear_shapes(probe, shapes) == []

    def test_start_offset_is_the_old_match_start(self):
        old = re.compile(OLD_IDLE_PROMPT_STRICT, re.MULTILINE)
        for text in _random_texts(2950, self.TOKENS, 20000, 20):
            match = old.search(text)
            assert codex._strict_idle_prompt_start(text) == (match and match.start()), repr(text)

    def test_single_line_match_is_unchanged(self):
        # The re.match callers hand it one line at a time (no newline inside).
        tokens = [t for t in self.TOKENS if t != "\n"]
        for line in _random_texts(866, tokens, 20000, 8):
            assert bool(re.match(codex.IDLE_PROMPT_STRICT_PATTERN, line)) == bool(
                re.match(OLD_IDLE_PROMPT_STRICT, line)
            ), repr(line)


class TestTuiProgress:
    TOKENS = ["•", "◦", "(", ")", "1", "2h ", "3m ", "45s", " • ", "esc to interrupt", " ", "x"]
    TOKENS += ["\n", "Working "]

    def test_current_form_is_clean(self):
        assert structural_issues(codex.TUI_PROGRESS_PATTERN, "search", re.MULTILINE) == []

    def test_old_form_is_flagged_and_grows_superlinearly(self):
        assert structural_issues(OLD_TUI_PROGRESS, "search", re.MULTILINE)
        probe = pattern_probe(OLD_TUI_PROGRESS, "search", re.MULTILINE)
        assert first_superlinear(probe, input_shapes(OLD_TUI_PROGRESS, re.MULTILINE))

    def test_current_form_grows_linearly(self):
        shapes = sorted(
            set(input_shapes(OLD_TUI_PROGRESS, re.MULTILINE))
            | set(input_shapes(codex.TUI_PROGRESS_PATTERN, re.MULTILINE))
        )
        probe = pattern_probe(codex.TUI_PROGRESS_PATTERN, "search", re.MULTILINE)
        assert nonlinear_shapes(probe, shapes) == []

    def test_matches_exactly_where_the_old_form_did(self):
        for text in _random_texts(739, self.TOKENS, 20000, 20):
            # Every caller passes re.MULTILINE and only asks whether it matches.
            assert bool(re.search(codex.TUI_PROGRESS_PATTERN, text, re.MULTILINE)) == bool(
                re.search(OLD_TUI_PROGRESS, text, re.MULTILINE)
            ), repr(text)

    def test_real_spinner_rows_still_match(self):
        for row in (
            "• Working (0s • esc to interrupt)",
            "• Working (1m 00s • esc to interrupt)",
            "• Working (1h 00m 00s • esc to interrupt)",
            "◦Applying both edits(52s • esc to interrupt)",
            "•Applying both edits(43s • esc to interrupt)»Ask Codex to do anything",
            "prior output\n• Starting script creation (10s • esc to interrupt)",
        ):
            assert re.search(codex.TUI_PROGRESS_PATTERN, row, re.MULTILINE), row
