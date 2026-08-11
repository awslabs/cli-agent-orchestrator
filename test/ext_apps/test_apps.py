"""Tests for the ui://cao/* MCP App resources.

Covers the enriched ``_meta.ui`` annotation (layout hints attached only for
resource-rendering tools), the resource-body resolver, and best-effort +
default-off registration.
"""

from __future__ import annotations

import logging
import shutil

import pytest

import cli_agent_orchestrator.ext_apps.apps as apps_module
from cli_agent_orchestrator.ext_apps import (
    AGENT_RESOURCE_URI,
    DASHBOARD_RESOURCE_URI,
    EVENT_STREAM_RESOURCE_URI,
    GRAPH_RESOURCE_URI,
    PREFERRED_FRAMES,
    get_resource_body,
    register_apps,
    ui_meta,
)
from cli_agent_orchestrator.ext_apps.apps import _RESOURCE_FILES, RESOURCE_MIME_TYPE


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
        # CAO declares no elevated browser permissions by design.
        assert "permissions" not in ui

    def test_enriched_shape_with_resource(self) -> None:
        meta = ui_meta(visibility=["model", "app"], resource_uri=DASHBOARD_RESOURCE_URI)
        ui = meta["ui"]
        assert ui["resourceUri"] == DASHBOARD_RESOURCE_URI
        assert ui["visibility"] == ["model", "app"]
        assert ui["preferredFrameSize"] == PREFERRED_FRAMES[DASHBOARD_RESOURCE_URI]
        assert ui["prefersBorder"] is True
        assert ui["domain"] == "cao-dashboard"
        # No elevated permissions requested for the read-only fleet views.
        assert "permissions" not in ui

    def test_permissions_omitted_by_default(self) -> None:
        # The spec `permissions` field is an OBJECT keyed by capability
        # (camera/microphone/geolocation/clipboardWrite). CAO needs none, so the
        # field is omitted (== no permissions requested, per the spec default).
        assert "permissions" not in ui_meta()["ui"]
        assert "permissions" not in ui_meta(resource_uri=AGENT_RESOURCE_URI)["ui"]

    def test_permissions_emitted_as_object_when_requested(self) -> None:
        # Fidelity: when a caller declares permissions, they pass through as the
        # spec's object shape (each capability is an empty object `{}`).
        meta = ui_meta(
            resource_uri=DASHBOARD_RESOURCE_URI,
            permissions={"clipboardWrite": {}},
        )
        assert meta["ui"]["permissions"] == {"clipboardWrite": {}}

    def test_resource_mime_type_is_spec_literal(self) -> None:
        # SEP-1865 (stable 2026-01-26): HTML UI resources MUST use this MIME.
        assert RESOURCE_MIME_TYPE == "text/html;profile=mcp-app"

    def test_default_frame_for_unknown_resource(self) -> None:
        meta = ui_meta(resource_uri="ui://cao/unknown")
        assert meta["ui"]["preferredFrameSize"] == {"width": 1280, "height": 800}

    def test_preferred_frames_cover_all_views(self) -> None:
        assert set(PREFERRED_FRAMES) == {
            DASHBOARD_RESOURCE_URI,
            AGENT_RESOURCE_URI,
            EVENT_STREAM_RESOURCE_URI,
            GRAPH_RESOURCE_URI,
        }

    def test_graph_resource_uses_dashboard_size_frame(self) -> None:
        meta = ui_meta(visibility=["model", "app"], resource_uri=GRAPH_RESOURCE_URI)
        assert meta["ui"]["preferredFrameSize"] == {"width": 1280, "height": 800}
        assert meta["ui"]["domain"] == "cao-graph"


class TestGraphResourceRegistration:
    def test_graph_resource_file_mapping(self) -> None:
        assert _RESOURCE_FILES[GRAPH_RESOURCE_URI] == "graph.html"

    def test_graph_resource_uses_default_csp(self) -> None:
        # No new CSP domain for the graph resource — reuses DEFAULT_CSP unchanged.
        meta = ui_meta(visibility=["model", "app"], resource_uri=GRAPH_RESOURCE_URI)
        assert meta["ui"]["csp"] == {
            "connectDomains": ["http://127.0.0.1:9889", "http://localhost:9889"],
            "resourceDomains": [],
            "frameDomains": [],
            "baseUriDomains": [],
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

    def test_empty_artifact_raises(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "graph.html").write_text("", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError):
            get_resource_body(GRAPH_RESOURCE_URI)


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
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")

        class NoResourceMCP:
            pass

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(NoResourceMCP()) is False

        records = [
            record
            for record in caplog.records
            if "no @mcp.resource decorator" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].levelno >= logging.WARNING

    def test_missing_bundles_warn_at_visible_startup_level(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setattr("cli_agent_orchestrator.ext_apps.apps.apps_static_dir", lambda: None)

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    return fn

                return decorator

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(StubMCP()) is False

        records = [
            record for record in caplog.records if "apps_static/ not found" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].levelno >= logging.WARNING

    def test_complete_registration_has_no_warning(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for name in ("dashboard.html", "agent.html", "event-stream.html", "graph.html"):
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

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(StubMCP()) is True
        assert set(registered) == {
            DASHBOARD_RESOURCE_URI,
            AGENT_RESOURCE_URI,
            EVENT_STREAM_RESOURCE_URI,
            GRAPH_RESOURCE_URI,
        }
        posture_records = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert posture_records == []

    def test_partial_artifacts_warn(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for name in _RESOURCE_FILES.values():
            if name == "graph.html":
                continue
            (tmp_path / name).write_text(f"<title>{name}</title>", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    return fn

                return decorator

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(StubMCP()) is True

        records = [
            record for record in caplog.records if "3/4 artifacts present" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].levelno >= logging.WARNING
        assert "registered 4 handlers" in records[0].getMessage()

    def test_zero_artifacts_warn(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    return fn

                return decorator

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(StubMCP()) is True

        records = [
            record for record in caplog.records if "MCP App resources:" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].levelno >= logging.WARNING
        assert "registered 4 handlers" in records[0].getMessage()
        assert "0/4 artifacts present" in records[0].getMessage()
        assert str(tmp_path) in records[0].getMessage()

    def test_truncated_artifact_warns_and_serves_placeholder(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for name in _RESOURCE_FILES.values():
            (tmp_path / name).write_text(f"<title>{name}</title>", encoding="utf-8")
        (tmp_path / "graph.html").write_text("", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))
        handlers = {}

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    handlers[uri] = fn
                    return fn

                return decorator

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(StubMCP()) is True
            html = handlers[GRAPH_RESOURCE_URI]()

        summary_records = [
            record for record in caplog.records if "3/4 artifacts present" in record.getMessage()
        ]
        assert len(summary_records) == 1
        assert summary_records[0].levelno >= logging.WARNING
        request_records = [
            record
            for record in caplog.records
            if "artifact missing at request time: graph.html" in record.getMessage()
        ]
        assert len(request_records) == 1
        assert request_records[0].levelno >= logging.WARNING
        assert "view not built" in html
        assert html != ""

    def test_registered_handler_does_not_switch_static_directories(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        override_dir = tmp_path / "override"
        override_dir.mkdir()
        for name in _RESOURCE_FILES.values():
            (override_dir / name).write_text(f"<title>{name}</title>", encoding="utf-8")

        fake_module = tmp_path / "fake-package" / "cli_agent_orchestrator" / "ext_apps" / "apps.py"
        fallback_dir = fake_module.parents[1] / "ext_apps" / "apps_static"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / "dashboard.html").write_text(
            "<title>STALE FALLBACK dashboard.html</title>",
            encoding="utf-8",
        )
        monkeypatch.setattr(apps_module, "__file__", str(fake_module))
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(override_dir))
        handlers = {}

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    handlers[uri] = fn
                    return fn

                return decorator

        assert register_apps(StubMCP()) is True
        shutil.rmtree(override_dir)

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.ext_apps.apps"):
            html = handlers[DASHBOARD_RESOURCE_URI]()

        assert "STALE FALLBACK" not in html
        assert "view not built" in html
        request_records = [
            record
            for record in caplog.records
            if "artifact missing at request time: dashboard.html" in record.getMessage()
        ]
        assert len(request_records) == 1
        assert request_records[0].levelno >= logging.WARNING

    def test_handlers_and_bound_body_resolver_agree_for_all_resources(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        startup_dir = tmp_path / "startup"
        current_dir = tmp_path / "current"
        startup_dir.mkdir()
        current_dir.mkdir()
        for uri, name in _RESOURCE_FILES.items():
            (startup_dir / name).write_text(f"<title>STARTUP {uri}</title>", encoding="utf-8")
            (current_dir / name).write_text(f"<title>CURRENT {uri}</title>", encoding="utf-8")

        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(startup_dir))
        handlers = {}

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    handlers[uri] = fn
                    return fn

                return decorator

        assert register_apps(StubMCP()) is True
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(current_dir))

        for uri in _RESOURCE_FILES:
            handler_html = handlers[uri]()
            bound_html = get_resource_body(uri, startup_dir)
            current_html = get_resource_body(uri)
            assert handler_html == bound_html
            assert f"STARTUP {uri}" in handler_html
            assert f"CURRENT {uri}" in current_html
            assert current_html != handler_html

    def test_decorator_failure_logs_error_with_uri(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for name in _RESOURCE_FILES.values():
            (tmp_path / name).write_text(f"<title>{name}</title>", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    if uri == EVENT_STREAM_RESOURCE_URI:
                        raise RuntimeError("registration failed")
                    return fn

                return decorator

        with caplog.at_level(logging.ERROR, logger="cli_agent_orchestrator.ext_apps.apps"):
            assert register_apps(StubMCP()) is True

        records = [
            record
            for record in caplog.records
            if "Failed to register MCP App resource" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].levelno >= logging.ERROR
        assert EVENT_STREAM_RESOURCE_URI in records[0].getMessage()

    def test_graph_resource_gated_by_apps_enabled(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The graph resource follows the same CAO_MCP_APPS_ENABLED gate as the rest."""

        (tmp_path / "graph.html").write_text("<title>graph.html</title>", encoding="utf-8")
        monkeypatch.setenv("CAO_MCP_APPS_STATIC_DIR", str(tmp_path))

        class StubMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    return fn

                return decorator

        monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
        assert register_apps(StubMCP()) is False

        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        registered: list[str] = []

        class RecordingMCP:
            def resource(self, uri, **kw):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    registered.append(uri)
                    return fn

                return decorator

        assert register_apps(RecordingMCP()) is True
        assert GRAPH_RESOURCE_URI in registered
