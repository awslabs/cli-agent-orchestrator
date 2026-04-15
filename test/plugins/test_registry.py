"""Tests for plugin registry discovery, dispatch, and lifecycle behavior."""

import logging
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.plugins import CaoPlugin, PluginRegistry, hook
from cli_agent_orchestrator.plugins.events import PostSendMessageEvent


@dataclass
class FakeEntryPoint:
    """Simple synthetic entry point for registry tests."""

    name: str
    loaded: object

    def load(self) -> object:
        """Return the configured object for this fake entry point."""

        return self.loaded


def make_entry_point(name: str, loaded: object) -> FakeEntryPoint:
    """Construct a fake entry point for a test plugin class or object."""

    return FakeEntryPoint(name=name, loaded=loaded)


class TestPluginRegistryLoad:
    """Tests for plugin discovery and registration."""

    @pytest.mark.asyncio
    async def test_load_with_no_entry_points_emits_info_and_keeps_dispatch_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No registered plugins should leave the registry empty and log INFO."""

        registry = PluginRegistry()

        with patch("importlib.metadata.entry_points", return_value=[]):
            with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.plugins.registry"):
                await registry.load()

        assert registry._plugins == []
        assert registry._dispatch == {}
        assert "No CAO plugins registered" in caplog.text

    @pytest.mark.asyncio
    async def test_load_single_plugin_with_one_hook_dispatches_matching_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A single registered hook should receive matching dispatched events."""

        received: list[str] = []

        class SingleHookPlugin(CaoPlugin):
            @hook("post_send_message")
            async def on_message(self, event: PostSendMessageEvent) -> None:
                received.append(event.message)

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[make_entry_point("single-hook", SingleHookPlugin)],
        ):
            with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.plugins.registry"):
                await registry.load()

        await registry.dispatch("post_send_message", PostSendMessageEvent(message="hello"))

        assert received == ["hello"]
        assert len(registry._plugins) == 1
        assert len(registry._dispatch["post_send_message"]) == 1
        assert "Loaded CAO plugin: single-hook" in caplog.text

    @pytest.mark.asyncio
    async def test_load_single_plugin_with_two_hooks_for_same_event_invokes_both(self) -> None:
        """Two hooks on the same plugin should both be registered and called."""

        received: list[str] = []

        class DoubleHookPlugin(CaoPlugin):
            @hook("post_send_message")
            async def first(self, event: PostSendMessageEvent) -> None:
                received.append(f"first:{event.message}")

            @hook("post_send_message")
            async def second(self, event: PostSendMessageEvent) -> None:
                received.append(f"second:{event.message}")

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[make_entry_point("double-hook", DoubleHookPlugin)],
        ):
            await registry.load()

        await registry.dispatch("post_send_message", PostSendMessageEvent(message="hello"))

        assert set(received) == {"first:hello", "second:hello"}
        assert len(received) == 2
        assert len(registry._dispatch["post_send_message"]) == 2

    @pytest.mark.asyncio
    async def test_load_multiple_plugins_for_same_event_invokes_all(self) -> None:
        """Hooks from multiple plugins should all receive the event."""

        received: list[str] = []

        class FirstPlugin(CaoPlugin):
            @hook("post_send_message")
            async def on_message(self, event: PostSendMessageEvent) -> None:
                received.append(f"first:{event.message}")

        class SecondPlugin(CaoPlugin):
            @hook("post_send_message")
            async def on_message(self, event: PostSendMessageEvent) -> None:
                received.append(f"second:{event.message}")

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[
                make_entry_point("first", FirstPlugin),
                make_entry_point("second", SecondPlugin),
            ],
        ):
            await registry.load()

        await registry.dispatch("post_send_message", PostSendMessageEvent(message="hello"))

        assert set(received) == {"first:hello", "second:hello"}
        assert len(registry._plugins) == 2
        assert len(registry._dispatch["post_send_message"]) == 2

    @pytest.mark.asyncio
    async def test_load_skips_plugin_when_setup_raises_and_loads_remaining(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A setup failure should log a warning and not block later plugins."""

        received: list[str] = []

        class FailingSetupPlugin(CaoPlugin):
            async def setup(self) -> None:
                raise RuntimeError("setup failed")

            @hook("post_send_message")
            async def on_message(self, event: PostSendMessageEvent) -> None:
                received.append(f"failing:{event.message}")

        class HealthyPlugin(CaoPlugin):
            @hook("post_send_message")
            async def on_message(self, event: PostSendMessageEvent) -> None:
                received.append(f"healthy:{event.message}")

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[
                make_entry_point("failing-setup", FailingSetupPlugin),
                make_entry_point("healthy", HealthyPlugin),
            ],
        ):
            with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.registry"):
                await registry.load()

        await registry.dispatch("post_send_message", PostSendMessageEvent(message="hello"))

        assert received == ["healthy:hello"]
        assert len(registry._plugins) == 1
        assert "Failed to load plugin 'failing-setup'" in caplog.text
        assert caplog.records[-1].exc_info is not None

    @pytest.mark.asyncio
    async def test_load_skips_non_plugin_entry_point_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-CaoPlugin entry point should be skipped and logged."""

        class NotAPlugin:
            pass

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[make_entry_point("not-a-plugin", NotAPlugin)],
        ):
            with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.registry"):
                await registry.load()

        assert registry._plugins == []
        assert registry._dispatch == {}
        assert "not a CaoPlugin subclass, skipping" in caplog.text


class TestPluginRegistryDispatch:
    """Tests for dispatch-time behavior and error isolation."""

    @pytest.mark.asyncio
    async def test_dispatch_logs_warning_and_continues_when_hook_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing hook should not prevent other matching hooks from running."""

        received: list[str] = []

        class FailingHookPlugin(CaoPlugin):
            @hook("post_send_message")
            async def broken(self, event: PostSendMessageEvent) -> None:
                received.append("broken")
                raise RuntimeError("dispatch failed")

            @hook("post_send_message")
            async def healthy(self, event: PostSendMessageEvent) -> None:
                received.append("healthy")

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[make_entry_point("failing-hook", FailingHookPlugin)],
        ):
            await registry.load()

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.registry"):
            await registry.dispatch("post_send_message", PostSendMessageEvent(message="hello"))

        assert set(received) == {"broken", "healthy"}
        assert "raised an error for event 'post_send_message'" in caplog.text
        assert caplog.records[-1].exc_info is not None

    @pytest.mark.asyncio
    async def test_dispatch_with_no_registered_handlers_is_no_op(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dispatching an unhandled event should do nothing and not error."""

        registry = PluginRegistry()

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.registry"):
            await registry.dispatch("post_send_message", PostSendMessageEvent(message="hello"))

        assert registry._dispatch == {}
        assert caplog.records == []


class TestPluginRegistryTeardown:
    """Tests for plugin teardown behavior."""

    @pytest.mark.asyncio
    async def test_teardown_logs_warning_and_continues_after_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A teardown failure should not prevent later plugins from tearing down."""

        torn_down: list[str] = []

        class FailingTeardownPlugin(CaoPlugin):
            async def teardown(self) -> None:
                torn_down.append("failing")
                raise RuntimeError("teardown failed")

        class HealthyTeardownPlugin(CaoPlugin):
            async def teardown(self) -> None:
                torn_down.append("healthy")

        registry = PluginRegistry()

        with patch(
            "importlib.metadata.entry_points",
            return_value=[
                make_entry_point("failing", FailingTeardownPlugin),
                make_entry_point("healthy", HealthyTeardownPlugin),
            ],
        ):
            await registry.load()

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.plugins.registry"):
            await registry.teardown()

        assert set(torn_down) == {"failing", "healthy"}
        assert "Plugin teardown failed for FailingTeardownPlugin" in caplog.text
        assert caplog.records[-1].exc_info is not None
