"""``TmuxClient.session_exists_strict`` against a REAL tmux server (#498).

This is the primitive the whole confirm-then-dismantle teardown rests on: it must
keep "the session is gone" apart from "the lookup failed", because
``delete_session`` deletes registry rows only once tmux is provably gone, and a
lookup error reaching it as False is exactly how rows get dropped from under a
live session.

Every test here runs a real ``tmux`` on a private socket, so the classification is
pinned against the tool's actual exit statuses and messages rather than against a
mock of the layer under test. That distinction is load-bearing: the bug this file
was written for was that the previous implementation went through libtmux's
``Server.sessions`` property, which wraps its ``list-sessions`` fetch in
``try/except Exception: pass``. Under a MagicMock that looks fine; against a real
socket failure it yields an EMPTY session list, so an unanswerable lookup reports
a clean absence and the check fails OPEN. No amount of mocking finds that — only
a real transport failure does.

Transient failures are injected AT THE TMUX BOUNDARY (pointing the client at a
socket path that cannot be spoken to, unlinking the socket, or killing the server
underneath it), never by stubbing ``session_exists_strict`` or the ``cmd`` call
itself.

One constraint the injection vectors have to respect: **tmux's stderr wording is
version-dependent.** Pointing tmux at a socket path that is a regular file yields
``Socket operation on non-socket`` (ENOTSOCK) on tmux 3.7b but ``no server running
on <path>`` (ECONNREFUSED) on tmux 3.2a — the earlier version's message reads as a
confirmed absence, so tests built on that vector are red on tmux < 3.3. The
messages tmux renders as ``error connecting to <path> (<strerror>)`` are worded by
the kernel's errno instead, so they are stable across versions; ``_unreachable``
below uses one of those (ENOTDIR), and it is also privilege-independent, unlike
the permission-denied vector.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock

import pytest

from cli_agent_orchestrator.clients import tmux as tmux_module
from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxLookupError

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

_LSOF_AVAILABLE = shutil.which("lsof") is not None
requires_lsof = pytest.mark.skipif(not _LSOF_AVAILABLE, reason="lsof not installed")

# A binary name that cannot exist, for making a detector tier unavailable.
_MISSING_BINARY = "cao-no-such-binary-abc123"


def _hide_kernel_socket_table(monkeypatch):
    """Make the ladder behave as it does on a host without ``/proc/net/unix``."""
    monkeypatch.setattr(tmux_module, "_PROC_NET_UNIX", "/nonexistent/proc/net/unix")


def _pretend_to_be_macos(monkeypatch):
    """Drive the detector ladder down its macOS path from this Linux host.

    Both halves of what a Mac is, through the module's two seams:

    * ``_HOST_PLATFORM`` selects the LADDER (``_socket_table_detectors`` puts
      ``/proc/net/unix`` first on Linux and leaves it out entirely elsewhere), and
    * ``_PROC_NET_UNIX`` points somewhere nonexistent, because on a Mac it is.

    Setting only the platform would prove nothing here — this box's real
    ``/proc/net/unix`` would still answer if the ladder wrongly consulted it. With
    both set, a ladder that still leads with the kernel table gets no answer at
    all and falls through to the coarse process tier, which is precisely the
    fixup-5 regression.

    It is NOT a substitute for running the suite on a Mac: only the real platform
    proves that macOS lsof prints what this parser reads.
    """
    monkeypatch.setattr(tmux_module, "_HOST_PLATFORM", "darwin")
    _hide_kernel_socket_table(monkeypatch)


def _hide_lsof(monkeypatch):
    monkeypatch.setattr(tmux_module, "_LSOF_COMMAND", _MISSING_BINARY)


def _client_on(socket_path):
    """A TmuxClient talking to ``socket_path`` instead of the default server."""
    import libtmux

    client = TmuxClient()
    client.server = libtmux.Server(socket_path=str(socket_path))
    return client


@pytest.fixture
def short_tmpdir():
    """A scratch directory with a SHORT absolute path, removed afterwards.

    Not ``tmp_path``: pytest derives it from the test name under
    ``$TMPDIR/pytest-of-<user>/``, which on macOS already blows past the ~104-byte
    ``sockaddr_un`` limit — tmux then fails every command with "File name too
    long" and the tests measure that instead of what they mean to.
    """
    directory = Path(tempfile.mkdtemp(prefix="cao-t4-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def tmux_socket(short_tmpdir):
    """A private tmux socket path, with the server killed on the way out.

    Private socket: these tests must never see — or reap — the developer's own
    tmux sessions, and CAO's real sessions live on the default socket.
    """
    socket_path = short_tmpdir / f"s-{uuid.uuid4().hex[:6]}.sock"
    try:
        yield socket_path
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
            check=False,
        )


@pytest.fixture
def unrelated_tmux_server(short_tmpdir):
    """A live tmux server on a DIFFERENT private socket, killed on the way out.

    Present so the coarse process-table tier cannot answer ABSENT by accident:
    with some tmux process running that does not name the socket under test, that
    tier says UNKNOWN (→ fail closed → raise). Every developer machine looks like
    this, which is why the macOS regression showed up as "teardown always
    raises". Tests that must prove the SOCKET TABLE reached a verdict take this
    fixture so passing via "no tmux anywhere" is impossible.
    """
    socket_path = short_tmpdir / f"other-{uuid.uuid4().hex[:6]}.sock"
    subprocess.run(
        ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", "cao-unrelated"],
        capture_output=True,
        check=True,
    )
    try:
        yield socket_path
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"], capture_output=True, check=False
        )


def _start_session(socket_path, session_name):
    subprocess.run(
        ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", session_name],
        capture_output=True,
        check=True,
    )


def _server_pid(socket_path):
    """PID of the tmux server on ``socket_path`` (asked while it is reachable)."""
    result = subprocess.run(
        ["tmux", "-S", str(socket_path), "display-message", "-p", "#{pid}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _unreachable(directory):
    """A socket path tmux cannot speak to, with VERSION-STABLE stderr.

    The path traverses a regular file, so path resolution fails with ENOTDIR and
    tmux reports ``error connecting to <path> (Not a directory)``. The wording
    comes from ``strerror`` rather than from tmux, so unlike the non-socket vector
    it does not change between tmux versions (measured identical on 3.2a), and
    unlike the permission-denied vector it behaves the same for root.

    What matters is that it is genuinely UNANSWERABLE: a server could be running
    for all we know, we simply cannot ask.
    """
    blocker = directory / "not-a-directory"
    if not blocker.exists():
        blocker.write_text("blocks path resolution")
    return blocker / "s.sock"


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _process_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestConfirmedAnswers:
    """The two cases the check is allowed to answer."""

    def test_live_session_is_true(self, tmux_socket):
        _start_session(tmux_socket, "cao-alive")

        assert _client_on(tmux_socket).session_exists_strict("cao-alive") is True

    def test_absent_session_on_a_running_server_is_false(self, tmux_socket):
        _start_session(tmux_socket, "cao-alive")

        assert _client_on(tmux_socket).session_exists_strict("cao-absent") is False

    def test_match_is_exact_not_a_prefix(self, tmux_socket):
        """``cao-demo`` must not answer for ``cao-demo-2`` or vice versa.

        tmux's own target syntax prefix-matches by default (``-t foo`` finds
        ``foobar``), so a check built on it could report a DIFFERENT session's
        liveness — and a teardown would then refuse to dismantle a genuinely dead
        session, or worse, act on the wrong one.
        """
        _start_session(tmux_socket, "cao-demo-2")
        client = _client_on(tmux_socket)

        assert client.session_exists_strict("cao-demo-2") is True
        assert client.session_exists_strict("cao-demo") is False
        assert client.session_exists_strict("cao-demo-22") is False

    def test_no_server_at_all_is_a_confirmed_absence(self, tmux_socket):
        """A socket nothing is listening on: False, NOT an error.

        No server holds no sessions, so there is nothing a teardown could orphan.
        Classifying this as unanswerable would strand registry rows left over from
        a server that has since died — ``delete_session`` would raise forever and
        never reconcile them.
        """
        assert not os.path.exists(tmux_socket)

        assert _client_on(tmux_socket).session_exists_strict("cao-anything") is False

    def test_server_shut_down_under_us_is_a_confirmed_absence(self, tmux_socket):
        """Same, via the other message tmux uses ("no server running on ...").

        Reached when the socket FILE outlives its server, which is what a
        ``kill-server`` leaves behind — the ordinary end state of a CAO session,
        so this must not be an error either.
        """
        _start_session(tmux_socket, "cao-alive")
        client = _client_on(tmux_socket)
        server_pid = _server_pid(tmux_socket)
        assert client.session_exists_strict("cao-alive") is True

        subprocess.run(
            ["tmux", "-S", str(tmux_socket), "kill-server"], capture_output=True, check=True
        )
        # kill-server returns once the kill is delivered, not once the server is
        # gone. A list-sessions that connects while it is still tearing down is
        # answered "server exited unexpectedly", which is neither of the markers
        # this vector is meant to exercise, so the lookup fails closed and the
        # test flakes. Wait for the process the way every other kill here does.
        assert _wait_until(lambda: not _process_is_alive(server_pid))

        assert client.session_exists_strict("cao-alive") is False


class TestUnanswerableLookupsFailClosed:
    """The case that must NOT be answered — the point of the whole method.

    Each injects a real transport failure and asserts ``TmuxLookupError``. Before
    #498's fix these all returned False ("session gone"), which is what let a
    teardown drop the registry rows of a session that was still running.
    """

    def test_socket_path_cannot_be_resolved(self, short_tmpdir):
        """tmux: "error connecting to ... (Not a directory)" — cannot tell, raise."""
        with pytest.raises(TmuxLookupError, match="could not determine whether"):
            _client_on(_unreachable(short_tmpdir)).session_exists_strict("cao-demo")

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root is not stopped by mode 000, so this vector cannot inject"
    )
    def test_socket_directory_is_unreadable(self, short_tmpdir):
        """tmux: "Permission denied" — cannot tell, must raise.

        The closest reachable analogue of the transient socket failure a loaded or
        sandboxed host produces: the path could name a live server, we simply
        cannot ask.
        """
        locked = short_tmpdir / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            with pytest.raises(TmuxLookupError, match="could not determine whether"):
                _client_on(locked / "s.sock").session_exists_strict("cao-demo")
        finally:
            os.chmod(locked, 0o755)

    def test_error_message_names_the_session_and_the_cause(self, short_tmpdir):
        """The raise has to be diagnosable — it surfaces as an HTTP 500 body."""
        with pytest.raises(TmuxLookupError) as excinfo:
            _client_on(_unreachable(short_tmpdir)).session_exists_strict("cao-demo")

        message = str(excinfo.value)
        assert "cao-demo" in message
        assert "not a directory" in message.lower()

    def test_a_live_session_is_never_reported_gone_when_the_lookup_fails(self, tmux_socket):
        """The end-to-end shape of the bug, on one real session.

        The session is REALLY running the whole time. Asked over a working socket
        the check says True; asked over a broken one it must RAISE rather than say
        False — because a False here is what authorises dismantling it.
        """
        _start_session(tmux_socket, "cao-still-running")
        assert _client_on(tmux_socket).session_exists_strict("cao-still-running") is True

        # Same session, unreachable socket path.
        with pytest.raises(TmuxLookupError):
            _client_on(_unreachable(tmux_socket.parent)).session_exists_strict("cao-still-running")

        # Still there — nothing about the failed lookup touched it.
        assert _client_on(tmux_socket).session_exists_strict("cao-still-running") is True


class TestUnlinkedSocketUnderALiveServer:
    """A missing socket path is NOT by itself proof that the session is gone.

    A tmux server keeps serving its socket after the PATH has been unlinked — the
    everyday cause is a tmp-cleaner or systemd-tmpfiles sweeping /tmp on a
    long-lived host, and the server recovers on ``kill -USR1 <pid>``. tmux then
    fails with ``error connecting to <path> (No such file or directory)``, which is
    BYTE-IDENTICAL to the message for a socket that never existed. Reading that
    message as a confirmed absence let teardown delete the registry rows and
    dismantle the FIFO/status state of a session whose agents were still running
    (#498, review finding F1).

    The two tests below are the discriminating pair: same stderr, opposite
    verdicts, decided by whether a server is actually bound to the path.
    """

    def test_unlinked_socket_with_a_live_server_raises(self, tmux_socket):
        _start_session(tmux_socket, "cao-unlinked")
        client = _client_on(tmux_socket)
        assert client.session_exists_strict("cao-unlinked") is True
        server_pid = _server_pid(tmux_socket)

        os.unlink(tmux_socket)  # exactly what a tmp-cleaner does
        try:
            assert not os.path.exists(tmux_socket)
            assert _process_is_alive(server_pid), "server died — test would prove nothing"

            with pytest.raises(TmuxLookupError, match="could not determine whether"):
                client.session_exists_strict("cao-unlinked")

            # And the session really was alive throughout: SIGUSR1 makes the
            # server recreate the socket, and it is simply there again.
            os.kill(server_pid, signal.SIGUSR1)
            assert _wait_until(lambda: os.path.exists(tmux_socket))
            assert client.session_exists_strict("cao-unlinked") is True
        finally:
            # Never leave a server behind: the fixture's kill-server cannot reach
            # one whose socket path is gone.
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGKILL)

    def test_unlinked_socket_with_a_dead_server_is_a_confirmed_absence(self, tmux_socket):
        """The other half: rows for a dead server must stay teardownable.

        Same ENOENT stderr as the test above. If this returned an error instead of
        False, ``delete_session`` would raise on every retry and registry rows
        left by a server that is long gone could never be reconciled — which is
        why the fix disambiguates rather than just failing closed on the message.
        """
        _start_session(tmux_socket, "cao-doomed")
        client = _client_on(tmux_socket)
        server_pid = _server_pid(tmux_socket)

        os.unlink(tmux_socket)
        os.kill(server_pid, signal.SIGKILL)
        assert _wait_until(lambda: not _process_is_alive(server_pid))

        assert client.session_exists_strict("cao-doomed") is False

    def test_liveness_falls_back_to_the_process_table(self, tmux_socket, monkeypatch):
        """Last resort: no socket table at all, so ``ps`` must still say ALIVE.

        Pins the bottom tier for real (not with a stubbed probe): this server was
        started with an explicit ``-S``, so the socket path is in its argv. That
        is exactly the tier's blind spot too — a server started WITHOUT ``-S``
        (every real CAO server) does not name its socket, which is why this tier
        can only ever answer ALIVE or UNKNOWN here, never ABSENT.
        """
        _hide_kernel_socket_table(monkeypatch)
        _hide_lsof(monkeypatch)
        _start_session(tmux_socket, "cao-fallback")
        client = _client_on(tmux_socket)
        server_pid = _server_pid(tmux_socket)

        os.unlink(tmux_socket)
        try:
            with pytest.raises(TmuxLookupError, match="still bound to it"):
                client.session_exists_strict("cao-fallback")
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGKILL)

    def test_undeterminable_liveness_fails_closed(
        self, tmux_socket, unrelated_tmux_server, monkeypatch
    ):
        """No socket table AND no usable process table: raise, even for a socket
        that never existed.

        Detection being unavailable is not evidence of absence. This is the one
        place the fix deliberately gives up reconciling a possibly-dead server in
        exchange for never dismantling a possibly-live one — and after fixup 6 it
        takes THREE mechanisms to be unavailable at once, where fixup 5 reached it
        on every non-Linux host.
        """
        _hide_kernel_socket_table(monkeypatch)
        _hide_lsof(monkeypatch)
        monkeypatch.setattr(tmux_module, "_tmux_process_command_lines", lambda: None)
        assert not os.path.exists(tmux_socket)

        with pytest.raises(TmuxLookupError, match="cannot be determined on this host"):
            _client_on(tmux_socket).session_exists_strict("cao-anything")


class TestPortableServerDetection:
    """Server detection must reach a VERDICT on macOS/BSD too, not just Linux.

    Fixup 5 detected "is a tmux server still bound to this unlinked path?" only
    through ``/proc/net/unix``. That file does not exist off Linux, so the tier
    answered "cannot tell" for every socket, and the (correct) fail-closed rule
    then made ``session_exists_strict`` raise UNCONDITIONALLY on macOS —
    permanently blocking teardown and shipping two red tests there, while CI
    (ubuntu-latest on every job) stayed green.

    The fix distinguishes "detection was INCONCLUSIVE" from "this detector is
    UNAVAILABLE on this platform" and gives the second case another detector:
    ``lsof -U``, which keeps reporting a socket's path after the path has been
    unlinked because it reads the process's open descriptor, not the filesystem.

    These tests run on Linux through two seams. Hiding ``_PROC_NET_UNIX``
    exercises the lsof MECHANISM (it is what a Mac has instead of a kernel table);
    ``_pretend_to_be_macos`` additionally sets ``_HOST_PLATFORM`` so the ladder
    SELECTION is exercised too. Neither is a substitute for running the suite on a
    Mac, since only the real platform proves macOS lsof prints what we parse.
    """

    def test_the_detector_ladder_is_platform_selected(self):
        """Linux prefers the kernel table; everything else must NOT be left
        with only a detector that cannot exist there."""
        linux = tmux_module._socket_table_detectors("linux")
        assert linux[0] is tmux_module._bound_unix_socket_paths_via_proc
        assert tmux_module._bound_unix_socket_paths_via_lsof in linux

        for platform_name in ("darwin", "freebsd13", "openbsd7"):
            ladder = tmux_module._socket_table_detectors(platform_name)
            assert tmux_module._bound_unix_socket_paths_via_proc not in ladder
            assert ladder == (tmux_module._bound_unix_socket_paths_via_lsof,)

    @requires_lsof
    def test_lsof_reports_the_bound_path_even_after_it_is_unlinked(
        self, tmux_socket, unrelated_tmux_server
    ):
        """The mechanism the mac path rests on, measured against real tmux.

        Same property ``/proc/net/unix`` has: bound while alive, STILL bound once
        the path is unlinked, gone once the server dies.

        Takes ``unrelated_tmux_server`` so some OTHER named socket is bound for
        the whole test. ``_bound_unix_socket_paths_via_lsof`` reports an empty
        parse as ``None`` ("cannot tell"), deliberately, so that a host with
        nothing bound cannot read as a confirmed absence. Where the socket under
        test is the only named unix socket present, which is the ordinary case in
        a container, the post-kill listing is empty and the detector answers
        ``None``, so the final ``not in`` below raises instead of evaluating.
        """
        _start_session(tmux_socket, "cao-lsof")
        server_pid = _server_pid(tmux_socket)
        try:
            assert str(tmux_socket) in tmux_module._bound_unix_socket_paths_via_lsof()

            os.unlink(tmux_socket)
            assert str(tmux_socket) in tmux_module._bound_unix_socket_paths_via_lsof()

            os.kill(server_pid, signal.SIGKILL)
            assert _wait_until(lambda: not _process_is_alive(server_pid))
            assert _wait_until(
                lambda: str(tmux_socket) not in tmux_module._bound_unix_socket_paths_via_lsof()
            )
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGKILL)

    @requires_lsof
    def test_no_server_at_all_is_confirmed_without_the_kernel_table(
        self, tmux_socket, unrelated_tmux_server, monkeypatch
    ):
        """STATE 1 on the mac path: nothing ever listened → False, teardownable.

        This is the regression, reproduced on Linux. With the kernel table hidden,
        fixup 5 fell through to the process table, saw an unrelated tmux server it
        could not attribute to this path, answered UNKNOWN and RAISED — so on
        macOS no absence could ever be confirmed and teardown could never proceed.
        """
        _hide_kernel_socket_table(monkeypatch)
        assert not os.path.exists(tmux_socket)

        assert _client_on(tmux_socket).session_exists_strict("cao-anything") is False

    @requires_lsof
    def test_dead_server_behind_an_unlinked_socket_is_confirmed_without_the_kernel_table(
        self, tmux_socket, unrelated_tmux_server, monkeypatch
    ):
        """STATE 2 on the mac path: server dead, socket unlinked → False.

        The other test fixup 5 ships red on macOS. Rows left by a server that is
        genuinely gone have to stay reconcilable, or ``delete_session`` raises on
        every retry forever.
        """
        _hide_kernel_socket_table(monkeypatch)
        _start_session(tmux_socket, "cao-doomed")
        client = _client_on(tmux_socket)
        server_pid = _server_pid(tmux_socket)

        os.unlink(tmux_socket)
        os.kill(server_pid, signal.SIGKILL)
        assert _wait_until(lambda: not _process_is_alive(server_pid))

        assert client.session_exists_strict("cao-doomed") is False

    @requires_lsof
    def test_the_macos_ladder_reaches_a_verdict(
        self, tmux_socket, unrelated_tmux_server, monkeypatch
    ):
        """The same absence, decided by the ladder the DARWIN branch selects.

        The two tests above hide the kernel table, which proves the lsof detector
        works but still reaches it as Linux's tier 2. This one also flips
        ``_HOST_PLATFORM``, so the verdict has to come from the non-Linux ladder —
        the one fixup 5 left with a single detector that cannot exist there.
        """
        _pretend_to_be_macos(monkeypatch)
        assert not os.path.exists(tmux_socket)

        assert _client_on(tmux_socket).session_exists_strict("cao-anything") is False

    @requires_lsof
    def test_live_server_behind_an_unlinked_socket_still_raises_without_the_kernel_table(
        self, tmux_socket, monkeypatch
    ):
        """STATE 3 on the mac path: the property fixup 5 won, kept.

        Making states 1 and 2 answerable off Linux must not re-open the fail-open:
        a server still serving an unlinked socket has to raise, not return False.
        """
        _hide_kernel_socket_table(monkeypatch)
        _start_session(tmux_socket, "cao-unlinked-mac")
        client = _client_on(tmux_socket)
        server_pid = _server_pid(tmux_socket)

        os.unlink(tmux_socket)
        try:
            assert _process_is_alive(server_pid), "server died — test would prove nothing"

            with pytest.raises(TmuxLookupError, match="still bound to it"):
                client.session_exists_strict("cao-unlinked-mac")
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGKILL)

    def test_lsof_is_bounded_in_time(self, monkeypatch):
        """Teardown must not hang behind lsof on a busy host or a hung mount."""
        recorded = {}

        def fake_run(argv, **kwargs):
            recorded.update(argv=argv, kwargs=kwargs)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(tmux_module.subprocess, "run", fake_run)

        # A timeout is "this detector cannot tell", never "nothing is bound".
        assert tmux_module._bound_unix_socket_paths_via_lsof() is None
        assert recorded["kwargs"]["timeout"] == tmux_module._LSOF_TIMEOUT_SECONDS
        assert 0 < tmux_module._LSOF_TIMEOUT_SECONDS <= 10
        # "-b" must not creep in: it suppresses the socket rows on Linux lsof.
        assert "-b" not in recorded["argv"]

    def test_a_missing_lsof_is_not_an_absence(self, monkeypatch):
        _hide_lsof(monkeypatch)

        assert tmux_module._bound_unix_socket_paths_via_lsof() is None

    def test_an_empty_lsof_listing_is_not_an_absence(self, monkeypatch):
        """An empty parse means lsof failed, not that no socket is bound.

        Returning an empty set here would make every missing socket path read as
        a confirmed absence — the exact fail-open shape of the original bug.
        """
        monkeypatch.setattr(
            tmux_module.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        assert tmux_module._bound_unix_socket_paths_via_lsof() is None

    def test_lsof_name_decoration_and_non_paths_are_handled(self, monkeypatch):
        """Linux lsof appends " type=STREAM"; macOS lsof does not. Both parse.

        Unnamed sockets render as a bare "type=STREAM" name and must not become
        a bound path. Field-per-line output is also what keeps a socket path
        containing a SPACE intact, which column splitting would not.
        """
        stdout = "p123\nn/tmp/with space.sock type=STREAM\nntype=STREAM\nn/tmp/plain.sock\nf7\n"
        monkeypatch.setattr(
            tmux_module.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout, "warning"),
        )

        paths = tmux_module._bound_unix_socket_paths_via_lsof()

        # Nonzero exit with a usable listing still counts: lsof exits 1 merely
        # for warning about processes it could not fully inspect.
        assert "/tmp/with space.sock" in paths
        assert "/tmp/plain.sock" in paths
        assert "type=STREAM" not in paths


class TestOnlyTmuxLookupErrorEscapes:
    """The docstring advertises exactly one exception type; keep it true.

    Both call sites (``kill_session``'s verify poll and ``delete_session``'s
    confirm step) are written against ``TmuxLookupError``, so anything else
    leaking out relies on their broader ``except Exception`` guards to stay
    fail-closed by accident (review finding F7).
    """

    def test_a_failing_tmux_invocation_is_translated(self, tmux_socket):
        client = _client_on(tmux_socket)

        with mock.patch.object(client.server, "cmd", side_effect=OSError("tmux binary gone")):
            with pytest.raises(TmuxLookupError, match="could not determine whether"):
                client.session_exists_strict("cao-demo")

    def test_the_original_cause_is_preserved(self, tmux_socket):
        client = _client_on(tmux_socket)
        cause = OSError("tmux binary gone")

        with mock.patch.object(client.server, "cmd", side_effect=cause):
            with pytest.raises(TmuxLookupError) as excinfo:
                client.session_exists_strict("cao-demo")

        assert excinfo.value.__cause__ is cause
        assert "tmux binary gone" in str(excinfo.value)


class TestLenientCheckIsUnchanged:
    """``session_exists`` must keep its fail-OPEN behavior.

    Dozens of best-effort callers (status, UI, duplicate-name guards) rely on it
    collapsing errors to False, so #498 deliberately hardened only the strict
    check. Pinned here so a later "consistency" cleanup has to be a deliberate
    choice rather than an accident.
    """

    def test_lenient_check_swallows_an_unanswerable_lookup(self, short_tmpdir):
        assert _client_on(_unreachable(short_tmpdir)).session_exists("cao-demo") is False

    def test_lenient_check_still_finds_a_live_session(self, tmux_socket):
        _start_session(tmux_socket, "cao-alive")

        assert _client_on(tmux_socket).session_exists("cao-alive") is True
