"""Structural guard (GH #974): no live-status pattern may match settled output.

Every provider decides PROCESSING by matching a "the agent is working" pattern
against pane text. Those patterns are each spelled out independently, and the
tests around them only ever assert the positive direction — "this spinner line
reads PROCESSING". Nothing asserted the inverse, so when a new busy pattern's
match surface overlaps the glyph the same TUI uses for *finished* output, an
ordinary settled line reads PROCESSING. ``get_status_from_screen`` used to take
ANY match in the bottom rows, so the settled line pinned PROCESSING until it
scrolled off: ``_wait_for_completion`` saw neither COMPLETED nor IDLE and
burned its whole budget as a timeout, and InboxService (IDLE or COMPLETED only)
starved. That is #908 -> #956 -> #974. The scan is now scoped to the newest
CONVERSATION row above the input box — blank rows, box chrome and the status
area the TUI draws under the spinner (task-list rows, queued messages) do not
count, so they cannot hide a live spinner either (PR #975 review, P2-1) — via
the one shared walker, ``_newest_conversation_row`` (pinned by
``TestSingleConversationRowWalker``); this file guards the patterns themselves.

This file enumerates the busy patterns instead of the lines: every module-level
pattern in every provider module whose NAME says it is a live-status pattern
(see ``_BUSY_NAME`` / ``_IS_PATTERN_NAME``) is discovered and must reject a
shared corpus of settled output. The scope is the naming convention, nothing
more: a busy pattern named outside it, or a regex inlined at its call site, is
not covered, which is why ``test_discovery_finds_the_known_busy_patterns`` pins
the names this file is known to depend on. A pattern that is deliberately loose
because it is only ever applied inside a narrower context has to say so in
``KNOWN_LOOSE``.
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
# PROGRESS is codex's word for it (TUI_PROGRESS_PATTERN); BACKGROUND_WAIT is
# claude_code's still-running-task line, a PROCESSING signal on both of its
# paths. Plain WAIT would also pull in the WAITING_* user-prompt patterns,
# which are not busy signals.
_BUSY_NAME = re.compile(r"SPINNER|PROCESSING|WORKING|BUSY|PROGRESS|BACKGROUND_WAIT")
_IS_PATTERN_NAME = re.compile(r"(?:PATTERN|_RE)$")
# A string pattern's flags are the call site's choice (cursor_cli and codex
# search several of theirs with IGNORECASE), so string patterns are compiled
# with the superset. For a "must not match" check more flags can only make the
# guard stricter, never let a hit through.
_STRING_PATTERN_FLAGS = re.MULTILINE | re.IGNORECASE

# Settled output: finished responses, tool-call lines and static chrome. None of
# these is a live spinner, in any provider's TUI, and no busy pattern may match
# any of them on its own.
SETTLED_LINES = [
    # The MCP tool-call line: parentheses, but no spinner status.
    "● Calling cao-mcp-server… (ctrl+o to expand)",
    # The completed-response marker, same shape.
    "⏺ Ran the suite… (12s)",
    # Settled bullets that open with a hyphenated or contracted -ing compound
    # and end in an ellipsis (PR #975 review, P3-1). "*" and "·" are spinner
    # frames as well as markdown bullets.
    "* Long-running jobs now stop after 30 minutes…",
    "* Self-hosting is supported, see the docs…",
    "· Re-running the failed tests is the next step…",
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
# settled is their POSITION: the end-of-turn summary is the newest
# conversation row above the input box, not the response. They are therefore guarded through the
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
# The newest conversation row above the box once a turn has finished (GH #865).
END_OF_TURN_SUMMARY = "✻ Worked for 3m 12s"

# Live spinner lines, for the opposite direction: narrowing a busy pattern must
# not go so far that nothing matches a real spinner any more.
LIVE_SPINNER_LINES = [
    "● Leavening… (running UserPromptSubmit hook)",
    "● Sublimating… (12s · ↓ 300 tokens · esc to interrupt)",
    "● Dilly-dallying… (12s · ↓ 300 tokens)",
    "● Beboppin'… (thinking)",
    "✢ Compacting conversation…",
    # The default verbs carrying "-" / "'", on the animated glyphs and on the
    # "·" / "*" spinner frames.
    "✢ Dilly-dallying… (12s · ↓ 300 tokens)",
    "· Dilly-dallying… (12s · ↓ 300 tokens)",
    "* Beboppin’… (thinking)",
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
    "claude_code.SEPARATOR_WALK_SPINNER_PATTERN": (
        "Any glyph + ellipsis, applied by get_status() only to the lines between "
        "the last two separators, where the newest TUI draws nothing but the "
        "live turn. It matches the version footer and a '·' bullet on its own."
    ),
    "codex.PROCESSING_PATTERN": (
        "Legacy keyword list (thinking|working|running|...) that no status path "
        "applies any more: codex's own comment notes it matches 'running' in "
        "'You are running Codex in...', and it matches 'Re-running' in a "
        "settled bullet here. Codex reads PROCESSING from TUI_PROGRESS_PATTERN. "
        "Listed so the guard stays green on it without hiding that a bare word "
        "list is not a busy signal."
    ),
    "cursor_cli.PROCESSING_PATTERN": (
        "KNOWN LIVE GAP, sibling of GH #974: the glyph class includes '·' with "
        "no first-word anchor, so a settled '·' bullet or the version footer "
        "directly above the last separator reads PROCESSING in "
        "cursor_cli.get_status(), which inlines this regex in its walk back "
        "from the last separator and uses the name for its last-marker "
        "fallback. Tracked for its own issue; listed here so this guard stays "
        "green on it without hiding it."
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
                    compiled = re.compile(value, _STRING_PATTERN_FLAGS)
                except re.error:  # a fragment that is not valid on its own
                    continue
            else:
                continue
            found.append((f"{mod_info.name}.{name}", compiled))
    return found


DISCOVERED = _discover()


def test_discovery_finds_the_known_busy_patterns():
    """Renaming a pattern out of the naming convention must not drop coverage.

    This list is the guard's declared scope: every busy pattern this file is
    known to cover, by name, one per naming token in ``_BUSY_NAME``. A pattern
    that is not here and not named to the convention is not covered.
    """
    names = {qualname for qualname, _ in DISCOVERED}
    for expected in (
        "claude_code.REDUCED_MOTION_SPINNER_PATTERN",
        "claude_code.NEW_TUI_BOX_SPINNER_PATTERN",
        "claude_code.AFTER_SUMMARY_SPINNER_PATTERN",
        "claude_code.SEPARATOR_WALK_SPINNER_PATTERN",
        "claude_code.BACKGROUND_WAIT_PATTERN",
        "codex.TUI_PROGRESS_PATTERN",
        "cursor_cli.PROCESSING_PATTERN",
        "minimax_code._PROCESSING_HEAD_PATTERN",
        "omp._WORKING_PATTERN",
        "copilot_cli.COPILOT_BUSY_ROW_PATTERN",
    ):
        assert expected in names, f"{expected} no longer discovered; fix _BUSY_NAME"


@pytest.mark.parametrize("qualname,pattern", [pytest.param(q, p, id=q) for q, p in DISCOVERED])
def test_busy_pattern_rejects_settled_output(qualname, pattern):
    if qualname in KNOWN_LOOSE:
        pytest.skip(f"deliberately loose: {KNOWN_LOOSE[qualname]}")
    hits = [line for line in ALL_SETTLED_LINES if pattern.search(line)]
    assert not hits, (
        f"{qualname} reads settled output as a live spinner: {hits}. A busy "
        f"pattern that matches finished output pins PROCESSING wherever it is "
        f"applied to rows that have settled (GH #974). Anchor the match on "
        f"something only the spinner prints, or scope the call site to the row "
        f"the spinner can occupy (the newest conversation row above the input box, see _newest_conversation_row) and add "
        f"{qualname} to KNOWN_LOOSE naming that scope."
    )


@pytest.mark.parametrize("qualname", sorted(KNOWN_LOOSE))
def test_known_loose_entries_are_still_real_and_still_loose(qualname):
    by_name = dict(DISCOVERED)
    assert qualname in by_name, f"{qualname} is gone; drop it from KNOWN_LOOSE"
    assert any(
        by_name[qualname].search(line) for line in ALL_SETTLED_LINES
    ), f"{qualname} is tight now; drop it from KNOWN_LOOSE so it is guarded"


# The two patterns claude_code applies to the newest conversation row above
# the input box (_newest_conversation_row),
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


class TestSingleConversationRowWalker:
    """One walker decides what counts as a conversation row (PR #975, P2-1).

    The #974 defect class was "two walkers each decide what counts as a
    conversation row": the screen path and the raw input-box walk each skipped
    their own idea of chrome, so when the skip set grew (the status area the
    TUI draws under the spinner — task-list rows, queued messages) one path was
    fixed and the other kept the gap. ``_newest_conversation_row`` is now the
    single definition; this test pins that structurally, so a third walker
    cannot appear quietly.
    """

    @staticmethod
    def _functions_referencing(names: set) -> dict:
        """Map name -> set of claude_code functions whose body references it."""
        import ast
        import inspect

        import cli_agent_orchestrator.providers.claude_code as cc

        tree = ast.parse(inspect.getsource(cc))
        hits: dict = {name: set() for name in names}

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def _visit_func(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = _visit_func
            visit_AsyncFunctionDef = _visit_func

            def visit_Name(self, node):
                if node.id in hits and self.stack:
                    hits[node.id].add(self.stack[-1])

            def visit_Attribute(self, node):
                if node.attr in hits and self.stack:
                    hits[node.attr].add(self.stack[-1])
                self.generic_visit(node)

        Visitor().visit(tree)
        return hits

    # Patterns the walker consults that also have a job OUTSIDE it, so the
    # one-function rule below cannot apply to them. The effort footer is also
    # stripped out of response text (_strip_effort_footer_lines and the
    # response extractor), which is a different question from "is this row
    # conversation". Anything not listed here is skip-set-only.
    SHARED_WITH_OTHER_JOBS = {"EFFORT_FOOTER_LINE_PATTERN"}

    @staticmethod
    def _patterns_referenced_in(func_name: str) -> set:
        """The ``*_PATTERN`` names the body of ``func_name`` consults."""
        import ast
        import inspect

        import cli_agent_orchestrator.providers.claude_code as cc

        found: set = set()
        for node in ast.walk(ast.parse(inspect.getsource(cc))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Name) and inner.id.endswith("_PATTERN"):
                        found.add(inner.id)
        return found

    def test_the_row_skip_set_has_one_definition(self):
        """Every pattern the shared walker consults may be consulted by the
        shared walker only. A new walker would have to reference them (or
        silently diverge by reproducing their text, which the behaviour tests
        above catch).

        The skip set is read OUT of the walker rather than listed here. Listing
        it was the smaller version of the same defect class: when P2-2 added
        CHROME_CONTINUATION_ROW_PATTERN to the walker, a hand-written list of
        two names would have gone on passing while the new pattern was
        unguarded. Deriving it means a pattern is covered the moment it joins
        the walker.
        """
        skip_set = self._patterns_referenced_in("_newest_conversation_row")
        # Sanity: the derivation found the walker and its three row patterns.
        assert {
            "NEW_TUI_BOX_CHROME_PATTERN",
            "STATUS_AREA_ROW_PATTERN",
            "CHROME_CONTINUATION_ROW_PATTERN",
        } <= skip_set, skip_set
        guarded = skip_set - self.SHARED_WITH_OTHER_JOBS
        hits = self._functions_referencing(guarded)
        for name in sorted(guarded):
            assert hits[name] == {"_newest_conversation_row"}, (name, hits[name])

    def test_both_detector_paths_walk_through_the_shared_helper(self):
        hits = self._functions_referencing({"_newest_conversation_row"})
        callers = hits["_newest_conversation_row"]
        assert {"get_status", "get_status_from_screen"} <= callers, callers
