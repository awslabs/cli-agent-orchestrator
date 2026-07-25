"""Unit tests for TmuxBackend — verify it satisfies the TerminalBackend ABC."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackend, TerminalBackendError
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend


class TestTmuxBackendSatisfiesABC:
    """Verify TmuxBackend is a valid TerminalBackend implementation."""

    def test_is_instance_of_terminal_backend(self):
        """TmuxBackend should be an instance of TerminalBackend ABC."""
        with patch("cli_agent_orchestrator.backends.tmux_backend.TmuxClient"):
            backend = TmuxBackend(client=MagicMock())
        assert isinstance(backend, TerminalBackend)

    def test_all_abstract_methods_implemented(self):
        """TmuxBackend should implement all abstract methods without error."""
        # If any method is missing, instantiation would raise TypeError
        with patch("cli_agent_orchestrator.backends.tmux_backend.TmuxClient"):
            backend = TmuxBackend(client=MagicMock())
        # Verify all methods exist
        assert hasattr(backend, "create_session")
        assert hasattr(backend, "session_exists")
        assert hasattr(backend, "list_sessions")
        assert hasattr(backend, "kill_session")
        assert hasattr(backend, "create_window")
        assert hasattr(backend, "kill_window")
        assert hasattr(backend, "send_keys")
        assert hasattr(backend, "send_special_key")
        assert hasattr(backend, "get_history")
        assert hasattr(backend, "get_pane_working_directory")
        assert hasattr(backend, "get_pane_current_command")
        assert hasattr(backend, "attach_session")
        assert hasattr(backend, "pipe_pane")
        assert hasattr(backend, "stop_pipe_pane")


class TestTmuxBackendDelegation:
    """Verify TmuxBackend delegates all calls to TmuxClient."""

    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def backend(self, mock_client):
        return TmuxBackend(client=mock_client)

    def test_create_session_delegates(self, backend, mock_client):
        mock_client.create_session.return_value = "window-0"
        result = backend.create_session("cao-test", "window-0", "tid123", "/tmp")
        mock_client.create_session.assert_called_once_with(
            "cao-test", "window-0", "tid123", "/tmp", extra_env=None
        )
        assert result == "window-0"

    def test_create_session_wraps_error(self, backend, mock_client):
        mock_client.create_session.side_effect = RuntimeError("tmux failed")
        with pytest.raises(TerminalBackendError, match="Failed to create session"):
            backend.create_session("cao-test", "window-0", "tid123")

    def test_session_exists_delegates(self, backend, mock_client):
        mock_client.session_exists.return_value = True
        assert backend.session_exists("cao-test") is True
        mock_client.session_exists.assert_called_once_with("cao-test")

    def test_list_sessions_delegates(self, backend, mock_client):
        mock_client.list_sessions.return_value = [{"id": "s1", "name": "s1", "status": "active"}]
        result = backend.list_sessions()
        assert len(result) == 1
        mock_client.list_sessions.assert_called_once()

    def test_prepare_web_attach_returns_window_target(self, backend):
        """A row with no recorded identity keeps the name-resolved form.

        Rows predating identity persistence are the documented edge of the
        identity-addressed attach, not a case it silently upgrades.
        """
        assert backend.prepare_web_attach("cao-test", "developer-abcd") == [
            "tmux",
            "-u",
            "attach-session",
            "-t",
            "cao-test:developer-abcd",
        ]

    def test_prepare_web_attach_targets_the_registered_pane_on_its_server(self, backend):
        """Verified against tmux 3.7b: a pane-id target selects its window.

        A bare session-id target does not — it lands on whichever window
        the session last had current — so the pane id is the only target
        that means "open this worker's terminal". The names are passed but
        must not appear anywhere in the argv.
        """
        argv = backend.prepare_web_attach(
            "cao-test",
            "developer-abcd",
            pane_id="%263",
            server_socket_path="/private/tmp/cao.sock",
        )
        assert argv == [
            "tmux",
            "-S",
            "/private/tmp/cao.sock",
            "-u",
            "attach-session",
            "-t",
            "%263",
        ]
        assert "cao-test:developer-abcd" not in argv
        assert "developer-abcd" not in argv

    def test_prepare_web_attach_refuses_a_pane_id_with_no_recorded_server(self, backend):
        """A pane id with no server is not a weaker identity — it is unsafe.

        Not hypothetical: a restarted tmux server hands out ``%0``/``%1``
        again, so the ids a row recorded before the restart resolve to
        different live panes afterwards. Attaching such an id against
        whichever server this process happens to reach is how a card opens
        somebody else's terminal. It must fail closed rather than infer
        the default server, and it must not quietly fall back to the name
        form either — that is the same misdelivery by another route.
        """
        with pytest.raises(TerminalBackendError, match="unpinned id"):
            backend.prepare_web_attach("cao-test", "developer-abcd", pane_id="%263")

    def test_kill_session_delegates(self, backend, mock_client):
        mock_client.kill_session.return_value = True
        assert backend.kill_session("cao-test") is True
        mock_client.kill_session.assert_called_once_with("cao-test")

    def test_create_window_delegates(self, backend, mock_client):
        mock_client.create_window.return_value = "dev-1234"
        result = backend.create_window("cao-test", "dev-1234", "tid456", "/home")
        mock_client.create_window.assert_called_once_with(
            "cao-test", "dev-1234", "tid456", "/home", None, extra_env=None
        )
        assert result == "dev-1234"

    def test_create_window_wraps_error(self, backend, mock_client):
        mock_client.create_window.side_effect = ValueError("Session not found")
        with pytest.raises(TerminalBackendError, match="Failed to create window"):
            backend.create_window("cao-test", "dev-1234", "tid456")

    def test_kill_window_delegates(self, backend, mock_client):
        mock_client.kill_window.return_value = True
        assert backend.kill_window("cao-test", "window-0") is True
        mock_client.kill_window.assert_called_once_with("cao-test", "window-0")

    def test_terminal_bound_window_identity_delegates(self, backend, mock_client):
        mock_client.terminal_bound_window_identity.return_value = {
            "pane_id": "%4",
            "window_id": "@3",
        }
        result = backend.terminal_bound_window_identity("abcd1234", "cao-test", "window-0")
        assert result == {"pane_id": "%4", "window_id": "@3"}
        mock_client.terminal_bound_window_identity.assert_called_once_with(
            "abcd1234", "cao-test", "window-0"
        )

    def test_send_keys_delegates(self, backend, mock_client):
        backend.send_keys("cao-test", "window-0", "hello", enter_count=2)
        mock_client.send_keys.assert_called_once_with(
            "cao-test",
            "window-0",
            "hello",
            enter_count=2,
            force_bracketed_paste=False,
            submit_delay=0.3,
            pane_id=None,
        )

    def test_send_keys_passes_a_verified_pane_id_through(self, backend, mock_client):
        """A caller that supplies an exact pane must reach the client with it.

        Dropping it here would be silent: the delegation would still
        succeed and the write would still land — by name, at whatever
        currently answers to it, which is what the caller declined.
        """
        backend.send_keys("cao-test", "window-0", "hello", pane_id="%263")
        assert mock_client.send_keys.call_args.kwargs["pane_id"] == "%263"

    def test_send_special_key_delegates(self, backend, mock_client):
        backend.send_special_key("cao-test", "window-0", "C-c")
        mock_client.send_special_key.assert_called_once_with(
            "cao-test", "window-0", "C-c", pane_id=None
        )

    def test_send_special_key_passes_a_verified_pane_through(self, backend, mock_client):
        """A control key is delivered to the proven pane, not to a name.

        ``Enter`` submits whatever a composer is holding and ``C-c``
        interrupts whatever is running, so a name-resolved target that has
        since been reused acts on a stranger's session immediately.
        """
        backend.send_special_key("cao-test", "window-0", "C-c", pane_id="%9")
        mock_client.send_special_key.assert_called_once_with(
            "cao-test", "window-0", "C-c", pane_id="%9"
        )

    def test_get_history_delegates(self, backend, mock_client):
        mock_client.get_history.return_value = "output text"
        result = backend.get_history("cao-test", "window-0", tail_lines=50)
        mock_client.get_history.assert_called_once_with(
            "cao-test",
            "window-0",
            tail_lines=50,
            strip_escapes=False,
            full_history=False,
        )
        assert result == "output text"

    def test_get_pane_working_directory_delegates(self, backend, mock_client):
        mock_client.get_pane_working_directory.return_value = "/home/user"
        result = backend.get_pane_working_directory("cao-test", "window-0")
        assert result == "/home/user"

    def test_get_pane_current_command_delegates(self, backend, mock_client):
        mock_client.get_pane_current_command.return_value = "python"
        result = backend.get_pane_current_command("cao-test", "window-0")
        assert result == "python"

    def test_pipe_pane_delegates(self, backend, mock_client):
        backend.pipe_pane("cao-test", "window-0", "/tmp/log.txt")
        mock_client.pipe_pane.assert_called_once_with("cao-test", "window-0", "/tmp/log.txt")

    def test_stop_pipe_pane_delegates(self, backend, mock_client):
        backend.stop_pipe_pane("cao-test", "window-0")
        mock_client.stop_pipe_pane.assert_called_once_with("cao-test", "window-0")


class TestTmuxBackendDefaultClient:
    """Verify TmuxBackend uses module singleton when no client is provided."""

    @patch("cli_agent_orchestrator.clients.tmux.tmux_client")
    def test_uses_module_singleton(self, mock_singleton):
        backend = TmuxBackend()
        assert backend._client is mock_singleton
