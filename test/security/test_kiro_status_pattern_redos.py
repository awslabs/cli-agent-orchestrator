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
  without re.DOTALL). It now calls ``kiro_cli.has_single_line_permission_prompt``;
* the v3 approval title ``\\S.*\\brequires approval\\s*$`` and declined-trust line
  ``Not trusted: .*Kiro exited ...`` (each searched for on one line of the
  screen) restarted at every non-blank / every "Not trusted: " on the line and
  re-scanned the rest of it from each. They now start at the line start and
  skip to the first such place. Text matches exactly when it did, and a match
  ends where the old one did;
* the v3 trust title and footers were linear where they are applied, but
  ``^\\s*`` failed the guard's strict check (search with re.MULTILINE). ``^``
  became ``\\A``, its meaning without re.MULTILINE.

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

    def test_second_question_inside_an_open_prompt_adds_no_span(self):
        # A scan that resumed at `start + 1` instead of `end` would restart
        # inside the first prompt's own text, treat its embedded "Allow this
        # action?" as a second question, and report an extra span starting
        # there (21, 61) even though the first prompt already consumed it.
        text = "Allow this action? [ Allow this action? y/n/t]: done\n[y/n/t]: x"
        assert _old_permission_spans(text) == [(0, 47)]
        assert kiro_cli._legacy_permission_prompt_spans(text) == [(0, 47)]


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

    # The skip before the header stops only at "<name> · " (name, blanks, "·",
    # a blank). These rows put the name earlier on the row in every other way,
    # so a weaker stop -- at any occurrence of the name, or at "<name> ·" --
    # changes their verdict. ``{p}`` is the profile name.
    BOUNDARY_ROWS = [
        ("{p} · claude-sonnet-4 · ◔ 5%", True),
        ("{p}/{p} · claude · ◔ 5%", True),  # the name as a prefix of an earlier token
        ("kiro {p}: {p} · auto · ◔ 12%", True),  # the name repeated
        ("{p}{p} · auto · ◔ 12%", True),  # the name twice in a row
        ("{p} {p} · auto · ◔ 12%", True),  # the name, a blank, then the header
        ("x{p}x {p} · auto · ◔ 12%", True),  # the name inside a word
        ("{p}· {p} · auto · ◔ 12%", True),  # "·" with no blank before it
        ("{p} ·{p} · auto · ◔ 12%", True),  # "·" with no blank after it
        ("{p}\t·\xa0{p} · auto · ◔ 12%", True),  # other blanks start a header too
        ("{p} · {p} · auto · ◔ 12%", True),  # a header start, then another
        ("{p}  ·  auto  ·  ◔  12%", True),
        ("{p}/{p} · claude ◔ 5%", False),  # no second "·" before "◔"
        ("{p}· auto · ◔ 12%", False),
        ("{p} ·auto · ◔ 12%", False),
        ("x{p} · auto · ◔ 12%", True),  # the old form never needed a word boundary
        ("x{p}x · auto · ◔ 12%", False),
        ("{p}/{p}", False),
        ("{q} · auto · ◔ 12%", False),  # a shorter name is not the profile
    ]

    @pytest.mark.parametrize("profile", PROFILES)
    @pytest.mark.parametrize("template,expected", BOUNDARY_ROWS)
    def test_rows_with_the_name_earlier_match_where_they_did(self, profile, template, expected):
        new = kiro_cli.KiroCliProvider("t", "s", "w", profile)._new_tui_header_pattern
        row = template.format(p=profile, q=profile[:3])
        assert bool(re.search(self._old(profile), row)) is expected, "the old form's verdict"
        assert bool(re.search(new, row)) is expected, repr(row)

    @pytest.mark.parametrize("profile", PROFILES)
    def test_rows_built_from_header_fragments_match_where_they_did(self, profile):
        # Whole fragments, so rows with an earlier name and then a full header
        # are common rather than rare.
        new = kiro_cli.KiroCliProvider("t", "s", "w", profile)._new_tui_header_pattern
        fragments = [profile, profile, profile[:3], " · ", "·", " ", "\t", "\xa0", "/", ":"]
        fragments += ["claude-sonnet-4", "auto", " · ◔ 5%", "◔", "12%", "x"]
        rng = random.Random(f"{profile}-fragments")
        for _ in range(20000):
            row = "".join(rng.choice(fragments) for _ in range(rng.randrange(0, 10)))
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


# The v3 dialog patterns (#1167) as they were defined, and how kiro_v3_dialog
# and kiro_v3_startup_selector apply each one: to one line of the screen.
OLD_V3 = {
    "_V3_APPROVAL_TITLE": (r"\S.*\brequires approval\s*$", "search"),
    "_V3_TRUST_DECLINED": (r"Not trusted: .*Kiro exited without loading this folder", "search"),
    "_V3_TRUST_TITLE": (r"^\s*Trust this workspace\?\s*$", "search"),
    "_V3_APPROVAL_FOOTER": (r"^\s*esc to close · ↑↓ to navigate · ↵ to select\b", "match"),
    "_V3_TRUST_FOOTER": (r"^\s*esc to exit · ↑↓ to navigate · ↵ to select\s*$", "match"),
    "_V3_SELECTOR_FOOTER": (
        r"^\s*esc to (?:close|exit) · ↑↓ to navigate · ↵ to select\b",
        "match",
    ),
}
SEARCHED = ("_V3_APPROVAL_TITLE", "_V3_TRUST_DECLINED")
ANCHORED = tuple(sorted(set(OLD_V3) - set(SEARCHED)))

#: What str.splitlines() splits on: no line the dialog code reads holds one.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
#: Characters around a dialog row in the differential inputs: blanks, line
#: breaks (no line holds one, but the match must be the old one on any text),
#: and word and non-word neighbours.
_EDGES = [" ", " ", "\t", "\xa0", "\u3000", "\r", "\n", "\x0b", "\u2028", "x", "_", "·", ":"]
_FOOTER_TAIL = " · ↑↓ to navigate · ↵ to select"

#: Name -> fragments of its rows, whole and in pieces, for the differential inputs.
TOKENS = {
    "_V3_APPROVAL_TITLE": ["requires approval", "requires", " approval", "requires approvals"]
    + ["shell"],
    "_V3_TRUST_DECLINED": ["Not trusted: ", "Not trusted:", "Not trusted", "N", "K"]
    + ["Kiro exited without loading this folder", "Kiro exited", " without loading this folder"],
    "_V3_TRUST_TITLE": ["Trust this workspace?", "Trust this workspace", "?"],
    "_V3_APPROVAL_FOOTER": ["esc to close" + _FOOTER_TAIL, "esc to close", _FOOTER_TAIL],
    "_V3_TRUST_FOOTER": ["esc to exit" + _FOOTER_TAIL, "esc to exit", _FOOTER_TAIL],
    "_V3_SELECTOR_FOOTER": [f"esc to {w}{_FOOTER_TAIL}" for w in ("close", "exit", "cancel")]
    + ["esc to exit", _FOOTER_TAIL],
}

V3_FIXTURES = sorted(FIXTURES.glob("kiro_cli_tui_2_35_1_v3_*.txt"))


def _one_line_shapes(pattern: str) -> list[tuple[str, str]]:
    """The shapes ``input_shapes`` grows from ``pattern`` that fit on one line."""
    return [s for s in input_shapes(pattern) if not _LINE_BREAKS & set(s[0] + s[1])]


def _dialog_lines(text: str) -> list[str]:
    """The lines kiro_v3_dialog and kiro_v3_startup_selector read."""
    clean = kiro_cli.strip_terminal_escapes(text)
    return [line.rstrip() for line in clean.splitlines() if line.strip()]


def _random_texts(name: str, longest: int):
    rng = random.Random(name)
    tokens = TOKENS[name] + _EDGES
    for _ in range(20000):
        yield "".join(rng.choice(tokens) for _ in range(rng.randrange(0, longest)))


class TestV3SearchedLines:
    """The approval title and the declined-trust line, searched for on each line.

    Unanchored, the search restarted at every place the pattern can start (any
    non-blank; every "Not trusted: ") and re-scanned the rest of the line from
    each, so one long line cost time quadratic in its length. Both now start at
    the line start and skip to the first such place. Text matches exactly when
    it did, and the match ends where the old one did; it begins at the start of
    the old match's line.
    """

    @pytest.mark.parametrize("name", SEARCHED)
    def test_old_form_grows_superlinearly_on_one_line(self, name):
        old, mode = OLD_V3[name]
        assert structural_issues(old, mode)
        assert first_superlinear(pattern_probe(old, mode), _one_line_shapes(old))

    @pytest.mark.parametrize("name", SEARCHED)
    def test_current_form_is_clean_and_grows_linearly(self, name):
        old, mode = OLD_V3[name]
        new = getattr(kiro_cli, name)
        assert structural_issues(new, mode) == []
        assert structural_issues(new.pattern, "search", re.MULTILINE) == []
        shapes = sorted(set(input_shapes(old)) | set(input_shapes(new)))
        assert nonlinear_shapes(pattern_probe(new, mode), shapes) == []

    @pytest.mark.parametrize("name", SEARCHED)
    def test_matches_where_the_old_form_did(self, name):
        # Line breaks included, though no line holds one: the verdict is the old
        # one on any text, not only on the lines the dialog code reads. With them,
        # the title's ``$`` under (?m) would also match before an inner newline.
        old, _mode = OLD_V3[name]
        new = getattr(kiro_cli, name)
        matched = 0
        for text in _random_texts(name, 14):
            expected = re.search(old, text)
            found = new.search(text)
            assert bool(found) == bool(expected), repr(text)
            if found and expected:
                line_start = text.rfind("\n", 0, expected.start()) + 1
                assert found.span() == (line_start, expected.end()), repr(text)
                matched += 1
        assert matched > 200, "the inputs must hit the pattern often"


class TestV3AnchoredLines:
    """The trust title and the footers: ``^`` became ``\\A``.

    Applied the way kiro_cli applies them (``.match``, or ``.search`` without
    re.MULTILINE), they were already linear: ``^`` holds at one position. Only
    the guard's strict check (``search`` with re.MULTILINE) found them slow,
    where ``^\\s*`` re-scans a run of blank lines from every line start.
    Without re.MULTILINE ``^`` and ``\\A`` mean the same, so nothing else
    changed, and with it ``\\A`` still holds at one position.
    """

    @pytest.mark.parametrize("name", ANCHORED)
    def test_only_the_anchor_changed(self, name):
        old, _mode = OLD_V3[name]
        new = getattr(kiro_cli, name)
        assert old.startswith("^") and new.pattern == "\\A" + old[1:]
        assert not new.flags & re.MULTILINE

    @pytest.mark.parametrize("name", ANCHORED)
    def test_old_form_was_linear_where_used_and_failed_only_the_strict_check(self, name):
        old, mode = OLD_V3[name]
        assert structural_issues(old, mode) == []
        assert nonlinear_shapes(pattern_probe(old, mode), _one_line_shapes(old)) == []
        assert structural_issues(old, "search", re.MULTILINE)
        assert first_superlinear(pattern_probe(old, "search", re.MULTILINE), input_shapes(old))

    @pytest.mark.parametrize("name", ANCHORED)
    def test_current_form_passes_the_strict_check_and_grows_linearly(self, name):
        old, mode = OLD_V3[name]
        new = getattr(kiro_cli, name)
        assert structural_issues(new, mode) == []
        assert structural_issues(new.pattern, "search", re.MULTILINE) == []
        shapes = sorted(set(input_shapes(old)) | set(input_shapes(new)))
        probe = pattern_probe(new.pattern, "search", re.MULTILINE)
        assert nonlinear_shapes(probe, shapes) == []

    @pytest.mark.parametrize("name", ANCHORED)
    def test_current_form_returns_the_old_matches(self, name):
        old, mode = OLD_V3[name]
        new = getattr(kiro_cli, name)
        matched = 0
        for text in _random_texts(name, 8):
            expected = getattr(re.compile(old), mode)(text)
            found = getattr(new, mode)(text)
            assert (found and found.span()) == (expected and expected.span()), repr(text)
            matched += bool(expected)
        assert matched > 200, "the inputs must hit the pattern often"


class TestV3DialogVerdicts:
    """The dialog readers give the old verdicts on every captured v3 pane."""

    @pytest.mark.parametrize("fixture", V3_FIXTURES, ids=lambda path: path.stem)
    def test_readers_give_the_old_verdicts(self, fixture, monkeypatch):
        screen = fixture.read_text(encoding="utf-8")
        new = (kiro_cli.kiro_v3_dialog(screen), kiro_cli.kiro_v3_startup_selector(screen))
        for name, (old, _mode) in OLD_V3.items():
            monkeypatch.setattr(kiro_cli, name, re.compile(old))
        assert (kiro_cli.kiro_v3_dialog(screen), kiro_cli.kiro_v3_startup_selector(screen)) == new

    @pytest.mark.parametrize("name", sorted(OLD_V3))
    def test_each_pattern_reads_each_captured_line_as_before(self, name):
        old, mode = OLD_V3[name]
        new = getattr(kiro_cli, name)
        hits = 0
        for fixture in V3_FIXTURES:
            for line in _dialog_lines(fixture.read_text(encoding="utf-8")):
                verdict = bool(getattr(new, mode)(line))
                assert verdict == bool(getattr(re.compile(old), mode)(line)), (fixture.name, line)
                hits += verdict
        assert hits, f"no captured v3 pane holds a {name} line"
