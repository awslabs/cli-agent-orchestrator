"""Coverage for the AG-UI SSE endpoint's error/edge branches (`/agui/v1/stream`)
and the ``emit_ui`` validation guards.

The happy paths (auth 401/403, ``?since=`` replay, STATE_SNAPSHOT/DELTA over a
populated fleet) live in ``test_agui_stream_endpoint.py``. This module targets
the failure-isolation branches that keep the stream alive when a backend hiccups:

* a token-validation exception mapping to a clean 401,
* ``_fleet_snapshot`` swallowing a per-session terminal-listing error,
* the ``?since=`` history replay swallowing a log error,
* the connect STATE_SNAPSHOT and per-event STATE_DELTA swallowing snapshot errors,
* the replay/live de-duplication ``continue`` when the same event id appears in
  both the ``?since=`` replay and the live drain,

plus ``emit_ui`` rejecting an oversized props payload. All fakes drain a finite
sequence so the SSE generator always terminates (no test can hang).
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


def _install_bus(monkeypatch, events, log=None):
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus(events))

    class _Log:
        def history(self, **kwargs):
            return []

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log",
        lambda: (log if log is not None else _Log()),
    )


def test_token_validation_exception_maps_to_401(monkeypatch):
    """A non-HTTP error from token parsing (e.g. a malformed JWT) fails closed
    as a clean 401, not an opaque 500."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)

    def _boom(_tok):
        raise ValueError("malformed token")

    monkeypatch.setattr(main, "extract_scopes_from_token", _boom)
    resp = client.get("/agui/v1/stream", params={"access_token": "garbage"})
    assert resp.status_code == 401
    assert "invalid or expired" in resp.text


def test_token_httpexception_is_reraised(monkeypatch):
    """An HTTPException raised during token validation is re-raised verbatim
    (the ``except HTTPException: raise`` branch), not swallowed into a 401."""
    from fastapi import HTTPException

    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)

    def _raise_http(_tok):
        raise HTTPException(status_code=403, detail="explicit forbidden")

    monkeypatch.setattr(main, "extract_scopes_from_token", _raise_http)
    resp = client.get("/agui/v1/stream", params={"access_token": "x"})
    assert resp.status_code == 403
    assert "explicit forbidden" in resp.text


def test_fleet_snapshot_isolates_terminal_listing_failure(monkeypatch):
    """A per-session terminal-listing error is swallowed; the snapshot still
    emits (with that session contributing no terminals) and the stream lives."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_sessions",
        lambda: [{"id": "sess-1", "name": "sess-1"}],
    )

    def _boom(_session_id):
        raise RuntimeError("backend down")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.list_terminals_by_session", _boom)
    _install_bus(monkeypatch, [])

    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    # Snapshot still emitted despite the terminal-listing failure.
    assert "STATE_SNAPSHOT" in body


def test_history_replay_failure_is_isolated(monkeypatch):
    """If the event-log replay raises, the endpoint logs and continues to the
    live stream rather than 500-ing."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)

    class _BoomLog:
        def history(self, since=None, **kwargs):
            raise RuntimeError("log unavailable")

    live_event = {
        "id": "ev-live",
        "kind": "launch",
        "terminal_id": "t-2",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:01Z",
        "detail": {"agent_name": "worker", "provider": "mock_cli"},
    }
    _install_bus(monkeypatch, [live_event], log=_BoomLog())

    with client.stream("GET", "/agui/v1/stream", params={"since": "2026-07-01T00:00:00Z"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    # Replay blew up but the live event still made it onto the wire.
    assert "STEP_STARTED" in body


def test_snapshot_and_delta_failures_are_isolated(monkeypatch):
    """If ``_fleet_snapshot`` raises, both the connect STATE_SNAPSHOT and the
    per-event STATE_DELTA branches swallow the error and the live event frame
    is still delivered."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)

    def _boom():
        raise RuntimeError("session backend down")

    # list_sessions raises inside _fleet_snapshot -> both snapshot & delta except.
    monkeypatch.setattr("cli_agent_orchestrator.services.session_service.list_sessions", _boom)

    live_event = {
        "id": "ev-live",
        "kind": "launch",
        "terminal_id": "t-2",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:01Z",
        "detail": {"agent_name": "worker", "provider": "mock_cli"},
    }
    _install_bus(monkeypatch, [live_event])

    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    # No snapshot could be built, but the event frame still streamed.
    assert "STEP_STARTED" in body
    assert "STATE_SNAPSHOT" not in body


def test_replay_live_overlap_is_deduped(monkeypatch):
    """When the same event id appears in both the ``?since=`` replay and the
    live drain, the live copy is skipped (the dedup ``continue`` branch)."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)

    dup = {
        "id": "evt-dup",
        "kind": "launch",
        "terminal_id": "t-1",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:05Z",
        "detail": {"agent_name": "w", "provider": "mock_cli"},
    }

    class _Log:
        def history(self, since=None, **kwargs):
            return [dup]

    # The same event id arrives live -> must be de-duplicated (skipped).
    _install_bus(monkeypatch, [dup], log=_Log())

    with client.stream("GET", "/agui/v1/stream", params={"since": "2026-07-01T00:00:00Z"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    # The SSE `id:` cursor for the event appears exactly once (the replayed
    # frame); the live copy was skipped by the dedup ``continue`` branch. Without
    # dedup there would be two `id: evt-dup` frames.
    assert body.count("id: evt-dup") == 1


def test_emit_ui_rejects_oversized_props(monkeypatch):
    """A props payload over the 8 KB bound is refused with 400."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    big = {"blob": "x" * (8 * 1024 + 10)}
    resp = client.post("/agui/v1/emit_ui", json={"component": "metric", "props": big})
    assert resp.status_code == 400
    assert "too large" in resp.text


def test_emit_ui_rejects_non_serializable_props():
    """A props value that ``json.dumps`` cannot encode is refused with 400.

    This branch is unreachable through the HTTP body (a parsed JSON body is
    serializable by construction), so it is exercised by invoking the endpoint
    coroutine directly with a non-JSON-serializable value (a ``set``), bypassing
    the scope dependency. Guards the defensive serialize check server-side.
    """
    import asyncio

    from fastapi import HTTPException

    from cli_agent_orchestrator.api.main import EmitUIRequest, agui_emit_ui

    body = EmitUIRequest(component="metric", props={"bad": {1, 2, 3}})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(agui_emit_ui(body, _scopes=["cao:write"]))
    assert excinfo.value.status_code == 400
    assert "JSON-serializable" in str(excinfo.value.detail)
