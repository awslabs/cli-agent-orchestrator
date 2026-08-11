"""FastMCP middleware for exposing only the MCP Apps tool surface."""

from typing import Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from mcp import types as mt

# FastMCP 3.2's _is_model_visible() filters tools whose _meta.ui.visibility is
# only ["app"] out of both tools/list and get_tool(). Consequently
# cao_fetch_history, subscribe_events, and submit_command are currently
# unreachable through FastMCP. Keep all six names here so the restriction remains
# correct when native app-tool registration makes those tools addressable.
APP_SURFACE_TOOL_NAMES = frozenset(
    {
        "render_dashboard",
        "render_agent_view",
        "cao_fetch_history",
        "subscribe_events",
        "render_graph_view",
        "submit_command",
    }
)


def _is_app_surface_tool_name(tool_name: str) -> bool:
    """Return whether a bare or namespaced tool name belongs to the app surface."""

    return tool_name.rsplit("___", 1)[-1] in APP_SURFACE_TOOL_NAMES


class AppSurfaceOnlyMiddleware(Middleware):
    """Hide and reject tools that are not part of the MCP Apps surface."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [tool for tool in tools if _is_app_surface_tool_name(tool.name)]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        if not _is_app_surface_tool_name(tool_name):
            raise ToolError(
                f"Tool '{tool_name}' is unavailable because the server is in "
                "app-surface-only mode (CAO_MCP_APPS_ONLY=true)."
            )
        return await call_next(context)
