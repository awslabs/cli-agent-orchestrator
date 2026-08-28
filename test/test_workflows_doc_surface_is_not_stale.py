"""``docs/workflows.md``'s two reference tables match the live surfaces (issue #583 Bolt 3).

WHY THIS EXISTS, and it is the second instance of one failure shape rather than a new idea.

``test_command_catalog_counts_are_not_stale.py`` was written this same Bolt because ``catalog.rs``
carried four stale command counts in prose while every test was green. Its docstring names the cause:
"nothing compared the table against the CLI." Bolt 3 then did the same thing one file over —
``docs/workflows.md`` announced "All thirteen verbs" and "Eleven workflow tools are exposed over MCP"
after this Bolt added two verbs and four tools, so the shipped documentation of the authoring feature
was false about the authoring feature. The whole suite was green, including the three tests that read
``docs/workflows.md`` for other reasons, because none of them counts anything.

So this test compares the prose and both tables against the live surfaces. Two distinct checks,
because a count and a table drift independently:

* the **spelled-out count** in the prose sentence, which is what a reader believes before they count
  the rows themselves;
* the **table rows**, in both directions — a surface with no row is undocumented, and a row with no
  surface documents something that does not exist.

The count check alone would be a false green whenever one row is added and another dropped. The row
check alone would be a false green whenever the table is complete and the sentence above it is wrong,
which is exactly the state this Bolt shipped in before the fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

DOC = Path(__file__).resolve().parents[1] / "docs" / "workflows.md"

#: The spelled-out numbers the two sentences can use. Kept as an explicit map rather than a
#: general-purpose number-word parser: the set of counts a reference table can plausibly have is
#: small, and an unmapped word should fail loudly rather than parse to something plausible.
_NUMBER_WORDS = {
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def _doc() -> str:
    assert DOC.is_file(), f"expected the workflows guide at {DOC}"
    return DOC.read_text()


def _spelled_count(source: str, pattern: str) -> int:
    """The integer value of the spelled-out number in the sentence matching ``pattern``."""
    m = re.search(pattern, source, re.IGNORECASE)
    assert m is not None, f"the sentence stating the count no longer matches {pattern!r}"
    word = m.group(1).lower()
    assert word in _NUMBER_WORDS, f"unmapped number word {word!r} — add it to _NUMBER_WORDS"
    return _NUMBER_WORDS[word]


def _live_cli_verbs() -> set[str]:
    """Every leaf verb registered under ``cao workflow``, walked from the Click tree."""
    from cli_agent_orchestrator.cli.main import cli

    assert isinstance(cli, click.Group)
    workflow = cli.commands.get("workflow")
    assert isinstance(workflow, click.Group), "`cao workflow` is no longer a Click group"
    return set(workflow.commands)


def _live_mcp_workflow_tools() -> set[str]:
    """Every ``workflow_*`` coroutine registered as an MCP tool in the server module.

    Read from the source with a regex rather than by importing and introspecting the FastMCP
    registry: the decorator wraps each function, and the registry's internal shape is not a
    contract this test should depend on. What a reader of the docs cares about is which
    ``@mcp.tool()``-decorated ``workflow_*`` functions exist, and that is textual.
    """
    server = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "cli_agent_orchestrator"
        / "mcp_server"
        / "server.py"
    )
    assert server.is_file(), f"expected the MCP server at {server}"
    return set(
        re.findall(
            r"@mcp\.tool\(\)\s*\n(?:@[^\n]*\n)*async def (workflow_[a-z_]+)\s*\(",
            server.read_text(),
        )
    )


def _table_rows(source: str, heading: str, first_column: str) -> set[str]:
    """The first-column cell of every row in the Markdown table under ``heading``.

    ``first_column`` is the header cell that identifies the right table, so a section that grows a
    second table does not silently widen this parse.
    """
    start = source.index(heading)
    section = source[start : source.index("\n## ", start + len(heading))]
    header = section.index(f"| {first_column} |")
    body = section[header:]
    rows: set[str] = set()
    for line in body.splitlines()[2:]:  # skip the header row and the |---| separator
        if not line.startswith("|"):
            break
        cell = line.split("|")[1].strip()
        # Rows name their subject in backticks; take the first token inside them and drop any
        # argument placeholder, so `create <name>` and `workflow_create` both reduce to a bare name.
        m = re.search(r"`([a-z_]+)", cell)
        if m:
            rows.add(m.group(1))
    return rows


def test_the_parse_is_not_vacuous():
    """Guard the guard: an empty parse would make every assertion below pass trivially."""
    source = _doc()
    assert _live_cli_verbs(), "walked no CLI verbs — the Click walk is broken"
    assert _live_mcp_workflow_tools(), "found no MCP workflow tools — the regex is broken"
    assert _table_rows(source, "## CLI reference", "Verb"), "parsed no rows from the CLI table"
    assert _table_rows(source, "## MCP tool reference", "Tool"), "parsed no rows from the MCP table"


def test_the_documented_cli_verb_count_matches_the_click_tree():
    expected = len(_live_cli_verbs())
    stated = _spelled_count(_doc(), r"All (\w+) verbs live under")
    assert stated == expected, (
        f"docs/workflows.md says {stated} `cao workflow` verbs; Click registers {expected}. "
        "Update the sentence and the table together."
    )


def test_every_cli_verb_has_a_row_and_every_row_has_a_verb():
    documented = _table_rows(_doc(), "## CLI reference", "Verb")
    live = _live_cli_verbs()
    assert not (live - documented), f"CLI verbs with no table row: {sorted(live - documented)}"
    assert not (documented - live), f"table rows with no CLI verb: {sorted(documented - live)}"


def test_the_documented_mcp_tool_count_matches_the_server():
    expected = len(_live_mcp_workflow_tools())
    stated = _spelled_count(_doc(), r"(\w+) workflow tools are exposed over MCP")
    assert stated == expected, (
        f"docs/workflows.md says {stated} MCP workflow tools; the server registers {expected}. "
        "Update the sentence and the table together."
    )


def test_every_mcp_tool_has_a_row_and_every_row_has_a_tool():
    documented = _table_rows(_doc(), "## MCP tool reference", "Tool")
    live = _live_mcp_workflow_tools()
    assert not (live - documented), f"MCP tools with no table row: {sorted(live - documented)}"
    assert not (documented - live), f"table rows with no MCP tool: {sorted(documented - live)}"
