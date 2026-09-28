"""Atomic session-teardown tests (#498).

These exercise the REAL ``delete_session`` reconciliation logic against:

* a faithful in-memory tmux backend that models the side effects that actually
  cause the two stores to drift — ``kill_window`` dropping the last window (and
  thus the whole session), ``kill_session`` racing tmux's own reaping, and
  ``kill_session`` failing outright,
* a REAL SQLite registry (``clients.database`` with a per-test engine), so
  ``list_terminals_by_session`` / ``delete_terminals_by_ids`` /
  ``db_delete_terminal`` run their production SQL, and
* a ``FakeRuntime`` recording the non-row side effects (FIFO reader, status
  monitor, provider registration), so a terminal that keeps its row while its
  pipeline is gone — a zombie — is observable rather than invisible.

Mocking ``delete_session`` itself would prove nothing for an ordering bug, so we
drive the true function and assert the invariant the fix requires: after a
SUCCESSFUL return the tmux session is provably gone AND no registry rows survive
for it; a kill that never takes is surfaced as an error, not a false success,
with the session left WHOLE (rows AND runtime); and a re-run reconciles a
half-torn-down session.

The concurrency tests drive the real functions from real threads and force the
interleaving with barriers/events injected at the exact race windows, rather than
mocking the race away.
"""

import threading
import time
from typing import Dict, List, Set
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import (
    session_env,
    session_lock,
    session_service,
    terminal_service,
)

# Any thread join / lock acquire in these tests must be bounded: a self-deadlock
# or a lock never released has to FAIL the test, not hang the whole run.
DEADLOCK_TIMEOUT = 10.0


def _wait_until_lock_contended(session_name, waiters=2, timeout=DEADLOCK_TIMEOUT):
    """Block until ``waiters`` threads are registered on ``session_name``'s lock.

    This is the deterministic way to establish that a second caller is blocked,
    and it replaces the only alternative available to a test that cannot see
    inside ``lock.acquire()``: "assert it is still blocked because it had not
    finished after N seconds". That formulation is a guess about scheduling, and
    a busy machine invalidates it — which is exactly how these tests flaked in
    the full suite while passing in isolation.

    ``session_lifecycle_lock`` bumps its refcount BEFORE calling
    ``lock.acquire()``, so a count of 2 while another thread demonstrably holds
    the lock means the second caller has committed to acquiring it and cannot
    reach its critical section until the holder releases. Blocked-ness is then a
    property of the construction rather than of the clock: a slow machine only
    makes this wait longer, it cannot make the observation wrong.

    ``timeout`` is an outer safety bound only, so a thread that never arrives
    fails the test instead of hanging the run.
    """
    deadline = time.monotonic() + timeout
    while True:
        with session_lock._registry_guard:
            entry = session_lock._session_locks.get(session_name)
        registered = 0 if entry is None else entry[1]
        if registered >= waiters:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"only {registered} thread(s) registered on the lifecycle lock for "
                f"{session_name!r} after {timeout}s, expected {waiters}: the "
                "operation under test never reached the lock"
            )
        time.sleep(0.005)


class FakeTmuxBackend:
    """In-memory stand-in for the tmux backend modelling teardown side effects.

    State is ``session_name -> {window_name, ...}``. The behaviours that matter
    for the atomicity bug are modelled faithfully:

    * ``kill_window`` removes a window; removing a session's LAST window drops
      the whole session — exactly like tmux, so a "was it alive" snapshot taken
      before the terminal loop is stale by the time the kill would run.
    * ``kill_session`` removes the session AND all its windows (as tmux does),
      and mirrors the verified tmux primitive's two production failure shapes:
        - ``kill_lag``: ``session.kill()`` returns but tmux has not finished
          reaping, so ``kill_session`` polls before returning True.
        - ``kill_fails``: the kill is swallowed entirely and the session
          survives indefinitely (observation A).
    """

    def __init__(self, kill_lag: int = 0, kill_fails: bool = False) -> None:
        self._sessions: Dict[str, Set[str]] = {}
        self._kill_lag = kill_lag
        self._kill_fails = kill_fails
        self._pending_reap: Dict[str, int] = {}
        self.kill_session_calls = 0
        self.kill_window_calls = 0
        self._lock = threading.Lock()

    # --- test helpers ---
    def add_session(self, session_name: str, windows: Set[str]) -> None:
        self._sessions[session_name] = set(windows)

    def windows(self, session_name: str) -> Set[str]:
        """The live windows of ``session_name`` (empty set if it is gone).

        Lets a test assert WHICH windows survived a partial teardown, not merely
        that the session still exists.
        """
        return set(self._sessions.get(session_name, set()))

    # --- backend surface used by delete_session / terminal teardown ---
    def session_exists(self, session_name: str) -> bool:
        # Resolve a lagged kill: report alive until the lag counter drains.
        if session_name in self._pending_reap:
            remaining = self._pending_reap[session_name]
            if remaining <= 0:
                self._pending_reap.pop(session_name, None)
                self._sessions.pop(session_name, None)
                return False
            self._pending_reap[session_name] = remaining - 1
            return True
        return session_name in self._sessions

    def session_exists_strict(self, session_name: str) -> bool:
        # No transport layer in the in-memory fake, so a strict check can never
        # fail to answer: it is identical to the lenient one here. Tests that
        # need to model a lookup error subclass this and override the strict
        # check to raise (see PostLoopLookupErrorBackend).
        return self.session_exists(session_name)

    def kill_session(self, session_name: str) -> bool:
        with self._lock:
            self.kill_session_calls += 1
            if session_name not in self._sessions:
                return False
            if self._kill_fails:
                # Swallowed failure: session survives, caller (old code) never knew.
                return False
            if self._kill_lag > 0:
                # The tmux primitive now owns verification: it does not report
                # success until the lagged reap has actually completed.
                self._pending_reap[session_name] = self._kill_lag
                for _ in range(self._kill_lag + 1):
                    if not self.session_exists(session_name):
                        return True
                return False
            self._sessions.pop(session_name, None)
            return True

    def kill_window(self, session_name: str, window_name: str) -> bool:
        with self._lock:
            self.kill_window_calls += 1
            windows = self._sessions.get(session_name)
            if not windows or window_name not in windows:
                return False
            windows.discard(window_name)
            # tmux drops a session once its last window is killed.
            if not windows:
                self._sessions.pop(session_name, None)
            return True

    # --- surface touched by capture_terminal_snapshot (read-only) ---
    def get_history(self, session_name, window_name, **kwargs) -> str:
        return f"scrollback for {session_name}:{window_name}"

    def get_pane_working_directory(self, session_name, window_name) -> str:
        return "/tmp"

    def stop_pipe_pane(self, session_name, window_name) -> None:
        return None

    def supports_event_inbox(self) -> bool:
        return False

    # --- surface touched by create_terminal ---
    def create_session(self, session_name, window_name, *args, **kwargs) -> None:
        with self._lock:
            if session_name in self._sessions:
                raise RuntimeError(f"duplicate session {session_name}")
            self._sessions[session_name] = {window_name}

    def create_window(self, session_name, window_name, *args, **kwargs) -> str:
        with self._lock:
            self._sessions.setdefault(session_name, set()).add(window_name)
            return window_name

    def pipe_pane(self, session_name, window_name, target) -> None:
        return None

    def send_special_key(self, session_name, window_name, key) -> None:
        return None

    def get_pane_id(self, terminal_id, session_name, window_name) -> str:
        return f"%{terminal_id}"


class FakeRuntime:
    """Records the NON-ROW per-terminal state a teardown dismantles.

    A terminal is "live" when its row, its FIFO reader, its status-monitor
    buffers and its provider registration all exist. The zombie bug the fix
    removes was a row restored WITHOUT any of the rest, so the tests need to see
    those three independently of the DB.
    """

    def __init__(self) -> None:
        self.fifo_readers: Set[str] = set()
        self.status_buffers: Set[str] = set()
        self.providers: Set[str] = set()
        self.lock = threading.Lock()

    def register(self, terminal_id: str) -> None:
        with self.lock:
            self.fifo_readers.add(terminal_id)
            self.status_buffers.add(terminal_id)
            self.providers.add(terminal_id)

    def is_fully_live(self, terminal_id: str) -> bool:
        with self.lock:
            return (
                terminal_id in self.fifo_readers
                and terminal_id in self.status_buffers
                and terminal_id in self.providers
            )

    def is_fully_gone(self, terminal_id: str) -> bool:
        with self.lock:
            return not (
                terminal_id in self.fifo_readers
                or terminal_id in self.status_buffers
                or terminal_id in self.providers
            )


@pytest.fixture
def runtime(monkeypatch):
    """Patch the runtime singletons terminal create/teardown touch onto a recorder.

    Both directions are recorded so a create's registrations and a teardown's
    dismantling are visible to the same assertions.
    """
    rt = FakeRuntime()
    monkeypatch.setattr(
        terminal_service.fifo_manager,
        "stop_reader",
        lambda tid: rt.fifo_readers.discard(tid),
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "clear_terminal",
        lambda tid: rt.status_buffers.discard(tid),
    )
    monkeypatch.setattr(
        terminal_service.provider_manager,
        "cleanup_provider",
        lambda tid: rt.providers.discard(tid),
    )
    # Create-path stubs: real providers would launch a CLI agent.
    monkeypatch.setattr(
        terminal_service.fifo_manager,
        "create_reader",
        lambda tid, **kw: rt.fifo_readers.add(tid),
    )

    def _create_provider(provider, terminal_id, *args, **kwargs):
        rt.status_buffers.add(terminal_id)
        rt.providers.add(terminal_id)
        stub = MagicMock()
        stub.shell_baseline = None

        async def _init():
            return None

        stub.initialize = _init
        return stub

    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", _create_provider)
    return rt


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """Point ``clients.database`` at a fresh per-test SQLite registry."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cao.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    # terminal_service imports TERMINAL_LOG_DIR at module scope; redirect the
    # snapshot writes into tmp_path so tests don't touch the real log dir.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", log_dir)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_backend():
    """Ensure the registry backend singleton is restored after each test."""
    yield
    set_backend(None)  # type: ignore[arg-type]


def _seed(backend, session_name, terminals, runtime=None):
    """Create session windows + matching DB rows. ``terminals`` is a list of
    (terminal_id, window_name)."""
    backend.add_session(session_name, {w for _, w in terminals})
    for terminal_id, window_name in terminals:
        database.create_terminal(
            terminal_id=terminal_id,
            tmux_session=session_name,
            tmux_window=window_name,
            provider="claude_code",
            agent_profile="developer",
        )
        if runtime is not None:
            runtime.register(terminal_id)


def test_success_leaves_no_orphan_in_either_store(real_db, runtime):
    """Happy path: after delete_session, tmux session gone AND no DB rows."""
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-happy", [("t1", "w1"), ("t2", "w2")], runtime)

    result = session_service.delete_session("cao-happy")

    assert result == {"deleted": ["cao-happy"], "errors": []}
    assert backend.session_exists("cao-happy") is False
    assert database.list_terminals_by_session("cao-happy") == []
    # The runtime is dismantled too — no half-state left behind.
    assert runtime.is_fully_gone("t1")
    assert runtime.is_fully_gone("t2")


def test_kill_session_lag_is_confirmed_before_returning_success(real_db, runtime):
    """kill_session returns before tmux reaps the session (a real race).

    The tmux backend primitive polls until the session is provably gone, and
    delete_session trusts that verified result instead of polling a second time.
    The invariant still holds THE MOMENT delete_session returns.
    """
    backend = FakeTmuxBackend(kill_lag=3)
    set_backend(backend)
    _seed(backend, "cao-lag", [("t1", "w1")], runtime)

    result = session_service.delete_session("cao-lag")

    assert result == {"deleted": ["cao-lag"], "errors": []}
    # Provably gone at return time — not "eventually".
    assert backend.session_exists("cao-lag") is False
    assert database.list_terminals_by_session("cao-lag") == []
    assert backend.kill_session_calls == 1


def test_silent_kill_session_failure_is_surfaced_not_swallowed(real_db, runtime):
    """kill_session fails silently (observation A) — must raise, not report success.

    Pre-fix code ignored kill_session's return and reported success while the
    tmux session lived on, orphaned. The reconciling code trusts the verified
    kill result and raises when the session survives.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    _seed(backend, "cao-broken", [("t1", "w1")], runtime)

    with pytest.raises(RuntimeError, match="still exists after kill_session"):
        session_service.delete_session("cao-broken")

    # The tmux session survives (the failure was real) ...
    assert backend.session_exists("cao-broken") is True


def test_failed_kill_leaves_session_whole_not_a_zombie(real_db, runtime):
    """An unconfirmable kill must leave the session FULLY intact (#498).

    This is the zombie bug. The prior revision dismantled every terminal's
    runtime (FIFO reader, status buffers, provider) and deleted its row, then on
    a failed kill restored ONLY the row from a snapshot — leaving a registry
    entry for a terminal with no output pipeline, no status tracking and no
    provider: a row that looks live and is not. The fix defers ALL destructive
    work past the confirmation point, so a failed kill changes nothing.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    _seed(backend, "cao-zombie", [("t1", "w1"), ("t2", "w2")], runtime)

    with pytest.raises(RuntimeError, match="still exists after kill_session"):
        session_service.delete_session("cao-zombie")

    # Session alive => rows present ...
    assert backend.session_exists("cao-zombie") is True
    assert {r["id"] for r in database.list_terminals_by_session("cao-zombie")} == {"t1", "t2"}
    # ... AND the runtime behind those rows is still there. This is what the
    # snapshot/restore approach could not deliver.
    assert runtime.is_fully_live("t1")
    assert runtime.is_fully_live("t2")
    # The windows are untouched too, so the surviving session is still usable.
    assert backend.kill_window_calls == 0


def test_failed_kill_preserves_last_active(real_db, runtime):
    """The restore path was lossy: ``last_active`` did not survive it (#498, P2).

    Not deleting the row in the first place preserves every column, including the
    ones a hand-written reconstruction forgot.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    _seed(backend, "cao-lossy", [("t1", "w1")], runtime)
    before = database.get_terminal_metadata("t1")
    assert before is not None and before["last_active"] is not None

    with pytest.raises(RuntimeError):
        session_service.delete_session("cao-lossy")

    after = database.get_terminal_metadata("t1")
    assert after is not None
    assert after["last_active"] == before["last_active"]
    assert after["agent_profile"] == before["agent_profile"]
    assert after["provider"] == before["provider"]


def test_rerun_reconciles_half_torn_down_session(real_db, runtime):
    """delete_session is idempotent and re-runnable after a failed teardown.

    First run: kill_session is broken → raises, tmux session survives with its
    rows. Second run (kill now works): the liveness check finds the surviving
    session, kills it via the verified primitive, and the teardown completes.
    Reconciled — no orphan in either store.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    _seed(backend, "cao-recover", [("t1", "w1")], runtime)

    with pytest.raises(RuntimeError):
        session_service.delete_session("cao-recover")
    assert backend.session_exists("cao-recover") is True

    # Repair the backend (kill now succeeds) and re-run — must reconcile.
    backend._kill_fails = False

    result = session_service.delete_session("cao-recover")

    assert result == {"deleted": ["cao-recover"], "errors": []}
    assert backend.session_exists("cao-recover") is False
    assert database.list_terminals_by_session("cao-recover") == []
    assert runtime.is_fully_gone("t1")


def test_rerun_after_partial_runtime_teardown_is_idempotent(real_db, runtime):
    """A re-run over an already-dismantled runtime must still complete.

    Models the residue of a crash midway through phase 5: t1's runtime is
    already gone and its row already deleted, t2 is untouched, the tmux session
    still stands. Every dismantle step is idempotent, so the re-run reconciles
    the remainder rather than raising on the parts already done.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-partial", [("t1", "w1"), ("t2", "w2")], runtime)
    # Simulate the half-done state.
    terminal_service.dismantle_terminal_runtime(
        "t1", database.get_terminal_metadata("t1"), kill_window=False
    )
    database.delete_terminal("t1")
    assert runtime.is_fully_gone("t1")

    result = session_service.delete_session("cao-partial")

    assert result == {"deleted": ["cao-partial"], "errors": []}
    assert backend.session_exists("cao-partial") is False
    assert database.list_terminals_by_session("cao-partial") == []
    assert runtime.is_fully_gone("t2")


def test_leftover_row_from_failed_row_delete_is_reconciled(real_db, runtime, monkeypatch):
    """A terminal whose row deletion raises is still swept by the by-id sweep.

    ``delete_terminal_row`` raising must not (a) abort the whole teardown, nor
    (b) leave a registry row pointing at a session that is now dead. The
    post-kill ``delete_terminals_by_ids`` sweep reconciles it.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-leak", [("t1", "w1"), ("t2", "w2")], runtime)

    real_delete_row = terminal_service.delete_terminal_row

    def _flaky(terminal_id, metadata, registry=None):
        if terminal_id == "t1":
            raise RuntimeError("boom during t1 row delete")
        return real_delete_row(terminal_id, metadata, registry=registry)

    monkeypatch.setattr(terminal_service, "delete_terminal_row", _flaky)

    result = session_service.delete_session("cao-leak")

    assert result == {"deleted": ["cao-leak"], "errors": []}
    assert backend.session_exists("cao-leak") is False
    # t1's row survived its failed delete; the reconciliation sweep guarantees
    # no registry row outlives the dead session.
    assert database.list_terminals_by_session("cao-leak") == []


def test_already_dead_session_is_safe_noop(real_db, runtime):
    """Deleting an already-dead session (no tmux session, stray rows) is a safe
    no-op that still reconciles the registry — no kill, no error."""
    backend = FakeTmuxBackend()
    set_backend(backend)
    # DB row exists but the tmux session does NOT (died externally).
    database.create_terminal(
        terminal_id="t1",
        tmux_session="cao-ghost",
        tmux_window="w1",
        provider="claude_code",
        agent_profile="developer",
    )
    runtime.register("t1")

    result = session_service.delete_session("cao-ghost")

    assert result == {"deleted": ["cao-ghost"], "errors": []}
    assert backend.kill_session_calls == 0
    assert database.list_terminals_by_session("cao-ghost") == []
    assert runtime.is_fully_gone("t1")


# ── Finding 1: a lookup error during verification is not "gone" ──────────────


class PostLoopLookupErrorBackend(FakeTmuxBackend):
    """Strict existence check raises on the liveness check.

    Models a transient libtmux/socket error at exactly the moment
    ``delete_session`` checks liveness. The lenient ``session_exists`` collapses
    to False ("assume gone"); the STRICT check must surface the error so the
    teardown does not delete rows for a session it could not confirm dead (#498
    finding 1).
    """

    def session_exists_strict(self, session_name: str) -> bool:
        raise OSError("tmux socket error during liveness check")


def test_lookup_error_in_liveness_check_does_not_report_false_success(real_db, runtime):
    """A lookup error on the liveness check must raise and preserve BOTH the
    rows and the runtime — never dismantle a session that may be alive.

    On the pre-fix code the check used the lenient ``session_exists``, which
    swallows the error and returns False, so kill_session was skipped and the
    rows swept — a false success while the session may live on.
    """
    backend = PostLoopLookupErrorBackend()
    set_backend(backend)
    _seed(backend, "cao-flaky", [("t1", "w1")], runtime)

    with pytest.raises(RuntimeError, match="could not verify tmux session"):
        session_service.delete_session("cao-flaky")

    # The session may still be alive, so it must be left entirely alone.
    assert {r["id"] for r in database.list_terminals_by_session("cao-flaky")} == {"t1"}
    assert runtime.is_fully_live("t1")


class VerifyPollLookupErrorClient:
    """A TmuxClient-like object whose verification poll hits a lookup error.

    Exercises ``TmuxClient.kill_session``'s REAL verify loop (via
    ``session_exists_strict``) against a transient error: the initial lookup
    finds the session, ``session.kill()`` is dispatched, and the strict verify
    then raises a non-absence error — which must make kill_session return False,
    not a false True (#498 finding 1).
    """

    def __init__(self) -> None:
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        self._client = TmuxClient.__new__(TmuxClient)  # bypass libtmux.Server()
        self._client.server = self  # we stand in for .server.sessions.get
        self.sessions = self
        self._kill_dispatched = False
        self.killed = MagicMock()

    # server.sessions.get(...) surface
    def get(self, session_name=None, **kwargs):
        if not self._kill_dispatched:
            self._kill_dispatched = True
            session = MagicMock()
            session.kill = self.killed
            return session
        # verification poll: transient transport error (NOT absence)
        raise OSError("tmux socket error during verify poll")

    def kill_session(self, session_name):
        return self._client.kill_session(session_name)


def test_kill_session_verify_poll_lookup_error_returns_false():
    """TmuxClient.kill_session must return False when the verify poll can't tell
    whether the session is gone — the error must not read as confirmed absence
    (#498 finding 1)."""
    stub = VerifyPollLookupErrorClient()

    assert stub.kill_session("cao-x") is False
    stub.killed.assert_called_once()


# ── Finding 4: disappearance between check and kill is success, not 500 ──────


class DisappearBeforeKillBackend(FakeTmuxBackend):
    """Session is present at the strict liveness check but gone by kill_session.

    Models tmux dropping the session in the window between ``delete_session``'s
    liveness check and ``kill_session``'s own lookup. ``kill_session`` then
    returns False PER CONTRACT (base.py: False also means "not found"), which
    must be treated as success — the target is already gone (#498 finding 4).
    """

    def __init__(self) -> None:
        super().__init__()
        self._checked_once = False

    def _liveness_check(self, session_name: str) -> bool:
        # First liveness call (whichever method the code under test uses)
        # reports alive, but drops the session as a side effect — it vanishes in
        # the race window. kill_session then sees it already absent and returns
        # False per contract; the follow-up strict check confirms the absence, so
        # the fix treats it as success. Overriding BOTH check methods keeps the
        # test honest against the pre-fix code (which used lenient
        # session_exists).
        if not self._checked_once:
            self._checked_once = True
            self._sessions.pop(session_name, None)
            self._pending_reap.pop(session_name, None)
            return True
        return session_name in self._sessions

    def session_exists(self, session_name: str) -> bool:
        return self._liveness_check(session_name)

    def session_exists_strict(self, session_name: str) -> bool:
        return self._liveness_check(session_name)


def test_disappearance_between_check_and_kill_is_success_and_cleans_up(
    real_db, runtime, monkeypatch
):
    """If the session vanishes between the liveness check and the kill lookup,
    delete_session SUCCEEDS and still runs residual-row cleanup (#498 finding 4).

    Pre-fix, any False from kill_session was treated as "still exists" and the
    service raised RuntimeError (surfaced as HTTP 500), skipping cleanup — even
    though teardown had actually succeeded. The fix's strict follow-up check
    sees the confirmed absence and proceeds.
    """
    backend = DisappearBeforeKillBackend()
    set_backend(backend)
    # Two terminals; the second's row delete raises, leaving a residual row that
    # only the post-kill sweep can clear — proving cleanup ran and was NOT
    # skipped by a spurious failure.
    _seed(backend, "cao-vanish", [("t1", "w1"), ("t-leak", "w2")], runtime)

    real_delete_row = terminal_service.delete_terminal_row

    def _flaky(terminal_id, metadata, registry=None):
        if terminal_id == "t-leak":
            raise RuntimeError("boom during t-leak row delete")
        return real_delete_row(terminal_id, metadata, registry=registry)

    monkeypatch.setattr(terminal_service, "delete_terminal_row", _flaky)

    result = session_service.delete_session("cao-vanish")

    assert result == {"deleted": ["cao-vanish"], "errors": []}
    # Residual incarnation rows are cleaned up (cleanup not skipped by a
    # spurious "still exists" failure on the already-gone kill).
    assert database.list_terminals_by_session("cao-vanish") == []


# ── Finding 3: real concurrency — the lifecycle lock, driven by real threads ──
#
# These do NOT mock the race away. Each drives the production functions from
# threads and forces the interleaving with an event tripped inside a backend
# method that only the critical section calls, so the "other" operation is
# guaranteed to be in flight at the exact moment the race window is open.


def _run_threads(targets, timeout=DEADLOCK_TIMEOUT):
    """Run callables concurrently; return their results/exceptions in order.

    Joins with a timeout so a self-deadlock or a lock never released FAILS the
    test instead of hanging the run forever.
    """
    results: List = [None] * len(targets)

    def _wrap(i, fn):
        def _inner():
            try:
                results[i] = ("ok", fn())
            except BaseException as e:  # noqa: BLE001 — reported to the test
                results[i] = ("raised", e)

        return _inner

    threads = [
        threading.Thread(target=_wrap(i, fn), name=f"race-{i}", daemon=True)
        for i, fn in enumerate(targets)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    alive = [t.name for t in threads if t.is_alive()]
    assert not alive, f"threads did not finish within {timeout}s (deadlock?): {alive}"
    return results


class TeardownEntryGateBackend(FakeTmuxBackend):
    """Signals when a teardown has entered its critical section, then waits.

    ``session_exists_strict`` is called by ``delete_session`` only AFTER it has
    taken the lifecycle lock and enumerated rows. Tripping ``entered`` there and
    then blocking on ``release`` parks a teardown with the lock held and its race
    window wide open, so the other thread's attempt is guaranteed to overlap.

    ``phases`` records, in order, the backend mutations that a caller can only
    reach from INSIDE a lifecycle critical section. That turns "did the create
    interleave?" into a question about ordering, answerable exactly, instead of a
    question about how much wall clock elapsed without the create finishing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.phases: List[str] = []
        self._phases_lock = threading.Lock()
        self._gated = False

    def _record(self, phase: str) -> None:
        with self._phases_lock:
            self.phases.append(phase)

    def session_exists_strict(self, session_name: str) -> bool:
        if not self._gated:
            self._gated = True
            self._record("teardown-enter")
            self.entered.set()
            assert self.release.wait(DEADLOCK_TIMEOUT), "gate never released"
        return self.session_exists(session_name)

    def kill_session(self, session_name: str) -> bool:
        killed = super().kill_session(session_name)
        self._record("teardown-kill")
        return killed

    def create_session(self, session_name, window_name, *args, **kwargs) -> None:
        super().create_session(session_name, window_name, *args, **kwargs)
        self._record("create-enter")


def _create_in_thread(session_name):
    """Run the async create_terminal from a worker thread (its own loop)."""
    import asyncio

    return asyncio.run(
        terminal_service.create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=session_name,
            new_session=True,
        )
    )


def test_create_blocked_by_in_flight_teardown_same_name(real_db, runtime):
    """Order A — teardown holds the name, a create for it must WAIT (#498 F3).

    The teardown is parked inside its critical section with the lock held and its
    race window open; a create for the same name is launched and must not
    interleave. When the teardown is released, it completes; only then does the
    create build the new incarnation. Both stores end up describing the SAME
    thing — the new session, live, with exactly its own row — and the old
    incarnation is gone from both.

    Without mutual exclusion this is the interleaving that orphans: the create
    puts a live tmux session under the name while the teardown, which already
    decided the name was dead, kills it and sweeps.
    """
    backend = TeardownEntryGateBackend()
    set_backend(backend)
    _seed(backend, "cao-race-a", [("t-old", "w-old")], runtime)

    create_started = threading.Event()
    create_finished = threading.Event()

    def _teardown():
        return session_service.delete_session("cao-race-a")

    def _create():
        assert backend.entered.wait(DEADLOCK_TIMEOUT), "teardown never entered its section"
        create_started.set()
        try:
            return _create_in_thread("cao-race-a")
        finally:
            create_finished.set()

    def _referee():
        # Wait for POSITIVE evidence that the create is parked on the lifecycle
        # lock the teardown is holding, then release the teardown. The create is
        # committed to ``lock.acquire()`` at that point, so it provably cannot
        # reach its critical section until the teardown's ``with`` block exits.
        try:
            assert create_started.wait(DEADLOCK_TIMEOUT), "create thread never started"
            _wait_until_lock_contended("cao-race-a")
            assert (
                not create_finished.is_set()
            ), "create completed while the teardown still held the lifecycle lock"
        finally:
            # Unconditional: a referee that died holding the gate would strand
            # the teardown and report a bogus deadlock instead of the real failure.
            backend.release.set()

    results = _run_threads([_teardown, _create, _referee])
    teardown_result, create_result, referee_result = results

    assert teardown_result[0] == "ok", f"teardown failed: {teardown_result[1]!r}"
    assert (
        referee_result[0] == "ok"
    ), f"create was not blocked by the in-flight teardown: {referee_result[1]!r}"
    assert create_result[0] == "ok", f"create failed: {create_result[1]!r}"

    # No interleaving, proven by ORDER: the create's critical section began only
    # after the teardown had entered its own and killed the old incarnation.
    # Without the lock, the create's ``create_session`` would land between
    # ``teardown-enter`` and ``teardown-kill`` — the interleaving that orphans.
    assert backend.phases == [
        "teardown-enter",
        "teardown-kill",
        "create-enter",
    ], f"create/teardown critical sections interleaved: {backend.phases}"

    new_id = create_result[1].id
    # Stores agree: the new incarnation is live in tmux and is the ONLY row.
    assert backend.session_exists("cao-race-a") is True
    assert {r["id"] for r in database.list_terminals_by_session("cao-race-a")} == {new_id}
    assert runtime.is_fully_live(new_id)
    # The old incarnation is gone from both stores.
    assert database.get_terminal_metadata("t-old") is None
    assert runtime.is_fully_gone("t-old")


class CreateEntryGateBackend(FakeTmuxBackend):
    """Signals when a CREATE has entered its critical section, then waits.

    ``create_session`` is only reached with the lifecycle lock held, so tripping
    ``entered`` there parks a create mid-transition: tmux session made, row not
    yet written. That is precisely the window in which a teardown must not be
    able to observe the name.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def create_session(self, session_name, window_name, *args, **kwargs) -> None:
        super().create_session(session_name, window_name, *args, **kwargs)
        self.entered.set()
        assert self.release.wait(DEADLOCK_TIMEOUT), "gate never released"


def test_teardown_blocked_by_in_flight_create_same_name(real_db, runtime):
    """Order B — a create holds the name, a teardown for it must WAIT (#498 F3).

    This is the interleaving id-scoping alone cannot fix. The create is parked
    with the tmux session already made but its row NOT yet written. If a teardown
    could run now it would enumerate ZERO rows, find a live tmux session, kill
    it, and return success — destroying the session the create is still building
    while the create's row (written afterwards) survives, pointing at nothing.

    With the lock, the teardown blocks until the create is complete, then
    enumerates the NEW row and tears the new incarnation down properly: both
    stores end empty.
    """
    backend = CreateEntryGateBackend()
    set_backend(backend)

    teardown_started = threading.Event()
    teardown_finished = threading.Event()

    def _create():
        return _create_in_thread("cao-race-b")

    def _teardown():
        assert backend.entered.wait(DEADLOCK_TIMEOUT), "create never entered its section"
        teardown_started.set()
        try:
            return session_service.delete_session("cao-race-b")
        finally:
            teardown_finished.set()

    def _referee():
        # Same deterministic probe as order A, mirrored: release the create only
        # once the teardown is provably parked on the lock the create holds.
        try:
            assert teardown_started.wait(DEADLOCK_TIMEOUT), "teardown thread never started"
            _wait_until_lock_contended("cao-race-b")
            assert (
                not teardown_finished.is_set()
            ), "teardown completed while the create still held the lifecycle lock"
        finally:
            # Unconditional, for the same reason as order A.
            backend.release.set()

    results = _run_threads([_create, _teardown, _referee])
    create_result, teardown_result, referee_result = results

    assert create_result[0] == "ok", f"create failed: {create_result[1]!r}"
    assert (
        referee_result[0] == "ok"
    ), f"teardown was not blocked by the in-flight create: {referee_result[1]!r}"
    assert teardown_result[0] == "ok", f"teardown failed: {teardown_result[1]!r}"

    new_id = create_result[1].id
    # The teardown saw the new row (it could not have run before the row was
    # written) and tore the incarnation down completely — neither store retains it.
    assert backend.session_exists("cao-race-b") is False
    assert database.list_terminals_by_session("cao-race-b") == []
    assert database.get_terminal_metadata(new_id) is None
    assert runtime.is_fully_gone(new_id)


class OverlapDetectingBackend(FakeTmuxBackend):
    """Detects two teardowns of the same name inside their critical sections.

    Every teardown passes through ``session_exists_strict`` while holding the
    lifecycle lock. Marking the name busy there and unmarking it on the way out
    records whether any two critical sections for the SAME name were ever
    concurrent. Under mutual exclusion this must never happen.

    The FIRST entrant holds its section open until the other teardown is provably
    registered on the lifecycle lock — a positive signal that it has arrived and
    is committed to acquiring, rather than a guess that it probably has by now —
    and then dwells briefly so that an implementation WITHOUT mutual exclusion,
    which would keep walking straight into this method, is actually seen.

    That dwell is a sensitivity aid ONLY, and is deliberately not load-bearing:
    making it too short can merely cause the detector to MISS a broken
    implementation, never to fail a correct one. The proof that the two teardowns
    serialized does not rest on it — see ``enumerated`` in the test.
    """

    SENSITIVITY_DWELL = 0.05

    def __init__(self) -> None:
        super().__init__()
        self._busy: Set[str] = set()
        self._busy_lock = threading.Lock()
        self.overlaps: List[str] = []
        self._held_open = False

    def session_exists_strict(self, session_name: str) -> bool:
        with self._busy_lock:
            if session_name in self._busy:
                self.overlaps.append(session_name)
            self._busy.add(session_name)
            hold_open, self._held_open = not self._held_open, True
        try:
            if hold_open:
                _wait_until_lock_contended(session_name)
                time.sleep(self.SENSITIVITY_DWELL)
            return self.session_exists(session_name)
        finally:
            with self._busy_lock:
                self._busy.discard(session_name)


def test_two_concurrent_teardowns_same_name_do_not_overlap(real_db, runtime, monkeypatch):
    """Two teardowns of the same name must SERIALIZE, not interleave (#498 F3).

    Without the lock both enumerate the same rows, both act on the same live
    session, and both sweep — the second racing the first's kill and row
    deletion. The overlap detector asserts the critical sections never coexist:
    the second waits, then finds the session already gone and the rows already
    swept and completes as a no-op.

    Both calls must succeed — tearing down an already-dead session is not an
    error — and neither store may retain anything.
    """
    backend = OverlapDetectingBackend()
    set_backend(backend)
    _seed(backend, "cao-double", [("t1", "w1"), ("t2", "w2")], runtime)

    # What each teardown enumerated at the TOP of its critical section. This is
    # the timing-free proof that the two serialized: row enumeration and row
    # deletion both live inside the critical section, so if the sections cannot
    # coexist then whichever teardown runs second is guaranteed to find the
    # registry already swept. Seeing the same rows twice means they overlapped.
    enumerated: List[int] = []
    enumerated_lock = threading.Lock()
    real_list_terminals = session_service.list_terminals_by_session

    def _recording_list_terminals(session_name):
        rows = real_list_terminals(session_name)
        if session_name == "cao-double":
            with enumerated_lock:
                enumerated.append(len(rows))
        return rows

    monkeypatch.setattr(session_service, "list_terminals_by_session", _recording_list_terminals)

    start = threading.Barrier(2, timeout=DEADLOCK_TIMEOUT)

    def _teardown():
        start.wait()
        return session_service.delete_session("cao-double")

    results = _run_threads([_teardown, _teardown])

    for status, value in results:
        assert status == "ok", f"a teardown raised: {value!r}"
        assert value == {"deleted": ["cao-double"], "errors": []}
    assert backend.overlaps == [], (
        "two teardowns of the same session name were inside their critical "
        f"sections simultaneously: {backend.overlaps}"
    )
    # The winner saw both rows, the loser saw a registry already swept. Only
    # strict serialization produces this; an interleaving has the loser
    # enumerating rows the winner has not deleted yet.
    assert sorted(enumerated) == [0, 2], (
        "the two teardowns did not serialize — rows each enumerated inside its "
        f"critical section: {enumerated}"
    )
    # Exactly one kill reached the backend: the loser saw the session already gone.
    assert backend.kill_session_calls == 1
    assert backend.session_exists("cao-double") is False
    assert database.list_terminals_by_session("cao-double") == []
    assert runtime.is_fully_gone("t1")
    assert runtime.is_fully_gone("t2")


def test_teardowns_of_different_names_run_concurrently(real_db, runtime):
    """The lock is per NAME, not global: different names must OVERLAP (#498).

    Each teardown signals on entry to its critical section and then waits for
    the OTHER to signal before proceeding. That is only satisfiable if both hold
    their locks at the same time. A global lock would deadlock here — which the
    join timeout turns into a failure rather than a hang.
    """

    class RendezvousBackend(FakeTmuxBackend):
        def __init__(self) -> None:
            super().__init__()
            self.arrived = threading.Barrier(2, timeout=DEADLOCK_TIMEOUT)
            self._seen: Set[str] = set()

        def session_exists_strict(self, session_name: str) -> bool:
            if session_name not in self._seen:
                self._seen.add(session_name)
                # Both teardowns must be inside their critical sections at once.
                self.arrived.wait()
            return self.session_exists(session_name)

    backend = RendezvousBackend()
    set_backend(backend)
    _seed(backend, "cao-a", [("ta", "wa")], runtime)
    _seed(backend, "cao-b", [("tb", "wb")], runtime)

    results = _run_threads(
        [
            lambda: session_service.delete_session("cao-a"),
            lambda: session_service.delete_session("cao-b"),
        ]
    )

    for status, value in results:
        assert status == "ok", f"a teardown raised: {value!r}"
    assert backend.session_exists("cao-a") is False
    assert backend.session_exists("cao-b") is False
    assert database.list_terminals_by_session("cao-a") == []
    assert database.list_terminals_by_session("cao-b") == []


class RecreateOnKillBackend(FakeTmuxBackend):
    """Reuses the session name the instant the old incarnation's kill lands.

    ``kill_session`` reaps the old session and then immediately stands a NEW one
    up under the same name with its own fresh-id row — modelling a create that
    claims the freed name between the kill and the teardown's sweep. Hooking
    ``kill_session`` (rather than the sweep helper) keeps the injection point
    identical across implementations, so the test measures the sweep's SCOPING
    rather than which helper it happens to call.
    """

    def kill_session(self, session_name: str) -> bool:
        killed = super().kill_session(session_name)
        if killed and not database.get_terminal_metadata("t-new"):
            self.add_session(session_name, {"w-new"})
            database.create_terminal(
                terminal_id="t-new",
                tmux_session=session_name,
                tmux_window="w-new",
                provider="claude_code",
                agent_profile="developer",
            )
        return killed


def test_sweep_is_scoped_to_the_enumerated_incarnation(real_db, runtime):
    """The reconciliation sweep deletes ONLY the ids it enumerated (#498).

    Defence in depth behind the lifecycle lock. The lock stops an in-process
    create from interleaving, but the sweep must still be scoped by ID rather
    than by session NAME, because "every row carrying this name" is not the same
    set as "the rows this teardown decided to remove" — anything that appears
    under the name after enumeration (an out-of-band writer, a future
    multi-process topology, a retry that re-registers) belongs to a live
    incarnation this teardown knows nothing about.

    A new incarnation claims the name after enumeration (see
    ``RecreateOnKillBackend``). It must survive. Under the old
    ``delete_terminals_by_session(name)`` sweep it is destroyed — an unconditional
    delete of EVERY row for the name — while its tmux session lives on.
    """
    backend = RecreateOnKillBackend()
    set_backend(backend)
    _seed(backend, "cao-reuse", [("t-old", "w-old")], runtime)

    result = session_service.delete_session("cao-reuse")

    assert result == {"deleted": ["cao-reuse"], "errors": []}
    # The newcomer's row and tmux session both survive; only the enumerated
    # incarnation was removed.
    assert backend.session_exists("cao-reuse") is True
    assert {r["id"] for r in database.list_terminals_by_session("cao-reuse")} == {"t-new"}


def test_teardown_does_not_self_deadlock_on_the_lifecycle_lock(real_db, runtime):
    """The lock is non-reentrant, so nothing inside the critical section may
    re-acquire it. A regression that did (e.g. a nested delete_session, or a
    plugin dispatched from inside the lock that tears down the same name) would
    hang forever; the timeout makes it a FAILURE instead.

    Also proves the lock registry does not leak: after N teardowns of distinct
    names, nothing remains registered.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    for i in range(5):
        _seed(backend, f"cao-nd{i}", [(f"t{i}", f"w{i}")], runtime)

    results = _run_threads(
        [(lambda n=f"cao-nd{i}": session_service.delete_session(n)) for i in range(5)]
    )
    for status, value in results:
        assert status == "ok", f"teardown raised: {value!r}"

    # Refcounted registry drains to empty — no unbounded accumulation.
    assert session_lock._session_locks == {}

    # And the lock is genuinely re-acquirable after every release (a lock left
    # held would block here until the timeout).
    acquired = []
    for i in range(5):
        with session_lock.session_lifecycle_lock(f"cao-nd{i}"):
            acquired.append(i)
    assert acquired == [0, 1, 2, 3, 4]
    assert session_lock._session_locks == {}


def test_no_plugin_code_runs_inside_the_lifecycle_lock(real_db, runtime):
    """Plugin hooks must be dispatched only AFTER the lock is released (#498).

    Plugin code is third-party and unbounded, and on the API path it does not
    merely get scheduled: ``delete_session`` runs under ``asyncio.to_thread``, so
    ``dispatch_plugin_event`` finds no running loop and falls back to
    ``asyncio.run`` — the hook executes to completion INLINE. A hook dispatched
    from inside the critical section therefore holds the per-name lifecycle lock
    for as long as it runs, and a slow or hanging plugin stalls every subsequent
    create/teardown of that session name.

    Rather than assert on ordering (which cannot tell "after the last step" from
    "after the release"), each hook here probes the lock directly: it tries to
    acquire the very lock ``delete_session`` uses, non-blocking. Succeeding proves
    the lock was already free when the hook ran. Both event types are checked,
    because ``post_kill_terminal`` is emitted per contained terminal from what
    used to be the middle of the critical section.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-plug", [("t1", "w1"), ("t2", "w2")], runtime)

    observed: List[tuple] = []

    class ProbingRegistry:
        """A plugin registry whose hook tests whether the lock is still held."""

        async def dispatch(self, event_type, event):
            # The registry guard is only ever held for dict bookkeeping, so
            # reading the entry here is safe and non-blocking.
            entry = session_lock._session_locks.get("cao-plug")
            if entry is None:
                # Refcount drained to zero and the entry was evicted, which only
                # happens after the holder released: definitively free.
                observed.append((event_type, True))
                return
            lock, _holders = entry
            free = lock.acquire(blocking=False)
            observed.append((event_type, free))
            if free:
                lock.release()

    session_service.delete_session("cao-plug", registry=ProbingRegistry())

    kinds = [kind for kind, _ in observed]
    # Every terminal's event fired, plus the session one, and nothing was
    # swallowed by moving the dispatch out of the loop.
    assert kinds == ["post_kill_terminal", "post_kill_terminal", "post_kill_session"]
    still_locked = [kind for kind, was_free in observed if not was_free]
    assert still_locked == [], (
        "these plugin events were dispatched while the lifecycle lock for "
        f"'cao-plug' was still held: {still_locked}"
    )
    # The teardown itself still did its job.
    assert backend.session_exists("cao-plug") is False
    assert database.list_terminals_by_session("cao-plug") == []


# ── A raise in the TAIL of the critical section must not destroy the events ───


class RecordingRegistry:
    """Records every dispatched event type, in order."""

    def __init__(self) -> None:
        self.events: List[str] = []

    async def dispatch(self, event_type, event):
        self.events.append(event_type)


def _db_locked(*_args, **_kwargs):
    """Raise the SQLite error the tail sweep realistically hits.

    ``clients/database.py`` builds its engine with neither ``busy_timeout`` nor
    WAL, so with several concurrent writers (status monitor, inbox, other
    terminals) a write can exhaust sqlite3's default 5s timeout and raise.
    """
    raise OperationalError("DELETE FROM terminals", {}, Exception("database is locked"))


def test_tail_sweep_failure_does_not_lose_the_plugin_events(real_db, runtime, monkeypatch):
    """A raise in the tail must not turn a COMPLETED teardown into zero events.

    By the time the by-id sweep runs, tmux is provably gone, every runtime is
    dismantled and every row is deleted — the teardown is complete and durable.
    Before the guard, an ``OperationalError`` from that sweep propagated out of
    ``delete_session``, so the dispatch loop past the critical section was never
    reached and ALL THREE events were dropped. The per-terminal ones are then
    unrecoverable: a re-run rebuilds ``torn_down`` from rows that no longer
    exist, so it can only re-emit ``post_kill_session``.

    Pre-PR this could not happen — ``delete_terminal`` emitted
    ``post_kill_terminal`` inline per terminal, so a tail failure cost at most
    the session event. Deferring dispatch past the lock is right; its failure
    mode needed handling.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-tail", [("t1", "w1"), ("t2", "w2")], runtime)
    monkeypatch.setattr(session_service, "delete_terminals_by_ids", _db_locked)

    registry = RecordingRegistry()
    result = session_service.delete_session("cao-tail", registry=registry)

    # Every event the completed work is entitled to, in order.
    assert registry.events == ["post_kill_terminal", "post_kill_terminal", "post_kill_session"]
    # The teardown DID complete, so it must not be reported as a total failure.
    assert result["deleted"] == ["cao-tail"]
    assert backend.session_exists("cao-tail") is False
    assert database.list_terminals_by_session("cao-tail") == []
    assert runtime.is_fully_gone("t1")
    assert runtime.is_fully_gone("t2")
    # ... but the tail error is not swallowed silently either.
    assert [e["step"] for e in result["errors"]] == ["delete_terminals_by_ids"]
    assert "database is locked" in result["errors"][0]["error"]


def test_clear_session_env_failure_does_not_lose_the_plugin_events(real_db, runtime, monkeypatch):
    """Same for the other unguarded tail step, the forwarded-env drop.

    It is the last statement in the critical section, so a raise here loses the
    events for work that is entirely finished — including the row sweep.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-envfail", [("t1", "w1")], runtime)

    def _boom(session_name):
        raise RuntimeError("session env store unreadable")

    monkeypatch.setattr(session_service, "clear_session_env", _boom)

    registry = RecordingRegistry()
    result = session_service.delete_session("cao-envfail", registry=registry)

    assert registry.events == ["post_kill_terminal", "post_kill_session"]
    assert result["deleted"] == ["cao-envfail"]
    assert [e["step"] for e in result["errors"]] == ["clear_session_env"]
    assert database.list_terminals_by_session("cao-envfail") == []


def test_tail_guard_does_not_mask_an_unconfirmed_kill(real_db, runtime, monkeypatch):
    """The guard covers ONLY the post-confirmation tail.

    An unconfirmed kill must still raise with nothing dismantled and no event
    emitted — the tail steps are never even reached, so guarding them cannot
    convert a real failure into a reported success.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    _seed(backend, "cao-tailsafe", [("t1", "w1")], runtime)
    swept: List[List[str]] = []
    monkeypatch.setattr(
        session_service, "delete_terminals_by_ids", lambda ids: swept.append(list(ids))
    )

    registry = RecordingRegistry()
    with pytest.raises(RuntimeError, match="still exists after kill_session"):
        session_service.delete_session("cao-tailsafe", registry=registry)

    assert registry.events == []
    assert swept == []
    assert backend.session_exists("cao-tailsafe") is True
    assert {r["id"] for r in database.list_terminals_by_session("cao-tailsafe")} == {"t1"}
    assert runtime.is_fully_live("t1")


# --- Atomicity of the locked CREATE closure itself -------------------------
#
# The tests above pin the teardown side and the create-vs-teardown ordering. The
# two below pin the create's OWN all-or-nothing property: the closure holding the
# lifecycle lock makes the backend resource AND its registry row, and a failure
# between them must not leave the backend resource behind.


def _create_in_thread_kw(**kwargs):
    """Run the async create_terminal from a worker thread with explicit kwargs.

    Same as ``_create_in_thread`` but for the cases that need to vary
    ``new_session`` / ``env_vars``.
    """
    import asyncio

    return asyncio.run(
        terminal_service.create_terminal(
            provider="claude_code",
            agent_profile="developer",
            **kwargs,
        )
    )


def test_row_write_failure_kills_the_session_it_just_created(real_db, runtime, monkeypatch):
    """new_session=True: a mid-closure failure must not orphan the tmux session.

    The registry write is the LAST step of the locked critical section, and it is
    the one that realistically fails: ``clients/database.py`` builds its engine
    with neither ``busy_timeout`` nor WAL, so a concurrent writer makes
    "database is locked" an ordinary outcome rather than a pathological one.

    The regression this catches: the closure returns its
    ``(window_name, session_created, window_created)`` tuple only on FULL
    success, so when it raises after ``create_session`` landed, the outer
    ``session_created`` flag the ``except`` block keys its teardown off is still
    False — nothing is killed, and a live tmux session is left with no registry
    row. That is precisely the divergence #498 exists to eliminate, produced by
    the create path instead of the teardown path. Pre-#498 code set the flag
    immediately after ``create_session``, so any later failure killed it.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    monkeypatch.setattr(terminal_service, "db_create_terminal", _db_locked)

    with pytest.raises(OperationalError, match="database is locked"):
        _create_in_thread_kw(
            session_name="cao-rollback",
            new_session=True,
            env_vars={"SECRET": "s3cret"},
        )

    # The session this call created is GONE — not left running behind a failed
    # create, and not waiting on an out-of-band reconciliation to notice it.
    assert backend.session_exists("cao-rollback") is False
    assert backend.kill_session_calls == 1
    # Neither store holds anything for the name: no orphan in EITHER direction.
    assert database.list_terminals_by_session("cao-rollback") == []
    # Forwarded env is dropped with the session, so the secret cannot linger in
    # memory or bleed into a future reuse of the name.
    assert session_env.get_session_env("cao-rollback") == {}


def test_row_write_failure_kills_only_the_window_it_added(real_db, runtime, monkeypatch):
    """new_session=False: kill the added WINDOW, leave the session and its peers.

    This is the branch every MCP spawn/assign-into-an-existing-session call
    takes, and its rollback is NOT the session-level one: the session pre-existed
    this call, so tearing it down would destroy terminals the failed create never
    owned. Only the one window added under the lock may go.

    Same regression as the session branch — ``window_created`` is also assigned
    only from a successful return, so a mid-closure failure left the pane alive
    with no row: invisible to every list/tree view (the row is gone), never
    reconciled, sitting there indefinitely.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-existing", [("t-peer", "w-peer")], runtime)
    monkeypatch.setattr(terminal_service, "db_create_terminal", _db_locked)

    with pytest.raises(OperationalError, match="database is locked"):
        _create_in_thread_kw(session_name="cao-existing", new_session=False)

    # The added window is gone...
    assert backend.kill_window_calls == 1
    # ...and the pre-existing session survived with its own window untouched.
    assert backend.session_exists("cao-existing") is True
    assert backend.windows("cao-existing") == {"w-peer"}
    assert backend.kill_session_calls == 0
    # The peer terminal is untouched in both stores.
    assert {r["id"] for r in database.list_terminals_by_session("cao-existing")} == {"t-peer"}
    assert runtime.is_fully_live("t-peer")


class KeyboardInterruptOnKillBackend(FakeTmuxBackend):
    """``kill_session`` raises a BaseException instead of returning.

    Models a Ctrl-C (or a SystemExit from a shutdown signal) landing while the
    rollback is mid-kill — the one window where ``except Exception`` around the
    kill is not enough.
    """

    def kill_session(self, session_name: str) -> bool:
        self.kill_session_calls += 1
        raise KeyboardInterrupt()


def test_rollback_clears_forwarded_env_even_when_the_kill_raises_base_exception(
    real_db, runtime, monkeypatch
):
    """A BaseException out of the rollback kill must not strand the secret.

    ``_roll_back_backend_create_locked`` clears the forwarded-env mapping in a
    ``finally``, not as a following statement, for exactly this case: with two
    sequential ``try/except Exception`` blocks a ``KeyboardInterrupt``/
    ``SystemExit`` from the kill skips the clear entirely, leaving an operator's
    forwarded secret (``cao launch --env``) in the process-global map keyed to a
    session name that no longer has a live session — and which a later launch can
    reuse and silently inherit. ``finally`` still lets the BaseException
    propagate, which is required: a Ctrl-C must not be swallowed here.
    """
    backend = KeyboardInterruptOnKillBackend()
    set_backend(backend)
    monkeypatch.setattr(terminal_service, "db_create_terminal", _db_locked)

    # The KeyboardInterrupt from the rollback replaces the OperationalError that
    # triggered it, and propagates all the way out: create_terminal's own cleanup
    # `except Exception` cannot catch a BaseException either.
    with pytest.raises(KeyboardInterrupt):
        _create_in_thread_kw(
            session_name="cao-ctrl-c",
            new_session=True,
            env_vars={"SECRET": "s3cret"},
        )

    # The kill WAS attempted (so this is the interrupted-rollback path, not a
    # rollback that never ran) ...
    assert backend.kill_session_calls == 1
    # ... and the secret is gone regardless of how that attempt ended.
    assert session_env.get_session_env("cao-ctrl-c") == {}
    # The row write is what failed, so neither store should hold a row.
    assert database.list_terminals_by_session("cao-ctrl-c") == []
    # The lifecycle lock is released on the BaseException path too — a leaked
    # lock would deadlock every later create/teardown of this name, so prove a
    # subsequent acquire of the SAME name still succeeds promptly.
    assert session_lock._session_locks == {}
    acquired = threading.Event()

    def _reacquire():
        with session_lock.session_lifecycle_lock("cao-ctrl-c"):
            acquired.set()

    t = threading.Thread(target=_reacquire, daemon=True)
    t.start()
    t.join(timeout=DEADLOCK_TIMEOUT)
    assert acquired.is_set(), "lifecycle lock was not released when the rollback was interrupted"


# ---------------------------------------------------------------------------
# Cancellation during the off-thread create transaction (#498 follow-up).
#
# ``asyncio.to_thread`` cancellation cancels only the AWAITER: the worker
# thread runs on, takes the lifecycle lock, creates the backend session and
# commits the registry row — into the void. ``CancelledError`` is a
# BaseException, so ``create_terminal``'s ``except Exception`` cleanup never
# sees it, and the outer created-flags are still False so it would tear down
# nothing anyway. The result is a live session + row with no FIFO, no
# provider, and no caller that knows the terminal exists — exactly the
# divergence #498 exists to eliminate.
#
# Both tests below block the worker at a chosen point with an Event, cancel
# the awaiting task, then release the worker and require the compensating
# rollback (observed via a signalling ``db_delete_terminal``) to leave both
# stores empty. Blocked-ness and completion are established by events, never
# by the clock.
# ---------------------------------------------------------------------------


def _cancel_create_scenario(session_name, worker_blocked, release_worker, rolled_back):
    """Drive create_terminal to cancellation from its own event loop.

    Returns the exception the awaited task raised (must be CancelledError).
    The worker is released BEFORE the task is awaited: the fix holds the
    cancellation until the worker's outcome is compensated, so the task cannot
    finish while the worker is still parked — awaiting first would deadlock
    the scenario against its own gate. The payoff is the strongest possible
    assertion: by the time CancelledError propagates out of the task, the
    compensating rollback must ALREADY be durable (``rolled_back`` set), not
    merely scheduled.
    """
    import asyncio

    async def scenario():
        task = asyncio.create_task(
            terminal_service.create_terminal(
                provider="claude_code",
                agent_profile="developer",
                session_name=session_name,
                new_session=True,
            )
        )
        assert await asyncio.to_thread(
            worker_blocked.wait, DEADLOCK_TIMEOUT
        ), "create worker never reached its blocking point"
        task.cancel()
        release_worker.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), DEADLOCK_TIMEOUT)
        except BaseException as exc:  # noqa: BLE001 — the type IS the assertion
            raised = exc
        else:
            raised = None
        assert rolled_back.is_set(), (
            "CancelledError propagated before the compensating rollback was "
            "durable — the cancellation must wait for the compensation"
        )
        return raised

    return asyncio.run(scenario())


def _signalling_row_delete(monkeypatch, rolled_back):
    """Patch terminal_service.db_delete_terminal to signal after deleting."""
    real_delete = terminal_service.db_delete_terminal

    def signalling(terminal_id):
        result = real_delete(terminal_id)
        rolled_back.set()
        return result

    monkeypatch.setattr(terminal_service, "db_delete_terminal", signalling)


def test_cancelled_create_rolls_back_worker_blocked_on_lifecycle_lock(
    real_db, runtime, monkeypatch
):
    """Cancel while the worker waits for the lock — BEFORE the backend create.

    The test holds the lifecycle lock, so the worker is provably parked in
    ``lock.acquire()`` (same construction as ``_wait_until_lock_contended``).
    After cancellation the worker proceeds to build the session AND commit the
    row; the compensator must reacquire the lock and remove both.
    """
    import asyncio

    backend = FakeTmuxBackend()
    set_backend(backend)

    worker_blocked = threading.Event()
    release_worker = threading.Event()
    rolled_back = threading.Event()
    _signalling_row_delete(monkeypatch, rolled_back)

    def _hold_lock():
        with session_lock.session_lifecycle_lock("cao-cancel-lock"):
            _wait_until_lock_contended("cao-cancel-lock")
            worker_blocked.set()
            assert release_worker.wait(DEADLOCK_TIMEOUT), "scenario never released the holder"

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    try:
        raised = _cancel_create_scenario(
            "cao-cancel-lock", worker_blocked, release_worker, rolled_back
        )
    finally:
        release_worker.set()
        holder.join(timeout=DEADLOCK_TIMEOUT)

    assert isinstance(raised, asyncio.CancelledError), f"expected CancelledError, got {raised!r}"
    # Both stores empty: the void-created session and its row were compensated.
    assert backend.session_exists("cao-cancel-lock") is False
    assert database.list_terminals_by_session("cao-cancel-lock") == []
    # The name is reusable: no lock entry leaked.
    assert session_lock._session_locks == {}


def test_cancelled_create_compensation_holds_the_lifecycle_lock(real_db, runtime, monkeypatch):
    """The compensating rollback must run UNDER the lifecycle lock.

    Unlocked, its late kill could destroy a NEW incarnation of the name that a
    concurrent caller legitimately built after the void-created one — the same
    never-observable-half-built argument the create's own rollback makes. The
    proof is by construction, not by clock: the compensator is parked inside
    its ``kill_session`` while a contender is provably registered on the lock
    (the refcount observation ``_wait_until_lock_contended`` exists for), and
    the contender must not have acquired.
    """
    import asyncio

    class KillGateBackend(FakeTmuxBackend):
        def __init__(self) -> None:
            super().__init__()
            self.in_kill = threading.Event()
            self.kill_release = threading.Event()

        def kill_session(self, session_name: str) -> bool:
            self.in_kill.set()
            assert self.kill_release.wait(DEADLOCK_TIMEOUT), "referee never released the kill"
            return super().kill_session(session_name)

    backend = KillGateBackend()
    set_backend(backend)

    worker_blocked = threading.Event()
    release_worker = threading.Event()
    rolled_back = threading.Event()
    _signalling_row_delete(monkeypatch, rolled_back)

    real_create = terminal_service.db_create_terminal

    def gated_create(*args, **kwargs):
        worker_blocked.set()
        assert release_worker.wait(DEADLOCK_TIMEOUT), "scenario never released the row write"
        return real_create(*args, **kwargs)

    monkeypatch.setattr(terminal_service, "db_create_terminal", gated_create)

    contender_acquired = threading.Event()
    thread_errors: Dict[str, BaseException] = {}

    def _contend():
        try:
            assert backend.in_kill.wait(DEADLOCK_TIMEOUT), "compensator never reached its kill"
            with session_lock.session_lifecycle_lock("cao-cancel-lockheld"):
                contender_acquired.set()
        except BaseException as exc:  # noqa: BLE001 — recorded for the main thread
            thread_errors["contender"] = exc

    def _referee():
        try:
            assert backend.in_kill.wait(DEADLOCK_TIMEOUT), "compensator never reached its kill"
            # Holder (the compensator) + contender both registered: the
            # contender is committed to lock.acquire() and provably cannot be
            # inside its critical section while the compensator's kill is gated.
            _wait_until_lock_contended("cao-cancel-lockheld")
            assert (
                not contender_acquired.is_set()
            ), "compensation ran without holding the lifecycle lock"
        except BaseException as exc:  # noqa: BLE001 — recorded for the main thread
            thread_errors["referee"] = exc
        finally:
            # Unconditional: a referee that died holding the gate would strand
            # the compensator and report a bogus deadlock instead of the real
            # failure.
            backend.kill_release.set()

    contender = threading.Thread(target=_contend, daemon=True)
    referee = threading.Thread(target=_referee, daemon=True)
    contender.start()
    referee.start()
    try:
        raised = _cancel_create_scenario(
            "cao-cancel-lockheld", worker_blocked, release_worker, rolled_back
        )
    finally:
        backend.kill_release.set()
        contender.join(timeout=DEADLOCK_TIMEOUT)
        referee.join(timeout=DEADLOCK_TIMEOUT)

    assert thread_errors == {}, f"observer thread failed: {thread_errors!r}"
    assert isinstance(raised, asyncio.CancelledError), f"expected CancelledError, got {raised!r}"
    # The contender got the name once compensation finished — nothing leaked.
    assert contender_acquired.is_set(), "contender never acquired after compensation"
    assert backend.session_exists("cao-cancel-lockheld") is False
    assert database.list_terminals_by_session("cao-cancel-lockheld") == []
    assert session_lock._session_locks == {}


def test_cancelled_create_rolls_back_worker_blocked_in_row_write(real_db, runtime, monkeypatch):
    """Cancel while the worker is inside ``db_create_terminal`` — AFTER the
    backend create. The tmux session exists at cancellation time; the row
    commits after. The compensator must remove both."""
    import asyncio

    backend = FakeTmuxBackend()
    set_backend(backend)

    worker_blocked = threading.Event()
    release_worker = threading.Event()
    rolled_back = threading.Event()
    _signalling_row_delete(monkeypatch, rolled_back)

    real_create = terminal_service.db_create_terminal

    def gated_create(*args, **kwargs):
        worker_blocked.set()
        assert release_worker.wait(DEADLOCK_TIMEOUT), "scenario never released the row write"
        return real_create(*args, **kwargs)

    monkeypatch.setattr(terminal_service, "db_create_terminal", gated_create)

    raised = _cancel_create_scenario("cao-cancel-row", worker_blocked, release_worker, rolled_back)

    assert isinstance(raised, asyncio.CancelledError), f"expected CancelledError, got {raised!r}"
    # The session provably existed when the worker was parked in the row write
    # (create_session precedes db_create_terminal under the lock), so an empty
    # backend here is the compensator's doing, not a create that never ran.
    assert backend.session_exists("cao-cancel-row") is False
    assert database.list_terminals_by_session("cao-cancel-row") == []
    assert session_lock._session_locks == {}
