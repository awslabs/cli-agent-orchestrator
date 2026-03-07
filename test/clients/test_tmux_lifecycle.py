"""Tests for TmuxClient window lifecycle configuration."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


@pytest.fixture
def client():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        return TmuxClient()


@pytest.fixture
def mock_session():
    """Mock tmux session with window and pane."""
    session = MagicMock()
    window = MagicMock()
    pane = MagicMock()
    
    window.name = "test-window"
    window.active_pane = pane
    
    # Mock windows as a MagicMock with get method
    windows_mock = MagicMock()
    windows_mock.get = MagicMock(return_value=window)
    session.windows = windows_mock
    
    return session, window, pane


class TestWindowLifecycle:
    """Tests for window lifecycle configuration (auto-close and cleanup)."""

    def test_configure_window_lifecycle_sets_remain_on_exit_off(self, client, mock_session):
        """Verify remain-on-exit is set to off for auto-close behavior."""
        session, window, pane = mock_session
        client.server.sessions.get = MagicMock(return_value=session)
        
        client._configure_window_lifecycle("test-session", "test-window", "abc12345")
        
        # Verify set-option was called with remain-on-exit off
        pane.cmd.assert_any_call(
            "set-option", "-t", "test-session:test-window", "remain-on-exit", "off"
        )

    def test_configure_window_lifecycle_sets_pane_exited_hook(self, client, mock_session):
        """Verify pane-exited hook is configured to call DELETE terminal API."""
        session, window, pane = mock_session
        client.server.sessions.get = MagicMock(return_value=session)
        
        terminal_id = "abc12345"
        client._configure_window_lifecycle("test-session", "test-window", terminal_id)
        
        # Find the set-hook call
        hook_call = None
        for call in pane.cmd.call_args_list:
            if call[0][0] == "set-hook":
                hook_call = call
                break
        
        assert hook_call is not None, "set-hook should be called"
        args = hook_call[0]
        
        # Verify hook structure
        assert args[0] == "set-hook"
        assert args[1] == "-t"
        assert args[2] == "test-session:test-window"
        assert args[3] == "pane-exited"
        
        # Verify hook command contains critical elements
        hook_command = args[4]
        assert "run-shell -b" in hook_command, "Should run in background"
        assert "curl" in hook_command, "Should use curl"
        assert "--max-time 2" in hook_command, "Should have timeout"
        assert f"/terminals/{terminal_id}" in hook_command, "Should target correct terminal"
        assert "-X DELETE" in hook_command, "Should use DELETE method"
        assert ">/dev/null 2>&1" in hook_command, "Should redirect output"
        assert "|| true" in hook_command, "Should always succeed"

    def test_configure_window_lifecycle_handles_missing_session(self, client):
        """Verify graceful handling when session doesn't exist."""
        client.server.sessions.get = MagicMock(return_value=None)
        
        # Should not raise, just log warning
        client._configure_window_lifecycle("missing-session", "test-window", "abc12345")

    def test_configure_window_lifecycle_handles_missing_window(self, client):
        """Verify graceful handling when window doesn't exist."""
        session = MagicMock()
        session.windows.get = MagicMock(return_value=None)
        client.server.sessions.get = MagicMock(return_value=session)
        
        # Should not raise, just log warning
        client._configure_window_lifecycle("test-session", "missing-window", "abc12345")

    def test_configure_window_lifecycle_handles_missing_pane(self, client):
        """Verify graceful handling when pane doesn't exist."""
        session = MagicMock()
        window = MagicMock()
        window.active_pane = None
        session.windows.get = MagicMock(return_value=window)
        client.server.sessions.get = MagicMock(return_value=session)
        
        # Should not raise, just log warning
        client._configure_window_lifecycle("test-session", "test-window", "abc12345")

    def test_configure_window_lifecycle_handles_cmd_failure(self, client, mock_session):
        """Verify graceful handling when tmux cmd fails."""
        session, window, pane = mock_session
        pane.cmd.side_effect = Exception("tmux command failed")
        client.server.sessions.get = MagicMock(return_value=session)
        
        # Should not raise, just log warning
        client._configure_window_lifecycle("test-session", "test-window", "abc12345")

    @patch("cli_agent_orchestrator.clients.tmux.TmuxClient._configure_window_lifecycle")
    def test_create_session_calls_lifecycle_config(self, mock_lifecycle, client):
        """Verify create_session calls lifecycle configuration."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        
        client.server.new_session = MagicMock(return_value=mock_session)
        
        terminal_id = "abc12345"
        client.create_session("test-session", "test-window", terminal_id)
        
        mock_lifecycle.assert_called_once_with("test-session", "test-window", terminal_id)

    @patch("cli_agent_orchestrator.clients.tmux.TmuxClient._configure_window_lifecycle")
    def test_create_window_calls_lifecycle_config(self, mock_lifecycle, client):
        """Verify create_window calls lifecycle configuration."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        
        mock_session.new_window = MagicMock(return_value=mock_window)
        client.server.sessions.get = MagicMock(return_value=mock_session)
        
        terminal_id = "abc12345"
        client.create_window("test-session", "test-window", terminal_id)
        
        mock_lifecycle.assert_called_once_with("test-session", "test-window", terminal_id)
