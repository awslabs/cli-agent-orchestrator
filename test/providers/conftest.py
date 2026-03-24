"""Shared fixtures for provider integration tests.

Bootstraps the event-driven pipeline (EventBus → StatusMonitor) and mocks
the database layer so integration tests can use the real create_terminal()
flow without needing a running DB.
"""

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest
import pytest_asyncio

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.status_monitor import status_monitor


@pytest_asyncio.fixture
async def event_pipeline():
    """Bootstrap EventBus + StatusMonitor for the current test's event loop.

    This enables the full pipeline:
      tmux pipe-pane → FIFO → FifoReader thread → EventBus → StatusMonitor
    so that provider.initialize() (which polls status_monitor) works correctly.
    """
    loop = asyncio.get_running_loop()

    # Clear stale subscriptions from previous tests (each test gets a new loop)
    with bus._lock:
        bus._exact.clear()
        bus._wildcard.clear()
    bus.set_loop(loop)

    # Start StatusMonitor as a background task
    monitor_task = asyncio.create_task(status_monitor.run())

    yield

    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    bus.set_loop(None)


@pytest.fixture
def mock_db():
    """Mock database functions with an in-memory dict.

    Patches the DB calls used by terminal_service so integration tests
    can run create_terminal() / delete_terminal() without a real database.
    The mock stores terminal metadata in a dict and serves it back from
    get_terminal_metadata(), mimicking real DB behavior.
    """
    terminals = {}

    def _create(terminal_id, session_name, window_name, provider, agent_profile):
        terminals[terminal_id] = {
            "id": terminal_id,
            "tmux_session": session_name,
            "tmux_window": window_name,
            "provider": provider,
            "agent_profile": agent_profile,
            "last_active": datetime.now(),
        }
        return terminals[terminal_id]

    def _get(terminal_id):
        return terminals.get(terminal_id)

    with (
        patch(
            "cli_agent_orchestrator.services.terminal_service.db_create_terminal",
            side_effect=_create,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            side_effect=_get,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.db_delete_terminal",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.update_last_active",
        ),
        # Also patch get_terminal_metadata in provider_manager (on-demand lookup)
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            side_effect=_get,
        ),
    ):
        yield terminals
