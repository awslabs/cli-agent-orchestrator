"""Tests for event_bus module."""

from unittest.mock import patch

from cli_agent_orchestrator.services.event_bus import EventBus


class TestEventBusSubscribe:
    @patch("cli_agent_orchestrator.services.event_bus.get_server_settings")
    def test_subscribe_uses_configured_queue_size(self, mock_settings):
        """subscribe() creates queue with size from server settings."""
        mock_settings.return_value = {
            "mcp_request_timeout": 30,
            "event_bus_max_queue_size": 4096,
            "provider_init_timeout": 60,
            "startup_prompt_handler_timeout": 20,
        }
        bus = EventBus()
        queue = bus.subscribe("terminal.*.output")
        assert queue.maxsize == 4096
