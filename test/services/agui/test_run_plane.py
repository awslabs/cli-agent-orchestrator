"""Unit tests for the AG-UI run plane (services/agui/run_plane.py).

Covers:
- RunAgentInput parsing from camelCase
- EventEncoder framing (data:-only camelCase with type field)
- Interrupt outcome emission
- Resume round-trip via run plane
- Uncovered/expired interrupt -> RUN_ERROR
- 501 behaviour when ag-ui-protocol is absent (mocked)
- Lifecycle legality (RUN_STARTED first, RUN_FINISHED/ERROR last)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    ApprovalDecision,
    Interrupt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_run_input(
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    resume: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Build a minimal RunAgentInput-shaped camelCase dict."""
    data: Dict[str, Any] = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        data["resume"] = resume
    return data


async def _collect_stream(gen) -> List[str]:
    """Collect all frames from an async generator."""
    frames = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _parse_frames(raw_frames: List[str]) -> List[Dict[str, Any]]:
    """Parse SSE data: frames into JSON dicts."""
    results = []
    for frame in raw_frames:
        # Each frame is "data: {...}\n\n"
        for line in frame.strip().split("\n"):
            if line.startswith("data: "):
                payload = line[len("data: ") :]
                results.append(json.loads(payload))
    return results


# ---------------------------------------------------------------------------
# Tests: basic lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_emits_run_started_first():
    """RUN_STARTED is the first frame emitted."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    input_data = _minimal_run_input()
    frames = await _collect_stream(run_plane_stream(input_data=input_data))
    parsed = _parse_frames(frames)

    assert len(parsed) >= 2
    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[0]["threadId"] == "thread-1"
    assert parsed[0]["runId"] == "run-1"


@pytest.mark.asyncio
async def test_run_plane_emits_run_finished_last():
    """RUN_FINISHED with success outcome is the last frame."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    input_data = _minimal_run_input()
    frames = await _collect_stream(run_plane_stream(input_data=input_data))
    parsed = _parse_frames(frames)

    last = parsed[-1]
    assert last["type"] == "RUN_FINISHED"
    assert last["outcome"]["type"] == "success"


@pytest.mark.asyncio
async def test_run_plane_state_snapshot_after_started():
    """STATE_SNAPSHOT is emitted after RUN_STARTED when snapshot_fn provided."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    snapshot = {"sessions": [], "terminals": [], "counts": {"sessions": 0, "terminals": 0}}

    input_data = _minimal_run_input()
    frames = await _collect_stream(
        run_plane_stream(input_data=input_data, snapshot_fn=lambda: snapshot)
    )
    parsed = _parse_frames(frames)

    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "STATE_SNAPSHOT"
    assert parsed[1]["snapshot"] == snapshot


@pytest.mark.asyncio
async def test_run_plane_no_snapshot_fn():
    """Without snapshot_fn, only RUN_STARTED and RUN_FINISHED are emitted."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    input_data = _minimal_run_input()
    frames = await _collect_stream(run_plane_stream(input_data=input_data, snapshot_fn=None))
    parsed = _parse_frames(frames)

    assert len(parsed) == 2
    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "RUN_FINISHED"


# ---------------------------------------------------------------------------
# Tests: interrupt outcome emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_interrupt_outcome():
    """Open interrupts produce STATE_SNAPSHOT + RUN_FINISHED with interrupt outcome."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    # Create an open interrupt (use pattern that actually matches the classifier)
    construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    snapshot = {"sessions": [], "terminals": []}
    input_data = _minimal_run_input()

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
            snapshot_fn=lambda: snapshot,
        )
    )
    parsed = _parse_frames(frames)

    # RUN_STARTED, STATE_SNAPSHOT, RUN_FINISHED(interrupt)
    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "STATE_SNAPSHOT"
    assert parsed[2]["type"] == "RUN_FINISHED"
    assert parsed[2]["outcome"]["type"] == "interrupt"
    assert len(parsed[2]["outcome"]["interrupts"]) == 1
    intr = parsed[2]["outcome"]["interrupts"][0]
    assert intr["reason"] == "claude-code:permission_request"
    # WS-3 wire shape: response_schema advertises the resume payload contract,
    # and interior metadata is camelCased (terminalId/sessionName).
    assert "approved" in intr["responseSchema"]["properties"]
    assert intr["metadata"]["terminalId"] == "t-1"
    assert intr["metadata"]["sessionName"] == "s-1"


@pytest.mark.asyncio
async def test_run_plane_interrupt_no_snapshot_fn():
    """Open interrupts with no snapshot_fn still emit RUN_FINISHED interrupt."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    construct.on_provider_waiting(
        terminal_id="t-1",
        provider="kiro_cli",
        raw_prompt="Allow this action? [y/n/t]:",
        session_name="s-1",
    )

    input_data = _minimal_run_input()

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
            snapshot_fn=None,
        )
    )
    parsed = _parse_frames(frames)

    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "RUN_FINISHED"
    assert parsed[1]["outcome"]["type"] == "interrupt"


# ---------------------------------------------------------------------------
# Tests: resume round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_resume_approve():
    """resume[] with approved payload resolves the interrupt."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"approved": True},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    # Interrupt should be resolved now
    assert construct.get_interrupt(interrupt.id).resolved
    assert construct.get_interrupt(interrupt.id).outcome == "approve"

    # Stream should end with RUN_FINISHED success (no more open interrupts)
    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[-1]["type"] == "RUN_FINISHED"
    assert parsed[-1]["outcome"]["type"] == "success"


@pytest.mark.asyncio
async def test_run_plane_resume_deny():
    """resume[] with approved=false maps to deny."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"approved": False},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert construct.get_interrupt(interrupt.id).resolved
    assert construct.get_interrupt(interrupt.id).outcome == "deny"
    assert parsed[-1]["type"] == "RUN_FINISHED"
    assert parsed[-1]["outcome"]["type"] == "success"


@pytest.mark.asyncio
async def test_run_plane_resume_edit():
    """resume[] with editedArgs maps to edit decision."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    # Use a prompt that results in options including "edit"
    # (permission_request includes ["approve", "deny", "edit"])
    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"editedArgs": "modified command"},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert construct.get_interrupt(interrupt.id).resolved
    assert construct.get_interrupt(interrupt.id).outcome == "edit"
    assert parsed[-1]["type"] == "RUN_FINISHED"
    assert parsed[-1]["outcome"]["type"] == "success"


@pytest.mark.asyncio
async def test_run_plane_resume_cancelled_maps_to_deny():
    """resume[] with status 'cancelled' maps to deny decision."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "cancelled",
                "payload": {},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert construct.get_interrupt(interrupt.id).resolved
    assert construct.get_interrupt(interrupt.id).outcome == "deny"


# ---------------------------------------------------------------------------
# Tests: resume error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_resume_unknown_interrupt():
    """resume[] referencing unknown interrupt produces RUN_ERROR."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": "nonexistent-id",
                "status": "resolved",
                "payload": {"approved": True},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "RUN_ERROR"
    assert "nonexistent-id" in parsed[1]["message"]


@pytest.mark.asyncio
async def test_run_plane_resume_already_resolved_is_idempotent():
    """resume[] on already-resolved interrupt is idempotent (no error)."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    # Resolve it first
    await construct.resume(interrupt.id, ApprovalDecision.APPROVE)

    # Now try to resume it again through run_plane
    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"approved": True},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    # Should succeed (idempotent)
    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[-1]["type"] == "RUN_FINISHED"
    assert parsed[-1]["outcome"]["type"] == "success"


# ---------------------------------------------------------------------------
# Tests: live projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_live_projection():
    """Live events from bus are translated to stock AG-UI frames."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    # Simulate bus events
    live_events = [
        {
            "id": "evt-1",
            "kind": "launch",
            "terminal_id": "t-1",
            "session_name": "s-1",
            "timestamp": "2026-07-04T00:00:00Z",
            "detail": {
                "event_type": "post_create_terminal",
                "agent_name": "worker",
                "provider": "claude_code",
            },
        },
        {
            "id": "evt-2",
            "kind": "completion",
            "terminal_id": "t-1",
            "session_name": "s-1",
            "timestamp": "2026-07-04T00:01:00Z",
            "detail": {"event_type": "post_kill_terminal", "agent_name": "worker"},
        },
    ]

    async def _bus_events():
        for event in live_events:
            yield event

    input_data = _minimal_run_input()
    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            bus_subscribe_fn=_bus_events,
        )
    )
    parsed = _parse_frames(frames)

    # RUN_STARTED, STEP_STARTED, STEP_FINISHED, RUN_FINISHED
    types = [p["type"] for p in parsed]
    assert types[0] == "RUN_STARTED"
    assert "STEP_STARTED" in types
    assert "STEP_FINISHED" in types
    assert types[-1] == "RUN_FINISHED"


# ---------------------------------------------------------------------------
# Tests: encoder framing verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encoder_produces_data_only_camel_case():
    """Every frame is data:-only (no event: line) with camelCase keys."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    input_data = _minimal_run_input()
    frames = await _collect_stream(run_plane_stream(input_data=input_data))

    for frame in frames:
        # Must start with "data: "
        assert frame.startswith("data: "), f"Frame does not start with 'data: ': {frame!r}"
        # Must end with \n\n
        assert frame.endswith("\n\n"), f"Frame does not end with \\n\\n: {frame!r}"
        # Parse the JSON
        payload = json.loads(frame[len("data: ") : -2])
        # Must have a "type" field
        assert "type" in payload, f"Frame missing 'type' field: {payload}"
        # threadId and runId should be camelCase where present
        if "threadId" in payload:
            assert "thread_id" not in payload or payload.get("thread_id") is None


# ---------------------------------------------------------------------------
# Tests: 501 without extra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_501_without_extra():
    """When AG_UI_AVAILABLE is False, stream yields an error frame."""
    import cli_agent_orchestrator.services.agui.run_plane as run_plane_mod

    original = run_plane_mod.AG_UI_AVAILABLE
    try:
        run_plane_mod.AG_UI_AVAILABLE = False

        input_data = _minimal_run_input()
        frames = await _collect_stream(run_plane_mod.run_plane_stream(input_data=input_data))
        parsed = _parse_frames(frames)
        assert len(parsed) == 1
        assert "ag-ui-protocol not installed" in parsed[0]["error"]
    finally:
        run_plane_mod.AG_UI_AVAILABLE = original


# ---------------------------------------------------------------------------
# Tests: payload mapping helpers
# ---------------------------------------------------------------------------


def test_map_resume_payload_approved():
    from cli_agent_orchestrator.services.agui.run_plane import _map_resume_payload

    assert _map_resume_payload({"approved": True}) == "approve"
    assert _map_resume_payload({"approved": False}) == "deny"
    assert _map_resume_payload({"editedArgs": "hello"}) == "edit"
    assert _map_resume_payload({"edited_args": "hello"}) == "edit"
    # Ambiguous payloads now return None instead of defaulting to "approve"
    assert _map_resume_payload({}) is None
    assert _map_resume_payload(None) is None
    assert _map_resume_payload("string") is None
    # Non-boolean 'approved' values are ambiguous
    assert _map_resume_payload({"approved": "yes"}) is None
    assert _map_resume_payload({"approved": 1}) is None
    assert _map_resume_payload({"status": "resolved"}) is None


def test_extract_edited_text():
    from cli_agent_orchestrator.services.agui.run_plane import _extract_edited_text

    assert _extract_edited_text({"editedArgs": "hello"}) == "hello"
    assert _extract_edited_text({"edited_args": "world"}) == "world"
    assert _extract_edited_text({"editedArgs": {"key": "val"}}) == '{"key": "val"}'
    assert _extract_edited_text({}) is None
    assert _extract_edited_text(None) is None


# ---------------------------------------------------------------------------
# Tests: ambiguous payload -> RUN_ERROR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_resume_empty_payload_produces_error():
    """resume[] with empty dict payload (no explicit approved) produces RUN_ERROR."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "RUN_ERROR"
    assert "Ambiguous resume payload" in parsed[1]["message"]
    # Interrupt should NOT have been resolved
    assert not construct.get_interrupt(interrupt.id).resolved


@pytest.mark.asyncio
async def test_run_plane_resume_non_dict_payload_produces_error():
    """resume[] with non-dict payload (e.g. None or string) produces RUN_ERROR."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": None,
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "RUN_ERROR"
    assert "Ambiguous resume payload" in parsed[1]["message"]
    assert not construct.get_interrupt(interrupt.id).resolved


@pytest.mark.asyncio
async def test_run_plane_resume_non_boolean_approved_produces_error():
    """resume[] with non-boolean 'approved' (e.g. string 'yes') produces RUN_ERROR."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"approved": "yes"},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    assert parsed[0]["type"] == "RUN_STARTED"
    assert parsed[1]["type"] == "RUN_ERROR"
    assert "Ambiguous resume payload" in parsed[1]["message"]
    assert not construct.get_interrupt(interrupt.id).resolved


@pytest.mark.asyncio
async def test_run_plane_resume_cancelled_status_still_works_with_empty_payload():
    """resume[] with status='cancelled' overrides to deny even with empty payload."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    input_data = _minimal_run_input(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "cancelled",
                "payload": {},
            }
        ]
    )

    frames = await _collect_stream(
        run_plane_stream(
            input_data=input_data,
            approval_construct=construct,
        )
    )
    parsed = _parse_frames(frames)

    # Cancelled status overrides to deny regardless of payload mapping
    assert construct.get_interrupt(interrupt.id).resolved
    assert construct.get_interrupt(interrupt.id).outcome == "deny"
    assert parsed[-1]["type"] == "RUN_FINISHED"
    assert parsed[-1]["outcome"]["type"] == "success"


# ---------------------------------------------------------------------------
# Item 4 — Accept negotiation / get_content_type
# ---------------------------------------------------------------------------


class TestAcceptNegotiation:
    """Tests for the accept parameter and content-type negotiation."""

    @pytest.mark.asyncio
    async def test_accept_text_event_stream_unchanged(self):
        """Accept: text/event-stream produces the same frames as today."""
        from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

        input_data = _minimal_run_input()
        frames_with_accept = await _collect_stream(
            run_plane_stream(input_data=input_data, accept="text/event-stream")
        )
        frames_without = await _collect_stream(run_plane_stream(input_data=input_data, accept=None))
        # Same frames content (both are data: lines)
        assert len(frames_with_accept) == len(frames_without)
        for a, b in zip(frames_with_accept, frames_without):
            assert a.startswith("data: ") == b.startswith("data: ")

    @pytest.mark.asyncio
    async def test_absent_accept_identical_to_today(self):
        """Absent Accept header (None) produces standard SSE frames."""
        from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

        input_data = _minimal_run_input()
        frames = await _collect_stream(run_plane_stream(input_data=input_data, accept=None))
        assert len(frames) >= 2
        # All meaningful frames are data: lines
        data_frames = [f for f in frames if f.startswith("data: ")]
        assert len(data_frames) >= 2

    def test_get_content_type_with_sse_accept(self):
        """get_run_plane_content_type returns text/event-stream for SSE accept."""
        from cli_agent_orchestrator.services.agui.run_plane import get_run_plane_content_type

        assert get_run_plane_content_type("text/event-stream") == "text/event-stream"

    def test_get_content_type_with_none(self):
        """get_run_plane_content_type returns text/event-stream for None."""
        from cli_agent_orchestrator.services.agui.run_plane import get_run_plane_content_type

        assert get_run_plane_content_type(None) == "text/event-stream"

    def test_get_content_type_fallback(self):
        """get_run_plane_content_type always returns a valid content type."""
        from cli_agent_orchestrator.services.agui.run_plane import get_run_plane_content_type

        result = get_run_plane_content_type("application/json")
        assert result  # non-empty
        assert "text/event-stream" in result  # SDK currently defaults to SSE

    def test_get_content_type_without_agui_extra(self):
        """get_run_plane_content_type returns text/event-stream when AG_UI_AVAILABLE is False."""
        import cli_agent_orchestrator.services.agui.run_plane as run_plane_mod

        original = run_plane_mod.AG_UI_AVAILABLE
        try:
            run_plane_mod.AG_UI_AVAILABLE = False
            assert run_plane_mod.get_run_plane_content_type() == "text/event-stream"
            assert (
                run_plane_mod.get_run_plane_content_type("application/json") == "text/event-stream"
            )
        finally:
            run_plane_mod.AG_UI_AVAILABLE = original
