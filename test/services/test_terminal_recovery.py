"""Recovery of tmux output streams after a CAO API-process restart."""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus


@patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
@patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
@patch("cli_agent_orchestrator.services.terminal_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.terminal_service.get_backend")
def test_recover_persisted_tmux_terminal_rearms_pipe_and_seeds_status(
    backend_getter, list_terminals, fifo_manager, status_monitor
):
    from cli_agent_orchestrator.services.terminal_service import (
        recover_persisted_terminal_output_streams,
    )

    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.list_sessions.return_value = [{"id": "cao-live"}]
    backend.get_history.return_value = "❯ ready"
    backend_getter.return_value = backend
    list_terminals.return_value = [
        {"id": "abc12345", "tmux_window": "tech_lead-a1b2"}
    ]
    status_monitor.seed_from_snapshot.return_value = TerminalStatus.COMPLETED

    assert recover_persisted_terminal_output_streams() == 1

    fifo_manager.create_reader.assert_called_once()
    backend.stop_pipe_pane.assert_called_once_with("cao-live", "tech_lead-a1b2")
    backend.pipe_pane.assert_called_once()
    status_monitor.seed_from_snapshot.assert_called_once_with("abc12345", "❯ ready")


@patch("cli_agent_orchestrator.services.terminal_service.get_backend")
def test_recover_persisted_terminal_streams_skips_event_inbox_backend(backend_getter):
    from cli_agent_orchestrator.services.terminal_service import (
        recover_persisted_terminal_output_streams,
    )

    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    backend_getter.return_value = backend

    assert recover_persisted_terminal_output_streams() == 0
    backend.list_sessions.assert_not_called()
