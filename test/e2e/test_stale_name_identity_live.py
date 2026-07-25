"""A reused window name must not be a writable handle on someone else's pane.

The defect this exists to keep out is a delivery, not a lookup. A terminal
row records the window it was created in. Nothing ever demoted that row
when its window went away, and the ordinary send path resolved
``session:window`` **by name** — so once a later, unrelated window took
the freed name, the stale row became a live handle on it. A message
addressed to a terminal that no longer existed arrived in a stranger's
composer and was submitted there.

Nothing that inspects an argv can catch that: the argv is self-consistent
and correct, and the name resolves. Only the receiving pane can say
whether bytes arrived, so the assertion here is made against the bytes a
real pane received and not against a screen capture, a log line, or a
recorded command.

The pane's foreground process is a raw recorder rather than a provider
TUI, for the same reason as the other live delivery suites: it occupies
the composer's position, owns the tty, and writes every byte it receives
to a file verbatim. Rendered text could not distinguish "nothing was
sent" from "something was sent and the renderer swallowed it".

Isolation: every tmux invocation — this file's own and the ones
``TmuxClient`` makes for itself — carries one explicit ``-S`` socket that
this run created. ``TmuxClient`` hardcodes ``tmux`` and passes no socket,
so it is handed the selector as a shim ahead of the real binary on
``PATH``, with any inherited ``TMUX`` stripped. The mechanics live in
``test.fixtures.tmux_server``.

Run with: ``pytest -m e2e test/e2e/test_stale_name_identity_live.py -v``
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

from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.services import terminal_service

pytestmark = pytest.mark.e2e

# Distinctive enough that finding any fragment of it in a pane's byte
# stream is unambiguous evidence of delivery.
PAYLOAD = "STALE-NAME-MUST-NOT-ARRIVE-8f31c2"

# Reads the tty in raw mode and appends every byte to a file. It never
# advertises bracketed paste, because what is being measured is whether
# anything arrives at all — not how it would be framed.
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


@dataclass
class RecorderPane:
    """One real pane whose received bytes are readable from the test."""

    session: str
    window: str
    capture: Path

    def received(self) -> bytes:
        return self.capture.read_bytes()


@pytest.fixture(scope="module")
def tmux_server() -> Iterator[TmuxServer]:
    """One private tmux server, named explicitly on every invocation."""
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    with isolated_tmux_server() as server:
        yield server


@pytest.fixture
def tmux_backend(tmux_server: TmuxServer, monkeypatch: pytest.MonkeyPatch) -> TmuxBackend:
    """A backend whose unqualified ``tmux`` calls land on our server only."""
    assert tmux_server.owned_root is not None
    shim_dir = tmux_server.write_shim(tmux_server.owned_root / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    backend = TmuxBackend()
    set_backend(backend)
    return backend


def _spawn_recorder(tmux: TmuxServer, tmp_path: Path, session: str, window: str) -> RecorderPane:
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER)
    capture = tmp_path / f"received-{uuid.uuid4().hex[:8]}.bin"
    capture.write_bytes(b"")
    ready = tmp_path / f"ready-{uuid.uuid4().hex[:8]}"
    command = [sys.executable, str(script), str(capture), str(ready)]
    tmux.out(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        window,
        " ".join(f"'{part}'" for part in command),
    )
    # Waited for rather than slept past: until the recorder holds raw mode
    # a send would be measuring a race rather than a decision.
    end = time.monotonic() + 5.0
    while time.monotonic() < end and not ready.exists():
        time.sleep(0.05)
    assert ready.exists(), "the recorder never signalled ready"
    return RecorderPane(session=session, window=window, capture=capture)


def _register(terminal_id: str, pane: RecorderPane, client: TmuxClient) -> dict:
    """Record the full canonical identity of a live pane, as a launch would.

    The identity is read back through the same observation the boundary
    later compares against, so the test cannot pass by recording facts
    nobody could observe.
    """
    identity = client.window_identity(pane.session, pane.window)
    assert identity is not None, "the fixture pane had no observable identity"
    for field in ("pane_id", "window_id", "session_id", "pane_pid", "server_socket_path"):
        assert field in identity, f"{field} missing from the observed identity"
    database.create_terminal(
        terminal_id=terminal_id,
        tmux_session=pane.session,
        tmux_window=pane.window,
        provider="claude_code",
        pane_id=identity["pane_id"],
        window_id=identity["window_id"],
        session_id=identity["session_id"],
        pane_pid=int(identity["pane_pid"]),
        server_socket_path=identity["server_socket_path"],
    )
    return identity


class TestReusedWindowNameIsNotAHandle:
    def test_send_input_refuses_and_delivers_zero_bytes(
        self,
        isolated_memory_db,
        tmux_server: TmuxServer,
        tmux_backend: TmuxBackend,
        tmp_path: Path,
    ):
        """The whole defect, end to end, measured at the receiving tty."""
        client = TmuxClient()
        name = f"cao-stale-{uuid.uuid4().hex[:6]}"
        window = "worker-alpha"
        terminal_id = uuid.uuid4().hex[:8]

        original = _spawn_recorder(tmux_server, tmp_path, name, window)
        recorded = _register(terminal_id, original, client)

        # The registered window goes away, and a later, unrelated one
        # takes the freed name — the sequence a resumed or restarted
        # worker produces every time.
        tmux_server.run("kill-session", "-t", f"={name}", check=False)
        impostor = _spawn_recorder(tmux_server, tmp_path, name, window)
        assert impostor.received() == b"", "the impostor pane started with a dirty buffer"

        # The name now resolves — to somebody else.
        reresolved = client.window_identity(name, window)
        assert reresolved is not None
        assert reresolved["pane_id"] != recorded["pane_id"]

        # The byte claim is asserted first and on its own, not folded into
        # a ``pytest.raises``. If the boundary regresses, this must fail
        # with the misdelivered bytes in the message — "no exception was
        # raised" would describe the symptom and hide the delivery.
        refusal: Exception | None = None
        try:
            terminal_service.send_input(terminal_id, PAYLOAD)
        except terminal_service.TerminalIdentityMismatchError as exc:
            refusal = exc

        # Settle long enough that a delivery in flight would have landed:
        # asserting emptiness immediately would pass even if the write had
        # been issued.
        time.sleep(0.5)
        received = impostor.received()
        assert received == b"", f"bytes reached an unrelated pane: {received!r}"
        assert PAYLOAD.encode() not in received
        assert isinstance(refusal, terminal_service.TerminalIdentityMismatchError)
        # The refusal names the stale target rather than failing generically.
        assert recorded["pane_id"] in str(refusal)

    def test_the_row_is_demoted_by_its_own_identity_not_by_name(
        self,
        isolated_memory_db,
        tmux_server: TmuxServer,
        tmux_backend: TmuxBackend,
        tmp_path: Path,
    ):
        """A live window wearing the name does not keep the dead row alive."""
        client = TmuxClient()
        name = f"cao-stale-{uuid.uuid4().hex[:6]}"
        window = "worker-beta"
        terminal_id = uuid.uuid4().hex[:8]

        original = _spawn_recorder(tmux_server, tmp_path, name, window)
        recorded = _register(terminal_id, original, client)
        tmux_server.run("kill-session", "-t", f"={name}", check=False)
        impostor = _spawn_recorder(tmux_server, tmp_path, name, window)
        live = client.window_identity(name, window)
        assert live is not None and live["pane_id"] != recorded["pane_id"]

        with pytest.raises(terminal_service.TerminalIdentityMismatchError):
            terminal_service.send_input(terminal_id, PAYLOAD)

        after = database.get_terminal_metadata(terminal_id)
        assert after["lifecycle_state"] == terminal_service.LIFECYCLE_DEAD
        # The demotion records what was observed and leaves the identity
        # alone: re-pointing the row at the live impostor is precisely the
        # aliasing this whole boundary exists to prevent.
        assert after["pane_id"] == recorded["pane_id"]
        assert after["pane_id"] != live["pane_id"]
        assert impostor.received() == b""

    @pytest.mark.parametrize(
        "dropped",
        ["server_socket_path", "session_id", "window_id", "pane_pid"],
    )
    def test_a_partial_identity_fails_closed_rather_than_being_trusted(
        self,
        isolated_memory_db,
        tmux_server: TmuxServer,
        tmux_backend: TmuxBackend,
        tmp_path: Path,
        dropped: str,
    ):
        """Every field is load-bearing; none of them is inferred.

        A row missing any component is refused outright rather than
        checked on the fields it does have. The dangerous case is concrete:
        a restarted tmux server issues ``%0``/``%1`` again, so a row that
        recorded a pane id without its server resolves, after the restart,
        to a different live pane at the same id — and a boundary that
        checked "the fields we have" would pass it.
        """
        client = TmuxClient()
        name = f"cao-stale-{uuid.uuid4().hex[:6]}"
        window = "worker-partial"
        terminal_id = uuid.uuid4().hex[:8]

        pane = _spawn_recorder(tmux_server, tmp_path, name, window)
        identity = client.window_identity(pane.session, pane.window)
        assert identity is not None
        fields = {
            "pane_id": identity["pane_id"],
            "window_id": identity["window_id"],
            "session_id": identity["session_id"],
            "pane_pid": int(identity["pane_pid"]),
            "server_socket_path": identity["server_socket_path"],
        }
        fields[dropped] = None
        database.create_terminal(
            terminal_id=terminal_id,
            tmux_session=pane.session,
            tmux_window=pane.window,
            provider="claude_code",
            **fields,
        )

        with pytest.raises(terminal_service.TerminalIdentityMismatchError, match="incomplete"):
            terminal_service.send_input(terminal_id, PAYLOAD)

        time.sleep(0.3)
        # The pane is perfectly healthy — the refusal is about the row, so
        # the live worker must be untouched rather than written to.
        assert PAYLOAD.encode() not in pane.received()
        after = database.get_terminal_metadata(terminal_id)
        assert after["lifecycle_state"] == terminal_service.LIFECYCLE_UNKNOWN_LIVENESS

    def test_a_renamed_window_is_still_the_same_terminal(
        self,
        isolated_memory_db,
        tmux_server: TmuxServer,
        tmux_backend: TmuxBackend,
        tmp_path: Path,
    ):
        """Identity survives relabelling — names are labels, not identity.

        The counterpart to the refusal above. A boundary that demoted on a
        name mismatch would reap live workers for the ordinary act of
        renaming their own window, which is worse than the defect it was
        meant to fix.
        """
        client = TmuxClient()
        name = f"cao-stale-{uuid.uuid4().hex[:6]}"
        window = "worker-gamma"
        terminal_id = uuid.uuid4().hex[:8]

        pane = _spawn_recorder(tmux_server, tmp_path, name, window)
        recorded = _register(terminal_id, pane, client)
        tmux_server.run("rename-window", "-t", recorded["window_id"], "renamed-by-worker")

        terminal_service.send_input(terminal_id, PAYLOAD)

        end = time.monotonic() + 5.0
        while time.monotonic() < end and PAYLOAD.encode() not in pane.received():
            time.sleep(0.05)
        assert PAYLOAD.encode() in pane.received(), "the renamed pane did not receive its message"
        after = database.get_terminal_metadata(terminal_id)
        assert after["lifecycle_state"] == terminal_service.LIFECYCLE_LIVE
