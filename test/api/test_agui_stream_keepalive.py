"""Idle keep-alive contract for the AG-UI ambient stream.

An idle fleet publishes nothing, so without a keep-alive the stream sends no
bytes and a proxy with a read timeout closes it (CloudFront's default is 30s).
The endpoint therefore emits the run plane's ``:keep-alive`` SSE comment after
each ``RUN_PLANE_HEARTBEAT_SECONDS`` of silence, and the event that ends the
silence still arrives exactly once.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui import run_plane

client = TestClient(app, base_url="http://localhost")

_EVENT = {
    "id": "evt-late",
    "kind": "launch",
    "terminal_id": "t-1",
    "session_name": "s",
    "timestamp": "2026-07-04T00:00:01Z",
    "detail": {"agent_name": "w", "provider": "mock_cli"},
}


class _SlowBus:
    """SseBus stand-in whose ``drain`` waits ``delay`` seconds, yields one event, then ends."""

    def __init__(self, delay: float):
        self._delay = delay
        self.unregistered = 0

    def register(self, overflow_close: bool = False):
        return object()

    def unregister(self, sub):
        self.unregistered += 1

    async def drain(self, sub):
        await asyncio.sleep(self._delay)
        yield dict(_EVENT)


class _Log:
    def history(self, since=None, **kwargs):
        return []

    def after_id(self, event_id, **kwargs):
        return []


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )


def _stream_body(monkeypatch, bus) -> str:
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: bus)
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        return "".join(resp.iter_text())


def test_idle_stream_sends_keepalive_then_the_event_once(monkeypatch):
    monkeypatch.setattr(run_plane, "RUN_PLANE_HEARTBEAT_SECONDS", 0.05)
    bus = _SlowBus(delay=0.3)

    body = _stream_body(monkeypatch, bus)

    assert ":keep-alive\n\n" in body
    # The keep-alives come before the event, and the shielded read was not lost.
    assert body.index(":keep-alive") < body.index("id: evt-late")
    assert body.count("id: evt-late") == 1
    assert bus.unregistered == 1


def test_busy_stream_sends_no_keepalive(monkeypatch):
    monkeypatch.setattr(run_plane, "RUN_PLANE_HEARTBEAT_SECONDS", 5.0)
    bus = _SlowBus(delay=0.0)

    body = _stream_body(monkeypatch, bus)

    assert ":keep-alive" not in body
    assert body.count("id: evt-late") == 1
