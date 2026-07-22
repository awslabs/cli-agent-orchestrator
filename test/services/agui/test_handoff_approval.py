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

    def test_message_redacts_raw_prompt(self, construct, emitter):
        # Metadata-only (docs/agui.md, "privacy tests"): the raw terminal prompt
        # body must not be retained on the Interrupt nor emitted in the
        # approval_card props. Only the classified category (reason) survives.
        raw = "\u2191/\u2193 to navigate SECRET-TOKEN-abc123"
        interrupt = construct.on_provider_waiting(
            terminal_id="t-1",
            provider="claude_code",
            raw_prompt=raw,
        )
        assert interrupt.message == ""
        assert "SECRET-TOKEN-abc123" not in interrupt.message
        assert interrupt.reason == "claude-code:permission_request"
        # The raw body is absent from the emitted approval_card props too.
        card_props = emitter.intents[0]["props"]
        assert card_props["message"] == ""
        assert "SECRET-TOKEN-abc123" not in str(card_props)

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

    def test_sanitize_edit_strips_cr_and_truncates_at_newline(self):
        # WS-2: an edited answer is a single line. CR is dropped and everything
        # after the first LF is discarded so it cannot auto-submit or smuggle a
        # follow-on command line into the live PTY.
        from cli_agent_orchestrator.services.agui.handoff_approval import _sanitize_edited_text

        assert _sanitize_edited_text("answer\nrm -rf ~") == "answer"
        assert _sanitize_edited_text("a\r\nb") == "a"
        assert _sanitize_edited_text("y\rrm -rf ~") == "yrm -rf ~"

    @pytest.mark.asyncio
    async def test_edit_cr_lf_neutralized_before_delivery(self, construct, delivery):
        # WS-2 (positive injection test): "y\rrm -rf ~" must reach the terminal
        # as a single CR/LF-free line — no separate Enter between "y" and the
        # command tail, so the picker cannot be made to submit + inject.
        interrupt = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="y\rrm -rf ~")
        sent = [c for c in delivery.calls if c[0] == "send_input"]
        assert sent == [("send_input", "t-1", "yrm -rf ~")]
        assert "\r" not in sent[0][2] and "\n" not in sent[0][2]

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

    def test_send_input_clears_line_then_delegates(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.agui.handoff_approval import (
            TerminalServiceAnswerDelivery,
        )

        events: List[Tuple[str, str, str]] = []
        monkeypatch.setattr(
            terminal_service,
            "send_special_key",
            lambda tid, key: events.append(("key", tid, key)) or True,
        )
        monkeypatch.setattr(
            terminal_service,
            "send_input",
            lambda tid, text: events.append(("input", tid, text)) or True,
        )

        TerminalServiceAnswerDelivery().send_input("t-9", "hello")
        # A line-clear (C-u) precedes the paste so a retry replaces, not appends.
        assert events == [("key", "t-9", "C-u"), ("input", "t-9", "hello")]

    def test_send_input_delivers_even_if_clear_fails(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.agui.handoff_approval import (
            TerminalServiceAnswerDelivery,
        )

        inputs: List[Tuple[str, str]] = []

        def _clear_fails(tid, key):
            raise RuntimeError("clear failed")

        monkeypatch.setattr(terminal_service, "send_special_key", _clear_fails)
        monkeypatch.setattr(
            terminal_service, "send_input", lambda tid, text: inputs.append((tid, text)) or True
        )

        # Best-effort clear: a failed clear must not block the actual delivery.
        TerminalServiceAnswerDelivery().send_input("t-9", "hello")
        assert inputs == [("t-9", "hello")]

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


# ---------------------------------------------------------------------------
# Variant A: delivery failure is retryable (P1) + off-loop delivery (P2)
# ---------------------------------------------------------------------------


class _FailingDelivery:
    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        raise RuntimeError("backend down")

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        raise RuntimeError("backend down")


class TestDeliveryFailureRetryable:
    """A delivery failure leaves the interrupt unresolved and retryable (P1)."""

    @pytest.mark.asyncio
    async def test_failure_raises_and_leaves_unresolved(self):
        from cli_agent_orchestrator.services.agui.handoff_approval import DeliveryError

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_FailingDelivery()
        )
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")

        with pytest.raises(DeliveryError):
            await construct.resume(interrupt.id, ApprovalDecision.APPROVE)

        # Retryable: not resolved, still open and mapped.
        assert not interrupt.resolved
        assert interrupt.outcome is None
        assert construct.get_interrupt(interrupt.id) is not None
        assert any(i.id == interrupt.id for i in construct.pending())

    @pytest.mark.asyncio
    async def test_retry_after_failure_succeeds(self):
        from cli_agent_orchestrator.services.agui.handoff_approval import DeliveryError

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_FailingDelivery()
        )
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        with pytest.raises(DeliveryError):
            await construct.resume(interrupt.id, ApprovalDecision.APPROVE)

        # Swap in a working delivery; the retry now resolves.
        working = MockAnswerDelivery()
        construct._answer_delivery = working
        result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert result.resolved
        assert result.outcome == "approve"
        assert len(working.calls) == 1

    @pytest.mark.asyncio
    async def test_delivery_runs_off_loop_via_to_thread(self, monkeypatch):
        import asyncio as _asyncio

        calls = {"n": 0}
        real_to_thread = _asyncio.to_thread

        async def _spy(fn, *args, **kwargs):
            calls["n"] += 1
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(_asyncio, "to_thread", _spy)

        delivery = MockAnswerDelivery()
        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        await construct.resume(interrupt.id, ApprovalDecision.APPROVE)

        # Delivery was dispatched off the event loop exactly once.
        assert calls["n"] == 1
        assert len(delivery.calls) == 1

    @pytest.mark.asyncio
    async def test_delivery_beats_concurrent_expire(self):
        """If expire() races in while delivery is in flight but delivery
        SUCCEEDS, the delivered decision wins (the terminal received the input);
        the raced expiry does not overwrite the recorded outcome."""

        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")

        class _ExpiringDelivery:
            def send_input(self, terminal_id, text, **kwargs):
                construct.expire(terminal_id)

            def send_special_key(self, terminal_id, key):
                construct.expire(terminal_id)

        construct._answer_delivery = _ExpiringDelivery()
        result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        # Delivery wins: the decision outcome is committed, not "expired".
        assert result.resolved
        assert result.outcome == "approve"

    @pytest.mark.asyncio
    async def test_slow_delivery_completes_and_commits(self, monkeypatch):
        """A slow delivery runs to completion and commits (no hard timeout, so
        no orphaned worker); the slow-path warning threshold is exercised."""
        import time as _time

        from cli_agent_orchestrator.services.agui import handoff_approval as _mod

        # Warn threshold below the delivery duration so the slow-warn branch fires.
        monkeypatch.setattr(_mod, "_DELIVERY_SLOW_WARN_SECONDS", 0.01)

        class _SlowDelivery:
            def send_input(self, terminal_id, text, **kwargs):
                _time.sleep(0.05)

            def send_special_key(self, terminal_id, key):
                _time.sleep(0.05)

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_SlowDelivery()
        )
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")

        result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        # No DeliveryError: the delivery completed and the decision committed.
        assert result.resolved
        assert result.outcome == "approve"

    @pytest.mark.asyncio
    async def test_expire_during_failed_delivery_honors_expiry(self):
        """If expire() races in while a delivery is in flight and that delivery
        then FAILS, expiry wins: the failure is NOT advertised as retryable
        (no DeliveryError) — resume returns the expired interrupt. Regression
        for the 'retryable contract vs concurrent expiry' race."""
        import threading

        from cli_agent_orchestrator.services.agui.handoff_approval import DeliveryError

        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
        interrupt = construct.on_provider_waiting("t-x", "codex", "Approve execution? (y/n)")

        class _ControlledFailure:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def send_input(self, terminal_id, text, **kwargs):
                self.started.set()
                self.release.wait()
                raise RuntimeError("backend down")

            def send_special_key(self, terminal_id, key):
                return self.send_input(terminal_id, key)

        delivery = _ControlledFailure()
        construct._answer_delivery = delivery

        task = asyncio.create_task(construct.resume(interrupt.id, ApprovalDecision.APPROVE))
        await asyncio.to_thread(delivery.started.wait)
        # Expire mid-flight, then let the delivery fail.
        construct.expire("t-x")
        delivery.release.set()

        # No DeliveryError surfaces — expiry is reconciled.
        result = await task
        assert result.resolved
        assert result.outcome == "expired"

        # A subsequent resume is idempotent on the expired state (not retryable).
        retry = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert retry.outcome == "expired"

    @pytest.mark.asyncio
    async def test_concurrent_resume_same_interrupt_joins_single_delivery(self):
        """Two concurrent resumes of the SAME interrupt share one authoritative
        delivery: the second JOINS the in-flight task rather than starting a
        second (contrary) delivery. Both callers observe the same outcome, and
        exactly one keystroke reaches the terminal.

        (Replaces the old cross-interrupt serialization test: per-interrupt —
        not construct-wide — scoping is now the contract, so different terminals
        no longer block each other; see test_stuck_delivery_does_not_block_
        other_terminal for P2.)
        """
        import threading

        first_in_delivery = threading.Event()
        release_first = threading.Event()

        class _SeqDelivery:
            def __init__(self) -> None:
                self.calls = 0

            def send_input(self, terminal_id, text, **kwargs):
                self.calls += 1
                first_in_delivery.set()
                release_first.wait()

            def send_special_key(self, terminal_id, key):
                return True

        delivery = _SeqDelivery()
        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
        i1 = construct.on_provider_waiting("t-a", "kiro_cli", "Allow this action? [y/n/t]:")

        t1 = asyncio.create_task(construct.resume(i1.id, ApprovalDecision.APPROVE))
        await asyncio.to_thread(first_in_delivery.wait)
        # Contrary decision arrives while the first delivery is still in flight.
        t2 = asyncio.create_task(construct.resume(i1.id, ApprovalDecision.DENY))
        await asyncio.sleep(0.05)  # give t2 a chance to (wrongly) start its own delivery

        release_first.set()
        r1, r2 = await asyncio.gather(t1, t2)
        # Exactly one delivery; both observe the first (approve) outcome.
        assert delivery.calls == 1
        assert r1.outcome == "approve"
        assert r2.outcome == "approve"


# ---------------------------------------------------------------------------
# Cancellation & blast-radius safety (fanhongy P1 + P2)
# ---------------------------------------------------------------------------


class TestCancellationSafety:
    """P1: a cancelled awaiter must not let a contrary retry overtake the
    shielded in-flight delivery. P2: a stuck delivery for one interrupt must not
    block approvals for a different terminal."""

    @pytest.mark.asyncio
    async def test_cancel_then_contrary_retry_does_not_overtake_delivery(self):
        """P1 (blocking): cancelling an awaiting ``resume()`` — e.g. when an
        ``/agui/v1/run`` stream disconnects — must NOT let a contrary retry
        deliver and commit before the shielded in-flight delivery lands.

        Reproduces the reviewer's ``[C-u, C-u, n, y]`` sequence: without the
        cancellation shield, the cancelled approve worker pastes ``y`` AFTER the
        retry committed ``deny`` and pasted ``n``. With the shield the first
        (authoritative) decision wins, the retry joins it, and no opposite
        keystroke is ever sent.
        """
        import threading

        order: List[str] = []
        first_in_delivery = threading.Event()
        release_first = threading.Event()

        class _PausingDelivery:
            def __init__(self) -> None:
                self.calls = 0

            def send_input(self, terminal_id, text, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    first_in_delivery.set()
                    release_first.wait()
                order.append(text)

            def send_special_key(self, terminal_id, key):
                return True

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_PausingDelivery()
        )
        interrupt = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")

        approve = asyncio.create_task(construct.resume(interrupt.id, ApprovalDecision.APPROVE))
        await asyncio.to_thread(first_in_delivery.wait)

        # The awaiting resume() is cancelled (stream disconnect). The shielded
        # delivery+commit task must survive.
        approve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await approve

        # A contrary retry arrives while the shielded approve delivery is still
        # in flight; it must JOIN it, not start a second (deny) delivery.
        retry = asyncio.create_task(construct.resume(interrupt.id, ApprovalDecision.DENY))
        await asyncio.sleep(0.05)  # give the retry a chance to (wrongly) deliver "n"

        # Release the original (shielded) delivery; let everything settle.
        release_first.set()
        result = await retry

        # Exactly one delivery, and it was the approve ("y"); the contrary deny
        # ("n") never reached the terminal, and the committed outcome is approve.
        assert order == ["y"]
        assert result.resolved
        assert result.outcome == "approve"
        assert construct.get_interrupt(interrupt.id).outcome == "approve"

    @pytest.mark.asyncio
    async def test_stuck_delivery_does_not_block_other_terminal(self):
        """P2: a stuck delivery for one interrupt must not block approvals for a
        different terminal. The construct lock is held only for check+register,
        not across the unbounded backend delivery, so the blast radius of a hung
        backend is bounded to its own interrupt."""
        import threading

        release_a = threading.Event()

        class _StuckForA:
            def send_input(self, terminal_id, text, **kwargs):
                if terminal_id == "t-a":
                    release_a.wait()

            def send_special_key(self, terminal_id, key):
                return True

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_StuckForA()
        )
        ia = construct.on_provider_waiting("t-a", "kiro_cli", "Allow this action? [y/n/t]:")
        ib = construct.on_provider_waiting("t-b", "kiro_cli", "Allow this action? [y/n/t]:")

        a = asyncio.create_task(construct.resume(ia.id, ApprovalDecision.APPROVE))
        await asyncio.sleep(0.02)  # let A get stuck in delivery

        # B must resolve even though A's delivery is stuck. On the pre-fix
        # construct-wide lock this wait_for times out (P2 reproduction).
        b = await asyncio.wait_for(construct.resume(ib.id, ApprovalDecision.APPROVE), timeout=2.0)
        assert b.resolved and b.outcome == "approve"
        # A is still in flight / unresolved until released.
        assert not construct.get_interrupt(ia.id).resolved

        release_a.set()
        await a
        assert construct.get_interrupt(ia.id).resolved

    @pytest.mark.asyncio
    async def test_same_terminal_cross_interrupt_deliveries_serialize(self):
        """A delivery worker outlives its interrupt's registry state: a status
        flap can expire the original interrupt mid-delivery and create a
        REPLACEMENT interrupt for the same terminal. Per-interrupt join does
        not cover that pair, so without per-terminal serialization the two
        workers would interleave keystrokes on one PTY ([C-u, C-u, n, y]).
        The replacement's delivery must WAIT for the stuck worker to finish."""
        import threading

        order: List[str] = []
        first_in_delivery = threading.Event()
        release_first = threading.Event()

        class _StickyFirst:
            def __init__(self) -> None:
                self.calls = 0

            def send_input(self, terminal_id, text, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    order.append("first-start")
                    first_in_delivery.set()
                    release_first.wait()
                    order.append("first-end")
                else:
                    order.append(f"second:{text}")

            def send_special_key(self, terminal_id, key):
                return True

        delivery = _StickyFirst()
        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
        ia = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")

        a = asyncio.create_task(construct.resume(ia.id, ApprovalDecision.APPROVE))
        await asyncio.to_thread(first_in_delivery.wait)

        # Status flap while A's worker is stuck mid-delivery: the terminal
        # blips out of WAITING (expire) and back in (replacement interrupt).
        construct.expire("t-1")
        ib = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        assert ib.id != ia.id

        b = asyncio.create_task(construct.resume(ib.id, ApprovalDecision.DENY))
        await asyncio.sleep(0.05)  # give B a chance to (wrongly) deliver concurrently

        # B must NOT have delivered while A's worker is still in flight.
        assert order == ["first-start"]

        release_first.set()
        ra, rb = await asyncio.gather(a, b)

        # Strict serialization: A's worker fully completes before B delivers.
        assert order == ["first-start", "first-end", "second:n"]
        # Records reflect delivered reality: A's successful delivery overwrote
        # the flap expiry (delivery-beats-expire); B delivered deny afterwards.
        assert ra.outcome == "approve"
        assert rb.outcome == "deny"

    @pytest.mark.asyncio
    async def test_queued_delivery_honors_expiry_before_sending(self):
        """If an interrupt expires while its delivery is QUEUED behind a stuck
        sibling delivery on the same terminal, nothing has been sent for it, so
        expiry wins with zero keystrokes (symmetric with the failed-delivery
        expiry rule)."""
        import threading

        first_in_delivery = threading.Event()
        release_first = threading.Event()

        class _StickyFirst:
            def __init__(self) -> None:
                self.calls = 0

            def send_input(self, terminal_id, text, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    first_in_delivery.set()
                    release_first.wait()

            def send_special_key(self, terminal_id, key):
                return True

        delivery = _StickyFirst()
        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
        ia = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")

        a = asyncio.create_task(construct.resume(ia.id, ApprovalDecision.APPROVE))
        await asyncio.to_thread(first_in_delivery.wait)

        construct.expire("t-1")
        ib = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        b = asyncio.create_task(construct.resume(ib.id, ApprovalDecision.DENY))
        await asyncio.sleep(0.05)  # let B queue behind A's stuck delivery

        # B expires while queued (second flap) — before anything was sent for it.
        construct.expire("t-1")

        release_first.set()
        ra, rb = await asyncio.gather(a, b)

        assert ra.outcome == "approve"
        # Zero keystrokes for B: expiry won while it was still queued.
        assert rb.outcome == "expired"
        assert delivery.calls == 1
        # Refcount cleanup: both A (delivered) and B (expiry-before-send) decremented.
        assert "t-1" not in construct._delivery_locks


# ---------------------------------------------------------------------------
# Item 1 — In-flight watchdog tests
# ---------------------------------------------------------------------------


class TestInFlightWatchdog:
    """Tests for the active-hang watchdog (loop.call_later while delivery runs)."""

    @pytest.mark.asyncio
    async def test_slow_delivery_fires_inflight_warning_before_completion(
        self, monkeypatch, caplog
    ):
        """A slow delivery triggers the in-flight watchdog WARNING BEFORE the
        worker completes — not just the post-hoc duration warning."""
        import logging
        import threading
        import time as _time

        from cli_agent_orchestrator.services.agui import handoff_approval as _mod

        # Tiny threshold so the watchdog fires quickly.
        monkeypatch.setattr(_mod, "_DELIVERY_SLOW_WARN_SECONDS", 0.02)

        worker_started = threading.Event()
        release_worker = threading.Event()
        watchdog_fired_before_completion = False

        class _ControlledSlow:
            def send_input(self, terminal_id, text, **kwargs):
                worker_started.set()
                release_worker.wait()

            def send_special_key(self, terminal_id, key):
                worker_started.set()
                release_worker.wait()
                return True

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_ControlledSlow()
        )
        interrupt = construct.on_provider_waiting("t-wd1", "claude_code", "Allow? [y/n]")

        async def _drive():
            nonlocal watchdog_fired_before_completion
            task = asyncio.create_task(construct.resume(interrupt.id, ApprovalDecision.APPROVE))
            # Wait for worker to start
            await asyncio.to_thread(worker_started.wait)
            # Give the watchdog time to fire (threshold is 0.02s)
            await asyncio.sleep(0.08)
            # Check logs for in-flight warning BEFORE releasing the worker
            watchdog_fired_before_completion = any(
                "in-flight" in r.message and "t-wd1" in r.message
                for r in caplog.records
                if r.levelno >= logging.WARNING
            )
            release_worker.set()
            return await task

        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.agui.handoff_approval"
        ):
            result = await _drive()

        assert watchdog_fired_before_completion, "Watchdog should fire BEFORE worker completes"
        assert result.resolved
        assert result.outcome == "approve"

    @pytest.mark.asyncio
    async def test_fast_delivery_no_watchdog_warning(self, monkeypatch, caplog):
        """A fast delivery does not trigger the in-flight watchdog — the timer
        is cancelled before it fires."""
        import logging

        from cli_agent_orchestrator.services.agui import handoff_approval as _mod

        # Threshold well above the delivery time
        monkeypatch.setattr(_mod, "_DELIVERY_SLOW_WARN_SECONDS", 10.0)

        class _FastDelivery:
            def send_input(self, terminal_id, text, **kwargs):
                pass

            def send_special_key(self, terminal_id, key):
                return True

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_FastDelivery()
        )
        interrupt = construct.on_provider_waiting("t-wd2", "claude_code", "Allow? [y/n]")

        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.agui.handoff_approval"
        ):
            result = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)

        # No in-flight warning should have been emitted
        inflight_warnings = [
            r for r in caplog.records if "in-flight" in r.message and r.levelno >= logging.WARNING
        ]
        assert not inflight_warnings, "Fast delivery should not trigger watchdog"
        assert result.resolved
        assert result.outcome == "approve"

    @pytest.mark.asyncio
    async def test_watchdog_fires_but_delivery_succeeds(self, monkeypatch, caplog):
        """The watchdog fires but delivery then completes normally — outcome
        still commits correctly (watchdog is observability-only)."""
        import logging
        import time as _time

        from cli_agent_orchestrator.services.agui import handoff_approval as _mod

        monkeypatch.setattr(_mod, "_DELIVERY_SLOW_WARN_SECONDS", 0.01)

        class _SlowButSucceeds:
            def send_input(self, terminal_id, text, **kwargs):
                _time.sleep(0.05)

            def send_special_key(self, terminal_id, key):
                _time.sleep(0.05)
                return True

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_SlowButSucceeds()
        )
        interrupt = construct.on_provider_waiting("t-wd3", "claude_code", "Allow? [y/n]")

        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.agui.handoff_approval"
        ):
            result = await construct.resume(interrupt.id, ApprovalDecision.DENY)

        # Watchdog fired (in-flight warning present)
        inflight_warnings = [
            r for r in caplog.records if "in-flight" in r.message and r.levelno >= logging.WARNING
        ]
        assert inflight_warnings, "Watchdog should fire for slow delivery"
        # But delivery still succeeded
        assert result.resolved
        assert result.outcome == "deny"


# ---------------------------------------------------------------------------
# Round 6 — P1: sanitized-empty edit rejection + P2: lock cleanup
# ---------------------------------------------------------------------------


class TestSanitizedEmptyEditRejection:
    """An edit that sanitizes to empty must be rejected, not delivered."""

    @pytest.mark.asyncio
    async def test_leading_newline_edit_rejected(self, construct):
        """edited_text='\\nrm -rf ~' passes raw validation but sanitizes to
        empty — must raise ValueError, not deliver."""
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        with pytest.raises(ValueError, match="empty after sanitization"):
            await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="\nrm -rf ~")
        assert not interrupt.resolved

    @pytest.mark.asyncio
    async def test_control_only_edit_rejected(self, construct):
        """edited_text containing only control chars sanitizes to empty."""
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        with pytest.raises(ValueError, match="empty after sanitization"):
            await construct.resume(interrupt.id, ApprovalDecision.EDIT, edited_text="\x01\x02\x03")
        assert not interrupt.resolved

    @pytest.mark.asyncio
    async def test_valid_edit_after_sanitization_succeeds(self, construct):
        """An edit with valid content after sanitization delivers normally."""
        interrupt = construct.on_provider_waiting("t-1", "claude_code", "\u2191/\u2193 to navigate")
        result = await construct.resume(
            interrupt.id, ApprovalDecision.EDIT, edited_text="valid command"
        )
        assert result.resolved
        assert result.outcome == "edit"


class TestDeliveryLockCleanup:
    """Per-terminal delivery locks are cleaned up after delivery completes."""

    @pytest.mark.asyncio
    async def test_lock_removed_after_successful_delivery(self):
        """A terminal's delivery lock is removed once no delivery is in flight."""
        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=MockAnswerDelivery()
        )
        interrupt = construct.on_provider_waiting(
            "t-cleanup", "claude_code", "\u2191/\u2193 to navigate"
        )
        await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        # Lock should be cleaned up since no other delivery is queued
        assert "t-cleanup" not in construct._delivery_locks

    @pytest.mark.asyncio
    async def test_lock_count_bounded_after_many_terminals(self):
        """After approvals for many terminals, lock count stays bounded."""
        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=MockAnswerDelivery()
        )
        for i in range(50):
            interrupt = construct.on_provider_waiting(
                f"t-{i}", "claude_code", "\u2191/\u2193 to navigate"
            )
            await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        # All locks should be cleaned up (no concurrent deliveries)
        assert len(construct._delivery_locks) == 0

    @pytest.mark.asyncio
    async def test_three_delivery_refcount_survives_queued_waiter(self):
        """Regression for the waiter-pop race: A holding, B queued, A releases
        (flawed cleanup popped the entry here), C arrives while B delivers.
        With ref-counting, C must join the SAME entry and serialize behind B."""
        import threading
        from typing import List

        order: List[str] = []
        first_in, release_first = threading.Event(), threading.Event()
        second_in, release_second = threading.Event(), threading.Event()

        class _StickyFirstTwo:
            def __init__(self) -> None:
                self.calls = 0

            def send_input(self, terminal_id, text, **kwargs):
                self.calls += 1
                n = self.calls
                order.append(f"start:{n}")
                if n == 1:
                    first_in.set()
                    release_first.wait()
                elif n == 2:
                    second_in.set()
                    release_second.wait()
                order.append(f"end:{n}")

            def send_special_key(self, terminal_id, key):
                self.calls += 1
                n = self.calls
                order.append(f"start:{n}")
                if n == 1:
                    first_in.set()
                    release_first.wait()
                elif n == 2:
                    second_in.set()
                    release_second.wait()
                order.append(f"end:{n}")
                return True

        delivery = _StickyFirstTwo()
        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)

        ia = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        a = asyncio.create_task(construct.resume(ia.id, ApprovalDecision.APPROVE))
        await asyncio.to_thread(first_in.wait)  # A is mid-delivery

        # Flap 1 while A is stuck: B becomes a QUEUED WAITER (refs now 2).
        construct.expire("t-1")
        ib = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        b = asyncio.create_task(construct.resume(ib.id, ApprovalDecision.DENY))
        await asyncio.sleep(0.05)  # let B reach the lock queue

        # A releases. The FLAWED cleanup popped the dict entry at this exact
        # moment (locked() is False while B is only scheduled, not yet holding).
        release_first.set()
        await asyncio.to_thread(second_in.wait)  # B is now mid-delivery

        # Flap 2 while B is stuck: C arrives. Old code: fresh lock -> C delivers
        # CONCURRENTLY with B. Refcounted code: same entry, C queues behind B.
        construct.expire("t-1")
        ic = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        c = asyncio.create_task(construct.resume(ic.id, ApprovalDecision.APPROVE))
        await asyncio.sleep(0.05)  # give C the chance to (wrongly) interleave

        # C must NOT have started while B is still in flight.
        assert order == ["start:1", "end:1", "start:2"]

        release_second.set()
        ra, rb, rc = await asyncio.gather(a, b, c)

        # Strict serialization, all THREE actually delivered.
        assert order == ["start:1", "end:1", "start:2", "end:2", "start:3", "end:3"]
        assert delivery.calls == 3
        # Delivery-beats-expire: each flap expiry is overwritten by the delivery.
        assert ra.outcome == "approve"
        assert rb.outcome == "deny"
        assert rc.outcome == "approve"
        # Entry fully released and cleaned up.
        assert "t-1" not in construct._delivery_locks

    @pytest.mark.asyncio
    async def test_failure_path_cleans_up_lock(self):
        """DeliveryError propagates AND the terminal's lock entry is removed."""
        from cli_agent_orchestrator.services.agui.handoff_approval import DeliveryError

        class _FailDelivery:
            def send_input(self, terminal_id, text, **kwargs):
                raise RuntimeError("backend down")

            def send_special_key(self, terminal_id, key):
                raise RuntimeError("backend down")

        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=_FailDelivery()
        )
        interrupt = construct.on_provider_waiting(
            "t-fail", "claude_code", "\u2191/\u2193 to navigate"
        )
        with pytest.raises(DeliveryError):
            await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
        assert "t-fail" not in construct._delivery_locks

    @pytest.mark.asyncio
    async def test_resolved_before_resume_creates_no_lock(self):
        """An interrupt resolved (expired) before resume hits the idempotent
        early-return — no lock entry is ever created."""
        construct = AgentHandoffWithApproval(
            emitter=RecordingUiEmitter(), answer_delivery=MockAnswerDelivery()
        )
        i1 = construct.on_provider_waiting("t-exp", "claude_code", "\u2191/\u2193 to navigate")
        # Expire i1 before resuming — hits idempotent early-return in resume()
        construct.expire("t-exp")

        r1 = await construct.resume(i1.id, ApprovalDecision.APPROVE)
        assert r1.outcome == "expired"
        assert "t-exp" not in construct._delivery_locks
