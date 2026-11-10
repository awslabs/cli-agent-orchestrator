"""Every run-plane event validates against the AG-UI 1.0 JSON Schema (A-M13).

AG-UI 1.0 ``architecture.mdx`` (Design Principles, "Tolerant reader, strict
writer"): "A producer MUST emit only what the schema defines". The schema closes
every event (``unevaluatedProperties: false``), while the SDK models are
``extra="allow"``, so a ``thread_id=`` or ``run_id=`` passed to an event that
does not define them reaches the wire as an undescribed key. Only
``RUN_STARTED`` and ``RUN_FINISHED`` carry ``threadId``/``runId``.

Each frame is validated with a Draft 2020-12 validator against the vendored
``spec/1.0/schema.json`` (``#/$defs/Event``), not with the SDK models, because
``extra="allow"`` would accept the very keys under test.

``_translate_live_frame`` is reached only when the run plane is given a
``bus_subscribe_fn``; ``POST /agui/v1/run`` passes none today, so those tests
cover the function, not the wire.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("ag_ui")

from test.services.agui.agui_schema import schema_errors  # noqa: E402

from ag_ui.encoder import EventEncoder  # noqa: E402

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter  # noqa: E402
from cli_agent_orchestrator.services.agui.handoff_approval import (  # noqa: E402
    AgentHandoffWithApproval,
)
from cli_agent_orchestrator.services.agui.run_plane import (  # noqa: E402
    _translate_live_frame,
    run_plane_stream,
)

_KIRO_PROMPT = ("kiro_cli", "Allow this action? [y/n/t]:")
_SELECTOR_PROMPT = ("claude_code", "\u2191/\u2193 to navigate")


def _data(frames: List[str]) -> List[Dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for frame in frames
        for line in frame.strip().split("\n")
        if line.startswith("data: ")
    ]


def _input(resume: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "threadId": "thread-m13",
        "runId": "run-m13",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        body["resume"] = resume
    return body


async def _stream(construct: Any, snapshot: bool = True, **kw: Any) -> List[Dict[str, Any]]:
    frames = [
        f
        async for f in run_plane_stream(
            input_data=_input(**kw),
            approval_construct=construct,
            snapshot_fn=(lambda: {"sessions": [], "terminals": []}) if snapshot else None,
        )
    ]
    return _data(frames)


class _RecordingDelivery:
    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        pass

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        return True


class _FailingDelivery:
    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        raise RuntimeError("backend down")

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        raise RuntimeError("backend down")


def _construct(delivery: Any = None) -> AgentHandoffWithApproval:
    return AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)


def _raise(construct: AgentHandoffWithApproval, prompt: tuple = _KIRO_PROMPT) -> Any:
    provider, raw = prompt
    return construct.on_provider_waiting(
        terminal_id="t-m13", provider=provider, raw_prompt=raw, session_name="s-m13"
    )


@pytest.mark.asyncio
async def test_interrupt_run_emits_only_described_fields():
    construct = _construct()
    _raise(construct)
    events = await _stream(construct)
    assert [e["type"] for e in events] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    for event in events:
        assert schema_errors(event) == [], event


@pytest.mark.asyncio
async def test_success_run_emits_only_described_fields():
    events = await _stream(_construct())
    assert [e["type"] for e in events] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    for event in events:
        assert schema_errors(event) == [], event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt, delivery, payload",
    [
        pytest.param(_KIRO_PROMPT, _RecordingDelivery, {}, id="ambiguous"),
        pytest.param(_SELECTOR_PROMPT, _RecordingDelivery, {"editedArgs": "x"}, id="invalid"),
        pytest.param(_KIRO_PROMPT, _FailingDelivery, {"approved": True}, id="delivery"),
    ],
)
async def test_run_error_exits_emit_only_described_fields(prompt, delivery, payload):
    construct = _construct(delivery())
    pending = _raise(construct, prompt)
    events = await _stream(
        construct,
        resume=[{"interruptId": pending.id, "status": "resolved", "payload": payload}],
    )
    assert events[-1]["type"] == "RUN_ERROR", events
    for event in events:
        assert schema_errors(event) == [], event


@pytest.mark.parametrize(
    "agui_type, data",
    [
        ("STATE_SNAPSHOT", {"snapshot": {"sessions": []}}),
        ("STATE_DELTA", {"delta": [{"op": "add", "path": "/x", "value": 1}]}),
        ("STEP_STARTED", {"step_id": "t1", "step_name": "worker"}),
        ("STEP_FINISHED", {"step_id": "t1", "step_name": "worker"}),
        ("TOOL_CALL_START", {"tool_call_id": "c1", "tool_call_name": "handoff"}),
        ("TOOL_CALL_END", {"tool_call_id": "c1"}),
        ("CUSTOM", {"name": "cao.generative_ui", "value": {"component": "metric"}}),
        ("TEXT_MESSAGE_CONTENT", {"message_id": "m1", "delta": ""}),
        ("RAW", {"event": {"kind": "other"}}),
        ("RUN_ERROR", {"message": "boom"}),
        ("SOMETHING_ELSE", {"x": 1}),
    ],
)
def test_live_frames_emit_only_described_fields(agui_type, data):
    encoded = _translate_live_frame(agui_type, data, "thread-m13", "run-m13", EventEncoder())
    assert encoded is not None
    (event,) = _data([encoded])
    assert schema_errors(event) == [], event


@pytest.mark.parametrize("agui_type", ["STEP_STARTED", "STEP_FINISHED"])
def test_step_frames_carry_the_terminal_in_metadata(agui_type):
    """The terminal id moves from an undescribed ``stepId`` to ``metadata``."""
    encoded = _translate_live_frame(
        agui_type, {"step_id": "t1", "step_name": "worker"}, "th", "r", EventEncoder()
    )
    (event,) = _data([encoded])
    # One nested ``cao`` object, the shape the ambient stream uses for CAO fields.
    assert event["metadata"] == {"cao": {"terminalId": "t1"}}
    assert "stepId" not in event


def test_step_frame_without_a_terminal_has_no_metadata():
    encoded = _translate_live_frame(
        "STEP_STARTED", {"step_name": "worker"}, "th", "r", EventEncoder()
    )
    (event,) = _data([encoded])
    assert "metadata" not in event


# ---------------------------------------------------------------------------
# W5 (A-O4): the interrupt's expiry reaches the wire as ``expiresAt``
# ---------------------------------------------------------------------------


def _interrupts(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    finished = events[-1]
    assert finished["type"] == "RUN_FINISHED" and finished["outcome"]["type"] == "interrupt"
    return finished["outcome"]["interrupts"]


@pytest.mark.asyncio
async def test_interrupt_carries_expires_at_when_cao_knows_it():
    """``Interrupt.expiresAt`` (ISO 8601 by convention) mirrors CAO's ``expires_at``."""
    construct = _construct()
    pending = _raise(construct)
    pending.expires_at = "2026-11-04T18:00:00+00:00"
    (intr,) = _interrupts(await _stream(construct))
    assert intr["expiresAt"] == "2026-11-04T18:00:00+00:00"


@pytest.mark.asyncio
async def test_interrupt_omits_expires_at_when_unknown():
    """Absent means absent: no deadline, no key (never ``null``)."""
    construct = _construct()
    _raise(construct)
    (intr,) = _interrupts(await _stream(construct))
    assert "expiresAt" not in intr


def test_the_schema_check_rejects_an_undescribed_event_key():
    """Guard for the guard: the old ``threadId`` on RUN_ERROR fails the schema."""
    assert schema_errors({"type": "RUN_ERROR", "message": "x", "threadId": "t"})
    assert schema_errors({"type": "STEP_STARTED", "stepName": "s", "stepId": "t1"})
    assert schema_errors({"type": "RUN_ERROR", "message": "x"}) == []


# ---------------------------------------------------------------------------
# NB-4: the live-projection path (function-level; not reachable from the wire)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_follows_a_live_run_error():
    """lifecycle.mdx: "A producer MUST NOT emit anything after RUN_ERROR except
    RUN_STARTED." A RUN_ERROR from the live projection ends the run there."""

    async def bus():
        yield {"id": "e1", "kind": "error", "terminal_id": None, "session_name": "s", "detail": {}}
        yield {
            "id": "e2",
            "kind": "launch",
            "terminal_id": "t9",
            "session_name": "s",
            "detail": {"agent_name": "worker"},
        }

    frames = [
        f
        async for f in run_plane_stream(
            input_data=_input(), bus_subscribe_fn=bus, heartbeat_interval=5.0
        )
    ]
    types = [e["type"] for e in _data(frames)]
    assert types[-1] == "RUN_ERROR", types
    assert "RUN_FINISHED" not in types and "STEP_STARTED" not in types, types


def test_step_without_a_name_gets_a_unique_one():
    """lifecycle.mdx: "A producer MUST NOT open a step whose name is already open".
    The fallback name is per terminal, never one shared constant."""
    names = {
        _data([_translate_live_frame("STEP_STARTED", {"step_id": t}, "th", "r", EventEncoder())])[
            0
        ]["stepName"]
        for t in ("t1", "t2")
    }
    assert len(names) == 2, names


def test_step_finish_without_a_name_closes_the_step_it_opened():
    started = _data(
        [_translate_live_frame("STEP_STARTED", {"step_id": "t1"}, "th", "r", EventEncoder())]
    )
    finished = _data(
        [_translate_live_frame("STEP_FINISHED", {"step_id": "t1"}, "th", "r", EventEncoder())]
    )
    assert started[0]["stepName"] == finished[0]["stepName"]


def test_step_finish_with_nothing_to_match_emits_nothing():
    """lifecycle.mdx: a producer "MUST NOT finish a step that was never opened"."""
    assert _translate_live_frame("STEP_FINISHED", {}, "th", "r", EventEncoder()) is None
