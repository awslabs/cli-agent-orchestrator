"""Claude Code permission prompts offer approve and deny only.

Claude Code's permission dialog is a selector. CAO's ``edit`` answer types free
text into the pane, and the dialog has no text field to receive it, so an edit
cannot carry the operator's text to the agent. These tests pin that the option
is not offered for ``claude-code:permission_request``, that an edit decision is
refused before anything reaches the terminal, and that the interrupt stays
answerable. Other providers keep ``edit``.
"""

from __future__ import annotations

import json
from typing import Any, List, Tuple

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    ApprovalDecision,
)

_CLAUDE_PERMISSION_PROMPT = "\u2191/\u2193 to navigate"
_KIRO_PERMISSION_PROMPT = "Allow this action? [y/n/t]:"

client = TestClient(app, base_url="http://localhost")


class _RecordingDelivery:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str]] = []

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        self.calls.append(("input", terminal_id, text))

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        self.calls.append(("key", terminal_id, key))
        return True


@pytest.fixture
def emitter() -> RecordingUiEmitter:
    return RecordingUiEmitter()


@pytest.fixture
def delivery() -> _RecordingDelivery:
    return _RecordingDelivery()


@pytest.fixture
def construct(emitter, delivery) -> AgentHandoffWithApproval:
    return AgentHandoffWithApproval(emitter=emitter, answer_delivery=delivery)


@pytest.fixture
def bridge(construct, monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    app.state.approval_bridge = ApprovalBridge(construct=construct)
    yield app.state.approval_bridge
    del app.state.approval_bridge


# (i) options ----------------------------------------------------------------


def test_claude_code_permission_request_options_exclude_edit(construct, emitter):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)

    assert interrupt.reason == "claude-code:permission_request"
    assert interrupt.options == ["approve", "deny"]
    card = [e for e in emitter.intents if e.get("component") == "approval_card"][-1]
    assert card["props"]["options"] == ["approve", "deny"]


# (ii) edit refused, nothing delivered, interrupt still pending ---------------


@pytest.mark.asyncio
async def test_claude_code_permission_request_edit_is_refused_without_delivery(construct, delivery):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)

    with pytest.raises(ValueError, match="not supported"):
        await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="other text")

    assert delivery.calls == []
    assert not construct.get_interrupt(interrupt.id).resolved
    assert [p.id for p in construct.pending()] == [interrupt.id]


def test_claude_code_permission_request_edit_via_rest_is_4xx_without_delivery(
    bridge, construct, delivery
):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)

    resp = client.post(
        f"/agui/v1/interrupts/{interrupt.id}/resume",
        json={"decision": "edit", "edited_text": "other text"},
    )

    assert resp.status_code == 422
    assert "not supported" in resp.json()["detail"]
    assert delivery.calls == []
    assert not construct.get_interrupt(interrupt.id).resolved


@pytest.mark.asyncio
async def test_claude_code_permission_request_edit_via_run_plane_is_run_error(construct, delivery):
    pytest.importorskip("ag_ui")
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)
    body = {
        "threadId": "th",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "resume": [
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"editedArgs": "other text"},
            }
        ],
    }

    frames = []
    async for raw in run_plane_stream(input_data=body, approval_construct=construct):
        for line in raw.strip().split("\n"):
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))

    assert frames[0]["type"] == "RUN_STARTED"
    assert frames[-1]["type"] == "RUN_ERROR"
    assert "not supported" in frames[-1]["message"]
    assert delivery.calls == []
    assert not construct.get_interrupt(interrupt.id).resolved


# (iii) approve and deny still work -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "key"),
    [(ApprovalDecision.APPROVE, "Enter"), (ApprovalDecision.DENY, "Escape")],
)
async def test_claude_code_permission_request_approve_and_deny_still_deliver(
    construct, delivery, decision, key
):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)

    result = await construct.resume(interrupt.id, decision)

    assert result.resolved
    assert result.outcome == decision.value
    assert delivery.calls == [("key", "t-1", key)]


@pytest.mark.asyncio
async def test_approve_after_refused_edit_still_delivers(construct, delivery):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)
    with pytest.raises(ValueError):
        await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="other text")

    result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)

    assert result.outcome == "approve"
    assert delivery.calls == [("key", "t-1", "Enter")]


# (iv) edit unchanged where it is supported ---------------------------------


@pytest.mark.asyncio
async def test_kiro_permission_request_edit_unchanged(construct, delivery):
    interrupt = construct.on_provider_waiting("t-1", "kiro_cli", _KIRO_PERMISSION_PROMPT)
    assert interrupt.reason == "kiro:permission_request"
    assert interrupt.options == ["approve", "deny", "edit"]

    result = await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="t")

    assert result.outcome == "edit"
    assert delivery.calls == [("input", "t-1", "t")]


def test_codex_approval_request_still_offers_edit(construct):
    interrupt = construct.on_provider_waiting("t-1", "codex", "Approve execution? (y/n)")
    assert interrupt.reason == "codex:approval_request"
    assert interrupt.options == ["approve", "deny", "edit"]


# (v) an edit that arrives while an approve is in flight joins it ------------


@pytest.mark.asyncio
async def test_edit_during_in_flight_approve_joins_it_without_typing():
    """First decision wins: a later edit joins the in-flight approve.

    The edit is not validated against the options here, because ``resume`` joins
    an in-flight delivery before it validates. It returns the approve outcome,
    which names the decision that was delivered, and nothing is typed for it.
    """
    import asyncio
    import threading

    first_in_delivery = threading.Event()
    release_first = threading.Event()

    class _PausingDelivery(_RecordingDelivery):
        def send_special_key(self, terminal_id: str, key: str) -> bool:
            first_in_delivery.set()
            release_first.wait()
            return super().send_special_key(terminal_id, key)

    delivery = _PausingDelivery()
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _CLAUDE_PERMISSION_PROMPT)

    approve = asyncio.create_task(construct.resume(interrupt.id, ApprovalDecision.APPROVE))
    await asyncio.to_thread(first_in_delivery.wait)

    edit = asyncio.create_task(
        construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="other text")
    )
    await asyncio.sleep(0.05)  # give the edit a chance to (wrongly) type or raise
    assert not edit.done()

    release_first.set()
    edit_result = await edit
    approve_result = await approve

    assert edit_result.outcome == "approve"
    assert approve_result.outcome == "approve"
    assert delivery.calls == [("key", "t-1", "Enter")]
    assert not any(kind == "input" for kind, _, _ in delivery.calls)
    final = construct.get_interrupt(interrupt.id)
    assert final.resolved
    assert final.outcome == "approve"


def test_claude_code_trust_prompt_options_unchanged(construct):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", "Yes, I trust this folder")
    assert interrupt.reason == "claude-code:trust_prompt"
    assert interrupt.options == ["approve", "deny"]
