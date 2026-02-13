"""Unit tests for session service."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import session_service


@pytest.fixture
def mock_tmux():
    """Mock tmux client."""
    with patch("cli_agent_orchestrator.services.session_service.tmux_client") as mock:
        yield mock


@pytest.fixture
def mock_db():
    """Mock database functions."""
    with patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session") as list_term, \
         patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session") as del_term:
        yield {"list": list_term, "delete": del_term}


@pytest.fixture
def mock_provider_manager():
    """Mock provider manager."""
    with patch("cli_agent_orchestrator.services.session_service.provider_manager") as mock:
        yield mock


class TestListSessions:
    """Test list_sessions function."""

    def test_list_sessions_empty(self, mock_tmux):
        """Test listing sessions when none exist."""
        mock_tmux.list_sessions.return_value = []
        
        result = session_service.list_sessions()
        
        assert result == []
        mock_tmux.list_sessions.assert_called_once()

    def test_list_sessions_filters_cao_prefix(self, mock_tmux):
        """Test listing sessions filters CAO prefix."""
        mock_tmux.list_sessions.return_value = [
            {"id": "cao-session-1", "name": "cao-session-1"},
            {"id": "other-session", "name": "other-session"},
            {"id": "cao-session-2", "name": "cao-session-2"}
        ]
        
        result = session_service.list_sessions()
        
        assert len(result) == 2
        session_ids = [s["id"] for s in result]
        assert "cao-session-1" in session_ids
        assert "cao-session-2" in session_ids
        assert "other-session" not in session_ids


class TestGetSession:
    """Test get_session function."""

    def test_get_session_success(self, mock_tmux, mock_db):
        """Test successful session retrieval."""
        mock_tmux.session_exists.return_value = True
        mock_tmux.list_sessions.return_value = [
            {"id": "test-session", "name": "test-session", "windows": 2}
        ]
        mock_db["list"].return_value = [{"id": "term1"}, {"id": "term2"}]
        
        result = session_service.get_session("test-session")
        
        assert result is not None
        assert result["session"]["id"] == "test-session"
        assert len(result["terminals"]) == 2

    def test_get_session_not_found(self, mock_tmux):
        """Test session retrieval when not found."""
        mock_tmux.session_exists.return_value = False
        
        with pytest.raises(ValueError, match="Session 'nonexistent' not found"):
            session_service.get_session("nonexistent")


class TestDeleteSession:
    """Test delete_session function."""

    def test_delete_session_success(self, mock_tmux, mock_db, mock_provider_manager):
        """Test successful session deletion."""
        mock_tmux.session_exists.return_value = True
        mock_db["list"].return_value = []
        
        result = session_service.delete_session("test-session")
        
        assert result is True
        mock_tmux.kill_session.assert_called_once_with("test-session")
        mock_db["delete"].assert_called_once_with("test-session")

    def test_delete_session_with_terminals(self, mock_tmux, mock_db, mock_provider_manager):
        """Test session deletion with terminals."""
        mock_tmux.session_exists.return_value = True
        mock_db["list"].return_value = [{"id": "terminal1"}, {"id": "terminal2"}]
        
        result = session_service.delete_session("test-session")
        
        assert result is True
        assert mock_provider_manager.cleanup_provider.call_count == 2
        mock_tmux.kill_session.assert_called_once_with("test-session")
        mock_db["delete"].assert_called_once_with("test-session")

    def test_delete_session_not_found(self, mock_tmux, mock_db):
        """Test session deletion when not found."""
        mock_tmux.session_exists.return_value = False
        
        with pytest.raises(ValueError, match="Session 'nonexistent' not found"):
            session_service.delete_session("nonexistent")