"""Tests for MCP server utilities."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.utils import get_terminal_record


class TestGetTerminalRecord:
    """Tests for get_terminal_record function."""

    @patch("cli_agent_orchestrator.mcp_server.utils.SessionLocal")
    def test_get_terminal_record_found(self, mock_session_local):
        """Test getting terminal record when it exists."""
        # Setup mock
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db

        mock_terminal = MagicMock()
        mock_terminal.id = "term-123"
        mock_terminal.session_name = "test-session"

        mock_query = MagicMock()
        mock_filter = MagicMock()
        mock_filter.first.return_value = mock_terminal
        mock_query.filter.return_value = mock_filter
        mock_db.query.return_value = mock_query

        # Execute
        result = get_terminal_record("term-123")

        # Verify
        assert result == mock_terminal
        mock_db.close.assert_called_once()

    @patch("cli_agent_orchestrator.mcp_server.utils.SessionLocal")
    def test_get_terminal_record_not_found(self, mock_session_local):
        """Test getting terminal record when it doesn't exist."""
        # Setup mock
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db

        mock_query = MagicMock()
        mock_filter = MagicMock()
        mock_filter.first.return_value = None
        mock_query.filter.return_value = mock_filter
        mock_db.query.return_value = mock_query

        # Execute
        result = get_terminal_record("nonexistent")

        # Verify
        assert result is None
        mock_db.close.assert_called_once()

    @patch("cli_agent_orchestrator.mcp_server.utils.SessionLocal")
    def test_get_terminal_record_closes_session_on_exception(self, mock_session_local):
        """Test that database session is closed even on exception."""
        # Setup mock
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.side_effect = Exception("Database error")

        # Execute and verify exception is raised
        with pytest.raises(Exception, match="Database error"):
            get_terminal_record("term-123")

        # Verify session was closed
        mock_db.close.assert_called_once()
