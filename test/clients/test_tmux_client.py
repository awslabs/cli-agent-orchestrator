"""Tests for TmuxClient methods (mocked libtmux — no real tmux required)."""

import os
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture
def tmux():
    """Create a TmuxClient with a mocked libtmux.Server."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        client = TmuxClient()
        client.server = mock_server
        yield client


# ── _resolve_and_validate_working_directory ──────────────────────────


class TestResolveAndValidateWorkingDirectory:
    def test_defaults_to_cwd(self, tmux, tmp_path):
        with patch("os.getcwd", return_value=str(tmp_path)):
            result = tmux._resolve_and_validate_working_directory(None)
        assert result == os.path.realpath(str(tmp_path))

    def test_valid_directory(self, tmux, tmp_path):
        result = tmux._resolve_and_validate_working_directory(str(tmp_path))
        assert result == os.path.realpath(str(tmp_path))

    def test_blocked_root(self, tmux):
        with pytest.raises(ValueError, match="blocked system path"):
            tmux._resolve_and_validate_working_directory("/")

    def test_blocked_etc(self, tmux):
        with pytest.raises(ValueError, match="blocked system path"):
            tmux._resolve_and_validate_working_directory("/etc")

    def test_nonexistent_directory(self, tmux):
        with pytest.raises(ValueError, match="does not exist"):
            tmux._resolve_and_validate_working_directory("/nonexistent/dir/xyz")


# ── create_session ───────────────────────────────────────────────────


class TestCreateSession:
    def test_create_session_success(self, tmux, tmp_path):
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        result = tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        assert result == "my-window"
        tmux.server.new_session.assert_called_once()

    def test_create_session_window_name_none(self, tmux, tmp_path):
        mock_window = MagicMock()
        mock_window.name = None
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        with pytest.raises(ValueError, match="Window name is None"):
            tmux.create_session("ses", "w", "tid1", str(tmp_path))

    def test_create_session_raises_on_failure(self, tmux, tmp_path):
        tmux.server.new_session.side_effect = Exception("tmux error")

        with pytest.raises(Exception, match="tmux error"):
            tmux.create_session("ses", "w", "tid1", str(tmp_path))

    def test_create_session_uses_explicit_dimensions(self, tmux, tmp_path):
        """Guard against regressing the kiro-cli 2.1.x SIGWINCH-repaint bug (#216).

        Default detached pane is 80x24. When the user attaches, tmux resizes
        the pane to their real terminal size and kiro-cli 2.1.x fails to
        repaint (blank screen, input silently dropped). Creating the pane at
        220x50 makes the attach-time resize a no-op or shrink, which kiro
        handles correctly.
        """
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        kwargs = tmux.server.new_session.call_args.kwargs
        assert kwargs.get("x") == 220
        assert kwargs.get("y") == 50


# ── create_window ────────────────────────────────────────────────────


class TestCreateWindow:
    def test_create_window_success(self, tmux, tmp_path):
        mock_window = MagicMock()
        mock_window.name = "agent-window"
        mock_session = MagicMock()
        mock_session.new_window.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.create_window("ses", "agent-window", "tid2", str(tmp_path))

        assert result == "agent-window"

    def test_create_window_session_not_found(self, tmux, tmp_path):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            tmux.create_window("nonexistent", "w", "tid2", str(tmp_path))

    def test_create_window_name_none(self, tmux, tmp_path):
        mock_window = MagicMock()
        mock_window.name = None
        mock_session = MagicMock()
        mock_session.new_window.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="Window name is None"):
            tmux.create_window("ses", "w", "tid2", str(tmp_path))


# ── send_keys ────────────────────────────────────────────────────────


class TestSendKeys:
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_success(self, mock_subprocess, mock_time, tmux):
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=1)

        # load-buffer, paste-buffer, send-keys Enter, delete-buffer
        assert mock_subprocess.run.call_count == 4

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_multiple_enters(self, mock_subprocess, mock_time, tmux):
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=3)

        # load-buffer + paste-buffer + 3 send-keys Enter + delete-buffer = 6
        assert mock_subprocess.run.call_count == 6

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_raises_on_failure(self, mock_subprocess, mock_time, tmux):
        mock_subprocess.run.side_effect = Exception("tmux send failed")

        with pytest.raises(Exception, match="tmux send failed"):
            tmux.send_keys("ses", "win", "hello")


# ── send_keys_via_paste ──────────────────────────────────────────────


class TestSendKeysViaPaste:
    @patch("cli_agent_orchestrator.clients.tmux.time")
    def test_send_keys_via_paste_success(self, mock_time, tmux):
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        tmux.send_keys_via_paste("ses", "win", "hello")

        tmux.server.cmd.assert_any_call("set-buffer", "-b", "cao_paste", "hello")
        mock_pane.cmd.assert_called_once_with("paste-buffer", "-p", "-b", "cao_paste")
        mock_pane.send_keys.assert_called_once_with("C-m", enter=False)

    @patch("cli_agent_orchestrator.clients.tmux.time")
    def test_send_keys_via_paste_session_not_found(self, mock_time, tmux):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            tmux.send_keys_via_paste("nonexistent", "win", "hello")

    @patch("cli_agent_orchestrator.clients.tmux.time")
    def test_send_keys_via_paste_window_not_found(self, mock_time, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="not found"):
            tmux.send_keys_via_paste("ses", "nonexistent", "hello")


# ── send_special_key ─────────────────────────────────────────────────


class TestSendSpecialKey:
    def test_send_special_key_success(self, tmux):
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        tmux.send_special_key("ses", "win", "C-d")

        mock_pane.send_keys.assert_called_once_with("C-d", enter=False)

    def test_send_special_key_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            tmux.send_special_key("nonexistent", "win", "C-d")

    def test_send_special_key_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="not found"):
            tmux.send_special_key("ses", "nonexistent", "C-d")


# ── get_history ──────────────────────────────────────────────────────


class TestGetHistory:
    def test_get_history_success(self, tmux):
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["line1", "line2", "line3"]
        mock_pane.cmd.return_value = mock_result
        mock_window = MagicMock()
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_history("ses", "win")

        assert result == "line1\nline2\nline3"

    def test_get_history_empty_output(self, tmux):
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = []
        mock_pane.cmd.return_value = mock_result
        mock_window = MagicMock()
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_history("ses", "win")

        assert result == ""

    def test_get_history_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            tmux.get_history("nonexistent", "win")

    def test_get_history_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="not found"):
            tmux.get_history("ses", "nonexistent")

    def test_get_history_custom_tail_lines(self, tmux):
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["line"]
        mock_pane.cmd.return_value = mock_result
        mock_window = MagicMock()
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        tmux.get_history("ses", "win", tail_lines=50)

        mock_pane.cmd.assert_called_once_with("capture-pane", "-e", "-p", "-S", "-50")


# ── list_sessions ────────────────────────────────────────────────────


class TestListSessions:
    def test_list_sessions_success(self, tmux):
        mock_session = MagicMock()
        mock_session.name = "cao-test"
        mock_session.attached_sessions = []
        tmux.server.sessions = [mock_session]

        result = tmux.list_sessions()

        assert len(result) == 1
        assert result[0]["name"] == "cao-test"
        assert result[0]["status"] == "detached"

    def test_list_sessions_attached(self, tmux):
        mock_session = MagicMock()
        mock_session.name = "cao-test"
        mock_session.attached_sessions = [MagicMock()]
        tmux.server.sessions = [mock_session]

        result = tmux.list_sessions()

        assert result[0]["status"] == "active"

    def test_list_sessions_returns_empty_on_error(self, tmux):
        tmux.server.sessions = MagicMock(side_effect=Exception("no server"))
        tmux.server.sessions.__iter__ = MagicMock(side_effect=Exception("no server"))

        result = tmux.list_sessions()

        assert result == []


# ── get_session_windows ──────────────────────────────────────────────


class TestGetSessionWindows:
    def test_get_session_windows_success(self, tmux):
        mock_window = MagicMock()
        mock_window.name = "agent-win"
        mock_window.index = 0
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_session_windows("ses")

        assert len(result) == 1
        assert result[0]["name"] == "agent-win"

    def test_get_session_windows_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.get_session_windows("nonexistent")

        assert result == []

    def test_get_session_windows_error(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        result = tmux.get_session_windows("ses")

        assert result == []


# ── kill_session ─────────────────────────────────────────────────────


class TestKillSession:
    def test_kill_session_success(self, tmux):
        mock_session = MagicMock()
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.kill_session("ses")

        assert result is True
        mock_session.kill.assert_called_once()

    def test_kill_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.kill_session("nonexistent")

        assert result is False

    def test_kill_session_error(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        result = tmux.kill_session("ses")

        assert result is False


# ── kill_window ──────────────────────────────────────────────────────


class TestKillWindow:
    def test_kill_window_success(self, tmux):
        mock_window = MagicMock()
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.kill_window("ses", "win")

        assert result is True
        mock_window.kill.assert_called_once()

    def test_kill_window_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.kill_window("ses", "win")

        assert result is False

    def test_kill_window_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.kill_window("ses", "nonexistent")

        assert result is False

    def test_kill_window_error(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        result = tmux.kill_window("ses", "win")

        assert result is False


# ── session_exists ───────────────────────────────────────────────────


class TestSessionExists:
    def test_session_exists_true(self, tmux):
        tmux.server.sessions.get.return_value = MagicMock()

        assert tmux.session_exists("ses") is True

    def test_session_exists_false(self, tmux):
        tmux.server.sessions.get.return_value = None

        assert tmux.session_exists("ses") is False

    def test_session_exists_error(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        assert tmux.session_exists("ses") is False


# ── get_pane_working_directory ───────────────────────────────────────


class TestGetPaneWorkingDirectory:
    def test_get_pane_working_directory_success(self, tmux):
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["/home/user/project"]
        mock_pane.cmd.return_value = mock_result
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_pane_working_directory("ses", "win")

        assert result == "/home/user/project"

    def test_get_pane_working_directory_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.get_pane_working_directory("ses", "win")

        assert result is None

    def test_get_pane_working_directory_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_pane_working_directory("ses", "win")

        assert result is None

    def test_get_pane_working_directory_error(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        result = tmux.get_pane_working_directory("ses", "win")

        assert result is None


# ── pipe_pane / stop_pipe_pane ───────────────────────────────────────


class TestPipePane:
    def test_pipe_pane_success(self, tmux):
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        tmux.pipe_pane("ses", "win", "/tmp/log.txt")

        mock_pane.cmd.assert_called_once_with("pipe-pane", "-o", "cat >> /tmp/log.txt")

    def test_pipe_pane_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            tmux.pipe_pane("nonexistent", "win", "/tmp/log.txt")

    def test_pipe_pane_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="not found"):
            tmux.pipe_pane("ses", "nonexistent", "/tmp/log.txt")


class TestStopPipePane:
    def test_stop_pipe_pane_success(self, tmux):
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        tmux.stop_pipe_pane("ses", "win")

        mock_pane.cmd.assert_called_once_with("pipe-pane")

    def test_stop_pipe_pane_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            tmux.stop_pipe_pane("nonexistent", "win")

    def test_stop_pipe_pane_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        with pytest.raises(ValueError, match="not found"):
            tmux.stop_pipe_pane("ses", "nonexistent")
