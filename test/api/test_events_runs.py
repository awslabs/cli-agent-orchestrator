"""Tests for the Runs dashboard SSE stream (/events/runs): status + flow frames.

The generator is tested directly: httpx's ASGITransport buffers entire
responses, so an infinite SSE stream cannot be exercised through it. The
EventSourceResponse wrapper (ping/disconnect handling) is sse-starlette's own
tested behavior. The two assertions on the endpoint function cover the
allowlist gate and the media type.
"""

import asyncio
import json

import pytest

from cli_agent_orchestrator.services.event_bus import bus

TERMINAL_ID = "abc12345"
TOPIC = f"terminal.{TERMINAL_ID}.status"


@pytest.mark.asyncio
class TestRunsStatusEventsSSE:
    async def test_yields_status_event_per_publish(self):
        from cli_agent_orchestrator.api.main import _runs_event_stream

        baseline_wildcards = len(bus._wildcard)
        stream = _runs_event_stream()
        try:
            reader = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)  # let the generator subscribe
            assert len(bus._wildcard) == baseline_wildcards + 1
            bus._dispatch(TOPIC, {"status": "processing"})
            event = await asyncio.wait_for(reader, timeout=3.0)
        finally:
            await stream.aclose()

        assert event["event"] == "status"
        assert json.loads(event["data"]) == {"terminal_id": TERMINAL_ID, "status": "processing"}
        # aclose() routes through the generator's finally → no leaked queues.
        assert len(bus._wildcard) == baseline_wildcards

    async def test_endpoint_returns_event_stream_response(self):
        from starlette.requests import Request as StarletteRequest

        from cli_agent_orchestrator.api.main import runs_events

        request = StarletteRequest(
            {"type": "http", "method": "GET", "headers": [], "client": ("127.0.0.1", 50000)}
        )
        resp = await runs_events(request)
        assert resp.media_type == "text/event-stream"
        # Tear down the generator the response wraps so no subscription leaks.
        await resp.body_iterator.aclose()

    async def test_endpoint_rejects_non_allowlisted_client(self):
        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        from cli_agent_orchestrator.api.main import runs_events

        request = StarletteRequest(
            {"type": "http", "method": "GET", "headers": [], "client": ("10.9.8.7", 50000)}
        )
        with pytest.raises(HTTPException) as exc:
            await runs_events(request)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
class TestRunsFlowEventsSSE:
    async def test_flow_frames_relayed(self):
        """flow.message publishes surface as 'flow' SSE frames for the graph."""
        from cli_agent_orchestrator.api.main import _runs_event_stream

        stream = _runs_event_stream()
        try:
            reader = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)  # let the generator subscribe
            bus._dispatch(
                "flow.message",
                {"sender_id": "aaaa1111", "receiver_id": "bbbb2222", "kind": "handoff"},
            )
            event = await asyncio.wait_for(reader, timeout=3.0)
        finally:
            await stream.aclose()

        assert event["event"] == "flow"
        assert json.loads(event["data"]) == {
            "sender_id": "aaaa1111",
            "receiver_id": "bbbb2222",
            "kind": "handoff",
        }

    async def test_status_frames_still_relayed_through_multiplexer(self):
        from cli_agent_orchestrator.api.main import _runs_event_stream

        stream = _runs_event_stream()
        try:
            reader = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)
            bus._dispatch(TOPIC, {"status": "processing"})
            event = await asyncio.wait_for(reader, timeout=3.0)
        finally:
            await stream.aclose()

        assert event["event"] == "status"
        assert json.loads(event["data"]) == {"terminal_id": TERMINAL_ID, "status": "processing"}

    async def test_multiplexer_cleans_up_both_subscriptions(self):
        from cli_agent_orchestrator.api.main import _runs_event_stream

        wildcard_before = len(bus._wildcard)
        exact_before = len(bus._exact.get("flow.message", []))
        stream = _runs_event_stream()
        reader = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        await stream.aclose()
        await asyncio.sleep(0.05)
        assert len(bus._wildcard) == wildcard_before
        assert len(bus._exact.get("flow.message", [])) == exact_before
