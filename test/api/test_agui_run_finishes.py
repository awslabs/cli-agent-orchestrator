"""POST /agui/v1/run must end its body (ag-ui-protocol/ag-ui#2216).

A stock AG-UI client completes a run only when the response body ends. These
tests drive the real route with the real SseBus, which never ends on its own,
and bound each request so a regression fails fast instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import sse_bus
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

# Without the [agui] extra the route returns 501; skip rather than fail for the wrong reason.
pytest.importorskip("ag_ui")

RUN_DEADLINE_SECONDS = 5.0
_WAITING_PROMPT = "↑/↓ to navigate"


class _FakeBridge:
    def __init__(self, construct: AgentHandoffWithApproval):
        self.construct = construct


@pytest.fixture(autouse=True)
def _route_env(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.services.session_service.list_sessions", lambda: [])
    sse_bus.reset_bus()
    yield
    sse_bus.reset_bus()


@pytest.fixture
def construct():
    c = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    app.state.approval_bridge = _FakeBridge(c)
    yield c
    del app.state.approval_bridge


def _body(resume: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "threadId": "thread-finish",
        "runId": "run-finish",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        body["resume"] = resume
    return body


def _approve(interrupt_id: str) -> Dict[str, Any]:
    return {"interruptId": interrupt_id, "status": "resolved", "payload": {"approved": True}}


async def _post_run(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        try:
            resp = await asyncio.wait_for(
                http.post("/agui/v1/run", json=body), timeout=RUN_DEADLINE_SECONDS
            )
        except asyncio.TimeoutError:
            pytest.fail(f"/agui/v1/run body did not end within {RUN_DEADLINE_SECONDS}s")
    assert resp.status_code == 200
    return [
        json.loads(line[len("data: ") :])
        for line in resp.text.split("\n")
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_run_ends_when_nothing_is_pending(construct):
    frames = await _post_run(_body())

    assert [f["type"] for f in frames] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    assert frames[-1]["outcome"]["type"] == "success"


@pytest.mark.asyncio
async def test_run_ends_after_resume_resolves_the_only_interrupt(construct):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _WAITING_PROMPT)

    frames = await _post_run(_body(resume=[_approve(interrupt.id)]))

    assert construct.get_interrupt(interrupt.id).resolved
    assert frames[0]["type"] == "RUN_STARTED"
    assert frames[-1]["type"] == "RUN_FINISHED"
    assert frames[-1]["outcome"]["type"] == "success"


@pytest.mark.asyncio
async def test_run_ends_with_remaining_interrupt_after_partial_resume(construct):
    first = construct.on_provider_waiting("t-1", "claude_code", _WAITING_PROMPT)
    second = construct.on_provider_waiting("t-2", "claude_code", _WAITING_PROMPT)

    frames = await _post_run(_body(resume=[_approve(first.id)]))

    assert construct.get_interrupt(first.id).resolved
    assert frames[-1]["type"] == "RUN_FINISHED"
    assert frames[-1]["outcome"]["type"] == "interrupt"
    assert [i["id"] for i in frames[-1]["outcome"]["interrupts"]] == [second.id]


@pytest.mark.asyncio
async def test_resume_entry_without_status_is_rejected_with_422(construct):
    # AG-UI 1.0 requires `status` on every resume entry. The body must be rejected
    # before the stream starts, not end an already-sent 200 with no frames.
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _WAITING_PROMPT)
    entry = {"interruptId": interrupt.id, "payload": {"approved": True}}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        resp = await asyncio.wait_for(
            http.post("/agui/v1/run", json=_body(resume=[entry])),
            timeout=RUN_DEADLINE_SECONDS,
        )

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    locs = [tuple(err["loc"]) for err in resp.json()["detail"]]
    assert ("body", "resume", 0, "status") in locs
    assert not construct.get_interrupt(interrupt.id).resolved


@pytest.mark.asyncio
async def test_resume_entry_with_status_still_streams_to_run_finished(construct):
    interrupt = construct.on_provider_waiting("t-1", "claude_code", _WAITING_PROMPT)
    entry = {"interruptId": interrupt.id, "status": "resolved", "payload": {"approved": True}}

    frames = await _post_run(_body(resume=[entry]))

    assert construct.get_interrupt(interrupt.id).resolved
    assert [f["type"] for f in frames][0] == "RUN_STARTED"
    assert frames[-1]["type"] == "RUN_FINISHED"
    assert frames[-1]["outcome"]["type"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"", b"not json", b'{"threadId": '])
async def test_empty_or_non_json_body_is_rejected_with_422(construct, raw):
    # request.json() raises JSONDecodeError for these; the route used to answer 500.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        resp = await asyncio.wait_for(
            http.post("/agui/v1/run", content=raw, headers={"content-type": "application/json"}),
            timeout=RUN_DEADLINE_SECONDS,
        )

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    with pytest.raises(ValueError) as decode_error:
        json.loads(raw)
    assert resp.json()["detail"] == [
        {
            "type": "json_invalid",
            "loc": ["body"],
            "msg": "invalid JSON body",
            "ctx": {"error": str(decode_error.value)},
        }
    ]


@pytest.mark.asyncio
async def test_resume_with_unknown_interrupt_id_ends_with_run_error(construct):
    frames = await _post_run(_body(resume=[_approve("nonexistent-id")]))

    assert [f["type"] for f in frames] == ["RUN_STARTED", "RUN_ERROR"]
    assert "nonexistent-id" in frames[-1]["message"]


@pytest.fixture
def no_bridge():
    saved = getattr(app.state, "approval_bridge", None)
    if saved is not None:
        del app.state.approval_bridge
    yield
    if saved is not None:
        app.state.approval_bridge = saved


@pytest.mark.asyncio
async def test_resume_without_approval_bridge_is_rejected_with_404(no_bridge):
    # With no bridge nothing can process resume[]; the run must not report success.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        resp = await asyncio.wait_for(
            http.post("/agui/v1/run", json=_body(resume=[_approve("stale-id")])),
            timeout=RUN_DEADLINE_SECONDS,
        )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Approval bridge not initialized"}


@pytest.mark.asyncio
async def test_run_without_resume_still_finishes_without_approval_bridge(no_bridge):
    frames = await _post_run(_body())

    assert [f["type"] for f in frames] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
