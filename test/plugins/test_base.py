"""Tests for CAO plugin base primitives."""

from typing import Any

import pytest

from cli_agent_orchestrator.plugins.base import _HOOK_EVENT_ATTR, CaoPlugin, hook


class ExamplePlugin(CaoPlugin):
    """Simple plugin used to verify hook registration."""

    @hook("message_sent")
    async def on_message(self, event: Any) -> None:
        """Handle a message event."""

    @hook("session_created")
    async def on_session_created(self, event: Any) -> None:
        """Handle a session creation event."""


class TestCaoPlugin:
    """Tests for the CaoPlugin base class."""

    @pytest.mark.asyncio
    async def test_setup_is_awaitable_no_op(self) -> None:
        """Default setup() is awaitable and returns None."""

        plugin = CaoPlugin()

        result = await plugin.setup()

        assert result is None

    @pytest.mark.asyncio
    async def test_teardown_is_awaitable_no_op(self) -> None:
        """Default teardown() is awaitable and returns None."""

        plugin = CaoPlugin()

        result = await plugin.teardown()

        assert result is None


class TestHookDecorator:
    """Tests for the @hook decorator."""

    def test_hook_sets_event_attribute(self) -> None:
        """Decorator attaches the configured event type to the method."""

        assert getattr(ExamplePlugin.on_message, _HOOK_EVENT_ATTR) == "message_sent"

    def test_hook_preserves_original_callable_reference(self) -> None:
        """Decorator returns the same callable instead of wrapping it."""

        async def handler(event: Any) -> None:
            """Standalone handler used for identity checks."""

        decorated = hook("terminal_created")(handler)

        assert decorated is handler

    def test_multiple_methods_can_register_distinct_events(self) -> None:
        """Each decorated method retains its own hook event attribute."""

        assert getattr(ExamplePlugin.on_message, _HOOK_EVENT_ATTR) == "message_sent"
        assert getattr(ExamplePlugin.on_session_created, _HOOK_EVENT_ATTR) == "session_created"
