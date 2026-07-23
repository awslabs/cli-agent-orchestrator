"""F-SL1: default-ON single-flag end-to-end smoke.

#436's only human Must-Fix was flag-gating drift — the route answered under
CAO_AGUI_ENABLED while the lifecycle publisher gated on a different flag, so the
documented quickstart streamed nothing. These tests enable ONLY
``CAO_AGUI_ENABLED``, drive a REAL lifecycle hook, and assert the frame travels
the full path (hook -> publisher gate -> event log -> /agui/v1/stream SSE) AND
is projected by the run plane. The live-tail bus is stubbed to terminate so a
regression fails fast instead of hanging.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def _only_agui_flag(monkeypatch):
    """Enable ONLY the documented CAO_AGUI_ENABLED flag (never MCP Apps)."""
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")


class _EmptyBus:
    """Bus whose drain terminates immediately so the stream ends after replay.

    ``publish`` is a no-op: the publisher appends to the (real) event log first,
    which is what the stream's history replay reads, so dropping live fan-out
    does not affect the wire assertion.
    """

    def publish(self, event):
        pass

    def register(self, overflow_close=False):
        return object()

    def unregister(self, queue):
        pass

    async def drain(self, queue):
        return
        yield  # pragma: no cover


def _emit_real_terminal_event(terminal_id: str) -> None:
    from cli_agent_orchestrator.plugins.builtin.event_log_publisher import EventLogPublisher
    from cli_agent_orchestrator.plugins.events import PostCreateTerminalEvent

    pub = EventLogPublisher()
    asyncio.run(
        pub.on_post_create_terminal(
            PostCreateTerminalEvent(
                terminal_id=terminal_id,
                agent_name="developer",
                provider="mock_cli",
                session_id="cao-smoke",
            )
        )
    )


def test_single_flag_drives_lifecycle_event_onto_the_stream(monkeypatch) -> None:
    # Terminate the live tail so the stream ends after the real-log replay.
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _EmptyBus())
    tid = "term-" + uuid.uuid4().hex[:8]
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    # Drive a REAL lifecycle hook (exercises the publisher's single-flag gate
    # and the real event log).
    _emit_real_terminal_event(tid)

    frames = []
    with client.stream("GET", "/agui/v1/stream", params={"since": since}) as resp:
        assert resp.status_code == 200  # surface up under CAO_AGUI_ENABLED alone
        for line in resp.iter_lines():
            frames.append(line)

    joined = "\n".join(frames)
    assert tid in joined, f"lifecycle event did not reach the stream: {frames!r}"


def test_publisher_noops_without_any_flag(monkeypatch) -> None:
    """Anti-regression for the reverse: with no flag the observer is silent."""
    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    from cli_agent_orchestrator.services.event_log_service import get_event_log

    tid = "term-" + uuid.uuid4().hex[:8]
    _emit_real_terminal_event(tid)
    assert not any(r.get("terminal_id") == tid for r in get_event_log().history())


@pytest.mark.asyncio
async def test_run_plane_projects_the_lifecycle_event(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _EmptyBus())
    from cli_agent_orchestrator.plugins.builtin.event_log_publisher import EventLogPublisher
    from cli_agent_orchestrator.plugins.events import PostCreateTerminalEvent
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream
    from cli_agent_orchestrator.services.event_log_service import get_event_log

    tid = "term-" + uuid.uuid4().hex[:8]
    await EventLogPublisher().on_post_create_terminal(
        PostCreateTerminalEvent(
            terminal_id=tid, agent_name="developer", provider="mock_cli", session_id="cao-smoke"
        )
    )
    record = [r for r in get_event_log().history() if r.get("terminal_id") == tid][-1]

    inp = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }

    async def bus():
        yield record

    frames = [
        f
        async for f in run_plane_stream(
            input_data=inp, bus_subscribe_fn=bus, heartbeat_interval=5.0
        )
    ]
    data_frames = [f for f in frames if f.startswith("data:")]
    # RUN_STARTED + projected lifecycle frame + RUN_FINISHED, with the id on the wire.
    assert len(data_frames) >= 3
    assert any(tid in f for f in frames), frames
