"""Topology widget registration.

Ships a minimal vanilla HTML/JS/CSS topology view as a second MCP App surface
alongside the ``ui://cao/*`` views:

  * a static bundle under ``static/topology.*``
  * ``mount_widget_static(app)`` — mounts the bundle at ``/widgets/topology/``
    on the main FastAPI app so any SSE-capable host can fetch it
  * ``register_widget(mcp)`` — best-effort registration on the FastMCP server as
    a ``cao://widget/topology`` resource. If the running fastmcp build does not
    expose ``@mcp.resource`` we fall back to the static mount and rely on the
    host (Claude / Claude Desktop, ChatGPT, VS Code GitHub Copilot, Goose,
    Postman, MCPJam, Archestra.AI) to render it.

Both entry points are **default-off**: they are no-ops unless
``CAO_MCP_APPS_ENABLED`` is set, matching the contract of ``register_apps`` /
``register_app_tools`` so the default localhost posture is unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).with_name("static")
WIDGET_HTML_PATH = _STATIC_DIR / "topology.html"
WIDGET_RESOURCE_URI = "cao://widget/topology"
WIDGET_MOUNT_PATH = "/widgets/topology"


def _is_enabled() -> bool:
    """Return whether the MCP App surface is enabled via ``apps.enabled``
    (``CAO_MCP_APPS_ENABLED`` env var or ``settings.json``)."""

    return bool(ConfigService.get("apps.enabled", default=False))


def mount_widget_static(app: Any) -> None:
    """Mount the static bundle at ``/widgets/topology/`` on a FastAPI app.

    No-op unless ``apps.enabled`` is set. Caller is the main FastAPI app
    in ``api/main.py``. Idempotent — re-mounting the same path is skipped (FastAPI
    raises if the same path is mounted twice).
    """
    if not _is_enabled():
        return

    from fastapi.staticfiles import StaticFiles

    # Check whether we've already mounted to avoid the double-mount error
    # when lifespan logic runs more than once in dev/reload mode.
    for route in getattr(app, "routes", []):
        if getattr(route, "path", None) == WIDGET_MOUNT_PATH:
            return
    app.mount(
        WIDGET_MOUNT_PATH,
        StaticFiles(directory=str(_STATIC_DIR), html=True),
        name="topology-widget",
    )
    logger.info("Topology widget mounted at %s", WIDGET_MOUNT_PATH)


def register_widget(mcp: Any) -> bool:
    """Register the widget as an MCP resource on the FastMCP server.

    Best-effort and side-effect free when disabled: returns ``False`` when
    ``CAO_MCP_APPS_ENABLED`` is unset, when the widget HTML is missing, or when
    the running fastmcp build does not expose ``@mcp.resource``; otherwise
    registers ``cao://widget/topology`` and returns ``True``.
    """
    if not _is_enabled():
        logger.info("MCP Apps disabled (CAO_MCP_APPS_ENABLED unset); skipping widget registration")
        return False

    if not WIDGET_HTML_PATH.exists():
        logger.warning(
            "Topology widget HTML not found at %s — skipping MCP registration",
            WIDGET_HTML_PATH,
        )
        return False

    decorator = getattr(mcp, "resource", None)
    if not callable(decorator):
        logger.info(
            "FastMCP build does not expose @mcp.resource; widget served via /widgets/topology"
        )
        return False

    try:

        @decorator(WIDGET_RESOURCE_URI)  # type: ignore[misc]
        def topology_widget() -> str:
            """SEP-1865 widget body for hosts that render MCP Apps inline."""
            return WIDGET_HTML_PATH.read_text(encoding="utf-8")

        logger.info("Topology widget registered at %s", WIDGET_RESOURCE_URI)
        return True
    except Exception:
        logger.warning("Failed to register topology widget on MCP server", exc_info=True)
        return False
