"""Tests for the app-surface-only FastMCP middleware."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from cli_agent_orchestrator.ext_apps.apps import _RESOURCE_FILES
from cli_agent_orchestrator.mcp_server.app_surface_only import (
    APP_SURFACE_TOOL_NAMES,
    AppSurfaceOnlyMiddleware,
)
from cli_agent_orchestrator.mcp_server.app_tools import register_app_tools


def _server_with_tools(*, app_surface_only: bool, app_tools_are_stubs: bool = True) -> FastMCP:
    mcp = FastMCP("app-surface-only-test")

    names = {"handoff", "assign", "send_message", "unrelated_tool"}
    if app_tools_are_stubs:
        names |= APP_SURFACE_TOOL_NAMES

    for name in names:

        async def tool_stub() -> str:
            return "tool result"

        mcp.tool(name=name)(tool_stub)

    if not app_tools_are_stubs:
        register_app_tools(mcp)
    if app_surface_only:
        mcp.add_middleware(AppSurfaceOnlyMiddleware())
    return mcp


@pytest.mark.asyncio
async def test_tools_list_contains_only_app_surface_tools_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    mcp = _server_with_tools(app_surface_only=True)

    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    assert names == APP_SURFACE_TOOL_NAMES
    assert {"handoff", "assign", "send_message"}.isdisjoint(names)


@pytest.mark.asyncio
async def test_tools_list_is_unchanged_when_mode_is_off(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    baseline = _server_with_tools(app_surface_only=False)
    mode_off = _server_with_tools(app_surface_only=False)

    async with Client(baseline) as client:
        baseline_names = {tool.name for tool in await client.list_tools()}
    async with Client(mode_off) as client:
        mode_off_names = {tool.name for tool in await client.list_tools()}

    assert mode_off_names == baseline_names
    assert mode_off_names == APP_SURFACE_TOOL_NAMES | {
        "handoff",
        "assign",
        "send_message",
        "unrelated_tool",
    }


@pytest.mark.asyncio
async def test_resources_and_graph_tool_register_in_both_modes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))
    for filename in _RESOURCE_FILES.values():
        (tmp_path / filename).write_text("<html></html>", encoding="utf-8")

    for app_surface_only in (False, True):
        mcp = _server_with_tools(
            app_surface_only=app_surface_only,
            app_tools_are_stubs=False,
        )
        async with Client(mcp) as client:
            tool_names = {tool.name for tool in await client.list_tools()}
            resource_uris = {str(resource.uri) for resource in await client.list_resources()}

        assert "render_graph_view" in tool_names
        assert {
            "ui://cao/dashboard",
            "ui://cao/agent",
            "ui://cao/event-stream",
            "ui://cao/graph",
        } <= resource_uris


@pytest.mark.asyncio
async def test_calling_hidden_tool_returns_mode_specific_error(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    mcp = _server_with_tools(app_surface_only=True)

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="app-surface-only mode"):
            await client.call_tool("handoff")


@pytest.mark.asyncio
async def test_app_surface_tool_names_match_registration(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    mcp = FastMCP("app-surface-registration-test")

    assert register_app_tools(mcp) is True

    registered_names = {tool.name for tool in await mcp.local_provider.list_tools()}
    assert registered_names == APP_SURFACE_TOOL_NAMES
