"""Smoke coverage for the new TmuxMultiplexer home."""

from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer


@pytest.fixture
def tmux():
    """Create a TmuxMultiplexer with a mocked libtmux.Server."""
    with patch("cli_agent_orchestrator.multiplexers.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server

        client = TmuxMultiplexer()
        client.server = mock_server
        yield client


@pytest.fixture
def mock_subprocess():
    with patch("cli_agent_orchestrator.multiplexers.tmux.subprocess") as mock:
        mock.run.return_value = None
        yield mock


@pytest.fixture
def mock_uuid():
    with patch("cli_agent_orchestrator.multiplexers.tmux.uuid") as mock:
        mock.uuid4.return_value.hex = "abcd1234efgh"
        yield mock


class TestTmuxMultiplexerClient:
    def test_create_session_success(self, tmux):
        mock_window = MagicMock()
        mock_window.name = "my-window"
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        tmux.server.new_session.return_value = mock_session

        with patch.object(tmux, "_resolve_and_validate_working_directory", return_value="/tmp/work"):
            result = tmux.create_session("ses", "my-window", "tid1", "/tmp/work")

        assert result == "my-window"
        tmux.server.new_session.assert_called_once()

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

    def test_pipe_pane_success(self, tmux):
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        tmux.server.sessions.get.return_value = mock_session

        tmux.pipe_pane("ses", "win", "/tmp/log.txt")

        mock_pane.cmd.assert_called_once_with("pipe-pane", "-o", "cat >> /tmp/log.txt")


class TestTmuxMultiplexerSendKeys:
    def test_basic_message(self, tmux, mock_subprocess, mock_uuid):
        tmux.send_keys("sess", "win", "hello")

        assert mock_subprocess.run.call_count == 4
        calls = mock_subprocess.run.call_args_list

        assert calls[0] == call(
            ["tmux", "load-buffer", "-b", "cao_abcd1234", "-"],
            input=b"hello",
            check=True,
        )
        assert calls[1] == call(
            ["tmux", "paste-buffer", "-p", "-b", "cao_abcd1234", "-t", "sess:win"],
            check=True,
        )
        assert calls[2] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )
        assert calls[3] == call(
            ["tmux", "delete-buffer", "-b", "cao_abcd1234"],
            check=False,
        )

    def test_buffer_cleanup_on_error(self, tmux, mock_subprocess, mock_uuid):
        mock_subprocess.run.side_effect = [
            None,
            Exception("paste failed"),
            None,
        ]

        with pytest.raises(Exception, match="paste failed"):
            tmux.send_keys("sess", "win", "msg")

        last_call = mock_subprocess.run.call_args_list[-1]
        assert last_call == call(
            ["tmux", "delete-buffer", "-b", "cao_abcd1234"],
            check=False,
        )

    def test_double_enter(self, tmux, mock_subprocess, mock_uuid):
        tmux.send_keys("sess", "win", "hello", enter_count=2)

        assert mock_subprocess.run.call_count == 5
        calls = mock_subprocess.run.call_args_list
        assert calls[2] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )
        assert calls[3] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )
