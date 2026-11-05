"""A Kiro v3 ``/tools`` panel that scrolls, drawn and read as kiro-cli 3.0.0 draws it.

No live capture of a scrolling ``/tools`` panel exists. A live 3.0.0 helper run
kept only herdr's history at teardown, whose top rows were the top of the open
panel's frame (its title among them) and CAO's refusal of the ``↓ more`` row.
So this draws the panel from the TUI's own layout code, which is the same in
2.37.0 and 3.0.0, with the rows, columns and spacing of the 2.35.1 captures in
``fixtures/``:

* the list shows ``max(pane rows - 9, 5)`` rows at a time;
* while rows are above, ``  ↑ more`` sits above the column header; while rows
  are below, ``  ↓ more`` sits below the last row; the footer then reads
  ``esc to close · ↑↓ to scroll`` instead of ``esc to close``;
* Down and Up move the list by one row, stopping at its ends (``scroll``);
* the title counts every tool, ``/tools · N tags``;
* the panel is drawn in the input frame's place, under the session's chrome
  (``CHROME``: the v3 notice and the trust-all banner, as captured live) and
  over three reserved rows (``BELOW``: the hidden copy hint's row, its margin
  and the input frame's margin). So a frame whose list scrolls is two or three
  rows taller than the pane even before the chrome, and its top rows, the
  title among them, go above the pane's top row (``KiroPane``).

``LIVE_ROWS`` are the 36 tags of that live run's helper (``fs_read`` and
``@cao-mcp-server``): the 2.35.1 capture's ``read`` and 34 ``cao-mcp-server``
tools, plus ``create_ephemeral_agent``, for the 35 tools the live run's
``/mcp`` showed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

Row = Tuple[str, str, str]

#: The width the 2.35.1 captures were taken at.
WIDTH = 200
_CAPTURE = Path(__file__).parent / "fixtures" / "kiro_cli_tui_2_35_1_v3_tools_readonly_mcp.txt"
_CAPTURED_ROW = re.compile(r"^ (\S+)\s{2,}(builtin|mcp)\s{2,}(.*?)\s*$")
#: The live frame's rows above the panel, as the live 3.0.0 history shows them.
CHROME: Tuple[str, ...] = (
    "• cao-helper: MCP tools: V2 allowed every tool on its trusted servers; V3 blocks all but 1",
    "  allowlisted tool. Edit its permissions rules, or ask its owner to. Back to V2: kiro-cli "
    "settings chat.agentEngine v2,",
    "  or kiro-cli --v2 for one session.",
    "",
    "",
    "─" * WIDTH,
    " Trust All Tools active, confirmations are off · /quit to exit",
    "─" * WIDTH,
)
#: The three rows Kiro keeps below an open panel's footer.
BELOW: Tuple[str, ...] = (" ", "", "")
#: What a herdr "visible" read returns: the pane's last 50 lines (``HerdrBackend``).
HERDR_VISIBLE_LINES = 50


def _captured_rows() -> List[Row]:
    rows = []
    for line in _CAPTURE.read_text(encoding="utf-8").splitlines():
        match = _CAPTURED_ROW.match(line)
        if match is not None:
            name, source, description = match.groups()
            rows.append((name, source, description))
    assert len(rows) == 35, len(rows)
    return rows


def _live_rows() -> List[Row]:
    rows = _captured_rows()
    after = [name for name, _, _ in rows].index("@cao-mcp-server/complete_assignment")
    rows.insert(
        after + 1,
        (
            "@cao-mcp-server/create_ephemeral_agent",
            "mcp",
            "Create and store a bounded agent profile for its creator to launch once.",
        ),
    )
    return rows


LIVE_ROWS: Tuple[Row, ...] = tuple(_live_rows())


def visible_rows(height: int) -> int:
    """How many rows Kiro shows at a time in a pane ``height`` rows tall."""
    return max(height - 9, 5)


def last_offset(count: int, height: int) -> int:
    """The offset of the last screen of ``count`` rows: Down stops there."""
    return max(0, count - visible_rows(height))


def scroll(offset: int, key: str, count: int, height: int) -> int:
    """The offset after one Down or Up, as Kiro's panel moves it."""
    if key == "Down":
        return min(last_offset(count, height), offset + 1)
    return max(0, offset - 1)


def panel(
    rows: Sequence[Row],
    *,
    height: int,
    offset: int = 0,
    declared: Optional[int] = None,
    more_above: Optional[bool] = None,
    more_below: Optional[bool] = None,
) -> List[str]:
    """The ``/tools`` panel's own lines, title to footer, open at ``offset``.

    ``declared`` overrides the title's count, and ``more_above`` and
    ``more_below`` whether a scroll row is drawn, for a faulty panel.
    """
    shown = visible_rows(height)
    count = len(rows) if declared is None else declared
    up = offset > 0 if more_above is None else more_above
    down = offset + shown < len(rows) if more_below is None else more_below
    name_width = max(max((len(row[0]) for row in rows), default=0), 12) + 2
    source_width = max(max((len(row[1]) for row in rows), default=0), 10) + 2
    description_width = max(WIDTH - name_width - source_width - 2, 10)
    lines = [
        f" /tools · {count} tag{'' if count == 1 else 's'}",
        "─" * WIDTH,
        " search:   type to filter",
        "",
    ]
    if up:
        lines.append("   ↑ more")
    lines.append(" " + "Name".ljust(name_width) + "Source".ljust(source_width) + "Description")
    for name, source, description in rows[offset : offset + shown]:
        if len(description) > description_width:
            description = description[: description_width - 1] + "…"
        lines.append(" " + name.ljust(name_width) + source.ljust(source_width) + description)
    if down:
        lines.append("   ↓ more")
    lines.append("─" * WIDTH)
    lines.append(" esc to close" + (" · ↑↓ to scroll" if up or down else ""))
    return lines


def frame(rows: Sequence[Row], **drawn) -> List[str]:
    """Everything Kiro draws for one ``/tools`` frame: chrome, panel, reserved rows."""
    return [*CHROME, *panel(rows, **drawn), *BELOW]


def render(rows: Sequence[Row], **drawn) -> str:
    """A whole frame as text: what a read that reaches back far enough sees."""
    return "\n".join(frame(rows, **drawn))


class KiroPane:
    """A pane ``height`` rows tall that Kiro draws frames into.

    A frame taller than the pane leaves its top rows above the pane's top row,
    in the scrollback. Kiro's renderer clears the scrollback (``CSI 3 J``) when
    it redraws an overflowing frame, so by default the scrollback holds only
    the current frame's overflow; ``keep_old_overflow`` keeps every earlier
    frame's overflow too, as a terminal that ignores that clear would.
    """

    def __init__(self, height: int, *, keep_old_overflow: bool = False) -> None:
        self.height = height
        self.keep_old_overflow = keep_old_overflow
        self.scrollback: List[str] = []
        self.visible: List[str] = [""] * height

    def draw(self, lines: Sequence[str]) -> None:
        lines = list(lines)
        overflow = max(0, len(lines) - self.height)
        if not self.keep_old_overflow:
            self.scrollback = []
        self.scrollback += lines[:overflow]
        self.visible = [""] * (self.height - len(lines[overflow:])) + lines[overflow:]

    def copy(self) -> "KiroPane":
        twin = KiroPane(self.height, keep_old_overflow=self.keep_old_overflow)
        twin.scrollback, twin.visible = list(self.scrollback), list(self.visible)
        return twin

    def capture(self, start: int) -> str:
        """tmux ``capture-pane -S start``: 0 is the visible pane, ``-N`` adds N lines above it."""
        above = self.scrollback[max(0, len(self.scrollback) + start) :] if start < 0 else []
        return "\n".join(above + self.visible)

    def recent(self, lines: int) -> str:
        """herdr ``pane read --source recent --lines N``: the last N lines."""
        return "\n".join((self.scrollback + self.visible)[-lines:])

    def read(self, backend: str, *, tail_lines: Optional[int], visible_only: bool) -> str:
        """``TerminalBackend.get_history`` as each backend implements it."""
        if backend == "tmux":
            return self.capture(0 if visible_only else -(tail_lines or 200))
        return self.recent(HERDR_VISIBLE_LINES if visible_only else (tail_lines or 500))


__all__ = [
    "BELOW",
    "CHROME",
    "HERDR_VISIBLE_LINES",
    "KiroPane",
    "LIVE_ROWS",
    "Row",
    "WIDTH",
    "frame",
    "last_offset",
    "panel",
    "render",
    "scroll",
    "visible_rows",
]
