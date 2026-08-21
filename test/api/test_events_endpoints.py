"""Integration tests for the FastAPI event endpoints.

Covers ``/events/history`` returning six-primitive events, ``/events`` advertising
the ``text/event-stream`` media type, and the drop-on-slow guarantee that a full
subscriber queue never blocks the producer.
"""

import asyncio

import pytest

from cli_agent_orchestrator.services import sse_bus as sse_bus_module
from cli_agent_orchestrator.services.event_log_service import get_event_log
from cli_agent_orchestrator.services.sse_bus import get_bus


@pytest.fixture(autouse=True)
def _enable_mcp_apps(monkeypatch):
    """The event endpoints are part of the default-off MCP Apps surface.

    Enable the flag for the happy-path tests in this module; the default-off
    behavior (404 when unset) is asserted explicitly by the ``*_when_disabled``
    tests, which clear it again.
    """

    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")


@pytest.mark.integration
def test_events_history_404_when_disabled(client, monkeypatch) -> None:
    """/events/history is not reachable unless the MCP Apps surface is enabled."""

    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    assert client.get("/events/history").status_code == 404


@pytest.mark.integration
def test_events_stream_404_when_disabled(client, monkeypatch) -> None:
    """/events is not reachable unless the MCP Apps surface is enabled."""

    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    assert client.get("/events").status_code == 404


@pytest.mark.integration
def test_events_history_returns_six_primitive_events(client) -> None:
    """/events/history replays normalized events from the ring buffer."""

    log = get_event_log()
    ev = log.append("launch", "abcd1234", "cao-foo", {"event_type": "post_create_terminal"})

    response = client.get("/events/history", params={"limit": 500})
    assert response.status_code == 200
    events = response.json()["events"]
    assert any(e["id"] == ev["id"] and e["kind"] == "launch" for e in events)
    # every replayed event speaks the closed vocabulary
    valid = {"launch", "handoff", "a2a_delegation", "file_mod", "completion", "error", "other"}
    assert all(e["kind"] in valid for e in events)


@pytest.mark.integration
def test_events_history_kinds_filter(client) -> None:
    """The kinds filter narrows the replayed events."""

    log = get_event_log()
    log.append("launch", "aaaa1111", "cao-x", {"event_type": "post_create_terminal"})
    log.append("completion", "aaaa1111", "cao-x", {"event_type": "post_kill_terminal"})

    response = client.get("/events/history", params={"kinds": "completion"})
    assert response.status_code == 200
    assert all(e["kind"] == "completion" for e in response.json()["events"])


@pytest.mark.integration
def test_events_stream_advertises_event_stream_media_type() -> None:
    """The /events endpoint returns a text/event-stream StreamingResponse."""

    from cli_agent_orchestrator.api.main import events_stream

    async def _call():
        return await events_stream()

    response = asyncio.run(_call())
    assert response.media_type == "text/event-stream"


@pytest.mark.integration
def test_full_subscriber_queue_does_not_block_producer() -> None:
    """A full subscriber queue drops events while publish returns immediately."""

    async def _run():
        bus = get_bus()
        gen = bus.subscribe()
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)  # register the subscriber queue

        # Overfill far past capacity; none of these should raise or block.
        for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE + 100):
            bus.publish({"id": str(i), "kind": "launch"})

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass

    asyncio.run(_run())


@pytest.mark.integration
def test_events_history_rejects_invalid_kind(client) -> None:
    """An unknown kind is rejected with 400 rather than silently matching nothing."""

    resp = client.get("/events/history", params={"kinds": "bogus"})
    assert resp.status_code == 400


@pytest.mark.integration
def test_events_history_accepts_mixed_valid_kinds(client) -> None:
    """A comma-separated list of valid kinds passes validation."""

    resp = client.get("/events/history", params={"kinds": "launch,completion"})
    assert resp.status_code == 200


@pytest.mark.integration
def test_events_history_rejects_one_invalid_among_valid(client) -> None:
    """A single bad token anywhere in the list still fails closed."""

    resp = client.get("/events/history", params={"kinds": "launch,bogus"})
    assert resp.status_code == 400


@pytest.mark.integration
def test_events_history_clamps_limit_over_capacity(client) -> None:
    """``limit`` above the ring capacity is rejected by the Query bound (422)."""

    from cli_agent_orchestrator.services.event_log_service import RING_CAPACITY

    resp = client.get("/events/history", params={"limit": RING_CAPACITY + 1})
    assert resp.status_code == 422


@pytest.mark.integration
def test_events_history_rejects_negative_limit(client) -> None:
    """A negative ``limit`` is rejected by the Query lower bound (422)."""

    resp = client.get("/events/history", params={"limit": -1})
    assert resp.status_code == 422
