"""Live byte-exactness for the §3.2 navigation/editing keys (§10.2).

The unit tier proves the mapping table; it cannot prove what tmux emits
for a name.  These tests run a real ``cao-server`` against a real pane
whose foreground process records the byte stream verbatim, and assert
the exact bytes every §3.2 row lands — including the two answers only a
live pane can give:

* cursor keys are DECCKM-translated by tmux (``ESC[A`` in normal mode,
  ``ESC O A`` in application-cursor mode — the translation is tmux's,
  per §3.2); and
* Home/End are hard-coded ``ESC[1~``/``ESC[4~`` regardless of that mode
  (OD5, source-observed — pinned here so a tmux change is caught rather
  than believed).

Isolation is the same as ``test_control_input_live.py``'s: a private
tmux server named on every invocation, a redirected ``$HOME``, and a
byte recorder in the pane.  Run with:

    pytest -m e2e test/e2e/test_control_input_navigation_live.py -v
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from test.e2e.test_control_input_live import (
    LivePane,
    _await,
    _register,
    _send,
    _settled,
)
from test.fixtures.cao_server import CaoServer, _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Iterator, List, Optional

import pytest
import requests

pytestmark = pytest.mark.e2e


# A byte recorder in raw mode, with DECSET 2004 on (so a leaked paste
# sentinel would be visible as bytes), writing a ready file only once the
# modes are in effect.  The application-cursor variant additionally sets
# DECCKM (?1h), which is the mode under which tmux emits SS3 cursors.
_RECORDER = r"""
import os, sys, tty
fd = sys.stdin.fileno()
tty.setraw(fd)
os.write(1, b"\x1b[?2004h" + sys.argv[3].encode("latin-1"))
sink = open(sys.argv[1], "ab", buffering=0)
with open(sys.argv[2], "w") as ready:
    ready.write("ready")
while True:
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    sink.write(chunk)
"""


@pytest.fixture(scope="session")
def tmux_server() -> Iterator[TmuxServer]:
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    with isolated_tmux_server() as server:
        yield server


@pytest.fixture(scope="session")
def control_server(
    tmp_path_factory: pytest.TempPathFactory, tmux_server: TmuxServer
) -> Iterator[CaoServer]:
    assert tmux_server.owned_root is not None
    shim = tmux_server.write_shim(tmux_server.owned_root / "bin")
    home = tmp_path_factory.mktemp("cao_home_navigation_live")
    server = _start_cao_server(
        home,
        _pick_free_port(),
        extra_env={"PATH": tmux_server.subprocess_env(shim)["PATH"]},
    )
    try:
        yield server
    finally:
        server.stop()


def _spawn_recorder(
    server: CaoServer,
    tmux: TmuxServer,
    tmp_path: Path,
    *,
    name: str,
    mode_bytes: bytes = b"",
) -> LivePane:
    script = tmp_path / f"recorder-{name}.py"
    script.write_text(_RECORDER)
    capture = tmp_path / f"{name}.bin"
    capture.touch()
    ready = tmp_path / f"{name}.ready"
    session = f"cao-nav-{uuid.uuid4().hex[:6]}"
    printed = tmux.out(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "acceptance",
        "-P",
        "-F",
        "#{pane_id}\t#{window_id}\t#{socket_path}",
        " ".join(
            f"'{part}'"
            for part in [
                sys.executable,
                str(script),
                str(capture),
                str(ready),
                mode_bytes.decode("latin-1"),
            ]
        ),
    )
    pane_id, window_id, socket_path = printed.split("\t")
    assert Path(socket_path).resolve() == tmux.socket_path.resolve()
    pane = LivePane(
        terminal_id=uuid.uuid4().hex[:8],
        session=session,
        pane_id=pane_id,
        window_id=window_id,
        generation=f"gen-live-{uuid.uuid4().hex[:8]}",
        capture=capture,
        server_socket_path=socket_path,
    )
    _register(server, pane, "mock_cli")
    assert _await(ready.exists), f"the {name} recorder never signalled ready"
    return pane


@pytest.fixture
def recorder(
    control_server: CaoServer, tmux_server: TmuxServer, tmp_path: Path
) -> Iterator[LivePane]:
    pane = _spawn_recorder(control_server, tmux_server, tmp_path, name="plain")
    yield pane
    tmux_server.kill_session(pane.session)


@pytest.fixture
def app_cursor_recorder(
    control_server: CaoServer, tmux_server: TmuxServer, tmp_path: Path
) -> Iterator[LivePane]:
    # DECCKM (?1h): application cursor mode.
    pane = _spawn_recorder(
        control_server, tmux_server, tmp_path, name="appcur", mode_bytes=b"\x1b[?1h"
    )
    yield pane
    tmux_server.kill_session(pane.session)


def _send_keys(server: CaoServer, pane: LivePane, events: List[dict]) -> dict:
    response = _send(server, pane, text=None, events=events)
    assert response.status_code == 200, response.text
    return response.json()


# The §3.2 byte table, asserted exactly: what the pane receives per wire
# name in a normal-mode pane (the tmux canonical name in, the encoded
# bytes out).  Aliases (PPage/NPage/DC/IC) never appear — the sink passes
# the canonical name, and this is where that is proven at the byte level.
NORMAL_MODE_BYTES = [
    ("Up", b"\x1b[A"),
    ("Down", b"\x1b[B"),
    ("Right", b"\x1b[C"),
    ("Left", b"\x1b[D"),
    ("Home", b"\x1b[1~"),
    ("End", b"\x1b[4~"),
    ("PageUp", b"\x1b[5~"),
    ("PageDown", b"\x1b[6~"),
    ("Delete", b"\x1b[3~"),
    ("Insert", b"\x1b[2~"),
    ("Tab", b"\x09"),
]


class TestNavigationKeyBytes:
    @pytest.mark.parametrize("key,expected", NORMAL_MODE_BYTES)
    def test_each_new_key_arrives_byte_exact(self, control_server, recorder, key, expected):
        start = recorder.size()
        body = _send_keys(control_server, recorder, [{"type": "key", "key": key}])
        assert body["outcome"] == "accepted", body
        _settled(recorder, start, expected)
        time.sleep(0.2)
        # Exactly these bytes and no others: no sentinel framing, no
        # literalized name, no doubled key.
        assert recorder.bytes_since(start) == expected

    @pytest.mark.parametrize(
        "key,expected",
        [("Up", b"\x1bOA"), ("Down", b"\x1bOB"), ("Right", b"\x1bOC"), ("Left", b"\x1bOD")],
    )
    def test_cursor_keys_use_ss3_in_application_cursor_mode(
        self, control_server, app_cursor_recorder, key, expected
    ):
        """DECCKM translation is tmux's (§3.2): the same wire name emits
        SS3 in application-cursor mode, which is what makes the mapping
        correct for readline-style and fullscreen TUIs alike."""
        start = app_cursor_recorder.size()
        body = _send_keys(control_server, app_cursor_recorder, [{"type": "key", "key": key}])
        assert body["outcome"] == "accepted", body
        _settled(app_cursor_recorder, start, expected)
        time.sleep(0.2)
        assert app_cursor_recorder.bytes_since(start) == expected

    @pytest.mark.parametrize("key,expected", [("Home", b"\x1b[1~"), ("End", b"\x1b[4~")])
    def test_home_and_end_ignore_application_cursor_mode(
        self, control_server, app_cursor_recorder, key, expected
    ):
        """OD5: tmux hard-codes Home/End to ``ESC[1~``/``ESC[4~`` in both
        modes.  Pinned live so a tmux behaviour change is caught here,
        not inferred."""
        start = app_cursor_recorder.size()
        body = _send_keys(control_server, app_cursor_recorder, [{"type": "key", "key": key}])
        assert body["outcome"] == "accepted", body
        _settled(app_cursor_recorder, start, expected)
        time.sleep(0.2)
        assert app_cursor_recorder.bytes_since(start) == expected

    def test_a_mixed_sequence_lands_byte_exact_and_in_order(self, control_server, recorder):
        """Text, navigation, and the submitting Enter, one lease, one order."""
        start = recorder.size()
        events = [
            {"type": "text", "text": "hi"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Left"},
            {"type": "key", "key": "Enter"},
        ]
        body = _send_keys(control_server, recorder, events)
        assert body["outcome"] == "accepted", body
        assert [event["outcome"] for event in body["events"]] == ["sent"] * 4
        expected = b"hi" + b"\x1b[A" + b"\x1b[D" + b"\r"
        _settled(recorder, start, expected)
        time.sleep(0.2)
        assert recorder.bytes_since(start) == expected

    def test_an_unsupported_key_is_refused_with_zero_bytes(self, control_server, recorder):
        """BTab has no registry pin: refused before any write, and the
        pane's byte stream proves nothing left the server."""
        start = recorder.size()
        body = _send_keys(control_server, recorder, [{"type": "key", "key": "BTab"}])
        assert body["outcome"] == "refused", body
        assert body["reason_code"] == "unsupported-key", body
        time.sleep(0.3)
        assert recorder.bytes_since(start) == b""


class TestArbiterAndGuardAgainstARealPane:
    def test_a_concurrent_lease_contender_is_pane_busy(self, control_server, recorder):
        """The cross-process half of the arbiter, proven from a second
        process (this one): the test holds the pane's flock under the
        server's own state root, and the server's writer is refused with
        zero bytes rather than queueing or interleaving."""
        lock_dir = recorder_lock_dir(recorder, control_server)
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / f"pane-{recorder.pane_id[1:]}.lock"
        start = recorder.size()
        with open(lock_file, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            body = _send_keys(control_server, recorder, [{"type": "key", "key": "Up"}])
            assert body["outcome"] == "refused", body
            assert body["reason_code"] == "pane-busy", body
            assert "input lease is held by" in body["detail"], body
            time.sleep(0.3)
            assert recorder.bytes_since(start) == b""
        # Released: the same request is accepted — refusal is the
        # reattemptable answer, and re-attempting works.
        body = _send_keys(control_server, recorder, [{"type": "key", "key": "Up"}])
        assert body["outcome"] == "accepted", body
        _settled(recorder, start, b"\x1b[A")

    def test_the_copy_mode_guard_still_precedes_the_payload(
        self, control_server, tmux_server, recorder
    ):
        """cond-0178 on the navigation path: a proven copy mode is exited
        once, then the payload lands — the guard precedes every write."""
        tmux_server.out("copy-mode", "-t", recorder.pane_id)
        assert _await(
            lambda: tmux_server.out("display", "-p", "-t", recorder.pane_id, "#{pane_in_mode}")
            == "1"
        )
        start = recorder.size()
        body = _send_keys(control_server, recorder, [{"type": "key", "key": "PageUp"}])
        assert body["outcome"] == "accepted", body
        # The exit control ran before the payload (the mode is gone), and
        # the payload bytes still arrived exactly.
        assert _await(
            lambda: tmux_server.out("display", "-p", "-t", recorder.pane_id, "#{pane_in_mode}")
            == "0"
        )
        _settled(recorder, start, b"\x1b[5~")
        time.sleep(0.2)
        assert recorder.bytes_since(start) == b"\x1b[5~"


def recorder_lock_dir(pane: LivePane, server: CaoServer) -> Path:
    """The pane-input lock directory under the *server's* state root."""
    return server.home_dir / ".aws" / "cli-agent-orchestrator" / "pane-input-locks"
