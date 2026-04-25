"""Unit tests for TMux client working directory methods."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a mock subprocess.CompletedProcess."""
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    m.stderr = stderr
    return m


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
                    result = client._resolve_and_validate_working_directory(None)
                    assert result == "/home/user/project"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Creating symlinks on Windows requires Developer Mode or elevated privileges",
    )
    def test_resolve_symlinks(self, tmp_path):
        """Test that symlinks are resolved to real paths."""
        client = TmuxClient()

        # Create real directory and symlink
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        real_dir_resolved = str(real_dir.resolve())
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

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_success(self, mock_sp):
        """Test successful working directory retrieval."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                       # has-session
            _completed("test-window\n", returncode=0),          # list-windows
            _completed("/home/user/project\n", returncode=0),   # display-message
        ]

        client = TmuxClient()
        result = client.get_pane_working_directory("test-session", "test-window")

        assert result == "/home/user/project"
        # display-message call uses #{pane_current_path} format
        dm_call = mock_sp.run.call_args_list[2]
        assert "display-message" in dm_call[0][0]
        assert "#{pane_current_path}" in dm_call[0][0]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_session_not_found(self, mock_sp):
        """Test returns None when session not found."""
        mock_sp.run.return_value = _completed("", returncode=1)  # has-session fails

        client = TmuxClient()
        result = client.get_pane_working_directory("nonexistent", "window")

        assert result is None

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_handles_exception(self, mock_sp):
        """Test exception handling returns None."""
        mock_sp.run.side_effect = Exception("Connection error")

        client = TmuxClient()
        result = client.get_pane_working_directory("session", "window")

        assert result is None

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_with_working_directory(self, mock_sp):
        """Test create_session passes working_directory to tmux."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),              # new-session
            _completed("test-window\n", returncode=0), # list-windows
        ]

        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user/test/dir"):
                result = client.create_session(
                    "test-session", "test-window", "terminal-1", "/home/user/test/dir"
                )

        assert result == "test-window"
        # new-session call should include -c and the working directory
        ns_call = mock_sp.run.call_args_list[0]
        cmd = ns_call[0][0]
        assert "-c" in cmd
        assert "/home/user/test/dir" in cmd

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_defaults_working_directory(self, mock_sp):
        """Test create_session with None working_directory uses cwd."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),              # new-session
            _completed("test-window\n", returncode=0), # list-windows
        ]

        client = TmuxClient()
        with patch("os.getcwd", return_value="/home/user/project"):
            with patch("os.path.isdir", return_value=True):
                with patch("os.path.realpath", return_value="/home/user/project"):
                    result = client.create_session(
                        "test-session", "test-window", "terminal-1", None
                    )

        assert result == "test-window"
        ns_call = mock_sp.run.call_args_list[0]
        cmd = ns_call[0][0]
        assert "-c" in cmd
        assert "/home/user/project" in cmd

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_expands_tilde_in_working_directory(self, mock_sp):
        """Test create_session expands ~ to home directory for remote clients."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),              # new-session
            _completed("test-window\n", returncode=0), # list-windows
        ]

        client = TmuxClient()
        with patch("os.path.expanduser", return_value="/home/user/q/my-project"):
            with patch("os.path.isdir", return_value=True):
                with patch("os.path.realpath", return_value="/home/user/q/my-project"):
                    result = client.create_session(
                        "test-session", "test-window", "terminal-1", "~/q/my-project"
                    )

        assert result == "test-window"
        ns_call = mock_sp.run.call_args_list[0]
        cmd = ns_call[0][0]
        assert "/home/user/q/my-project" in cmd

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_with_working_directory(self, mock_sp):
        """Test create_window passes working_directory to tmux."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),              # has-session
            _completed("", returncode=0),              # new-window
            _completed("test-window\n", returncode=0), # list-windows
        ]

        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user/test/dir"):
                result = client.create_window(
                    "test-session", "test-window", "terminal-1", "/home/user/test/dir"
                )

        assert result == "test-window"
        # new-window call should include -c and the working directory
        nw_call = mock_sp.run.call_args_list[1]
        cmd = nw_call[0][0]
        assert "-c" in cmd
        assert "/home/user/test/dir" in cmd

    def test_resolve_home_directory_itself(self):
        """Test that home directory itself is allowed."""
        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user"):
                result = client._resolve_and_validate_working_directory("/home/user")
        assert result == "/home/user"

    def test_allows_path_outside_home_directory(self):
        """Test that paths outside home are allowed if not in blocklist."""
        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/Volumes/workplace/project"):
                result = client._resolve_and_validate_working_directory(
                    "/Volumes/workplace/project"
                )
        assert result == "/Volumes/workplace/project"

    def test_allows_opt_directory(self):
        """Test that /opt paths are allowed (not in blocklist)."""
        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/opt/projects/my-app"):
                result = client._resolve_and_validate_working_directory("/opt/projects/my-app")
        assert result == "/opt/projects/my-app"

    def test_raises_for_blocked_system_directory(self):
        """Test ValueError for blocked system directories."""
        client = TmuxClient()
        for blocked in ["/etc", "/var", "/root", "/boot", "/tmp"]:
            with patch("os.path.realpath", return_value=blocked):
                with pytest.raises(ValueError, match="blocked system path"):
                    client._resolve_and_validate_working_directory(blocked)

    def test_allows_subdirectory_of_blocked_path(self):
        """Subdirectories under blocked paths are allowed (e.g., /var/folders on macOS)."""
        client = TmuxClient()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/var/folders/abc/project"):
                result = client._resolve_and_validate_working_directory("/var/folders/abc/project")
        assert result == "/var/folders/abc/project"

    def test_raises_for_root_directory(self):
        """Test ValueError for filesystem root."""
        client = TmuxClient()
        with patch("os.path.realpath", return_value="/"):
            with pytest.raises(ValueError, match="blocked system path"):
                client._resolve_and_validate_working_directory("/")

    def test_raises_for_symlink_to_blocked_path(self):
        """Test that symlinks resolving to blocked paths are rejected."""
        client = TmuxClient()

        # Mock realpath to simulate a symlink resolving to a blocked path
        # (on macOS /etc -> /private/etc, so we mock instead)
        with patch("os.path.realpath", return_value="/var"):
            with pytest.raises(ValueError, match="blocked system path"):
                client._resolve_and_validate_working_directory("/some/link")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Creating symlinks on Windows requires Developer Mode or elevated privileges",
    )
    def test_resolve_symlinked_home_directory(self, tmp_path):
        """Test that a symlinked home directory works (AWS /local/home pattern)."""
        client = TmuxClient()

        # Simulate AWS layout: /local/home/user is real, /home/user is a symlink
        real_home = tmp_path / "local" / "home" / "user"
        real_home.mkdir(parents=True)
        symlink_home = tmp_path / "home" / "user"
        symlink_home.parent.mkdir(parents=True)
        symlink_home.symlink_to(real_home)

        project_dir = real_home / "cli-agent-orchestrator"
        project_dir.mkdir()

        result = client._resolve_and_validate_working_directory(str(project_dir))
        assert result == str(project_dir.resolve())

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Creating symlinks on Windows requires Developer Mode or elevated privileges",
    )
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

        result = client._resolve_and_validate_working_directory(str(symlink_project))
        # Both resolve to the real path
        assert result == str(project_dir.resolve())

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_window_not_found(self, mock_sp):
        """Test returns None when window not found."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),           # has-session
            _completed("other\n", returncode=0),    # list-windows (different window)
        ]

        client = TmuxClient()
        result = client.get_pane_working_directory("test-session", "nonexistent-window")

        assert result is None

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_no_stdout(self, mock_sp):
        """Test returns None when display-message returns empty stdout."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),              # has-session
            _completed("test-window\n", returncode=0), # list-windows
            _completed("", returncode=0),              # display-message (empty)
        ]

        client = TmuxClient()
        result = client.get_pane_working_directory("test-session", "test-window")

        assert result is None
