"""Preserve transcript row grammar without overlapping whitespace backtracking."""

import subprocess
import sys

import pytest

from cli_agent_orchestrator.providers import kimi_transcript as kt


@pytest.mark.parametrize(
    "row",
    [
        "Loading agent...",
        "\tLoading configuration \u2026\t",
        "\u280b Restoring conversation . \u2026",
        "Resolving dependencies",
        "Send /help for help information",
        "No session yet",
        "No session yet \u2014 start a conversation",
        "Run /web to continue your session in the browser",
        "\u2726 Try Kimi Code Web UI now",
        'MCP server "example" connected \u00b7 ready',
        "tmux extended-keys is off; configure tmux",
        "\u2827 MCP Servers: 0/1 connected, 0 tools\t",
        "\u2826 example-mcp-server (connecting)\u2003",
        "connecting to mcp servers ... (1/3)",
        "\u2003CONNECTING TO MCP SERVERS \u00b7 2/4\t",
    ],
)
def test_boot_row_variants_remain_chrome(row):
    assert kt.is_boot_chrome_line(row)
    assert kt.classify_line(row) is kt.KimiLineKind.BOOT_CHROME


@pytest.mark.parametrize(
    "row",
    [
        "Loading agent\t!",
        "Restoring conversation\u2003not startup",
        "connecting to mcp servers\t!",
        "connecting to mcp servers is only a phrase",
        "\u2826 example-mcp-server (connecting) is only a phrase",
        "\u25cf Loading configuration is the next step",
    ],
)
def test_nonmatching_boot_vocabulary_is_not_chrome(row):
    assert not kt.is_boot_chrome_line(row)
    assert kt.classify_line(row) is not kt.KimiLineKind.BOOT_CHROME


@pytest.mark.parametrize("bullet", ["", "\u2022", "\u25cf"])
@pytest.mark.parametrize("whitespace", ["", " ", "\t", "\t \u2003"])
def test_collapsed_output_preserves_bullets_and_indentation(bullet, whitespace):
    row = f"{whitespace}{bullet}{whitespace}\u2026{whitespace}(23 more lines, expand)"
    assert kt.classify_line(row) is kt.KimiLineKind.TOOL_CHROME


@pytest.mark.parametrize(
    "row",
    [
        "The UI shows \u2026 (23 more lines, expand)",
        "\u25cf The UI shows \u2026 (23 more lines, expand)",
        "\t\u2026 not a collapsed-output counter",
        "\u2022 \u25cf \u2026 (23 more lines, expand)",
    ],
)
def test_collapsed_output_mentions_are_not_tool_chrome(row):
    assert kt.classify_line(row) is not kt.KimiLineKind.TOOL_CHROME


@pytest.mark.parametrize(
    "pattern_name,prefix,repeated,accepted_tail,rejected_tail",
    [
        ("BOOT_MESSAGE_ROW_RE", "Restoring conversation", "\t", "\u2026", "!"),
        ("BOOT_MESSAGE_ROW_RE", "Loading agent", " \u2003", "...", "!"),
        ("MCP_BOOT_ROW_RE", "CONNECTING TO MCP SERVERS", "\t", " (2/5)", "!"),
        ("MCP_BOOT_ROW_RE", "connecting to mcp servers", " \u2003", "...", "!"),
        ("COLLAPSED_TOOL_OUTPUT_RE", "", "\t", "\u2026 (23 more lines", "!"),
        ("COLLAPSED_TOOL_OUTPUT_RE", "", " \u2003", "\u2026 (2 more lines", "!"),
        ("BOOT_MESSAGE_ROW_RE", "No session yet \u2014 detail", "\t", "\n...", "\n!"),
        (
            "BOOT_MESSAGE_ROW_RE",
            'MCP server "example" connected \u00b7 detail',
            " \u2003",
            "\n...",
            "\n!",
        ),
        ("BOOT_MESSAGE_ROW_RE", "tmux extended-keys is off", "\t", "\n", "\n!"),
        ("BOOT_MESSAGE_ROW_RE", "\u2726 Try Kimi Code Web UI", "\t", "\n", "\n!"),
        ("MCP_BOOT_ROW_RE", "\u2827 MCP Servers: 0/1", "\t", "\n", "\n!"),
        ("MCP_BOOT_ROW_RE", "\u2827 MCP Servers: 0/", "1", "\n", "\n!"),
        ("MCP_BOOT_ROW_RE", "\u2826 example (connecting)", "\t", "\n", "\n!"),
        ("MCP_BOOT_ROW_RE", "\u2826", "\u00a0", "example (connecting)", "\n!"),
        ("MCP_BOOT_ROW_RE", "\u2826", "xa0", "(connecting)", "\n!"),
    ],
)
def test_long_matching_and_nonmatching_whitespace_remains_bounded(
    pattern_name, prefix, repeated, accepted_tail, rejected_tail
):
    # A child timeout bounds regressions without leaving a stuck regex in pytest.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from cli_agent_orchestrator.providers import kimi_transcript as kt

pattern = getattr(kt, sys.argv[1])
for length in (32768, 131072):
    row = sys.argv[2] + sys.argv[3] * length
    assert not pattern.search(row + sys.argv[5])
    assert pattern.search(row + sys.argv[4])
""",
            pattern_name,
            prefix,
            repeated,
            accepted_tail,
            rejected_tail,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
