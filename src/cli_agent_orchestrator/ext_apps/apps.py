"""``ui://cao/*`` MCP App resources, ``_meta.ui`` annotations, and registration.

The three views are shipped as **single-file HTML** artifacts built by the
``cao_mcp_apps`` frontend (``vite-plugin-singlefile``) into ``apps_static/``.
``register_apps`` mounts each artifact as an MCP resource under its ``ui://cao/*``
URI so an MCP App host can load it into a sandboxed iframe.

The surface is default-off, gated on ``apps.enabled`` — resolved via
``ConfigService`` (``CAO_MCP_APPS_ENABLED`` env var, or the ``apps.enabled``
key in ``settings.json``; see docs/configuration.md).

Resolution of ``apps_static/`` tries, in order:

1. the ``apps.static_dir`` override (``CAO_MCP_APPS_STATIC_DIR`` env var, or
   ``settings.json``), read via ``ConfigService``,
2. the packaged location ``<package>/apps_static`` (wheel installs), then
3. the source-tree location ``<repo-root>/apps_static`` (editable/dev installs).

This module imports nothing from ``clients.*`` — it stays on the HTTP-only side of
the boundary and only reads static files from disk.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# Stable ``ui://cao/*`` resource URIs the iframe entry points are served under.
DASHBOARD_RESOURCE_URI = "ui://cao/dashboard"
AGENT_RESOURCE_URI = "ui://cao/agent"
EVENT_STREAM_RESOURCE_URI = "ui://cao/event-stream"

# Default Content-Security-Policy domains for the sandboxed iframe, expressed in the
# **structured** SEP-1865 ``_meta.ui.csp`` shape (NOT a raw CSP string). The host
# composes the actual CSP header from
# these declared domains. ``connectDomains`` allows the iframe to stream the loopback
DEFAULT_CSP = {
    "connectDomains": ["http://127.0.0.1:9889", "http://localhost:9889"],
    "resourceDomains": [],
    "frameDomains": [],
    "baseUriDomains": [],
}

# SEP-1865 mandates this MIME type for HTML MCP App resources.
RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"

# Maps each resource URI to the single-file artifact built under apps_static/.
_RESOURCE_FILES = {
    DASHBOARD_RESOURCE_URI: "dashboard.html",
    AGENT_RESOURCE_URI: "agent.html",
    EVENT_STREAM_RESOURCE_URI: "event-stream.html",
}

# Preferred iframe sizes per resource. NOTE: `preferredFrameSize` is a
# **CAO-specific** hint, NOT part of the SEP-1865 `_meta.ui` schema — the spec
# sizes views via the host's `HostContext.containerDimensions` and the View's
# `ui/notifications/size-changed` notification. CAO attaches it as an additive
# hint hosts MAY ignore; ``ui_meta`` writes the matching entry as
# ``_meta.ui.preferredFrameSize``. The dashboard is widest (fleet table), the
# agent view is medium (single-agent detail), and the event ticker is compact.
_DEFAULT_FRAME: Dict[str, int] = {"width": 1280, "height": 800}
PREFERRED_FRAMES: Dict[str, Dict[str, int]] = {
    DASHBOARD_RESOURCE_URI: {"width": 1280, "height": 800},
    AGENT_RESOURCE_URI: {"width": 1024, "height": 720},
    EVENT_STREAM_RESOURCE_URI: {"width": 640, "height": 480},
}


def _is_enabled() -> bool:
    """Return whether the MCP App surface is enabled via ``apps.enabled``
    (``CAO_MCP_APPS_ENABLED`` env var or ``settings.json``)."""

    return bool(ConfigService.get("apps.enabled", default=False))


def apps_static_dir() -> Optional[Path]:
    """Return the first existing ``apps_static`` directory, or ``None``.

    Tries the ``apps.static_dir`` override (``CAO_MCP_APPS_STATIC_DIR`` env var
    or ``settings.json``), the packaged location, then the source-tree location.
    """

    override = ConfigService.get("apps.static_dir", default=None)
    candidates: List[Path] = []
    if override:
        candidates.append(Path(override))
    # <package>/ext_apps/apps_static — the package-shipped location the frontend
    # build (`npm run build:all`) emits to and the Phase 0 gates scan.
    package_root = Path(__file__).resolve().parents[1]
    candidates.append(package_root / "ext_apps" / "apps_static")
    # <package>/apps_static  (alternate packaged location)
    candidates.append(package_root / "apps_static")
    # <repo-root>/apps_static  (editable/dev fallback)
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root / "apps_static")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def ui_meta(
    csp: Optional[dict] = None,
    required_scopes: Optional[List[str]] = None,
    visibility: Optional[List[str]] = None,
    resource_uri: Optional[str] = None,
    permissions: Optional[dict] = None,
) -> dict:
    """Build the ``_meta.ui`` annotation attached to an MCP App tool/resource.

    Returns the SEP-1865 ``_meta.ui`` object. ``visibility`` and ``resourceUri``
    are the spec-defined tool keys; ``csp``, ``permissions``, ``domain`` and
    ``prefersBorder`` are the spec-defined resource keys; ``requiredScopes`` and
    ``preferredFrameSize`` are CAO extensions (the choke point reads
    ``requiredScopes`` for its scope pre-check; ``preferredFrameSize`` is an
    additive sizing hint — the spec itself sizes via ``containerDimensions`` /
    ``ui/notifications/size-changed``).

    Args:
        csp: Structured CSP domains (``connectDomains`` / ``resourceDomains`` /
            ``frameDomains`` / ``baseUriDomains``). Defaults to :data:`DEFAULT_CSP`.
        required_scopes: CAO scopes the host should require before invoking the
            tool. Empty/None means no scope gate (a read tool).
        visibility: ``["model", "app"]`` or ``["app"]`` per SEP-1865. Omitted for
            resources (which are not tools).
        resource_uri: The ``ui://cao/*`` resource the tool result renders in.
        permissions: Spec ``_meta.ui.permissions`` object keyed by capability
            (``camera`` / ``microphone`` / ``geolocation`` / ``clipboardWrite``,
            each an empty object ``{}``). Omitted by default: CAO's read-only
            fleet views request **no elevated browser permissions**. Supported
            here for full spec fidelity / downstream reuse.

    Returns:
        ``{"ui": {...}}`` suitable to pass as a tool's ``_meta``.
    """

    ui: dict = {
        "csp": csp or dict(DEFAULT_CSP),
        "requiredScopes": list(required_scopes) if required_scopes else [],
    }
    # Spec `_meta.ui.permissions` is an OBJECT keyed by capability, NOT an array.
    # Emit only when explicitly requested; an omitted field == no permissions
    # requested (the spec's secure default), which is CAO's posture.
    if permissions:
        ui["permissions"] = dict(permissions)
    if visibility is not None:
        ui["visibility"] = list(visibility)
    if resource_uri is not None:
        ui["resourceUri"] = resource_uri
        # Spec resource keys (prefersBorder, domain) plus the CAO-specific
        # preferredFrameSize sizing hint, attached only for resource-rendering
        # tools. Hosts that don't understand a key ignore it; the spec sizes via
        # containerDimensions / size-changed, with preferredFrameSize as an
        # additive CAO hint.
        ui["preferredFrameSize"] = dict(PREFERRED_FRAMES.get(resource_uri, _DEFAULT_FRAME))
        ui["prefersBorder"] = True
        ui["domain"] = resource_uri.split("//", 1)[-1].replace("/", "-")
    return {"ui": ui}


def _read_resource_html(filename: str) -> Optional[str]:
    """Read a single-file artifact from ``apps_static/`` if present."""

    static_dir = apps_static_dir()
    if static_dir is None:
        return None
    path = static_dir / filename
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def get_resource_body(uri: str) -> str:
    """Return the HTML body for a registered ``ui://cao/*`` resource.

    Resolves the artifact via :func:`apps_static_dir` (env override → packaged
    location → source tree). Raises ``KeyError`` for an unknown URI and
    ``FileNotFoundError`` when the artifact is absent (e.g. the frontend has not
    been built in a dev tree). Production wheels always ship the artifacts via the
    hatch ``artifacts`` rule in ``pyproject.toml``.
    """

    filename = _RESOURCE_FILES.get(uri)
    if filename is None:
        raise KeyError(f"Unknown MCP Apps resource: {uri}")
    static_dir = apps_static_dir()
    if static_dir is None:
        raise FileNotFoundError("apps_static/ not found (frontend not built)")
    path = static_dir / filename
    return path.read_text(encoding="utf-8")


def register_apps(mcp: Any) -> bool:
    """Register the three ``ui://cao/*`` resources on the FastMCP server.

    Best-effort and side-effect free when disabled:

    * returns ``False`` (logging at info level) when ``CAO_MCP_APPS_ENABLED`` is
      unset, when the running FastMCP has no ``resource`` decorator, or when the
      ``apps_static/`` build output is missing;
    * otherwise registers one resource per built artifact and returns ``True``.
    """

    if not _is_enabled():
        logger.info(
            "MCP Apps disabled (CAO_MCP_APPS_ENABLED unset); skipping resource registration"
        )
        return False

    resource_decorator = getattr(mcp, "resource", None)
    if not callable(resource_decorator):
        logger.info(
            "FastMCP build has no @mcp.resource decorator; skipping MCP App resource registration"
        )
        return False

    static_dir = apps_static_dir()
    if static_dir is None:
        logger.info(
            "apps_static/ not found (frontend not built); skipping MCP App resource registration"
        )
        return False

    registered = 0
    for uri, filename in _RESOURCE_FILES.items():

        def _make_handler(fname: str, resource_uri: str):
            def _handler() -> str:
                html = _read_resource_html(fname)
                if html is None:
                    logger.warning("MCP App artifact missing at request time: %s", fname)
                    return f"<!doctype html><title>{resource_uri}</title><p>view not built</p>"
                return html

            return _handler

        try:
            decorated = resource_decorator(uri, mime_type=RESOURCE_MIME_TYPE)(
                _make_handler(filename, uri)
            )
            # Reference the decorated handler so linters do not flag it unused;
            # FastMCP retains its own registration regardless.
            del decorated
            registered += 1
        except Exception:  # pragma: no cover - defensive: never crash startup
            logger.exception("Failed to register MCP App resource %s", uri)

    logger.info(
        "Registered %d/%d MCP App resources from %s", registered, len(_RESOURCE_FILES), static_dir
    )
    return registered > 0
