"""Read a Kiro v3 pane's ``/tools`` and ``/mcp list`` panels before any task (#1167).

The TUI header names the agent, not its tools: a same-name workspace agent
with no tools, or a file that widened on load, still shows the right name. So
before a v3 terminal is recorded as verified, CAO opens both panels in the
verified pane and checks what the engine actually loaded:

* every builtin tag is one the grant implies, and every MCP row belongs to a
  granted server (and tool);
* ``/mcp list`` shows exactly the configured servers, each running with at
  least one tool.

The parsers are pure and fail closed: any layout they do not recognize
(v2's per-tool panels, the slash palette, a clipped list) is a refusal, never
a pass. Provisional (#1167): the codes ``kiro_v3_tools_mismatch`` and
``kiro_v3_mcp_not_ready`` and the panel shapes, captured on kiro-cli 2.35.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3Refusal
from cli_agent_orchestrator.utils.tool_mapping import mcp_reference_covers, split_mcp_reference

_SEPARATOR = re.compile(r"^\s*─{20,}\s*$")
_TOOLS_TITLE = re.compile(r"^\s*/tools · (\d+) tags?\s*$")
_TOOLS_COLUMNS = re.compile(r"^\s*Name\s+Source\s+Description\s*$")
_TOOLS_ROW = re.compile(r"^\s*(\S+)\s{2,}(builtin|mcp)(?:\s{2,}.*)?$")
_NO_TOOLS = re.compile(r"^\s*No tools available\s*$")
_MCP_TITLE = re.compile(r"^\s*/mcp list · (\d+) configured\s*$")
_MCP_HEADING = re.compile(r"^\s*Configured Servers\s*$")
_MCP_COLUMNS = re.compile(r"^\s*Name\s+Source\s+Status\s+Details\s*$")
# Status is a glyph and a word ("● running", "✗ failed"); a server still
# starting reads "<glyph> loading" (glyph not captured yet), so only the word
# is relied on for that state.
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

#: Builtin tags each CAO atom implies. MCP disclosure tools carry ``read``, so
#: any MCP grant implies it too.
_TAGS_BY_ATOM = {
    "fs_read": {"read"},
    "fs_list": {"read"},
    "fs_write": {"write"},
    "execute_bash": {"shell", "subagent"},
    "web_fetch": {"web"},
}


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


def parse_tools_panel(screen: str) -> ToolsPanel:
    """Parse the v3 ``/tools · N tags`` panel; refuse any other layout."""
    body = _body(screen, _TOOLS_TITLE)
    if body is None:
        raise KiroAttestationError("kiro_v3_tools_mismatch", "no complete /tools panel on screen")
    match = _TOOLS_TITLE.match(body[0])
    assert match is not None
    declared = int(match.group(1))
    rows = [line for line in body[1:] if line.strip() and not _SEPARATOR.match(line)]
    if rows and _SEARCH.match(rows[0]):
        rows = rows[1:]
    if rows and _NO_TOOLS.match(rows[0]):
        raise KiroAttestationError("kiro_v3_tools_mismatch", "the agent has no tools available")
    if not rows or not _TOOLS_COLUMNS.match(rows[0]):
        raise KiroAttestationError("kiro_v3_tools_mismatch", "unrecognized /tools layout")
    tags: Set[str] = set()
    mcp_tools: Set[str] = set()
    for row in rows[1:]:
        parsed = _TOOLS_ROW.match(row)
        if parsed is None:
            raise KiroAttestationError(
                "kiro_v3_tools_mismatch", f"unrecognized row {row.strip()!r}"
            )
        name, source = parsed.groups()
        (tags if source == "builtin" else mcp_tools).add(name)
    if len(tags) + len(mcp_tools) != declared:
        # A clipped or scrolled list could hide a row; never pass on a partial read.
        raise KiroAttestationError(
            "kiro_v3_tools_mismatch",
            f"the panel declares {declared} tags but {len(tags) + len(mcp_tools)} are visible",
        )
    return ToolsPanel(frozenset(tags), frozenset(mcp_tools))


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


def granted_tags(grant: Iterable[str]) -> frozenset[str]:
    """The builtin tags a CAO grant may show in ``/tools``."""
    tags: Set[str] = set()
    for entry in grant:
        if entry == "fs_*":
            tags.update({"read", "write"})
        elif entry in _TAGS_BY_ATOM:
            tags.update(_TAGS_BY_ATOM[entry])
        elif split_mcp_reference(entry) is not None:
            tags.add("read")
    return frozenset(tags)


def check_tools_panel(panel: ToolsPanel, grant: List[str]) -> None:
    """Refuse a tag outside the grant's tags, or an MCP tool of an ungranted server."""
    extra_tags = sorted(panel.tags - granted_tags(grant))
    if extra_tags:
        raise KiroAttestationError("kiro_v3_tools_mismatch", f"tags beyond the grant: {extra_tags}")
    refs = [entry for entry in grant if split_mcp_reference(entry) is not None]
    for tool in sorted(panel.mcp_tools):
        if not any(mcp_reference_covers(ref, tool) for ref in refs):
            raise KiroAttestationError("kiro_v3_tools_mismatch", f"MCP tool {tool} is not granted")


def check_mcp_panel(servers: Dict[str, McpServerRow], expected: Iterable[str]) -> Tuple[str, ...]:
    """Require exactly the ``expected`` servers, each running with a tool.

    Returns the servers still loading (the caller reads again until its
    timeout); raises on anything else.
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
    "ToolsPanel",
    "check_mcp_panel",
    "check_tools_panel",
    "granted_tags",
    "palette_shows_only",
    "parse_mcp_panel",
    "parse_tools_panel",
]
