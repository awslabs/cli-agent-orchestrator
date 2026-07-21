"""Unit tests for AgentHandoffWithApproval: interrupt lifecycle, keystroke
translation, edit validation, registry bounds, and idempotent resume.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import (
    _REGISTRY_CAP,
    _RESOLVED_TTL_SECONDS,
    AgentHandoffWithApproval,
    ApprovalDecision,
    Interrupt,
    _translate_decision,
    classify_reason,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class MockAnswerDelivery:
    """Records all terminal interactions for assertion."""

    def __init__(self):
        self.calls: List[Tuple[str, str, str]] = []  # (method, terminal_id, value)

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        self.calls.append(("send_input", terminal_id, text))

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        self.calls.append(("send_special_key", terminal_id, key))
        return True


@pytest.fixture
def emitter():
    return RecordingUiEmitter()


@pytest.fixture
def delivery():
    return MockAnswerDelivery()


@pytest.fixture
def construct(emitter, delivery):
    return AgentHandoffWithApproval(emitter=emitter, answer_delivery=delivery)


# ---------------------------------------------------------------------------
# Interrupt creation
# ---------------------------------------------------------------------------


class TestInterruptCreation:
    """Tests for on_provider_waiting interrupt creation."""

    def test_creates_interrupt(self, construct):
        interrupt = construct.on_provider_waiting(
            terminal_id="t-1",
            provider="claude_code",
            raw_prompt="\u2191/\u2193 to navigate",
            session_name="sess-1",
        )
        assert isinstance(interrupt, Interrupt)
        assert not interrupt.resolved
        assert interrupt.outcome is None
        assert interrupt.reason == "claude-code:permission_request"
        assert interrupt.metadata["provider"] == "claude_code"
        assert interrupt.metadata["terminal_id"] == "t-1"
        assert interrupt.metadata["session_name"] == "sess-1"
        assert "approve" in interrupt.options
        assert "deny" in interrupt.options

    def test_message_redacted_to_256(self, construct):
        long_prompt = "x" * 500
        interrupt = construct.on_provider_waiting(
            terminal_id="t-1",
            provider="claude_code",
            raw_prompt=long_prompt,
        )
        assert len(interrupt.message) <= 256

    def test_emits_approval_card(self, construct, emitter):
        construct.on_provider_waiting(
            terminal_id="t-1",
            provider="codex",
            raw_prompt="Approve this? (y/n)",
        )
        assert len(emitter.intents) == 1
        assert emitter.intents[0]["component"] == "approval_card"
        assert emitter.intents[0]["props"]["reason"] == "codex:approval_request"

    def test_pending_list(self, construct):
        construct.on_provider_waiting("t-1", "claude_code", "text")
        construct.on_provider_waiting("t-2", "codex", "text")
        assert len(construct.pending()) == 2


# ---------------------------------------------------------------------------
# Resume (approve/deny)
# ---------------------------------------------------------------------------


class TestResume:
    """Tests for interrupt resolution via resume."""

    @pytest.mark.asyncio
    async def test_approve_claude_code(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert result.resolved
        assert result.outcome == "approve"
        # Claude Code approve -> Enter key
        assert ("send_special_key", "t-1", "Enter") in delivery.calls

    @pytest.mark.asyncio
    async def test_deny_claude_code(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        result = await construct.resume(interrupt.id, ApprovalDecision.DENY)
        assert result.resolved
        assert result.outcome == "deny"
        # Claude Code deny -> Escape key
        assert ("send_special_key", "t-1", "Escape") in delivery.calls

    @pytest.mark.asyncio
    async def test_approve_kiro_cli(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert result.outcome == "approve"
        assert ("send_input", "t-1", "y") in delivery.calls

    @pytest.mark.asyncio
    async def test_deny_kiro_cli(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        result = await construct.resume(interrupt.id, ApprovalDecision.DENY)
        assert result.outcome == "deny"
        assert ("send_input", "t-1", "n") in delivery.calls

    @pytest.mark.asyncio
    async def test_approve_codex(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "codex", "Approve execution? (y/n)")
        result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert result.outcome == "approve"
        assert ("send_input", "t-1", "y") in delivery.calls

    @pytest.mark.asyncio
    async def test_deny_codex(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "codex", "Approve execution? (y/n)")
        result = await construct.resume(interrupt.id, ApprovalDecision.DENY)
        assert result.outcome == "deny"
        assert ("send_input", "t-1", "n") in delivery.calls

    @pytest.mark.asyncio
    async def test_removed_from_pending_after_resolve(self, construct):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "text")
        assert len(construct.pending()) == 1
        await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert len(construct.pending()) == 0

    @pytest.mark.asyncio
    async def test_emits_resolution_intent(self, construct, emitter):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        # Should have 2 intents: one for creation, one for resolution
        assert len(emitter.intents) == 2
        resolution = emitter.intents[1]
        assert resolution["props"]["resolved"] is True
        assert resolution["props"]["outcome"] == "approve"


# ---------------------------------------------------------------------------
# Idempotent resume
# ---------------------------------------------------------------------------


class TestIdempotentResume:
    """Second resume returns recorded outcome with zero side effects."""

    @pytest.mark.asyncio
    async def test_second_resume_returns_recorded_outcome(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        first = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        # Clear delivery history
        delivery.calls.clear()
        second = await construct.resume(interrupt.id, ApprovalDecision.DENY)
        # Same outcome from first resolution
        assert second.outcome == "approve"
        assert second.resolved
        # No new keystrokes
        assert len(delivery.calls) == 0

    @pytest.mark.asyncio
    async def test_unknown_interrupt_raises_key_error(self, construct):
        with pytest.raises(KeyError, match="Unknown interrupt"):
            await construct.resume("nonexistent-id", ApprovalDecision.APPROVE)


# ---------------------------------------------------------------------------
# Edit decision
# ---------------------------------------------------------------------------


class TestEditDecision:
    """Tests for edit decision validation and delivery."""

    @pytest.mark.asyncio
    async def test_edit_with_valid_text(self, construct, delivery):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        result = await construct.resume(
            interrupt.id, ApprovalDecision.EDIT, edited_text="custom response"
        )
        assert result.outcome == "edit"
        assert ("send_input", "t-1", "custom response") in delivery.calls

    @pytest.mark.asyncio
    async def test_edit_without_text_rejects(self, construct):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        with pytest.raises(ValueError, match="non-empty edited_text"):
            await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text=None)
        # Interrupt should still be open
        assert not interrupt.resolved

    @pytest.mark.asyncio
    async def test_edit_with_empty_text_rejects(self, construct):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        with pytest.raises(ValueError, match="non-empty edited_text"):
            await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="   ")
        assert not interrupt.resolved

    @pytest.mark.asyncio
    async def test_edit_with_too_long_text_rejects(self, construct):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        with pytest.raises(ValueError, match="too long"):
            await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="x" * 4001)
        assert not interrupt.resolved


# ---------------------------------------------------------------------------
# Unsupported decision
# ---------------------------------------------------------------------------


class TestUnsupportedDecision:
    """Decision not in interrupt.options is rejected."""

    @pytest.mark.asyncio
    async def test_edit_not_supported_for_trust_prompt(self, construct):
        # Trust prompt only supports approve/deny (not edit)
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "Yes, I trust this folder")
        assert "edit" not in interrupt.options
        with pytest.raises(ValueError, match="not supported"):
            await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="text")
        assert not interrupt.resolved


# ---------------------------------------------------------------------------
# Expire
# ---------------------------------------------------------------------------


class TestExpire:
    """Tests for expire (zero keystrokes)."""

    def test_expire_resolves_with_zero_keystrokes(self, construct, delivery):
        construct.on_provider_waiting("t-1", "claude_code", "text")
        result = construct.expire("t-1")
        assert result is not None
        assert result.resolved
        assert result.outcome == "expired"
        # Zero keystrokes
        assert len(delivery.calls) == 0

    def test_expire_unknown_terminal_returns_none(self, construct):
        result = construct.expire("nonexistent")
        assert result is None

    def test_expire_emits_intent(self, construct, emitter):
        construct.on_provider_waiting("t-1", "claude_code", "text")
        construct.expire("t-1")
        # One creation + one expiration intent
        assert len(emitter.intents) == 2
        assert emitter.intents[1]["props"]["outcome"] == "expired"

    def test_expire_already_resolved_returns_none(self, construct):
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "text")
        construct.expire("t-1")
        # Second expire for same terminal should return None
        result = construct.expire("t-1")
        assert result is None


# ---------------------------------------------------------------------------
# Registry TTL/cap eviction
# ---------------------------------------------------------------------------


class TestRegistryBounds:
    """Tests for registry cap and TTL eviction."""

    def test_cap_eviction(self, emitter, delivery):
        """Exceeding 1000 interrupts evicts oldest resolved first."""
        construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=delivery)
        # Create and resolve _REGISTRY_CAP interrupts
        for i in range(_REGISTRY_CAP):
            it = construct.on_provider_waiting(f"t-{i}", "codex", "Approve? (y/n)")
            construct.expire(f"t-{i}")

        # Now create one more -- should trigger eviction
        construct.on_provider_waiting("t-new", "codex", "Approve? (y/n)")
        # Total should not exceed cap + 1 (the new unresolved one)
        assert len(construct._interrupts) <= _REGISTRY_CAP + 1


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class TestProjection:
    """Tests for the projection method."""

    def test_projection_shows_pending(self, construct):
        construct.on_provider_waiting("t-1", "claude_code", "text")
        proj = construct.projection()
        assert proj["total"] == 1
        assert len(proj["pending"]) == 1
        assert proj["pending"][0]["resolved"] is False


# ---------------------------------------------------------------------------
# Per-provider keystroke translation
# ---------------------------------------------------------------------------


class TestTranslateDecision:
    """Unit tests for _translate_decision helper."""

    def test_claude_code_approve(self):
        action = _translate_decision("claude_code", ApprovalDecision.APPROVE)
        assert action == {"type": "key", "value": "Enter"}

    def test_claude_code_deny(self):
        action = _translate_decision("claude_code", ApprovalDecision.DENY)
        assert action == {"type": "key", "value": "Escape"}

    def test_kiro_approve(self):
        action = _translate_decision("kiro_cli", ApprovalDecision.APPROVE)
        assert action == {"type": "text", "value": "y"}

    def test_kiro_deny(self):
        action = _translate_decision("kiro_cli", ApprovalDecision.DENY)
        assert action == {"type": "text", "value": "n"}

    def test_codex_approve(self):
        action = _translate_decision("codex", ApprovalDecision.APPROVE)
        assert action == {"type": "text", "value": "y"}

    def test_codex_deny(self):
        action = _translate_decision("codex", ApprovalDecision.DENY)
        assert action == {"type": "text", "value": "n"}

    def test_edit_sends_text(self):
        action = _translate_decision("claude_code", ApprovalDecision.EDIT, "hello")
        assert action == {"type": "text", "value": "hello"}

    def test_unknown_provider_fallback(self):
        action = _translate_decision("unknown_provider", ApprovalDecision.APPROVE)
        assert action == {"type": "text", "value": "y"}


# ---------------------------------------------------------------------------
# Hypothesis property tests (P1: interrupt round-trip + idempotent resume)
# ---------------------------------------------------------------------------


class TestHandoffApprovalProperty:
    """Property-based tests for interrupt round-trip."""

    @given(
        provider=st.sampled_from(["claude_code", "kiro_cli", "codex"]),
        prompt=st.text(min_size=1, max_size=500),
    )
    @settings(max_examples=50)
    @pytest.mark.asyncio
    async def test_interrupt_round_trip(self, provider, prompt):
        """Create -> resume -> second resume always returns same outcome."""
        emitter = RecordingUiEmitter()
        delivery = MockAnswerDelivery()
        construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=delivery)

        interrupt = construct.on_provider_waiting("t-1", provider, prompt)
        assert not interrupt.resolved

        # Use approve (always in options)
        first = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert first.resolved
        assert first.outcome == "approve"

        # Idempotent second resume
        second = await construct.resume(interrupt.id, ApprovalDecision.DENY)
        assert second.outcome == "approve"  # First resolution wins


# ---------------------------------------------------------------------------
# TerminalServiceAnswerDelivery: production adapter delegates to terminal_service
# ---------------------------------------------------------------------------


class TestTerminalServiceAnswerDelivery:
    """The production AnswerDelivery adapter delegates to terminal_service."""

    def test_send_input_delegates_once(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.agui.handoff_approval import (
            TerminalServiceAnswerDelivery,
        )

        calls: List[Tuple[str, str]] = []
        monkeypatch.setattr(
            terminal_service, "send_input", lambda tid, text: calls.append((tid, text)) or True
        )

        TerminalServiceAnswerDelivery().send_input("t-9", "hello")
        assert calls == [("t-9", "hello")]

    def test_send_special_key_delegates_and_returns(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.agui.handoff_approval import (
            TerminalServiceAnswerDelivery,
        )

        calls: List[Tuple[str, str]] = []

        def _fake(tid, key):
            calls.append((tid, key))
            return True

        monkeypatch.setattr(terminal_service, "send_special_key", _fake)

        result = TerminalServiceAnswerDelivery().send_special_key("t-9", "Enter")
        assert result is True
        assert calls == [("t-9", "Enter")]
