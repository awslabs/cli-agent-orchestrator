"""Unit tests for HerdrBackend — pane_id resolution and command construction."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import (
    TerminalBackend,
    TerminalBackendError,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

# --- Fixtures ---


@pytest.fixture
def backend():
    # Patch os.path.exists so _ensure_session_running finds the socket immediately,
    # avoiding the 5-second poll timeout in unit tests.
    with patch("cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True):
        yield HerdrBackend(send_delay_ms=0)


def _make_pane_list_response(panes):
    """Build a herdr pane list JSON envelope."""
    return json.dumps({"id": "cli:pane:list", "result": {"panes": panes, "type": "pane_list"}})


def _make_workspace_list_response(workspaces):
    """Build a herdr workspace list JSON envelope."""
    return json.dumps(
        {"id": "cli:workspace:list", "result": {"workspaces": workspaces, "type": "workspace_list"}}
    )


def _make_tab_list_response(tabs):
    """Build a herdr tab list JSON envelope."""
    return json.dumps({"id": "cli:tab:list", "result": {"tabs": tabs, "type": "tab_list"}})


def _completed(stdout="", returncode=0):
    """Create a mock CompletedProcess."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.returncode = returncode
    mock.stderr = ""
    return mock


# --- ABC Compliance ---


class TestHerdrBackendABC:
    """Verify HerdrBackend satisfies the ABC."""

    def test_is_instance_of_terminal_backend(self, backend):
        assert isinstance(backend, TerminalBackend)

    def test_all_methods_implemented(self, backend):
        for method in [
            "create_session",
            "session_exists",
            "list_sessions",
            "kill_session",
            "create_window",
            "kill_window",
            "send_keys",
            "send_special_key",
            "get_history",
            "get_pane_working_directory",
            "get_pane_current_command",
            "attach_session",
            "pipe_pane",
            "stop_pipe_pane",
        ]:
            assert callable(getattr(backend, method))


# --- Command Construction ---


class TestHerdrBackendCommands:
    """Verify correct herdr CLI command construction for each method."""

    @patch("subprocess.run")
    def test_session_exists_true(self, mock_run, backend):
        """session_exists returns True when workspace label matches."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        mock_run.return_value = _completed(_make_workspace_list_response(ws))

        assert backend.session_exists("cao-test") is True

    @patch("subprocess.run")
    def test_session_exists_false(self, mock_run, backend):
        """session_exists returns False when no workspace matches."""
        ws = [{"label": "other", "workspace_id": "w1"}]
        mock_run.return_value = _completed(_make_workspace_list_response(ws))

        assert backend.session_exists("cao-test") is False

    @patch("subprocess.run")
    def test_list_sessions(self, mock_run, backend):
        """list_sessions returns workspace labels."""
        ws = [
            {"label": "cao-proj1", "workspace_id": "w1"},
            {"label": "cao-proj2", "workspace_id": "w2"},
        ]
        mock_run.return_value = _completed(_make_workspace_list_response(ws))

        result = backend.list_sessions()
        assert len(result) == 2
        assert result[0]["name"] == "cao-proj1"
        assert result[1]["name"] == "cao-proj2"

    @patch("subprocess.run")
    def test_prepare_web_attach_focuses_tab_and_returns_herdr_command(self, mock_run, backend):
        """Browser attachment focuses the requested Herdr tab before opening its TUI."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-1", "workspace_id": "w1", "label": "developer-abcd"}]
        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(),
        ]

        command = backend.prepare_web_attach("cao-test", "developer-abcd")

        assert command == ["herdr", "--session", "cao"]
        assert mock_run.call_args_list[-1].args[0][-3:] == ["tab", "focus", "tab-1"]

    def test_prepare_web_attach_propagates_tab_not_found(self, backend):
        """Browser attachment propagates a missing requested Herdr tab."""
        error = TerminalNotFoundError("cao-test:missing-window")

        with (
            patch.object(backend, "_resolve_workspace_id", return_value="w1"),
            patch.object(backend, "_resolve_tab_id", side_effect=error),
            pytest.raises(TerminalNotFoundError) as exc_info,
        ):
            backend.prepare_web_attach("cao-test", "missing-window")

        assert exc_info.value is error

    @patch("subprocess.run")
    def test_create_session_calls_workspace_create(self, mock_run, backend):
        """create_session should call herdr workspace create with --label and inject env."""
        # Include root_pane.pane_id so _parse_new_pane_id succeeds and _inject_env_vars
        # uses the known pane_id directly (no fallback pane list scan needed).
        ws_create_resp = _completed(
            json.dumps(
                {
                    "id": "cli:workspace:create",
                    "result": {
                        "workspace_id": "w_new",
                        "root_pane": {
                            "pane_id": "w_new-1",
                            "workspace_id": "w_new",
                            "tab_id": "tab-0",
                        },
                        "type": "workspace_created",
                    },
                }
            )
        )
        mock_run.side_effect = [
            ws_create_resp,  # workspace create
            _completed(),  # tab rename (root tab labeled with window_name)
            _completed(),  # pane send-text (env export)
            _completed(),  # pane send-keys Enter
        ]

        backend.create_session("cao-myproj", "window-0", "tid1", "/home/user/project")

        # First call should be workspace create
        cmd = mock_run.call_args_list[0][0][0]
        assert cmd[:3] == ["herdr", "--session", "cao"]
        assert "workspace" in cmd
        assert "create" in cmd
        assert "--label" in cmd
        assert "cao-myproj" in cmd
        assert "--cwd" in cmd
        assert "/home/user/project" in cmd
        # Env injection should have sent the export command (call index 2)
        env_cmd = mock_run.call_args_list[2][0][0]
        assert "send-text" in env_cmd
        assert "CAO_TERMINAL_ID=tid1" in env_cmd[-1]
        assert "CAO_SESSION_NAME=cao-myproj" in env_cmd[-1]

    @staticmethod
    def _workspace_create_resp():
        """workspace create response carrying a root pane_id for env injection."""
        return _completed(
            json.dumps(
                {
                    "id": "cli:workspace:create",
                    "result": {
                        "workspace_id": "w_new",
                        "root_pane": {
                            "pane_id": "w_new-1",
                            "workspace_id": "w_new",
                            "tab_id": "tab-0",
                        },
                        "type": "workspace_created",
                    },
                }
            )
        )

    @patch("subprocess.run")
    def test_create_session_forwards_extra_env(self, mock_run, backend):
        """extra_env from cao launch --env is exported into the pane (shell-quoted)."""
        mock_run.side_effect = [
            self._workspace_create_resp(),  # workspace create
            _completed(),  # tab rename
            _completed(),  # pane send-text (env export)
            _completed(),  # pane send-keys Enter
        ]

        backend.create_session(
            "cao-myproj",
            "window-0",
            "tid1",
            "/home/user/project",
            extra_env={"AWS_REGION": "us-west-2", "MNEMOSYNE_DIR": "/root/mn"},
        )

        env_cmd = mock_run.call_args_list[2][0][0][-1]
        assert "export AWS_REGION=us-west-2" in env_cmd
        assert "export MNEMOSYNE_DIR=/root/mn" in env_cmd
        # CAO identity vars still present
        assert "CAO_TERMINAL_ID=tid1" in env_cmd

    @patch("subprocess.run")
    def test_create_session_drops_blocked_and_oversized_env(self, mock_run, backend):
        """Blocked-prefix and oversized extra_env values are filtered out (tmux parity)."""
        mock_run.side_effect = [
            self._workspace_create_resp(),
            _completed(),
            _completed(),
            _completed(),
        ]

        backend.create_session(
            "cao-myproj",
            "window-0",
            "tid1",
            "/home/user/project",
            extra_env={
                "CLAUDE_SECRET": "x",  # blocked prefix
                "BIG": "x" * 2048,  # at the 2048-byte cap -> dropped
                "OK": "kept",
            },
        )

        env_cmd = mock_run.call_args_list[2][0][0][-1]
        assert "CLAUDE_SECRET" not in env_cmd
        assert "BIG=" not in env_cmd
        assert "export OK=kept" in env_cmd

    @patch("subprocess.run")
    def test_create_session_quotes_env_values(self, mock_run, backend):
        """Operator-supplied values are shell-quoted to stay injection-safe."""
        mock_run.side_effect = [
            self._workspace_create_resp(),
            _completed(),
            _completed(),
            _completed(),
        ]

        backend.create_session(
            "cao-myproj",
            "window-0",
            "tid1",
            "/home/user/project",
            extra_env={"DANGER": "a; rm -rf /"},
        )

        env_cmd = mock_run.call_args_list[2][0][0][-1]
        # shlex.quote wraps the value so the embedded "; rm" cannot break out.
        assert "export DANGER='a; rm -rf /'" in env_cmd

    @patch("subprocess.run")
    def test_create_window_forwards_extra_env(self, mock_run, backend):
        """create_window threads extra_env into the injected exports too."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tab_create_resp = _completed(
            json.dumps(
                {
                    "id": "cli:tab:create",
                    "result": {
                        "root_pane": {
                            "pane_id": "w1-2",
                            "workspace_id": "w1",
                            "tab_id": "tab-1",
                        },
                        "type": "tab_created",
                    },
                }
            )
        )
        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # _resolve_workspace_id
            tab_create_resp,  # tab create
            _completed(),  # pane send-text (env export)
            _completed(),  # pane send-keys Enter
        ]

        backend.create_window(
            "cao-test",
            "window-1",
            "tid2",
            extra_env={"AWS_REGION": "eu-central-1"},
        )

        send_text_call = next(c[0][0] for c in mock_run.call_args_list if "send-text" in c[0][0])
        assert "export AWS_REGION=eu-central-1" in send_text_call[-1]

    @patch("subprocess.run")
    def test_kill_session_calls_workspace_close(self, mock_run, backend):
        """kill_session should resolve workspace_id then call herdr workspace close <id>."""
        ws = [{"label": "cao-test", "workspace_id": "w_abc123"}]
        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # _resolve_workspace_id
            _completed(),  # workspace close
        ]

        result = backend.kill_session("cao-test")

        assert result is True
        close_call = mock_run.call_args_list[1][0][0]
        assert "workspace" in close_call
        assert "close" in close_call
        assert "w_abc123" in close_call
        assert "--label" not in close_call

    @patch("subprocess.run")
    def test_send_keys_calls_send_text_then_enter(self, mock_run, backend):
        """send_keys should call pane send-text then pane send-keys Enter."""
        # Resolution path: workspace list + tab list + pane list (3 calls)
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-0", "workspace_id": "w1", "label": "window-0"}]
        panes = [{"tab_id": "tab-0", "pane_id": "w1-1", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # workspace list
            _completed(_make_tab_list_response(tabs)),  # tab list
            _completed(_make_pane_list_response(panes)),  # pane list
            _completed(),  # send-text
            _completed(),  # send-keys Enter
        ]

        backend.send_keys("cao-test", "window-0", "hello world", enter_count=1)

        calls = [c[0][0] for c in mock_run.call_args_list]
        # After the 3 resolution calls: send-text then send-keys Enter
        assert "send-text" in calls[-2]
        assert "hello world" in calls[-2]
        assert "send-keys" in calls[-1]
        assert "Enter" in calls[-1]

    @patch("subprocess.run")
    def test_send_special_key_enter(self, mock_run, backend):
        """send_special_key with empty string sends Enter."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-0", "workspace_id": "w1", "label": "window-0"}]
        panes = [{"tab_id": "tab-0", "pane_id": "w1-1", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            _completed(),  # send-keys Enter
        ]

        backend.send_special_key("cao-test", "window-0", "")

        cmd = mock_run.call_args_list[-1][0][0]
        assert "send-keys" in cmd
        assert "Enter" in cmd

    @patch("subprocess.run")
    def test_get_history_calls_pane_read(self, mock_run, backend):
        """get_history should call herdr pane read with correct flags."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-0", "workspace_id": "w1", "label": "window-0"}]
        panes = [{"tab_id": "tab-0", "pane_id": "w1-1", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            _completed(stdout="pane output here"),  # pane read
        ]

        result = backend.get_history("cao-test", "window-0", tail_lines=50)

        assert result == "pane output here"
        cmd = mock_run.call_args_list[-1][0][0]
        assert "pane" in cmd
        assert "read" in cmd
        assert "--lines" in cmd
        assert "50" in cmd

    @patch("subprocess.run")
    def test_get_history_strip_escapes_requests_text_format(self, mock_run, backend):
        """strip_escapes=True maps to herdr's --format text (ANSI stripped)."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-0", "workspace_id": "w1", "label": "window-0"}]
        panes = [{"tab_id": "tab-0", "pane_id": "w1-1", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            _completed(stdout="plain text"),  # pane read
        ]

        backend.get_history("cao-test", "window-0", tail_lines=50, strip_escapes=True)

        cmd = mock_run.call_args_list[-1][0][0]
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "text"

    @patch("subprocess.run")
    def test_get_history_default_omits_format(self, mock_run, backend):
        """strip_escapes=False leaves format unset (herdr default preserved)."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-0", "workspace_id": "w1", "label": "window-0"}]
        panes = [{"tab_id": "tab-0", "pane_id": "w1-1", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            _completed(stdout="output"),  # pane read
        ]

        backend.get_history("cao-test", "window-0", tail_lines=50)

        cmd = mock_run.call_args_list[-1][0][0]
        assert "--format" not in cmd

    @patch("subprocess.run")
    def test_get_pane_working_directory(self, mock_run, backend):
        """get_pane_working_directory should parse cwd from pane get."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-0", "workspace_id": "w1", "label": "window-0"}]
        panes = [{"tab_id": "tab-0", "pane_id": "w1-1", "workspace_id": "w1"}]
        pane_info = json.dumps(
            {
                "id": "cli:pane:get",
                "result": {"pane": {"cwd": "/home/user/project"}, "type": "pane_info"},
            }
        )

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            _completed(stdout=pane_info),  # pane get
        ]

        result = backend.get_pane_working_directory("cao-test", "window-0")
        assert result == "/home/user/project"

    @patch("subprocess.run")
    def test_pipe_pane_is_noop(self, mock_run, backend):
        """pipe_pane should be a no-op (no subprocess calls)."""
        backend.pipe_pane("cao-test", "window-0", "/tmp/log.txt")
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_stop_pipe_pane_is_noop(self, mock_run, backend):
        """stop_pipe_pane should be a no-op (no subprocess calls)."""
        backend.stop_pipe_pane("cao-test", "window-0")
        mock_run.assert_not_called()


# --- Error Handling ---


class TestHerdrBackendErrors:
    """Verify error wrapping behavior."""

    @patch("subprocess.run")
    def test_nonzero_exit_raises_backend_error(self, mock_run, backend):
        """Non-zero herdr exit should raise TerminalBackendError."""
        mock_run.return_value = _completed(returncode=1)
        mock_run.return_value.stderr = "workspace not found"

        with pytest.raises(TerminalBackendError, match="herdr command failed"):
            backend._run_herdr(["workspace", "close", "--label", "missing"])

    @patch("subprocess.run")
    def test_herdr_not_found_raises_backend_error(self, mock_run, backend):
        """FileNotFoundError from herdr should raise TerminalBackendError."""
        mock_run.side_effect = FileNotFoundError("herdr")

        with pytest.raises(TerminalBackendError, match="herdr CLI not found"):
            backend._run_herdr(["workspace", "list"])


# --- Multi-pane resolution (S-008) ---


class TestMultiPaneResolution:
    """Test _resolve_pane_id_from_window with multiple panes in one workspace."""

    @pytest.fixture
    def backend(self):
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True
        ):
            yield HerdrBackend(send_delay_ms=0)

    @patch("subprocess.run")
    def test_resolves_correct_pane_via_window_mapping(self, mock_run, backend):
        """With window→terminal mapping, resolves correct pane via fresh tab+pane lookup.

        First call: workspace list (cache miss) + tab list + pane list = 3 subprocess calls.
        Second call: workspace cache hit + tab list + pane list = 2 subprocess calls.
        """
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [
            {"tab_id": "tab-1", "workspace_id": "w1", "label": "developer-abc1"},
            {"tab_id": "tab-2", "workspace_id": "w1", "label": "developer-abc2"},
        ]
        panes = [
            {"tab_id": "tab-1", "pane_id": "w1-1", "workspace_id": "w1"},
            {"tab_id": "tab-2", "pane_id": "w1-2", "workspace_id": "w1"},
        ]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # workspace list (cache miss)
            _completed(_make_tab_list_response(tabs)),  # tab list
            _completed(_make_pane_list_response(panes)),  # pane list
            # second call: workspace cache hit, so only tab + pane needed
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
        ]

        assert backend._resolve_pane_id_from_window("cao-test", "developer-abc1") == "w1-1"
        assert backend._resolve_pane_id_from_window("cao-test", "developer-abc2") == "w1-2"

    @patch("subprocess.run")
    def test_wrong_window_raises_not_found(self, mock_run, backend):
        """A tab with no matching pane raises TerminalNotFoundError keyed on session:window."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        # Tab exists but no panes match its tab_id
        tabs = [{"tab_id": "tab-x", "workspace_id": "w1", "label": "unknown-win"}]
        panes = [{"tab_id": "tab-other", "pane_id": "w1-1", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
        ]

        with pytest.raises(TerminalNotFoundError, match="cao-test:unknown-win"):
            backend._resolve_pane_id_from_window("cao-test", "unknown-win")

    @patch("subprocess.run")
    def test_no_matching_tab_raises_not_found(self, mock_run, backend):
        """No tab for the window: _resolve_tab_id raises TerminalBackendError, which is
        re-raised as TerminalNotFoundError keyed on session:window. No silent fallback to
        an arbitrary pane in the workspace."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        # Tabs exist in the workspace but none match the requested window label
        tabs = [{"tab_id": "tab-other", "workspace_id": "w1", "label": "some-other-window"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
        ]

        with pytest.raises(TerminalNotFoundError, match="cao-test:unmapped-window"):
            backend._resolve_pane_id_from_window("cao-test", "unmapped-window")

    @patch("subprocess.run")
    def test_three_terminals_each_resolves_correctly(self, mock_run, backend):
        """Three terminals each resolve to their distinct pane via fresh tab+pane lookup.

        First call: workspace list (cache miss) + tab + pane = 3 calls.
        Second/third calls: workspace cache hit + tab + pane = 2 calls each.
        Total: 7 subprocess calls.
        """
        ws = [{"label": "cao-proj", "workspace_id": "w5"}]
        tabs = [
            {"tab_id": "tab-c", "workspace_id": "w5", "label": "conductor-a1"},
            {"tab_id": "tab-w", "workspace_id": "w5", "label": "worker-b2"},
            {"tab_id": "tab-r", "workspace_id": "w5", "label": "reviewer-c3"},
        ]
        panes = [
            {"tab_id": "tab-c", "pane_id": "w5-1", "workspace_id": "w5"},
            {"tab_id": "tab-w", "pane_id": "w5-2", "workspace_id": "w5"},
            {"tab_id": "tab-r", "pane_id": "w5-3", "workspace_id": "w5"},
        ]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # workspace list (cache miss)
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            # second + third calls: workspace cache hit
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
        ]

        assert backend._resolve_pane_id_from_window("cao-proj", "conductor-a1") == "w5-1"
        assert backend._resolve_pane_id_from_window("cao-proj", "worker-b2") == "w5-2"
        assert backend._resolve_pane_id_from_window("cao-proj", "reviewer-c3") == "w5-3"

    @patch("subprocess.run")
    def test_resolve_pane_id_from_window_always_does_fresh_lookup(self, mock_run, backend):
        """Calling _resolve_pane_id_from_window twice returns fresh results each call,
        even when pane_id shifts between calls (simulates post-deletion renumbering).

        First call: workspace list (cache miss) + tab list + pane list = 3 subprocess calls.
        Second call: workspace cache hit + tab list + pane list = 2 subprocess calls.
        Total: 5 subprocess calls.
        """
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [{"tab_id": "tab-1", "workspace_id": "w1", "label": "window-0"}]
        # First call: pane_id is w1-3; second call: pane_id shifted to w1-2
        first_panes = [{"tab_id": "tab-1", "pane_id": "w1-3", "workspace_id": "w1"}]
        second_panes = [{"tab_id": "tab-1", "pane_id": "w1-2", "workspace_id": "w1"}]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # workspace list (cache miss)
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(first_panes)),
            # second call: workspace cache hit, so only tab + pane
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(second_panes)),
        ]

        first_result = backend._resolve_pane_id_from_window("cao-test", "window-0")
        second_result = backend._resolve_pane_id_from_window("cao-test", "window-0")

        assert first_result == "w1-3"
        assert second_result == "w1-2"
        # 3 calls (first) + 2 calls (second, workspace cached) = 5 total
        assert mock_run.call_count == 5

    @patch("subprocess.run")
    def test_resolve_pane_id_from_window_uses_tab_id(self, mock_run, backend):
        """When multiple panes exist in the workspace, the correct one is returned by
        matching tab_id (not just the first pane in workspace order)."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tabs = [
            {"tab_id": "tab-other", "workspace_id": "w1", "label": "other-window"},
            {"tab_id": "tab-target", "workspace_id": "w1", "label": "target-window"},
        ]
        # target pane appears second — a first-pane-wins strategy would return wrong result
        panes = [
            {"tab_id": "tab-other", "pane_id": "w1-1", "workspace_id": "w1"},
            {"tab_id": "tab-target", "pane_id": "w1-2", "workspace_id": "w1"},
        ]

        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),
            _completed(_make_tab_list_response(tabs)),
            _completed(_make_pane_list_response(panes)),
        ]

        result = backend._resolve_pane_id_from_window("cao-test", "target-window")
        assert result == "w1-2"


# --- Session socket path ---


class TestSessionSocketPath:
    """Test _session_socket_path for named and default sessions."""

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"})
    def test_named_session_uses_subdir(self):
        """Named session should produce <config_home>/herdr/<name>/herdr.sock."""
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True
        ):
            b = HerdrBackend(herdr_session="cao")
        assert b._session_socket_path() == "/custom/config/herdr/sessions/cao/herdr.sock"

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"})
    def test_default_session_uses_flat_path(self):
        """'default' session should produce <config_home>/herdr/herdr.sock (no subdir)."""
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True
        ):
            b = HerdrBackend(herdr_session="default")
        assert b._session_socket_path() == "/custom/config/herdr/herdr.sock"

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"})
    def test_arbitrary_session_name(self):
        """An arbitrary session name should appear as a subdirectory."""
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True
        ):
            b = HerdrBackend(herdr_session="my-workspace")
        assert b._session_socket_path() == "/custom/config/herdr/sessions/my-workspace/herdr.sock"


# --- Ensure session running ---


class TestEnsureSessionRunning:
    """Test _ensure_session_running startup logic."""

    def test_does_nothing_when_socket_exists(self):
        """If socket already exists, no Popen should be called."""
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True
        ):
            with patch("subprocess.Popen") as mock_popen:
                HerdrBackend(herdr_session="cao")
        mock_popen.assert_not_called()

    def test_starts_server_when_socket_absent(self):
        """If socket is absent, Popen should be called with herdr server args."""
        # Socket absent initially, then appears after first poll.
        exists_sequence = [False, True]

        def exists_side_effect(path):
            return exists_sequence.pop(0) if exists_sequence else True

        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            side_effect=exists_side_effect,
        ):
            with patch("subprocess.Popen") as mock_popen:
                with patch("time.sleep"):
                    HerdrBackend(herdr_session="cao")

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd == ["herdr", "--session", "cao", "server"]

    def test_logs_warning_when_socket_never_appears(self):
        """If socket never appears within 15s, a warning is logged and no error raised."""
        # Simulate clock: first call returns 0.0 (sets deadline=15.0),
        # all subsequent calls return 16.0 (past deadline, exits loop).
        # Using a counter so exhaustion from logging internals is not an issue.
        call_count = {"n": 0}

        def fake_time():
            call_count["n"] += 1
            return 0.0 if call_count["n"] == 1 else 16.0

        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=False,
        ):
            with patch("subprocess.Popen"):
                with patch("cli_agent_orchestrator.backends.herdr_backend.time.sleep"):
                    with patch(
                        "cli_agent_orchestrator.backends.herdr_backend.time.time",
                        side_effect=fake_time,
                    ):
                        # Should not raise
                        HerdrBackend(herdr_session="cao")


# --- create_window window_shell ---


class TestCreateWindowWindowShell:
    """Verify create_window handles window_shell correctly."""

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_create_window_with_window_shell(self, mock_run, mock_sleep, backend):
        """When window_shell is provided, pane run is called after a 0.5s sleep."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tab_create_resp = _completed(
            json.dumps(
                {
                    "id": "cli:tab:create",
                    "result": {
                        "root_pane": {"pane_id": "w1-5", "workspace_id": "w1"},
                        "type": "tab_created",
                    },
                }
            )
        )
        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # _resolve_workspace_id
            tab_create_resp,  # tab create
            _completed(),  # pane send-text (env export)
            _completed(),  # pane send-keys Enter
            _completed(),  # pane run
        ]

        backend.create_window(
            "cao-test",
            "restored-win",
            "tid99",
            "/home/user",
            window_shell="cat '/path/file'; exec /bin/bash -l",
        )

        mock_sleep.assert_called_once_with(0.5)
        pane_run_call = mock_run.call_args_list[-1][0][0]
        assert "pane" in pane_run_call
        assert "run" in pane_run_call
        assert "w1-5" in pane_run_call
        assert "cat '/path/file'; exec /bin/bash -l" in pane_run_call

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_create_window_window_shell_failure_is_nonfatal(self, mock_run, mock_sleep, backend):
        """If pane run raises, create_window still returns window_name without raising."""
        ws = [{"label": "cao-test", "workspace_id": "w1"}]
        tab_create_resp = _completed(
            json.dumps(
                {
                    "id": "cli:tab:create",
                    "result": {
                        "root_pane": {"pane_id": "w1-6", "workspace_id": "w1"},
                        "type": "tab_created",
                    },
                }
            )
        )
        pane_run_fail = _completed(returncode=1)
        pane_run_fail.stderr = "pane not found"
        mock_run.side_effect = [
            _completed(_make_workspace_list_response(ws)),  # _resolve_workspace_id
            tab_create_resp,  # tab create
            _completed(),  # pane send-text (env export)
            _completed(),  # pane send-keys Enter
            pane_run_fail,  # pane run (fails)
        ]

        result = backend.create_window(
            "cao-test",
            "restored-win",
            "tid99",
            "/home/user",
            window_shell="exec /bin/bash -l",
        )

        assert result == "restored-win"


# --- get_native_status() mapping ---


class TestGetNativeStatus:
    """Verify get_native_status() returns correct TerminalStatus for all herdr states."""

    from cli_agent_orchestrator.models.terminal import TerminalStatus as _TS

    def _make_pane_get_response(self, agent_status: str) -> str:
        return json.dumps(
            {
                "id": "cli:pane:get",
                "result": {
                    "pane": {"pane_id": "w1-1", "agent_status": agent_status},
                    "type": "pane_info",
                },
            }
        )

    def _setup_fresh_resolution(self, backend):
        """Pre-populate the workspace cache for tests.

        _resolve_pane_id_from_window performs fresh tab+pane lookups. Pre-populating
        the workspace cache (which has a TTL and is stable) means tests only need to
        mock tab list, pane list, and pane get — not workspace list as well.
        """
        backend._workspace_cache["s"] = ("w1", time.time())

    def _make_resolution_side_effects(self, pane_get_response):
        """Build the mock side_effect sequence for a fresh resolution + pane get call."""
        tabs = [{"tab_id": "tab-1", "workspace_id": "w1", "label": "w"}]
        panes = [{"tab_id": "tab-1", "pane_id": "w1-1", "workspace_id": "w1"}]
        return [
            _completed(_make_tab_list_response(tabs)),  # tab list
            _completed(_make_pane_list_response(panes)),  # pane list
            pane_get_response,  # pane get
        ]

    @patch("subprocess.run")
    def test_working_returns_processing(self, mock_run, backend):
        self._setup_fresh_resolution(backend)
        mock_run.side_effect = self._make_resolution_side_effects(
            _completed(self._make_pane_get_response("working"))
        )

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        result = backend.get_native_status("s", "w")
        assert result == TerminalStatus.PROCESSING

    @patch("subprocess.run")
    def test_blocked_returns_waiting_user_answer(self, mock_run, backend):
        self._setup_fresh_resolution(backend)
        mock_run.side_effect = self._make_resolution_side_effects(
            _completed(self._make_pane_get_response("blocked"))
        )

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        result = backend.get_native_status("s", "w")
        assert result == TerminalStatus.WAITING_USER_ANSWER

    @patch("subprocess.run")
    def test_done_returns_completed(self, mock_run, backend):
        self._setup_fresh_resolution(backend)
        mock_run.side_effect = self._make_resolution_side_effects(
            _completed(self._make_pane_get_response("done"))
        )

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        result = backend.get_native_status("s", "w")
        assert result == TerminalStatus.COMPLETED

    @patch("subprocess.run")
    def test_idle_returns_idle(self, mock_run, backend):
        self._setup_fresh_resolution(backend)
        mock_run.side_effect = self._make_resolution_side_effects(
            _completed(self._make_pane_get_response("idle"))
        )

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        result = backend.get_native_status("s", "w")
        assert result == TerminalStatus.IDLE

    @patch("subprocess.run")
    def test_unknown_returns_none(self, mock_run, backend):
        """herdr 'unknown' -> None (not ERROR): a wrapped exec launch hides the
        agent CLI, so herdr never registers it and reports 'unknown' for a
        healthy pane. None lets the caller resolve status another way."""
        self._setup_fresh_resolution(backend)
        mock_run.side_effect = self._make_resolution_side_effects(
            _completed(self._make_pane_get_response("unknown"))
        )

        result = backend.get_native_status("s", "w")
        assert result is None

    @patch("subprocess.run")
    def test_unrecognized_status_returns_none(self, mock_run, backend):
        """Any agent_status herdr may add later that CAO does not map -> None,
        same as 'unknown' (never leaks through as a bogus status)."""
        self._setup_fresh_resolution(backend)
        mock_run.side_effect = self._make_resolution_side_effects(
            _completed(self._make_pane_get_response("some-future-state"))
        )

        result = backend.get_native_status("s", "w")
        assert result is None

    @patch("subprocess.run")
    def test_command_failure_returns_none(self, mock_run, backend):
        self._setup_fresh_resolution(backend)
        pane_get_fail = _completed(returncode=1)
        mock_run.side_effect = self._make_resolution_side_effects(pane_get_fail)

        result = backend.get_native_status("s", "w")
        assert result is None


# --- Tests for _sanitize_herdr_args (security boundary) ---


class TestSanitizeHerdrArgs:
    """Tests for the argument validation/sanitization gate."""

    def test_happy_path_workspace_create(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        result = _sanitize_herdr_args(["workspace", "create", "--label", "my-session"])
        assert result == ["workspace", "create", "--label", "my-session"]

    def test_happy_path_pane_list(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        result = _sanitize_herdr_args(["pane", "list"])
        assert result == ["pane", "list"]

    def test_happy_path_with_path_containing_parens(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        result = _sanitize_herdr_args(
            ["workspace", "create", "--cwd", "/Users/foo/My Project (1)/src"]
        )
        assert result == ["workspace", "create", "--cwd", "/Users/foo/My Project (1)/src"]

    def test_rejects_empty_args(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="must not be empty"):
            _sanitize_herdr_args([])

    def test_rejects_unknown_subcommand(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="not in allowlist"):
            _sanitize_herdr_args(["exec", "rm", "-rf", "/"])

    def test_rejects_control_characters(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="unsafe characters"):
            _sanitize_herdr_args(["workspace", "create", "--label", "evil\x00name"])

    def test_rejects_newline_in_arg(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="unsafe characters"):
            _sanitize_herdr_args(["pane", "list\n"])

    def test_rejects_unknown_flag(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="not in allowlist"):
            _sanitize_herdr_args(["workspace", "create", "--session", "attacker"])

    def test_rejects_session_flag_injection(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="not in allowlist"):
            _sanitize_herdr_args(["pane", "list", "--session", "evil"])

    def test_send_text_payload_exempt_from_validation(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        payload = "export FOO=bar; rm -rf /; echo $(whoami)"
        result = _sanitize_herdr_args(["pane", "send-text", "w1-1", payload])
        assert result[3] == payload

    def test_pane_run_payload_exempt_from_validation(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        payload = "cat '/path/file'; exec /bin/bash -l"
        result = _sanitize_herdr_args(["pane", "run", "w1-1", payload])
        assert result[3] == payload

    def test_send_text_pane_id_still_validated(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        with pytest.raises(ValueError, match="unsafe characters"):
            _sanitize_herdr_args(["pane", "send-text", "evil\x00pane", "hello"])

    def test_returns_new_list(self):
        from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args

        original = ["pane", "list"]
        result = _sanitize_herdr_args(original)
        assert result == original
        assert result is not original
