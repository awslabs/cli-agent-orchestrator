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
