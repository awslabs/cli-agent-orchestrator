"""Coverage for AG-UI endpoint/lifespan branches not hit by the unit-level
construct tests (they exercise the underlying functions directly rather than the
HTTP handlers + app lifespan wiring).

Covers:
- Lifespan wiring: the ApprovalBridge is started under CAO_AGUI_ENABLED and
  cancelled on shutdown.
- Resume route: the ``except ValueError`` → HTTP 422 branch.
- Run route: ``snapshot_fn`` iterating real sessions/terminals and the
  ``_bus_events`` drain, consumed end-to-end via a terminating bus.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main


@pytest.fixture(autouse=True)
def _only_agui(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)


class _TerminatingBus:
    """Bus whose drain yields one event then ends (covers the live-yield path)."""

    def register(self, overflow_close=False):  # noqa: ANN001
        return object()

    def unregister(self, queue):  # noqa: ANN001
        pass

    async def drain(self, queue):  # noqa: ANN001
        yield {
            "id": "e1",
            "kind": "agent_started",
            "terminal_id": "t1",
            "session_name": "cao-x",
            "detail": {},
        }


def test_lifespan_starts_and_stops_approval_bridge():
    # Entering the context runs lifespan startup (wires the ApprovalBridge under
    # CAO_AGUI_ENABLED); exiting runs shutdown (cancels the bridge task).
    from cli_agent_orchestrator.services.agui.handoff_approval import (
        TerminalServiceAnswerDelivery,
    )

    with TestClient(main.app, base_url="http://localhost") as client:
        assert client.get("/health").status_code == 200
        assert hasattr(main.app.state, "approval_bridge")
        # Regression guard (P1): the production construct must be wired with a
        # real terminal-service-backed delivery so resume actually reaches the
        # CLI, not answer_delivery=None.
        construct = main.app.state.approval_bridge.construct
        assert isinstance(construct._answer_delivery, TerminalServiceAnswerDelivery)


def test_resume_returns_422_on_edit_validation_error():
    from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
    from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval

    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    main.app.state.approval_bridge = ApprovalBridge(construct=construct)
    try:
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        client = TestClient(main.app, base_url="http://localhost")
        # Valid decision ("edit") but empty edited_text → construct.resume raises
        # ValueError → the endpoint's `except ValueError` → 422.
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit", "edited_text": ""},
        )
        assert resp.status_code == 422
    finally:
        if hasattr(main.app.state, "approval_bridge"):
            del main.app.state.approval_bridge


def test_run_endpoint_stream_builds_snapshot_and_drains_bus(monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _TerminatingBus()
    )
    # Sessions + terminals so snapshot_fn's loop body executes.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_sessions",
        lambda: [{"id": "s1", "name": "cao-x", "status": "active"}],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda sid: [
            {"id": "t1", "session_name": "cao-x", "provider": "mock_cli", "status": "idle"}
        ],
    )
    client = TestClient(main.app, base_url="http://localhost")
    body = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    frames = []
    with client.stream("POST", "/agui/v1/run", json=body) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            frames.append(line)
    joined = "\n".join(frames)
    assert "RUN_STARTED" in joined
    assert "RUN_FINISHED" in joined


def test_run_endpoint_snapshot_tolerates_terminal_lookup_failure(monkeypatch):
    """snapshot_fn's per-session try/except swallows a terminal-lookup failure."""

    class _EmptyBus(_TerminatingBus):
        async def drain(self, queue):  # noqa: ANN001
            return
            yield  # pragma: no cover

    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _EmptyBus())
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_sessions",
        lambda: [{"id": "s1", "name": "cao-x", "status": "active"}],
    )

    def _boom(sid):
        raise RuntimeError("terminal lookup failed")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.list_terminals_by_session", _boom)
    client = TestClient(main.app, base_url="http://localhost")
    body = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with client.stream("POST", "/agui/v1/run", json=body) as resp:
        assert resp.status_code == 200
        frames = [line for line in resp.iter_lines()]
    assert any("RUN_STARTED" in f for f in frames)
