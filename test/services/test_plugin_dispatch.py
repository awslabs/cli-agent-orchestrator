"""Tests for plugin dispatch adapter behavior."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.plugins import MessageSentEvent
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event


def test_dispatch_plugin_event_noops_when_registry_missing():
    """Missing registry should be a silent no-op."""

    event = MessageSentEvent(
        session_id="cao-demo",
        sender="supervisor-1",
        receiver="worker-1",
        message="Hello",
        orchestration_type="send_message",
    )

    dispatch_plugin_event(None, "message_sent", event)


def test_dispatch_plugin_event_logs_and_swallows_registry_errors(caplog):
    """Adapter-level failures should be logged and must not propagate."""

    registry = MagicMock()
    registry.dispatch = AsyncMock(side_effect=RuntimeError("dispatch failed"))
    event = MessageSentEvent(
        session_id="cao-demo",
        sender="supervisor-1",
        receiver="worker-1",
        message="Hello",
        orchestration_type="send_message",
    )

    with caplog.at_level("WARNING"):
        dispatch_plugin_event(registry, "message_sent", event)

    registry.dispatch.assert_awaited_once_with("message_sent", event)
    assert caplog.records[-1].message == "Plugin event dispatch failed for message_sent"
    assert caplog.records[-1].exc_info is not None


@pytest.mark.asyncio
async def test_dispatch_plugin_event_schedules_dispatch_in_running_loop():
    """A running event loop should use create_task and still complete dispatch."""

    registry = MagicMock()
    registry.dispatch = AsyncMock()
    event = MessageSentEvent(
        session_id="cao-demo",
        sender="supervisor-1",
        receiver="worker-1",
        message="Hello",
        orchestration_type="assign",
    )

    dispatch_plugin_event(registry, "message_sent", event)
    await asyncio.sleep(0)

    registry.dispatch.assert_awaited_once_with("message_sent", event)
