"""Tests for TmuxClient.send_keys paste-buffer implementation."""

from unittest.mock import MagicMock, call, patch

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


@pytest.fixture(autouse=True)
def reset_version_cache():
    """Keep the class-level tmux-version cache from leaking across tests."""
    TmuxClient._paste_buffer_sanitizes = None
    yield
    TmuxClient._paste_buffer_sanitizes = None


@pytest.fixture
def sanitizing_tmux():
    """Host tmux >= 3.7 (vis(3)-sanitizes pasted buffers)."""
    with patch.object(TmuxClient, "_paste_buffer_sanitizes", True):
        yield


@pytest.fixture
def legacy_tmux():
    """Host tmux < 3.7 (buffer bytes pass through unchanged)."""
    with patch.object(TmuxClient, "_paste_buffer_sanitizes", False):
        yield


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

    def test_double_enter(self, client, mock_subprocess, mock_uuid):
        """When enter_count=2, two Enter keys are sent after pasting."""
        client.send_keys("sess", "win", "hello", enter_count=2)

        assert mock_subprocess.run.call_count == 5  # load + paste + 2 Enter + delete
        calls = mock_subprocess.run.call_args_list
        # Both Enters
        assert calls[2] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )
        assert calls[3] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )

    def test_large_message(self, client, mock_subprocess, mock_uuid):
        """Large messages go through in a single load-buffer call (no chunking)."""
        msg = "X" * 50000
        client.send_keys("sess", "win", msg)

        # Still exactly 4 subprocess calls — no chunking
        assert mock_subprocess.run.call_count == 4
        load_call = mock_subprocess.run.call_args_list[0]
        assert len(load_call[1]["input"]) == 50000


class TestSendKeysNoHandCraftedMarkersOnModernTmux:
    """Regression tests for issue #413 (tmux >= 3.7).

    tmux >= 3.7 sanitizes pasted buffer content through vis(3), turning raw
    ESC (0x1b) bytes into the literal characters "^[". On those versions
    send_keys must never hand-craft \\x1b[200~/\\x1b[201~ markers in the
    buffer; it must let tmux emit them conditionally via paste-buffer -p.
    -r (raw, used by the legacy force_bracketed_paste path) and -S (would
    bypass the vis(3) hardening) are both forbidden.
    """

    def test_buffer_content_has_no_escape_bytes(
        self, client, mock_subprocess, mock_uuid, sanitizing_tmux
    ):
        """Loaded buffer contains only raw message bytes — no ESC, no markers."""
        client.send_keys("sess", "win", "hello world", force_bracketed_paste=True)

        load_call = mock_subprocess.run.call_args_list[0]
        buf_content = load_call[1]["input"]
        assert b"\x1b" not in buf_content
        assert b"[200~" not in buf_content
        assert b"[201~" not in buf_content
        assert buf_content == b"hello world"

    def test_paste_uses_p_flag_not_r_or_S(
        self, client, mock_subprocess, mock_uuid, sanitizing_tmux
    ):
        """paste-buffer is invoked with -p and never -r or -S."""
        client.send_keys("sess", "win", "hello", force_bracketed_paste=True)

        paste_call = mock_subprocess.run.call_args_list[1]
        paste_argv = paste_call[0][0]
        assert paste_argv[:2] == ["tmux", "paste-buffer"]
        assert "-p" in paste_argv
        assert "-r" not in paste_argv
        assert "-S" not in paste_argv

    def test_force_bracketed_paste_multiline_content_unmodified(
        self, client, mock_subprocess, mock_uuid, sanitizing_tmux
    ):
        """Multi-line message delivery loads the content byte-for-byte."""
        msg = "line 1\nline 2\n\nline 4 with \x03 control char"
        client.send_keys("sess", "win", msg, force_bracketed_paste=True)

        load_call = mock_subprocess.run.call_args_list[0]
        assert load_call == call(
            ["tmux", "load-buffer", "-b", "cao_abcd1234", "-"],
            input=msg.encode(),
            check=True,
        )

    def test_force_flag_delivery_identical_to_default(
        self, client, mock_subprocess, mock_uuid, sanitizing_tmux
    ):
        """On >= 3.7 force_bracketed_paste does not alter the tmux command sequence."""
        client.send_keys("sess", "win", "same message", force_bracketed_paste=True)
        forced_calls = list(mock_subprocess.run.call_args_list)
        mock_subprocess.run.reset_mock()

        client.send_keys("sess", "win", "same message", force_bracketed_paste=False)
        default_calls = list(mock_subprocess.run.call_args_list)

        assert forced_calls == default_calls


class TestSendKeysLegacyWrapOnOldTmux:
    """On tmux < 3.7 the pre-#413 contract is preserved byte-for-byte.

    paste-buffer -p only emits markers when the pane enabled DECSET 2004 and
    some TUIs (e.g. kiro-cli) never do, so forced delivery keeps the
    hand-crafted wrap + -r (no LF->CR conversion) that #230 introduced —
    safe there because pre-3.7 tmux passes buffer bytes through unchanged.
    """

    def test_forced_paste_wraps_and_uses_r(self, client, mock_subprocess, mock_uuid, legacy_tmux):
        msg = "task line 1\n\n[Assigned by terminal abc]"
        client.send_keys("sess", "win", msg, force_bracketed_paste=True)

        calls = mock_subprocess.run.call_args_list
        assert calls[0] == call(
            ["tmux", "load-buffer", "-b", "cao_abcd1234", "-"],
            input=b"\x1b[200~" + msg.encode() + b"\x1b[201~",
            check=True,
        )
        assert calls[1] == call(
            ["tmux", "paste-buffer", "-r", "-b", "cao_abcd1234", "-t", "sess:win"],
            check=True,
        )

    def test_unforced_paste_stays_raw_with_p(self, client, mock_subprocess, mock_uuid, legacy_tmux):
        """Init-time shell commands keep the raw + -p path on every version."""
        client.send_keys("sess", "win", "ls -la", force_bracketed_paste=False)

        calls = mock_subprocess.run.call_args_list
        assert calls[0][1]["input"] == b"ls -la"
        assert calls[1] == call(
            ["tmux", "paste-buffer", "-p", "-b", "cao_abcd1234", "-t", "sess:win"],
            check=True,
        )


class TestTmuxSanitizationDetection:
    """Version probe behind the tmux >= 3.7 vis(3) gate."""

    @pytest.mark.parametrize(
        "version_output,expected",
        [
            ("tmux 3.3a\n", False),
            ("tmux 3.4\n", False),
            ("tmux 3.6\n", False),
            ("tmux 3.7\n", True),
            ("tmux 3.7a\n", True),
            ("tmux 3.10\n", True),
            ("tmux 4.0\n", True),
            ("tmux next-3.8\n", True),
            ("tmux master\n", True),  # unparseable -> assume sanitizing
        ],
    )
    def test_version_parsing(self, mock_subprocess, version_output, expected):
        mock_subprocess.run.return_value = MagicMock(stdout=version_output)

        assert TmuxClient._tmux_sanitizes_paste_buffers() is expected
        mock_subprocess.run.assert_called_once_with(
            ["tmux", "-V"], capture_output=True, text=True, check=True
        )

    def test_probe_failure_assumes_sanitizing(self, mock_subprocess):
        """If tmux -V fails, prefer raw + -p: never garbage, worst case per-line."""
        mock_subprocess.run.side_effect = Exception("tmux not found")

        assert TmuxClient._tmux_sanitizes_paste_buffers() is True

    def test_result_is_cached(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(stdout="tmux 3.4\n")

        assert TmuxClient._tmux_sanitizes_paste_buffers() is False
        assert TmuxClient._tmux_sanitizes_paste_buffers() is False
        assert mock_subprocess.run.call_count == 1


class TestSendKeysLogRedaction:
    """send_keys must not log payload content at INFO — launch commands carry
    MCP env values (API tokens) and full system prompts. Content is DEBUG-only."""

    def test_info_log_omits_payload(self, client, mock_subprocess, mock_uuid, caplog):
        import logging

        secret = "API_TOKEN=super-secret-value"
        with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.clients.tmux"):
            client.send_keys("sess", "win", f"launch --env {secret}")

        info_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
        assert "super-secret-value" not in info_text
        # Metadata still logged: target and payload length.
        assert "sess:win" in info_text
        assert "keys length" in info_text

    def test_debug_log_retains_payload_for_troubleshooting(
        self, client, mock_subprocess, mock_uuid, caplog
    ):
        import logging

        with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.clients.tmux"):
            client.send_keys("sess", "win", "visible-at-debug")

        debug_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
        assert "visible-at-debug" in debug_text
