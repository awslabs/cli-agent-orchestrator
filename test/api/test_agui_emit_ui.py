"""AUD-01: the generative-UI producer endpoint (`POST /agui/v1/emit_ui`).

Proves the loop is real end-to-end: an agent-authored UI intent is validated
server-side against the frozen allow-list, published onto the fleet event bus,
and maps to a GENERATIVE_UI frame via the AG-UI adapter. Off-list components
are refused at the producer (400) so they never reach the bus.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_GENERATIVE_UI,
    to_agui_event,
)

# Host in ALLOWED_HOSTS so TrustedHostMiddleware admits the request.
client = TestClient(app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    # Ensure auth is off for the test (query-param/header auth not exercised here).
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)


def test_emit_ui_publishes_generative_ui_intent():
    resp = client.post(
        "/agui/v1/emit_ui",
        json={
            "component": "approval_card",
            "props": {"title": "Approve handoff?", "risk": "high"},
            "terminal_id": "t-1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["component"] == "approval_card"
    assert body["event_id"]

    # The event is now in the fleet history; the adapter maps it to GENERATIVE_UI.
    from cli_agent_orchestrator.services.event_log_service import get_event_log

    events = get_event_log().history()
    match = [e for e in events if e.get("id") == body["event_id"]]
    assert match, "emitted event not found in fleet history"
    agui_type, data = to_agui_event(match[0])
    assert agui_type == AGUI_GENERATIVE_UI
    assert data["component"] == "approval_card"
    assert data["props"]["title"] == "Approve handoff?"


def test_emit_ui_refuses_off_list_component():
    resp = client.post(
        "/agui/v1/emit_ui",
        json={"component": "iframe", "props": {"src": "http://evil"}},
    )
    assert resp.status_code == 400
    assert "iframe" in resp.text


def test_emit_ui_rejects_oversized_props():
    resp = client.post(
        "/agui/v1/emit_ui",
        json={"component": "metric", "props": {"blob": "x" * 20000}},
    )
    assert resp.status_code == 400
    assert "too large" in resp.text.lower()


def test_emit_ui_404_when_surface_disabled(monkeypatch):
    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "false")
    resp = client.post("/agui/v1/emit_ui", json={"component": "metric", "props": {}})
    assert resp.status_code == 404
