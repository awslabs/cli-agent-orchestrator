"""Structural guard (GH #974): no live-status pattern may match settled output.

Every provider decides PROCESSING by matching a "the agent is working" pattern
against pane text. Those patterns are each spelled out independently, and the
tests around them only ever assert the positive direction — "this spinner line
reads PROCESSING". Nothing asserted the inverse, so when a new busy pattern's
match surface overlaps the glyph the same TUI uses for *finished* output, an
ordinary settled line reads PROCESSING. On ``get_status_from_screen`` that scan
is an unscoped ``any()`` over the bottom rows, so the settled line pins
PROCESSING until it scrolls off: ``_wait_for_completion`` sees neither COMPLETED
nor IDLE and burns its whole budget as a timeout, and InboxService (IDLE or
COMPLETED only) starves. That is #908 -> #956 -> #974.

This file enumerates the busy patterns instead of the lines: every module-level
live-status pattern in every provider module is discovered by name and must
reject a shared corpus of settled output. A new provider, or a new busy pattern
in an existing one, is covered the moment it is defined -- no test has to
remember to add it. A pattern that is deliberately loose because it is only ever
applied inside a narrower context has to say so in ``KNOWN_LOOSE``.
"""

import importlib
import pkgutil
import re

import pytest

import cli_agent_orchestrator.providers as providers_pkg
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

# A module-level regex is a live-status pattern if its name says so. Restricted
# to names that end in PATTERN/_RE so shared sub-expressions (_SPINNER_VERB, a
# verb character class) are not treated as line patterns in their own right.
_BUSY_NAME = re.compile(r"SPINNER|PROCESSING|WORKING|BUSY")
_IS_PATTERN_NAME = re.compile(r"(?:PATTERN|_RE)$")

# Settled output: finished responses, tool-call lines and static chrome. None of
# these is a live spinner, in any provider's TUI, and no busy pattern may match
# any of them on its own.
SETTLED_LINES = [
    # The MCP tool-call line: parentheses, but no spinner status.
    "● Calling cao-mcp-server… (ctrl+o to expand)",
    # The completed-response marker, same shape.
    "⏺ Ran the suite… (12s)",
    # Static chrome.
    "● high · /effort",
    "* Remember to deploy…",
    "· latest: 1.2.3 … update available",
    "└ Completed in 12s",
    "● Ready",
]

# GH #974: a finished response that reports its own timing. "●" is Claude
# Code's response marker AND its reduced-motion spinner glyph, and the
# parenthetical is the spinner's own status shape, so these rows are
# byte-for-byte a live spinner with a non-gerund message (#908, #956). No
# regex can reject them without also rejecting that spinner; what makes them
# settled is their POSITION: the end-of-turn summary is the newest row above
# the input box, not the response. They are therefore guarded through the
# public status paths in the finished-turn frame (TestSettledFrameNeverPins
# Processing), not against each pattern in isolation.
SETTLED_STATUS_ROWS = [
    "● Done. The fix is in place… (3m 12s)",
    "● Approved the plan… (↓ 1.2k tokens)",
    "● Ran the suite… (12s)",
    "● Thought about it… (esc to interrupt)",
    "● Refactoring complete… (3m 12s)",
]
ALL_SETTLED_LINES = SETTLED_LINES + SETTLED_STATUS_ROWS
# The newest row above the box once a turn has finished (GH #865).
END_OF_TURN_SUMMARY = "✻ Worked for 3m 12s"

# Live spinner lines, for the opposite direction: narrowing a busy pattern must
# not go so far that nothing matches a real spinner any more.
LIVE_SPINNER_LINES = [
    "● Leavening… (running UserPromptSubmit hook)",
    "● Sublimating… (12s · ↓ 300 tokens · esc to interrupt)",
    "● Dilly-dallying… (12s · ↓ 300 tokens)",
    "● Beboppin'… (thinking)",
    "✢ Compacting conversation…",
]

# Patterns that match settled output on purpose, because the call site supplies
# the context they lack. Each entry names that context. An entry that stops
# being loose, or names a pattern that no longer exists, fails below -- the
# allowlist cannot rot into a silent exemption.
KNOWN_LOOSE = {
    "claude_code.REDUCED_MOTION_SPINNER_PATTERN": (
        "GH #908/#956/#974: matches any '● <message>… (<spinner status>)' row, "
        "so it also matches a finished response that reports its own timing "
        "(SETTLED_STATUS_ROWS). The message is not a property of the spinner "
        "row (custom spinnerVerbs, hook verbs, task text in any language), so "
        "the pattern cannot be tightened; both call sites supply position "
        "instead: get_status()'s input-box walk and get_status_from_screen() "
        "match it only on the newest conversation row above the input box, "
        "where a finished turn has its end-of-turn summary."
    ),
    "claude_code.PROCESSING_PATTERN": (
        "Legacy pre-TUI fragment, composed into larger expressions and only "
        "applied between separators. NEW_TUI_BOX_SPINNER_PATTERN is the tight "
        "line-level form used next to the input box."
    ),
    "cursor_cli.PROCESSING_PATTERN": (
        "KNOWN LIVE GAP, sibling of GH #974: the glyph class includes '·' with "
        "no first-word gerund anchor, so a settled '·' bullet or the version "
        "footer directly above the last separator reads PROCESSING on "
        "cursor_cli's backwards walk (cursor_cli.py:728). Tracked for its own "
        "issue; listed here so this guard stays green on it without hiding it."
    ),
}


def _discover():
    """(qualname, compiled pattern) for every live-status pattern in providers/."""
    found = []
    for mod_info in pkgutil.iter_modules(providers_pkg.__path__):
        if mod_info.ispkg:
            continue
        mod = importlib.import_module(f"{providers_pkg.__name__}.{mod_info.name}")
        for name, value in vars(mod).items():
            if not (_BUSY_NAME.search(name) and _IS_PATTERN_NAME.search(name)):
                continue
            if isinstance(value, re.Pattern):
                compiled = value
            elif isinstance(value, str):
                try:
                    compiled = re.compile(value, re.MULTILINE)
                except re.error:  # a fragment that is not valid on its own
                    continue
            else:
                continue
            found.append((f"{mod_info.name}.{name}", compiled))
    return found


DISCOVERED = _discover()


def test_discovery_finds_the_known_busy_patterns():
    """Renaming a pattern out of the naming convention must not drop coverage."""
    names = {qualname for qualname, _ in DISCOVERED}
    for expected in (
        "claude_code.REDUCED_MOTION_SPINNER_PATTERN",
        "claude_code.NEW_TUI_BOX_SPINNER_PATTERN",
        "cursor_cli.PROCESSING_PATTERN",
        "minimax_code._PROCESSING_HEAD_PATTERN",
    ):
        assert expected in names, f"{expected} no longer discovered; fix _BUSY_NAME"
    assert len(names) >= 15


@pytest.mark.parametrize("qualname,pattern", [pytest.param(q, p, id=q) for q, p in DISCOVERED])
def test_busy_pattern_rejects_settled_output(qualname, pattern):
    if qualname in KNOWN_LOOSE:
        pytest.skip(f"deliberately loose: {KNOWN_LOOSE[qualname]}")
    hits = [line for line in ALL_SETTLED_LINES if pattern.search(line)]
    assert not hits, (
        f"{qualname} reads settled output as a live spinner: {hits}. A busy "
        f"pattern that matches finished output pins PROCESSING on the unscoped "
        f"screen scan (GH #974). Anchor the match -- e.g. require the first "
        f"word after the glyph to be a gerund, as claude_code._SPINNER_VERB "
        f"does -- or add {qualname} to KNOWN_LOOSE with the scoping that makes "
        f"it safe."
    )


@pytest.mark.parametrize("qualname", sorted(KNOWN_LOOSE))
def test_known_loose_entries_are_still_real_and_still_loose(qualname):
    by_name = dict(DISCOVERED)
    assert qualname in by_name, f"{qualname} is gone; drop it from KNOWN_LOOSE"
    assert any(
        by_name[qualname].search(line) for line in ALL_SETTLED_LINES
    ), f"{qualname} is tight now; drop it from KNOWN_LOOSE so it is guarded"


# The two patterns claude_code applies to the newest row above the input box,
# in get_status()'s input-box walk and in get_status_from_screen(). A live
# spinner that neither matches reads COMPLETED there, the #908 failure.
BOX_ROW_PATTERNS = (
    "claude_code.NEW_TUI_BOX_SPINNER_PATTERN",
    "claude_code.REDUCED_MOTION_SPINNER_PATTERN",
)


@pytest.mark.parametrize("line", LIVE_SPINNER_LINES)
def test_a_real_spinner_still_matches_a_box_row_pattern(line):
    """The narrowing direction has a floor: do not anchor the spinner away."""
    by_name = dict(DISCOVERED)
    assert any(
        by_name[qualname].search(line) for qualname in BOX_ROW_PATTERNS
    ), f"no box-row busy pattern matches the live spinner {line!r}"


class TestSettledFrameNeverPinsProcessing:
    """The same invariant through the public status paths, including the latch.

    The regex guard above is the cheap enumeration; this is the behaviour it
    stands in for. A frame that has settled must not read PROCESSING on any
    poll: ``get_status_from_screen`` used to scan every bottom row for a busy
    match, so a settled row that matched once matched on every later poll too
    (GH #974). Polled three times so a latch, not just a first read, is caught.
    """

    BOX = "─" * 60

    def _screen(self, *lines):
        return ["❯ review the diff", *lines, self.BOX, "❯ ", self.BOX, "  ? for shortcuts"]

    def _assert_never_processing(self, screen):
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        for _ in range(3):
            assert provider.get_status_from_screen(screen) != TerminalStatus.PROCESSING
            assert provider.get_status("\n".join(screen)) != TerminalStatus.PROCESSING

    @pytest.mark.parametrize("line", SETTLED_LINES)
    def test_settled_line_alone_above_the_box_is_not_processing(self, line):
        """Directly above the box, with nothing under it: the one position a
        live spinner occupies, so only the pattern itself rejects the line."""
        self._assert_never_processing(self._screen(line))

    @pytest.mark.parametrize("line", ALL_SETTLED_LINES)
    def test_settled_line_in_a_finished_turn_is_not_processing(self, line):
        """The finished-turn frame: the line, response body, the end-of-turn
        summary as the newest row. This is where the #974 latch lived, and the
        only frame in which SETTLED_STATUS_ROWS can be told from a spinner."""
        screen = self._screen(line, "  and the rest of the answer.", "", END_OF_TURN_SUMMARY)
        self._assert_never_processing(screen)

    @pytest.mark.parametrize("line", ALL_SETTLED_LINES)
    def test_settled_line_with_chrome_below_the_summary_is_not_processing(self, line):
        """Box chrome ("⎿ Tip: …") renders between the summary and the box; it
        must be skipped on the way up, not mistaken for the newest row."""
        screen = self._screen(line, "", END_OF_TURN_SUMMARY, "⎿ Tip: press ctrl+o to expand")
        self._assert_never_processing(screen)
