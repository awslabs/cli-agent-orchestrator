"""Tests for cleanup service."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    HandoffResultModel,
    get_handoff_result,
    upsert_handoff_result,
)
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

    @patch("cli_agent_orchestrator.services.cleanup_service.provider_manager")
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_retains_grok_row_when_provider_cleanup_is_deferred(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local, mock_provider_manager
    ):
        """Retention cleanup keeps the only retry handle for a private Grok home."""
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        old_terminal = MagicMock(id="retained-grok", provider="grok_cli")
        old_terminal_query = MagicMock()
        terminal_delete_query = MagicMock()
        inbox_query = MagicMock()
        idempotency_query = MagicMock()
        mock_db.query.side_effect = [
            old_terminal_query,
            terminal_delete_query,
            inbox_query,
            idempotency_query,
        ]
        old_terminal_query.filter.return_value.all.return_value = [old_terminal]
        terminal_delete_query.filter.return_value.filter.return_value.delete.return_value = 0
        inbox_query.filter.return_value.delete.return_value = 0
        idempotency_query.filter.return_value.delete.return_value = 0
        mock_provider_manager.cleanup_provider.return_value = False
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        cleanup_old_data()

        mock_provider_manager.cleanup_provider.assert_called_once_with("retained-grok")
        terminal_delete_query.filter.return_value.filter.assert_called_once()

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
        # Session 3: query.delete() for idempotency-key deletion
        assert mock_db.query.call_count >= 2
        assert mock_db.commit.call_count == 3

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


class TestHandoffResultRetention:
    """The handoff_results sweep (issue #447), PR #453 review findings 1 and 7.

    Two distinct gaps, two tests. Finding 1 is the CLOCK: every other swept table
    defaults its timestamp to naive-local ``datetime.now``, so ``cutoff_date``
    matches them, but ``HandoffResultModel.created_at`` defaults to ``_utcnow()``
    and SQLite keeps only the UTC wall-clock -- a local cutoff therefore deletes
    rows UTC-offset hours early or late. Finding 7 is the WIRING: every other
    cleanup test here patches ``cleanup_service.SessionLocal``, which does NOT
    intercept the ``database.SessionLocal`` that ``delete_old_handoff_results``
    opens internally, so no existing test touches the handoff table at all.
    """

    @patch("cli_agent_orchestrator.services.cleanup_service.delete_old_handoff_results")
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 30)
    def test_handoff_sweep_uses_a_tz_aware_utc_cutoff(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local, mock_delete
    ):
        """The cutoff handed to the handoff sweep must be tz-aware UTC, not the
        naive-local ``cutoff_date`` the sibling tables use.

        The ``tzinfo is not None`` assertion is what makes this non-vacuous and
        TZ-independent: the pre-fix code passed ``cutoff_date``, which is naive in
        EVERY timezone including UTC, so this fails on the old code without needing
        the suite to run east of Greenwich.
        """
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False
        mock_delete.return_value = 0

        cleanup_old_data()

        mock_delete.assert_called_once()
        cutoff = mock_delete.call_args[0][0]
        assert cutoff.tzinfo is not None, "handoff cutoff must be tz-aware, not naive local"
        assert cutoff.utcoffset() == timedelta(0), "handoff cutoff must be UTC"
        expected = datetime.now(timezone.utc) - timedelta(days=30)
        assert abs((cutoff - expected).total_seconds()) < 60

    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 30)
    def test_handoff_sweep_reaches_the_real_table(
        self, mock_log_dir, mock_terminal_log_dir, monkeypatch
    ):
        """End-to-end through the REAL ``delete_old_handoff_results``: an aged row is
        swept and a fresh one survives.

        ``database.SessionLocal`` is patched in ADDITION to
        ``cleanup_service.SessionLocal`` -- patching only the latter (what every
        other test in this file does) leaves the handoff sweep talking to the
        operator's real DB, which is why this path shipped unverified.
        """
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.cleanup_service.SessionLocal", TestSession
        )
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        upsert_handoff_result("aged", "completed", last_message="old output")
        upsert_handoff_result("fresh", "completed", last_message="new output")
        with TestSession() as db:
            row = db.query(HandoffResultModel).filter(HandoffResultModel.job_id == "aged").first()
            row.created_at = datetime.now(timezone.utc) - timedelta(days=31)
            db.commit()

        cleanup_old_data()

        assert get_handoff_result("aged") is None
        assert get_handoff_result("fresh") is not None
