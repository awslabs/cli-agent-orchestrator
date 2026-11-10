"""Read a Kiro v3 pane's ``/tools`` and ``/mcp list`` panels before any task (#1167).

The TUI header names the agent, not its tools: a same-name workspace agent
with no tools, or a file that widened on load, still shows the right name. So
before a v3 terminal is recorded as verified, CAO opens both panels in the
verified pane and checks what the engine actually loaded:

* ``/mcp list`` shows exactly the configured servers, each running with at
  least one tool; it is checked first, and opened again until no server is
  still loading (v3's panel is a snapshot taken as it opens, so reading an
  open panel again never shows a change);
* then, on ``/tools``, every builtin tag is one the grant implies, and every MCP
  row belongs to a granted server (and tool). A list longer than the pane
  scrolls: Kiro draws ``↑ more`` and ``↓ more`` rows around the rows that fit,
  and Down moves the list by one row. So the panel is read one screen per Down
  (``parse_tools_page``), each screen must be the one before it moved down by
  exactly one row, and the joined rows are checked as one panel
  (``join_tools_pages``): every row the title declares, each seen once.
  Kiro draws a scrolling panel taller than the pane, so its title is read from
  the pane's recent lines; only one ``/tools`` title may be in them, and
  nothing but blank rows may follow the panel's footer. By default Kiro keeps
  the scrollback and repaints only the pane, so rows above the pane can hold
  old text under that one title: the one-row check (``check_tools_page``) is
  what rejects those; the one-title rule rejects copies of the panel.

The parsers are pure and fail closed: any layout they do not recognize
(v2's per-tool panels, the slash palette, a clipped list) is a refusal, never
a pass. Provisional (#1167): the codes ``kiro_v3_tools_mismatch`` and
``kiro_v3_mcp_not_ready`` and the panel shapes, captured on kiro-cli 2.35.1;
the ``/tools`` scroll rows are read from kiro-cli 3.0.0's bundled TUI, not
captured live.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cli_agent_orchestrator.providers.kiro_v3_schema import (
    KiroV3EngineSchema,
    KiroV3Refusal,
    pinned_kiro_v3_schemas,
)
from cli_agent_orchestrator.utils.tool_mapping import (
    kiro_v3_exact_tools,
    mcp_reference_covers,
    split_mcp_reference,
)

_SEPARATOR = re.compile(r"^\s*─{20,}\s*$")
_TOOLS_TITLE = re.compile(r"^\s*/tools · (\d+) tags?\s*$")
_TOOLS_COLUMNS = re.compile(r"^\s*Name\s+Source\s+Description\s*$")
_TOOLS_ROW = re.compile(r"^\s*(\S+)\s{2,}(builtin|mcp)(?:\s{2,}.*)?$")
_NO_TOOLS = re.compile(r"^\s*No tools available\s*$")
# A /tools list longer than the pane shows ``max(rows - 9, 5)`` rows at a time;
# kiro-cli 3.0.0's TUI draws these two rows around them (``  ↑ more`` above the
# column header, ``  ↓ more`` below the last row) while more rows are above or
# below. Only the Unicode glyphs are recognized (icons off draws ``^``/``v``,
# which refuses, as ``* running`` does on /mcp).
_TOOLS_MORE_ABOVE = re.compile(r"^\s*↑ more\s*$")
_TOOLS_MORE_BELOW = re.compile(r"^\s*↓ more\s*$")
_MCP_TITLE = re.compile(r"^\s*/mcp list · (\d+) configured\s*$")
_MCP_HEADING = re.compile(r"^\s*Configured Servers\s*$")
_MCP_COLUMNS = re.compile(r"^\s*Name\s+Source\s+Status\s+Details\s*$")
# Status is a glyph and a word ("● running", "✗ failed"); a server still
# starting reads "◌ loading" in Kiro's bundled TUI (not yet captured live), so
# only the word is relied on for that state.
_MCP_ROW = re.compile(r"^\s*(\S+)\s{2,}(\S+)\s{2,}(\S+ \S+)\s{2,}(.*?)\s*$")
_MCP_TOOL_COUNT = re.compile(r"^(\d+) tools?$")
_SEARCH = re.compile(r"^\s*search:\s+type to filter\s*$")
# The /mcp panel's footer carries right-aligned key hints after "esc to close".
_CLOSE = re.compile(r"^\s*esc to close(?:\s|$)")
_MCP_EDIT_HINT = re.compile(r"^\s*To edit local configs:")

#: The palette states CAO types before pressing Enter. ``/tools`` must be the
#: highlighted-free completion row and nothing else, so Enter opens the panel
#: and can never pick ``trust-all`` (Tab would select it).
_TYPED_TOOLS = re.compile(r"^\s*/tools\s{2,}List available tools\b")
_TYPED_MCP = re.compile(r"^\s*/mcp\s{2,}Show MCP server status\s*$")
_PALETTE_SELECTED = re.compile(r"^\s*❯\s")
_PALETTE_FOOTER = re.compile(r"^\s*esc to cancel · ↵ to select\s*$")


class KiroAttestationError(KiroV3Refusal):
    """The pane's tools or MCP servers do not match the grant; nothing was typed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ToolsPanel:
    tags: frozenset[str]
    mcp_tools: frozenset[str]


@dataclass(frozen=True)
class ToolsPage:
    """One screen of the ``/tools`` panel.

    ``rows`` are the ``(name, source)`` rows on screen, top to bottom;
    ``more_above`` and ``more_below`` say whether Kiro drew its ``↑ more`` or
    ``↓ more`` row, that is, whether the list scrolls on past this screen.
    """

    declared: int
    rows: Tuple[Tuple[str, str], ...]
    more_above: bool
    more_below: bool


@dataclass(frozen=True)
class McpServerRow:
    name: str
    status: str
    tools: int


def _body(screen: str, title: re.Pattern[str]) -> Optional[List[str]]:
    """The panel lines after its title, up to its ``esc to close`` footer."""
    lines = screen.splitlines()
    starts = [i for i, line in enumerate(lines) if title.match(line)]
    if len(starts) != 1:
        return None
    body: List[str] = []
    for line in lines[starts[0] :]:
        if _CLOSE.match(line):
            return body
        body.append(line)
    return None


def _lines_to_footer(screen: str) -> List[str]:
    """``screen``'s lines up to and including the ``/tools`` panel's footer."""
    lines = screen.splitlines()
    start = next(i for i, line in enumerate(lines) if _TOOLS_TITLE.match(line))
    end = next(i for i in range(start, len(lines)) if _CLOSE.match(lines[i]))
    return lines[: end + 1]


def parse_tools_page(screen: str) -> ToolsPage:
    """Parse one screen of the v3 ``/tools · N tags`` panel; refuse any other layout.

    Only the rows on screen are read: the declared count is checked once every
    screen of a scrolling list has been joined (``join_tools_pages``).

    ``screen`` may hold lines from above the pane: Kiro draws a scrolling panel
    taller than the pane (see ``KiroCliProvider._read_v3_tools_screen``). So
    exactly one ``/tools`` title must be in it, and the panel must be the last
    thing drawn: below its footer, only blank rows (Kiro keeps three). Rows
    above the pane may still be stale; ``check_tools_page`` rejects those.
    """
    body = _body(screen, _TOOLS_TITLE)
    if body is None:
        raise KiroAttestationError("kiro_v3_tools_mismatch", "no complete /tools panel on screen")
    if any(line.strip() for line in screen.splitlines()[len(_lines_to_footer(screen)) :]):
        raise KiroAttestationError(
            "kiro_v3_tools_mismatch", "something is drawn below the /tools panel"
        )
    match = _TOOLS_TITLE.match(body[0])
    assert match is not None
    declared = int(match.group(1))
    rows = [line for line in body[1:] if line.strip() and not _SEPARATOR.match(line)]
    if rows and _SEARCH.match(rows[0]):
        rows = rows[1:]
    if rows and _NO_TOOLS.match(rows[0]):
        raise KiroAttestationError("kiro_v3_tools_mismatch", "the agent has no tools available")
    more_above = bool(rows) and _TOOLS_MORE_ABOVE.match(rows[0]) is not None
    if more_above:
        rows = rows[1:]
    if not rows or not _TOOLS_COLUMNS.match(rows[0]):
        raise KiroAttestationError("kiro_v3_tools_mismatch", "unrecognized /tools layout")
    rows = rows[1:]
    more_below = bool(rows) and _TOOLS_MORE_BELOW.match(rows[-1]) is not None
    if more_below:
        rows = rows[:-1]
    if (more_above or more_below) and not rows:
        raise KiroAttestationError("kiro_v3_tools_mismatch", "unrecognized /tools layout")
    tools: List[Tuple[str, str]] = []
    for row in rows:
        parsed = _TOOLS_ROW.match(row)
        if parsed is None:
            raise KiroAttestationError(
                "kiro_v3_tools_mismatch", f"unrecognized row {row.strip()!r}"
            )
        name, source = parsed.groups()
        tools.append((name, source))
    return ToolsPage(declared, tuple(tools), more_above, more_below)


def check_tools_page(page: ToolsPage, previous: Optional[ToolsPage] = None) -> None:
    """Refuse unless ``page`` can be the next screen read of one ``/tools`` panel.

    With no ``previous``, the panel has just opened, so it must start at its
    first row (no ``↑ more``). Otherwise ``page`` was read after one Down on
    ``previous``, and Kiro's Down moves a scrolling list by one row: so it
    must have the same title and the same number of rows, all but the top row
    of ``previous`` at its top, and one row ``previous`` did not show at its
    bottom. A frame that still shows the old screen, one drawn halfway, one
    that moved by more than one row, or a list that changed, refuses. So do
    old rows left above the pane under the one title, which is what Kiro's
    default redraw leaves on a pane under 12 rows: this check, not the
    one-title rule, is what keeps them out.
    """
    if previous is None:
        if page.more_above:
            raise KiroAttestationError(
                "kiro_v3_tools_mismatch", "the /tools panel did not start at its first row"
            )
        return
    if not (
        previous.more_below
        and page.more_above
        and page.declared == previous.declared
        and len(page.rows) == len(previous.rows) >= 1
        and page.rows[:-1] == previous.rows[1:]
        and page.rows[-1] not in previous.rows
    ):
        raise KiroAttestationError(
            "kiro_v3_tools_mismatch", "the /tools panel did not move down by one row"
        )


def join_tools_pages(pages: Sequence[ToolsPage]) -> ToolsPanel:
    """Join the screens of one ``/tools`` panel, read one Down apart, and check them.

    The first screen must be the top of the list and the last its bottom
    (no ``↓ more``); every later screen must follow the one before it
    (``check_tools_page``), so only its bottom row is new.
    The joined rows must hold no row twice and exactly the count the title
    declares. A clipped, scrolled or partly read list refuses: never pass on
    a partial read.
    """
    if not pages:
        raise KiroAttestationError("kiro_v3_tools_mismatch", "no /tools panel was read")
    first = pages[0]
    check_tools_page(first)
    rows = list(first.rows)
    for previous, page in zip(pages, pages[1:]):
        check_tools_page(page, previous)
        rows.append(page.rows[-1])
    if pages[-1].more_below:
        raise KiroAttestationError(
            "kiro_v3_tools_mismatch", "the /tools panel has rows below the last screen read"
        )
    twice = sorted(name for (name, _), count in Counter(rows).items() if count > 1)
    if twice:
        raise KiroAttestationError(
            "kiro_v3_tools_mismatch", f"the /tools panel shows {twice} more than once"
        )
    if len(rows) != first.declared:
        raise KiroAttestationError(
            "kiro_v3_tools_mismatch",
            f"the panel declares {first.declared} tags but {len(rows)} are visible",
        )
    tags = {name for name, source in rows if source == "builtin"}
    mcp_tools = {name for name, source in rows if source != "builtin"}
    return ToolsPanel(frozenset(tags), frozenset(mcp_tools))


def parse_tools_panel(screen: str) -> ToolsPanel:
    """Parse a v3 ``/tools · N tags`` panel that fits on one screen; refuse any other.

    A panel that scrolls refuses here: read it screen by screen and join the
    screens with ``join_tools_pages``.
    """
    return join_tools_pages([parse_tools_page(screen)])


def parse_mcp_panel(screen: str) -> Dict[str, McpServerRow]:
    """Parse the v3 ``/mcp list · N configured`` panel; refuse any other layout."""
    body = _body(screen, _MCP_TITLE)
    if body is None:
        raise KiroAttestationError("kiro_v3_mcp_not_ready", "no complete /mcp list panel on screen")
    match = _MCP_TITLE.match(body[0])
    assert match is not None
    declared = int(match.group(1))
    rows = [line for line in body[1:] if line.strip() and not _SEPARATOR.match(line)]
    if rows and _SEARCH.match(rows[0]):
        rows = rows[1:]
    if len(rows) < 2 or not _MCP_HEADING.match(rows[0]) or not _MCP_COLUMNS.match(rows[1]):
        if declared == 0 and not any(_MCP_COLUMNS.match(row) for row in rows):
            return {}
        raise KiroAttestationError("kiro_v3_mcp_not_ready", "unrecognized /mcp list layout")
    servers: Dict[str, McpServerRow] = {}
    for row in rows[2:]:
        if _MCP_EDIT_HINT.match(row):
            continue
        parsed = _MCP_ROW.match(row)
        if parsed is None:
            raise KiroAttestationError("kiro_v3_mcp_not_ready", f"unrecognized row {row.strip()!r}")
        name, status, details = parsed.group(1), parsed.group(3), parsed.group(4)
        count = _MCP_TOOL_COUNT.match(details)
        servers[name] = McpServerRow(name, status, int(count.group(1)) if count else 0)
    if len(servers) != declared:
        raise KiroAttestationError(
            "kiro_v3_mcp_not_ready",
            f"the panel declares {declared} servers but {len(servers)} are visible",
        )
    return servers


def granted_tags(
    grant: Iterable[str], schemas: Optional[Iterable[KiroV3EngineSchema]] = None
) -> frozenset[str]:
    """The builtin tags a CAO grant may show in ``/tools``.

    ``/tools`` shows one row for each tag that an exposed built-in carries: a
    file whose ``tools`` bring ``code`` shows both ``read`` and ``write``. The
    exposed built-ins are the grant's exact v3 tools, so the tags are read from
    each pinned schema's tag map (``schema.tags``), not kept in a second table.
    Where the given engines disagree, only tags that every one implies are
    allowed. Any MCP reference also implies ``read``: the MCP disclosure tools
    carry it.
    """
    entries = list(grant)
    exact = kiro_v3_exact_tools(entries)
    per_engine = [
        frozenset(tag for tag, tools in schema.tags.items() if tools & exact)
        for schema in (pinned_kiro_v3_schemas() if schemas is None else schemas)
    ]
    tags: Set[str] = set(frozenset.intersection(*per_engine)) if per_engine else set()
    if any(split_mcp_reference(entry) is not None for entry in entries):
        tags.add("read")
    return frozenset(tags)


def check_tools_panel(
    panel: ToolsPanel,
    grant: List[str],
    schemas: Optional[Iterable[KiroV3EngineSchema]] = None,
) -> None:
    """Refuse a tag outside the grant's tags, or an MCP tool of an ungranted server.

    ``schemas`` defaults to every pinned engine schema (see ``granted_tags``).
    """
    extra_tags = sorted(panel.tags - granted_tags(grant, schemas))
    if extra_tags:
        raise KiroAttestationError("kiro_v3_tools_mismatch", f"tags beyond the grant: {extra_tags}")
    refs = [entry for entry in grant if split_mcp_reference(entry) is not None]
    for tool in sorted(panel.mcp_tools):
        if not any(mcp_reference_covers(ref, tool) for ref in refs):
            raise KiroAttestationError("kiro_v3_tools_mismatch", f"MCP tool {tool} is not granted")


def check_mcp_panel(servers: Dict[str, McpServerRow], expected: Iterable[str]) -> Tuple[str, ...]:
    """Require exactly the ``expected`` servers, each running with a tool.

    Returns the servers still loading (the caller opens the panel again until
    its timeout); raises on anything else.
    """
    wanted = set(expected)
    extra = sorted(set(servers) - wanted)
    missing = sorted(wanted - set(servers))
    if extra or missing:
        raise KiroAttestationError(
            "kiro_v3_mcp_not_ready", f"servers differ: extra {extra}, missing {missing}"
        )
    loading = tuple(
        sorted(name for name, row in servers.items() if row.status.split()[-1] == "loading")
    )
    for name, row in sorted(servers.items()):
        if name in loading:
            continue
        if row.status != "● running" or row.tools < 1:
            raise KiroAttestationError(
                "kiro_v3_mcp_not_ready", f"{name} is {row.status!r} with {row.tools} tools"
            )
    return loading


def palette_shows_only(screen: str, command: str) -> bool:
    """Whether the typed slash command's palette is the safe, unselected one.

    ``command`` is ``/tools`` or ``/mcp``. The completion row for it must be
    present, its footer must be the cancel/select one, and no ``❯`` selection
    (``trust-all`` after a Tab) may be on screen.
    """
    pattern = _TYPED_TOOLS if command == "/tools" else _TYPED_MCP
    lines = screen.splitlines()
    return (
        any(pattern.match(line) for line in lines)
        and any(_PALETTE_FOOTER.match(line) for line in lines)
        and not any(_PALETTE_SELECTED.match(line) for line in lines)
    )


__all__ = [
    "KiroAttestationError",
    "McpServerRow",
    "ToolsPage",
    "ToolsPanel",
    "check_mcp_panel",
    "check_tools_page",
    "check_tools_panel",
    "granted_tags",
    "join_tools_pages",
    "palette_shows_only",
    "parse_mcp_panel",
    "parse_tools_page",
    "parse_tools_panel",
]
