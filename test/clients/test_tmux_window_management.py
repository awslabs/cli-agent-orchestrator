"""Tests for TmuxClient window and session management methods."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


@pytest.fixture
def client():
    """Create a TmuxClient with mocked libtmux."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        return TmuxClient()


@pytest.fixture
def mock_server(client):
    """Mock the tmux server with sessions and windows."""
    mock_server = MagicMock()
    client.server = mock_server
    return mock_server


class TestWindowExists:
    """Tests for window_exists method."""

    def test_window_exists_returns_true(self, client, mock_server):
        """Returns True when window exists in session."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        mock_server.sessions.get.return_value = mock_session

        result = client.window_exists("test-session", "test-window")

        assert result is True
        mock_server.sessions.get.assert_called_once_with(session_name="test-session")

    def test_window_exists_returns_false_when_window_not_found(self, client, mock_server):
        """Returns False when window doesn't exist in session."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "other-window"
        mock_session.windows = [mock_window]
        mock_server.sessions.get.return_value = mock_session

        result = client.window_exists("test-session", "test-window")

        assert result is False

    def test_window_exists_returns_false_when_session_not_found(self, client, mock_server):
        """Returns False when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.window_exists("nonexistent-session", "test-window")

        assert result is False

    def test_window_exists_handles_exception(self, client, mock_server):
        """Returns False when exception occurs during check."""
        mock_server.sessions.get.side_effect = Exception("tmux error")

        result = client.window_exists("test-session", "test-window")

        assert result is False

    def test_window_exists_with_multiple_windows(self, client, mock_server):
        """Finds correct window among multiple windows."""
        mock_session = MagicMock()
        mock_window1 = MagicMock()
        mock_window1.name = "window1"
        mock_window2 = MagicMock()
        mock_window2.name = "window2"
        mock_window3 = MagicMock()
        mock_window3.name = "window3"
        mock_session.windows = [mock_window1, mock_window2, mock_window3]
        mock_server.sessions.get.return_value = mock_session

        result = client.window_exists("test-session", "window2")

        assert result is True


class TestKillWindow:
    """Tests for kill_window method."""

    def test_kill_window_success(self, client, mock_server):
        """Successfully kills a window."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        result = client.kill_window("test-session", "test-window")

        assert result is True
        mock_server.sessions.get.assert_called_once_with(session_name="test-session")
        mock_session.windows.get.assert_called_once_with(window_name="test-window")
        mock_window.kill.assert_called_once()

    def test_kill_window_session_not_found(self, client, mock_server):
        """Returns False when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.kill_window("nonexistent-session", "test-window")

        assert result is False

    def test_kill_window_window_not_found(self, client, mock_server):
        """Returns False when window doesn't exist."""
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        mock_server.sessions.get.return_value = mock_session

        result = client.kill_window("test-session", "nonexistent-window")

        assert result is False

    def test_kill_window_handles_exception(self, client, mock_server):
        """Returns False when exception occurs during kill."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.kill.side_effect = Exception("tmux error")
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        result = client.kill_window("test-session", "test-window")

        assert result is False


class TestListSessions:
    """Tests for list_sessions method."""

    def test_list_sessions_success(self, client, mock_server):
        """Returns list of sessions."""
        mock_session1 = MagicMock()
        mock_session1.name = "session1"
        mock_session2 = MagicMock()
        mock_session2.name = "session2"
        mock_server.sessions = [mock_session1, mock_session2]

        with patch.object(client, "get_session") as mock_get_session:
            mock_get_session.side_effect = [
                {"name": "session1", "id": "1"},
                {"name": "session2", "id": "2"},
            ]
            result = client.list_sessions()

        assert len(result) == 2
        assert result[0]["name"] == "session1"
        assert result[1]["name"] == "session2"

    def test_list_sessions_handles_exception(self, client, mock_server):
        """Returns empty list when exception occurs."""
        mock_server.sessions = None

        result = client.list_sessions()

        assert result == []

    def test_list_sessions_filters_none_sessions(self, client, mock_server):
        """Filters out sessions where get_session returns None."""
        mock_session1 = MagicMock()
        mock_session1.name = "session1"
        mock_session2 = MagicMock()
        mock_session2.name = "session2"
        mock_server.sessions = [mock_session1, mock_session2]

        with patch.object(client, "get_session") as mock_get_session:
            mock_get_session.side_effect = [
                {"name": "session1", "id": "1"},
                None,  # session2 returns None
            ]
            result = client.list_sessions()

        assert len(result) == 1
        assert result[0]["name"] == "session1"


class TestGetSessionWindows:
    """Tests for get_session_windows method."""

    def test_get_session_windows_success(self, client, mock_server):
        """Returns list of windows in session."""
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
        assert result[0] == {"name": "window1", "index": "0"}
        assert result[1] == {"name": "window2", "index": "1"}

    def test_get_session_windows_session_not_found(self, client, mock_server):
        """Returns empty list when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.get_session_windows("nonexistent-session")

        assert result == []

    def test_get_session_windows_handles_exception(self, client, mock_server):
        """Returns empty list when exception occurs."""
        mock_server.sessions.get.side_effect = Exception("tmux error")

        result = client.get_session_windows("test-session")

        assert result == []


class TestKillSession:
    """Tests for kill_session method."""

    def test_kill_session_success(self, client, mock_server):
        """Successfully kills a session."""
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        result = client.kill_session("test-session")

        assert result is True
        mock_session.kill.assert_called_once()

    def test_kill_session_not_found(self, client, mock_server):
        """Returns False when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.kill_session("nonexistent-session")

        assert result is False

    def test_kill_session_handles_exception(self, client, mock_server):
        """Returns False when exception occurs."""
        mock_session = MagicMock()
        mock_session.kill.side_effect = Exception("tmux error")
        mock_server.sessions.get.return_value = mock_session

        result = client.kill_session("test-session")

        assert result is False


class TestSessionExists:
    """Tests for session_exists method."""

    def test_session_exists_returns_true(self, client, mock_server):
        """Returns True when session exists."""
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        result = client.session_exists("test-session")

        assert result is True

    def test_session_exists_returns_false(self, client, mock_server):
        """Returns False when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.session_exists("nonexistent-session")

        assert result is False

    def test_session_exists_handles_exception(self, client, mock_server):
        """Returns False when exception occurs."""
        mock_server.sessions.get.side_effect = Exception("tmux error")

        result = client.session_exists("test-session")

        assert result is False



class TestGetSession:
    """Tests for get_session method."""

    def test_get_session_attached(self, client, mock_server):
        """Returns session info when session is attached."""
        mock_session = MagicMock()
        mock_session.name = "test-session"
        mock_session.attached_sessions = [MagicMock()]  # Has attached sessions
        mock_server.sessions.get.return_value = mock_session

        result = client.get_session("test-session")

        assert result == {
            "id": "test-session",
            "name": "test-session",
            "status": "active",
        }

    def test_get_session_detached(self, client, mock_server):
        """Returns session info when session is detached."""
        mock_session = MagicMock()
        mock_session.name = "test-session"
        mock_session.attached_sessions = []  # No attached sessions
        mock_server.sessions.get.return_value = mock_session

        result = client.get_session("test-session")

        assert result == {
            "id": "test-session",
            "name": "test-session",
            "status": "detached",
        }

    def test_get_session_not_found(self, client, mock_server):
        """Returns None when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.get_session("nonexistent-session")

        assert result is None

    def test_get_session_handles_exception(self, client, mock_server):
        """Returns None when exception occurs."""
        mock_server.sessions.get.side_effect = Exception("tmux error")

        result = client.get_session("test-session")

        assert result is None


class TestGetPaneWorkingDirectory:
    """Tests for get_pane_working_directory method."""

    def test_get_pane_working_directory_success(self, client, mock_server):
        """Returns working directory when pane exists."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["/home/user/project"]
        mock_pane.cmd.return_value = mock_result
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        result = client.get_pane_working_directory("test-session", "test-window")

        assert result == "/home/user/project"
        mock_pane.cmd.assert_called_once_with("display-message", "-p", "#{pane_current_path}")

    def test_get_pane_working_directory_session_not_found(self, client, mock_server):
        """Returns None when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        result = client.get_pane_working_directory("nonexistent-session", "test-window")

        assert result is None

    def test_get_pane_working_directory_window_not_found(self, client, mock_server):
        """Returns None when window doesn't exist."""
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        mock_server.sessions.get.return_value = mock_session

        result = client.get_pane_working_directory("test-session", "nonexistent-window")

        assert result is None

    def test_get_pane_working_directory_no_active_pane(self, client, mock_server):
        """Returns None when no active pane."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = None
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        result = client.get_pane_working_directory("test-session", "test-window")

        assert result is None

    def test_get_pane_working_directory_handles_exception(self, client, mock_server):
        """Returns None when exception occurs."""
        mock_server.sessions.get.side_effect = Exception("tmux error")

        result = client.get_pane_working_directory("test-session", "test-window")

        assert result is None


class TestPipePane:
    """Tests for pipe_pane method."""

    def test_pipe_pane_success(self, client, mock_server):
        """Successfully starts pipe-pane."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        client.pipe_pane("test-session", "test-window", "/tmp/output.log")

        mock_pane.cmd.assert_called_once_with("pipe-pane", "-o", "cat >> /tmp/output.log")

    def test_pipe_pane_session_not_found(self, client, mock_server):
        """Raises ValueError when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="Session 'test-session' not found"):
            client.pipe_pane("test-session", "test-window", "/tmp/output.log")

    def test_pipe_pane_window_not_found(self, client, mock_server):
        """Raises ValueError when window doesn't exist."""
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        mock_server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="Window 'test-window' not found"):
            client.pipe_pane("test-session", "test-window", "/tmp/output.log")

    def test_pipe_pane_no_active_pane(self, client, mock_server):
        """Does nothing when no active pane."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = None
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        # Should not raise, just do nothing
        client.pipe_pane("test-session", "test-window", "/tmp/output.log")

    def test_pipe_pane_handles_exception(self, client, mock_server):
        """Raises exception when pipe-pane fails."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.cmd.side_effect = Exception("tmux error")
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        with pytest.raises(Exception, match="tmux error"):
            client.pipe_pane("test-session", "test-window", "/tmp/output.log")


class TestStopPipePane:
    """Tests for stop_pipe_pane method."""

    def test_stop_pipe_pane_success(self, client, mock_server):
        """Successfully stops pipe-pane."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        client.stop_pipe_pane("test-session", "test-window")

        mock_pane.cmd.assert_called_once_with("pipe-pane")

    def test_stop_pipe_pane_session_not_found(self, client, mock_server):
        """Raises ValueError when session doesn't exist."""
        mock_server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="Session 'test-session' not found"):
            client.stop_pipe_pane("test-session", "test-window")

    def test_stop_pipe_pane_window_not_found(self, client, mock_server):
        """Raises ValueError when window doesn't exist."""
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        mock_server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="Window 'test-window' not found"):
            client.stop_pipe_pane("test-session", "test-window")

    def test_stop_pipe_pane_no_active_pane(self, client, mock_server):
        """Does nothing when no active pane."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = None
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        # Should not raise, just do nothing
        client.stop_pipe_pane("test-session", "test-window")

    def test_stop_pipe_pane_handles_exception(self, client, mock_server):
        """Raises exception when stop fails."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.cmd.side_effect = Exception("tmux error")
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        with pytest.raises(Exception, match="tmux error"):
            client.stop_pipe_pane("test-session", "test-window")
