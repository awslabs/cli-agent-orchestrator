"""Tests for TmuxClient.send_keys paste-buffer implementation."""

from unittest.mock import call, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


@pytest.fixture
def client():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        return TmuxClient()


@pytest.fixture
def mock_subprocess():
    with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock:
        mock.run.return_value = None
        yield mock


@pytest.fixture
def mock_uuid():
    with patch("cli_agent_orchestrator.clients.tmux.uuid") as mock:
        mock.uuid4.return_value.hex = "abcd1234efgh"
        yield mock


class TestSendKeys:
    """Tests for the paste-buffer based send_keys implementation."""

    def test_basic_message(self, client, mock_subprocess, mock_uuid):
        """Sends load-buffer, paste-buffer -p, send-keys Enter, delete-buffer."""
        client.send_keys("sess", "win", "hello")

        assert mock_subprocess.run.call_count == 4
        calls = mock_subprocess.run.call_args_list

        # load-buffer with unique name and message as stdin
        assert calls[0] == call(
            ["tmux", "load-buffer", "-b", "cao_abcd1234", "-"],
            input=b"hello",
            check=True,
        )
        # paste-buffer with -p (bracketed paste)
        assert calls[1] == call(
            ["tmux", "paste-buffer", "-p", "-b", "cao_abcd1234", "-t", "sess:win"],
            check=True,
        )
        # send Enter
        assert calls[2] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )
        # delete-buffer (best-effort)
        assert calls[3] == call(
            ["tmux", "delete-buffer", "-b", "cao_abcd1234"],
            check=False,
        )

    def test_multiline_message(self, client, mock_subprocess, mock_uuid):
        """Multi-line content is sent as-is; -p flag handles newlines."""
        msg = "line 1\nline 2\nline 3"
        client.send_keys("sess", "win", msg)

        load_call = mock_subprocess.run.call_args_list[0]
        assert load_call == call(
            ["tmux", "load-buffer", "-b", "cao_abcd1234", "-"],
            input=msg.encode(),
            check=True,
        )

    def test_special_characters(self, client, mock_subprocess, mock_uuid):
        """Quotes, backticks, dollars are sent raw (no tmux key interpretation)."""
        msg = """He said "hello" and ran `cmd` with $VAR"""
        client.send_keys("sess", "win", msg)

        load_call = mock_subprocess.run.call_args_list[0]
        assert load_call[1]["input"] == msg.encode()

    def test_empty_message(self, client, mock_subprocess, mock_uuid):
        """Empty string still goes through the full pipeline."""
        client.send_keys("sess", "win", "")

        assert mock_subprocess.run.call_count == 4
        load_call = mock_subprocess.run.call_args_list[0]
        assert load_call[1]["input"] == b""

    def test_buffer_cleanup_on_error(self, client, mock_subprocess, mock_uuid):
        """Buffer is deleted even when paste-buffer fails."""
        mock_subprocess.run.side_effect = [
            None,  # load-buffer succeeds
            Exception("paste failed"),  # paste-buffer fails
            None,  # delete-buffer in finally
        ]

        with pytest.raises(Exception, match="paste failed"):
            client.send_keys("sess", "win", "msg")

        # delete-buffer still called in finally block
        last_call = mock_subprocess.run.call_args_list[-1]
        assert last_call == call(
            ["tmux", "delete-buffer", "-b", "cao_abcd1234"],
            check=False,
        )

    def test_unique_buffer_per_call(self, client, mock_subprocess):
        """Each call gets a unique buffer name to prevent race conditions."""
        with patch("cli_agent_orchestrator.clients.tmux.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value.hex = "aaaa1111bbbb"
            client.send_keys("sess", "win", "msg1")

            mock_uuid.uuid4.return_value.hex = "cccc2222dddd"
            client.send_keys("sess", "win", "msg2")

        calls = mock_subprocess.run.call_args_list
        # First call uses cao_aaaa1111
        assert calls[0][0][0][3] == "cao_aaaa1111"
        # Second call (index 4, after 4 calls from first send_keys) uses cao_cccc2222
        assert calls[4][0][0][3] == "cao_cccc2222"

    def test_large_message(self, client, mock_subprocess, mock_uuid):
        """Large messages go through in a single load-buffer call (no chunking)."""
        msg = "X" * 50000
        client.send_keys("sess", "win", msg)

        # Still exactly 4 subprocess calls — no chunking
        assert mock_subprocess.run.call_count == 4
        load_call = mock_subprocess.run.call_args_list[0]
        assert len(load_call[1]["input"]) == 50000
