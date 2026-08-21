"""Overflow → reconnect → backfill contract for the AG-UI stream endpoint.

Reproduces the reviewer's must-fix #3 residue at the HTTP boundary (PR #436,
``fanhongy``): when a subscriber's bounded queue overflows the stream must
*close* (so the browser's ``EventSource`` reconnects) rather than silently
dropping events while holding the connection open. On reconnect the endpoint
replays the dropped record via ``Last-Event-ID`` exactly once.

The real overflow mechanics live in ``test/services/test_sse_bus_overflow.py``;
here we pin the *endpoint* contract:
* it registers its live subscription with ``overflow_close=True`` (opts into the
  gap-signal behaviour); and
* when the bus drain closes (overflow), the HTTP stream ends and the subscriber
  is unregistered — it is no longer left open; then
* a follow-up request carrying ``Last-Event-ID`` replays the dropped record once.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")


class _OverflowBus:
    """SseBus stand-in whose ``drain`` yields a finite prefix then returns.

    A finite ``drain`` models the overflow-close path: the endpoint delivers the
    pre-gap events and then the generator ends. Records register/unregister and
    the ``overflow_close`` flag the endpoint passes, so the test can assert the
    subscriber is opened with gap-signalling on and is not left open afterward.
    """

    def __init__(self, events):
        self._events = list(events)
        self.overflow_close = None
        self.registered = 0
        self.unregistered = 0

    def register(self, overflow_close: bool = False):
        self.registered += 1
        self.overflow_close = overflow_close
        return object()

    def unregister(self, sub):
        self.unregistered += 1

    async def drain(self, sub):
        for event in self._events:
            yield event


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)


def test_overflow_triggers_reconnect_backfill(monkeypatch):
    # Two pre-gap events delivered before overflow closes the stream.
    pre_gap = [
        {
            "id": "evt-1",
            "kind": "launch",
            "terminal_id": "t-1",
            "session_name": "s",
            "timestamp": "2026-07-04T00:00:01Z",
            "detail": {"agent_name": "w", "provider": "mock_cli"},
        },
        {
            "id": "evt-2",
            "kind": "completion",
            "terminal_id": "t-1",
            "session_name": "s",
            "timestamp": "2026-07-04T00:00:02Z",
            "detail": {"agent_name": "w", "provider": "mock_cli"},
        },
    ]
    # The record that was DROPPED on overflow — must be replayed on reconnect.
    dropped = {
        "id": "evt-3-dropped",
        "kind": "completion",
        "terminal_id": "t-1",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:03Z",
        "detail": {"agent_name": "w", "provider": "mock_cli"},
    }

    seen = {"after_id": None, "after_id_calls": 0}

    class _Log:
        def history(self, since=None, **kwargs):
            return []

        def after_id(self, event_id, **kwargs):
            seen["after_id"] = event_id
            seen["after_id_calls"] += 1
            # Everything strictly after the client's last-seen id — i.e. the
            # record dropped during the overflow gap.
            return [dropped] if event_id == "evt-2" else []

    bus = _OverflowBus(pre_gap)
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: bus)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )

    # 1) First connection: overflow closes the stream after the pre-gap events.
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    # The endpoint opted into the gap-signal behaviour and the subscriber was
    # NOT left open — it was unregistered when the drain closed.
    assert bus.overflow_close is True
    assert bus.registered == 1 and bus.unregistered == 1
    assert "id: evt-1" in body and "id: evt-2" in body
    # The dropped record was not on the first stream (that is the gap).
    assert "evt-3-dropped" not in body

    # 2) Reconnect with Last-Event-ID = the last id the client actually saw.
    bus2 = _OverflowBus([])  # live drain empty; only the replay matters here
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: bus2)
    with client.stream("GET", "/agui/v1/stream", headers={"Last-Event-ID": "evt-2"}) as resp:
        assert resp.status_code == 200
        body2 = "".join(resp.iter_text())

    # The dropped record is replayed exactly once via the after-id lookup.
    assert seen["after_id"] == "evt-2"
    assert seen["after_id_calls"] == 1
    assert body2.count("id: evt-3-dropped") == 1
