"""FastMCP middleware for exposing only the MCP Apps tool surface."""

from typing import Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from mcp import types as mt

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


class AppSurfaceOnlyMiddleware(Middleware):
    """Hide and reject tools that are not part of the MCP Apps surface."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [tool for tool in tools if tool.name in APP_SURFACE_TOOL_NAMES]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        if tool_name not in APP_SURFACE_TOOL_NAMES:
            raise ToolError(
                f"Tool '{tool_name}' is unavailable because the server is in "
                "app-surface-only mode (CAO_MCP_APPS_ONLY=true)."
            )
        return await call_next(context)
