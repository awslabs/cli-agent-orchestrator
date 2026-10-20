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
# these is a live spinner, in any provider's TUI.
SETTLED_LINES = [
    # GH #974: a finished response that reports its own timing. "●" is also
    # Claude Code's response marker and its reduced-motion spinner glyph.
    "● Done. The fix is in place… (3m 12s)",
    "● Approved the plan… (↓ 1.2k tokens)",
    "● Ran the suite… (12s)",
    "● Thought about it… (esc to interrupt)",
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
    hits = [line for line in SETTLED_LINES if pattern.search(line)]
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
        by_name[qualname].search(line) for line in SETTLED_LINES
    ), f"{qualname} is tight now; drop it from KNOWN_LOOSE so it is guarded"


@pytest.mark.parametrize("line", LIVE_SPINNER_LINES)
def test_a_real_spinner_still_matches_some_claude_code_busy_pattern(line):
    """The narrowing direction has a floor: do not anchor the spinner away."""
    assert any(
        pattern.search(line)
        for qualname, pattern in DISCOVERED
        if qualname.startswith("claude_code.") and qualname not in KNOWN_LOOSE
    ), f"no claude_code busy pattern matches the live spinner {line!r}"


class TestSettledFrameNeverPinsProcessing:
    """The same invariant through the public status paths, including the latch.

    The regex guard above is the cheap enumeration; this is the behaviour it
    stands in for. ``get_status_from_screen`` has no separator scoping and no
    "newer output below" escape, so a settled frame that reads PROCESSING once
    reads PROCESSING on every later poll too.
    """

    BOX = "─" * 60

    def _screen(self, line):
        return ["❯ review the diff", line, self.BOX, "❯ ", self.BOX, "  ? for shortcuts"]

    @pytest.mark.parametrize("line", SETTLED_LINES)
    def test_settled_line_is_not_processing_on_either_path(self, line):
        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        screen = self._screen(line)
        for _ in range(3):
            assert provider.get_status_from_screen(screen) != TerminalStatus.PROCESSING
            assert provider.get_status("\n".join(screen)) != TerminalStatus.PROCESSING
