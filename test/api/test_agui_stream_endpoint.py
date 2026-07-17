"""Coverage for the AG-UI SSE endpoint (`/agui/v1/stream`).

Exercises the query-parameter auth branches (401/403) and the streaming
generator (history replay via ``?since=`` + STATE_SNAPSHOT on connect + a
per-event AG-UI frame + STATE_DELTA), without leaving the stream open: the
bus is stubbed to yield one event and then complete.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")


class _FakeBus:
    """SseBus stand-in for the streaming tests.

    Exposes the register/unregister/drain seam the endpoint uses. ``drain``
    yields a finite sequence and returns, so the SSE generator ends cleanly
    (frames flush, stream closes) instead of blocking on a live queue forever —
    no test can hang.
    """

    def __init__(self, events):
        self._events = list(events)

    def register(self, overflow_close=False):
        return object()

    def unregister(self, queue):
        pass

    async def drain(self, queue):
        for event in self._events:
            yield event


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")


def test_stream_requires_token_when_auth_enabled(monkeypatch):
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    resp = client.get("/agui/v1/stream")
    assert resp.status_code == 401
    assert "access_token" in resp.text


def test_stream_rejects_insufficient_scope(monkeypatch):
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(main, "extract_scopes_from_token", lambda tok: ["some:other"])
    resp = client.get("/agui/v1/stream", params={"access_token": "x"})
    assert resp.status_code == 403


def test_stream_replays_history_and_emits_state_frames(monkeypatch):
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)

    # A historical event for ?since= replay.
    replay_event = {
        "id": "evt-old",
        "kind": "handoff",
        "terminal_id": "t-1",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:00Z",
        "detail": {"sender": "a", "receiver": "b", "orchestration_type": "handoff"},
    }

    # An older event that a correct ``since=`` pass-through must exclude: the
    # endpoint forwards the query param to ``EventLog.history(since=...)`` and
    # this fake honors it with the same strictly-greater-than contract, so the
    # replay wiring in main.py is exercised end-to-end rather than stubbed out.
    stale_event = {
        "id": "evt-stale",
        "kind": "completion",
        "terminal_id": "t-0",
        "session_name": "s",
        "timestamp": "2026-07-03T23:59:58Z",
        "detail": {"agent_name": "old", "provider": "mock_cli"},
    }
    # Exactly AT the ?since= cursor: the contract is strictly-greater-than, so
    # this one must be EXCLUDED (a >= replay would re-deliver the event the
    # client already saw).
    boundary_event = {
        "id": "evt-boundary",
        "kind": "completion",
        "terminal_id": "t-1",
        "session_name": "s",
        "timestamp": "2026-07-03T23:59:59Z",
        "detail": {"agent_name": "edge", "provider": "mock_cli"},
    }

    seen: dict = {}

    class _FakeLog:
        def history(self, since=None, **kwargs):
            # Capture the forwarded value so the test can pin that main.py
            # passes the query param through verbatim (not just that some
            # filtering happened).
            seen["since"] = since
            events = [stale_event, boundary_event, replay_event]
            if since is None:
                return events
            return [e for e in events if str(e["timestamp"]) > since]

    # A single live event, then the subscription completes so the stream closes.
    live_event = {
        "id": "evt-live",
        "kind": "launch",
        "terminal_id": "t-2",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:01Z",
        "detail": {"agent_name": "worker", "provider": "mock_cli"},
    }

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log",
        lambda: _FakeLog(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.sse_bus.get_bus",
        lambda: _FakeBus([live_event]),
    )

    with client.stream("GET", "/agui/v1/stream", params={"since": "2026-07-03T23:59:59Z"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    # Replay frame + connect snapshot + live event frame all present.
    assert "STATE_SNAPSHOT" in body
    assert "STEP_STARTED" in body  # from the live launch event
    assert "event:" in body and "data:" in body
    # The ?since= boundary is enforced through the endpoint: the newer replay
    # event is present, the older one is filtered out before hitting the wire,
    # and the event exactly AT the cursor is excluded (strictly greater than).
    assert "evt-old" in body
    assert "evt-stale" not in body
    assert "evt-boundary" not in body
    # main.py forwarded the query param verbatim into the log lookup.
    assert seen["since"] == "2026-07-03T23:59:59Z"


def test_stream_fleet_snapshot_with_terminals_emits_delta(monkeypatch):
    """Exercise ``_fleet_snapshot`` over a session that HAS terminals, and the
    STATE_DELTA branch when the fleet snapshot changes between events."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)

    # A live session so the terminal-listing loop inside _fleet_snapshot runs.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_sessions",
        lambda: [{"id": "sess-1", "name": "sess-1"}],
    )

    calls = {"n": 0}

    def _terms(session_id):
        # Return an extra terminal on the second snapshot so the fleet state
        # moves and a STATE_DELTA is emitted after the live event.
        calls["n"] += 1
        terms = [
            {
                "id": "term-a",
                "name": "w1",
                "provider": "mock_cli",
                "session_name": "sess-1",
                "status": "idle",
            }
        ]
        if calls["n"] >= 2:
            terms.append(
                {
                    "id": "term-b",
                    "name": "w2",
                    "provider": "mock_cli",
                    "session_name": "sess-1",
                    "status": "processing",
                }
            )
        return terms

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.list_terminals_by_session", _terms)

    live_event = {
        "id": "ev-1",
        "kind": "launch",
        "terminal_id": "term-a",
        "session_name": "sess-1",
        "timestamp": "2026-07-04T00:00:01Z",
        "detail": {"agent_name": "w1", "provider": "mock_cli"},
    }

    class _Log:
        def history(self, **kwargs):
            return []

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus([live_event])
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )

    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    assert "STATE_SNAPSHOT" in body  # connect snapshot (over a populated fleet)
    assert "STATE_DELTA" in body  # snapshot moved after the live event
    assert calls["n"] >= 2  # _fleet_snapshot ran on connect and after the event
