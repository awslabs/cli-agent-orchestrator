"""Tests for TmuxClient methods (mocked subprocess — no real tmux required)."""

import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture
def tmux():
    """Create a TmuxClient with libtmux import mocked out."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        return TmuxClient()


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a mock CompletedProcess."""
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    m.stderr = stderr
    return m


# ── _resolve_and_validate_working_directory ──────────────────────────


class TestResolveAndValidateWorkingDirectory:
    def test_defaults_to_cwd(self, tmux, tmp_path):
        with patch("os.getcwd", return_value=str(tmp_path)):
            result = tmux._resolve_and_validate_working_directory(None)
        assert result == os.path.realpath(str(tmp_path))

    def test_valid_directory(self, tmux, tmp_path):
        result = tmux._resolve_and_validate_working_directory(str(tmp_path))
        assert result == os.path.realpath(str(tmp_path))

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Unix blocked-path list (/,/etc) has no Windows equivalent; "
               "path resolves to drive root or does not exist on Windows",
    )
    def test_blocked_root(self, tmux):
        with pytest.raises(ValueError, match="blocked system path"):
            tmux._resolve_and_validate_working_directory("/")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="/etc does not exist on Windows so the error is 'does not exist', not 'blocked'",
    )
    def test_blocked_etc(self, tmux):
        with pytest.raises(ValueError, match="blocked system path"):
            tmux._resolve_and_validate_working_directory("/etc")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="/nonexistent/dir/xyz is a relative path on Windows (no drive letter)",
    )
    def test_nonexistent_directory(self, tmux):
        with pytest.raises(ValueError, match="does not exist"):
            tmux._resolve_and_validate_working_directory("/nonexistent/dir/xyz")


# ── create_session ───────────────────────────────────────────────────


class TestCreateSession:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_success(self, mock_sp, tmux, tmp_path):
        # new-session succeeds, list-windows returns one window
        mock_sp.run.side_effect = [
            _completed("", returncode=0),            # new-session
            _completed("my-window\n", returncode=0), # list-windows
        ]
        result = tmux.create_session("ses", "my-window", "tid1", str(tmp_path))
        assert result == "my-window"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_window_name_none(self, mock_sp, tmux, tmp_path):
        # new-session succeeds, list-windows returns empty
        mock_sp.run.side_effect = [
            _completed("", returncode=0),  # new-session
            _completed("", returncode=0),  # list-windows (empty)
        ]
        with pytest.raises(ValueError, match="Window name is None"):
            tmux.create_session("ses", "w", "tid1", str(tmp_path))

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_raises_on_failure(self, mock_sp, tmux, tmp_path):
        mock_sp.run.side_effect = [
            _completed("", returncode=1, stderr="tmux error"),  # new-session fails
        ]
        with pytest.raises(RuntimeError, match="tmux error"):
            tmux.create_session("ses", "w", "tid1", str(tmp_path))


# ── create_window ────────────────────────────────────────────────────


class TestCreateWindow:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_success(self, mock_sp, tmux, tmp_path):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                        # has-session
            _completed("", returncode=0),                        # new-window
            _completed("agent-window\n", returncode=0),          # list-windows
        ]
        result = tmux.create_window("ses", "agent-window", "tid2", str(tmp_path))
        assert result == "agent-window"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_session_not_found(self, mock_sp, tmux, tmp_path):
        mock_sp.run.return_value = _completed("", returncode=1)  # has-session fails
        with pytest.raises(ValueError, match="not found"):
            tmux.create_window("nonexistent", "w", "tid2", str(tmp_path))

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_name_none(self, mock_sp, tmux, tmp_path):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),   # has-session
            _completed("", returncode=0),   # new-window
            _completed("", returncode=0),   # list-windows (empty)
        ]
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
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_via_paste_success(self, mock_sp, mock_time, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),           # has-session
            _completed("win\n", returncode=0),       # list-windows
            _completed("", returncode=0),            # set-buffer
            _completed("", returncode=0),            # paste-buffer
            _completed("", returncode=0),            # send-keys C-m
            _completed("", returncode=0),            # delete-buffer
        ]
        tmux.send_keys_via_paste("ses", "win", "hello")

        calls = mock_sp.run.call_args_list
        # set-buffer call
        assert any("set-buffer" in str(c) for c in calls)
        # paste-buffer with -p
        assert any("paste-buffer" in str(c) and "-p" in str(c) for c in calls)
        # send-keys C-m
        assert any("send-keys" in str(c) and "C-m" in str(c) for c in calls)

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_via_paste_session_not_found(self, mock_sp, mock_time, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)  # has-session fails

        with pytest.raises(ValueError, match="not found"):
            tmux.send_keys_via_paste("nonexistent", "win", "hello")

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_via_paste_window_not_found(self, mock_sp, mock_time, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),    # has-session
            _completed("other\n", returncode=0),  # list-windows (different window)
        ]
        with pytest.raises(ValueError, match="not found"):
            tmux.send_keys_via_paste("ses", "nonexistent", "hello")


# ── send_special_key ─────────────────────────────────────────────────


class TestSendSpecialKey:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_special_key_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),       # has-session
            _completed("win\n", returncode=0),  # list-windows
            _completed("", returncode=0),        # send-keys
        ]
        tmux.send_special_key("ses", "win", "C-d")

        calls = mock_sp.run.call_args_list
        assert any("send-keys" in str(c) and "C-d" in str(c) for c in calls)

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_special_key_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)  # has-session fails

        with pytest.raises(ValueError, match="not found"):
            tmux.send_special_key("nonexistent", "win", "C-d")

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_special_key_window_not_found(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),       # has-session
            _completed("other\n", returncode=0), # list-windows (wrong window)
        ]
        with pytest.raises(ValueError, match="not found"):
            tmux.send_special_key("ses", "nonexistent", "C-d")


# ── get_history ──────────────────────────────────────────────────────


class TestGetHistory:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_history_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),            # has-session
            _completed("win\n", returncode=0),        # list-windows
            _completed("line1\nline2\nline3\n", returncode=0),  # capture-pane
        ]
        result = tmux.get_history("ses", "win")
        assert "line1" in result
        assert "line2" in result

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_history_empty_output(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),   # has-session
            _completed("win\n", returncode=0),  # list-windows
            _completed("", returncode=0),    # capture-pane (empty)
        ]
        result = tmux.get_history("ses", "win")
        assert result == ""

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_history_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        with pytest.raises(ValueError, match="not found"):
            tmux.get_history("nonexistent", "win")

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_history_window_not_found(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),        # has-session
            _completed("other\n", returncode=0), # list-windows (wrong window)
        ]
        with pytest.raises(ValueError, match="not found"):
            tmux.get_history("ses", "nonexistent")

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_history_custom_tail_lines(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),         # has-session
            _completed("win\n", returncode=0),    # list-windows
            _completed("line\n", returncode=0),   # capture-pane
        ]
        tmux.get_history("ses", "win", tail_lines=50)

        capture_call = mock_sp.run.call_args_list[2]
        assert "-50" in capture_call[0][0]


# ── list_sessions ────────────────────────────────────────────────────


class TestListSessions:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_list_sessions_success(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed(
            "cao-test: 1 windows (created Mon Jan  1 00:00:00 2024)\n",
            returncode=0,
        )
        result = tmux.list_sessions()
        assert len(result) == 1
        assert result[0]["name"] == "cao-test"
        assert result[0]["status"] == "detached"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_list_sessions_attached(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed(
            "cao-test: 1 windows (created Mon Jan  1 00:00:00 2024) (attached)\n",
            returncode=0,
        )
        result = tmux.list_sessions()
        assert result[0]["status"] == "active"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_list_sessions_returns_empty_on_error(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        result = tmux.list_sessions()
        assert result == []


# ── get_session_windows ──────────────────────────────────────────────


class TestGetSessionWindows:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_session_windows_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                # has-session
            _completed("agent-win|0\n", returncode=0),  # list-windows
        ]
        result = tmux.get_session_windows("ses")
        assert len(result) == 1
        assert result[0]["name"] == "agent-win"
        assert result[0]["index"] == "0"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_session_windows_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        result = tmux.get_session_windows("nonexistent")
        assert result == []

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_session_windows_error(self, mock_sp, tmux):
        mock_sp.run.side_effect = Exception("tmux error")
        result = tmux.get_session_windows("ses")
        assert result == []


# ── kill_session ─────────────────────────────────────────────────────


class TestKillSession:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_session_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),  # has-session
            _completed("", returncode=0),  # kill-session
        ]
        result = tmux.kill_session("ses")
        assert result is True

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)  # has-session fails
        result = tmux.kill_session("nonexistent")
        assert result is False

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_session_error(self, mock_sp, tmux):
        mock_sp.run.side_effect = Exception("tmux error")
        result = tmux.kill_session("ses")
        assert result is False


# ── kill_window ──────────────────────────────────────────────────────


class TestKillWindow:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_window_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),        # has-session
            _completed("win\n", returncode=0),   # list-windows
            _completed("", returncode=0),        # kill-window
        ]
        result = tmux.kill_window("ses", "win")
        assert result is True

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_window_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)  # has-session fails
        result = tmux.kill_window("ses", "win")
        assert result is False

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_window_window_not_found(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),         # has-session
            _completed("other\n", returncode=0),  # list-windows (window not there)
        ]
        result = tmux.kill_window("ses", "nonexistent")
        assert result is False

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_kill_window_error(self, mock_sp, tmux):
        mock_sp.run.side_effect = Exception("tmux error")
        result = tmux.kill_window("ses", "win")
        assert result is False


# ── session_exists ───────────────────────────────────────────────────


class TestSessionExists:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_session_exists_true(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=0)
        assert tmux.session_exists("ses") is True

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_session_exists_false(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        assert tmux.session_exists("ses") is False

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_session_exists_error(self, mock_sp, tmux):
        mock_sp.run.side_effect = Exception("tmux error")
        assert tmux.session_exists("ses") is False


# ── get_pane_working_directory ───────────────────────────────────────


class TestGetPaneWorkingDirectory:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                   # has-session
            _completed("win\n", returncode=0),              # list-windows
            _completed("/home/user/project\n", returncode=0),  # display-message
        ]
        result = tmux.get_pane_working_directory("ses", "win")
        assert result == "/home/user/project"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        result = tmux.get_pane_working_directory("ses", "win")
        assert result is None

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_window_not_found(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),         # has-session
            _completed("other\n", returncode=0),  # list-windows (wrong window)
        ]
        result = tmux.get_pane_working_directory("ses", "win")
        assert result is None

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_get_pane_working_directory_error(self, mock_sp, tmux):
        mock_sp.run.side_effect = Exception("tmux error")
        result = tmux.get_pane_working_directory("ses", "win")
        assert result is None


# ── pipe_pane / stop_pipe_pane ───────────────────────────────────────


class TestPipePane:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_pipe_pane_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),       # has-session
            _completed("win\n", returncode=0),  # list-windows
            _completed("", returncode=0),        # pipe-pane
        ]
        tmux.pipe_pane("ses", "win", "/tmp/log.txt")

        pipe_call = mock_sp.run.call_args_list[2]
        cmd = pipe_call[0][0]
        assert "pipe-pane" in cmd
        assert "-t" in cmd
        assert "ses:win" in cmd
        assert any("cat >> /tmp/log.txt" in str(a) for a in cmd)

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_pipe_pane_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        with pytest.raises(ValueError, match="not found"):
            tmux.pipe_pane("nonexistent", "win", "/tmp/log.txt")

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_pipe_pane_window_not_found(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),         # has-session
            _completed("other\n", returncode=0),  # list-windows
        ]
        with pytest.raises(ValueError, match="not found"):
            tmux.pipe_pane("ses", "nonexistent", "/tmp/log.txt")


class TestStopPipePane:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_stop_pipe_pane_success(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),       # has-session
            _completed("win\n", returncode=0),  # list-windows
            _completed("", returncode=0),        # pipe-pane
        ]
        tmux.stop_pipe_pane("ses", "win")

        pipe_call = mock_sp.run.call_args_list[2]
        cmd = pipe_call[0][0]
        assert "pipe-pane" in cmd
        assert "ses:win" in cmd

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_stop_pipe_pane_session_not_found(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed("", returncode=1)
        with pytest.raises(ValueError, match="not found"):
            tmux.stop_pipe_pane("nonexistent", "win")

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_stop_pipe_pane_window_not_found(self, mock_sp, tmux):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),         # has-session
            _completed("other\n", returncode=0),  # list-windows
        ]
        with pytest.raises(ValueError, match="not found"):
            tmux.stop_pipe_pane("ses", "nonexistent")


# ── _build_windows_env_prefix ────────────────────────────────────────
# These tests exercise the helper in isolation (no real shell required).
# The Windows-specific integration behaviour (env actually visible in shell)
# is guarded by skipif so it only runs on Windows.


class TestBuildWindowsEnvPrefix:
    """Unit tests for _build_windows_env_prefix quoting edge-cases."""

    def test_empty_env_returns_empty_string(self, tmux):
        result = tmux._build_windows_env_prefix({})
        assert result == ""

    def test_single_simple_var(self, tmux):
        result = tmux._build_windows_env_prefix({"FOO": "bar"})
        assert "$env:FOO = 'bar'" in result
        assert result.startswith("pwsh -NoProfile -Command ")
        assert result.endswith("pwsh -NoProfile\"")

    def test_value_with_spaces(self, tmux):
        result = tmux._build_windows_env_prefix({"MY_VAR": "hello world"})
        assert "$env:MY_VAR = 'hello world'" in result

    def test_value_with_single_quote_escaped(self, tmux):
        # Embedded ' must be doubled to '' in PowerShell single-quoted string
        result = tmux._build_windows_env_prefix({"TZ": "it's fine"})
        assert "$env:TZ = 'it''s fine'" in result

    def test_value_with_multiple_single_quotes(self, tmux):
        result = tmux._build_windows_env_prefix({"MSG": "don't won't"})
        assert "$env:MSG = 'don''t won''t'" in result

    def test_multiple_vars_all_present(self, tmux):
        env = {"A": "1", "B": "2", "C": "3"}
        result = tmux._build_windows_env_prefix(env)
        for k, v in env.items():
            assert f"$env:{k} = '{v}'" in result

    def test_no_double_quote_interpolation(self, tmux):
        # Values containing $ must not be treated as PowerShell variables.
        # Single-quoted strings prevent expansion — just verify $ survives.
        result = tmux._build_windows_env_prefix({"PATH_EXTRA": "$HOME/bin"})
        assert "$HOME/bin" in result

    def test_value_with_semicolons(self, tmux):
        # Semicolons inside a single-quoted string are safe (not separators).
        result = tmux._build_windows_env_prefix({"FLAGS": "a=1;b=2"})
        assert "$env:FLAGS = 'a=1;b=2'" in result

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Requires a real PowerShell process — only runs on Windows",
    )
    def test_env_visible_in_spawned_pwsh(self, tmux):
        """Smoke-test: env vars set by the prefix are visible in a child pwsh."""
        import subprocess

        cmd_str = tmux._build_windows_env_prefix({"_CAO_TEST_VAR": "hello_cao"})
        # Strip the outer pwsh wrapper and replace the trailing interactive shell
        # with a one-shot echo so the test terminates.
        # cmd_str = 'pwsh -NoProfile -Command "$env:X = 'Y'; pwsh -NoProfile"'
        # Replace the trailing `; pwsh -NoProfile"` with `; Write-Output $env:X"`
        inner = cmd_str.split(' -Command "', 1)[1].rstrip('"')
        inner = inner.rsplit("; pwsh -NoProfile", 1)[0]
        inner += "; Write-Output $env:_CAO_TEST_VAR"
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", inner],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "hello_cao" in result.stdout


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows env-prefix injection path only active on win32",
)
class TestCreateSessionWindowsEnvInjection:
    """Integration-level tests for env injection via the pwsh prefix on Windows."""

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_appends_shell_command_on_windows(self, mock_sp, tmux, tmp_path):
        """On Windows, create_session appends a pwsh env-prefix as the shell command."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),            # new-session
            _completed("my-window\n", returncode=0), # list-windows
        ]
        tmux.create_session("ses", "my-window", "tid-win", str(tmp_path))

        new_session_call = mock_sp.run.call_args_list[0]
        cmd = new_session_call[0][0]
        # Last element should be the pwsh prefix command
        last_arg = cmd[-1]
        assert last_arg.startswith("pwsh -NoProfile -Command")
        # CAO_TERMINAL_ID must be injected
        assert "CAO_TERMINAL_ID" in last_arg
        assert "tid-win" in last_arg

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_appends_shell_command_on_windows(self, mock_sp, tmux, tmp_path):
        """On Windows, create_window appends a pwsh env-prefix as the shell command."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                      # has-session
            _completed("", returncode=0),                      # new-window
            _completed("agent-window\n", returncode=0),        # list-windows
        ]
        tmux.create_window("ses", "agent-window", "tid-win2", str(tmp_path))

        new_window_call = mock_sp.run.call_args_list[1]
        cmd = new_window_call[0][0]
        last_arg = cmd[-1]
        assert last_arg.startswith("pwsh -NoProfile -Command")
        assert "CAO_TERMINAL_ID" in last_arg
        assert "tid-win2" in last_arg
