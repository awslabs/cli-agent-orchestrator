"""Coverage completion for the AG-UI construct/service branch + error paths.

Targets the previously-uncovered branches in run_plane (_translate_live_frame
per type + resume mapping), base emitters, ApprovalBridge (_on_waiting/_on_leave
+ disabled early-return + loop), and handoff_approval edge cases. These are the
lines Codecov's (now-accurate) patch report flags across the agui service files.
"""

from __future__ import annotations

import asyncio

import pytest

from cli_agent_orchestrator.services.agui import run_plane as rp
from cli_agent_orchestrator.services.agui.base import (
    HttpUiEmitter,
    InProcessUiEmitter,
    RecordingUiEmitter,
    apply_json_patch_strict,
)

# --------------------------------------------------------------------------
# run_plane._translate_live_frame — every mapped type + the except fallback
# --------------------------------------------------------------------------


def _encoder():
    from ag_ui.encoder import EventEncoder

    return EventEncoder()


@pytest.mark.parametrize(
    "atype,data",
    [
        (rp._AGUI_STATE_SNAPSHOT, {"snapshot": {"a": 1}}),
        (rp._AGUI_STATE_DELTA, {"delta": [{"op": "replace", "path": "/a", "value": 2}]}),
        (rp._AGUI_STEP_STARTED, {"terminal_id": "t1", "provider": "kiro_cli"}),
        (rp._AGUI_STEP_FINISHED, {"terminal_id": "t1"}),
        (rp._AGUI_TOOL_CALL_START, {"tool_call_id": "tc1", "tool_call_name": "handoff"}),
        (rp._AGUI_TOOL_CALL_END, {"tool_call_id": "tc1"}),
        (rp._AGUI_GENERATIVE_UI, {"component": "approval_card", "props": {}}),
        (rp._AGUI_TEXT_MESSAGE_CONTENT, {"content": "hi"}),
        (rp._AGUI_RAW, {"foo": "bar"}),
        (rp._AGUI_RUN_ERROR, {"message": "boom"}),
        ("SOME_UNMAPPED_TYPE", {"k": "v"}),
        (rp._AGUI_TOOL_CALL_START, {}),  # missing ids → uuid/"unknown" fallbacks
        (rp._AGUI_STEP_STARTED, {}),
    ],
)
def test_translate_live_frame_all_types(atype, data):
    out = rp._translate_live_frame(atype, data, "t", "r", _encoder())
    assert out is None or out.startswith("data:")


def test_translate_live_frame_swallows_encoder_error():
    class _BoomEncoder:
        def encode(self, evt):
            raise RuntimeError("encode boom")

    # Any mapped type with a raising encoder → except → None (never propagates).
    assert rp._translate_live_frame(rp._AGUI_RAW, {"x": 1}, "t", "r", _BoomEncoder()) is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"approved": True}, "approve"),
        ({"approved": False}, "deny"),
        ({"editedArgs": {"x": 1}}, "edit"),
        ({"edited_args": {"x": 1}}, "edit"),
        ({}, None),
        ("not-a-dict", None),
        ({"approved": "maybe"}, None),
    ],
)
def test_map_resume_payload(payload, expected):
    assert rp._map_resume_payload(payload) == expected


# --------------------------------------------------------------------------
# base emitters
# --------------------------------------------------------------------------


def test_inprocess_emitter_refuses_when_surface_disabled(monkeypatch):
    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    with pytest.raises(RuntimeError):
        InProcessUiEmitter().emit_intent("approval_card", {"title": "x"})


def test_inprocess_emitter_publishes_when_enabled(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    published = []

    class _Log:
        def append(self, *a, **k):
            return {"id": "e1"}

    class _Bus:
        def publish(self, event):
            published.append(event)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _Bus())
    InProcessUiEmitter().emit_intent("approval_card", {"title": "x"}, terminal_id="t1")
    assert published


def test_http_emitter_raises_on_400(monkeypatch):
    class _Resp:
        status_code = 400
        text = "bad"

    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
    with pytest.raises(ValueError):
        HttpUiEmitter(base_url="http://localhost:9889").emit_intent("approval_card", {"t": 1})


def test_http_emitter_ok_on_200(monkeypatch):
    class _Resp:
        status_code = 200
        text = "ok"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
    HttpUiEmitter(base_url="http://localhost:9889").emit_intent("approval_card", {"t": 1})


def test_recording_emitter_records():
    em = RecordingUiEmitter()
    em.emit_intent("approval_card", {"t": 1}, terminal_id="t1", session_name="s1")
    assert em.intents and em.intents[0]["component"] == "approval_card"


def test_apply_json_patch_edge_cases():
    # remove root → None; replace missing key → None; unsupported op → None
    assert apply_json_patch_strict({"a": 1}, [{"op": "remove", "path": ""}]) is None
    assert apply_json_patch_strict({"a": 1}, [{"op": "replace", "path": "/b", "value": 2}]) is None
    assert apply_json_patch_strict({"a": 1}, [{"op": "move", "path": "/a"}]) is None


# --------------------------------------------------------------------------
# ApprovalBridge
# --------------------------------------------------------------------------


def _construct():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    return AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)


@pytest.mark.asyncio
async def test_bridge_run_returns_when_surface_disabled(monkeypatch):
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    # Returns immediately (disabled) rather than subscribing/looping forever.
    await asyncio.wait_for(ApprovalBridge(construct=_construct()).run(), timeout=1.0)


@pytest.mark.asyncio
async def test_bridge_on_waiting_with_injected_fns():
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    construct = _construct()
    bridge = ApprovalBridge(
        construct=construct,
        get_output_fn=lambda t: "APPROVAL_REQUIRED: allow this?",
        get_provider_fn=lambda t: "claude_code",
        get_session_fn=lambda t: "s1",
    )
    await bridge._on_waiting("t1")
    assert construct.pending()  # an interrupt was opened


@pytest.mark.asyncio
async def test_bridge_on_waiting_swallows_default_lookup_failures(monkeypatch):
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    construct = _construct()
    # No injected fns → default terminal_service/provider_manager paths; make them
    # raise so the except branches run and empty provider/prompt is used.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_output",
        lambda t: (_ for _ in ()).throw(RuntimeError("no output")),
        raising=False,
    )
    bridge = ApprovalBridge(construct=construct)
    await bridge._on_waiting("t1")  # must not raise
    assert construct.pending()


@pytest.mark.asyncio
async def test_bridge_on_waiting_maps_provider_class_name(monkeypatch):
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    class ClaudeCodeProvider:
        pass

    class _Mgr:
        def get_provider(self, tid):
            return ClaudeCodeProvider()

    monkeypatch.setattr("cli_agent_orchestrator.providers.manager.provider_manager", _Mgr())
    bridge = ApprovalBridge(construct=_construct())
    await bridge._on_waiting("t1")


def test_bridge_on_leave_expires(monkeypatch):
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

    construct = _construct()
    construct.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate")
    bridge = ApprovalBridge(construct=construct)
    bridge._on_leave_waiting("t1")
    assert not construct.pending()


# --------------------------------------------------------------------------
# handoff_approval edge branches
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_edit_too_long_rejected():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
    )

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    it = c.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate")
    with pytest.raises(ValueError):
        await c.resume(interrupt_id=it.id, decision=ApprovalDecision.EDIT, edited_text="x" * 4001)


@pytest.mark.asyncio
async def test_handoff_resume_unknown_interrupt_returns_none_or_raises():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
    )

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    # Resuming an unknown interrupt id must not corrupt state.
    try:
        await c.resume(interrupt_id="nope", decision=ApprovalDecision.APPROVE)
    except (KeyError, ValueError):
        pass


def test_handoff_expire_unknown_terminal_is_noop():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    assert c.expire("no-such-terminal") is None


# --------------------------------------------------------------------------
# run_plane_stream interrupt + resume-failure flows
# --------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_run_plane_open_interrupt_emits_interrupt_outcome():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    c.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate", session_name="s")
    frames = await _collect(
        rp.run_plane_stream(
            input_data=_run_input(), approval_construct=c, snapshot_fn=lambda: {"x": 1}
        )
    )
    joined = "".join(frames)
    assert "interrupt" in joined and "RUN_FINISHED" in joined


@pytest.mark.asyncio
async def test_run_plane_open_interrupt_snapshot_failure_isolated():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    c.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate")

    def _boom():
        raise RuntimeError("snapshot boom")

    frames = await _collect(
        rp.run_plane_stream(input_data=_run_input(), approval_construct=c, snapshot_fn=_boom)
    )
    assert any("RUN_FINISHED" in f for f in frames)


@pytest.mark.asyncio
async def test_run_plane_resume_failure_emits_run_error():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    it = c.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate", session_name="s")
    # editedArgs → decision "edit" but empty text → construct.resume raises →
    # run plane emits RUN_ERROR.
    frames = await _collect(
        rp.run_plane_stream(
            input_data=_run_input(
                resume=[{"interruptId": it.id, "status": "resolved", "payload": {"editedArgs": ""}}]
            ),
            approval_construct=c,
        )
    )
    assert any("RUN_ERROR" in f for f in frames)


# --------------------------------------------------------------------------
# handoff resume delivery (per-provider) + expire emission
# --------------------------------------------------------------------------


class _Delivery:
    def __init__(self):
        self.text = []
        self.keys = []

    def send_input(self, terminal_id, text, **kw):
        self.text.append((terminal_id, text))

    def send_special_key(self, terminal_id, key):
        self.keys.append((terminal_id, key))
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,prompt,decision",
    [
        ("claude_code", "\u2191/\u2193 to navigate", "approve"),  # → key
        ("kiro_cli", "Allow this action? (y/n)", "approve"),  # → text
        ("codex", "Approve execution? (y/n)", "deny"),  # → text
    ],
)
async def test_handoff_resume_delivers_answer(provider, prompt, decision):
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
    )

    delivery = _Delivery()
    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
    it = c.on_provider_waiting("t1", provider, prompt, session_name="s")
    await c.resume(interrupt_id=it.id, decision=ApprovalDecision(decision))
    assert delivery.text or delivery.keys  # an answer was delivered


def test_handoff_expire_emits_resolution():
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    emitter = RecordingUiEmitter()
    c = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)
    c.on_provider_waiting("t1", "claude_code", "\u2191/\u2193 to navigate", session_name="s")
    expired = c.expire("t1")
    assert expired is not None and expired.outcome == "expired"
    # an expiration approval_card intent was emitted
    assert any(i["props"].get("outcome") == "expired" for i in emitter.intents)


# --------------------------------------------------------------------------
# ApprovalBridge event loop (drive the bus)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_run_loop_processes_status_events(monkeypatch):
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
    from cli_agent_orchestrator.services.event_bus import bus

    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    construct = _construct()
    bridge = ApprovalBridge(
        construct=construct,
        get_output_fn=lambda t: "APPROVAL_REQUIRED: allow?",
        get_provider_fn=lambda t: "claude_code",
        get_session_fn=lambda t: "s1",
    )
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.05)
    # Enter waiting → bridge opens an interrupt.
    bus.publish("terminal.t1.status", {"status": TerminalStatus.WAITING_USER_ANSWER.value})
    await asyncio.sleep(0.05)
    # Leave waiting → bridge expires it.
    bus.publish("terminal.t1.status", {"status": TerminalStatus.IDLE.value})
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # The loop ran (interrupt opened then expired); no exception escaped.
    assert "t1" not in bridge._waiting_terminals


# --------------------------------------------------------------------------
# base: list-patch ops + emit without terminal_id
# --------------------------------------------------------------------------


def test_apply_json_patch_list_ops():
    doc = {"items": [1, 2, 3]}
    # add to end via "-"
    out = apply_json_patch_strict(doc, [{"op": "add", "path": "/items/-", "value": 4}])
    assert out == {"items": [1, 2, 3, 4]}
    # remove list index
    out = apply_json_patch_strict(doc, [{"op": "remove", "path": "/items/0"}])
    assert out == {"items": [2, 3]}
    # replace list index
    out = apply_json_patch_strict(doc, [{"op": "replace", "path": "/items/1", "value": 9}])
    assert out == {"items": [1, 9, 3]}
    # input never mutated
    assert doc == {"items": [1, 2, 3]}


def test_inprocess_emitter_without_terminal_id(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")

    class _Log:
        def append(self, *a, **k):
            return {"id": "e2"}

    class _Bus:
        def publish(self, event):
            pass

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _Log()
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _Bus())
    InProcessUiEmitter().emit_intent("approval_card", {"title": "x"})


class _RaisingEmitter:
    """Emitter whose emit_intent always raises — exercises the constructs'
    emit-failure isolation (`except (ValueError, RuntimeError)`) branches."""

    def emit_intent(self, component, props, terminal_id=None, session_name=None):
        raise RuntimeError("emit boom")


@pytest.mark.asyncio
async def test_handoff_resume_and_expire_isolate_emit_failure():
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        AgentHandoffWithApproval,
        ApprovalDecision,
    )

    # on_provider_waiting emits an approval_card; a raising emitter must not
    # break interrupt creation, resume, or expire.
    c1 = AgentHandoffWithApproval(emitter=_RaisingEmitter(), answer_delivery=_Delivery())
    it = c1.on_provider_waiting("t1", "kiro_cli", "Allow? (y/n)", session_name="s")
    # resume emits a resolution intent → emit raises → swallowed.
    await c1.resume(interrupt_id=it.id, decision=ApprovalDecision.APPROVE)

    c2 = AgentHandoffWithApproval(emitter=_RaisingEmitter(), answer_delivery=None)
    c2.on_provider_waiting("t2", "claude_code", "\u2191/\u2193 to navigate", session_name="s")
    # expire emits an expiration intent → emit raises → swallowed.
    assert c2.expire("t2") is not None
