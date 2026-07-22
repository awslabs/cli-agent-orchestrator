"""Integration tests for POST /agui/v1/run endpoint.

Covers:
- Route gating (404 when disabled)
- Scope requirements (cao:read floor, cao:write for resume[])
- Lifecycle-legality assertions over recorded streams
- 501 when ag-ui-protocol extra is missing
- Full interrupt/resume round-trip through the HTTP layer
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

client = TestClient(app, base_url="http://localhost")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_body(
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    resume: list = None,
) -> Dict[str, Any]:
    """Build a minimal RunAgentInput-shaped camelCase body."""
    body: Dict[str, Any] = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        body["resume"] = resume
    return body


def _parse_sse_frames(content: str) -> List[Dict[str, Any]]:
    """Parse SSE response text into a list of JSON payloads."""
    results = []
    for line in content.split("\n"):
        if line.startswith("data: "):
            payload = line[len("data: ") :]
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return results


class _FakeBus:
    """SseBus stand-in that yields no live events, so the stream closes."""

    def register(self, overflow_close=False):
        return object()

    def unregister(self, queue):
        pass

    async def drain(self, queue):
        # Yield nothing so the stream finishes immediately.
        return
        yield  # noqa: make it an async generator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    """Enable the AG-UI surface for all tests in this module."""
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")


@pytest.fixture(autouse=True)
def _auth_off(monkeypatch):
    """Disable auth by default (tests that need auth override this)."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)


@pytest.fixture(autouse=True)
def _stub_bus(monkeypatch):
    """Replace the SseBus so streaming tests finish immediately."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.sse_bus.get_bus",
        lambda: _FakeBus(),
    )


class _FakeBridge:
    """Minimal approval bridge stand-in for app.state."""

    def __init__(self, construct: AgentHandoffWithApproval):
        self.construct = construct


# ---------------------------------------------------------------------------
# Tests: route gating
# ---------------------------------------------------------------------------


def test_run_404_when_agui_disabled(monkeypatch):
    """POST /agui/v1/run returns 404 when AG-UI surface is disabled."""
    monkeypatch.setenv("CAO_AGUI_ENABLED", "false")
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)

    resp = client.post("/agui/v1/run", json=_minimal_body())
    assert resp.status_code == 404


def test_run_501_without_agui_extra(monkeypatch):
    """POST /agui/v1/run returns 501 when ag-ui-protocol is not installed."""
    import cli_agent_orchestrator.services.agui.run_plane as run_plane_mod

    original = run_plane_mod.AG_UI_AVAILABLE
    try:
        run_plane_mod.AG_UI_AVAILABLE = False
        resp = client.post("/agui/v1/run", json=_minimal_body())
        assert resp.status_code == 501
        body = resp.json()
        assert "ag-ui-protocol" in body["detail"]
        assert "pip install" in body["detail"]
    finally:
        run_plane_mod.AG_UI_AVAILABLE = original


# ---------------------------------------------------------------------------
# Tests: scope requirements
# ---------------------------------------------------------------------------


def test_run_requires_read_scope(monkeypatch):
    """POST /agui/v1/run requires at least cao:read scope when auth is enabled."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.security.auth.extract_scopes_from_token", lambda tok: []
    )

    resp = client.post(
        "/agui/v1/run",
        json=_minimal_body(),
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 403


def test_run_read_scope_sufficient_without_resume(monkeypatch):
    """cao:read is sufficient when resume[] is empty."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.security.auth.extract_scopes_from_token",
        lambda tok: ["cao:read"],
    )

    resp = client.post(
        "/agui/v1/run",
        json=_minimal_body(),
        headers={"Authorization": "Bearer test-token"},
    )
    # Should succeed (stream response)
    assert resp.status_code == 200


def test_run_write_required_for_resume(monkeypatch):
    """cao:write is required when resume[] is non-empty."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.security.auth.extract_scopes_from_token",
        lambda tok: ["cao:read"],
    )

    body = _minimal_body(
        resume=[
            {
                "interruptId": "int-1",
                "status": "resolved",
                "payload": {"approved": True},
            }
        ]
    )

    resp = client.post(
        "/agui/v1/run",
        json=body,
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 403
    assert "cao:write" in resp.json()["detail"]


def test_run_write_scope_allows_resume(monkeypatch):
    """cao:write permits resume[] entries."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.security.auth.extract_scopes_from_token",
        lambda tok: ["cao:read", "cao:write"],
    )

    # Even though the interrupt doesn't exist, we should get past the auth gate
    # and receive a streaming response (the error will be in the stream)
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)
    app.state.approval_bridge = _FakeBridge(construct)

    body = _minimal_body(
        resume=[
            {
                "interruptId": "nonexistent",
                "status": "resolved",
                "payload": {"approved": True},
            }
        ]
    )

    try:
        resp = client.post(
            "/agui/v1/run",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        # Should be 200 (streaming) with RUN_ERROR in the stream
        assert resp.status_code == 200
        frames = _parse_sse_frames(resp.text)
        assert frames[0]["type"] == "RUN_STARTED"
        assert frames[1]["type"] == "RUN_ERROR"
    finally:
        if hasattr(app.state, "approval_bridge"):
            del app.state.approval_bridge


# ---------------------------------------------------------------------------
# Tests: lifecycle legality
# ---------------------------------------------------------------------------


def test_run_lifecycle_legal_basic():
    """Basic run produces RUN_STARTED first and RUN_FINISHED last."""
    resp = client.post("/agui/v1/run", json=_minimal_body())
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")

    frames = _parse_sse_frames(resp.text)
    assert len(frames) >= 2
    assert frames[0]["type"] == "RUN_STARTED"
    assert frames[-1]["type"] == "RUN_FINISHED"


def test_run_lifecycle_legal_with_interrupts():
    """Run with open interrupts: RUN_STARTED, then STATE_SNAPSHOT+RUN_FINISHED(interrupt)."""
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    app.state.approval_bridge = _FakeBridge(construct)

    try:
        resp = client.post("/agui/v1/run", json=_minimal_body())
        assert resp.status_code == 200

        frames = _parse_sse_frames(resp.text)
        types = [f["type"] for f in frames]

        assert types[0] == "RUN_STARTED"
        assert types[-1] == "RUN_FINISHED"
        assert frames[-1]["outcome"]["type"] == "interrupt"
        # No events after RUN_FINISHED
        assert types.count("RUN_FINISHED") == 1
    finally:
        del app.state.approval_bridge


def test_run_lifecycle_no_events_after_finished():
    """No events are emitted after RUN_FINISHED or RUN_ERROR."""
    resp = client.post("/agui/v1/run", json=_minimal_body())
    frames = _parse_sse_frames(resp.text)

    found_terminal = False
    for frame in frames:
        if found_terminal:
            pytest.fail(f"Event after terminal frame: {frame['type']}")
        if frame["type"] in ("RUN_FINISHED", "RUN_ERROR"):
            found_terminal = True

    assert found_terminal, "No terminal event found"


# ---------------------------------------------------------------------------
# Tests: interrupt + resume round-trip via HTTP
# ---------------------------------------------------------------------------


def test_run_resume_approve_via_http():
    """Full approve round-trip: create interrupt, then resume via POST /agui/v1/run."""
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s-1",
    )

    app.state.approval_bridge = _FakeBridge(construct)

    try:
        body = _minimal_body(
            resume=[
                {
                    "interruptId": interrupt.id,
                    "status": "resolved",
                    "payload": {"approved": True},
                }
            ]
        )

        resp = client.post("/agui/v1/run", json=body)
        assert resp.status_code == 200

        frames = _parse_sse_frames(resp.text)
        types = [f["type"] for f in frames]

        assert types[0] == "RUN_STARTED"
        assert types[-1] == "RUN_FINISHED"
        # Interrupt resolved -> success outcome
        assert frames[-1]["outcome"]["type"] == "success"

        # Verify the construct state
        assert construct.get_interrupt(interrupt.id).resolved
        assert construct.get_interrupt(interrupt.id).outcome == "approve"
    finally:
        del app.state.approval_bridge


def test_run_resume_unknown_interrupt_via_http():
    """Resume with unknown interrupt produces RUN_ERROR in stream."""
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)

    app.state.approval_bridge = _FakeBridge(construct)

    try:
        body = _minimal_body(
            resume=[
                {
                    "interruptId": "does-not-exist",
                    "status": "resolved",
                    "payload": {"approved": True},
                }
            ]
        )

        resp = client.post("/agui/v1/run", json=body)
        assert resp.status_code == 200

        frames = _parse_sse_frames(resp.text)
        types = [f["type"] for f in frames]

        assert types[0] == "RUN_STARTED"
        assert types[1] == "RUN_ERROR"
        assert "does-not-exist" in frames[1]["message"]
    finally:
        del app.state.approval_bridge


# ---------------------------------------------------------------------------
# Tests: camelCase wire format
# ---------------------------------------------------------------------------


def test_run_frames_are_camel_case():
    """Wire frames use camelCase field names (threadId, runId, etc.)."""
    resp = client.post("/agui/v1/run", json=_minimal_body())
    frames = _parse_sse_frames(resp.text)

    for frame in frames:
        if "threadId" in frame:
            assert frame["threadId"] == "thread-1"
        if "runId" in frame:
            assert frame["runId"] == "run-1"


# ---------------------------------------------------------------------------
# Delivery failure (P1): run plane emits RUN_ERROR, not a success RUN_FINISHED
# ---------------------------------------------------------------------------


class _FailingDelivery:
    def send_input(self, terminal_id, text, **kwargs):
        raise RuntimeError("backend down")

    def send_special_key(self, terminal_id, key):
        raise RuntimeError("backend down")


def test_run_resume_delivery_failure_emits_run_error(monkeypatch):
    """A delivery failure during run-plane resume ends with RUN_ERROR, and the
    interrupt is left unresolved (retryable) rather than finishing as success."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)

    construct = AgentHandoffWithApproval(
        emitter=RecordingUiEmitter(), answer_delivery=_FailingDelivery()
    )
    interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
    app.state.approval_bridge = _FakeBridge(construct)

    body = _minimal_body(
        resume=[
            {
                "interruptId": interrupt.id,
                "status": "resolved",
                "payload": {"approved": True},
            }
        ]
    )
    try:
        resp = client.post("/agui/v1/run", json=body)
        assert resp.status_code == 200
        frames = _parse_sse_frames(resp.text)
        types = [f["type"] for f in frames]
        assert "RUN_ERROR" in types
        # No success RUN_FINISHED.
        success = [
            f
            for f in frames
            if f["type"] == "RUN_FINISHED" and f.get("outcome", {}).get("type") == "success"
        ]
        assert not success
        # Interrupt left unresolved (retryable).
        assert not interrupt.resolved
    finally:
        if hasattr(app.state, "approval_bridge"):
            del app.state.approval_bridge


def test_run_resume_retry_reachable_after_delivery_failure(monkeypatch):
    """After a delivery-failure RUN_ERROR, the interrupt is still open and a
    FRESH run carrying the same resume[] can retry and succeed (A's core value)."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)

    construct = AgentHandoffWithApproval(
        emitter=RecordingUiEmitter(), answer_delivery=_FailingDelivery()
    )
    interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
    app.state.approval_bridge = _FakeBridge(construct)

    body = _minimal_body(
        resume=[{"interruptId": interrupt.id, "status": "resolved", "payload": {"approved": True}}]
    )
    try:
        # First run fails to deliver -> RUN_ERROR, interrupt stays open.
        resp1 = client.post("/agui/v1/run", json=body)
        assert resp1.status_code == 200
        assert "RUN_ERROR" in [f["type"] for f in _parse_sse_frames(resp1.text)]
        assert not interrupt.resolved

        # Recover: delivery now works. A fresh run with the same resume[] resolves.
        construct._answer_delivery = None  # no-op delivery => commit succeeds
        resp2 = client.post("/agui/v1/run", json=body)
        assert resp2.status_code == 200
        assert interrupt.resolved
        assert interrupt.outcome == "approve"
    finally:
        if hasattr(app.state, "approval_bridge"):
            del app.state.approval_bridge


# ---------------------------------------------------------------------------
# Item 4 — Accept header negotiation
# ---------------------------------------------------------------------------


def test_accept_text_event_stream_content_type(client, monkeypatch):
    """Accept: text/event-stream returns negotiated content type from encoder."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)

    body = _minimal_body()
    resp = client.post(
        "/agui/v1/run",
        json=body,
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    # Content type comes from EventEncoder.get_content_type()
    assert "text/event-stream" in resp.headers["content-type"]


def test_absent_accept_header_defaults_to_sse(client, monkeypatch):
    """Absent Accept header defaults to text/event-stream (unchanged from today)."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)

    body = _minimal_body()
    resp = client.post("/agui/v1/run", json=body)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
