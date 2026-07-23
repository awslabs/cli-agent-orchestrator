"""Tests for cleanup service."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data


class TestCleanupOldData:
    """Tests for cleanup_old_data function."""

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_terminals(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local
    ):
        """Test that cleanup deletes old terminals from database."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 5

        # Setup mock directories (non-existent)
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute
        cleanup_old_data()

        # Verify terminal cleanup was called
        assert mock_db.query.called
        assert mock_db.commit.called

    @patch("cli_agent_orchestrator.services.cleanup_service.status_monitor")
    @patch("cli_agent_orchestrator.services.cleanup_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_inbox_messages(
        self,
        mock_log_dir,
        mock_terminal_log_dir,
        mock_session_local,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test that cleanup deletes old inbox messages from database."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.delete.return_value = 10

        # Setup mock directories (non-existent)
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute
        cleanup_old_data()

        # Verify cleanup was called:
        # Session 1: query.all() for terminal iteration + query.delete() for terminal deletion
        # Session 2: query.delete() for inbox deletion
        assert mock_db.query.call_count >= 2
        assert mock_db.commit.call_count == 2

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_terminal_log_files(self, mock_session_local):
        """Test that cleanup deletes old terminal log files."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        # Create temp directory with old and new log files
        with tempfile.TemporaryDirectory() as tmpdir:
            terminal_log_dir = Path(tmpdir) / "terminal"
            terminal_log_dir.mkdir()

            # Create old log file (older than retention period)
            old_log = terminal_log_dir / "old.log"
            old_log.write_text("old log content")
            old_time = (datetime.now() - timedelta(days=10)).timestamp()
            import os

            os.utime(old_log, (old_time, old_time))

            # Create new log file (within retention period)
            new_log = terminal_log_dir / "new.log"
            new_log.write_text("new log content")

            with patch(
                "cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR",
                terminal_log_dir,
            ):
                with patch(
                    "cli_agent_orchestrator.services.cleanup_service.LOG_DIR",
                    Path(tmpdir) / "nonexistent",
                ):
                    cleanup_old_data()

            # Verify old log was deleted, new log remains
            assert not old_log.exists()
            assert new_log.exists()

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_server_log_files(self, mock_session_local):
        """Test that cleanup deletes old server log files."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        # Create temp directory with old and new log files
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            log_dir.mkdir()

            # Create old log file
            old_log = log_dir / "server_old.log"
            old_log.write_text("old server log")
            old_time = (datetime.now() - timedelta(days=10)).timestamp()
            import os

            os.utime(old_log, (old_time, old_time))

            # Create new log file
            new_log = log_dir / "server_new.log"
            new_log.write_text("new server log")

            with patch(
                "cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR",
                Path(tmpdir) / "nonexistent",
            ):
                with patch(
                    "cli_agent_orchestrator.services.cleanup_service.LOG_DIR",
                    log_dir,
                ):
                    cleanup_old_data()

            # Verify old log was deleted, new log remains
            assert not old_log.exists()
            assert new_log.exists()

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_handles_database_error(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local
    ):
        """Test that cleanup handles database errors gracefully."""
        # Setup mock database session to raise an error
        mock_session_local.return_value.__enter__.side_effect = Exception("Database error")

        # Setup mock directories (non-existent)
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute - should not raise exception
        cleanup_old_data()  # Should log error but not raise

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_handles_empty_directories(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local
    ):
        """Test that cleanup handles empty or non-existent directories."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        # Setup mock directories as non-existent
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute - should complete without error
        cleanup_old_data()

        # Verify database operations still occurred
        assert mock_db.query.called

    @patch("cli_agent_orchestrator.services.cleanup_service.status_monitor")
    @patch("cli_agent_orchestrator.services.cleanup_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 30)
    def test_cleanup_uses_correct_retention_period(
        self, mock_session_local, mock_fifo_manager, mock_status_monitor
    ):
        """Test that cleanup uses the configured retention period."""
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db

        # Capture the filter argument to verify cutoff date
        filter_calls = []

        def capture_filter(condition):
            filter_calls.append(condition)
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_result.delete.return_value = 0
            return mock_result

        mock_db.query.return_value.filter = capture_filter

        with patch(
            "cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR"
        ) as mock_terminal:
            with patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR") as mock_log:
                mock_terminal.exists.return_value = False
                mock_log.exists.return_value = False
                cleanup_old_data()

        # Verify filter was called (terminals: .all() + .delete(), inbox: .delete())
        assert len(filter_calls) >= 2


# ------------------------------------------- v2 vintage invisibility (MIG)


class TestCleanupV2VintageInvisibility:
    """MIG durable regression: old cleanup has zero visibility into v2 state.

    A v2-shaped managed row in the shared terminals table (managed-* window
    or a generation/id present in the v2 surface) is preserved by the
    legacy retention cleanup — never deleted; ordinary old v1 rows are
    still cleaned up exactly as before.
    """

    def _run_cleanup(self, tmp_path, session):
        from cli_agent_orchestrator.services import cleanup_service

        prior = (
            cleanup_service.SessionLocal,
            cleanup_service.fifo_manager,
            cleanup_service.status_monitor,
            cleanup_service.TERMINAL_LOG_DIR,
            cleanup_service.LOG_DIR,
        )
        try:
            cleanup_service.SessionLocal = session
            cleanup_service.fifo_manager = type(
                "NoopFifo", (), {"stop_reader": staticmethod(lambda _id: None)}
            )()
            cleanup_service.status_monitor = type(
                "NoopMonitor", (), {"clear_terminal": staticmethod(lambda _id: None)}
            )()
            cleanup_service.TERMINAL_LOG_DIR = tmp_path / "terminal-logs"
            cleanup_service.LOG_DIR = tmp_path / "logs"
            cleanup_service.cleanup_old_data()
        finally:
            (
                cleanup_service.SessionLocal,
                cleanup_service.fifo_manager,
                cleanup_service.status_monitor,
                cleanup_service.TERMINAL_LOG_DIR,
                cleanup_service.LOG_DIR,
            ) = prior

    def _engine(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database

        engine = create_engine(
            f"sqlite:///{tmp_path / 'metadata.db'}", connect_args={"check_same_thread": False}
        )
        database.Base.metadata.create_all(bind=engine)
        return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def test_v2_shaped_managed_row_survives_old_cleanup(self, tmp_path):
        from cli_agent_orchestrator.clients import database

        engine, session = self._engine(tmp_path)
        old = datetime.now() - timedelta(days=999)
        with session() as db:
            db.add(
                database.TerminalModel(
                    id="0ldc1ean",
                    tmux_session="v2-session",
                    tmux_window="managed-v2-window",
                    provider="codex",
                    agent_profile="probe",
                    generation="gen-v2",
                    last_active=old,
                )
            )
            db.add(
                database.TerminalModel(
                    id="p1ainv1",
                    tmux_session="v1-session",
                    tmux_window="plain-v1-window",
                    provider="codex",
                    agent_profile="probe",
                    generation=None,
                    last_active=old,
                )
            )
            db.commit()
        self._run_cleanup(tmp_path, session)
        with session() as db:
            assert db.query(database.TerminalModel).filter_by(id="0ldc1ean").first() is not None
            assert db.query(database.TerminalModel).filter_by(id="p1ainv1").first() is None
        engine.dispose()

    def test_row_owned_by_v2_surface_survives_old_cleanup(self, tmp_path):
        from cli_agent_orchestrator.clients import database

        engine, session = self._engine(tmp_path)
        old = datetime.now() - timedelta(days=999)
        with session() as db:
            db.add(
                database.ManagedLaunchV2TerminalModel(
                    id="v2owned1",
                    tmux_session="v2-session",
                    tmux_window="win",
                    provider="codex",
                    generation="gen-owned",
                    protocol_vintage="v2",
                )
            )
            # The same id also appears in the shared table (e.g. written by
            # an older binary before the isolation landed): still preserved.
            db.add(
                database.TerminalModel(
                    id="v2owned1",
                    tmux_session="v2-session",
                    tmux_window="win",
                    provider="codex",
                    agent_profile="probe",
                    generation="gen-owned",
                    last_active=old,
                )
            )
            db.commit()
        self._run_cleanup(tmp_path, session)
        with session() as db:
            assert db.query(database.TerminalModel).filter_by(id="v2owned1").first() is not None
        engine.dispose()
