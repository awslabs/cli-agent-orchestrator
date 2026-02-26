"""Unit tests for TMux client working directory methods."""

import os
from pathlib import Path
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


class TestTmuxClientWorkingDirectory:
    """Test TMux client working directory functionality."""

    @pytest.fixture(autouse=True)
    def mock_tmux_server(self):
        """Mock libtmux.Server for all tests in this class."""
        with patch("cli_agent_orchestrator.clients.tmux.libtmux.Server") as mock_server_class:
            self.mock_server_class = mock_server_class
            self.mock_server = MagicMock()
            mock_server_class.return_value = self.mock_server
            yield mock_server_class

    def test_resolve_defaults_to_cwd(self):
        """Test that None defaults to current working directory."""
        client = TmuxClient()
        with patch("os.getcwd", return_value="/home/user/project"):
            with patch("os.path.realpath", return_value="/home/user/project"):
                with patch("os.path.isdir", return_value=True):
                    with patch("os.path.expanduser", return_value="/home/user"):
                        result = client._resolve_and_validate_working_directory(None)
                        assert result == "/home/user/project"

    def test_resolve_symlinks(self, tmp_path):
        """Test that symlinks are resolved to real paths."""
        client = TmuxClient()

        # Create real directory and symlink
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        real_dir_resolved = str(real_dir.resolve())
        # Mock expanduser so the tmp_path (outside ~) passes the home dir check
        parent = str(tmp_path.resolve())
        with patch("os.path.expanduser", return_value=parent):
            result = client._resolve_and_validate_working_directory(str(link_dir))
        assert result == real_dir_resolved

    def test_raises_for_nonexistent_directory(self):
        """Test ValueError for non-existent directory under home."""
        client = TmuxClient()
        # Use a path under home so it passes the containment check but fails isdir
        home = os.path.expanduser("~")
        fake_path = os.path.join(home, "nonexistent_dir_abc123")

        with pytest.raises(ValueError, match="Working directory does not exist"):
            client._resolve_and_validate_working_directory(fake_path)

    def test_get_pane_working_directory_success(self):
        """Test successful working directory retrieval."""
        # Setup mocks (use the fixture's mock_server)
        mock_session = Mock()
        mock_window = Mock()
        mock_pane = Mock()

        self.mock_server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        type(mock_window).active_pane = PropertyMock(return_value=mock_pane)

        # Mock pane.cmd() to return working directory
        mock_result = Mock()
        mock_result.stdout = ["/home/user/project"]
        mock_pane.cmd.return_value = mock_result

        client = TmuxClient()
        result = client.get_pane_working_directory("test-session", "test-window")

        assert result == "/home/user/project"
        mock_pane.cmd.assert_called_once_with("display-message", "-p", "#{pane_current_path}")

    def test_get_pane_working_directory_session_not_found(self):
        """Test returns None when session not found."""
        self.mock_server.sessions.get.return_value = None

        client = TmuxClient()
        result = client.get_pane_working_directory("nonexistent", "window")

        assert result is None

    def test_get_pane_working_directory_handles_exception(self):
        """Test exception handling returns None."""
        self.mock_server.sessions.get.side_effect = Exception("Connection error")

        client = TmuxClient()
        result = client.get_pane_working_directory("session", "window")

        assert result is None

    def test_create_session_with_working_directory(self):
        """Test create_session passes working_directory to tmux."""
        mock_session = Mock()
        mock_window = Mock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]

        self.mock_server.new_session.return_value = mock_session

        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user/test/dir"):
                with patch("os.path.expanduser", return_value="/home/user"):
                    result = client.create_session(
                        "test-session", "test-window", "terminal-1", "/home/user/test/dir"
                    )

        assert result == "test-window"
        self.mock_server.new_session.assert_called_once()
        call_args = self.mock_server.new_session.call_args
        assert call_args[1]["start_directory"] == "/home/user/test/dir"

    def test_create_session_defaults_working_directory(self):
        """Test create_session with None working_directory."""
        mock_session = Mock()
        mock_window = Mock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]

        self.mock_server.new_session.return_value = mock_session

        client = TmuxClient()
        with patch("os.getcwd", return_value="/home/user/project"):
            with patch("os.path.isdir", return_value=True):
                with patch("os.path.realpath", return_value="/home/user/project"):
                    with patch("os.path.expanduser", return_value="/home/user"):
                        result = client.create_session(
                            "test-session", "test-window", "terminal-1", None
                        )

        assert result == "test-window"
        self.mock_server.new_session.assert_called_once()
        call_args = self.mock_server.new_session.call_args
        assert call_args[1]["start_directory"] == "/home/user/project"

    def test_create_window_with_working_directory(self):
        """Test create_window passes working_directory to tmux."""
        mock_session = Mock()
        mock_window = Mock()
        mock_window.name = "test-window"

        self.mock_server.sessions.get.return_value = mock_session
        mock_session.new_window.return_value = mock_window

        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user/test/dir"):
                with patch("os.path.expanduser", return_value="/home/user"):
                    result = client.create_window(
                        "test-session", "test-window", "terminal-1", "/home/user/test/dir"
                    )

        assert result == "test-window"
        mock_session.new_window.assert_called_once()
        call_args = mock_session.new_window.call_args
        assert call_args[1]["start_directory"] == "/home/user/test/dir"

    def test_resolve_home_directory_itself(self):
        """Test that home directory itself is allowed."""
        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user"):
                with patch("os.path.expanduser", return_value="/home/user"):
                    result = client._resolve_and_validate_working_directory("/home/user")
        assert result == "/home/user"

    def test_raises_for_path_outside_home_directory(self):
        """Test ValueError for path outside user's home directory."""
        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.expanduser", return_value="/home/user"):
                with pytest.raises(ValueError, match="outside home directory"):
                    client._resolve_and_validate_working_directory("/opt/some/dir")

    def test_resolve_symlinked_home_directory(self, tmp_path):
        """Test that a symlinked home directory works (AWS /local/home pattern).

        On AWS environments, /home/user is often a symlink to /local/home/user.
        A working directory under /local/home/user should be allowed when ~ resolves
        to /home/user (which is a symlink to /local/home/user).
        """
        client = TmuxClient()

        # Simulate AWS layout: /local/home/user is real, /home/user is a symlink
        real_home = tmp_path / "local" / "home" / "user"
        real_home.mkdir(parents=True)
        symlink_home = tmp_path / "home" / "user"
        symlink_home.parent.mkdir(parents=True)
        symlink_home.symlink_to(real_home)

        project_dir = real_home / "cli-agent-orchestrator"
        project_dir.mkdir()

        # expanduser returns the symlink path (like /home/user)
        with patch("os.path.expanduser", return_value=str(symlink_home)):
            result = client._resolve_and_validate_working_directory(str(project_dir))

        # Should succeed — realpath resolves both to the same real tree
        assert result == str(project_dir.resolve())

    def test_resolve_symlinked_home_via_symlink_path(self, tmp_path):
        """Test passing the symlink path when home is symlinked."""
        client = TmuxClient()

        real_home = tmp_path / "local" / "home" / "user"
        real_home.mkdir(parents=True)
        symlink_home = tmp_path / "home" / "user"
        symlink_home.parent.mkdir(parents=True)
        symlink_home.symlink_to(real_home)

        project_dir = real_home / "project"
        project_dir.mkdir()

        # Pass the symlink-based path as working directory
        symlink_project = symlink_home / "project"

        with patch("os.path.expanduser", return_value=str(symlink_home)):
            result = client._resolve_and_validate_working_directory(str(symlink_project))

        # Both resolve to the real path
        assert result == str(project_dir.resolve())

    def test_get_pane_working_directory_window_not_found(self):
        """Test returns None when window not found."""
        mock_session = Mock()
        self.mock_server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = None

        client = TmuxClient()
        result = client.get_pane_working_directory("test-session", "nonexistent-window")

        assert result is None

    def test_get_pane_working_directory_no_stdout(self):
        """Test returns None when pane.cmd returns no stdout."""
        mock_session = Mock()
        mock_window = Mock()
        mock_pane = Mock()

        self.mock_server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        type(mock_window).active_pane = PropertyMock(return_value=mock_pane)

        mock_result = Mock()
        mock_result.stdout = []
        mock_pane.cmd.return_value = mock_result

        client = TmuxClient()
        result = client.get_pane_working_directory("test-session", "test-window")

        assert result is None
