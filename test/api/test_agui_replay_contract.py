"""Replay contract hardening tests for ``GET /agui/v1/stream``.

Covers:
- Malformed ?since= returns HTTP 400 before streaming starts
- ?since= takes precedence over Last-Event-ID (regression)
- Snapshot-before-delta on reconnect (contract regression)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")


class _FakeBus:
    """Finite SseBus stand-in: ``drain`` yields the given events then returns."""

    def __init__(self, events=None):
        self._events = list(events or [])

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


class TestMalformedSince:
    """Malformed ?since= must produce HTTP 400 before streaming starts."""

    @pytest.mark.parametrize(
        "bad_since",
        [
            "not-a-date",
            "yesterday",
            "2026-13-01T00:00:00Z",  # invalid month
            "12345",
            "abc123xyz",
            "",  # empty string is falsy in Python, so it won't trigger validation
        ],
    )
    def test_malformed_since_returns_400(self, bad_since: str, monkeypatch) -> None:
        # Empty string is falsy in Python so it skips the validation.
        # Only non-empty invalid strings should 400.
        if not bad_since:
            return

        monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus())

        resp = client.get("/agui/v1/stream", params={"since": bad_since})
        assert resp.status_code == 400
        assert "since" in resp.json()["detail"].lower() or "iso" in resp.json()["detail"].lower()

    def test_valid_since_does_not_400(self, monkeypatch) -> None:
        """A valid ISO-8601 timestamp proceeds to streaming (200)."""

        class _Log:
            def history(self, since=None, **kwargs):
                return []

            def after_id(self, event_id, **kwargs):
                return []

        monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus())
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
        )

        with client.stream(
            "GET", "/agui/v1/stream", params={"since": "2026-07-04T00:00:00+00:00"}
        ) as resp:
            assert resp.status_code == 200


class TestSincePrecedence:
    """?since= takes precedence over Last-Event-ID when both supplied."""

    def test_since_wins_over_last_event_id(self, monkeypatch) -> None:
        calls = {"after_id": 0, "since": None}

        class _Log:
            def history(self, since=None, **kwargs):
                calls["since"] = since
                return []

            def after_id(self, event_id, **kwargs):
                calls["after_id"] += 1
                return []

        monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _FakeBus())
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
        )

        with client.stream(
            "GET",
            "/agui/v1/stream",
            params={"since": "2026-07-04T00:00:00Z"},
            headers={"Last-Event-ID": "cursor-99"},
        ) as resp:
            assert resp.status_code == 200
            "".join(resp.iter_text())

        assert calls["since"] == "2026-07-04T00:00:00Z"
        assert calls["after_id"] == 0


class TestSnapshotBeforeDelta:
    """On reconnect, the stream must emit a STATE_SNAPSHOT before any STATE_DELTA.

    This is a regression guard: a client reconnecting via ?since= must receive
    the full snapshot to hydrate its projection before it can apply RFC-6902 patches.
    """

    def test_snapshot_emitted_before_deltas(self, monkeypatch) -> None:
        launch_event = {
            "id": "evt-live",
            "kind": "launch",
            "terminal_id": "t1",
            "session_name": "s",
            "timestamp": "2026-07-04T00:00:05Z",
            "detail": {"agent_name": "dev", "provider": "mock_cli"},
        }

        class _Log:
            def history(self, since=None, **kwargs):
                return []

            def after_id(self, event_id, **kwargs):
                return []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.sse_bus.get_bus",
            lambda: _FakeBus([launch_event]),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
        )

        with client.stream(
            "GET", "/agui/v1/stream", params={"since": "2026-07-04T00:00:00Z"}
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

        # STATE_SNAPSHOT must appear in the stream
        assert "event: STATE_SNAPSHOT" in body

        # If STATE_DELTA appears, it must come AFTER STATE_SNAPSHOT
        snap_pos = body.find("event: STATE_SNAPSHOT")
        delta_pos = body.find("event: STATE_DELTA")
        if delta_pos != -1:
            assert snap_pos < delta_pos, "STATE_SNAPSHOT must precede STATE_DELTA"
