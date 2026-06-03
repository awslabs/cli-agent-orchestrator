"""Tests for the session service."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.session_service import (
    create_session,
    delete_session,
    get_session,
    list_sessions,
)


class TestCreateSession:
    """Tests for create_session function."""

    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    def test_create_session_resolves_provider_when_omitted(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is None, resolve_provider is called and its result forwarded."""
        mock_resolve.return_value = "claude_code"
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        create_session(provider=None, agent_profile="my_agent")

        mock_resolve.assert_called_once_with("my_agent", fallback_provider="kiro_cli")
        assert mock_create_terminal.call_args.kwargs["provider"] == "claude_code"

    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    def test_create_session_uses_explicit_provider(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is explicitly passed, resolve_provider is NOT called."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        create_session(provider="kiro_cli", agent_profile="my_agent")

        mock_resolve.assert_not_called()
        assert mock_create_terminal.call_args.kwargs["provider"] == "kiro_cli"


class TestListSessions:
    """Tests for list_sessions function."""

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_success(self, mock_get_backend):
        """Test listing sessions successfully."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_empty(self, mock_get_backend):
        """Test listing sessions when none exist."""
        mock_get_backend.return_value.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_no_cao_sessions(self, mock_get_backend):
        """Test listing sessions when no CAO sessions exist."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_error(self, mock_get_backend):
        """Test listing sessions with error."""
        mock_get_backend.return_value.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []


class TestGetSession:
    """Tests for get_session function."""

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_success(self, mock_get_backend, mock_list_terminals):
        """Test getting session successfully."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-test", "name": "Test Session"}
        ]
        mock_list_terminals.return_value = [{"id": "terminal1", "session": "cao-test"}]

        result = get_session("cao-test")

        assert result["session"]["id"] == "cao-test"
        assert len(result["terminals"]) == 1
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_found(self, mock_get_backend):
        """Test getting non-existent session."""
        mock_get_backend.return_value.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            get_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_in_list(self, mock_get_backend):
        """Test getting session that exists but not in list."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-test' not found"):
            get_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_error(self, mock_get_backend):
        """Test getting session with error."""
        mock_get_backend.return_value.session_exists.side_effect = Exception("Tmux error")

        with pytest.raises(Exception, match="Tmux error"):
            get_session("cao-test")


class TestDeleteSession:
    """Tests for delete_session function."""

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_success(
        self, mock_get_backend, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Test deleting session successfully."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
        ]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        mock_delete_terminals.assert_called_once_with("cao-test")
        assert mock_provider_manager.cleanup_provider.call_count == 2

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_when_backend_session_already_gone(
        self, mock_get_backend, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Backend session already gone — delete_session should not raise, not call kill_session,
        but still call delete_terminals_by_session."""
        mock_get_backend.return_value.session_exists.return_value = False
        mock_list_terminals.return_value = [{"id": "terminal1"}]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_not_called()
        mock_delete_terminals.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_no_terminals(
        self, mock_get_backend, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Test deleting session with no terminals."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_provider_manager.cleanup_provider.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_error(self, mock_get_backend, mock_list_terminals):
        """Test deleting session with error."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            delete_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_continues_when_provider_cleanup_fails(
        self, mock_get_backend, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Test that delete_session continues even when provider cleanup fails for some terminals."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
            {"id": "terminal3"},
        ]

        # First terminal cleanup fails, others succeed
        mock_provider_manager.cleanup_provider.side_effect = [
            Exception("Provider cleanup error for terminal1"),
            None,  # terminal2 succeeds
            None,  # terminal3 succeeds
        ]

        result = delete_session("cao-test")

        # Session should still be deleted despite provider cleanup failure
        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        mock_delete_terminals.assert_called_once_with("cao-test")
        # All three provider cleanups were attempted
        assert mock_provider_manager.cleanup_provider.call_count == 3

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.provider_manager")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_cleans_up_provider_for_each_terminal(
        self, mock_get_backend, mock_list_terminals, mock_provider_manager, mock_delete_terminals
    ):
        """Test that delete_session calls cleanup_provider for every terminal in the session."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "term-aaa"},
            {"id": "term-bbb"},
            {"id": "term-ccc"},
            {"id": "term-ddd"},
        ]

        result = delete_session("cao-multi-terminal")

        assert result == {"deleted": ["cao-multi-terminal"], "errors": []}
        # Verify cleanup_provider was called for each terminal with the correct ID
        expected_calls = [
            (("term-aaa",),),
            (("term-bbb",),),
            (("term-ccc",),),
            (("term-ddd",),),
        ]
        assert mock_provider_manager.cleanup_provider.call_count == 4
        mock_provider_manager.cleanup_provider.assert_any_call("term-aaa")
        mock_provider_manager.cleanup_provider.assert_any_call("term-bbb")
        mock_provider_manager.cleanup_provider.assert_any_call("term-ccc")
        mock_provider_manager.cleanup_provider.assert_any_call("term-ddd")
