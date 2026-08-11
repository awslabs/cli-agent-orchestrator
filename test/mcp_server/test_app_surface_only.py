"""Tests for the app-surface-only FastMCP middleware."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from cli_agent_orchestrator.ext_apps.apps import _RESOURCE_FILES
from cli_agent_orchestrator.mcp_server.app_surface_only import (
    APP_SURFACE_TOOL_NAMES,
    AppSurfaceOnlyMiddleware,
)
from cli_agent_orchestrator.mcp_server.app_tools import register_app_tools

_REAL_SERVER_PROBE = textwrap.dedent("""
    import asyncio
    import json
    from unittest.mock import patch

    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from cli_agent_orchestrator.mcp_server import app_tools
    from cli_agent_orchestrator.mcp_server.server import mcp

    TOOL_ARGS = {
        "render_dashboard": {},
        "render_agent_view": {"terminal_id": "test-terminal"},
        "cao_fetch_history": {},
        "subscribe_events": {},
        "render_graph_view": {"provider": "test-provider"},
        "submit_command": {"kind": "pause"},
    }

    async def probe():
        calls = {}
        with (
            patch.object(app_tools, "_render_dashboard_impl", return_value={"ok": True}),
            patch.object(app_tools, "_render_agent_view_impl", return_value={"ok": True}),
            patch.object(app_tools, "_cao_fetch_history_impl", return_value={"ok": True}),
            patch.object(app_tools, "_subscribe_events_impl", return_value={"ok": True}),
            patch.object(app_tools, "_render_graph_view_impl", return_value={"ok": True}),
            patch.object(app_tools, "_submit_command_impl", return_value={"ok": True}),
        ):
            async with Client(mcp) as client:
                listed = {tool.name for tool in await client.list_tools()}
                for name, arguments in TOOL_ARGS.items():
                    try:
                        await client.call_tool(name, arguments)
                    except ToolError as exc:
                        calls[name] = {"status": "error", "message": str(exc)}
                    else:
                        calls[name] = {"status": "ok"}
        return {"listed": sorted(listed), "calls": calls}

    print("PROBE_RESULT=" + json.dumps(asyncio.run(probe()), sort_keys=True))
    """)


def _probe_real_server(*, apps_only: str | None) -> dict[str, Any]:
    env = os.environ.copy()
    env["CAO_MCP_APPS_ENABLED"] = "true"
    env.pop("AUTH0_DOMAIN", None)
    env.pop("CAO_AUTH_JWKS_URI", None)
    if apps_only is None:
        env.pop("CAO_MCP_APPS_ONLY", None)
    else:
        env["CAO_MCP_APPS_ONLY"] = apps_only

    completed = subprocess.run(
        [sys.executable, "-c", _REAL_SERVER_PROBE],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("PROBE_RESULT=")
    )
    return json.loads(result_line.removeprefix("PROBE_RESULT="))


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


def test_real_server_tool_names_are_unchanged_when_mode_is_off() -> None:
    baseline_names = set(_probe_real_server(apps_only=None)["listed"])
    explicit_off_names = set(_probe_real_server(apps_only="false")["listed"])

    assert baseline_names
    assert explicit_off_names == baseline_names


def test_real_server_tool_names_do_not_use_app_namespace_separator() -> None:
    """Pin the precondition for suffix matching in AppSurfaceOnlyMiddleware.

    An in-session tool named ``foo___submit_command`` would pass the suffix
    allowlist. If the real assembled surface ever registers a name containing
    ``___``, switch the middleware to exact-name matching; do not relax this
    assertion.
    """

    tool_names = set(_probe_real_server(apps_only="false")["listed"])

    assert tool_names
    assert not any("___" in name for name in tool_names)


def test_real_server_fastmcp_32_app_tool_reachability() -> None:
    """Characterize FastMCP 3.2's known app-only tool visibility limitation.

    This drives the real assembled server through ``fastmcp.Client``. The
    shipped iframe currently sends bare names from ``mcpApp.ts:callServerTool``.
    Its ``fetchHistory()`` awaits that call before evaluating
    ``result?.events ?? []``, so an unknown ``cao_fetch_history`` rejects the
    promise rather than degrading to an empty timeline.

    When native app-tool registration lands, calls will likely use
    ``{app}___{tool}`` names. This test is expected to fail then: update its
    expectations together with ``mcpApp.ts:callServerTool`` and rebuild the
    bundles so the server and shipped frontend change in lockstep.
    """

    result = _probe_real_server(apps_only="true")
    listed = set(result["listed"])
    reachable = {"render_dashboard", "render_agent_view", "render_graph_view"}
    unreachable = APP_SURFACE_TOOL_NAMES - reachable

    for name in reachable:
        assert name in listed
        assert result["calls"][name] == {"status": "ok"}
    for name in unreachable:
        assert name not in listed
        assert result["calls"][name]["status"] == "error"
        assert result["calls"][name]["message"] == f"Unknown tool: '{name}'"


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
async def test_namespaced_app_tool_suffix_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    mcp = FastMCP("namespaced-app-tool-test")

    @mcp.tool(name="gateway___cao___render_dashboard")
    async def namespaced_render_dashboard() -> str:
        return "ok"

    mcp.add_middleware(AppSurfaceOnlyMiddleware())

    async with Client(mcp) as client:
        listed_names = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool("gateway___cao___render_dashboard")

    assert "gateway___cao___render_dashboard" in listed_names
    assert result.data == "ok"


@pytest.mark.asyncio
async def test_namespaced_non_app_tool_suffix_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    mcp = FastMCP("namespaced-hidden-tool-test")

    @mcp.tool(name="gateway___render_dashboard___handoff")
    async def namespaced_handoff() -> str:
        return "not allowed"

    mcp.add_middleware(AppSurfaceOnlyMiddleware())

    async with Client(mcp) as client:
        listed_names = {tool.name for tool in await client.list_tools()}
        assert "gateway___render_dashboard___handoff" not in listed_names
        with pytest.raises(ToolError, match="app-surface-only mode"):
            await client.call_tool("gateway___render_dashboard___handoff")


@pytest.mark.asyncio
async def test_app_surface_tool_names_match_registration(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    mcp = FastMCP("app-surface-registration-test")

    assert register_app_tools(mcp) is True

    registered_names = {tool.name for tool in await mcp.local_provider.list_tools()}
    # register_widget() adds a resource, not a tool, so this is the complete
    # drift guard for the app-surface tool allowlist.
    assert registered_names == APP_SURFACE_TOOL_NAMES
