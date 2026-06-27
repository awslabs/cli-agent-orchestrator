"""Umbrella plugin packaging the MCP Apps surface (SEP-1865 / SEP-2133).

On MCP server startup (via the ``on_mcp_server`` hook) this plugin registers the
MCP App tools (``render_dashboard`` / ``render_agent_view`` / ``cao_fetch_history``
/ ``subscribe_events`` / ``submit_command``), the ``ui://cao/*`` resources, the
topology widget (``cao://widget/topology``), and advertises the SEP-2133 UI
capability on the ``initialize`` handshake.

Everything is **default-off** via ``CAO_MCP_APPS_ENABLED`` and best-effort, so
the default posture is byte-for-byte unchanged when the flag is unset. Durable
event observation (the ring buffer that backs ``cao_fetch_history``) is handled
by the companion ``event_log_publisher`` plugin; this plugin owns the
MCP-server-facing registration.
"""

from typing import Any

from cli_agent_orchestrator.plugins.base import CaoPlugin


class McpAppsPlugin(CaoPlugin):
    """Registers the CAO MCP Apps surface on the FastMCP server at startup."""

    def on_mcp_server(self, mcp: Any) -> None:
        # Imported lazily so plugin discovery never pulls the MCP App stack at
        # import time (and to avoid an import cycle through mcp_server).
        from cli_agent_orchestrator.ext_apps import advertise_capability, register_widget
        from cli_agent_orchestrator.mcp_server.app_tools import register_app_tools

        # register_app_tools also registers the ui://cao/* resources. Each call
        # is best-effort and default-off; none raise on an older FastMCP build or
        # missing artifacts.
        register_app_tools(mcp)
        register_widget(mcp)
        advertise_capability(mcp)
