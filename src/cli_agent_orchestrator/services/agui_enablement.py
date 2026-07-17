"""Shared enablement gate for the AG-UI / fleet-event surfaces.

Both the HTTP surface (``/agui/v1/stream``, ``/events``) and the
``EventLogPublisher`` observer that *feeds* it must agree on when the surface is
live — otherwise the routes can be enabled while the publisher stays silent, and
the stream is starved of the lifecycle events it is supposed to relay.

The surface is enabled by **either**:

* ``CAO_AGUI_ENABLED`` — the dedicated AG-UI flag, so AG-UI can be turned on
  independently of the MCP Apps iframe surface; or
* ``apps.enabled`` (``CAO_MCP_APPS_ENABLED`` env var / ``settings.json``) — the
  pre-existing MCP Apps flag, because both surfaces are read-outs of the same
  in-process event source with the same metadata-only privacy boundary.

With neither set the surface is absent and the observer no-ops (zero per-event
cost, no fleet metadata retained).
"""

from __future__ import annotations

import os

from cli_agent_orchestrator.services.config_service import ConfigService

_TRUTHY = ("1", "true", "yes")


def agui_surface_enabled() -> bool:
    """Return whether the AG-UI / fleet-event surface should be live."""

    if os.environ.get("CAO_AGUI_ENABLED", "").strip().lower() in _TRUTHY:
        return True
    return bool(ConfigService.get("apps.enabled", default=False))
