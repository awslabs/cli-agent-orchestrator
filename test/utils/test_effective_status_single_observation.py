"""``effective_status`` decides placement and status in ONE registry read (#745).

Before this fix ``effective_status`` called ``runtime_registry.is_remote`` and
then ``runtime_registry.get_status`` — two lock acquisitions from a worker
thread while the channel loop mutated the registry, so a disconnect between them
could hand a waiter a stale COMPLETED (Copilot 4104866574 / 4070130514 on #802).
It now takes ``runtime_registry.observe`` once and only consults the local status
monitor for a confirmed-local terminal.
"""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import effective_status


class TestEffectiveStatusSingleObservation:
    def test_makes_exactly_one_registry_call_observe(self):
        registry = MagicMock()
        registry.observe.return_value = (True, TerminalStatus.PROCESSING)
        monitor = MagicMock()
        with (
            patch("cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry),
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
        ):
            assert effective_status("t1") == TerminalStatus.PROCESSING

        registry.observe.assert_called_once_with("t1")
        registry.is_remote.assert_not_called()
        registry.get_status.assert_not_called()

    def test_remote_terminal_returns_observed_status_not_local_monitor(self):
        registry = MagicMock()
        registry.observe.return_value = (True, TerminalStatus.UNKNOWN)
        monitor = MagicMock()
        with (
            patch("cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry),
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
        ):
            # A bound terminal whose runtime unregistered observes UNKNOWN; the
            # local monitor (which could still hold COMPLETED) is never consulted.
            assert effective_status("t1") == TerminalStatus.UNKNOWN
        monitor.get_status.assert_not_called()

    def test_placement_read_failure_never_calls_local_monitor(self):
        registry = MagicMock()
        registry.observe.return_value = (True, TerminalStatus.UNKNOWN)
        monitor = MagicMock()
        with (
            patch("cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry),
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
        ):
            assert effective_status("t1") == TerminalStatus.UNKNOWN
        monitor.get_status.assert_not_called()

    def test_local_row_consults_the_local_status_monitor(self):
        registry = MagicMock()
        registry.observe.return_value = (False, None)
        monitor = MagicMock()
        monitor.get_status.return_value = TerminalStatus.IDLE
        with (
            patch("cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry),
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
        ):
            assert effective_status("t1") == TerminalStatus.IDLE
        monitor.get_status.assert_called_once_with("t1")
