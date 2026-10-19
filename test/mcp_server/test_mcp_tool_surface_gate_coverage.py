"""Every tool on every MCP surface is gated, or the gap is declared here (#671).

The CLASS of defect #671 belongs to: **an authorization policy enforced per
registration, so a newly registered tool is ungated by default and the policy
silently stops covering the surface as it grows.** #769 and #811 added
``_tool_denied_reason`` at 7 call sites; the surface was 30 tools; nothing
noticed the other 23, because nothing in the suite ever asked the *registry*
what it contained.

The repo already solved this class once, on its other authorization surface:
``test/api/test_scope_coverage.py::test_every_mutating_route_is_scope_gated``
enumerates the live FastAPI route table and fails if any mutating route lacks a
``require_any_scope`` dependency, with an ``_EXEMPT`` set that makes each
exception a reviewable line of code. That guard was never applied to the MCP
tool surface. This file is that guard, for MCP.

Two levels, because the class has two shapes:

1. **Within a gated surface** — enumerate the real ``server.mcp`` registry (not a
   synthetic one: a synthetic server can only prove the gate works on tools the
   test itself registered) and assert the installed gate denies a restricted
   bound caller for every tool, except the explicitly reviewed callback set.
2. **Across surfaces** — a second ``FastMCP`` instance in the package is the same
   defect one level up. Every ``FastMCP`` in ``src/`` must either carry the gate
   or be declared below with its tool names pinned, so adding a tool to a
   knowingly-ungated surface fails this test instead of shipping quietly.

The gate is driven directly rather than through ``fastmcp.Client`` so that no
real tool implementation runs: the decision, not the body, is what is under test.
"""

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.ops_mcp_server import server as ops_server

_SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"

BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}

#: Tools on the CAO surface that a restricted bound caller may still reach.
#: Pinned literally: widening it is a diff reviewers can see, which is the whole
#: point. See ``server.CALLBACK_EXEMPT_TOOLS`` for why these four must stay open.
REVIEWED_UNGATED = frozenset(
    {
        "send_message",
        "complete_assignment",
        "workflow_return",
        "answer_user_prompt",
    }
)

#: MCP surfaces in this package that carry NO allowlist gate, with the tools they
#: expose. ``cao-ops-mcp-server`` is the operator control plane (``cao-ops`` in the
#: operator agent-plugin), normally run by a human's MCP host with no
#: ``CAO_TERMINAL_ID``. But ``providers/cursor_cli.py:508-524`` forwards
#: ``CAO_TERMINAL_ID`` into every server in a profile's ``mcpServers`` map, so a
#: profile that lists this server hands a bound terminal ``launch_session`` /
#: ``install_profile`` / ``shutdown_session`` with its ``tools:`` allowlist
#: unconsulted — #671's shape, on a surface that can mint a session. Recorded, not
#: fixed here: it needs its own issue and its own grant selector.
UNGATED_SURFACES = {
    "cao-ops-mcp-server": frozenset(
        {
            "decisions_list",
            "decisions_purge",
            "decisions_set_exclusions",
            "decisions_set_point",
            "decisions_set_tier",
            "decisions_status",
            "get_profile_details",
            "get_session_info",
            "get_terminal_output",
            "get_terminal_status",
            "install_profile",
            "launch_session",
            "list_profiles",
            "list_sessions",
            "read_session_output",
            "send_session_message",
            "shutdown_session",
        }
    ),
}


def _tool_names(mcp):
    return {tool.name for tool in asyncio.run(mcp._list_tools())}


def _gate(mcp):
    """The installed allowlist gate on ``mcp``, or None."""
    for middleware in getattr(mcp, "middleware", []):
        if type(middleware).__name__ == "_AllowlistGate":
            return middleware
    return None


def _decide(mcp, tool_name, allowed_tools):
    """Ask the installed gate about ``tool_name``; no implementation runs.

    Returns the denial message, or None if the gate passed the call through.
    """
    gate = _gate(mcp)
    assert gate is not None, "no allowlist gate installed on this surface"
    context = SimpleNamespace(message=SimpleNamespace(name=tool_name, arguments={}))
    sentinel = object()

    async def call_next(_context):
        return sentinel

    ctx = {
        "terminal_id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "codex",
        "agent_profile": "worker",
        "allowed_tools": allowed_tools,
    }
    with patch.dict(os.environ, BOUND):
        with patch.object(server, "_get_terminal_context_from_env", return_value=ctx):
            try:
                assert asyncio.run(gate.on_call_tool(context, call_next)) is sentinel
                return None
            except ToolError as exc:
                return str(exc)


@pytest.mark.parametrize("tool", sorted(_tool_names(server.mcp)))
def test_every_registered_cao_tool_is_gated(tool):
    """The registry, not a hand-written list, decides what must be covered."""
    denial = _decide(server.mcp, tool, ["fs_read"])
    if tool in REVIEWED_UNGATED:
        assert denial is None, f"{tool} is in REVIEWED_UNGATED but the gate denies it"
        return
    assert denial is not None, (
        f"{tool} is registered on cao-mcp-server and reachable by a bound terminal "
        f"whose allowlist omits '@cao-mcp-server'. Gate it, or add it to "
        f"REVIEWED_UNGATED here and to server.CALLBACK_EXEMPT_TOOLS with a reason."
    )
    assert "@cao-mcp-server" in denial


def test_the_reviewed_ungated_set_matches_the_implementation():
    """The guard's allowlist and the server's exemption cannot drift apart."""
    assert REVIEWED_UNGATED == set(server.CALLBACK_EXEMPT_TOOLS)


def test_the_real_gate_denies_by_default_not_by_enumeration():
    """A name the gate has never heard of is denied, on the real server object.

    The decision is taken before dispatch and keyed on the name alone, so a tool
    a later PR adds — or one a plugin's ``on_mcp_server`` hook adds — is covered
    without anyone remembering a guard. That inverted default is the fix for the
    class; this asserts it against ``server.mcp`` itself, not a stand-in.
    """
    denial = _decide(server.mcp, "tool_from_a_later_pr", ["fs_read"])
    assert denial is not None and "@cao-mcp-server" in denial


def test_the_gate_is_installed_after_plugin_registration():
    """Install order, so a plugin-registered tool lands inside the gate."""
    source = (_SRC / "mcp_server" / "server.py").read_text(encoding="utf-8")
    register = source.index("\nregister_mcp_server_surfaces(mcp)")
    install = source.index("\n_install_allowlist_middleware(mcp)")
    assert register < install, (
        "_install_allowlist_middleware(mcp) must run after "
        "register_mcp_server_surfaces(mcp), or plugin-registered tools sit "
        "outside the gate."
    )


def test_every_fastmcp_surface_in_the_package_is_gated_or_declared():
    """A new ungated MCP surface is this defect one level up."""
    found = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if re.search(r"^\s*\w+\s*=\s*FastMCP\(", path.read_text(encoding="utf-8"), re.MULTILINE)
    }
    assert found == {
        "mcp_server/server.py",
        "ops_mcp_server/server.py",
    }, (
        f"MCP surfaces changed: {sorted(found)}. A new FastMCP server is a new "
        f"authorization surface — install the allowlist gate on it, or declare it "
        f"in UNGATED_SURFACES with the reason it needs none."
    )
    assert _gate(server.mcp) is not None


def test_the_ungated_ops_surface_has_not_grown():
    """Adding a tool to a knowingly-ungated surface must fail here, not ship."""
    assert _gate(ops_server.mcp) is None, (
        "cao-ops-mcp-server is now gated — delete its UNGATED_SURFACES entry and "
        "move it into the per-tool coverage test above."
    )
    assert _tool_names(ops_server.mcp) == UNGATED_SURFACES["cao-ops-mcp-server"], (
        "cao-ops-mcp-server's tool surface changed while it is still ungated. A "
        "bound terminal whose profile lists this server reaches these tools with "
        "its 'tools:' allowlist unconsulted (see UNGATED_SURFACES)."
    )
