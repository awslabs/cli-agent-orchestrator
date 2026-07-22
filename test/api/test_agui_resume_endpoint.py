"""Tests for POST /agui/v1/interrupts/{id}/resume endpoint.

Guard matrix:
- 404 when AG-UI surface disabled
- 404 for unknown interrupt_id
- 422 for invalid decision
- 422 for edit validation failures
- Idempotent 200 on re-resume
- Scope check (cao:write or cao:admin required)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    ApprovalDecision,
)

# Host in ALLOWED_HOSTS so TrustedHostMiddleware admits the request.
client = TestClient(app, base_url="http://localhost")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    """Enable AG-UI surface and disable auth for tests."""
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)


@pytest.fixture
def approval_bridge():
    """Create and wire a fresh approval bridge onto app.state."""
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)
    bridge = ApprovalBridge(construct=construct)
    app.state.approval_bridge = bridge
    yield bridge
    # Cleanup
    if hasattr(app.state, "approval_bridge"):
        del app.state.approval_bridge


# ---------------------------------------------------------------------------
# Gate: 404 when surface disabled
# ---------------------------------------------------------------------------


class TestSurfaceGate:
    """Surface gate returns 404 when AG-UI is disabled."""

    def test_404_when_disabled(self, monkeypatch):
        monkeypatch.setenv("CAO_AGUI_ENABLED", "false")
        monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
        resp = client.post(
            "/agui/v1/interrupts/some-id/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 404 for unknown interrupt
# ---------------------------------------------------------------------------


class TestUnknownInterrupt:
    """Returns 404 for unknown interrupt_id."""

    def test_unknown_id(self, approval_bridge):
        resp = client.post(
            "/agui/v1/interrupts/nonexistent-uuid/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 404
        assert "Unknown interrupt" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 422 for invalid decision
# ---------------------------------------------------------------------------


class TestInvalidDecision:
    """Returns 422 for invalid decision values."""

    def test_invalid_decision_value(self, approval_bridge):
        # Create an interrupt first
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "invalid_decision"},
        )
        assert resp.status_code == 422

    def test_unsupported_decision_for_category(self, approval_bridge):
        """Edit not supported for trust_prompt (only approve/deny)."""
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "Yes, I trust this folder")
        assert "edit" not in interrupt.options
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit", "edited_text": "something"},
        )
        assert resp.status_code == 422
        assert "not supported" in resp.json()["detail"]

    def test_edit_without_text(self, approval_bridge):
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit"},
        )
        assert resp.status_code == 422
        assert "non-empty" in resp.json()["detail"]

    def test_edit_with_too_long_text(self, approval_bridge):
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit", "edited_text": "x" * 4001},
        )
        assert resp.status_code == 422
        assert "too long" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Successful resume
# ---------------------------------------------------------------------------


class TestSuccessfulResume:
    """200 on valid resume."""

    def test_approve(self, approval_bridge):
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["resolved"] is True
        assert body["outcome"] == "approve"
        assert body["interrupt_id"] == interrupt.id

    def test_deny(self, approval_bridge):
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "codex", "Approve execution? (y/n)")
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "deny"},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "deny"

    def test_edit(self, approval_bridge):
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit", "edited_text": "custom command"},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "edit"


# ---------------------------------------------------------------------------
# Idempotent resume
# ---------------------------------------------------------------------------


class TestIdempotentResume:
    """Re-resume returns recorded outcome."""

    def test_second_resume_returns_same_outcome(self, approval_bridge):
        construct = approval_bridge.construct
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        # First resume
        resp1 = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "approve"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["outcome"] == "approve"

        # Second resume with different decision
        resp2 = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "deny"},
        )
        assert resp2.status_code == 200
        # Returns the recorded first outcome
        assert resp2.json()["outcome"] == "approve"


# ---------------------------------------------------------------------------
# Bridge not initialized
# ---------------------------------------------------------------------------


class TestBridgeNotInitialized:
    """Returns 404 when approval bridge is not initialized on app.state."""

    def test_no_bridge(self, monkeypatch):
        # Remove bridge from app.state if set
        if hasattr(app.state, "approval_bridge"):
            delattr(app.state, "approval_bridge")
        resp = client.post(
            "/agui/v1/interrupts/some-id/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 404
        assert "not initialized" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Delivery: resume must actually reach the waiting terminal (exactly once)
# ---------------------------------------------------------------------------


class TestDeliveryOnResume:
    """Resolving an interrupt delivers the decision to the CLI exactly once.

    Regression guard for the P1 bug where the production construct was wired
    with ``answer_delivery=None``, so resume reported success without ever
    sending approve/deny/edit to the terminal.
    """

    @pytest.fixture
    def delivery_bridge(self, monkeypatch):
        """Bridge whose construct uses the real terminal-service-backed delivery,
        with terminal_service patched to count deliveries."""
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.agui.handoff_approval import (
            TerminalServiceAnswerDelivery,
        )

        deliveries: list = []
        monkeypatch.setattr(
            terminal_service,
            "send_input",
            lambda tid, text: deliveries.append(("input", tid, text)) or True,
        )
        monkeypatch.setattr(
            terminal_service,
            "send_special_key",
            lambda tid, key: deliveries.append(("key", tid, key)) or True,
        )

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(),
            answer_delivery=TerminalServiceAnswerDelivery(),
        )
        bridge = ApprovalBridge(construct=construct)
        app.state.approval_bridge = bridge
        yield bridge, deliveries
        if hasattr(app.state, "approval_bridge"):
            del app.state.approval_bridge

    def test_approve_delivers_exactly_once(self, delivery_bridge):
        bridge, deliveries = delivery_bridge
        interrupt = bridge.construct.on_provider_waiting(
            "t-deliver", "claude_code", "\u2191/\u2193 to navigate"
        )

        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "approve"

        # Exactly one delivery reached the waiting terminal.
        assert len(deliveries) == 1
        assert deliveries[0][1] == "t-deliver"

        # Idempotent re-resume must NOT deliver again.
        resp2 = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "deny"},
        )
        assert resp2.status_code == 200
        assert len(deliveries) == 1

    def test_edit_delivers_translated_text_once(self, delivery_bridge):
        bridge, deliveries = delivery_bridge
        interrupt = bridge.construct.on_provider_waiting(
            "t-edit", "claude_code", "\u2191/\u2193 to navigate"
        )

        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit", "edited_text": "custom command"},
        )
        assert resp.status_code == 200
        # Text delivery is preceded by a line-clear (C-u) so a retry replaces
        # rather than appends; the command itself is delivered exactly once.
        inputs = [d for d in deliveries if d[0] == "input"]
        assert len(inputs) == 1
        _, tid, payload = inputs[0]
        assert tid == "t-edit"
        assert "custom command" in payload
        # The clear precedes the paste.
        assert deliveries[0] == ("key", "t-edit", "C-u")


# ---------------------------------------------------------------------------
# Delivery failure (P1): failure must NOT be reported as a successful resolution
# ---------------------------------------------------------------------------


class _FailingDelivery:
    def send_input(self, terminal_id, text, **kwargs):
        raise RuntimeError("backend down")

    def send_special_key(self, terminal_id, key):
        raise RuntimeError("backend down")


class TestDeliveryFailureRest:
    """REST resume returns a non-success status when delivery fails (retryable)."""

    def test_delivery_failure_returns_502_and_stays_unresolved(self):
        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_FailingDelivery()
        )
        bridge = ApprovalBridge(construct=construct)
        app.state.approval_bridge = bridge
        try:
            interrupt = construct.on_provider_waiting(
                "t-1", "claude_code", "\u2191/\u2193 to navigate"
            )
            resp = client.post(
                f"/agui/v1/interrupts/{interrupt.id}/resume",
                json={"decision": "approve"},
            )
            # Failure surfaced, not reported as success.
            assert resp.status_code == 502
            # Machine-readable retryable flag in the body.
            assert resp.json()["detail"]["retryable"] is True
            # Interrupt left unresolved / retryable.
            assert not interrupt.resolved
            assert construct.get_interrupt(interrupt.id) is not None
        finally:
            if hasattr(app.state, "approval_bridge"):
                del app.state.approval_bridge


def test_leading_newline_edit_returns_422(client, monkeypatch):
    """An edit with leading newline that sanitizes to empty returns 422."""
    import cli_agent_orchestrator.api.main as main_mod

    monkeypatch.setattr(main_mod, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)

    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
    bridge = ApprovalBridge(construct=construct)
    app.state.approval_bridge = bridge

    try:
        resp = client.post(
            f"/agui/v1/interrupts/{interrupt.id}/resume",
            json={"decision": "edit", "edited_text": "\nrm -rf ~"},
        )
        assert resp.status_code == 422
        assert "empty after sanitization" in resp.json()["detail"]
        assert not interrupt.resolved
    finally:
        if hasattr(app.state, "approval_bridge"):
            del app.state.approval_bridge
