"""ReDoS guards for Kiro's status detection (CWE-1333), tier 1.

Two of Kiro's status checks ran over the whole status buffer on every poll and
backtracked superlinearly:

* the legacy permission prompt ``Allow this action\\?.*?\\[.*?y.*?/.*?n.*?/.*?t.*?\\]:``
  (re.DOTALL). With the question in the buffer but no closing "]:" after it,
  the six lazy gaps tried every ordered combination of the marker characters
  in the text that followed: polynomial of degree ~6. It is now a linear scan,
  ``kiro_cli._legacy_permission_prompt_spans``;
* ``TUI_INITIALIZING_PATTERN``'s ``\\d+ of \\d+ mcp servers ...`` restarted inside
  every run of digits and re-scanned the rest of the run: quadratic in the run.
  A ``(?<!\\d)`` lookbehind now starts it only where a run starts.

Each fix is checked three ways, none of them timed: the structure check (or,
for the scan, a step count of the characters it reads), step growth at n, 4n
and 16n on inputs generated from the OLD pattern's structure, and a
differential test that the new form returns exactly the old matches.
"""

import random
import re
from pathlib import Path
from test.security.redos_analysis import (
    first_superlinear,
    input_shapes,
    instrumented_steps,
    nonlinear_shapes,
    pattern_probe,
    structural_issues,
)

import pytest

from cli_agent_orchestrator.providers import kiro_cli

OLD_PERMISSION_PROMPT = r"Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:"
OLD_TUI_INITIALIZING = (
    r"Initializing\.\.\."
    r"|\d+ of \d+ mcp servers initialized\.\s*ctrl-c to start chatting now"
    r"|Initializing\s*·\s*type to queue a message"
)

FIXTURES = Path(__file__).resolve().parent.parent / "providers" / "fixtures"


def _old_permission_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(OLD_PERMISSION_PROMPT, text, re.DOTALL)]


def _scan_steps(text: str) -> int:
    return instrumented_steps(kiro_cli._legacy_permission_prompt_spans, text, {})


class TestLegacyPermissionPrompt:
    def test_old_regex_is_flagged_and_grows_superlinearly(self):
        assert structural_issues(OLD_PERMISSION_PROMPT, "finditer", re.DOTALL)
        probe = pattern_probe(OLD_PERMISSION_PROMPT, "finditer", re.DOTALL)
        assert first_superlinear(probe, input_shapes(OLD_PERMISSION_PROMPT, re.DOTALL))

    def test_scan_reads_linearly_many_characters(self):
        # Every shape the old regex's own structure suggests, including the
        # question repeated with no closing "]:".
        shapes = input_shapes(OLD_PERMISSION_PROMPT, re.DOTALL)
        assert nonlinear_shapes(_scan_steps, shapes) == []

    def test_scan_returns_the_old_regex_spans(self):
        rng = random.Random(675)
        tokens = ["Allow this action?", "Allow", "[", "]", ":", "]:", "y", "n", "t", "/", " ", "\n"]
        for _ in range(3000):
            text = "".join(rng.choice(tokens) for _ in range(rng.randrange(0, 30)))
            assert kiro_cli._legacy_permission_prompt_spans(text) == _old_permission_spans(text)

    @pytest.mark.parametrize(
        "name",
        sorted(p.name for p in FIXTURES.glob("kiro_cli_*.txt") if "Allow this" in p.read_text()),
    )
    def test_scan_returns_the_old_regex_spans_on_fixtures(self, name):
        text = (FIXTURES / name).read_text()
        clean = re.sub(kiro_cli.ANSI_CODE_PATTERN, "", text)
        spans = kiro_cli._legacy_permission_prompt_spans(clean)
        assert spans == _old_permission_spans(clean)
        assert spans, "fixture holds a permission prompt; the scan must find it"


class TestTuiInitializing:
    def test_current_form_is_clean(self):
        assert structural_issues(kiro_cli.TUI_INITIALIZING_PATTERN, "finditer") == []

    def test_old_form_is_flagged_and_grows_superlinearly(self):
        assert structural_issues(OLD_TUI_INITIALIZING, "finditer")
        probe = pattern_probe(OLD_TUI_INITIALIZING, "finditer")
        assert first_superlinear(probe, input_shapes(OLD_TUI_INITIALIZING))

    def test_current_form_grows_linearly(self):
        shapes = set(input_shapes(OLD_TUI_INITIALIZING))
        shapes |= set(input_shapes(kiro_cli.TUI_INITIALIZING_PATTERN))
        probe = pattern_probe(kiro_cli.TUI_INITIALIZING_PATTERN, "finditer")
        assert nonlinear_shapes(probe, sorted(shapes)) == []

    def test_current_form_returns_the_old_matches(self):
        rng = random.Random(1167)
        tokens = [
            "1",
            "23",
            " of ",
            "4",
            " mcp servers initialized.",
            " ",
            "ctrl-c to start chatting now",
            "Initializing",
            "...",
            "·",
            " type to queue a message",
            "\n",
        ]
        for _ in range(3000):
            text = "".join(rng.choice(tokens) for _ in range(rng.randrange(0, 25)))
            old = [m.span() for m in re.finditer(OLD_TUI_INITIALIZING, text)]
            new = [m.span() for m in re.finditer(kiro_cli.TUI_INITIALIZING_PATTERN, text)]
            assert new == old, text

    def test_composer_chrome_pattern_built_from_it_is_clean(self):
        # _COMPOSER_CHROME_RE splices TUI_INITIALIZING_PATTERN into a larger
        # alternation; the fix has to survive the splice.
        assert structural_issues(kiro_cli.KiroCliProvider._COMPOSER_CHROME_RE, "search") == []
