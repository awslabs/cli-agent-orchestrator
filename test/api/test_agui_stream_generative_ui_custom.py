"""The ambient stream carries generative UI as ``CUSTOM cao.generative_ui``.

AG-UI 1.0 (``docs/spec/1.0/processing.mdx``): "A producer MUST NOT emit an event
type the protocol does not describe ... ``CUSTOM`` and ``RAW`` exist for that".
``GENERATIVE_UI`` is not in the spec's ``EventType`` enum, so the ambient
``/agui/v1/stream`` must not name a frame with it. These tests pin the wire:
the frame's SSE ``event:`` name and its ``type`` are ``CUSTOM``, both parse as
the spec's ``EventType``, the frame validates as a spec ``CustomEvent``, its
``name`` is ``cao.generative_ui`` (the name the run plane already uses), and its
``value`` is the same ``{component, props, ...}`` mapping the run plane carries.
An off-list component is still refused as ``RAW`` with ``rejected_component``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui_stream import to_agui_event

ag_ui_core = pytest.importorskip("ag_ui.core")
EventType = ag_ui_core.EventType
CustomEvent = ag_ui_core.CustomEvent

client = TestClient(app, base_url="http://localhost")

_UI_EVENT = {
    "id": "evt-ui",
    "kind": "other",
    "terminal_id": "t-1",
    "session_name": "s",
    "timestamp": "2026-07-04T00:00:00Z",
    "detail": {
        "event_type": "post_agent_ui",
        "ui": {"component": "approval_card", "props": {"title": "Approve?", "risk": "high"}},
    },
}

_OFF_LIST_EVENT = {
    "id": "evt-off",
    "kind": "other",
    "terminal_id": "t-1",
    "session_name": "s",
    "timestamp": "2026-07-04T00:00:01Z",
    "ui": {"component": "iframe", "props": {"src": "http://evil"}},
}


class _FakeBus:
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
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)


def _stream_frames(monkeypatch, events) -> List[Tuple[str, Dict[str, Any]]]:
    """Drive ``/agui/v1/stream`` with *events* on the bus; return ``(event, data)`` frames."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.sse_bus.get_bus",
        lambda: _FakeBus(events),
    )
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames: List[Tuple[str, Dict[str, Any]]] = []
    for block in body.split("\n\n"):
        name = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None and data is not None:
            frames.append((name, data))
    return frames


def _assert_generative_ui_custom(name: str, data: Dict[str, Any]) -> None:
    assert name == "CUSTOM"
    assert EventType(name) is EventType.CUSTOM
    assert data["type"] == "CUSTOM"
    assert data["name"] == "cao.generative_ui"
    # The frame body is a valid spec CustomEvent, not just a lookalike.
    evt = CustomEvent.model_validate(data)
    assert evt.type is EventType.CUSTOM
    value = data["value"]
    assert value["component"] == "approval_card"
    assert value["props"] == {"title": "Approve?", "risk": "high"}
    assert value["event_id"] == "evt-ui"
    assert value["terminal_id"] == "t-1"


def test_mapping_emits_custom_cao_generative_ui():
    agui_type, data = to_agui_event(_UI_EVENT)
    _assert_generative_ui_custom(agui_type, data)


def test_ambient_wire_frame_is_custom_cao_generative_ui(monkeypatch):
    frames = _stream_frames(monkeypatch, [_UI_EVENT])
    names = [n for n, _ in frames]
    # Every frame on the ambient stream is named by a spec event type.
    for n in names:
        EventType(n)
    ui = [(n, d) for n, d in frames if d.get("name") == "cao.generative_ui"]
    assert len(ui) == 1, names
    _assert_generative_ui_custom(*ui[0])


def test_ambient_wire_off_list_component_is_still_refused_as_raw(monkeypatch):
    frames = _stream_frames(monkeypatch, [_OFF_LIST_EVENT])
    raw = [d for n, d in frames if n == "RAW"]
    assert len(raw) == 1
    assert raw[0]["rejected_component"] == "iframe"
    assert raw[0]["cao_kind"] == "generative_ui"
    assert not [d for _, d in frames if d.get("name") == "cao.generative_ui"]


@pytest.mark.asyncio
async def test_run_plane_carries_the_same_name_and_value_shape():
    """The run plane's CUSTOM frame keeps ``value = {component, props, ...}``."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    async def _bus():
        yield _UI_EVENT

    frames = []
    async for frame in run_plane_stream(
        input_data={
            "threadId": "th",
            "runId": "r",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        bus_subscribe_fn=_bus,
    ):
        frames.append(frame)
    parsed = [
        json.loads(line[len("data: ") :])
        for f in frames
        for line in f.strip().split("\n")
        if line.startswith("data: ")
    ]
    ui = [p for p in parsed if p.get("type") == "CUSTOM" and p.get("name") == "cao.generative_ui"]
    assert len(ui) == 1, [p.get("type") for p in parsed]
    value = ui[0]["value"]
    assert value["component"] == "approval_card"
    assert value["props"] == {"title": "Approve?", "risk": "high"}
    assert "name" not in value and "value" not in value
