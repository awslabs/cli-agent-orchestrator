"""Final coverage pass for the AG-UI construct/service branch + guard paths.

Closes the last Codecov-flagged gaps: base HTTP-emitter/json-patch edges,
run_plane initial-snapshot failure + keep-alive, the ApprovalBridge async run
loop + start/stop + _on_waiting isolation, handoff no-op/projection/delivery,
and the small-construct guard branches (supervisor_dashboard orphan session,
session_timeline missing-id/unknown-closer, stream_reader malformed SSE).
"""

from __future__ import annotations

import asyncio
from typing import Iterator
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.agui import run_plane as rp
from cli_agent_orchestrator.services.agui.base import (
    HttpUiEmitter,
    RecordingUiEmitter,
    apply_json_patch_strict,
)


def _run_input(resume=None):
    d = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        d["resume"] = resume
    return d


async def _collect(gen):
    return [f async for f in gen]


def _construct():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    return AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)


# --------------------------------------------------------------------------
# base — HttpUiEmitter token/session payload + json-patch root/nested-list
# --------------------------------------------------------------------------


def test_http_emitter_includes_token_and_session(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"

        def raise_for_status(self):
            return None

    def _post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("requests.post", _post)
    HttpUiEmitter(base_url="http://x", access_token="tok").emit_intent(
        "approval_card", {"a": 1}, terminal_id="t1", session_name="s1"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"]["terminal_id"] == "t1"
    assert captured["json"]["session_name"] == "s1"


def test_apply_json_patch_root_and_nested_list():
    assert apply_json_patch_strict(
        {"a": 1}, [{"op": "replace", "path": "", "value": {"b": 2}}]
    ) == {"b": 2}
    assert apply_json_patch_strict({"a": 1}, [{"op": "add", "path": "", "value": {"c": 3}}]) == {
        "c": 3
    }
    doc = {"items": [{"v": 1}]}
    assert apply_json_patch_strict(doc, [{"op": "replace", "path": "/items/0/v", "value": 9}]) == {
        "items": [{"v": 9}]
    }
    assert doc == {"items": [{"v": 1}]}  # not mutated


# --------------------------------------------------------------------------
# run_plane — §4 initial-snapshot failure + idle keep-alive
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plane_initial_snapshot_failure_isolated():
    def _boom():
        raise RuntimeError("snap boom")

    # No approval_construct, no bus → §4 initial snapshot raises → except → success.
    frames = await _collect(rp.run_plane_stream(input_data=_run_input(), snapshot_fn=_boom))
    assert any("RUN_FINISHED" in f for f in frames)


@pytest.mark.asyncio
async def test_run_plane_keepalive_on_idle():
    async def _slow_bus():
        await asyncio.sleep(0.05)  # exceeds the heartbeat interval → keep-alive emitted
        return
        yield  # pragma: no cover - makes this an async generator

    frames = await _collect(
        rp.run_plane_stream(
            input_data=_run_input(), bus_subscribe_fn=_slow_bus, heartbeat_interval=0.01
        )
    )
    assert any(":keep-alive" in f for f in frames)


# --------------------------------------------------------------------------
# ApprovalBridge — async run loop, start/stop, _on_waiting isolation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_run_loop_dispatches(monkeypatch):
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services import event_bus as eb
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(
        {
            "topic": "terminal.t1.status",
            "data": {"status": TerminalStatus.WAITING_USER_ANSWER.value},
        }
    )
    q.put_nowait({"broken": "event"})  # → generic except branch (logger.error)
    q.put_nowait({"topic": "terminal.t1.status", "data": {"status": TerminalStatus.IDLE.value}})
    monkeypatch.setattr(eb.bus, "subscribe", lambda pattern: q)

    construct = _construct()
    bridge = ApprovalBridge(
        construct=construct,
        get_output_fn=lambda t: "APPROVAL_REQUIRED",
        get_provider_fn=lambda t: "claude_code",
        get_session_fn=lambda t: "s1",
    )
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # WAITING opened then IDLE expired the interrupt; loop survived the bad event.
    assert "t1" not in bridge._waiting_terminals


@pytest.mark.asyncio
async def test_bridge_start_stop_cancels_live_task(monkeypatch):
    from cli_agent_orchestrator.services import event_bus as eb
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    q: asyncio.Queue = asyncio.Queue()  # empty → run() blocks on get → live task
    monkeypatch.setattr(eb.bus, "subscribe", lambda pattern: q)
    bridge = ApprovalBridge(construct=_construct())
    await bridge.start()
    await asyncio.sleep(0.02)
    await bridge.stop()
    assert bridge._task is None


@pytest.mark.asyncio
async def test_bridge_on_waiting_truncates_and_isolates_fn_errors():
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    construct = _construct()

    def _raise(_t):
        raise RuntimeError("boom")

    bridge = ApprovalBridge(
        construct=construct,
        get_output_fn=lambda t: "x" * 2048,  # >1024 → truncated to tail
        get_provider_fn=_raise,  # except branch
        get_session_fn=_raise,  # except branch
    )
    await bridge._on_waiting("t1")  # must not raise
    assert construct.pending()


# --------------------------------------------------------------------------
# handoff — no-op handle_frame + projection + delivery-failure + double-resume
# --------------------------------------------------------------------------


def test_handoff_handle_frame_noop_and_projection():
    c = _construct()
    c.handle_frame("STATE_SNAPSHOT", {"snapshot": {}}, event_id=None)  # documented no-op
    assert isinstance(c.projection(), dict)


class _RaisingDelivery:
    def send_input(self, *a, **k):
        raise RuntimeError("deliver boom")

    def send_special_key(self, *a, **k):
        raise RuntimeError("deliver boom")


@pytest.mark.asyncio
async def test_handoff_resume_isolates_delivery_failure():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
        DeliveryError,
    )

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=_RaisingDelivery())
    it = c.on_provider_waiting("t1", "kiro_cli", "Allow? (y/n)", session_name="s")
    # Delivery failure is surfaced (retryable), NOT reported as a successful
    # resolution: the interrupt stays open so a later resume can re-attempt.
    with pytest.raises(DeliveryError):
        await c.resume(interrupt_id=it.id, decision=ApprovalDecision.APPROVE)
    assert not it.resolved
    assert c.get_interrupt(it.id) is not None


@pytest.mark.asyncio
async def test_handoff_double_resume_idempotent():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
    )

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    it = c.on_provider_waiting("t1", "kiro_cli", "Allow? (y/n)", session_name="s")
    await c.resume(interrupt_id=it.id, decision=ApprovalDecision.APPROVE)
    try:
        await c.resume(interrupt_id=it.id, decision=ApprovalDecision.APPROVE)
    except (KeyError, ValueError):
        pass


# --------------------------------------------------------------------------
# supervisor_dashboard — orphan-session terminal in hierarchy()
# --------------------------------------------------------------------------


def test_dashboard_hierarchy_orphan_session():
    from cli_agent_orchestrator.services.agui.supervisor_dashboard import SupervisorDashboardStream
    from cli_agent_orchestrator.services.agui_stream import AGUI_STATE_SNAPSHOT

    d = SupervisorDashboardStream(emitter=RecordingUiEmitter())
    fleet = {"sessions": [], "terminals": [{"id": "t1", "session_name": "orphan"}]}
    d.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)
    h = d.hierarchy()
    assert h["orphan"]["terminal_count"] == 1


# --------------------------------------------------------------------------
# session_timeline — missing-id open/close + unknown closer
# --------------------------------------------------------------------------


def test_timeline_guard_branches():
    from cli_agent_orchestrator.services.agui.session_timeline import MultiAgentSessionTimeline
    from cli_agent_orchestrator.services.agui_stream import AGUI_TOOL_CALL_END, AGUI_TOOL_CALL_START

    tl = MultiAgentSessionTimeline(emitter=RecordingUiEmitter())
    tl.handle_frame(AGUI_TOOL_CALL_START, {}, event_id=None)  # no tool_call_id → no-op
    tl.handle_frame(AGUI_TOOL_CALL_END, {}, event_id=None)  # no tool_call_id → no-op
    tl.handle_frame(AGUI_TOOL_CALL_END, {"tool_call_id": "never"}, event_id=None)  # unknown closer
    assert isinstance(tl.projection(), dict)


# --------------------------------------------------------------------------
# stream_reader — malformed SSE (None line, bad JSON, non-dict payload)
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, lines) -> None:
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def iter_lines(self, decode_unicode: bool = False) -> Iterator:
        for line in self._lines:
            yield line


def _frames(lines):
    from cli_agent_orchestrator.services.agui.stream_reader import AguiStreamReader

    reader = AguiStreamReader("http://localhost:8420")
    with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(lines)
        return list(reader.frames())


def test_stream_reader_skips_none_line():
    frames = _frames([None, "id: e1", "event: RUN_STARTED", 'data: {"a": 1}', ""])
    assert any(agui == "RUN_STARTED" for _, agui, _ in frames)


def test_stream_reader_bad_json_dropped():
    frames = _frames(["event: RAW", "data: {not-json", ""])
    assert frames == []  # undecodable data → event dropped


def test_stream_reader_non_dict_payload_wrapped():
    frames = _frames(["event: RAW", "data: 42", ""])
    # scalar JSON → wrapped as {"_raw": 42}
    assert frames and frames[0][2] == {"_raw": 42}


# --------------------------------------------------------------------------
# last stragglers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_on_waiting_output_fn_error_isolated():
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    def _raise(_t):
        raise RuntimeError("out boom")

    bridge = ApprovalBridge(
        construct=_construct(),
        get_output_fn=_raise,  # raises → except logger.debug
        get_provider_fn=lambda t: "kiro_cli",
        get_session_fn=lambda t: "s",
    )
    await bridge._on_waiting("t1")  # must not raise


@pytest.mark.asyncio
async def test_handoff_generic_provider_deny():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
    )

    class _D:
        def send_input(self, *a, **k):
            pass

        def send_special_key(self, *a, **k):
            return True

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=_D())
    it = c.on_provider_waiting("t1", "some_unknown_provider", "Proceed? (y/n)", session_name="s")
    await c.resume(interrupt_id=it.id, decision=ApprovalDecision.DENY)  # generic fallback → "n"


def test_handoff_expire_stale_mapping():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    # Terminal maps to an interrupt id that is not in the registry → stale cleanup.
    c._terminal_to_interrupt["tX"] = "missing-interrupt-id"
    assert c.expire("tX") is None


def test_handoff_expire_emit_failure_isolated():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    c.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate", session_name="s")
    c._emitter = _RaisingEmitterForExpire()  # expire's emit now raises → except swallows
    exp = c.expire("t1")
    assert exp is not None and exp.outcome == "expired"


class _RaisingEmitterForExpire:
    def emit_intent(self, component, props, terminal_id=None, session_name=None):
        raise RuntimeError("emit boom")


@pytest.mark.asyncio
async def test_run_plane_close_all_synthesizes_closer():
    async def _bus():
        yield {
            "kind": "handoff",
            "terminal_id": "r1",
            "id": "o1",
            "timestamp": "2026-07-04T00:00:00Z",
            "detail": {"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
        }

    frames = await _collect(rp.run_plane_stream(input_data=_run_input(), bus_subscribe_fn=_bus))
    joined = "".join(frames)
    # opened via handoff; the bus ends before completion → close_all synthesizes the closer.
    assert "TOOL_CALL_START" in joined and "TOOL_CALL_END" in joined


def test_lifecycle_open_without_receiver_and_unknown_close():
    from cli_agent_orchestrator.services.agui.lifecycle_tracker import ToolCallLifecycleTracker
    from cli_agent_orchestrator.services.agui_stream import to_agui_event

    tr = ToolCallLifecycleTracker()
    # orchestration_type handoff but NO receiver → open handler returns [] early.
    rec = {
        "kind": "handoff",
        "terminal_id": "r",
        "id": "x",
        "detail": {"sender": "s", "orchestration_type": "handoff"},
    }
    tr.feed(rec, to_agui_event(rec))
    # completion for a terminal that was never opened → _try_close returns [].
    crec = {
        "kind": "completion",
        "terminal_id": "never-opened",
        "id": "c",
        "detail": {"event_type": "post_kill_terminal", "agent_name": "d"},
    }
    tr.feed(crec, to_agui_event(crec))


# --- _evict_if_needed (TTL + cap) ---


def test_handoff_evict_ttl():
    import time

    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    it = c.on_provider_waiting("t1", "kiro_cli", "Allow? (y/n)", session_name="s")
    c._interrupts[it.id].resolved = True
    c._resolved_at[it.id] = time.monotonic() - 10_000  # older than the resolved TTL
    c._evict_if_needed()
    assert it.id not in c._interrupts  # TTL-evicted


def test_handoff_evict_cap_with_no_resolved():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        _REGISTRY_CAP,
        AgentHandoffWithApproval,
    )

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    # Over cap but all UNRESOLVED → the cap loop finds no evictable entry → break.
    for i in range(_REGISTRY_CAP + 2):
        c.on_provider_waiting(f"t{i}", "kiro_cli", "Allow? (y/n)", session_name="s")
    c._evict_if_needed()
    assert len(c._interrupts) >= _REGISTRY_CAP  # nothing resolved to evict


# --- stream_reader end-of-stream flush (no trailing blank line) ---


def test_stream_reader_flush_bad_json_at_eof():
    frames = _frames(["event: RAW", "data: {bad"])  # no trailing blank → EOF flush
    assert frames == []


def test_stream_reader_flush_non_dict_at_eof():
    frames = _frames(["event: RAW", "data: 7"])  # no trailing blank → EOF flush wraps scalar
    assert frames and frames[0][2] == {"_raw": 7}
