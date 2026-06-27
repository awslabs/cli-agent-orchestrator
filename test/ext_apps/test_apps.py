"""Tests for the ui://cao/* MCP App resources.

Covers the enriched ``_meta.ui`` annotation (layout hints attached only for
resource-rendering tools), the resource-body resolver, and best-effort +
default-off registration.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.ext_apps import (
    AGENT_RESOURCE_URI,
    DASHBOARD_RESOURCE_URI,
    EVENT_STREAM_RESOURCE_URI,
    PREFERRED_FRAMES,
    get_resource_body,
    register_apps,
    ui_meta,
)


class TestUiMeta:
    def test_minimal_shape_without_resource(self) -> None:
        # submit_command-style tool: no resource → no layout hints.
        meta = ui_meta(required_scopes=["cao:write"], visibility=["app"])
        ui = meta["ui"]
        assert ui["requiredScopes"] == ["cao:write"]
        assert ui["visibility"] == ["app"]
        assert "csp" in ui
        assert "resourceUri" not in ui
        assert "preferredFrameSize" not in ui
        assert "domain" not in ui

    def test_enriched_shape_with_resource(self) -> None:
        meta = ui_meta(visibility=["model", "app"], resource_uri=DASHBOARD_RESOURCE_URI)
        ui = meta["ui"]
        assert ui["resourceUri"] == DASHBOARD_RESOURCE_URI
        assert ui["visibility"] == ["model", "app"]
        assert ui["preferredFrameSize"] == PREFERRED_FRAMES[DASHBOARD_RESOURCE_URI]
        assert ui["prefersBorder"] is True
        assert ui["domain"] == "cao-dashboard"

    def test_default_frame_for_unknown_resource(self) -> None:
        meta = ui_meta(resource_uri="ui://cao/unknown")
        assert meta["ui"]["preferredFrameSize"] == {"width": 1280, "height": 800}

    def test_preferred_frames_cover_all_views(self) -> None:
        assert set(PREFERRED_FRAMES) == {
            DASHBOARD_RESOURCE_URI,
            AGENT_RESOURCE_URI,
            EVENT_STREAM_RESOURCE_URI,
        }


class TestGetResourceBody:
    def test_unknown_uri_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            get_resource_body("ui://cao/nope")

    def test_resolves_from_static_dir(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "dashboard.html").write_text("<title>CAO Dashboard</title>", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))
        assert "CAO Dashboard" in get_resource_body(DASHBOARD_RESOURCE_URI)

    def test_missing_artifact_raises(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError):
            get_resource_body(DASHBOARD_RESOURCE_URI)


class TestRegisterApps:
    def test_returns_false_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    return fn

                return decorator

        assert register_apps(StubMCP()) is False

    def test_returns_false_without_resource_decorator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")

        class NoResourceMCP:
            pass

        assert register_apps(NoResourceMCP()) is False

    def test_registers_when_enabled_and_built(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        for name in ("dashboard.html", "agent.html", "event-stream.html"):
            (tmp_path / name).write_text(f"<title>{name}</title>", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))

        registered: list[str] = []

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    registered.append(uri)
                    return fn

                return decorator

        assert register_apps(StubMCP()) is True
        assert set(registered) == {
            DASHBOARD_RESOURCE_URI,
            AGENT_RESOURCE_URI,
            EVENT_STREAM_RESOURCE_URI,
        }
