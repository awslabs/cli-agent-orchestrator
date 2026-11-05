"""A Kiro v3 ``/tools`` panel that scrolls, drawn as kiro-cli 3.0.0's bundled TUI draws it.

No live capture of a scrolling ``/tools`` panel exists: a live 3.0.0 helper
run only kept the title (``/tools · 36 tags``) and CAO's refusal of the
``↓ more`` row. So this draws the panel from the TUI's own layout code, which
is the same in 2.37.0 and 3.0.0, with the rows, columns and spacing of the
2.35.1 captures in ``fixtures/``:

* the list shows ``max(pane rows - 9, 5)`` rows at a time;
* while rows are above, ``  ↑ more`` sits above the column header; while rows
  are below, ``  ↓ more`` sits below the last row; the footer then reads
  ``esc to close · ↑↓ to scroll`` instead of ``esc to close``;
* Down and Up move the list by one row, stopping at its ends (``scroll``);
* the title counts every tool, ``/tools · N tags``.

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
#: What sits above the panel when it leaves room: the trust banner, as captured.
_ABOVE = (
    "─" * WIDTH,
    " Trust All Tools active, confirmations are off · /quit to exit",
    "─" * WIDTH,
)


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


def render(
    rows: Sequence[Row],
    *,
    height: int,
    offset: int = 0,
    declared: Optional[int] = None,
    more_above: Optional[bool] = None,
    more_below: Optional[bool] = None,
) -> str:
    """The pane's screen with the ``/tools`` panel open at ``offset``.

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
    room = max(0, height - len(lines))
    above = ([""] * room + list(_ABOVE))[-room:] if room else []
    # A panel taller than the pane loses its top rows, title first.
    return "\n".join((above + lines)[-height:])


__all__ = ["LIVE_ROWS", "Row", "WIDTH", "last_offset", "render", "scroll", "visible_rows"]
