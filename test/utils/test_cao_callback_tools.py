"""A Kiro install keeps the CAO callback tools the tool gate always allows.

Kiro gives an agent only the tools in its installed ``tools`` list, written from the
resolved allowlist. A profile that pinned ``allowedTools`` without ``@cao-mcp-server``
therefore lost ``workflow_return`` and ``send_message`` on Kiro, though the gate lets
every bound terminal call them. ``with_cao_callback_tools`` adds them at install, and
the delegation ceiling treats them as granting nothing new.
"""

from __future__ import annotations

from cli_agent_orchestrator.mcp_server.server import CALLBACK_EXEMPT_TOOLS
from cli_agent_orchestrator.utils.delegation import covers, tools_exceeding
from cli_agent_orchestrator.utils.tool_mapping import (
    CAO_CALLBACK_REFS,
    CAO_CALLBACK_TOOLS,
    kiro_agent_tools,
    with_cao_callback_tools,
)

SERVERS = ["cao-mcp-server", "creds-agent"]
REFS = [
    "@cao-mcp-server/answer_user_prompt",
    "@cao-mcp-server/complete_assignment",
    "@cao-mcp-server/send_message",
    "@cao-mcp-server/workflow_return",
]


def test_the_gate_exemption_and_the_install_list_are_one_set() -> None:
    assert CALLBACK_EXEMPT_TOOLS is CAO_CALLBACK_TOOLS
    assert sorted(CAO_CALLBACK_REFS) == REFS


def test_a_pinned_allowlist_gains_the_four_callback_tools_after_its_own_entries() -> None:
    allowed = ["execute_bash", "fs_read", "@creds-agent"]

    assert with_cao_callback_tools(allowed, SERVERS) == [*allowed, *REFS]
    assert allowed == ["execute_bash", "fs_read", "@creds-agent"], "the input is not changed"


def test_the_kiro_tools_list_then_holds_workflow_return_and_no_other_cao_tool() -> None:
    tools = kiro_agent_tools(with_cao_callback_tools(["execute_bash", "fs_read"], SERVERS))

    assert "@cao-mcp-server/workflow_return" in tools
    assert [t for t in tools if t.startswith("@cao-mcp-server")] == REFS
    assert "@cao-mcp-server" not in tools


def test_a_callback_tool_already_granted_is_not_added_twice() -> None:
    allowed = ["fs_read", "@cao-mcp-server/send_message"]

    result = with_cao_callback_tools(allowed, SERVERS)

    assert result.count("@cao-mcp-server/send_message") == 1
    assert sorted(result[1:]) == REFS


def test_a_grant_that_covers_the_whole_server_is_unchanged() -> None:
    for allowed in (["fs_read", "@cao-mcp-server"], ["fs_read", "@cao-*"], ["*"]):
        assert with_cao_callback_tools(allowed, SERVERS) == allowed


def test_a_tool_pattern_grant_counts_for_the_tools_it_covers() -> None:
    result = with_cao_callback_tools(["@cao-mcp-server/send_*"], SERVERS)

    assert "@cao-mcp-server/send_message" not in result
    assert "@cao-mcp-server/workflow_return" in result


def test_a_profile_without_the_cao_server_gains_nothing() -> None:
    allowed = ["execute_bash", "fs_read"]

    assert with_cao_callback_tools(allowed, ["creds-agent"]) == allowed
    assert with_cao_callback_tools(allowed, None) == allowed


def test_the_ceiling_covers_callback_refs_and_nothing_else_of_the_server() -> None:
    ceiling = ["fs_read"]

    for ref in REFS:
        assert covers(ceiling, ref)
    assert not covers(ceiling, "@cao-mcp-server/handoff")
    assert not covers(ceiling, "@cao-mcp-server")
    assert not covers(ceiling, "@cao-mcp-server/*")
    assert tools_exceeding(["fs_read", *REFS], ceiling) == []
