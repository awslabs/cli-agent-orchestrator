"""Atomic session-teardown tests (caom-9k8).

These exercise the REAL ``delete_session`` reconciliation logic against:

* a faithful in-memory tmux backend that models the side effects that actually
  cause the two stores to drift — ``kill_window`` dropping the last window (and
  thus the whole session), ``kill_session`` racing tmux's own reaping, and
  ``kill_session`` failing outright, and
* a REAL SQLite registry (``clients.database`` with a per-test engine), so
  ``list_terminals_by_session`` / ``delete_terminals_by_session`` /
  ``db_delete_terminal`` run their production SQL.

Mocking ``delete_session`` itself would prove nothing for an ordering bug, so we
drive the true function and assert the invariant the bead demands: after a
SUCCESSFUL return the tmux session is provably gone AND no registry rows survive
for it; a kill that never takes is surfaced as an error, not a false success;
and a re-run reconciles a half-torn-down session.

Several tests are written to FAIL against the pre-fix implementation (which
snapshotted liveness before the terminal loop and ignored ``kill_session``'s
result) and PASS against the reconciling one.
"""

from typing import Dict, Set

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_service, terminal_service


class FakeTmuxBackend:
    """In-memory stand-in for the tmux backend modelling teardown side effects.

    State is ``session_name -> {window_name, ...}``. The behaviours that matter
    for the atomicity bug are modelled faithfully:

    * ``kill_window`` removes a window; removing a session's LAST window drops
      the whole session — exactly like tmux, so a "was it alive" snapshot taken
      before the terminal loop is stale by the time the kill would run.
    * ``kill_session`` supports two failure shapes seen in production:
        - ``kill_lag``: ``session.kill()`` returns but tmux has not finished
          reaping, so ``session_exists`` keeps reporting True for a few polls.
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

    # --- test helpers ---
    def add_session(self, session_name: str, windows: Set[str]) -> None:
        self._sessions[session_name] = set(windows)

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

    def kill_session(self, session_name: str) -> bool:
        self.kill_session_calls += 1
        if session_name not in self._sessions:
            return False
        if self._kill_fails:
            # Swallowed failure: session survives, caller (old code) never knew.
            return False
        if self._kill_lag > 0:
            # Returns "success" but tmux keeps reporting the session alive for
            # ``kill_lag`` more existence checks before it is actually reaped.
            self._pending_reap[session_name] = self._kill_lag
            return True
        self._sessions.pop(session_name, None)
        return True

    def kill_window(self, session_name: str, window_name: str) -> bool:
        self.kill_window_calls += 1
        windows = self._sessions.get(session_name)
        if not windows or window_name not in windows:
            return False
        windows.discard(window_name)
        # tmux drops a session once its last window is killed.
        if not windows:
            self._sessions.pop(session_name, None)
        return True


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
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _fast_kill_confirm(monkeypatch):
    """Shrink the kill-confirmation poll interval so tests stay fast.

    raising=False so this test module can also be run against a pre-fix
    ``session_service`` (which has no ``_KILL_CONFIRM_INTERVAL``) to confirm
    the assertions — not a missing attribute — are what fail there.
    """
    monkeypatch.setattr(session_service, "_KILL_CONFIRM_INTERVAL", 0.0, raising=False)


@pytest.fixture(autouse=True)
def _reset_backend():
    """Ensure the registry backend singleton is restored after each test."""
    yield
    from cli_agent_orchestrator.backends.registry import _backend  # noqa: F401

    set_backend(None)  # type: ignore[arg-type]


def _faithful_delete_terminal(backend, session_name):
    """Return a delete_terminal replacement modelling the real store effects.

    The production ``delete_terminal`` touches many singletons (FIFO reader,
    status monitor, provider manager, herdr inbox) irrelevant to the ordering
    bug. What matters for reconciliation is faithfully reproduced: it kills the
    terminal's tmux WINDOW via the backend and deletes its DB row — the two
    side effects that determine whether the stores agree.
    """

    def _fake(terminal_id, registry=None):
        metadata = database.get_terminal_metadata(terminal_id)
        if metadata:
            backend.kill_window(metadata["tmux_session"], metadata["tmux_window"])
        return database.delete_terminal(terminal_id)

    return _fake


def _seed(backend, session_name, terminals):
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


def test_success_leaves_no_orphan_in_either_store(real_db, monkeypatch):
    """Happy path: after delete_session, tmux session gone AND no DB rows."""
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-happy", [("t1", "w1"), ("t2", "w2")])
    monkeypatch.setattr(
        terminal_service, "delete_terminal", _faithful_delete_terminal(backend, "cao-happy")
    )

    result = session_service.delete_session("cao-happy")

    assert result == {"deleted": ["cao-happy"], "errors": []}
    assert backend.session_exists("cao-happy") is False
    assert database.list_terminals_by_session("cao-happy") == []


def test_kill_session_lag_is_confirmed_before_returning_success(real_db, monkeypatch):
    """kill_session returns before tmux reaps the session (a real race).

    The reconciling implementation polls until the session is provably gone, so
    the invariant holds THE MOMENT delete_session returns. The pre-fix code
    fired kill_session and returned immediately, so at return time the session
    was still visible — this test fails against it.
    """
    backend = FakeTmuxBackend(kill_lag=3)
    set_backend(backend)
    _seed(backend, "cao-lag", [("t1", "w1")])
    # Kill the window WITHOUT dropping the session, so the session-level
    # kill_session path (and its confirmation) is what must reap it. Model a
    # multi-window session: keep a second window alive so the loop's window
    # kills don't auto-drop the session.
    backend.add_session("cao-lag", {"w1", "keepalive"})
    monkeypatch.setattr(
        terminal_service, "delete_terminal", _faithful_delete_terminal(backend, "cao-lag")
    )

    result = session_service.delete_session("cao-lag")

    assert result == {"deleted": ["cao-lag"], "errors": []}
    # Provably gone at return time — not "eventually".
    assert backend.session_exists("cao-lag") is False
    assert database.list_terminals_by_session("cao-lag") == []
    assert backend.kill_session_calls == 1


def test_silent_kill_session_failure_is_surfaced_not_swallowed(real_db, monkeypatch):
    """kill_session fails silently (observation A) — must raise, not report success.

    Pre-fix code ignored kill_session's return and reported success while the
    tmux session lived on, orphaned. The reconciling code confirms the kill and
    raises when the session survives. Crucially it does NOT delete the leftover
    registry rows, so the state stays re-runnable rather than a permanent
    orphan.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    # Two windows so the loop's window kills don't drop the session on their
    # own; the session must be reaped by kill_session, which is broken here.
    _seed(backend, "cao-broken", [("t1", "w1")])
    backend.add_session("cao-broken", {"w1", "keepalive"})
    monkeypatch.setattr(
        terminal_service, "delete_terminal", _faithful_delete_terminal(backend, "cao-broken")
    )

    with pytest.raises(RuntimeError, match="still exists after kill_session"):
        session_service.delete_session("cao-broken")

    # The tmux session survives (the failure was real) ...
    assert backend.session_exists("cao-broken") is True


def test_rerun_reconciles_half_torn_down_session(real_db, monkeypatch):
    """delete_session is idempotent and re-runnable after a failed teardown.

    First run: kill_session is broken → raises, tmux session survives, its DB
    rows were removed by the terminal loop. Second run (kill now works): the
    post-loop liveness re-check finds the surviving session, kills it, confirms
    it gone, and the registry sweep is a safe no-op. Reconciled — no orphan.
    """
    backend = FakeTmuxBackend(kill_fails=True)
    set_backend(backend)
    _seed(backend, "cao-recover", [("t1", "w1")])
    backend.add_session("cao-recover", {"w1", "keepalive"})
    monkeypatch.setattr(
        terminal_service, "delete_terminal", _faithful_delete_terminal(backend, "cao-recover")
    )

    with pytest.raises(RuntimeError):
        session_service.delete_session("cao-recover")
    assert backend.session_exists("cao-recover") is True

    # Repair the backend (kill now succeeds) and re-run — must reconcile.
    backend._kill_fails = False

    result = session_service.delete_session("cao-recover")

    assert result == {"deleted": ["cao-recover"], "errors": []}
    assert backend.session_exists("cao-recover") is False
    assert database.list_terminals_by_session("cao-recover") == []


def test_leftover_row_from_failed_terminal_teardown_is_reconciled(real_db, monkeypatch):
    """A terminal whose teardown raised leaves a DB row; the sweep clears it.

    delete_terminal raising must not (a) abort the whole teardown, nor (b)
    leave a registry row pointing at a session that is about to be killed. The
    post-kill ``delete_terminals_by_session`` sweep reconciles it. Against the
    pre-fix code — which never swept and relied solely on per-terminal DB
    deletes — the leaked row survives, so this test fails there.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-leak", [("t1", "w1"), ("t2", "w2")])

    real_faithful = _faithful_delete_terminal(backend, "cao-leak")

    def _flaky(terminal_id, registry=None):
        if terminal_id == "t1":
            raise RuntimeError("boom during t1 teardown")
        return real_faithful(terminal_id, registry=registry)

    monkeypatch.setattr(terminal_service, "delete_terminal", _flaky)

    result = session_service.delete_session("cao-leak")

    assert result == {"deleted": ["cao-leak"], "errors": []}
    assert backend.session_exists("cao-leak") is False
    # t1's row leaked past its failed teardown; the reconciliation sweep
    # guarantees no registry row outlives the dead session.
    assert database.list_terminals_by_session("cao-leak") == []


def test_last_window_kill_drops_session_no_double_kill(real_db, monkeypatch):
    """Killing the last window drops the session; delete_session must not treat
    the (now absent) session as a lingering orphan.

    This is the stale-snapshot scenario: the pre-fix code snapshotted liveness
    BEFORE the loop. Here the loop's window kills drop the session, so a
    post-loop re-check (the fix) correctly sees it already gone and skips
    kill_session. The registry ends empty either way, but the fix avoids acting
    on stale state.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed(backend, "cao-drop", [("t1", "w1")])  # single window == whole session
    monkeypatch.setattr(
        terminal_service, "delete_terminal", _faithful_delete_terminal(backend, "cao-drop")
    )

    result = session_service.delete_session("cao-drop")

    assert result == {"deleted": ["cao-drop"], "errors": []}
    assert backend.session_exists("cao-drop") is False
    assert database.list_terminals_by_session("cao-drop") == []
    # The session was already gone after the window kill, so no session-level
    # kill was needed.
    assert backend.kill_session_calls == 0


def test_already_dead_session_is_safe_noop(real_db, monkeypatch):
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
    monkeypatch.setattr(
        terminal_service, "delete_terminal", _faithful_delete_terminal(backend, "cao-ghost")
    )

    result = session_service.delete_session("cao-ghost")

    assert result == {"deleted": ["cao-ghost"], "errors": []}
    assert backend.kill_session_calls == 0
    assert database.list_terminals_by_session("cao-ghost") == []
