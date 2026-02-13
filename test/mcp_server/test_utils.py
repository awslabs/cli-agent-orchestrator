"""Tests for MCP server utils."""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.utils import get_terminal_record


class TestGetTerminalRecord:
    def test_get_terminal_id_from_env(self):
        # Test getting terminal ID from environment
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "terminal123"}):
            terminal_id = os.getenv("CAO_TERMINAL_ID")
            assert terminal_id == "terminal123"

    def test_get_terminal_id_missing(self):
        # Test missing terminal ID
        with patch.dict(os.environ, {}, clear=True):
            terminal_id = os.getenv("CAO_TERMINAL_ID")
            assert terminal_id is None

    @patch("cli_agent_orchestrator.mcp_server.utils.SessionLocal")
    def test_get_terminal_record_found(self, mock_session_local):
        # Setup mock
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_terminal = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_terminal
        
        result = get_terminal_record("terminal123")
        
        assert result == mock_terminal
        mock_db.close.assert_called_once()

    @patch("cli_agent_orchestrator.mcp_server.utils.SessionLocal")
    def test_get_terminal_record_not_found(self, mock_session_local):
        # Setup mock
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = None
        
        result = get_terminal_record("nonexistent")
        
        assert result is None
        mock_db.close.assert_called_once()