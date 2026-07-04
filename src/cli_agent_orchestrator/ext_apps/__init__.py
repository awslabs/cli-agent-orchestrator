"""MCP App (SEP-1865 + SEP-2133) resource package for CAO.

Exposes two host-rendered surfaces, both **default-off** and **degrading
gracefully**:

* The ``ui://cao/*`` single-file views (dashboard, agent detail, event stream),
  the ``_meta.ui`` annotation builder (``ui_meta``), the resource body resolver
  (``get_resource_body``), and the registration entry point (``register_apps``).
* The vanilla topology widget (``cao://widget/topology``) with a non-MCP static
  fallback mounted at ``/widgets/topology/`` (``register_widget`` /
  ``mount_widget_static``) so SSE-capable hosts keep working on older fastmcp.

SEP-2133 capability negotiation is provided both pull-style
(``negotiate_capabilities``) and push-style (``advertise_capability`` /
``client_supports_mcp_apps``). Everything is a no-op unless
``CAO_MCP_APPS_ENABLED`` is set, and registration returns ``False`` (logging an
informational message) rather than raising on a FastMCP build that predates the
``@mcp.resource`` decorator.
"""

from cli_agent_orchestrator.ext_apps.apps import (
    AGENT_RESOURCE_URI,
    DASHBOARD_RESOURCE_URI,
    EVENT_STREAM_RESOURCE_URI,
    PREFERRED_FRAMES,
    get_resource_body,
    register_apps,
    ui_meta,
)
from cli_agent_orchestrator.ext_apps.sep2133 import (
    EXTENSION_ID,
    SERVER_EXTENSION_CAPABILITY,
    advertise_capability,
    client_supports_mcp_apps,
    negotiate_capabilities,
)
from cli_agent_orchestrator.ext_apps.widget import (
    WIDGET_HTML_PATH,
    mount_widget_static,
    register_widget,
)

__all__ = [
    # ui://cao/* views
    "DASHBOARD_RESOURCE_URI",
    "AGENT_RESOURCE_URI",
    "EVENT_STREAM_RESOURCE_URI",
    "PREFERRED_FRAMES",
    "get_resource_body",
    "ui_meta",
    "register_apps",
    # SEP-2133 capability negotiation
    "negotiate_capabilities",
    "advertise_capability",
    "client_supports_mcp_apps",
    "EXTENSION_ID",
    "SERVER_EXTENSION_CAPABILITY",
    # commit 7 topology widget
    "register_widget",
    "mount_widget_static",
    "WIDGET_HTML_PATH",
]
