"""Tests for the session service."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.session_service import (
    delete_session,
    get_session,
    list_sessions,
)


class TestListSessions:
    """Tests for list_sessions function."""

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_list_sessions_success(self, mock_tmux):
        """Test listing sessions successfully."""
        mock_tmux.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_list_sessions_empty(self, mock_tmux):
        """Test listing sessions when none exist."""
        mock_tmux.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_list_sessions_no_cao_sessions(self, mock_tmux):
        """Test listing sessions when no CAO sessions exist."""
        mock_tmux.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_list_sessions_error(self, mock_tmux):
        """Test listing sessions with error."""
        mock_tmux.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []


class TestGetSession:
    """Tests for get_session function."""

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_get_session_success(self, mock_tmux, mock_list_terminals):
        """Test getting session successfully."""
        mock_tmux.session_exists.return_value = True
        mock_tmux.list_sessions.return_value = [{"id": "cao-test", "name": "Test Session"}]
        mock_list_terminals.return_value = [{"id": "terminal1", "session": "cao-test"}]

        result = get_session("cao-test")

        assert result["session"]["id"] == "cao-test"
        assert len(result["terminals"]) == 1
        mock_tmux.session_exists.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_get_session_not_found(self, mock_tmux):
        """Test getting non-existent session."""
        mock_tmux.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            get_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_get_session_not_in_list(self, mock_tmux):
        """Test getting session that exists but not in list."""
        mock_tmux.session_exists.return_value = True
        mock_tmux.list_sessions.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-test' not found"):
            get_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_get_session_error(self, mock_tmux):
        """Test getting session with error."""
        mock_tmux.session_exists.side_effect = Exception("Tmux error")

        with pytest.raises(Exception, match="Tmux error"):
            get_session("cao-test")


class TestDeleteSession:
    """Tests for delete_session function."""

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_delete_session_success(
        self, mock_tmux, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Test deleting session successfully."""
        mock_tmux.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
        ]

        result = delete_session("cao-test")

        assert result is True
        mock_tmux.kill_session.assert_called_once_with("cao-test")
        mock_delete_terminals.assert_called_once_with("cao-test")
        assert mock_provider_manager.cleanup_provider.call_count == 2

    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_delete_session_not_found(self, mock_tmux):
        """Test deleting non-existent session."""
        mock_tmux.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            delete_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_delete_session_no_terminals(
        self, mock_tmux, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Test deleting session with no terminals."""
        mock_tmux.session_exists.return_value = True
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result is True
        mock_provider_manager.cleanup_provider.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.tmux_client")
    def test_delete_session_error(self, mock_tmux, mock_list_terminals):
        """Test deleting session with error."""
        mock_tmux.session_exists.return_value = True
        mock_list_terminals.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            delete_session("cao-test")
