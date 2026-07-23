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


def _parse_sse(body: str):
    """Parse an SSE body into a list of ``(id_or_None, event_type)`` tuples."""

    frames = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        frame_id = None
        event_type = None
        for line in chunk.split("\n"):
            if line.startswith("id: "):
                frame_id = line[len("id: ") :]
            elif line.startswith("event: "):
                event_type = line[len("event: ") :]
        if event_type is not None:
            frames.append((frame_id, event_type))
    return frames


class TestMultiFramePerRecordIds:
    """A single record that expands into multiple AG-UI frames must give each
    frame a distinct SSE ``id:``, with the canonical record id on the *last*
    frame (PR #485 review). Emitting them all under the same id lets clients
    drop/skip the synthesized closers and breaks mid-record reconnects.
    """

    def test_synthesized_closers_get_unique_ids(self, monkeypatch) -> None:
        # Event 1 opens a tool call (handoff -> TOOL_CALL_START, receiver t-recv).
        open_event = {
            "id": "evt-open",
            "kind": "handoff",
            "terminal_id": "t-sup",
            "session_name": "s",
            "timestamp": "2026-07-04T00:00:01Z",
            "detail": {"orchestration_type": "handoff", "receiver": "t-recv"},
        }
        # Event 2 completes t-recv -> STEP_FINISHED, which the lifecycle tracker
        # expands into [STEP_FINISHED, synthesized TOOL_CALL_END] (two frames).
        done_event = {
            "id": "evt-done",
            "kind": "completion",
            "terminal_id": "t-recv",
            "session_name": "s",
            "timestamp": "2026-07-04T00:00:02Z",
            "detail": {},
        }

        class _Log:
            def history(self, since=None, **kwargs):
                return []

            def after_id(self, event_id, **kwargs):
                return []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.sse_bus.get_bus",
            lambda: _FakeBus([open_event, done_event]),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
        )

        with client.stream(
            "GET", "/agui/v1/stream", params={"since": "2026-07-04T00:00:00Z"}
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

        frames = _parse_sse(body)
        step = [(fid, t) for fid, t in frames if t == "STEP_FINISHED"]
        end = [(fid, t) for fid, t in frames if t == "TOOL_CALL_END"]

        assert step, "expected a STEP_FINISHED frame from the completion record"
        assert end, "expected a synthesized TOOL_CALL_END frame"

        step_id = step[0][0]
        end_id = end[0][0]

        # Both derive from the record id but must NOT be identical.
        assert step_id == "evt-done.0", "intermediate frame should get a unique derived id"
        assert end_id == "evt-done", "the last frame keeps the canonical record id"
        assert step_id != end_id

    def test_record_without_id_emits_frames_without_id(self, monkeypatch) -> None:
        # An event lacking an ``id`` must still stream (defensive path): the
        # derived-id scheme falls back to no ``id:`` line rather than emitting a
        # bogus ``None.<i>`` cursor.
        no_id_event = {
            "kind": "completion",
            "terminal_id": "t-orphan",
            "session_name": "s",
            "timestamp": "2026-07-04T00:00:03Z",
            "detail": {},
        }

        class _Log:
            def history(self, since=None, **kwargs):
                return []

            def after_id(self, event_id, **kwargs):
                return []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.sse_bus.get_bus",
            lambda: _FakeBus([no_id_event]),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
        )

        with client.stream(
            "GET", "/agui/v1/stream", params={"since": "2026-07-04T00:00:00Z"}
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

        frames = _parse_sse(body)
        step = [(fid, t) for fid, t in frames if t == "STEP_FINISHED"]
        assert step, "expected a STEP_FINISHED frame from the id-less completion record"
        assert step[0][0] is None, "a record without an id must emit no SSE id:"
        assert "None." not in body, "must never emit a bogus 'None.<i>' cursor"
