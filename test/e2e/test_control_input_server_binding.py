"""Acceptance for §24.7 on two live tmux servers with colliding pane ids.

A tmux pane id is unique only within one tmux server, and several servers
routinely run on one host.  ``%1`` is therefore not a target: it names one
pane on this server and a different, unrelated pane on every other one.
The failure this closes is a control write that resolved its pane id
against whichever server the process happened to reach and landed in a
stranger's composer while every liveness check stayed green.

No fake can produce that failure, because a fake has only one server.  So
this suite builds the real thing: two live tmux servers, each holding a
pane with the *same* id, and asserts across them.

What is proven here, and why in these terms:

* **Bytes, not screens.**  Each pane runs ``cat`` redirected to a file, so
  the assertion is what the pane's own process received.  A refusal that
  writes nothing must leave a zero-byte file, which is the literal form of
  the claim that ``refused`` is re-attemptable.
* **Absence is proven, never merely awaited.**  Every "nothing arrived"
  assertion is paired with a correctly-bound write that *does* arrive, so
  an empty sink means the write was refused rather than that the poll was
  too quick.
* **The refusal survives a process boundary.**  The concurrency case runs
  each write in its own interpreter, resolving tmux from ``PATH``, which
  is how the real system reaches it.

The typed wire outcomes those refusals become are asserted in the service
and endpoint suites against fakes; the seam between the two halves is
``TmuxServerIdentityError.reason_code``, which both sides name.

Every tmux invocation reachable from here carries an explicit ``-S``
selector and runs with ``TMUX`` stripped (cond-0100), and the shared
server is proven untouched after each test.

Run with: ``pytest -m e2e test/e2e/test_control_input_server_binding.py -v``
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
import sys
import time
from pathlib import Path
from test.fixtures import tmux_server as iso
from test.fixtures.tmux_server import (
    TmuxServer,
    assert_shared_server_untouched,
    isolated_tmux_server,
    shared_server_sentinel,
)
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxServerIdentityError
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_SERVER_IDENTITY_MISMATCH,
    REASON_SERVER_IDENTITY_UNBOUND,
    normalize_server_identity,
)

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

# Long enough for a local tmux round trip and a write to land, short
# enough that a genuinely refused write does not stall the suite.
_ARRIVAL_TIMEOUT = 10.0
_POLL = 0.02


class _Pane:
    """One pane on one server, and the file its process writes to.

    Holding the two together is what makes the assertions readable: the
    question is never "what does this screen show" but "what did *this*
    pane's process receive", and the answer to that is bytes in a file.
    """

    def __init__(self, server: TmuxServer, pane_id: str, sink: Path) -> None:
        self.server = server
        self.pane_id = pane_id
        self.sink = sink

    @property
    def identity(self) -> str:
        """The canonical server identity a caller binds a control to."""
        return normalize_server_identity(str(self.server.socket_path)) or ""

    def received(self) -> str:
        return self.sink.read_text() if self.sink.exists() else ""

    def await_text(self, text: str) -> str:
        deadline = time.monotonic() + _ARRIVAL_TIMEOUT
        while time.monotonic() < deadline:
            content = self.received()
            if text in content:
                return content
            time.sleep(_POLL)
        return self.received()

    def screen(self) -> str:
        return self.server.out("capture-pane", "-p", "-t", self.pane_id)


def _open_pane(server: TmuxServer, session: str) -> _Pane:
    """A session whose single pane pipes everything it is sent into a file.

    ``cat -u`` is unbuffered, so a line that reached the pane is a line
    already on disk rather than one still sitting in a stdio buffer that
    a later assertion would have to guess about.
    """
    assert server.owned_root is not None
    sink = server.owned_root / f"{session}.sink"
    pane_id = server.new_session(
        session,
        "-P",
        "-F",
        "#{pane_id}",
        "--",
        "sh",
        "-c",
        f"exec cat -u > {shlex.quote(str(sink))}",
    )
    return _Pane(server, pane_id, sink)


@contextlib.contextmanager
def _client_on(server: TmuxServer) -> Iterator[TmuxClient]:
    """A real client whose every tmux invocation carries this selector.

    The production client builds ``[tmux_binary(), ...]`` with no server
    argument of its own, which is precisely the shape that made the
    original failure possible.  Rather than pointing it at a server
    through the ambient environment — the mistake cond-0100 forbids — the
    binary it resolves is a shim that has ``-S`` already applied, so an
    unqualified invocation is not reachable from inside this test at all.
    """
    assert server.owned_root is not None
    shim = server.write_shim(server.owned_root / "bin") / "tmux"
    with patch("cli_agent_orchestrator.clients.tmux.tmux_binary", return_value=str(shim)):
        yield TmuxClient()


@pytest.fixture(scope="module")
def sentinel() -> Iterator[Tuple[TmuxServer, iso.SessionIdentity, iso.ServerIdentity]]:
    with shared_server_sentinel("cao-control-sentinel") as canary:
        yield canary


@pytest.fixture
def colliding(sentinel) -> Iterator[Tuple[_Pane, _Pane]]:
    """Two servers, each holding a pane with the same id.

    The collision is asserted rather than hoped for: without it every
    test in this file would pass while proving nothing, because a write
    aimed at the wrong server would simply fail to find its target.
    """
    with (
        isolated_tmux_server("cao-anchor-a") as first,
        isolated_tmux_server("cao-anchor-b") as second,
    ):
        left = _open_pane(first, "cao-work")
        right = _open_pane(second, "cao-work")
        assert left.pane_id == right.pane_id, (
            "the two servers minted different pane ids, so this suite would "
            f"prove nothing: {left.pane_id} vs {right.pane_id}"
        )
        assert first.socket_path != second.socket_path
        assert first.identity().pid != second.identity().pid
        yield left, right
    assert_shared_server_untouched(*sentinel)


class TestTheServersAreGenuinelyDistinct:
    """The precondition, stated as a test rather than as a comment."""

    def test_one_pane_id_names_two_different_panes(self, colliding):
        left, right = colliding
        assert left.pane_id == right.pane_id
        assert left.identity != right.identity

    def test_each_pane_reports_its_own_server(self, colliding):
        """The observation the writer boundary makes, on real servers."""
        left, right = colliding
        with _client_on(left.server) as client:
            assert client.observe_pane_server_identity(left.pane_id) == left.identity
        with _client_on(right.server) as client:
            assert client.observe_pane_server_identity(right.pane_id) == right.identity


class TestABoundWriteReachesOneServerOnly:
    """Success path: the bytes land on the named server and nowhere else."""

    def test_the_text_and_its_enter_reach_only_the_bound_pane(self, colliding):
        left, right = colliding
        with _client_on(left.server) as client:
            chunks = client.send_literal_line(
                left.pane_id, "alpha-only", submit=True, expected_server_identity=left.identity
            )

        assert chunks == 1
        # The Enter is what makes the line arrive at all: ``cat`` is in
        # canonical mode, so an unsubmitted line is still in the line
        # discipline's buffer and not yet the pane process's input.
        assert left.await_text("alpha-only").splitlines() == ["alpha-only"]
        assert right.received() == "", "the write crossed to the other server"

    def test_the_other_pane_is_untouched_on_screen_too(self, colliding):
        left, right = colliding
        with _client_on(left.server) as client:
            client.send_literal_line(
                left.pane_id, "visible-here", submit=True, expected_server_identity=left.identity
            )

        left.await_text("visible-here")
        assert "visible-here" not in right.screen()

    def test_each_server_receives_only_its_own_traffic(self, colliding):
        """Interleaved writes stay separated, in order, on both sides."""
        left, right = colliding
        for pane, lines in ((left, ("l1", "l2")), (right, ("r1", "r2"))):
            with _client_on(pane.server) as client:
                for line in lines:
                    client.send_literal_line(
                        pane.pane_id, line, submit=True, expected_server_identity=pane.identity
                    )

        assert left.await_text("l2").splitlines() == ["l1", "l2"]
        assert right.await_text("r2").splitlines() == ["r1", "r2"]


class TestAMisboundWriteWritesNothing:
    """Failure path: the refusal is zero bytes on *both* servers."""

    def test_a_pane_on_another_server_is_refused(self, colliding):
        left, right = colliding
        with _client_on(left.server) as client:
            with pytest.raises(TmuxServerIdentityError) as excinfo:
                # The §24.7 shape exactly: a correct pane id, a bound
                # server the process is not actually reaching, and the
                # same id waiting on the server it *is* reaching.
                client.send_literal_line(
                    left.pane_id,
                    "must-not-land",
                    submit=True,
                    expected_server_identity=right.identity,
                )

            assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_MISMATCH
            assert excinfo.value.bound == right.identity
            assert excinfo.value.observed == left.identity
            assert excinfo.value.chunks_sent == 0
            assert excinfo.value.enter_attempted is False

            # Absence proven, not awaited: a correctly bound write on the
            # same client is round-tripped first, so an empty sink means
            # refused rather than not-yet-arrived.
            client.send_literal_line(
                left.pane_id, "control-line", submit=True, expected_server_identity=left.identity
            )

        assert left.await_text("control-line").splitlines() == ["control-line"]
        assert "must-not-land" not in left.received()
        assert right.received() == ""

    def test_an_unbound_write_is_refused_on_a_pane_that_exists(self, colliding):
        """A live, resolvable, correct pane is still not a target.

        ``None`` is a statement — "I have no binding" — and never a
        wildcard.  Allowing it would make every legacy row, whose server
        is deliberately left null, write to whatever server the process
        happened to reach.
        """
        left, right = colliding
        with _client_on(left.server) as client:
            with pytest.raises(TmuxServerIdentityError) as excinfo:
                client.send_literal_line(
                    left.pane_id, "unbound-text", submit=True, expected_server_identity=None
                )

            assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_UNBOUND
            assert excinfo.value.chunks_sent == 0

            client.send_literal_line(
                left.pane_id, "control-line", submit=True, expected_server_identity=left.identity
            )

        assert left.await_text("control-line").splitlines() == ["control-line"]
        assert "unbound-text" not in left.received()
        assert right.received() == ""

    def test_a_pane_absent_from_the_bound_server_is_refused(self, colliding):
        """The other half of the collision: right id, wrong direction."""
        left, right = colliding
        absent = "%99999"
        with _client_on(left.server) as client:
            with pytest.raises(TmuxServerIdentityError) as excinfo:
                client.send_literal_line(
                    absent, "nowhere", submit=True, expected_server_identity=left.identity
                )

            assert excinfo.value.observed is None
            assert excinfo.value.chunks_sent == 0

        assert left.received() == ""
        assert right.received() == ""

    def test_a_long_payload_is_refused_whole(self, colliding):
        """The check precedes the *first* chunk, not each of them.

        A payload large enough to be chunked is the case where a check
        placed inside the loop would leak a prefix before refusing.
        """
        left, right = colliding
        with _client_on(left.server) as client:
            with pytest.raises(TmuxServerIdentityError) as excinfo:
                client.send_literal_line(
                    left.pane_id,
                    "z" * 3000,
                    submit=True,
                    expected_server_identity=right.identity,
                )

            assert excinfo.value.chunks_sent == 0
            client.send_literal_line(
                left.pane_id, "control-line", submit=True, expected_server_identity=left.identity
            )

        assert left.await_text("control-line").splitlines() == ["control-line"]
        assert "z" not in left.received()
        assert right.received() == ""


# Run in its own interpreter so the two writes are genuinely concurrent
# and each resolves tmux the way the real system does — from PATH, with
# the fixture's shim carrying the selector.  Prints the refusal reason
# rather than a traceback so the parent asserts on the contract value.
_CHILD = """
import sys
from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxServerIdentityError

pane_id, identity, text = sys.argv[1], sys.argv[2], sys.argv[3]
bound = None if identity == "" else identity
try:
    TmuxClient().send_literal_line(pane_id, text, submit=True, expected_server_identity=bound)
except TmuxServerIdentityError as exc:
    print(f"refused {exc.reason_code} {exc.chunks_sent}")
else:
    print("wrote")
"""


class TestConcurrentWritesStaySeparated:
    """Two servers written at the same instant do not bleed into each other."""

    def _spawn(self, pane: _Pane, identity: str, text: str) -> subprocess.Popen:
        assert pane.server.owned_root is not None
        shim_dir = pane.server.write_shim(pane.server.owned_root / "bin")
        return subprocess.Popen(
            [sys.executable, "-c", _CHILD, pane.pane_id, identity, text],
            # PATH-prefixed with the shim and stripped of TMUX: the child
            # cannot reach an unqualified server even if it tried.
            env=pane.server.subprocess_env(shim_dir),
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_simultaneous_bound_writes_land_on_their_own_servers(self, colliding):
        left, right = colliding
        children = [
            self._spawn(left, left.identity, "concurrent-left"),
            self._spawn(right, right.identity, "concurrent-right"),
        ]
        outputs = [child.communicate(timeout=60) for child in children]

        for (stdout, stderr), child in zip(outputs, children):
            assert child.returncode == 0, stderr
            assert stdout.strip() == "wrote", stderr

        assert left.await_text("concurrent-left").splitlines() == ["concurrent-left"]
        assert right.await_text("concurrent-right").splitlines() == ["concurrent-right"]

    def test_a_misbound_write_refuses_while_a_bound_one_succeeds(self, colliding):
        """The refusal is decided per call, not per process or per host."""
        left, right = colliding
        misbound = self._spawn(left, right.identity, "must-not-land")
        bound = self._spawn(right, right.identity, "concurrent-right")

        refused_out, refused_err = misbound.communicate(timeout=60)
        wrote_out, wrote_err = bound.communicate(timeout=60)

        assert refused_out.strip() == f"refused {REASON_SERVER_IDENTITY_MISMATCH} 0", refused_err
        assert wrote_out.strip() == "wrote", wrote_err
        assert right.await_text("concurrent-right").splitlines() == ["concurrent-right"]
        assert left.received() == ""
