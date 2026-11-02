"""ReDoS guards for Kiro's status detection and extraction (CWE-1333).

These Kiro patterns backtracked superlinearly on agent-controlled text:

* the legacy permission prompt ``Allow this action\\?.*?\\[.*?y.*?/.*?n.*?/.*?t.*?\\]:``
  (re.DOTALL). With the question in the buffer but no closing "]:" after it,
  the six lazy gaps tried every ordered combination of the marker characters
  in the text that followed: polynomial of degree ~6. It is now a linear scan,
  ``kiro_cli._legacy_permission_prompt_spans``;
* ``TUI_INITIALIZING_PATTERN``'s ``\\d+ of \\d+ mcp servers ...`` restarted inside
  every run of digits and re-scanned the rest of the run: quadratic in the run.
  A ``(?<!\\d)`` lookbehind now starts it only where a run starts;
* ``_new_tui_header_pattern`` (extraction, one row at a time) restarted at every
  occurrence of the agent name on a row. It now skips to the first one;
* the AG-UI approval classifier kept its own copy of the permission regex (run
  without re.DOTALL). It now calls ``kiro_cli.has_single_line_permission_prompt``.

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


class TestNewTuiHeader:
    """``_new_tui_header_pattern`` filters header rows out of extracted content."""

    PROFILES = ("developer", "code_supervisor")

    @staticmethod
    def _old(profile: str) -> str:
        return rf"{re.escape(profile)}\s+·\s+.*·\s+◔\s*\d+%"

    @pytest.mark.parametrize("profile", PROFILES)
    def test_current_form_is_clean_and_grows_linearly(self, profile):
        new = kiro_cli.KiroCliProvider("t", "s", "w", profile)._new_tui_header_pattern
        assert structural_issues(new, "search") == []
        shapes = sorted(set(input_shapes(new)) | set(input_shapes(self._old(profile))))
        assert nonlinear_shapes(pattern_probe(new, "search"), shapes) == []

    @pytest.mark.parametrize("profile", PROFILES)
    def test_old_form_is_flagged_and_grows_superlinearly(self, profile):
        old = self._old(profile)
        assert structural_issues(old, "search")
        assert first_superlinear(pattern_probe(old, "search"), input_shapes(old))

    @pytest.mark.parametrize("profile", PROFILES)
    def test_rows_match_exactly_where_they_did(self, profile):
        # Applied to one row at a time (rows come from split("\n")).
        new = kiro_cli.KiroCliProvider("t", "s", "w", profile)._new_tui_header_pattern
        tokens = [profile, profile[:3], " ", "\t", "\xa0", "\r", "·", "◔", "5%", "50", "%", "m"]
        rng = random.Random(profile)
        for _ in range(20000):
            row = "".join(rng.choice(tokens) for _ in range(rng.randrange(0, 16)))
            assert bool(re.search(new, row)) == bool(re.search(self._old(profile), row)), repr(row)


class TestSingleLinePermissionPrompt:
    """The AG-UI approval classifier's reading: no re.DOTALL, so one line per prompt."""

    def test_old_copy_is_flagged_and_grows_superlinearly(self):
        assert structural_issues(OLD_PERMISSION_PROMPT, "search")
        probe = pattern_probe(OLD_PERMISSION_PROMPT, "search")
        assert first_superlinear(probe, input_shapes(OLD_PERMISSION_PROMPT))

    def test_scan_reads_linearly_many_characters(self):
        def probe(text: str) -> int:
            return instrumented_steps(kiro_cli.has_single_line_permission_prompt, text, {})

        shapes = input_shapes(OLD_PERMISSION_PROMPT)
        assert nonlinear_shapes(probe, shapes) == []

    def test_verdict_is_the_old_copy_verdict(self):
        rng = random.Random(43)
        tokens = ["Allow this action?", "Allow", "[", "]", ":", "]:", "y", "n", "t", "/", " "]
        tokens += ["\n", "\n", "\r"]
        for _ in range(20000):
            text = "".join(rng.choice(tokens) for _ in range(rng.randrange(0, 30)))
            assert kiro_cli.has_single_line_permission_prompt(text) == bool(
                re.search(OLD_PERMISSION_PROMPT, text)
            ), repr(text)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Allow this action? [y/n/t]:", "kiro:permission_request"),
            ("Allow this action? Use 't' to trust. [y/n/t]:", "kiro:permission_request"),
            # Split across lines: never matched without re.DOTALL, still does not.
            ("Allow this action?\n[y/n/t]:", "kiro:unknown_prompt"),
            ("Allow this action? [y/n/\nt]:", "kiro:unknown_prompt"),
        ],
    )
    def test_classifier_reads_the_same(self, text, expected):
        from cli_agent_orchestrator.services.agui.handoff_approval import classify_reason

        assert classify_reason("kiro_cli", text) == expected
