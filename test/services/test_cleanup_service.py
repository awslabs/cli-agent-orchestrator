"""Unit tests for cleanup service."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import cleanup_service


class TestCleanupOldData:
    """Test cleanup_old_data function."""

    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    def test_cleanup_old_data(self, mock_log_dir, mock_terminal_log_dir, mock_session_local):
        """Test cleanup of old data."""
        # Mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 2
        
        # Mock log directories
        mock_terminal_log_dir.exists.return_value = True
        mock_log_dir.exists.return_value = True
        
        # Mock log files with proper stat mtime (old files - 10 days old)
        old_mtime = (datetime.now() - timedelta(days=10)).timestamp()
        
        # Create mock stat result
        mock_stat = MagicMock()
        mock_stat.st_mtime = old_mtime
        
        old_terminal_log = MagicMock()
        old_terminal_log.stat.return_value = mock_stat
        
        old_server_log = MagicMock()
        old_server_log.stat.return_value = mock_stat
        
        mock_terminal_log_dir.glob.return_value = [old_terminal_log]
        mock_log_dir.glob.return_value = [old_server_log]
        
        cleanup_service.cleanup_old_data()
        
        # Verify database cleanup was called
        assert mock_db.query.call_count == 2  # terminals and messages
        old_terminal_log.unlink.assert_called_once()
        old_server_log.unlink.assert_called_once()

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    def test_cleanup_no_old_data(self, mock_log_dir, mock_terminal_log_dir, mock_session_local):
        """Test cleanup when no old data exists."""
        # Mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0
        
        # Mock log directories don't exist
        mock_terminal_log_dir.exists.return_value = False
        mock_log_dir.exists.return_value = False
        
        cleanup_service.cleanup_old_data()
        
        # Verify database cleanup was called
        assert mock_db.query.call_count == 2  # terminals and messages
        mock_terminal_log_dir.glob.assert_not_called()
        mock_log_dir.glob.assert_not_called()