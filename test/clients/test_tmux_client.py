"""Tests for TmuxClient methods (mocked libtmux — no real tmux required)."""

import contextlib
import io
import os
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest
from libtmux.exc import ObjectDoesNotExist


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


class TestTerminalBoundWindowIdentity:
    @staticmethod
    def _candidate(tmux):
        pane = MagicMock()
        pane.pane_id = "%9"
        pane.pane_pid = "101"
        window = MagicMock()
        window.window_id = "@7"
        window.active_pane = pane
        session = MagicMock()
        session.windows.get.return_value = window
        tmux.server.sessions.get.return_value = session
        return window

    def test_requires_process_lineage_terminal_binding(self, tmux):
        self._candidate(tmux)
        with (
            patch.object(tmux, "_descendant_processes", return_value=[101, 102]),
            patch.object(
                tmux,
                "_process_has_terminal_id",
                side_effect=lambda pid, terminal_id: pid == 102 and terminal_id == "abcd1234",
            ),
        ):
            result = tmux.terminal_bound_window_identity("abcd1234", "cao-session", "supervisor")

        assert result == {"pane_id": "%9", "window_id": "@7"}

    def test_wrong_or_reused_pane_is_not_proof(self, tmux):
        self._candidate(tmux)
        with (
            patch.object(tmux, "_descendant_processes", return_value=[101, 102]),
            patch.object(tmux, "_process_has_terminal_id", return_value=False),
        ):
            result = tmux.terminal_bound_window_identity("abcd1234", "cao-session", "supervisor")

        assert result is None

    def test_partial_identity_is_not_returned(self, tmux):
        window = self._candidate(tmux)
        window.window_id = None
        with patch.object(tmux, "_descendant_processes") as descendants:
            result = tmux.terminal_bound_window_identity("abcd1234", "cao-session", "supervisor")

        assert result is None
        descendants.assert_not_called()

    def test_descendant_processes_returns_bounded_tree(self, tmux):
        observed = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="101 1\n102 101\n103 102\n200 1\n",
        )
        with (
            patch("cli_agent_orchestrator.clients.tmux.shutil.which", return_value="/bin/ps"),
            patch(
                "cli_agent_orchestrator.clients.tmux.subprocess.run",
                return_value=observed,
            ),
        ):
            assert tmux._descendant_processes(101) == [101, 102, 103]

    def test_descendant_processes_refuses_malformed_or_oversized_observation(self, tmux):
        malformed = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="not-a-process-row\n",
        )
        children = "\n".join(f"{pid} 101" for pid in range(102, 167))
        oversized = subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=children)
        with (
            patch("cli_agent_orchestrator.clients.tmux.shutil.which", return_value="/bin/ps"),
            patch(
                "cli_agent_orchestrator.clients.tmux.subprocess.run",
                side_effect=[malformed, oversized],
            ),
        ):
            assert tmux._descendant_processes(101) is None
            assert tmux._descendant_processes(101) is None

    def test_descendant_processes_refuses_failed_observation(self, tmux):
        failed = subprocess.CompletedProcess(args=["ps"], returncode=1, stdout="")
        with (
            patch("cli_agent_orchestrator.clients.tmux.shutil.which", return_value="/bin/ps"),
            patch(
                "cli_agent_orchestrator.clients.tmux.subprocess.run",
                side_effect=[subprocess.TimeoutExpired("ps", 2), failed],
            ),
        ):
            assert tmux._descendant_processes(101) is None
            assert tmux._descendant_processes(101) is None

    def test_process_terminal_id_reads_exact_environment_entry(self, tmux):
        environment = io.BytesIO(b"PATH=/bin\0CAO_TERMINAL_ID=abcd1234\0")
        with patch("builtins.open", return_value=environment):
            assert tmux._process_has_terminal_id(101, "abcd1234") is True

    def test_process_terminal_id_uses_fail_closed_ps_fallback(self, tmux):
        matching = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=b"agent CAO_TERMINAL_ID=abcd1234 OTHER=value",
        )
        failed = subprocess.CompletedProcess(args=["ps"], returncode=1, stdout=b"")
        with (
            patch("builtins.open", side_effect=OSError),
            patch("cli_agent_orchestrator.clients.tmux.shutil.which", return_value="/bin/ps"),
            patch(
                "cli_agent_orchestrator.clients.tmux.subprocess.run",
                side_effect=[matching, failed],
            ),
        ):
            assert tmux._process_has_terminal_id(101, "abcd1234") is True
            assert tmux._process_has_terminal_id(101, "abcd1234") is False


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
    """History/liveness observation is narrow and bounded (COND-0242).

    It resolves one window's panes and captures that exact pane id. It must
    never reach libtmux's ``server.sessions`` / ``session.windows``, whose
    server-wide 136-field listings made one malformed row anywhere on the
    server fail an unrelated window's read and put every read in contention
    with ordinary API work.
    """

    TMUX = "/usr/bin/tmux"

    @staticmethod
    def _window_of(argv):
        """The window name the resolver asked tmux for, from its own argv."""
        target = argv[argv.index("-t") + 1]
        return target.split(":=", 1)[1]

    @classmethod
    def _fake_tmux(cls, *, panes=None, capture=""):
        """Record the argv of every tmux observation and answer it.

        ``panes=None`` synthesizes the row real tmux would return for the
        window that was actually asked for — pane id and window name — so the
        resolver's self-verification is exercised rather than bypassed.
        """
        calls: list = []

        def run(cmd, *args, **kwargs):
            argv = list(cmd)
            calls.append(argv)
            if "list-panes" in argv:
                stdout = panes if panes is not None else f"%0\t{cls._window_of(argv)}\n"
            else:
                stdout = capture
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        return run, calls

    @classmethod
    @contextlib.contextmanager
    def _observing(cls, run):
        """Every observation answered by ``run``, against a pinned tmux path so
        the test never depends on tmux being installed."""
        with (
            patch("cli_agent_orchestrator.clients.tmux.subprocess.run", run),
            patch("cli_agent_orchestrator.clients.tmux.tmux_binary", return_value=cls.TMUX),
        ):
            yield

    def test_get_history_success(self, tmux):
        run, calls = self._fake_tmux(capture="line1\nline2\nline3\n")
        with self._observing(run):
            result = tmux.get_history("ses", "win")

        assert result == "line1\nline2\nline3"
        tmux.server.sessions.get.assert_not_called()
        assert [c for c in calls if "list-panes" in c], "the pane is resolved one window at a time"
        assert not any(
            "list-sessions" in c or "-a" in c for c in calls
        ), "no server-wide observation may be issued to read one pane"

    def test_get_history_empty_output(self, tmux):
        run, _ = self._fake_tmux(capture="")
        with self._observing(run):
            assert tmux.get_history("ses", "win") == ""

    def test_get_history_session_not_found(self, tmux):
        def run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                list(cmd), 1, stdout="", stderr="can't find session: nonexistent\n"
            )

        with self._observing(run):
            with pytest.raises(ValueError, match="not found"):
                tmux.get_history("nonexistent", "win")

    def test_get_history_window_not_found(self, tmux):
        def run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                list(cmd), 1, stdout="", stderr="can't find window: nonexistent\n"
            )

        with self._observing(run):
            with pytest.raises(ValueError, match="not found"):
                tmux.get_history("ses", "nonexistent")

    def test_get_history_custom_tail_lines(self, tmux):
        run, calls = self._fake_tmux(capture="line\n")
        with self._observing(run):
            tmux.get_history("ses", "win", tail_lines=50)

        capture = next(c for c in calls if "capture-pane" in c)
        assert capture[1:] == ["capture-pane", "-e", "-p", "-S", "-50", "-t", "%0"]

    def test_get_history_full_history(self, tmux):
        run, calls = self._fake_tmux(capture="line1\nline2\n")
        with self._observing(run):
            result = tmux.get_history("ses", "win", strip_escapes=True, full_history=True)

        assert result == "line1\nline2"
        capture = next(c for c in calls if "capture-pane" in c)
        # full_history uses "-S" "-" (no line count), strip_escapes omits "-e"
        assert capture[1:] == ["capture-pane", "-p", "-S", "-", "-t", "%0"]

    def test_get_history_strips_every_trailing_blank_pane_row(self, tmux):
        """`capture-pane -p` returns the whole visible pane region, so a TUI
        that renders in a viewport at the top leaves every row below it blank.
        libtmux popped all strictly-empty trailing lines; this must too.

        It matters beyond cosmetics: `CopilotCliProvider.get_status` scores
        `"\n".join(lines[-40:])`, so ~40 blank rows empty that window and the
        WAITING_USER_ANSWER branch becomes unreachable — a terminal blocked on
        a [y/n] prompt reports PROCESSING forever.
        """
        pane = "TOP LINE\nDo you trust the contents of this directory? [y/n]\n" + "\n" * 28
        run, _ = self._fake_tmux(capture=pane)
        with self._observing(run):
            result = tmux.get_history("ses", "win")

        assert result == "TOP LINE\nDo you trust the contents of this directory? [y/n]"

    def test_get_history_preserves_interior_blank_lines(self, tmux):
        """Only the trailing run goes. An interior blank is pane content."""
        run, _ = self._fake_tmux(capture="a\n\nb\n\n\n")
        with self._observing(run):
            assert tmux.get_history("ses", "win") == "a\n\nb"

    def test_get_history_splits_only_on_newline(self, tmux):
        """`splitlines()` also breaks on \v, \f, \x85 and U+2028/U+2029, which
        the libtmux path never did — agent output carrying one would gain line
        breaks that no tail window expects."""
        run, _ = self._fake_tmux(capture="a\u2028b\n")
        with self._observing(run):
            assert tmux.get_history("ses", "win") == "a\u2028b"

    def test_get_history_addresses_the_window_exactly(self, tmux):
        """``=`` on each name component forbids tmux's default prefix match,
        preserving what ``windows.get(window_name=...)`` meant: this window,
        not whichever one happens to share a prefix with it."""
        run, calls = self._fake_tmux(capture="tail\n")
        with self._observing(run):
            tmux.get_history("ses", "win")

        listing = next(c for c in calls if "list-panes" in c)
        assert listing[listing.index("-t") + 1] == "=ses:=win"
        # The format carries the window name back so the resolver can prove
        # tmux answered for the window it named (a numeric name resolves as an
        # index first, which "=" does not suppress).
        assert listing[listing.index("-F") + 1] == "#{pane_id}\t#{window_name}"


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


# ── window_exists ────────────────────────────────────────────────────


class TestWindowExists:
    @staticmethod
    def _missing_object(**kwargs):
        if "default" in kwargs:
            return kwargs["default"]
        raise ObjectDoesNotExist("No objects found")

    def test_a_missing_window_is_observed_as_absent(self, tmux):
        mock_session = MagicMock()
        mock_session.windows.get.side_effect = self._missing_object
        tmux.server.is_alive.return_value = True
        tmux.server.sessions.get.return_value = mock_session

        assert tmux.window_exists("ses", "gone") is False
        mock_session.windows.get.assert_called_once_with(window_name="gone", default=None)

    def test_a_missing_session_is_observed_as_absent(self, tmux):
        tmux.server.is_alive.return_value = True
        tmux.server.sessions.get.side_effect = self._missing_object

        assert tmux.window_exists("gone", "win") is False
        tmux.server.sessions.get.assert_called_once_with(session_name="gone", default=None)

    def test_an_unreadable_server_is_not_misreported_as_absence(self, tmux):
        tmux.server.is_alive.return_value = True
        tmux.server.sessions.get.side_effect = RuntimeError("tmux server unreadable")

        with pytest.raises(RuntimeError, match="server unreadable"):
            tmux.window_exists("ses", "win")

    def test_a_failed_server_probe_is_not_misreported_as_absence(self, tmux):
        tmux.server.is_alive.return_value = False
        # This is how libtmux's Server.sessions property represents every
        # list-sessions failure, including permission and subprocess errors.
        tmux.server.sessions.get.return_value = None

        with pytest.raises(RuntimeError, match="unavailable or unreadable"):
            tmux.window_exists("ses", "win")

        tmux.server.sessions.get.assert_not_called()


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
