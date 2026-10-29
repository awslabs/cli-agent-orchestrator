"""The managers of the hierarchical factory example can report, but cannot delegate.

Review of #1108 (P2-1): with the whole ``@cao-mcp-server`` grant, a team manager
could ``assign`` the manager profile of another team, whose run copy carries that
team's simulator credential. The managers now hold only
``@cao-mcp-server/send_message``. This pins, against CAO's own gate, that CAO
refuses every identity-minting and discovery tool to them, that ``send_message``
stays reachable, and that the supervisor can still launch each manager.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.delegation import resolve_delegate_tools, tools_exceeding
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/robotics/hierarchical-factory"
MANAGERS = (
    "factory_line_manager.md",
    "factory_assembly_manager.md",
    "factory_shipping_manager.md",
)


def _profile(name: str) -> AgentProfile:
    return AgentProfile(**frontmatter.load(EXAMPLE / name).metadata)


def _allowed(profile: AgentProfile) -> list[str]:
    return resolve_allowed_tools(
        profile.allowedTools, profile.role, grantable_server_names(profile)
    )


def _denied(monkeypatch, allowed: list[str], tool: str):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: {"id": "abcd1234"})
    monkeypatch.setattr(server, "_caller_effective_allowed_tools", lambda context: allowed)
    return server._tool_denied_reason(tool)


@pytest.mark.parametrize("name", MANAGERS)
def test_a_manager_holds_only_send_message_of_the_cao_server(name):
    allowed = _allowed(_profile(name))
    assert "@cao-mcp-server/send_message" in allowed
    assert server.CAO_MCP_SERVER_SELECTOR not in allowed
    assert "*" not in allowed


@pytest.mark.parametrize("name", MANAGERS)
@pytest.mark.parametrize("tool", ["assign", "handoff", "find_profiles", "delete_terminal"])
def test_cao_refuses_a_manager_every_tool_that_reaches_another_team(monkeypatch, name, tool):
    allowed = _allowed(_profile(name))
    assert _denied(monkeypatch, allowed, tool) is not None


def test_a_manager_can_still_report_to_the_supervisor():
    assert "send_message" in server.CALLBACK_EXEMPT_TOOLS


def test_the_supervisor_can_assign_and_can_launch_every_manager(monkeypatch):
    supervisor = _profile("factory_supervisor.md")
    allowed = _allowed(supervisor)
    assert _denied(monkeypatch, allowed, "assign") is None
    ceiling = resolve_delegate_tools(supervisor.delegateTools, supervisor.role, allowed)
    for name in MANAGERS:
        assert tools_exceeding(_allowed(_profile(name)), ceiling) == [], name
