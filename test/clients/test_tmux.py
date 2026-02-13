"""Tests for tmux client."""

import os
import pytest
from unittest.mock import MagicMock, patch, call

from cli_agent_orchestrator.clients.tmux import TmuxClient


@pytest.fixture
def mock_subprocess():
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0, stdout="")
        yield mock


@pytest.fixture
def tmux_client():
    with patch("libtmux.Server") as mock_server:
        client = TmuxClient()
        client.server = mock_server.return_value
        yield client, mock_server.return_value


class TestTmuxClient:
    """Test tmux client operations."""
    
    def test_resolve_working_directory_default(self, tmux_client):
        client, _ = tmux_client
        
        with patch("os.getcwd", return_value="/current/dir"), \
             patch("os.path.realpath", return_value="/current/dir"), \
             patch("os.path.isdir", return_value=True):
            
            result = client._resolve_and_validate_working_directory(None)
            assert result == "/current/dir"
    
    def test_resolve_working_directory_custom(self, tmux_client):
        client, _ = tmux_client
        
        with patch("os.path.realpath", return_value="/custom/dir"), \
             patch("os.path.isdir", return_value=True):
            
            result = client._resolve_and_validate_working_directory("/custom/dir")
            assert result == "/custom/dir"
    
    def test_resolve_working_directory_invalid(self, tmux_client):
        client, _ = tmux_client
        
        with patch("os.path.realpath", return_value="/invalid/dir"), \
             patch("os.path.isdir", return_value=False):
            
            with pytest.raises(ValueError, match="Working directory does not exist"):
                client._resolve_and_validate_working_directory("/invalid/dir")
    
    def test_create_session_success(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        mock_server.new_session.return_value = mock_session
        
        with patch.object(client, "_resolve_and_validate_working_directory", return_value="/test/dir"), \
             patch.dict(os.environ, {}, clear=True):
            
            result = client.create_session("test-session", "test-window", "term123")
            
            assert result == "test-window"
            mock_server.new_session.assert_called_once_with(
                session_name="test-session",
                window_name="test-window",
                start_directory="/test/dir",
                detach=True,
                environment={"CAO_TERMINAL_ID": "term123"}
            )
    
    def test_create_session_with_working_directory(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        mock_server.new_session.return_value = mock_session
        
        with patch.object(client, "_resolve_and_validate_working_directory", return_value="/custom/dir"), \
             patch.dict(os.environ, {"CAO_TERMINAL_ID": "term123"}, clear=True):
            
            result = client.create_session("test-session", "test-window", "term123", "/custom/dir")
            
            assert result == "test-window"
            mock_server.new_session.assert_called_once_with(
                session_name="test-session",
                window_name="test-window",
                start_directory="/custom/dir",
                detach=True,
                environment={"CAO_TERMINAL_ID": "term123"}
            )
    
    def test_create_window_success(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "new-window"
        mock_session.new_window.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        with patch.object(client, "_resolve_and_validate_working_directory", return_value="/test/dir"):
            
            result = client.create_window("test-session", "new-window", "term123")
            
            assert result == "new-window"
            mock_session.new_window.assert_called_once_with(
                window_name="new-window",
                start_directory="/test/dir",
                environment={"CAO_TERMINAL_ID": "term123"}
            )
    
    def test_create_window_session_not_found(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_server.sessions.get.return_value = None
        
        with pytest.raises(ValueError, match="Session 'nonexistent' not found"):
            client.create_window("nonexistent", "window", "term123")
    
    def test_send_keys_success(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        with patch("time.sleep"):
            client.send_keys("test-session", "test-window", "hello world")
        
        mock_pane.send_keys.assert_has_calls([
            call("hello world", enter=False),
            call("C-m", enter=False)
        ])
    
    def test_send_keys_chunking_large_input(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        # Create a long message that will be chunked
        long_message = "a" * 150 + " " + "b" * 50
        
        with patch("time.sleep"):
            client.send_keys("test-session", "test-window", long_message)
        
        # Should be called multiple times for chunks plus final C-m
        assert mock_pane.send_keys.call_count >= 3
        mock_pane.send_keys.assert_any_call("C-m", enter=False)
    
    def test_get_history_success(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["line1", "line2", "line3"]
        mock_pane.cmd.return_value = mock_result
        mock_window.panes = [mock_pane]
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        result = client.get_history("test-session", "test-window")
        
        assert result == "line1\nline2\nline3"
        mock_pane.cmd.assert_called_once_with("capture-pane", "-e", "-p", "-S", "-200")
    
    def test_get_history_with_tail(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["line1", "line2"]
        mock_pane.cmd.return_value = mock_result
        mock_window.panes = [mock_pane]
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        result = client.get_history("test-session", "test-window", tail_lines=50)
        
        assert result == "line1\nline2"
        mock_pane.cmd.assert_called_once_with("capture-pane", "-e", "-p", "-S", "-50")
    
    def test_list_sessions(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session1 = MagicMock()
        mock_session1.name = "session1"
        mock_session1.attached_sessions = []
        
        mock_session2 = MagicMock()
        mock_session2.name = "session2"
        mock_session2.attached_sessions = ["client1"]
        
        mock_server.sessions = [mock_session1, mock_session2]
        
        result = client.list_sessions()
        
        assert len(result) == 2
        assert result[0]["name"] == "session1"
        assert result[0]["status"] == "detached"
        assert result[1]["name"] == "session2"
        assert result[1]["status"] == "active"
    
    def test_get_session_windows(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window1 = MagicMock()
        mock_window1.name = "window1"
        mock_window1.index = 0
        mock_window2 = MagicMock()
        mock_window2.name = "window2"
        mock_window2.index = 1
        mock_session.windows = [mock_window1, mock_window2]
        mock_server.sessions.get.return_value = mock_session
        
        result = client.get_session_windows("test-session")
        
        assert len(result) == 2
        assert result[0]["name"] == "window1"
        assert result[0]["index"] == "0"
        assert result[1]["name"] == "window2"
        assert result[1]["index"] == "1"
    
    def test_kill_session(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        
        result = client.kill_session("test-session")
        
        assert result is True
        mock_session.kill.assert_called_once()
    
    def test_session_exists_true(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        
        result = client.session_exists("test-session")
        assert result is True
    
    def test_session_exists_false(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_server.sessions.get.return_value = None
        
        result = client.session_exists("test-session")
        assert result is False
    
    def test_get_pane_working_directory(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["/current/working/dir"]
        mock_pane.cmd.return_value = mock_result
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        result = client.get_pane_working_directory("test-session", "test-window")
        
        assert result == "/current/working/dir"
        mock_pane.cmd.assert_called_once_with("display-message", "-p", "#{pane_current_path}")
    
    def test_pipe_pane_success(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        client.pipe_pane("test-session", "test-window", "/path/to/log.txt")
        
        mock_pane.cmd.assert_called_once_with("pipe-pane", "-o", "cat >> /path/to/log.txt")
    
    def test_stop_pipe_pane(self, tmux_client):
        client, mock_server = tmux_client
        
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        
        client.stop_pipe_pane("test-session", "test-window")
        
        mock_pane.cmd.assert_called_once_with("pipe-pane")