"""Live acceptance for the identity-bound control-input surface.

Everything else about this surface is tested against a fake tmux.  That
proves the logic; it cannot prove the bytes.  These tests run a real
``cao-server`` subprocess against a real tmux pane and read back exactly
what arrived at the pane's tty.

The pane's foreground process is a raw recorder rather than a provider
TUI, and that is deliberate rather than a convenience.  It occupies the
composer's position — it owns the tty, it is what tmux writes to — and it
does two things a real composer cannot:

* it **enables bracketed paste** (``DECSET 2004``), which is the only
  condition under which tmux emits ``ESC[200~``/``ESC[201~`` framing at
  all.  Testing against a pane that never enabled it would prove nothing,
  because no sender could leak a sentinel there;
* it records the byte stream verbatim, so the assertion is a byte
  comparison rather than a reading of rendered screen text.

That makes this the *more* hostile configuration, not a softer one.  A
companion test drives the real ``mock_cli`` REPL to show an interactive
program actually receives the line and acts on it, which is the part a
byte recorder cannot demonstrate.

Isolation: ``$HOME`` is redirected per session, so the SQLite state, the
journal and the pane locks are this run's and never the operator's; and
every tmux invocation — the fixture's own *and* the ones the server
subprocess makes for itself — carries one explicit ``-S`` socket that
this run created, with the inherited ``TMUX`` stripped from every child
environment.  The socket is the identity.  A private socket *directory*
is not: an inherited ``TMUX`` silently outranks it, which is how an
earlier version of this fixture reached the operator's shared server.
The mechanics live in ``test.fixtures.tmux_server`` and are proved in
``test/e2e/test_tmux_isolation.py``, which gates this file.

Run with: ``pytest -m e2e test/e2e/test_control_input_live.py -v``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from test.fixtures.cao_server import CaoServer, _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server, sanitized_env
from typing import Iterator, List, Optional

import pytest
import requests

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_CLI = REPO_ROOT / "test" / "providers" / "fixtures" / "bin" / "mock_cli"

TEXT = "/compact"

# ESC [ 2 0 0 ~ and its 8-bit C1 spelling.  Both are searched for as raw
# bytes: a sentinel that arrived would arrive as bytes, and a test that
# looked at decoded screen text could miss one that the terminal had
# already consumed as a mode change.
SENTINEL_START = b"\x1b[200~"
SENTINEL_END = b"\x1b[201~"
SENTINEL_C1_START = b"\xc2\x9b200~"

# Reads the tty in raw mode and appends every byte to a file, having
# first told the terminal it accepts bracketed paste.
RECORDER = r"""
import os, sys, tty
fd = sys.stdin.fileno()
tty.setraw(fd)
os.write(1, b"\x1b[?2004h")
sink = open(sys.argv[1], "ab", buffering=0)
# Announced only after raw mode and ?2004h are both in effect, so a
# sender can never be racing the pane's readiness.
with open(sys.argv[2], "w") as ready:
    ready.write("ready")
while True:
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    sink.write(chunk)
"""

# Registers an already-existing pane as a terminal, through the project's
# own database API, in the server's state root.
REGISTER = r"""
import sys
from cli_agent_orchestrator.clients.database import create_terminal
(terminal_id, session, window, pane_id, window_id, generation, provider) = sys.argv[1:8]
# Empty means "this terminal has no recorded server", which is what a row
# written before the pane id was server-qualified looks like.  It is passed
# through as None rather than as a path so the unbound case is a real
# absence in the database, not a value that happens not to match.
socket_path = sys.argv[8] or None
create_terminal(
    terminal_id=terminal_id,
    tmux_session=session,
    tmux_window=window,
    provider=provider,
    agent_profile="acceptance",
    generation=generation,
    pane_id=pane_id,
    window_id=window_id,
    server_socket_path=socket_path,
)
"""


@dataclass
class LivePane:
    """One registered terminal backed by one real pane."""

    terminal_id: str
    session: str
    pane_id: str
    window_id: str
    generation: str
    capture: Optional[Path]
    # The tmux server the pane id belongs to, as that server names itself.
    # Empty for a terminal registered without one.
    server_socket_path: str = ""

    def bytes_since(self, offset: int) -> bytes:
        if self.capture is None or not self.capture.exists():
            return b""
        with open(self.capture, "rb") as handle:
            handle.seek(offset)
            return handle.read()

    def size(self) -> int:
        if self.capture is None or not self.capture.exists():
            return 0
        return self.capture.stat().st_size


@pytest.fixture(scope="session")
def tmux_server() -> Iterator[TmuxServer]:
    """One private tmux server, named explicitly on every invocation.

    The handle is the isolation boundary in both directions: no pane
    created here is addressable from any other server, and nothing here
    can name a pane belonging to a real worker on the operator's.
    """
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    with isolated_tmux_server() as server:
        yield server


@pytest.fixture(scope="session")
def control_server(
    tmp_path_factory: pytest.TempPathFactory, tmux_server: TmuxServer
) -> Iterator[CaoServer]:
    """A real cao-server whose own tmux calls land on the private server.

    The server resolves ``tmux`` from ``PATH`` and passes no socket of
    its own, so the selector is handed to it as a shim ahead of the real
    binary on that ``PATH``.  Only ``PATH`` is overridden here: the
    managed-server fixture's other deletions (leaked auth variables, the
    inherited ``TMUX``) must survive this.
    """
    assert tmux_server.owned_root is not None
    shim = tmux_server.write_shim(tmux_server.owned_root / "bin")
    home = tmp_path_factory.mktemp("cao_home_control_input")
    server = _start_cao_server(
        home,
        _pick_free_port(),
        extra_env={"PATH": tmux_server.subprocess_env(shim)["PATH"]},
    )
    try:
        yield server
    finally:
        server.stop()


def _register(server: CaoServer, pane: LivePane, provider: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            REGISTER,
            pane.terminal_id,
            pane.session,
            "acceptance",
            pane.pane_id,
            pane.window_id,
            pane.generation,
            provider,
            pane.server_socket_path,
        ],
        env={**sanitized_env(), "HOME": str(server.home_dir)},
        check=False,
        capture_output=True,
        text=True,
    )
    # Reported rather than raised bare: a registration that fails inside a
    # fixture is otherwise a CalledProcessError with the whole script as
    # its argv and the actual cause discarded.
    assert result.returncode == 0, result.stderr or result.stdout


def _spawn(
    server: CaoServer,
    tmux: TmuxServer,
    *,
    command: List[str],
    provider: str,
    capture: Optional[Path],
    bind_server: bool = True,
) -> LivePane:
    session = f"cao-accept-{uuid.uuid4().hex[:6]}"
    # The socket is read back from the pane's own server at the moment the
    # pane exists, rather than taken from the fixture handle, because that
    # is what a real registration records: the server that answered for
    # this pane id, not the one the caller believed it was talking to.
    # Tab-separated so a socket path is never split on its own contents.
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
        " ".join(f"'{part}'" for part in command),
    )
    pane_id, window_id, socket_path = printed.split("\t")
    assert Path(socket_path).resolve() == tmux.socket_path.resolve(), (
        "the pane reports a different server than the fixture created it on: "
        f"{socket_path} vs {tmux.socket_path}"
    )
    pane = LivePane(
        terminal_id=uuid.uuid4().hex[:8],
        session=session,
        pane_id=pane_id,
        window_id=window_id,
        # Unique per pane because the column is: a shared literal
        # registers the first terminal and then fails every later one on
        # a UNIQUE constraint, which is right — a generation names one
        # incarnation of one terminal, not a test run.
        generation=f"gen-live-{uuid.uuid4().hex[:8]}",
        capture=capture,
        server_socket_path=socket_path if bind_server else "",
    )
    _register(server, pane, provider)
    return pane


@pytest.fixture
def recorder(
    control_server: CaoServer, tmux_server: TmuxServer, tmp_path: Path
) -> Iterator[LivePane]:
    """A registered terminal whose pane records the bytes it receives."""
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER)
    capture = tmp_path / "received.bin"
    capture.touch()
    ready = tmp_path / "recorder.ready"
    pane = _spawn(
        control_server,
        tmux_server,
        command=[sys.executable, str(script), str(capture), str(ready)],
        provider="mock_cli",
        capture=capture,
    )
    # Waited for rather than slept past: until the recorder has raw mode
    # and ?2004h in effect, a send would be measuring a race instead of a
    # delivery — and the sentinel control test would be meaningless.
    assert _await(ready.exists), "the recorder never signalled ready"
    yield pane
    tmux_server.kill_session(pane.session)


def _pane_alive(tmux: TmuxServer, pane: LivePane) -> bool:
    try:
        return tmux.out("display", "-p", "-t", pane.pane_id, "#{pane_dead}") == "0"
    except subprocess.CalledProcessError:
        return False


def _await(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def _send(
    server: CaoServer,
    pane: LivePane,
    *,
    control_id: Optional[str] = None,
    text: str = TEXT,
    **body,
) -> requests.Response:
    payload = {"control_id": control_id or f"ctl-{uuid.uuid4().hex[:10]}", "text": text}
    payload.update(body)
    return requests.post(
        f"{server.url}/terminals/{pane.terminal_id}/control-input",
        json=payload,
        timeout=30,
    )


def _settled(pane: LivePane, offset: int, expect: bytes) -> bytes:
    """Wait for the pane to have received ``expect``, then return the bytes."""
    _await(lambda: expect in pane.bytes_since(offset))
    return pane.bytes_since(offset)


def _assert_no_sentinels(received: bytes) -> None:
    assert SENTINEL_START not in received, received
    assert SENTINEL_END not in received, received
    assert SENTINEL_C1_START not in received, received
    assert b"\x1b" not in received, f"an escape byte reached the pane: {received!r}"


class TestLiteralDelivery:
    def test_the_exact_characters_arrive_with_an_explicit_enter(self, control_server, recorder):
        """The whole surface in one assertion: these bytes and no others."""
        start = recorder.size()
        response = _send(control_server, recorder)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "accepted", body
        assert body["text_sent"] is True
        assert body["enter_sent"] is True

        received = _settled(recorder, start, TEXT.encode())
        assert received in (b"/compact\r", b"/compact\n"), received
        _assert_no_sentinels(received)

    def test_a_pane_that_accepts_paste_framing_would_have_shown_it(
        self, control_server, tmux_server, recorder
    ):
        """Proof the previous test's silence means something.

        The same pane, pasted into with tmux's bracketed paste, receives
        the sentinels — so the recorder does have ``?2004h`` in effect
        and the byte search does find framing when framing is present.
        A negative result without this control would be unfalsifiable.
        """
        start = recorder.size()
        tmux_server.out("set-buffer", TEXT)
        tmux_server.out("paste-buffer", "-p", "-t", recorder.pane_id)

        received = _settled(recorder, start, SENTINEL_START)
        assert SENTINEL_START in received, received
        assert SENTINEL_END in received, received

    def test_a_real_repl_receives_the_line_and_submits_it(
        self, control_server, tmux_server, tmp_path
    ):
        """A byte recorder cannot show that a program *acted* on the line.

        ``mock_cli`` is an interactive REPL that answers only after a
        submitted line, so its reply is evidence the Enter was real.
        """
        if not MOCK_CLI.exists():
            pytest.skip("mock_cli fixture binary is absent")
        pane = _spawn(
            control_server,
            tmux_server,
            command=[str(MOCK_CLI)],
            provider="mock_cli",
            capture=None,
        )
        try:
            assert _await(
                lambda: "❯" in tmux_server.out("capture-pane", "-p", "-t", pane.pane_id)
            ), "the REPL never reached its prompt"
            response = _send(control_server, pane)
            assert response.json()["outcome"] == "accepted", response.text

            assert _await(
                lambda: "> MOCK: /compact"
                in tmux_server.out("capture-pane", "-p", "-t", pane.pane_id)
            ), tmux_server.out("capture-pane", "-p", "-t", pane.pane_id)
            screen = tmux_server.out("capture-pane", "-p", "-t", pane.pane_id)
            assert "200~" not in screen
            assert "201~" not in screen
        finally:
            tmux_server.kill_session(pane.session)


class TestIdentityRefusalsAgainstARealPane:
    def test_a_stale_generation_writes_nothing(self, control_server, recorder):
        start = recorder.size()
        body = _send(
            control_server, recorder, expected_identity={"terminal_generation": "gen-gone"}
        ).json()
        assert body["outcome"] == "refused"
        assert body["reason_code"] == "stale-generation"
        time.sleep(0.3)
        assert recorder.bytes_since(start) == b""

    def test_a_wrong_pane_writes_nothing(self, control_server, recorder):
        start = recorder.size()
        body = _send(control_server, recorder, expected_identity={"pane_birth_id": "%999"}).json()
        assert body["outcome"] == "refused"
        assert body["reason_code"] == "identity-mismatch"
        time.sleep(0.3)
        assert recorder.bytes_since(start) == b""

    def test_a_terminal_with_no_recorded_server_is_refused(
        self, control_server, tmux_server, tmp_path
    ):
        """A row written before pane ids were server-qualified.

        The migration adds the column but deliberately does not backfill
        it, so every terminal registered by an older binary reaches this
        path.  It has to refuse: the pane id in that row is a real pane
        id on *some* server, and the only way to guess which would be to
        ask whichever server happens to answer — which is the delivery
        to a stranger's pane this whole binding exists to prevent.

        ``refused`` rather than a hard error is the point: it is the one
        re-attemptable outcome, so a caller re-registers the terminal and
        the same control succeeds.  The bound twin below is that proof,
        and it is also what makes the empty capture below mean *refused*
        rather than *not yet*.
        """
        script = tmp_path / "recorder.py"
        script.write_text(RECORDER)

        def _pane(name: str, *, bind_server: bool) -> LivePane:
            capture = tmp_path / f"{name}.bin"
            capture.touch()
            ready = tmp_path / f"{name}.ready"
            pane = _spawn(
                control_server,
                tmux_server,
                command=[sys.executable, str(script), str(capture), str(ready)],
                provider="mock_cli",
                capture=capture,
                bind_server=bind_server,
            )
            assert _await(ready.exists), f"the {name} recorder never signalled ready"
            return pane

        unbound = _pane("unbound", bind_server=False)
        bound = _pane("bound", bind_server=True)
        try:
            start = unbound.size()
            body = _send(control_server, unbound).json()
            assert body["outcome"] == "refused", body
            assert body["reason_code"] == "server-identity-unbound", body
            assert body["chunks_sent"] in (0, None), body

            # The same request, on a pane identical but for the recorded
            # server, does arrive — so the refusal above is the binding
            # and nothing else about this pane or this text.
            assert _send(control_server, bound).json()["outcome"] == "accepted"
            _settled(bound, 0, TEXT.encode())
            assert unbound.bytes_since(start) == b"", unbound.bytes_since(start)
        finally:
            tmux_server.kill_session(unbound.session)
            tmux_server.kill_session(bound.session)

    def test_a_killed_pane_is_refused_rather_than_landing_elsewhere(
        self, control_server, tmux_server, recorder
    ):
        """The terminal record still names a pane id tmux no longer has."""
        tmux_server.out("kill-pane", "-t", recorder.pane_id)
        assert _await(lambda: not _pane_alive(tmux_server, recorder))
        body = _send(control_server, recorder).json()
        assert body["outcome"] == "refused"
        assert body["reason_code"] in {"pane-dead", "unknown-terminal"}, body


class TestAtMostOnceAgainstARealPane:
    def test_a_lost_response_is_resolved_by_asking_not_by_resending(self, control_server, recorder):
        """The at-most-once claim, measured in bytes at the pane."""
        start = recorder.size()
        control_id = f"ctl-{uuid.uuid4().hex[:10]}"
        first = _send(control_server, recorder, control_id=control_id).json()
        assert first["outcome"] == "accepted"
        _settled(recorder, start, TEXT.encode())

        looked_up = requests.get(
            f"{control_server.url}/control-input/{control_id}", timeout=30
        ).json()
        assert looked_up["outcome"] == "accepted"
        assert looked_up["request_digest"] == first["request_digest"]

        repeat = _send(control_server, recorder, control_id=control_id).json()
        assert repeat["outcome"] == "accepted"
        time.sleep(0.5)
        assert recorder.bytes_since(start).count(TEXT.encode()) == 1

    def test_concurrent_controls_never_interleave_at_the_pane(self, control_server, recorder):
        """Arbitration proved where it matters: in the byte stream.

        Two writers sharing a pane without an arbiter do not merely
        confuse an ordering — they splice each other's characters into
        one line, which a composer then submits as a single command
        nobody wrote.
        """
        start = recorder.size()
        texts = [f"/acc-{index:02d}" for index in range(8)]

        def fire(text: str) -> dict:
            return _send(control_server, recorder, text=text).json()

        with ThreadPoolExecutor(max_workers=len(texts)) as pool:
            answers = list(pool.map(fire, texts))

        accepted = [text for text, body in zip(texts, answers) if body["outcome"] == "accepted"]
        refused = [
            (text, body) for text, body in zip(texts, answers) if body["outcome"] == "refused"
        ]
        assert accepted, answers
        for _, body in refused:
            assert body["reason_code"] == "pane-busy", body

        assert _await(
            lambda: len(recorder.bytes_since(start).split(b"\r")) > len(accepted),
            timeout=10.0,
        )
        received = recorder.bytes_since(start)
        _assert_no_sentinels(received)
        lines = [line for line in received.split(b"\r") if line]
        # Each accepted control arrived whole and exactly once; nothing
        # that was refused left a fragment behind.
        assert sorted(lines) == sorted(text.encode() for text in accepted), (
            received,
            accepted,
        )
        for text, _ in refused:
            assert text.encode() not in received


class TestLiveCapabilityAdvertisement:
    def test_the_running_server_advertises_the_protocol(self, control_server):
        response = requests.get(f"{control_server.url}/control-input/capabilities", timeout=30)
        assert response.status_code == 200
        body = response.json()
        assert body["protocol"] == "cao-control-input-v1"
        assert body["bracketed_paste"] is False
        assert body["literal_write"] is True

    def test_an_unknown_terminal_is_a_typed_refusal_over_real_http(self, control_server):
        """Not a 404 — the status a caller must read as "no such surface"."""
        response = requests.post(
            f"{control_server.url}/terminals/{uuid.uuid4().hex[:8]}/control-input",
            json={"control_id": f"ctl-{uuid.uuid4().hex[:10]}", "text": TEXT},
            timeout=30,
        )
        assert response.status_code == 200
        assert response.json()["reason_code"] == "unknown-terminal"
