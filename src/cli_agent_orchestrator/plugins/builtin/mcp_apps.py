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

Authoritative spec (source of truth for the surface this plugin registers):
MCP Apps (SEP-1865, Status: Stable 2026-01-26).
- Overview: https://modelcontextprotocol.io/extensions/apps/overview
- Build guide: https://modelcontextprotocol.io/extensions/apps/build
- Stable spec: https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- Capability negotiation: https://modelcontextprotocol.io/extensions/overview#negotiation
- SDK (reference impl, v1.7.4): https://www.npmjs.com/package/@modelcontextprotocol/ext-apps
- Provenance / discussion: https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1865
"""

import logging
from typing import Any

from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)


def _surface_enabled() -> bool:
    """Return whether the MCP App surface is enabled via ``apps.enabled``
    (``CAO_MCP_APPS_ENABLED`` env var or ``settings.json``)."""

    return bool(ConfigService.get("apps.enabled", default=False))


class McpAppsPlugin(CaoPlugin):
    """Registers the CAO MCP Apps surface on the FastMCP server at startup."""

    def on_mcp_server(self, mcp: Any) -> None:
        # Imported lazily so plugin discovery never pulls the MCP App stack at
        # import time (and to avoid an import cycle through mcp_server).
        from cli_agent_orchestrator.ext_apps import advertise_capability, register_widget
        from cli_agent_orchestrator.mcp_server.app_tools import register_app_tools
        from cli_agent_orchestrator.security.auth import is_auth_enabled

        # Startup posture warning: the surface is enabled but no IdP is
        # configured, so the auth layer returns the full scope set and enforces
        # nothing. The ``submit_command`` choke point (assign / interrupt /
        # shutdown_session / ...) then rides CAO's existing localhost-trust model
        # unauthenticated — fine on a private loopback box, but surface it
        # explicitly so an operator who flips the flag on a shared or
        # port-forwarded host isn't surprised. Set ``AUTH0_DOMAIN`` or
        # ``CAO_AUTH_JWKS_URI`` to enforce ``cao:read`` / ``cao:write`` /
        # ``cao:admin`` scopes on mutations.
        if _surface_enabled() and not is_auth_enabled():
            logger.warning(
                "CAO_MCP_APPS_ENABLED is set but no IdP is configured "
                "(AUTH0_DOMAIN / CAO_AUTH_JWKS_URI unset): the MCP Apps surface is "
                "mounted with authorization off, so submit_command mutations "
                "(assign, interrupt, shutdown_session, ...) inherit CAO's "
                "unauthenticated localhost-trust model. Set AUTH0_DOMAIN or "
                "CAO_AUTH_JWKS_URI to enforce cao:read/cao:write/cao:admin scopes "
                "before exposing the surface beyond a trusted loopback host."
            )

        # register_app_tools also registers the ui://cao/* resources. Each call
        # is best-effort and default-off; none raise on an older FastMCP build or
        # missing artifacts.
        register_app_tools(mcp)
        register_widget(mcp)
        advertise_capability(mcp)
