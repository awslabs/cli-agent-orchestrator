"""Integration tests for plugin registry FastAPI lifespan wiring."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from cli_agent_orchestrator.api.main import app, get_plugin_registry, lifespan
from cli_agent_orchestrator.plugins import PluginRegistry


class TestPluginRegistryLifespan:
    """Tests for plugin registry startup, app state wiring, and teardown."""

    @pytest.mark.asyncio
    async def test_lifespan_stores_registry_and_tears_it_down(self) -> None:
        """The lifespan should create, store, expose, and tear down the registry."""

        mock_observer = MagicMock()
        mock_load = AsyncMock()
        mock_teardown = AsyncMock()

        request_scope = {"type": "http", "app": app, "headers": []}

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch(
                "cli_agent_orchestrator.api.main.PollingObserver",
                return_value=mock_observer,
            ),
            patch(
                "cli_agent_orchestrator.api.main.flow_daemon",
                return_value=asyncio.sleep(0),
            ),
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

            mock_teardown.assert_awaited_once()
            mock_observer.stop.assert_called_once()
            mock_observer.join.assert_called_once()
