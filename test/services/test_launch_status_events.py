"""Issue #1082 busy time: a status transition is persisted for BOTH backends --
tmux via StatusMonitor._apply_detection's sticky-latch pipeline, and herdr via
get_status()'s on-demand derivation, which never feeds that pipeline. A
recording failure must never affect the status either path returns."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _backend(event_inbox):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    return backend


class TestTmuxBusyTimeRecording:
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_status_change_persists_a_transition(self, mock_get_backend):
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        assert sm._apply_detection("t1", TerminalStatus.PROCESSING) is True

        with database.SessionLocal() as db:
            rows = db.query(database.LaunchStatusEventModel).filter_by(terminal_id="t1").all()
        assert [r.status for r in rows] == ["processing"]

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unchanged_status_records_nothing_new(self, mock_get_backend):
        """Mutation guard: recording must be gated on `changed`, not fire on
        every detection call regardless of whether the latch actually moved."""
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()
        sm._apply_detection("t2", TerminalStatus.PROCESSING)
        sm._apply_detection("t2", TerminalStatus.PROCESSING)  # same status again

        with database.SessionLocal() as db:
            n = db.query(database.LaunchStatusEventModel).filter_by(terminal_id="t2").count()
        assert n == 1

    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_recording_failure_never_changes_the_detection_result(self, mock_get_backend, mock_bus):
        """AC: a failure writing the record must never fail or alter a launch
        (here: the status-change detection result the caller observes)."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        with patch(
            "cli_agent_orchestrator.clients.database.record_launch_status_event",
            side_effect=RuntimeError("db is down"),
        ):
            changed = sm._apply_detection("t3", TerminalStatus.PROCESSING)

        assert changed is True
        mock_bus.publish.assert_called_once()  # the real side effect still ran


class TestHerdrBusyTimeRecording:
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_first_poll_records_a_transition(self, mock_get_backend, mock_pm):
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        assert sm.get_status("h1") == TerminalStatus.PROCESSING

        with database.SessionLocal() as db:
            rows = db.query(database.LaunchStatusEventModel).filter_by(terminal_id="h1").all()
        assert [r.status for r in rows] == ["processing"]

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_repeated_same_status_polls_record_once(self, mock_get_backend, mock_pm):
        """Mutation guard: dedup against the LAST recorded status, not against
        every poll -- herdr callers poll far more often than the status
        actually changes."""
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        for _ in range(5):
            sm.get_status("h2")

        with database.SessionLocal() as db:
            n = db.query(database.LaunchStatusEventModel).filter_by(terminal_id="h2").count()
        assert n == 1

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_status_change_records_a_second_transition(self, mock_get_backend, mock_pm):
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        sm.get_status("h3")
        provider.get_status.return_value = TerminalStatus.IDLE
        sm.get_status("h3")

        with database.SessionLocal() as db:
            rows = (
                db.query(database.LaunchStatusEventModel)
                .filter_by(terminal_id="h3")
                .order_by(database.LaunchStatusEventModel.id.asc())
                .all()
            )
        assert [r.status for r in rows] == ["processing", "idle"]

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_recording_failure_never_changes_returned_status(self, mock_get_backend, mock_pm):
        """AC: a failure writing the record must never fail or alter what
        get_status() returns to its (many) callers."""
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        with patch(
            "cli_agent_orchestrator.clients.database.record_launch_status_event",
            side_effect=RuntimeError("db is down"),
        ):
            result = sm.get_status("h4")

        assert result == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_clear_terminal_forgets_the_herdr_dedup_entry(self, mock_get_backend, mock_pm):
        """clear_terminal (teardown) must drop the dedup map entry so a relaunch
        on the same id is not spuriously deduped against a stale prior status."""
        from cli_agent_orchestrator.services import status_monitor as sm_mod

        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        sm.get_status("h5")
        assert "h5" in sm_mod._herdr_last_recorded_status

        sm.clear_terminal("h5")
        assert "h5" not in sm_mod._herdr_last_recorded_status
