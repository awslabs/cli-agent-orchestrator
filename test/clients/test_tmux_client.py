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


# ── encoding regression ──────────────────────────────────────────────


class TestSubprocessEncoding:
    """Ensure all text-decoding subprocess.run calls use encoding='utf-8'.

    On Windows, the default locale encoding is cp1252.  psmux/tmux and the
    agents we drive emit UTF-8; without an explicit encoding the box-drawing
    characters (e.g. U+2500 ─) used by Claude Code's TUI are mis-decoded as
    cp1252 mojibake, breaking the init-detection regex.
    """

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_run_helper_uses_utf8(self, mock_sp, tmux):
        """_run() must pass encoding='utf-8' to subprocess.run."""
        mock_sp.run.return_value = _completed("ses1|0\n", returncode=0)
        tmux.list_sessions()

        _, kwargs = mock_sp.run.call_args
        assert kwargs.get("encoding") == "utf-8", (
            "_run() must use encoding='utf-8' (not the locale default)"
        )

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_uses_utf8(self, mock_sp, tmux, tmp_path):
        """create_session()'s explicit subprocess.run must pass encoding='utf-8'."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),           # new-session
            _completed("", returncode=0),           # set-window-option via _run
            _completed("", returncode=0),           # rename-window via _run
            _completed("win\n", returncode=0),      # list-windows via _run
        ]
        tmux.create_session("ses", "win", "tid1", str(tmp_path))

        # First call is the explicit new-session subprocess.run
        _, kwargs = mock_sp.run.call_args_list[0]
        assert kwargs.get("encoding") == "utf-8", (
            "create_session() must use encoding='utf-8' on the new-session call"
        )

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_uses_utf8(self, mock_sp, tmux, tmp_path):
        """create_window()'s explicit subprocess.run must pass encoding='utf-8'."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),           # has-session
            _completed("", returncode=0),           # new-window
            _completed("", returncode=0),           # set-window-option via _run
            _completed("", returncode=0),           # rename-window via _run
            _completed("win\n", returncode=0),      # list-windows via _run
        ]
        tmux.create_window("ses", "win", "tid1", str(tmp_path))

        # Second call is the explicit new-window subprocess.run
        _, kwargs = mock_sp.run.call_args_list[1]
        assert kwargs.get("encoding") == "utf-8", (
            "create_window() must use encoding='utf-8' on the new-window call"
        )


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
            _completed("", returncode=0),            # set-window-option (psmux-workaround)
            _completed("", returncode=0),            # rename-window (psmux-workaround)
            _completed("my-window\n", returncode=0), # list-windows
        ]
        result = tmux.create_session("ses", "my-window", "tid1", str(tmp_path))
        assert result == "my-window"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_window_name_none(self, mock_sp, tmux, tmp_path):
        # new-session succeeds, list-windows returns empty
        mock_sp.run.side_effect = [
            _completed("", returncode=0),  # new-session
            _completed("", returncode=0),  # set-window-option (psmux-workaround)
            _completed("", returncode=0),  # rename-window (psmux-workaround)
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

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_filters_non_posix_env_names(self, mock_sp, tmux, tmp_path):
        """Windows env vars with invalid POSIX names must not appear in -e args."""
        mock_sp.run.side_effect = [
            _completed("", returncode=0),            # new-session
            _completed("", returncode=0),            # set-window-option (psmux-workaround)
            _completed("", returncode=0),            # rename-window (psmux-workaround)
            _completed("win\n", returncode=0),       # list-windows
        ]
        fake_env = {
            "MY_VAR": "test",
            "PROGRAMFILES(X86)": "C:\\Program Files (x86)",
            "ASL.LOG": "Destination=file",
            "COMMONPROGRAMFILES(ARM)": "C:\\Program Files (ARM)",
        }
        with patch.dict("os.environ", fake_env, clear=True):
            tmux.create_session("ses", "win", "tid-posix", str(tmp_path))

        new_session_call = mock_sp.run.call_args_list[0]
        argv = new_session_call[0][0]  # positional first arg is the cmd list

        # POSIX-valid names must be present
        assert "-e" in argv
        assert any(a == "MY_VAR=test" for a in argv), "MY_VAR should be forwarded"
        assert any(a.startswith("CAO_TERMINAL_ID=") for a in argv), "CAO_TERMINAL_ID should be forwarded"

        # Non-POSIX names must be absent
        assert not any("PROGRAMFILES(X86)" in a for a in argv), "PROGRAMFILES(X86) must be filtered"
        assert not any("ASL.LOG" in a for a in argv), "ASL.LOG must be filtered"
        assert not any("COMMONPROGRAMFILES(ARM)" in a for a in argv), "COMMONPROGRAMFILES(ARM) must be filtered"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_disables_auto_rename_and_renames_window(self, mock_sp, tmux, tmp_path):
        """After new-session, set-window-option automatic-rename off and rename-window must be called.

        psmux 3.3.4 immediately overwrites the -n NAME with the active process
        name. We defensively disable auto-rename and re-assert the name so that
        CAO's (session, window_name) registry lookups always succeed.
        """
        mock_sp.run.side_effect = [
            _completed("", returncode=0),            # new-session
            _completed("", returncode=0),            # set-window-option
            _completed("", returncode=0),            # rename-window
            _completed("my-win\n", returncode=0),    # list-windows
        ]
        tmux.create_session("ses", "my-win", "tid1", str(tmp_path))

        calls = mock_sp.run.call_args_list
        cmds = [c[0][0] for c in calls]

        assert any(
            "set-window-option" in cmd and "ses:0" in cmd
            and "automatic-rename" in cmd and "off" in cmd
            for cmd in cmds
        ), "set-window-option automatic-rename off must be called with target ses:0"

        assert any(
            "rename-window" in cmd and "ses:0" in cmd and "my-win" in cmd
            for cmd in cmds
        ), "rename-window must be called with target ses:0 and the intended name"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_session_strips_trailing_backslash_from_env_values(self, mock_sp, tmux, tmp_path):
        """Env values ending in backslash must have trailing backslashes stripped.

        psmux 3.3.4 misparses Windows argv when an -e KEY=VAL value contains
        spaces AND ends with `\\` — it swallows all subsequent -e flags into
        that value. Strip trailing backslashes before building -e args so that
        CAO_TERMINAL_ID (appended last) is always delivered to the spawned agent.
        """
        mock_sp.run.side_effect = [
            _completed("", returncode=0),       # new-session
            _completed("", returncode=0),       # set-window-option (psmux-workaround)
            _completed("", returncode=0),       # rename-window (psmux-workaround)
            _completed("win\n", returncode=0),  # list-windows
        ]
        fake_env = {
            "TRAILING_BS_VAR": "C:\\Foo\\Bar\\",
            "NORMAL_VAR": "hello",
        }
        with patch.dict("os.environ", fake_env, clear=True):
            tmux.create_session("ses", "win", "abc123", str(tmp_path))

        argv = mock_sp.run.call_args_list[0][0][0]

        # Trailing backslash must be stripped
        assert any(a == "TRAILING_BS_VAR=C:\\Foo\\Bar" for a in argv), (
            "Trailing backslash must be stripped from env values"
        )
        # Value without trailing backslash must be unchanged
        assert any(a == "NORMAL_VAR=hello" for a in argv), (
            "Values without trailing backslash must be forwarded unchanged"
        )
        # CAO_TERMINAL_ID must still be present (not swallowed)
        assert any(a == "CAO_TERMINAL_ID=abc123" for a in argv), (
            "CAO_TERMINAL_ID must be present in -e args even after path-like values"
        )


# ── create_window ────────────────────────────────────────────────────


class TestCreateWindow:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_success(self, mock_sp, tmux, tmp_path):
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                        # has-session
            _completed("", returncode=0),                        # new-window
            _completed("", returncode=0),                        # set-window-option (psmux-workaround)
            _completed("", returncode=0),                        # rename-window (psmux-workaround)
            _completed("agent-window\n", returncode=0),          # list-windows
        ]
        result = tmux.create_window("ses", "agent-window", "tid2", str(tmp_path))
        assert result == "agent-window"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_create_window_disables_auto_rename_and_renames_window(self, mock_sp, tmux, tmp_path):
        """After new-window, set-window-option automatic-rename off and rename-window must be called.

        psmux 3.3.4 immediately overwrites the -n NAME with the active process
        name. We target the new window via ':^' (last window) and re-assert the
        intended name so that CAO's registry lookups always succeed.
        """
        mock_sp.run.side_effect = [
            _completed("", returncode=0),                   # has-session
            _completed("", returncode=0),                   # new-window
            _completed("", returncode=0),                   # set-window-option
            _completed("", returncode=0),                   # rename-window
            _completed("agent-win\n", returncode=0),        # list-windows
        ]
        tmux.create_window("ses", "agent-win", "tid2", str(tmp_path))

        calls = mock_sp.run.call_args_list
        cmds = [c[0][0] for c in calls]

        assert any(
            "set-window-option" in cmd and "ses:^" in cmd
            and "automatic-rename" in cmd and "off" in cmd
            for cmd in cmds
        ), "set-window-option automatic-rename off must be called with target ses:^"

        assert any(
            "rename-window" in cmd and "ses:^" in cmd and "agent-win" in cmd
            for cmd in cmds
        ), "rename-window must be called with target ses:^ and the intended name"

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
            _completed("", returncode=0),   # set-window-option (psmux-workaround)
            _completed("", returncode=0),   # rename-window (psmux-workaround)
            _completed("", returncode=0),   # list-windows (empty)
        ]
        with pytest.raises(ValueError, match="Window name is None"):
            tmux.create_window("ses", "w", "tid2", str(tmp_path))


# ── send_keys ────────────────────────────────────────────────────────


class TestSendKeys:
    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_success_unix(self, mock_subprocess, mock_time, mock_sys, tmux):
        mock_sys.platform = "linux"
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=1)

        # load-buffer, paste-buffer, send-keys Enter, delete-buffer
        assert mock_subprocess.run.call_count == 4

    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_multiple_enters_unix(self, mock_subprocess, mock_time, mock_sys, tmux):
        mock_sys.platform = "linux"
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=3)

        # load-buffer + paste-buffer + 3 send-keys Enter + delete-buffer = 6
        assert mock_subprocess.run.call_count == 6

    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_raises_on_failure(self, mock_subprocess, mock_time, mock_sys, tmux):
        mock_sys.platform = "linux"
        mock_subprocess.run.side_effect = Exception("tmux send failed")

        with pytest.raises(Exception, match="tmux send failed"):
            tmux.send_keys("ses", "win", "hello")

    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_windows_uses_send_keys_literal(self, mock_subprocess, mock_time, mock_sys, tmux):
        """On Windows, send_keys() must use send-keys -l and skip load/paste-buffer."""
        mock_sys.platform = "win32"
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=1)

        calls = mock_subprocess.run.call_args_list
        cmds = [c[0][0] for c in calls]

        # send-keys -l must be called with the literal text
        assert any(
            "send-keys" in cmd and "-l" in cmd and "hello" in cmd
            for cmd in cmds
        ), "Expected send-keys -l <text> call on Windows"

        # load-buffer and paste-buffer must NOT be called
        assert not any("load-buffer" in cmd for cmd in cmds), "load-buffer must not be called on Windows"
        assert not any("paste-buffer" in cmd for cmd in cmds), "paste-buffer must not be called on Windows"

    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_unix_uses_paste_buffer(self, mock_subprocess, mock_time, mock_sys, tmux):
        """On Unix, send_keys() must use the paste-buffer flow, not send-keys -l."""
        mock_sys.platform = "linux"
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=1)

        calls = mock_subprocess.run.call_args_list
        cmds = [c[0][0] for c in calls]

        assert any("load-buffer" in cmd for cmd in cmds), "load-buffer must be called on Unix"
        assert any("paste-buffer" in cmd for cmd in cmds), "paste-buffer must be called on Unix"
        assert not any(
            "send-keys" in cmd and "-l" in cmd
            for cmd in cmds
        ), "send-keys -l must not be called on Unix"


# ── send_keys_via_paste ──────────────────────────────────────────────


class TestSendKeysViaPaste:
    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_via_paste_success_unix(self, mock_sp, mock_time, mock_sys, tmux):
        mock_sys.platform = "linux"
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

    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_via_paste_windows_uses_send_keys_literal(self, mock_sp, mock_time, mock_sys, tmux):
        """On Windows, send_keys_via_paste() must use send-keys -l and skip set/paste-buffer."""
        mock_sys.platform = "win32"
        mock_sp.run.side_effect = [
            _completed("", returncode=0),           # has-session
            _completed("win\n", returncode=0),       # list-windows
            _completed("", returncode=0),            # send-keys -l
            _completed("", returncode=0),            # send-keys C-m
        ]
        tmux.send_keys_via_paste("ses", "win", "hello")

        calls = mock_sp.run.call_args_list
        cmds = [c[0][0] for c in calls]

        # send-keys -l must be called with the literal text
        assert any(
            "send-keys" in cmd and "-l" in cmd and "hello" in cmd
            for cmd in cmds
        ), "Expected send-keys -l <text> call on Windows"

        # C-m must still be sent
        assert any(
            "send-keys" in cmd and "C-m" in cmd
            for cmd in cmds
        ), "Expected send-keys C-m on Windows"

        # set-buffer and paste-buffer must NOT be called
        assert not any("set-buffer" in cmd for cmd in cmds), "set-buffer must not be called on Windows"
        assert not any("paste-buffer" in cmd for cmd in cmds), "paste-buffer must not be called on Windows"

    @patch("cli_agent_orchestrator.clients.tmux.sys")
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_via_paste_unix_uses_paste_buffer(self, mock_sp, mock_time, mock_sys, tmux):
        """On Unix, send_keys_via_paste() must use the set-buffer + paste-buffer flow."""
        mock_sys.platform = "linux"
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
        cmds = [c[0][0] for c in calls]

        assert any("set-buffer" in cmd for cmd in cmds), "set-buffer must be called on Unix"
        assert any("paste-buffer" in cmd for cmd in cmds), "paste-buffer must be called on Unix"
        assert not any(
            "send-keys" in cmd and "-l" in cmd
            for cmd in cmds
        ), "send-keys -l must not be called on Unix"


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
            "cao-test|0\n",
            returncode=0,
        )
        result = tmux.list_sessions()
        assert len(result) == 1
        assert result[0]["name"] == "cao-test"
        assert result[0]["status"] == "detached"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_list_sessions_attached(self, mock_sp, tmux):
        mock_sp.run.return_value = _completed(
            "cao-test|1\n",
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


# ── pwsh_join ────────────────────────────────────────────────────────


class TestPwshJoin:
    """Unit tests for the pwsh_join() helper.

    The critical invariant: the output must start with '& ' so that
    PowerShell treats the first token as a command name rather than
    a string expression.  Without '&', pwsh raises ParserError on the
    second quoted token and the launched command never starts.
    """

    def test_prepends_call_operator(self):
        """Output must start with '& ' unconditionally."""
        from cli_agent_orchestrator.clients.tmux import pwsh_join

        result = pwsh_join(["claude", "--dangerously-skip-permissions"])
        assert result.startswith("& "), (
            "pwsh_join must prepend '& ' so PowerShell invokes the command"
        )

    def test_basic_command_and_args(self):
        """Each token is single-quoted and joined with spaces after '& '."""
        from cli_agent_orchestrator.clients.tmux import pwsh_join

        result = pwsh_join(["claude", "--flag", "value"])
        assert result == "& 'claude' '--flag' 'value'"

    def test_single_token(self):
        """A single-element list still gets the '& ' prefix."""
        from cli_agent_orchestrator.clients.tmux import pwsh_join

        result = pwsh_join(["codex"])
        assert result == "& 'codex'"

    def test_embedded_single_quote_is_doubled(self):
        """Single quotes inside a token are doubled (PowerShell escape rule)."""
        from cli_agent_orchestrator.clients.tmux import pwsh_join

        result = pwsh_join(["--prompt", "it's alive"])
        assert "it''s alive" in result
        # The call operator must still be present
        assert result.startswith("& ")

    def test_order_preserved(self):
        """Tokens appear in the same order as the input list."""
        from cli_agent_orchestrator.clients.tmux import pwsh_join

        parts = ["cmd", "a", "b", "c"]
        result = pwsh_join(parts)
        assert result == "& 'cmd' 'a' 'b' 'c'"


