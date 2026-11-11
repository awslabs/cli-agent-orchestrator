"""The Cooper mail agent can report, and cannot write memory or start agents.

Review of #1150: ``allowedTools: ["@cooper-email", "@cao-mcp-server"]`` let an
injected email call ``memory_store`` and leave text in global CAO memory for
later agents. The grant is now ``["@cooper-email"]``. ``cao-mcp-server`` stays
in ``mcpServers`` so Claude Code still exposes the callback tools. The gate
checks the caller's allowlist, so a supervisor can ``handoff`` to this agent,
and ``send_message`` stays reachable because it is callback-exempt.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import frontmatter
import pytest
from fastmcp import Client, FastMCP

from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils.delegation import tools_exceeding
from cli_agent_orchestrator.utils.enforcement import PROMPT, enforcement_for
from cli_agent_orchestrator.utils.tool_mapping import (
    get_disallowed_tools,
    granted_mcp_servers,
    mcp_reference_covers,
    resolve_allowed_tools,
)

PROFILE_PATH = Path(__file__).resolve().parents[2] / "examples/cooper-email-inbox/mail_agent.md"
BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}
DENIED_TO_THE_MAIL_AGENT = (
    "memory_store",
    "memory_forget",
    "store_lesson",
    "handoff",
    "assign",
)


def _profile() -> AgentProfile:
    return AgentProfile(**frontmatter.load(PROFILE_PATH).metadata)


def _allowed() -> list[str]:
    profile = _profile()
    return resolve_allowed_tools(
        profile.allowedTools, profile.role, grantable_server_names(profile)
    )


def _ctx(allowed_tools):
    return {
        "terminal_id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "claude_code",
        "agent_profile": "mail_agent",
        "allowed_tools": allowed_tools,
    }


def _server(tool_names):
    mcp = FastMCP("cao-mcp-server-under-test")
    for name in tool_names:

        def _impl(_name=name) -> dict:
            return {"reached": _name}

        mcp.add_tool(server.Tool.from_function(_impl, name=name))
    server._install_allowlist_middleware(mcp)
    return mcp


def _call(mcp, name):
    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(name, {})
            return result.data

    return asyncio.run(go())


def _covers_denied_cao_tool(entry: str) -> bool:
    prefix = f"{server.CAO_MCP_SERVER_SELECTOR}/"
    return any(mcp_reference_covers(entry, prefix + name) for name in DENIED_TO_THE_MAIL_AGENT)


def test_the_profile_grants_only_the_inbox_server():
    profile = _profile()
    assert profile.allowedTools == ["@cooper-email"]
    assert profile.provider == "claude_code"
    assert "cao-mcp-server" in profile.mcpServers
    assert "cooper-email" in profile.mcpServers
    allowed = _allowed()
    assert allowed == ["@cooper-email"]
    assert server.CAO_MCP_SERVER_SELECTOR not in allowed


def test_claude_code_and_grok_block_file_reads_and_mcode_does_not():
    """``mcode`` is prompt-only. ``minimax_code`` is not a provider id."""
    ids = {provider.value for provider in ProviderType}
    assert "mcode" in ids
    assert "minimax_code" not in ids
    allowed = _allowed()
    for provider in ("claude_code", "grok_cli"):
        blocked = get_disallowed_tools(provider, allowed)
        assert "Read" in blocked
        assert enforcement_for(provider) != PROMPT
    assert get_disallowed_tools("mcode", allowed) == []
    assert enforcement_for("mcode") == PROMPT


def test_grok_does_not_permit_the_ungranted_cao_server():
    """Grok's deny-by-default allows only servers named in ``allowedTools``."""
    profile = _profile()
    granted = granted_mcp_servers(_allowed(), profile.mcpServers)
    assert granted == ["cooper-email"]


@pytest.mark.parametrize("tool", DENIED_TO_THE_MAIL_AGENT)
def test_the_mail_agent_cannot_write_memory_or_start_agents(tool):
    allowed = _allowed()
    # The gate matches the whole-server selector only, so a per-tool grant such
    # as @cao-mcp-server/memory_store would still be denied below.
    assert not any(_covers_denied_cao_tool(entry) for entry in allowed)
    mcp = _server([tool])
    with patch.dict(os.environ, BOUND):
        with patch.object(server, "_get_terminal_context_from_env", return_value=_ctx(allowed)):
            with pytest.raises(Exception) as excinfo:
                _call(mcp, tool)
    assert "@cao-mcp-server" in str(excinfo.value)


def test_an_assigned_mail_agent_can_still_report():
    mcp = _server(["send_message"])
    with patch.dict(os.environ, BOUND):
        with patch.object(server, "_get_terminal_context_from_env", return_value=_ctx(_allowed())):
            assert _call(mcp, "send_message") == {"reached": "send_message"}


def test_a_supervisor_can_still_hand_off_to_the_mail_agent():
    """The gate checks the caller. A supervisor ceiling covers this profile."""
    mcp = _server(["handoff"])
    caller = ["@cao-mcp-server", "fs_read", "fs_list"]
    with patch.dict(os.environ, BOUND):
        with patch.object(server, "_get_terminal_context_from_env", return_value=_ctx(caller)):
            assert _call(mcp, "handoff") == {"reached": "handoff"}
    # ``*`` is the supervisor ceiling. The caller list above has no @cooper-email.
    assert tools_exceeding(_allowed(), ["*"]) == []
    assert tools_exceeding(_allowed(), caller) == ["@cooper-email"]
