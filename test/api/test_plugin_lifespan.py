"""Integration tests for plugin registry FastAPI lifespan wiring."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from cli_agent_orchestrator.api.main import app, get_plugin_registry, lifespan
from cli_agent_orchestrator.plugins import CaoPlugin, PluginRegistry, hook
from cli_agent_orchestrator.plugins.events import PostSendMessageEvent


async def fake_flow_daemon() -> None:
    """Minimal async flow daemon stub for lifespan tests."""


class TestPluginRegistryLifespan:
    """Tests for plugin registry startup, app state wiring, and teardown."""

    @pytest.mark.asyncio
    async def test_lifespan_stores_registry_and_tears_it_down(self) -> None:
        """The lifespan should create, store, expose, and tear down the registry."""

        mock_observer = MagicMock()
        ordering: list[str] = []
        mock_load = AsyncMock()
        mock_teardown = AsyncMock()
        mock_load.side_effect = lambda: ordering.append("registry_load")
        mock_observer.schedule.side_effect = lambda *args, **kwargs: ordering.append(
            "observer_schedule"
        )

        request_scope = {"type": "http", "app": app, "headers": []}

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch(
                "cli_agent_orchestrator.api.main.PollingObserver",
                return_value=mock_observer,
            ),
            patch("cli_agent_orchestrator.api.main.flow_daemon", fake_flow_daemon),
            patch.object(PluginRegistry, "load", mock_load),
            patch.object(PluginRegistry, "teardown", mock_teardown),
        ):
            async with lifespan(app):
                registry = app.state.plugin_registry

                assert isinstance(registry, PluginRegistry)
                assert get_plugin_registry(Request(request_scope)) is registry
                assert get_plugin_registry(Request(dict(request_scope))) is registry
                mock_load.assert_awaited_once()
                mock_observer.schedule.assert_called_once()
                mock_observer.start.assert_called_once()
                assert ordering == ["registry_load", "observer_schedule"]

            mock_teardown.assert_awaited_once()
            mock_observer.stop.assert_called_once()
            mock_observer.join.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_logs_no_plugins_registered_when_entry_points_are_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The lifespan should surface the empty-plugin INFO log from the registry."""

        mock_observer = MagicMock()

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch(
                "cli_agent_orchestrator.api.main.PollingObserver",
                return_value=mock_observer,
            ),
            patch("cli_agent_orchestrator.api.main.flow_daemon", fake_flow_daemon),
            patch("importlib.metadata.entry_points", return_value=[]),
        ):
            with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.plugins.registry"):
                async with lifespan(app):
                    assert isinstance(app.state.plugin_registry, PluginRegistry)

        assert "No CAO plugins registered" in caplog.text

    @pytest.mark.asyncio
    async def test_lifespan_tolerates_plugin_setup_failure(self) -> None:
        """The lifespan should still start when one plugin fails during setup."""

        mock_observer = MagicMock()

        class FailingPlugin(CaoPlugin):
            async def setup(self) -> None:
                raise RuntimeError("setup failed")

        class HealthyPlugin(CaoPlugin):
            @hook("post_send_message")
            async def on_message(self, event: PostSendMessageEvent) -> None:
                del event

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch(
                "cli_agent_orchestrator.api.main.PollingObserver",
                return_value=mock_observer,
            ),
            patch("cli_agent_orchestrator.api.main.flow_daemon", fake_flow_daemon),
            patch(
                "importlib.metadata.entry_points",
                return_value=[
                    type("EP", (), {"name": "failing", "load": lambda self: FailingPlugin})(),
                    type("EP", (), {"name": "healthy", "load": lambda self: HealthyPlugin})(),
                ],
            ),
        ):
            async with lifespan(app):
                registry = app.state.plugin_registry

                assert isinstance(registry, PluginRegistry)
                assert len(registry._plugins) == 1
