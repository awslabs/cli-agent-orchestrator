"""The v3 sequence-key primitive against a real tmux server.

The argv of ``send_sequence_key`` is proven exactly against a mocked
subprocess in ``test_tmux_literal_input.py``.  What a mock cannot prove
is that tmux agrees with the argv — and tmux's failure mode is silent:
``send-keys`` without ``-l`` does not error on a key name it does not
know, it sends the argument as literal bytes and exits 0.  The wire name
``Backspace`` is such a name (tmux's name for the erase key is
``BSpace``), so before the sink translated it, a v3 ``Backspace`` event
typed the nine characters "Backspace" into the composer while the journal
recorded the event ``sent``.

These tests run the real client against one disposable tmux server whose
pane records its tty byte stream verbatim, and assert the two halves of
the contract only a real server can answer:

* a ``Backspace`` event delivers the erase control (one ``0x7f`` byte),
  and the wire name's literal bytes never reach the pane;
* the names that were already exact — ``Escape``, ``C-c``, ``C-s``,
  ``Enter`` — still land as their exact control bytes.

Isolation follows ``test.fixtures.tmux_server``: one private server,
named by an explicit ``-S`` socket on every invocation and destroyed with
everything inside it.  The client resolves its tmux through a shim that
carries the same selector, so its ``list-panes`` probe and its
``send-keys`` write land on the fixture's server and nowhere else.
"""

from __future__ import annotations

import shutil
import sys
import time
import uuid
from pathlib import Path
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

# Reads the tty in raw mode and appends every byte to a file.  Raw mode
# matters twice over: the byte stream is recorded verbatim (a screen
# reading could mistake an echoed erase for text), and ISIG/IXON are off,
# so C-c and C-s arrive as data instead of acting on the line discipline.
# The code carries no single quotes on purpose: it travels to the pane as
# one single-quote-wrapped shell word (see _spawn_recorder), so a quote
# here would break the shell parse rather than fail a test assertion.
RECORDER = r"""
import os, sys, tty
fd = sys.stdin.fileno()
tty.setraw(fd)
sink = open(sys.argv[1], "ab", buffering=0)
with open(sys.argv[2], "w") as ready:
    ready.write("ready")
while True:
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    sink.write(chunk)
"""


@pytest.fixture
def tmux_server() -> Iterator[TmuxServer]:
    """One private tmux server, named explicitly on every invocation."""
    with isolated_tmux_server() as server:
        yield server


@pytest.fixture
def client(tmux_server: TmuxServer, monkeypatch) -> Iterator[TmuxClient]:
    """The real client, with every tmux it spawns aimed at the private server."""
    assert tmux_server.owned_root is not None
    shim_dir = tmux_server.write_shim(tmux_server.owned_root / "bin")
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.tmux.tmux_binary",
        lambda: str(shim_dir / "tmux"),
    )
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        yield TmuxClient()


def _wait_for(path: Path, message: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _await_bytes(capture: Path, count: int, timeout: float = 10.0) -> bytes:
    """The capture once it holds at least ``count`` bytes, or a loud timeout."""
    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline:
        data = capture.read_bytes() if capture.exists() else b""
        if len(data) >= count:
            return data
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {count} bytes; received {data!r}")


def _spawn_recorder(tmux_server: TmuxServer, tmp_path: Path) -> Tuple[str, str, Path]:
    """One pane running the byte recorder: (pane id, server socket, capture)."""
    capture = tmp_path / "capture.bin"
    ready = tmp_path / "ready"
    # Keep a dead recorder's pane around: its traceback on the pane screen
    # is the only error channel a pane that died before announcing has.
    tmux_server.run("set-option", "-g", "remain-on-exit", "on")
    # One shell-command string, each part single-quoted (the recorder
    # carries no single quotes), the way the live-acceptance suite spawns.
    command = " ".join(
        f"'{part}'" for part in [sys.executable, "-c", RECORDER, str(capture), str(ready)]
    )
    # The socket is read back from the pane's own server rather than taken
    # from the fixture handle: the identity the client must be bound to is
    # the one that answers for this pane id.
    printed = tmux_server.out(
        "new-session",
        "-d",
        "-s",
        f"cao-bspace-{uuid.uuid4().hex[:6]}",
        "-P",
        "-F",
        "#{pane_id}\t#{socket_path}",
        command,
    )
    pane_id, socket_path = printed.split("\t")
    try:
        _wait_for(ready, "the recorder never announced readiness")
    except AssertionError:
        # The pane's own screen is the recorder's only error channel: a
        # recorder that never became ready usually died on an exception
        # whose traceback is on that screen.
        screen = tmux_server.out("capture-pane", "-p", "-t", pane_id)
        raise AssertionError(f"the recorder never announced readiness; pane screen: {screen!r}")
    return pane_id, socket_path, capture


class TestBackspaceAtTheTmuxBoundary:
    """The wire name must become tmux's name, never literal bytes."""

    def test_a_backspace_event_delivers_erase_never_literal_bytes(
        self, client, tmux_server, tmp_path
    ):
        pane_id, socket_path, capture = _spawn_recorder(tmux_server, tmp_path)
        # A marker the erase can be observed against: two literal bytes first.
        tmux_server.run("send-keys", "-t", pane_id, "-l", "--", "AB")
        assert _await_bytes(capture, 2) == b"AB"

        client.send_sequence_key(pane_id, "Backspace", expected_server_identity=socket_path)

        # Exactly the erase control, one DEL byte, and nothing else: the
        # whole stream at this point is the marker plus 0x7f.  Before the
        # sink translation this read back b"ABBackspace" with tmux
        # reporting success.
        assert _await_bytes(capture, 3) == b"AB\x7f"
        assert b"Backspace" not in capture.read_bytes()

    def test_the_exact_key_names_still_land_as_their_control_bytes(
        self, client, tmux_server, tmp_path
    ):
        # The translation must not trade one wrong name for four broken
        # ones: the names that were already exact stay exact.
        pane_id, socket_path, capture = _spawn_recorder(tmux_server, tmp_path)
        expectations = [("Escape", b"\x1b"), ("C-c", b"\x03"), ("C-s", b"\x13"), ("Enter", b"\r")]
        received = 0
        for name, control in expectations:
            client.send_sequence_key(pane_id, name, expected_server_identity=socket_path)
            data = _await_bytes(capture, received + 1)
            received += 1
            assert data[received - 1 : received] == control, (
                f"{name} delivered {data[received - 1:received]!r}, expected {control!r}; "
                f"stream so far {data!r}"
            )
