"""Tests for TmuxClient methods (mocked libtmux — no real tmux required).

The one exception is ``TestRealTmuxExitEmpty`` at the bottom of this file,
which is deliberately NOT mocked — see its module docstring for why.
"""

import os
import shutil
import subprocess
import uuid
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture
def tmux():
    """Create a TmuxClient with a mocked libtmux.Server."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        # Sane default for server.cmd(): a successful tmux_cmd-shaped result.
        # A bare MagicMock() has a non-zero-comparing .returncode and a
        # non-iterable .stderr, so any test that does not care about the
        # underlying tmux invocation's outcome still gets a "success" shape
        # (see _set_server_exit_empty_off, which checks .returncode/.stderr
        # explicitly because libtmux 0.51's Server.cmd() does not raise on
        # tmux command failure).
        mock_server.cmd.return_value = MagicMock(returncode=0, stdout=[], stderr=[])
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

    def test_create_session_disables_exit_empty(self, tmux, tmp_path):
        """harness-control#845: creating a session must set the server-wide
        'exit-empty off' option (before new_session) so a transient empty moment
        during a mass teardown can't take the whole tmux server down.

        The option is applied via a single ``start-server ; set-option ...``
        tmux invocation (not two separate ``server.cmd()`` calls): a bare
        clean-server ``set-option`` never starts the server (see
        ``_set_server_exit_empty_off``'s docstring), and two SEPARATE
        commands ("start-server" then "set-option") still lose the race,
        because a server started with zero sessions evaluates the tmux
        default ``exit-empty on`` and can exit again before the second,
        separate client process connects.
        """
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        tmux.server.cmd.assert_any_call(
            "start-server", ";", "set-option", "-s", "exit-empty", "off"
        )

    def test_disables_exit_empty_before_new_session(self, tmux, tmp_path):
        """Copilot review (PR #599): ``assert_any_call`` only proves the call
        happened at SOME point, not that it happened before ``new_session`` —
        a regression that reordered the two would pass it unnoticed. Assert
        call ORDER instead, via ``mock_calls`` on the shared parent mock.
        """
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        call_names = [c[0] for c in tmux.server.mock_calls if c[0] in ("cmd", "new_session")]
        assert "cmd" in call_names
        assert "new_session" in call_names
        assert call_names.index("cmd") < call_names.index(
            "new_session"
        ), f"expected 'cmd' (exit-empty) before 'new_session', got order: {call_names}"

    def test_exit_empty_nonzero_returncode_does_not_block_launch(self, tmux, tmp_path):
        """libtmux 0.51's Server.cmd() RETURNS a failed tmux_cmd result instead
        of raising (this is exactly the shape that hid the reviewer-reported
        P2: a clean-server 'error connecting ...' status-1 result that no
        exception ever surfaced). Setting exit-empty is best-effort: a
        nonzero returncode must be logged, not raised, and must NOT abort the
        launch.
        """
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session
        tmux.server.cmd.return_value = MagicMock(
            returncode=1,
            stdout=[],
            stderr=["error connecting to /tmp/tmux-0/default (No such file or directory)"],
        )

        result = tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        assert result == "my-window"

    def test_exit_empty_failure_does_not_block_launch(self, tmux, tmp_path):
        """Setting exit-empty is best-effort: a failure must NOT abort the launch."""
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session
        tmux.server.cmd.side_effect = RuntimeError("tmux unavailable")

        # Must still succeed despite the set-option failure.
        result = tmux.create_session("ses", "my-window", "tid1", str(tmp_path))
        assert result == "my-window"

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

    def test_create_session_enables_mouse(self, tmux, tmp_path):
        """Mouse mode keeps wheel scroll inside tmux (#546): without it tmux
        forwards wheel events to the foreground application as Up/Down keys,
        so agent TUIs walk their input history instead of scrolling output.
        Session-level option: the user's other sessions are untouched."""
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        mock_session.set_option.assert_called_once_with("mouse", "on")

    def test_create_session_survives_mouse_option_failure(self, tmux, tmp_path):
        """set_option runs after new_session but outside the rollback guard,
        and libtmux raises on ANY set-option stderr -- if that propagated,
        a scroll convenience would orphan a live session and block relaunch
        under the same name. It must degrade to a warning instead."""
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        mock_session.set_option.side_effect = RuntimeError("unknown option: mouse")
        tmux.server.new_session.return_value = mock_session

        result = tmux.create_session("ses", "my-window", "tid1", str(tmp_path))

        assert result == "my-window"

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


class TestCreateSessionEnvironmentFiltering:
    """Tests for environment variable filtering in create_session (#242)."""

    def _get_passed_environment(self, tmux, tmp_path, env_override):
        mock_window = MagicMock()
        mock_window.name = "w"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        with patch.dict(os.environ, env_override, clear=True):
            tmux.create_session("ses", "w", "tid1", str(tmp_path))

        return tmux.server.new_session.call_args.kwargs["environment"]

    def test_essential_keys_always_passed(self, tmux, tmp_path):
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "PATH": "/usr/bin" * 500,
                "SHELL": "/bin/bash",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "LC_CTYPE": "UTF-8",
            },
        )
        assert env["HOME"] == "/home/user"
        assert env["PATH"] == "/usr/bin" * 500  # large PATH not dropped
        assert env["LC_ALL"] == "en_US.UTF-8"
        assert env["LC_CTYPE"] == "UTF-8"

    def test_blocked_prefixes_filtered(self, tmux, tmp_path):
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "CLAUDE_SESSION_ID": "abc",
                "CODEX_TOKEN": "secret",
                "__MISE_WATCH": "long_data",
            },
        )
        assert "CLAUDE_SESSION_ID" not in env
        assert "CODEX_TOKEN" not in env
        assert "__MISE_WATCH" not in env

    def test_allowed_claude_auth_vars_pass_through(self, tmux, tmp_path):
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_SKIP_FOUNDRY_AUTH": "1",
            },
        )
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["CLAUDE_CODE_SKIP_FOUNDRY_AUTH"] == "1"

    def test_cao_kiro_mise_aws_prefixes_pass(self, tmux, tmp_path):
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "CAO_TERMINAL_ID": "old",  # will be overwritten
                "CAO_SERVER_PORT": "9889",
                "KIRO_MODEL": "sonnet",
                "MISE_ENV": "dev",
                "AWS_PROFILE": "prod",
                "AWS_REGION": "us-east-1",
                "AWS_SESSION_TOKEN": "tok",
            },
        )
        assert env["CAO_SERVER_PORT"] == "9889"
        assert env["KIRO_MODEL"] == "sonnet"
        assert env["MISE_ENV"] == "dev"
        assert env["AWS_PROFILE"] == "prod"
        assert env["AWS_SESSION_TOKEN"] == "tok"
        # CAO_TERMINAL_ID is always overwritten
        assert env["CAO_TERMINAL_ID"] == "tid1"

    def test_large_prefix_vars_dropped(self, tmux, tmp_path):
        large_value = "x" * 2048  # exactly 2048 bytes, should be dropped (< 2048 fails)
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "CAO_BIG_VAR": large_value,
            },
        )
        assert "CAO_BIG_VAR" not in env

    def test_prefix_var_under_limit_passes(self, tmux, tmp_path):
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "CAO_SMALL": "x" * 2047,
            },
        )
        assert "CAO_SMALL" in env

    def test_unrecognized_vars_excluded(self, tmux, tmp_path):
        env = self._get_passed_environment(
            tmux,
            tmp_path,
            {
                "HOME": "/home/user",
                "RANDOM_VAR": "value",
                "MY_CUSTOM_THING": "data",
            },
        )
        assert "RANDOM_VAR" not in env
        assert "MY_CUSTOM_THING" not in env


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

    def test_create_window_with_window_shell(self, tmux, tmp_path):
        mock_window = MagicMock()
        mock_window.name = "restored-window"
        mock_session = MagicMock()
        mock_session.new_window.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.create_window(
            "ses", "restored-window", "tid2", str(tmp_path), window_shell="cat /tmp/x; exec bash -l"
        )

        assert result == "restored-window"
        call_kwargs = mock_session.new_window.call_args[1]
        assert call_kwargs["window_shell"] == "cat /tmp/x; exec bash -l"


# ── send_keys ────────────────────────────────────────────────────────


class TestSendKeys:
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_success(self, mock_subprocess, mock_time, tmux):
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=1)

        # copy-mode cancel, load-buffer, paste-buffer, pre-Enter cancel,
        # send-keys Enter, delete-buffer
        assert mock_subprocess.run.call_count == 6

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_multiple_enters(self, mock_subprocess, mock_time, tmux):
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=3)

        # copy-mode cancel + load-buffer + paste-buffer
        # + 3 x (pre-Enter cancel + send-keys Enter) + delete-buffer = 10
        assert mock_subprocess.run.call_count == 10

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_cancels_copy_mode_before_paste(self, mock_subprocess, mock_time, tmux):
        """A pane in copy mode consumes send-keys through the mode's key
        table instead of delivering them to the application, so the
        submitting Enter after paste-buffer is silently eaten (#654). The
        cancel must come first, and must not check the exit code: on a pane
        not in a mode the command fails with "not in a mode" by design."""
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello")

        first = mock_subprocess.run.call_args_list[0]
        assert first.args[0] == ["tmux", "send-keys", "-t", "ses:win", "-X", "cancel"]
        assert first.kwargs.get("check") is False

    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess")
    def test_send_keys_cancels_copy_mode_before_each_enter(self, mock_subprocess, mock_time, tmux):
        """The leading cancel alone is not enough: submit_delay is up to 2s
        (claude_code's paste_submit_delay), and a wheel scroll inside that
        window re-enters copy mode and eats the submitting Enter -- the
        message sits typed but unsubmitted (#654). Every Enter must be
        immediately preceded by its own cancel."""
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        tmux.send_keys("ses", "win", "hello", enter_count=2)

        calls = mock_subprocess.run.call_args_list
        cancel_argv = ["tmux", "send-keys", "-t", "ses:win", "-X", "cancel"]
        enter_argv = ["tmux", "send-keys", "-t", "ses:win", "Enter"]
        enter_indices = [i for i, c in enumerate(calls) if c.args[0] == enter_argv]
        assert len(enter_indices) == 2
        for i in enter_indices:
            assert calls[i - 1].args[0] == cancel_argv
            assert calls[i - 1].kwargs.get("check") is False

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
        # Copy-mode cancel (#654) must precede the paste, and again right
        # before the submitting C-m -- a wheel scroll during the 0.3s
        # post-paste sleep would re-enter copy mode and eat the submission.
        assert mock_pane.cmd.call_args_list[0] == call("send-keys", "-X", "cancel")
        assert mock_pane.cmd.call_args_list[1] == call("paste-buffer", "-p", "-b", "cao_paste")
        assert mock_pane.cmd.call_args_list[2] == call("send-keys", "-X", "cancel")
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

        # Copy-mode cancel (#654) must precede the key: a C-c/C-d sent into
        # an active mode is consumed by the mode's key table.
        mock_pane.cmd.assert_called_once_with("send-keys", "-X", "cancel")
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

    def test_get_history_full_history(self, tmux):
        mock_pane = MagicMock()
        mock_result = MagicMock()
        mock_result.stdout = ["line1", "line2"]
        mock_pane.cmd.return_value = mock_result
        mock_window = MagicMock()
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_history("ses", "win", strip_escapes=True, full_history=True)

        assert result == "line1\nline2"
        # full_history uses "-S" "-" (no line count), strip_escapes omits "-e"
        mock_pane.cmd.assert_called_once_with("capture-pane", "-p", "-S", "-")


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


def _cmd_result(returncode, stdout=(), stderr=()):
    """Build a stand-in for libtmux's ``tmux_cmd`` result object.

    ``session_exists_strict`` reads exactly three attributes off it, so this is
    the whole surface. Used to drive the verify poll, which now runs its own
    ``list-sessions`` instead of touching ``server.sessions`` (#498).
    """
    result = MagicMock()
    result.returncode = returncode
    result.stdout = list(stdout)
    result.stderr = list(stderr)
    return result


class TestKillSession:
    def test_kill_session_success(self, tmux):
        mock_session = MagicMock()
        tmux.server.sessions.get.return_value = mock_session
        # The strict verify runs list-sessions: exit 0 with "ses" absent from the
        # name list is an authoritative "gone" (#498).
        tmux.server.cmd.return_value = _cmd_result(0, stdout=["other"])

        result = tmux.kill_session("ses")

        assert result is True
        mock_session.kill.assert_called_once()

    def test_kill_session_polls_until_session_confirmed_gone(self, tmux, monkeypatch):
        """The BOUNDED RETRY loop is what makes True mean "confirmed gone".

        tmux does not always reap a session synchronously with ``session.kill()``,
        so the primitive polls. Here the session is still listed on the first
        verify and only absent on the second: kill_session must keep polling and
        return True, having slept between attempts. Only immediate-success and
        the timeout=0 path were covered before, leaving the retry loop — the
        whole point of the confirmation contract — unexercised (#498).
        """
        mock_session = MagicMock()
        tmux.server.sessions.get.return_value = mock_session
        # 1st verify: still listed -> must sleep and retry. 2nd: gone -> True.
        tmux.server.cmd.side_effect = [
            _cmd_result(0, stdout=["ses"]),
            _cmd_result(0, stdout=[]),
        ]
        sleeps: list[float] = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.tmux.time.sleep", lambda s: sleeps.append(s)
        )

        result = tmux.kill_session("ses")

        assert result is True
        mock_session.kill.assert_called_once()
        # Exactly one retry: it slept once, between the alive verify and the
        # one that confirmed absence.
        assert sleeps == [tmux._KILL_SESSION_VERIFY_INTERVAL_SECONDS]
        assert tmux.server.cmd.call_count == 2

    def test_kill_session_lookup_error_during_verify_is_not_gone(self, tmux, monkeypatch):
        """A transient lookup error during the verification poll must NOT be
        read as "session gone": kill_session returns False, never a false True
        (#498)."""
        mock_session = MagicMock()
        tmux.server.sessions.get.return_value = mock_session
        # Found on the initial lookup; the verify's list-sessions then fails in a
        # way that is NOT an absence (permission denied), so the strict check
        # raises TmuxLookupError, which must be caught as a failed kill.
        tmux.server.cmd.return_value = _cmd_result(
            1, stderr=["error connecting to /tmp/x.sock (Permission denied)"]
        )
        monkeypatch.setattr(tmux, "_KILL_SESSION_VERIFY_TIMEOUT_SECONDS", 0)

        result = tmux.kill_session("ses")

        assert result is False
        mock_session.kill.assert_called_once()

    def test_kill_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.kill_session("nonexistent")

        assert result is False

    def test_kill_session_error(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        result = tmux.kill_session("ses")

        assert result is False

    def test_kill_session_returns_false_when_session_survives(self, tmux, monkeypatch):
        mock_session = MagicMock()
        tmux.server.sessions.get.return_value = mock_session
        # Every verify authoritatively still lists the session, so the bounded
        # poll expires without confirmation.
        tmux.server.cmd.return_value = _cmd_result(0, stdout=["ses"])
        monkeypatch.setattr(tmux, "_KILL_SESSION_VERIFY_TIMEOUT_SECONDS", 0)

        result = tmux.kill_session("ses")

        assert result is False
        mock_session.kill.assert_called_once()


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


class TestGetPaneCurrentCommand:
    def test_get_pane_current_command_success(self, tmux):
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.cmd.return_value.stdout = ["bash"]
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_pane_current_command("ses", "win")

        assert result == "bash"
        mock_pane.cmd.assert_called_once_with("display-message", "-p", "#{pane_current_command}")

    def test_get_pane_current_command_session_not_found(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.get_pane_current_command("nonexistent", "win")

        assert result is None

    def test_get_pane_current_command_window_not_found(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = mock_session

        result = tmux.get_pane_current_command("ses", "nonexistent")

        assert result is None

    def test_get_pane_current_command_exception_returns_none(self, tmux):
        tmux.server.sessions.get.side_effect = Exception("tmux error")

        result = tmux.get_pane_current_command("ses", "win")

        assert result is None


class TestPaneIsBracketedPasteIncompatible:
    @pytest.mark.parametrize(
        "shell", ["sh", "dash", "bash", "zsh", "ksh", "mksh", "csh", "tcsh", "fish", "ash"]
    )
    def test_every_known_shell_is_incompatible(self, tmux, shell):
        with patch.object(tmux, "get_pane_current_command", return_value=shell):
            assert tmux._pane_is_bracketed_paste_incompatible("ses", "win") is True

    @pytest.mark.parametrize("program", ["node", "claude", "kiro-cli", "python3", "codex"])
    def test_known_tui_programs_are_compatible(self, tmux, program):
        with patch.object(tmux, "get_pane_current_command", return_value=program):
            assert tmux._pane_is_bracketed_paste_incompatible("ses", "win") is False

    def test_lookup_failure_is_treated_as_compatible(self, tmux):
        """Fails closed to the existing (pre-fix) behavior on an
        unresolvable pane command -- see send_keys' own docstring."""
        with patch.object(tmux, "get_pane_current_command", return_value=None):
            assert tmux._pane_is_bracketed_paste_incompatible("ses", "win") is False


# ── real-tmux regression guard for the exit-empty gap (PR #599) ────────
#
# Every test above mocks libtmux.Server entirely, which is exactly why
# haofeif's P2 finding (harness-control#845 follow-up) shipped unnoticed:
# the mock-based TestCreateSession tests only prove ``server.cmd(...)`` was
# CALLED with the right arguments, never that a real tmux server actually
# ends up with ``exit-empty off`` in force. The bug was a genuine tmux/
# libtmux interaction (a clean-server ``set-option`` failing to connect,
# silently, because libtmux 0.51's ``Server.cmd()`` returns rather than
# raises) that no amount of mock-call assertions could catch. This class
# exercises the REAL ``TmuxClient`` against a REAL, isolated tmux server —
# the same way the reviewer reproduced the finding — and is the guard that
# would have failed on the pre-fix code.


@pytest.mark.integration
class TestRealTmuxExitEmpty:
    """Drives the real tmux client end-to-end on an isolated socket.

    Marked ``integration`` (not ``e2e``): this repo's default ``pytest``
    invocation only excludes ``e2e`` (``pyproject.toml`` ``addopts``, and
    CI's "Unit Tests" job), so an ``integration``-marked test still runs in
    CI/build-and-test; the documented fast inner dev loop explicitly adds
    ``-m 'not integration'`` on top (DEVELOPMENT.md) to skip it. tmux itself
    is a project prerequisite, not an optional provider CLI (DEVELOPMENT.md:
    "tmux 3.2+ (for running the orchestrator and integration tests)"), so
    this is the correct tier — lighter than ``e2e`` (no CAO server, no
    provider CLI, no auth), but still a real subprocess/real-tmux test that
    the fast unit loop is allowed to skip.

    Each test gets its own uniquely-named tmux socket (no shared state with
    any other test, this repo's dev tmux server, or a parallel xdist worker)
    and the socket's server is killed in a ``finally`` regardless of outcome.
    """

    def _require_tmux(self) -> None:
        if not shutil.which("tmux"):
            pytest.skip("tmux not installed")

    def _show_exit_empty(self, socket_name: str) -> str:
        """Read exit-empty via a bare ``tmux`` CLI call — deliberately NOT
        through libtmux/TmuxClient, so this assertion does not share any code
        path with the thing under test."""
        result = subprocess.run(
            ["tmux", "-L", socket_name, "show-options", "-s", "exit-empty"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"'tmux show-options' failed unexpectedly: {result.stderr}"
        return result.stdout.strip()

    def test_exit_empty_off_after_first_create_session_on_clean_server(self, tmp_path):
        """The reviewer-reported P2, reproduced and guarded directly: on a
        CLEAN server (no prior sessions), 'exit-empty' must already read
        'off' after the very FIRST ``create_session()`` call — not only
        after a second one. Pre-fix, this assertion fails: the first call
        left the option at the tmux default 'on'.
        """
        self._require_tmux()

        import libtmux

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        socket_name = f"cao-test-exit-empty-{uuid.uuid4().hex[:12]}"
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)

        client = TmuxClient()
        client.server = libtmux.Server(socket_name=socket_name)

        try:
            window_name = client.create_session(
                "exit-empty-probe", "win1", "term-real-tmux-1", str(tmp_path)
            )
            assert window_name == "win1"

            assert self._show_exit_empty(socket_name) == "exit-empty off", (
                "exit-empty must be 'off' after the FIRST create_session() on a "
                "clean server (harness-control#845 / PR #599 review finding)"
            )
        finally:
            subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)

    def test_exit_empty_off_survives_external_server_restart(self, tmp_path):
        """The same gap reopens after an externally-restarted server (a
        crash, an admin ``kill-server``, ...): the reviewer noted this
        lifecycle is "still exposed to the teardown/create race this PR is
        meant to close" for exactly this reason. Simulate it directly:
        create a session, kill the server out from under the client, then
        create another session on the same socket and assert the option is
        back in force after that first post-restart create_session().
        """
        self._require_tmux()

        import libtmux

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        socket_name = f"cao-test-exit-empty-restart-{uuid.uuid4().hex[:12]}"
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)

        client = TmuxClient()
        client.server = libtmux.Server(socket_name=socket_name)

        try:
            client.create_session("restart-probe-1", "win1", "term-real-tmux-2", str(tmp_path))
            assert self._show_exit_empty(socket_name) == "exit-empty off"

            # Simulate an external restart (crash / admin action): the whole
            # server process goes away, socket included.
            subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)

            client.create_session("restart-probe-2", "win2", "term-real-tmux-3", str(tmp_path))
            assert self._show_exit_empty(socket_name) == "exit-empty off", (
                "exit-empty must be back 'off' after the first create_session() "
                "following an external server restart on the same socket"
            )
        finally:
            subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)
