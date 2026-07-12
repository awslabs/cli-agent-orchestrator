"""SEP-2133 capability negotiation for the CAO MCP App surface.

SEP-2133 (Extension Framework) lets a server advertise the MCP App capabilities
it supports so a host can decide whether to render the ``ui://cao/*`` resources.
CAO's negotiation is intentionally **default-off**: nothing here changes the
localhost-only posture unless ``CAO_MCP_APPS_ENABLED`` is set.

Two complementary surfaces are provided:

  ``negotiate_capabilities(client_capabilities)``
    Pull model — returns the capability set CAO offers given the client's
    capabilities. Returns ``{}`` (a no-op) when disabled.

  ``advertise_capability(mcp)``
    Push model — wraps the FastMCP server's ``create_initialization_options`` so
    the ``initialize`` response always advertises ``io.modelcontextprotocol/ui``
    in ``experimental_capabilities``. Call once at server startup. No-op when
    disabled. SEP-1865 hosts use this during the handshake to discover the
    surface before invoking UI-capable tools.

  ``client_supports_mcp_apps(mcp)``
    Returns True if the *current* MCP request context shows the connected client
    advertised ``io.modelcontextprotocol/ui`` support. Call inside a tool handler
    to decide whether to return a UI resource or a text-only fallback.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# The SEP-2133 extension identifier for MCP Apps.
EXTENSION_ID = "io.modelcontextprotocol/ui"

# The MCP App capabilities CAO advertises when enabled. ``resources`` signals the
# ``ui://cao/*`` views; ``tools`` signals the App tool channel; ``ui`` carries the
# rendering hints the host honours (CSP-sandboxed iframe, no host-side eval).
_CAPABILITIES: Dict[str, Any] = {
    "resources": True,
    "tools": True,
    "ui": {
        "iframe": True,
        "allowUnsafeEval": False,
    },
}

# Capability block injected into the server's ``initialize`` response by
# :func:`advertise_capability`. The host reads the declared MIME so it knows the
# ``ui://cao/*`` resource bodies are MCP App HTML, not arbitrary documents.
SERVER_EXTENSION_CAPABILITY: Dict[str, Any] = {
    EXTENSION_ID: {
        "mimeTypes": ["text/html;profile=mcp-app"],
    }
}


def _is_enabled() -> bool:
    """Return whether the MCP App surface is enabled via ``apps.enabled``
    (``CAO_MCP_APPS_ENABLED`` env var or ``settings.json``)."""

    return bool(ConfigService.get("apps.enabled", default=False))


def negotiate_capabilities(client_capabilities: Any = None) -> Dict[str, Any]:
    """Return the MCP App capabilities CAO offers given the client's capabilities.

    No-op (returns ``{}``) unless ``CAO_MCP_APPS_ENABLED`` is set. When enabled,
    returns CAO's advertised capability set; the ``client_capabilities`` argument
    is accepted for forward compatibility (future intersection logic) but does not
    yet narrow the result.
    """

    if not _is_enabled():
        return {}
    if client_capabilities is not None:
        logger.debug("SEP-2133 negotiation with client capabilities: %s", client_capabilities)
    return dict(_CAPABILITIES)


def advertise_capability(mcp: Any) -> None:
    """Inject the SEP-2133 extension into the server's initialize response.

    Wraps ``mcp._mcp_server.create_initialization_options`` to append
    ``io.modelcontextprotocol/ui`` to ``experimental_capabilities``. No-op when
    ``CAO_MCP_APPS_ENABLED`` is unset, and best-effort otherwise (a FastMCP build
    that exposes no ``_mcp_server`` is logged and skipped rather than crashing
    startup).

    Spec/SDK note: SEP-1865 (Final) advertises the capability under
    ``capabilities.extensions``. The installed MCP SDK's ``ServerCapabilities``
    has no ``extensions`` field (only ``experimental``), so we use the
    ``experimental`` extension point — the sanctioned place for vendor
    capabilities on this SDK. ``client_supports_mcp_apps`` accepts either
    location, so a host on a newer SDK that advertises under ``extensions`` is
    still recognized. Switch the advertise side to ``extensions`` once the SDK
    exposes it.
    """
    if not _is_enabled():
        return

    low_level = getattr(mcp, "_mcp_server", None)
    if low_level is None:
        logger.warning("SEP-2133: cannot find _mcp_server on %r; skipping advertisement", mcp)
        return

    _original = low_level.create_initialization_options

    def _patched(
        notification_options: Any = None, experimental_capabilities: Any = None, **kw: Any
    ) -> Any:
        caps: Dict[str, Any] = dict(experimental_capabilities or {})
        caps.update(SERVER_EXTENSION_CAPABILITY)
        return _original(
            notification_options=notification_options,
            experimental_capabilities=caps,
            **kw,
        )

    low_level.create_initialization_options = _patched
    logger.info("SEP-2133: advertised %s in server initialize response", EXTENSION_ID)


def client_supports_mcp_apps(mcp: Any) -> bool:
    """Return True if the connected client advertised MCP Apps support.

    Must be called from within an active MCP tool handler (uses the FastMCP
    request context). Returns False when the check is impossible (no context, no
    client params, or capability absent). When ``CAO_MCP_APPS_ENABLED`` is unset,
    always returns False so tools unconditionally serve text-only fallbacks.
    """
    if not _is_enabled():
        return False

    try:
        ctx = mcp.get_context()
        session = getattr(ctx, "session", None)
        if session is None:
            return False
        client_params = getattr(session, "client_params", None)
        if client_params is None:
            return False
        capabilities = getattr(client_params, "capabilities", None)
        if capabilities is None:
            return False
        # SEP-1865 advertises under capabilities.extensions (per SEP-1724); the
        # installed SDK only exposes `experimental`. Accept either so both
        # current- and future-SDK hosts are recognized.
        experimental = getattr(capabilities, "experimental", None) or {}
        extensions = getattr(capabilities, "extensions", None) or {}
        return EXTENSION_ID in experimental or EXTENSION_ID in extensions
    except Exception:
        logger.debug("client_supports_mcp_apps check failed; assuming unsupported", exc_info=True)
        return False
