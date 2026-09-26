"""Native CLI attach over the server's WS relay (#745/#776).

The client-side half of the attach relay: it owns the local TTY, so its
failure mode is a terminal the user cannot get out of. These cover the two
exits that must not depend on the user pressing a key.
"""

import os
import pty
import time
from unittest.mock import patch

import click
import pytest

from cli_agent_orchestrator.utils import remote_attach


class _FakeWS:
    """Sync websocket double: yields `frames`, then the relay goes away."""

    def __init__(self, frames=()):
        self._frames = list(frames)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def recv(self, *args, **kwargs):
        if self._frames:
            return self._frames.pop(0)
        raise OSError("relay closed")

    def close(self):
        self.closed = True


class _Stdin:
    def __init__(self, fd):
        self._fd = fd

    def isatty(self):
        return True

    def fileno(self):
        return self._fd


class TestWsUrl:
    def test_http_and_https_map_to_ws_and_wss(self):
        assert (
            remote_attach._ws_url("http://s:9889", "abcd1234")
            == "ws://s:9889/terminals/abcd1234/ws"
        )
        assert remote_attach._ws_url("https://s", "abcd1234") == "wss://s/terminals/abcd1234/ws"


class TestDetach:
    def test_remote_detach_exits_without_a_keystroke(self, capfd):
        """When the server ends the relay — the user detached inside the remote
        session — the CLI must return on its own. It reads stdin with a poll
        precisely so it is not parked in a blocking read that only the next
        keypress could release."""
        master, slave = pty.openpty()
        ws = _FakeWS([b"screen bytes"])
        try:
            with (
                patch.object(remote_attach.sys, "stdin", _Stdin(slave)),
                patch("websockets.sync.client.connect", return_value=ws),
            ):
                started = time.monotonic()
                remote_attach.attach_remote_terminal("abcd1234", "http://server:9889")
                elapsed = time.monotonic() - started
        finally:
            os.close(master)
            os.close(slave)

        # Nothing was ever written to the pty: the exit came from the relay.
        assert elapsed < 10
        assert ws.closed
        assert "[detached]" in capfd.readouterr().out

    def test_handshake_failure_is_a_click_error_not_a_raw_traceback(self):
        master, slave = pty.openpty()
        try:
            with (
                patch.object(remote_attach.sys, "stdin", _Stdin(slave)),
                patch(
                    "websockets.sync.client.connect",
                    side_effect=OSError("connection refused"),
                ),
            ):
                with pytest.raises(click.ClickException, match="could not reach"):
                    remote_attach.attach_remote_terminal("abcd1234", "http://server:9889")
        finally:
            os.close(master)
            os.close(slave)


class TestTheCredentialIsNotInTheUrl:
    """A bearer token in a query string is a token in every log on the path.

    The server accepts ``?token=`` only because a browser cannot set a header on
    a WebSocket handshake. This client can, and must: uvicorn logs the raw
    path+query, the access log's redaction was keyed on the parameter names the
    browser paths use, and any proxy in between keeps its own copy — so a native
    attach persisted a reusable credential in plaintext (Copilot review on #802).
    """

    def test_the_token_travels_as_an_authorization_header(self):
        master, slave = pty.openpty()
        ws = _FakeWS([b"bytes"])
        try:
            with (
                patch.object(remote_attach.sys, "stdin", _Stdin(slave)),
                patch("websockets.sync.client.connect", return_value=ws) as connect,
            ):
                remote_attach.attach_remote_terminal(
                    "abcd1234", "http://server:9889", token="SECRET.J.WT"
                )
        finally:
            os.close(master)
            os.close(slave)

        url = connect.call_args.args[0]
        assert "SECRET.J.WT" not in url
        assert "token" not in url
        assert connect.call_args.kwargs["additional_headers"] == {
            "Authorization": "Bearer SECRET.J.WT"
        }

    def test_no_token_sends_no_header(self):
        master, slave = pty.openpty()
        ws = _FakeWS([b"bytes"])
        try:
            with (
                patch.object(remote_attach.sys, "stdin", _Stdin(slave)),
                patch("websockets.sync.client.connect", return_value=ws) as connect,
            ):
                remote_attach.attach_remote_terminal("abcd1234", "http://server:9889")
        finally:
            os.close(master)
            os.close(slave)

        assert connect.call_args.kwargs["additional_headers"] is None


class TestRefusals:
    def test_non_tty_is_refused_before_connecting(self):
        class _NotATty:
            def isatty(self):
                return False

        with (
            patch.object(remote_attach.sys, "stdin", _NotATty()),
            patch("websockets.sync.client.connect") as connect,
        ):
            with pytest.raises(click.ClickException, match="requires a TTY"):
                remote_attach.attach_remote_terminal("abcd1234", "http://server:9889")
        connect.assert_not_called()
