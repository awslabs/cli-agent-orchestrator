"""Integration tests for ApprovalBridge with synthetic EventBus events.

Proves:
- on_provider_waiting is triggered when terminal enters WAITING_USER_ANSWER
- expire is called when terminal leaves WAITING_USER_ANSWER with open interrupt
- No-op when AG-UI surface is disabled
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
)
from cli_agent_orchestrator.services.event_bus import EventBus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_bus():
    """Create a fresh EventBus with a test event loop."""
    eb = EventBus()
    return eb


@pytest.fixture
def emitter():
    return RecordingUiEmitter()


@pytest.fixture
def construct(emitter):
    return AgentHandoffWithApproval(emitter=emitter, answer_delivery=None)


def _make_bridge(construct, get_output_fn=None, get_provider_fn=None, get_session_fn=None):
    return ApprovalBridge(
        construct=construct,
        get_output_fn=get_output_fn,
        get_provider_fn=get_provider_fn,
        get_session_fn=get_session_fn,
    )


# ---------------------------------------------------------------------------
# Tests: bridge triggers on WAITING_USER_ANSWER
# ---------------------------------------------------------------------------


class TestBridgeOnWaiting:
    """ApprovalBridge creates an interrupt when terminal enters WAITING_USER_ANSWER."""

    @pytest.mark.asyncio
    async def test_creates_interrupt_on_waiting(self, construct):
        """Synthetic WAITING_USER_ANSWER event triggers on_provider_waiting."""
        bridge = _make_bridge(
            construct,
            get_output_fn=lambda tid: "\u2191/\u2193 to navigate",
            get_provider_fn=lambda tid: "claude_code",
        )

        test_bus = EventBus()
        loop = asyncio.get_event_loop()
        test_bus.set_loop(loop)

        with (
            patch("cli_agent_orchestrator.services.agui.approval_bridge.bus", test_bus),
            patch(
                "cli_agent_orchestrator.services.agui_enablement.agui_surface_enabled",
                return_value=True,
            ),
        ):
            task = asyncio.create_task(bridge.run())
            await asyncio.sleep(0.01)

            test_bus.publish(
                "terminal.t-1.status",
                {"status": TerminalStatus.WAITING_USER_ANSWER.value},
            )
            await asyncio.sleep(0.05)

            assert len(construct.pending()) == 1
            interrupt = construct.pending()[0]
            assert interrupt.reason == "claude-code:permission_request"
            assert interrupt.metadata["terminal_id"] == "t-1"

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_no_duplicate_on_repeated_waiting(self, construct):
        """Multiple WAITING_USER_ANSWER events for same terminal create only one interrupt."""
        bridge = _make_bridge(
            construct,
            get_output_fn=lambda tid: "some prompt",
            get_provider_fn=lambda tid: "codex",
        )

        test_bus = EventBus()
        loop = asyncio.get_event_loop()
        test_bus.set_loop(loop)

        with (
            patch("cli_agent_orchestrator.services.agui.approval_bridge.bus", test_bus),
            patch(
                "cli_agent_orchestrator.services.agui_enablement.agui_surface_enabled",
                return_value=True,
            ),
        ):
            task = asyncio.create_task(bridge.run())
            await asyncio.sleep(0.01)

            test_bus.publish(
                "terminal.t-1.status",
                {"status": TerminalStatus.WAITING_USER_ANSWER.value},
            )
            await asyncio.sleep(0.05)
            test_bus.publish(
                "terminal.t-1.status",
                {"status": TerminalStatus.WAITING_USER_ANSWER.value},
            )
            await asyncio.sleep(0.05)

            assert len(construct.pending()) == 1

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Tests: bridge expires on leaving WAITING_USER_ANSWER
# ---------------------------------------------------------------------------


class TestBridgeOnLeaveWaiting:
    """ApprovalBridge calls expire when terminal leaves WAITING_USER_ANSWER."""

    @pytest.mark.asyncio
    async def test_expires_on_transition_away(self, construct):
        """Going from WAITING_USER_ANSWER to another status expires the interrupt."""
        bridge = _make_bridge(
            construct,
            get_output_fn=lambda tid: "prompt text",
            get_provider_fn=lambda tid: "claude_code",
        )

        test_bus = EventBus()
        loop = asyncio.get_event_loop()
        test_bus.set_loop(loop)

        with (
            patch("cli_agent_orchestrator.services.agui.approval_bridge.bus", test_bus),
            patch(
                "cli_agent_orchestrator.services.agui_enablement.agui_surface_enabled",
                return_value=True,
            ),
        ):
            task = asyncio.create_task(bridge.run())
            await asyncio.sleep(0.01)

            test_bus.publish(
                "terminal.t-1.status",
                {"status": TerminalStatus.WAITING_USER_ANSWER.value},
            )
            await asyncio.sleep(0.05)
            assert len(construct.pending()) == 1

            test_bus.publish(
                "terminal.t-1.status",
                {"status": TerminalStatus.PROCESSING.value},
            )
            await asyncio.sleep(0.05)

            assert len(construct.pending()) == 0
            all_interrupts = [i for i in construct._interrupts.values() if i.outcome == "expired"]
            assert len(all_interrupts) == 1

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Tests: bridge disabled when surface off
# ---------------------------------------------------------------------------


class TestBridgeDisabled:
    """ApprovalBridge is a no-op when AG-UI surface is disabled."""

    @pytest.mark.asyncio
    async def test_does_not_start_when_disabled(self, construct):
        """Bridge returns immediately when surface is disabled."""
        bridge = _make_bridge(construct)

        with patch(
            "cli_agent_orchestrator.services.agui_enablement.agui_surface_enabled",
            return_value=False,
        ):
            # run() should return quickly without blocking
            await asyncio.wait_for(bridge.run(), timeout=1.0)
            # No interrupts created
            assert len(construct.pending()) == 0
