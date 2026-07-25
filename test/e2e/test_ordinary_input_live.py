"""Live acceptance for ordinary message delivery, measured in bytes.

``send_input`` reaches a pane through ``TmuxClient.send_keys`` with
``force_bracketed_paste=True``.  Every other test of that path asserts the
*argv* it builds, which is exactly the blind spot that let a real defect
ship: the argv was self-consistently correct, and tmux did something else
with it.

The defect these tests exist to keep out: ``send_keys`` used to write the
paste markers into the buffer itself and paste with ``-r``.  tmux
sanitizes control bytes on their way out of a paste buffer, so those ESCs
never reached the pane as escapes — they arrived as the seven printable
characters ``^[[200~``, which a composer types out as visible text and
then submits.  Nothing that inspects an argv can see that.  Only the
pane's own byte stream can.

The pane's foreground process is a raw recorder rather than a provider
TUI, and that is deliberate.  It occupies the composer's position — it
owns the tty, it is what tmux writes to — and it does two things a real
composer cannot:

* it decides, per pane, whether to **enable bracketed paste** (``DECSET
  2004``).  Both answers are covered here, because they are different
  code paths inside tmux and only one of them can produce framing;
* it records the byte stream verbatim, so the assertion is a byte
  comparison and not a reading of rendered screen text.  Rendered text
  cannot distinguish an escape the terminal consumed from one it never
  received, and it cannot show a CR that a composer already acted on.

No artifact is asserted against: no screen capture, no log line, no
recorded argv.  The only evidence is what arrived at the tty.

Isolation: every tmux invocation — this file's own *and* the ones
``TmuxClient`` makes for itself — carries one explicit ``-S`` socket that
this run created.  ``TmuxClient.send_keys`` hardcodes ``tmux`` and passes
no socket, so it is handed the selector as a shim ahead of the real
binary on ``PATH``, with the inherited ``TMUX`` stripped.  ``$HOME`` is
neither set nor read: nothing here needs CAO's state root, because
``send_keys`` is called directly rather than through a server.  The
mechanics live in ``test.fixtures.tmux_server`` and are proved in
``test/e2e/test_tmux_isolation.py``.

Run with: ``pytest -m e2e test/e2e/test_ordinary_input_live.py -v``
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Iterator

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient

pytestmark = pytest.mark.e2e

# What a leak looks like once tmux has neutralized it: caret notation for
# ESC, as printable ASCII.  This is the byte string a composer would show
# the user and then send to the model.
LEAKED_START = b"^[[200~"
LEAKED_END = b"^[[201~"

# What genuine framing looks like: real ESC bytes, which a composer
# consumes as a mode signal and never displays.
FRAMED_START = b"\x1b[200~"
FRAMED_END = b"\x1b[201~"

# Two lines, because the single-line case cannot distinguish "newlines
# preserved" from "no newlines present".  The payload deliberately
# contains no ESC of its own, so any escape byte found in the capture
# came from the delivery path rather than from the message.
MULTILINE = "line one\nline two"

# Reads the tty in raw mode and appends every byte to a file, having
# first decided whether to advertise bracketed paste.
RECORDER = r"""
import os, sys, tty
fd = sys.stdin.fileno()
tty.setraw(fd)
if sys.argv[3] == "yes":
    os.write(1, b"\x1b[?2004h")
sink = open(sys.argv[1], "ab", buffering=0)
# Announced only after raw mode and the DECSET decision are both in
# effect, so a sender can never be racing the pane's readiness.
with open(sys.argv[2], "w") as ready:
    ready.write("ready")
while True:
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    sink.write(chunk)
"""


@dataclass
class RecorderPane:
    """One real pane whose received bytes are readable from the test."""

    session: str
    window: str
    capture: Path
    # Whether this pane told tmux it accepts bracketed paste.
    accepts_paste_framing: bool

    def received(self) -> bytes:
        return self.capture.read_bytes()

    def wait_for(self, expected: bytes, timeout: float = 5.0) -> bytes:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if expected in self.received():
                break
            time.sleep(0.05)
        return self.received()


@pytest.fixture(scope="module")
def tmux_server() -> Iterator[TmuxServer]:
    """One private tmux server, named explicitly on every invocation."""
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    with isolated_tmux_server() as server:
        yield server


@pytest.fixture
def client(tmux_server: TmuxServer, monkeypatch: pytest.MonkeyPatch) -> TmuxClient:
    """A ``TmuxClient`` whose unqualified ``tmux`` calls land on our server.

    ``TmuxClient`` resolves ``tmux`` from ``PATH`` and inherits this
    process's environment, so the selector has to reach it as a shim.
    ``TMUX``/``TMUX_PANE`` are removed as well: an explicit ``-S`` does
    outrank them, but leaving an ambient selector in place for a test
    about byte-level delivery is how a fixture ends up measuring the
    operator's pane.
    """
    assert tmux_server.owned_root is not None
    shim_dir = tmux_server.write_shim(tmux_server.owned_root / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return TmuxClient()


def _spawn_recorder(
    tmux: TmuxServer, tmp_path: Path, *, accepts_paste_framing: bool
) -> RecorderPane:
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER)
    suffix = "on" if accepts_paste_framing else "off"
    session = f"cao-ord-{suffix}-{uuid.uuid4().hex[:6]}"
    window = "delivery"
    capture = tmp_path / f"received-{suffix}.bin"
    capture.write_bytes(b"")
    ready = tmp_path / f"ready-{suffix}"
    command = [
        sys.executable,
        str(script),
        str(capture),
        str(ready),
        "yes" if accepts_paste_framing else "no",
    ]
    tmux.out(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        window,
        " ".join(f"'{part}'" for part in command),
    )
    # Waited for rather than slept past: until the recorder has raw mode
    # and its DECSET decision in effect, a send would measure a race.
    end = time.monotonic() + 5.0
    while time.monotonic() < end and not ready.exists():
        time.sleep(0.05)
    assert ready.exists(), f"the {suffix} recorder never signalled ready"
    return RecorderPane(
        session=session,
        window=window,
        capture=capture,
        accepts_paste_framing=accepts_paste_framing,
    )


@pytest.fixture
def framing_pane(tmux_server: TmuxServer, tmp_path: Path) -> Iterator[RecorderPane]:
    """A pane that advertised bracketed paste, as a modern TUI composer does."""
    pane = _spawn_recorder(tmux_server, tmp_path, accepts_paste_framing=True)
    yield pane
    tmux_server.kill_session(pane.session)


@pytest.fixture
def plain_pane(tmux_server: TmuxServer, tmp_path: Path) -> Iterator[RecorderPane]:
    """A pane that never advertised bracketed paste, as a bare shell does."""
    pane = _spawn_recorder(tmux_server, tmp_path, accepts_paste_framing=False)
    yield pane
    tmux_server.kill_session(pane.session)


def _deliver(client: TmuxClient, pane: RecorderPane, text: str) -> bytes:
    """One ordinary message delivery, exactly as ``send_input`` performs it.

    ``submit_delay`` is shortened only to keep the test quick; it governs
    the pause before Enter and has no bearing on which bytes arrive.
    """
    client.send_keys(
        pane.session,
        pane.window,
        text,
        enter_count=1,
        force_bracketed_paste=True,
        submit_delay=0.05,
    )
    return pane.wait_for(text.encode().split(b"\n")[-1])


def _assert_no_visible_sentinel(received: bytes) -> None:
    assert LEAKED_START not in received, (
        "the paste-start marker reached the pane as printable text; a composer "
        f"would type and submit it: {received!r}"
    )
    assert LEAKED_END not in received, (
        "the paste-end marker reached the pane as printable text; a composer "
        f"would type and submit it: {received!r}"
    )


class TestOrdinaryDeliveryLeavesNoVisibleMarkers:
    def test_a_composer_pane_receives_real_framing_and_no_printable_markers(
        self, client, framing_pane
    ):
        """The whole fix in one measurement: framed, and framed for real.

        Both halves are asserted because either alone is satisfiable by a
        broken implementation.  "No printable markers" alone passes if
        the framing were simply dropped, which would regress multi-line
        delivery back into per-line submission.  "Real framing" alone
        passes if the printable copy were *also* present.
        """
        received = _deliver(client, framing_pane, MULTILINE)

        _assert_no_visible_sentinel(received)
        assert FRAMED_START in received, received
        assert FRAMED_END in received, received
        assert MULTILINE.encode() in received, received

    def test_the_message_is_framed_once_and_arrives_whole(self, client, framing_pane):
        """Exactly one paste, with the payload inside it and nothing else.

        Slicing between the markers rather than searching the whole
        stream: a marker pair that bracketed the wrong span — empty, or
        swallowing the trailing Enter — would satisfy a containment
        check while still delivering the wrong input.
        """
        received = _deliver(client, framing_pane, MULTILINE)

        assert received.count(FRAMED_START) == 1, received
        assert received.count(FRAMED_END) == 1, received
        start = received.index(FRAMED_START) + len(FRAMED_START)
        end = received.index(FRAMED_END)
        assert received[start:end] == MULTILINE.encode(), received

    def test_newlines_survive_as_newlines(self, client, framing_pane):
        """The reason the old code passed ``-r``, kept.

        ``paste-buffer`` rewrites LF as CR unless told not to, and a
        composer reads CR as Enter — so a two-line message would be
        submitted as two messages, the first one truncated.  Asserted as
        the absence of *any* CR before the deliberate one: the payload
        contributes none, so every CR here is Enter's.
        """
        received = _deliver(client, framing_pane, MULTILINE)

        body = received[: received.index(FRAMED_END)]
        assert b"\r" not in body, body
        assert b"\n" in body, body

    def test_a_pane_that_never_asked_for_framing_gets_the_bare_message(self, client, plain_pane):
        """The control that makes the framing assertions mean something.

        tmux frames only for a pane that advertised ``DECSET 2004``, so
        this pane must receive no framing at all — proving the markers in
        the tests above came from tmux answering the pane's advertisement
        and are not something this code emits unconditionally.

        It is also the case the old implementation was written for, and
        got wrong in the most damaging way: forcing markers at a pane
        that cannot honour them meant they were *always* displayed.
        """
        received = _deliver(client, plain_pane, MULTILINE)

        _assert_no_visible_sentinel(received)
        assert FRAMED_START not in received, received
        assert FRAMED_END not in received, received
        assert MULTILINE.encode() in received, received

    def test_no_escape_byte_reaches_a_plain_pane_at_all(self, client, plain_pane):
        """Stronger than the marker check, and cheap: nothing escapes.

        The payload carries no ESC, so any escape byte in this capture
        was introduced by the delivery path — whether or not it happened
        to spell a paste marker.
        """
        received = _deliver(client, plain_pane, MULTILINE)

        assert b"\x1b" not in received, received


class TestTheRecorderCanSeeALeak:
    """Falsifiability: the assertions above can fail.

    A negative result is worth nothing unless the same recorder, the same
    capture and the same byte search do detect a leak when one is
    present.  This reproduces the old implementation's exact wire
    behaviour — markers written into the buffer, pasted with ``-r`` — and
    requires that it be caught.
    """

    def test_the_previous_implementations_bytes_are_caught(self, tmux_server, framing_pane):
        buf = f"cao_leak_{uuid.uuid4().hex[:8]}"
        legacy = FRAMED_START + MULTILINE.encode() + FRAMED_END
        tmux_server.run("load-buffer", "-b", buf, "-", input=legacy)
        try:
            tmux_server.run(
                "paste-buffer",
                "-r",
                "-b",
                buf,
                "-t",
                f"{framing_pane.session}:{framing_pane.window}",
            )
            received = framing_pane.wait_for(LEAKED_END)
        finally:
            tmux_server.run("delete-buffer", "-b", buf, check=False)

        # The bytes the fix removed, still detectable by the same search
        # the passing tests use.
        assert LEAKED_START in received, received
        assert LEAKED_END in received, received
        with pytest.raises(AssertionError):
            _assert_no_visible_sentinel(received)


class TestTmuxStillNeutralizesBufferedEscapes:
    """Why the fix is what it is, pinned to observable tmux behaviour.

    If a future tmux stopped sanitizing buffered control bytes, the old
    approach would start working again and this test would fail — which
    is the point.  It documents the constraint the fix is built on rather
    than leaving it as a claim in a commit message.
    """

    def test_an_escape_written_into_a_buffer_is_stored_intact(self, tmux_server):
        """The neutralization is in the writer, not in ``load-buffer``.

        Worth separating: if the buffer itself were lossy, no paste flag
        could recover the framing and the fix would have to look
        different.
        """
        buf = f"cao_store_{uuid.uuid4().hex[:8]}"
        tmux_server.run("load-buffer", "-b", buf, "-", input=FRAMED_START + b"hi" + FRAMED_END)
        try:
            stored = tmux_server.run("show-buffer", "-b", buf).stdout
        finally:
            tmux_server.run("delete-buffer", "-b", buf, check=False)

        assert "\x1b[200~hi\x1b[201~" == stored.rstrip("\n"), repr(stored)

    def test_but_arrives_at_the_pane_as_printable_text(self, tmux_server, framing_pane):
        """Stored intact, delivered neutralized — the whole defect."""
        buf = f"cao_neutral_{uuid.uuid4().hex[:8]}"
        tmux_server.run("load-buffer", "-b", buf, "-", input=FRAMED_START + b"hi" + FRAMED_END)
        try:
            tmux_server.run(
                "paste-buffer",
                "-r",
                "-b",
                buf,
                "-t",
                f"{framing_pane.session}:{framing_pane.window}",
            )
            received = framing_pane.wait_for(LEAKED_END)
        finally:
            tmux_server.run("delete-buffer", "-b", buf, check=False)

        assert LEAKED_START in received, received
        assert FRAMED_START not in received, received


class TestNothingHereTouchedAnotherServer:
    def test_every_pane_used_was_on_the_private_socket(self, tmux_server, framing_pane):
        """The isolation claim, asserted rather than assumed."""
        reported = tmux_server.out(
            "display-message",
            "-p",
            "-t",
            f"{framing_pane.session}:{framing_pane.window}",
            "#{socket_path}",
        )
        assert Path(reported).resolve() == tmux_server.socket_path.resolve()
