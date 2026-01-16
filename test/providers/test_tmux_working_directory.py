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
        with patch("os.getcwd", return_value="/current/dir"):
            with patch("os.path.isdir", return_value=True):
                result = client._resolve_and_validate_working_directory(None)
                assert result == os.path.realpath("/current/dir")

    def test_resolve_symlinks(self, tmp_path):
        """Test that symlinks are resolved to real paths."""
        client = TmuxClient()

        # Create real directory and symlink
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        result = client._resolve_and_validate_working_directory(str(link_dir))
        assert result == str(real_dir.resolve())

    def test_raises_for_nonexistent_directory(self):
        """Test ValueError for non-existent directory."""
        client = TmuxClient()

        with pytest.raises(ValueError, match="Working directory does not exist"):
            client._resolve_and_validate_working_directory("/nonexistent/path")

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
