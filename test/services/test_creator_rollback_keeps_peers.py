"""A failed create that made its session must not kill the peers in it (#1277).

On a non-headless ``cao launch`` the supervisor's row and session are committed
before ``provider.initialize()`` runs, so the pane is live, has
``CAO_TERMINAL_ID`` and can ``assign`` workers into its own session while CAO
still counts it as initializing. When that initialization then failed, the
rollback killed the WHOLE session (``created_session=True``), taking every worker
that had joined it with it, and herdr's ``workspace.closed`` reconciliation then
deleted their rows as ``pane_closed``.

These tests drive the real ``create_terminal`` and rollback helpers against the
in-memory tmux backend and the real SQLite registry from
``test_session_teardown_atomic``. The backend is reached only through
``get_backend()``, the same seam the herdr backend sits behind, so the rule the
tests pin is the backend-independent one.
"""

import asyncio
from test.services.test_session_teardown_atomic import (  # noqa: F401 -- fixtures
    FakeTmuxBackend,
    _reset_backend,
    real_db,
    runtime,
)
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_env, terminal_service

SESSION = "cao-1277"
INIT_TIMEOUT = "Claude Code initialization timed out after 60s"


def _install_provider(monkeypatch, rt, *, creator_init):
    """Patch ``create_provider`` so the FIRST terminal created runs ``creator_init``.

    Every later terminal (the peers) initializes successfully. ``creator_init``
    is awaited inside the creator's ``initialize()``, i.e. while the creator's
    row and session are committed but its create has not returned: the window in
    which a live supervisor pane assigns workers into its own session.
    """
    calls = {"n": 0}

    def _create_provider(provider, terminal_id, *args, **kwargs):
        rt.status_buffers.add(terminal_id)
        rt.providers.add(terminal_id)
        stub = MagicMock()
        stub.shell_baseline = None
        calls["n"] += 1
        is_creator = calls["n"] == 1

        async def _init():
            if is_creator:
                await creator_init()
            return None

        stub.initialize = _init
        return stub

    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", _create_provider)


async def _create(**kwargs):
    return await terminal_service.create_terminal(
        provider="claude_code", agent_profile="developer", **kwargs
    )


def _rows(session_name):
    return {r["id"]: r for r in database.list_terminals_by_session(session_name)}


def test_failed_creator_init_keeps_a_peer_that_joined_its_session(real_db, runtime, monkeypatch):
    """Acceptance 1: only the failed supervisor's window and row go; the worker stays.

    Before the fix the rollback ran ``kill_session`` because this call created
    the session, so the worker's window (and on herdr, its process) died with it.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    peer = {}

    async def _creator_init():
        terminal = await _create(session_name=SESSION, new_session=False)
        peer["id"] = terminal.id
        peer["window"] = terminal.name
        raise TimeoutError(INIT_TIMEOUT)

    _install_provider(monkeypatch, runtime, creator_init=_creator_init)

    with pytest.raises(TimeoutError, match="initialization timed out"):
        asyncio.run(_create(session_name=SESSION, new_session=True, env_vars={"K": "v"}))

    assert backend.kill_session_calls == 0
    assert backend.session_exists(SESSION) is True
    # The worker's window survives; the creator's window is the only one removed.
    assert backend.windows(SESSION) == {peer["window"]}
    # Only the worker's row is left, still in the incarnation it joined.
    rows = _rows(SESSION)
    assert set(rows) == {peer["id"]}
    assert rows[peer["id"]]["session_incarnation_id"] == database.get_session_incarnation(SESSION)
    assert runtime.is_fully_live(peer["id"])
    # The session lives on, so its forwarded env stays for later windows in it.
    assert session_env.get_session_env(SESSION) == {"K": "v"}


def test_failed_creator_init_alone_still_tears_the_session_down(real_db, runtime, monkeypatch):
    """Acceptance 2: with no other terminal in it, the session goes, env and all."""
    backend = FakeTmuxBackend()
    set_backend(backend)

    async def _creator_init():
        raise TimeoutError(INIT_TIMEOUT)

    _install_provider(monkeypatch, runtime, creator_init=_creator_init)

    with pytest.raises(TimeoutError, match="initialization timed out"):
        asyncio.run(_create(session_name=SESSION, new_session=True, env_vars={"K": "v"}))

    assert backend.kill_session_calls == 1
    assert backend.session_exists(SESSION) is False
    assert _rows(SESSION) == {}
    assert session_env.get_session_env(SESSION) == {}


class _JoinDuringKillBackend(FakeTmuxBackend):
    """A peer joins the session after the rollback's check, just before the kill.

    Models a terminal row written without this process's lifecycle lock (the
    cross-process gap documented on ``list_terminals_by_session``): the rollback
    saw no peer, so it kills the session, and the peer dies with it.
    """

    def __init__(self, session_name, peer_id, peer_window):
        super().__init__()
        self._join = (session_name, peer_id, peer_window)
        self.joined_record_id = None

    def kill_session(self, session_name):
        target, peer_id, peer_window = self._join
        if session_name == target and self.joined_record_id is None:
            self._sessions[session_name].add(peer_window)
            database.create_terminal(
                terminal_id=peer_id,
                tmux_session=session_name,
                tmux_window=peer_window,
                provider="claude_code",
                agent_profile="developer",
                session_incarnation_id=database.get_session_incarnation(session_name),
            )
            self.joined_record_id = database.create_launch_record(
                terminal_id=peer_id,
                session_name=session_name,
                provider="claude_code",
                target_name="developer",
                target_kind=None,
                launched_model=None,
                model_honored=None,
            )
        return super().kill_session(session_name)


def test_terminal_destroyed_by_a_creator_rollback_records_that_cause(real_db, runtime, monkeypatch):
    """Acceptance 5: the launch record says ``creator_rollback``, not ``pane_closed``."""
    backend = _JoinDuringKillBackend(SESSION, "peer1277", "w-peer1277")
    set_backend(backend)

    async def _creator_init():
        raise TimeoutError(INIT_TIMEOUT)

    _install_provider(monkeypatch, runtime, creator_init=_creator_init)

    with pytest.raises(TimeoutError, match="initialization timed out"):
        asyncio.run(_create(session_name=SESSION, new_session=True))

    assert backend.joined_record_id is not None
    assert backend.session_exists(SESSION) is False
    # The row that died with the session is gone, not left for a later
    # reconciliation to stamp as pane_closed / stale_cleanup.
    assert _rows(SESSION) == {}
    record = database.get_launch_record("peer1277")
    assert record is not None
    assert record["end_reason"] == "creator_rollback"
    assert record["ended_at"] is not None


def test_creator_rollback_is_a_recognised_end_reason():
    """The new cause is part of the closed set every finalizer validates against."""
    assert "creator_rollback" in database.LAUNCH_END_REASONS


def _seed_incarnation(backend, creator, peers):
    """Seed SESSION as a fresh incarnation made by ``creator``, joined by ``peers``.

    ``creator``/``peers`` are ``(terminal_id, window_name)`` pairs. Rows are
    written the way ``create_terminal`` writes them: the creator publishes the
    incarnation, each peer joins the current one.
    """
    backend.add_session(SESSION, {creator[1]} | {w for _, w in peers})
    incarnation = "inc-1277"
    database.create_terminal(
        terminal_id=creator[0],
        tmux_session=SESSION,
        tmux_window=creator[1],
        provider="claude_code",
        agent_profile="developer",
        session_incarnation_id=incarnation,
        new_session_incarnation=True,
    )
    for peer_id, peer_window in peers:
        database.create_terminal(
            terminal_id=peer_id,
            tmux_session=SESSION,
            tmux_window=peer_window,
            provider="claude_code",
            agent_profile="developer",
            session_incarnation_id=incarnation,
        )


def test_cancelled_creator_rollback_also_keeps_peers(real_db, runtime):
    """The cancelled-create compensator follows the same rule as the failed one."""
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed_incarnation(backend, ("c1277", "w-c1277"), [("p1277", "w-p1277")])
    session_env.set_session_env(SESSION, {"K": "v"})

    terminal_service._roll_back_cancelled_create(SESSION, "c1277", "w-c1277", created_session=True)

    assert backend.kill_session_calls == 0
    assert backend.windows(SESSION) == {"w-p1277"}
    assert set(_rows(SESSION)) == {"p1277"}
    assert session_env.get_session_env(SESSION) == {"K": "v"}


def test_window_only_rollback_clears_env_when_the_session_collapses(real_db, runtime):
    """Peers keep the session, but if it is gone after the window kill, drop the env.

    tmux drops a session with its last window. A peer row whose pane already
    exited (not yet reaped) still counts as a peer, so the rollback kills only
    the creator's window; the session then collapses and the forwarded env must
    not outlive it.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed_incarnation(backend, ("c1277", "w-c1277"), [("p1277", "w-p1277")])
    backend._sessions[SESSION].discard("w-p1277")  # the peer's pane already exited
    session_env.set_session_env(SESSION, {"K": "v"})

    terminal_service._roll_back_cancelled_create(SESSION, "c1277", "w-c1277", created_session=True)

    assert backend.kill_session_calls == 0
    assert backend.session_exists(SESSION) is False
    assert session_env.get_session_env(SESSION) == {}
    # The dead peer's row is left for the normal reconciliation, not deleted here.
    assert set(_rows(SESSION)) == {"p1277"}


def test_a_peer_in_an_older_incarnation_does_not_keep_the_session(real_db, runtime):
    """Only rows in the creator's incarnation are peers; stale same-name rows are not."""
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed_incarnation(backend, ("c1277", "w-c1277"), [])
    database.create_terminal(
        terminal_id="old1277",
        tmux_session=SESSION,
        tmux_window="w-old1277",
        provider="claude_code",
        agent_profile="developer",
        session_incarnation_id="inc-older",
    )
    session_env.set_session_env(SESSION, {"K": "v"})

    terminal_service._roll_back_cancelled_create(SESSION, "c1277", "w-c1277", created_session=True)

    assert backend.kill_session_calls == 1
    assert backend.session_exists(SESSION) is False
    assert session_env.get_session_env(SESSION) == {}
    # The older incarnation's row is not this rollback's to delete or stamp.
    assert "old1277" in _rows(SESSION)


def test_unreadable_registry_keeps_the_session_rather_than_risk_peers(
    real_db, runtime, monkeypatch
):
    """If the peer check cannot read the registry, remove only the creator's window.

    Killing possibly live workers is the irreversible mistake; a session left with
    one window fewer is not.
    """
    backend = FakeTmuxBackend()
    set_backend(backend)
    _seed_incarnation(backend, ("c1277", "w-c1277"), [("p1277", "w-p1277")])
    session_env.set_session_env(SESSION, {"K": "v"})

    def _list(session_name):
        # _still_owns_incarnation reads the creator's row through a different
        # call; only the session-wide listing fails.
        raise RuntimeError("database is locked")

    monkeypatch.setattr(terminal_service, "list_terminals_by_session", _list)

    terminal_service._roll_back_cancelled_create(SESSION, "c1277", "w-c1277", created_session=True)

    assert backend.kill_session_calls == 0
    assert backend.windows(SESSION) == {"w-p1277"}
    assert session_env.get_session_env(SESSION) == {"K": "v"}
