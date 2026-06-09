"""Tests for StatusMonitor — focus on backend-aware get_status().

get_status() is the single source of truth for terminal status. For pipe-pane
backends (tmux) it returns the pushed pipeline status; for event-inbox backends
(herdr), which never feed the pipeline, it derives status on demand from the
provider's native status. These tests pin both paths.
"""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def _backend(event_inbox):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    return backend


class TestGetStatusTmux:
    """Pipe-pane backend: get_status returns the pushed _last_status, unchanged."""

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_returns_pushed_status(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING

        assert sm.get_status("t1") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_never_seen(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        assert sm.get_status("missing") == TerminalStatus.UNKNOWN


class TestGetStatusEventInbox:
    """Event-inbox backend (herdr): derive status on demand from the provider."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_derives_from_provider_native_status(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        # _last_status is empty (herdr never feeds the pipeline) — the old code
        # would return UNKNOWN here.
        assert sm.get_status("t1") == TerminalStatus.IDLE
        provider.get_status.assert_called_once()

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_no_provider(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        mock_pm.get_provider.return_value = None

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_provider_lookup_raises(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        mock_pm.get_provider.side_effect = ValueError("terminal not in db")

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_provider_get_status_raises(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.side_effect = RuntimeError("herdr cli failed")
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN
