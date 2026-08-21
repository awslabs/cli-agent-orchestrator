"""Tests for the topology widget MCP App surface.

Coverage:
  * the static bundle exists and references the JS/CSS sidecars
  * mount_widget_static serves /widgets/topology/ when enabled, is idempotent,
    and is a no-op by default (CAO_MCP_APPS_ENABLED unset)
  * register_widget(mcp) is best-effort + default-off: returns False when
    disabled, when fastmcp lacks @mcp.resource, or when the decorator raises;
    returns True with a stubbed decorator — and never raises.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cli_agent_orchestrator.ext_apps import (
    WIDGET_HTML_PATH,
    mount_widget_static,
    register_widget,
)
from cli_agent_orchestrator.ext_apps.widget import WIDGET_MOUNT_PATH, WIDGET_RESOURCE_URI

_STATIC_DIR = WIDGET_HTML_PATH.parent


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)


class TestStaticBundle:
    def test_html_file_exists(self) -> None:
        assert WIDGET_HTML_PATH.exists()

    def test_html_references_js_and_css(self) -> None:
        body = WIDGET_HTML_PATH.read_text(encoding="utf-8")
        assert 'href="topology.css"' in body
        assert 'src="topology.js"' in body

    def test_js_subscribes_to_events(self) -> None:
        js = (_STATIC_DIR / "topology.js").read_text(encoding="utf-8")
        assert "/events" in js
        assert "EventSource" in js

    def test_js_renders_normalized_event_fields(self) -> None:
        """Renders the normalized wire shape (kind/detail), not type/payload.

        ``/events`` publishes ``{id, kind, terminal_id, session_name, timestamp,
        detail}``; the old ``type``/``payload`` field names never existed on the
        wire and rendered "?" / "{}" (Copilot C1).
        """
        js = (_STATIC_DIR / "topology.js").read_text(encoding="utf-8")
        assert "event.kind" in js
        assert "event.detail" in js
        assert "event.type" not in js
        assert "event.payload" not in js

    def test_css_present(self) -> None:
        assert (_STATIC_DIR / "topology.css").exists()


class TestMountWidgetStatic:
    def test_mount_serves_html_when_enabled(self, enabled: None) -> None:
        app = FastAPI()
        mount_widget_static(app)
        client = TestClient(app)
        resp = client.get("/widgets/topology/topology.html")
        assert resp.status_code == 200
        assert "<title>CAO Topology</title>" in resp.text

    def test_mount_is_idempotent(self, enabled: None) -> None:
        app = FastAPI()
        mount_widget_static(app)
        mount_widget_static(app)  # must not raise
        paths = [getattr(r, "path", None) for r in app.routes]
        assert paths.count(WIDGET_MOUNT_PATH) == 1

    def test_mount_is_noop_when_disabled(self, disabled: None) -> None:
        app = FastAPI()
        mount_widget_static(app)
        paths = [getattr(r, "path", None) for r in app.routes]
        assert WIDGET_MOUNT_PATH not in paths


class TestRegisterWidget:
    def test_returns_false_when_disabled(self, disabled: None) -> None:
        class StubMCP:
            def resource(self, uri):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    return fn

                return decorator

        assert register_widget(StubMCP()) is False

    def test_returns_false_when_mcp_has_no_resource_decorator(self, enabled: None) -> None:
        class NoResourceMCP:
            pass

        assert register_widget(NoResourceMCP()) is False

    def test_returns_true_with_stub_decorator(self, enabled: None) -> None:
        registered: list[tuple[str, object]] = []

        class StubMCP:
            def resource(self, uri):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    registered.append((uri, fn))
                    return fn

                return decorator

        assert register_widget(StubMCP()) is True
        assert len(registered) == 1
        uri, fn = registered[0]
        assert uri == WIDGET_RESOURCE_URI
        assert "<title>CAO Topology</title>" in fn()  # type: ignore[operator]

    def test_does_not_raise_when_decorator_fails(self, enabled: None) -> None:
        class BrokenMCP:
            def resource(self, uri):  # type: ignore[no-untyped-def]
                def decorator(fn):  # type: ignore[no-untyped-def]
                    raise RuntimeError("simulated registration failure")

                return decorator

        assert register_widget(BrokenMCP()) is False
