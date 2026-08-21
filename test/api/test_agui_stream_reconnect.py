"""Reconnect event-loss fix for the AG-UI stream endpoint (``Last-Event-ID``).

Reproduces the reviewer's must-fix (PR #436, ``fanhongy``): the viewer relies on
native ``EventSource`` reconnect, which resends the last ``id:`` cursor as the
``Last-Event-ID`` request header — but the endpoint only replayed on ``?since=``,
so any event missed during a drop was never replayed
(``history_calls=0, missed_event_replayed=False``).

The fix: accept ``Last-Event-ID`` and replay the event-log records *after* that
id before draining the live queue, while ``?since=`` keeps precedence. These
tests pin that contract at the HTTP boundary.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")


class _FakeBus:
    """Finite SseBus stand-in: ``drain`` yields the given events then returns."""

    def __init__(self, events):
        self._events = list(events)

    def register(self, overflow_close: bool = False):
        return object()

    def unregister(self, sub):
        pass

    async def drain(self, sub):
        for event in self._events:
            yield event


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)


def _missed_event(eid: str = "evt-missed") -> dict:
    return {
        "id": eid,
        "kind": "launch",
        "terminal_id": "t-1",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:05Z",
        "detail": {"agent_name": "w", "provider": "mock_cli"},
    }


def test_last_event_id_replays_missed(monkeypatch):
    """A ``Last-Event-ID`` header triggers the after-id lookup and replays the
    record produced after that id exactly once."""
    calls = {"after_id": [], "history": 0}
    missed = _missed_event()

    class _Log:
        def history(self, since=None, **kwargs):
            calls["history"] += 1
            return []

        def after_id(self, event_id, **kwargs):
            calls["after_id"].append(event_id)
            return [missed] if event_id == "cursor-42" else []

    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus([]))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )

    with client.stream("GET", "/agui/v1/stream", headers={"Last-Event-ID": "cursor-42"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    # The after-id lookup was invoked with the header value, ``?since=`` history
    # replay was NOT used, and the missed record was replayed exactly once.
    assert calls["after_id"] == ["cursor-42"]
    assert calls["history"] == 0
    assert body.count("id: evt-missed") == 1


def test_since_takes_precedence_over_last_event_id(monkeypatch):
    """When both ``?since=`` and ``Last-Event-ID`` are supplied, ``?since=`` wins
    and the after-id path is not taken."""
    calls = {"after_id": 0, "since": None}
    replayed = _missed_event("evt-since")

    class _Log:
        def history(self, since=None, **kwargs):
            calls["since"] = since
            return [replayed]

        def after_id(self, event_id, **kwargs):
            calls["after_id"] += 1
            return [_missed_event("evt-should-not-appear")]

    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus([]))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )

    with client.stream(
        "GET",
        "/agui/v1/stream",
        params={"since": "2026-07-04T00:00:00Z"},
        headers={"Last-Event-ID": "cursor-42"},
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    assert calls["since"] == "2026-07-04T00:00:00Z"
    assert calls["after_id"] == 0  # precedence: since wins, after-id skipped
    assert "id: evt-since" in body
    assert "evt-should-not-appear" not in body


def test_no_cursor_does_not_replay(monkeypatch):
    """With neither ``?since=`` nor ``Last-Event-ID``, no history replay runs."""
    calls = {"after_id": 0, "history": 0}

    class _Log:
        def history(self, since=None, **kwargs):
            calls["history"] += 1
            return []

        def after_id(self, event_id, **kwargs):
            calls["after_id"] += 1
            return []

    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus([]))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )

    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    assert calls["after_id"] == 0
    assert calls["history"] == 0
