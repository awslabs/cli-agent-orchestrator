"""The Kiro v3 ``/tools`` and ``/mcp list`` attestation parsers (#1167 Phase 1, gate G8).

Fixtures are kiro-cli 2.35.1 v3 panes captured in the #1167 measurements, with
the scratch path redacted at the same width. Every row of the design's D14
table gives its verdict here; v2's panels and the slash palette refuse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.kiro_attest import (
    KiroAttestationError,
    McpServerRow,
    check_mcp_panel,
    check_tools_panel,
    granted_tags,
    palette_shows_only,
    parse_mcp_panel,
    parse_tools_panel,
)

FIXTURES = Path(__file__).parent / "fixtures"
READ_ONLY_MCP = ["fs_read", "fs_list", "@cao-mcp-server"]
READ_ONLY = ["fs_read", "fs_list"]


def _screen(name: str) -> str:
    return (FIXTURES / f"kiro_cli_tui_2_35_1_v3_{name}.txt").read_text(encoding="utf-8")


def _tools_verdict(name: str, grant):
    check_tools_panel(parse_tools_panel(_screen(name)), grant)


@pytest.mark.parametrize(
    ("fixture", "grant"),
    [
        ("tools_readonly_mcp", READ_ONLY_MCP),
        ("tools_trustall", READ_ONLY_MCP),
        ("tools_web_write", ["fs_read", "fs_write", "web_fetch"]),
    ],
)
def test_matching_tools_panels_pass(fixture, grant):
    _tools_verdict(fixture, grant)


@pytest.mark.parametrize(
    ("fixture", "grant", "why"),
    [
        ("tools_read_tag_write", READ_ONLY, "write"),
        ("tools_shell_visible", READ_ONLY, "shell"),
        ("tools_default", READ_ONLY_MCP, "shell"),
        ("tools_none", READ_ONLY_MCP, "no tools"),
        ("tools_web_write", READ_ONLY, "web"),
        ("tools_universal", READ_ONLY, "write"),
        ("tools_readonly_mcp", READ_ONLY, "not granted"),
        ("palette_tools_tab", READ_ONLY_MCP, "no complete /tools panel"),
        ("palette_slash_menu", READ_ONLY_MCP, "no complete /tools panel"),
        ("v2_tools_panel", READ_ONLY_MCP, "no complete /tools panel"),
        ("idle", READ_ONLY_MCP, "no complete /tools panel"),
    ],
)
def test_mismatching_or_unrecognized_tools_panels_refuse(fixture, grant, why):
    with pytest.raises(KiroAttestationError) as excinfo:
        _tools_verdict(fixture, grant)
    assert excinfo.value.code == "kiro_v3_tools_mismatch"
    assert why in str(excinfo.value)


def test_a_clipped_tools_list_refuses():
    screen = _screen("tools_readonly_mcp")
    clipped = "\n".join(line for line in screen.splitlines() if "/workflow_wait" not in line)
    with pytest.raises(KiroAttestationError, match="declares 35 tags but 34"):
        parse_tools_panel(clipped)


def test_a_per_tool_grant_covers_only_its_tool():
    panel = parse_tools_panel(_screen("tools_readonly_mcp"))
    with pytest.raises(KiroAttestationError, match="is not granted"):
        check_tools_panel(panel, ["fs_read", "@cao-mcp-server/send_message"])


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [("mcp_running", ["cao-mcp-server"]), ("mcp_decoys_hidden", ["cao-mcp-server"])],
)
def test_matching_mcp_panels_pass(fixture, expected):
    assert check_mcp_panel(parse_mcp_panel(_screen(fixture)), expected) == ()


def test_decoy_servers_shown_refuse():
    servers = parse_mcp_panel(_screen("mcp_decoys_shown"))
    assert set(servers) == {"kv3-user-decoy", "kv3-ws-decoy", "cao-mcp-server"}
    with pytest.raises(KiroAttestationError) as excinfo:
        check_mcp_panel(servers, ["cao-mcp-server"])
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert "extra" in str(excinfo.value)


@pytest.mark.parametrize(
    "fixture", ["v2_mcp_panel", "palette_slash_menu", "idle", "tools_readonly_mcp"]
)
def test_unrecognized_mcp_layouts_refuse(fixture):
    with pytest.raises(KiroAttestationError) as excinfo:
        parse_mcp_panel(_screen(fixture))
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"


@pytest.mark.parametrize(
    ("row", "outcome"),
    [
        (McpServerRow("cao-mcp-server", "● running", 34), ()),
        (McpServerRow("cao-mcp-server", "⠋ loading", 0), ("cao-mcp-server",)),
        (McpServerRow("cao-mcp-server", "✗ failed", 0), "failed"),
        (McpServerRow("cao-mcp-server", "● running", 0), "0 tools"),
    ],
)
def test_server_states(row, outcome):
    servers = {row.name: row}
    if isinstance(outcome, tuple):
        assert check_mcp_panel(servers, [row.name]) == outcome
    else:
        with pytest.raises(KiroAttestationError, match=outcome):
            check_mcp_panel(servers, [row.name])


def test_a_missing_expected_server_refuses():
    with pytest.raises(KiroAttestationError, match="missing"):
        check_mcp_panel(parse_mcp_panel(_screen("mcp_running")), ["cao-mcp-server", "other"])


@pytest.mark.parametrize(
    ("grant", "tags"),
    [
        (READ_ONLY_MCP, {"read"}),
        (["@cao-mcp-server"], {"read"}),
        (["fs_write"], {"write"}),
        (["fs_*"], {"read", "write"}),
        (["execute_bash"], {"shell", "subagent"}),
        (["web_fetch"], {"web"}),
        (["@builtin"], set()),
        ([], set()),
    ],
)
def test_granted_tags(grant, tags):
    assert granted_tags(grant) == tags


def test_the_typed_palettes_are_recognized_and_tab_is_not():
    assert palette_shows_only(_screen("typed_tools"), "/tools")
    assert palette_shows_only(_screen("typed_mcp"), "/mcp")
    assert not palette_shows_only(_screen("palette_tools_tab"), "/tools")
    assert not palette_shows_only(_screen("typed_mcp"), "/tools")
    assert not palette_shows_only(_screen("idle"), "/tools")
