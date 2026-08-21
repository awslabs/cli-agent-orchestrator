"""Tests for the mcp_apps umbrella plugin and the MCP-server surface dispatcher."""

from __future__ import annotations

import logging
from typing import Any, List

from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.plugins.builtin.mcp_apps import McpAppsPlugin
from cli_agent_orchestrator.plugins.registry import register_mcp_server_surfaces


class _FakeLowLevel:
    def create_initialization_options(
        self,
        notification_options: Any = None,
        experimental_capabilities: Any = None,
        **kw: Any,
    ) -> dict:
        return {"experimental": dict(experimental_capabilities or {})}


class _FakeMcp:
    """Minimal FastMCP stand-in recording tool/resource registrations."""

    def __init__(self) -> None:
        self.tools: List[str] = []
        self.resources: List[str] = []
        self._mcp_server = _FakeLowLevel()

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def _deco(fn: Any) -> Any:
            self.tools.append(getattr(fn, "__name__", "tool"))
            return fn

        return _deco

    def resource(self, *args: Any, **kwargs: Any) -> Any:
        def _deco(fn: Any) -> Any:
            self.resources.append(getattr(fn, "__name__", "resource"))
            return fn

        return _deco


def test_mcp_apps_is_a_cao_plugin() -> None:
    assert issubclass(McpAppsPlugin, CaoPlugin)


def test_on_mcp_server_default_off_does_not_raise(monkeypatch) -> None:
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    # Default-off: registration is best-effort and must never raise.
    McpAppsPlugin().on_mcp_server(_FakeMcp())


def test_on_mcp_server_registers_tools_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    fake = _FakeMcp()
    McpAppsPlugin().on_mcp_server(fake)
    assert fake.tools, "expected the MCP App tools to register via mcp.tool"


def test_register_mcp_server_surfaces_dispatches_to_plugin(monkeypatch) -> None:
    # The dispatcher discovers the cao.plugins group and invokes on_mcp_server on
    # each; the mcp_apps entry registers the surface while others no-op. Proves
    # the plugin is wired through the entry-point group, not just callable.
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    fake = _FakeMcp()
    register_mcp_server_surfaces(fake)
    assert fake.tools, "expected mcp_apps to register the app tools via discovery"


def _no_idp(monkeypatch) -> None:
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)


def test_warns_when_enabled_without_idp(monkeypatch, caplog) -> None:
    # Enabled + no IdP: the surface mounts with authorization off, so a startup
    # warning must surface the unauthenticated localhost-trust posture.
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    _no_idp(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.builtin.mcp_apps"):
        McpAppsPlugin().on_mcp_server(_FakeMcp())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no IdP" in m and "CAO_MCP_APPS_ENABLED" in m for m in warnings), warnings


def test_no_warning_when_idp_configured(monkeypatch, caplog) -> None:
    # An IdP is configured, so the auth layer enforces scopes; the posture
    # warning must not fire.
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/.well-known/jwks.json")
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.builtin.mcp_apps"):
        McpAppsPlugin().on_mcp_server(_FakeMcp())
    assert not any("no IdP" in r.getMessage() for r in caplog.records)


def test_no_warning_when_surface_disabled(monkeypatch, caplog) -> None:
    # Default-off: no surface, so no posture warning regardless of IdP config.
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    _no_idp(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.builtin.mcp_apps"):
        McpAppsPlugin().on_mcp_server(_FakeMcp())
    assert not any("no IdP" in r.getMessage() for r in caplog.records)
