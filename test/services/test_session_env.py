"""Tests for the per-session forwarded-env store (issue #248).

The store is write-through: SQLite ``session_env`` is the source of truth and
the in-memory map is only a cache. Every test here runs against an isolated
per-test tmp database and a cold cache.
"""

import json
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_env
from cli_agent_orchestrator.services.session_env import (
    SessionEnvStoreError,
    clear_session_env,
    get_session_env,
    reconcile_session_env,
    set_session_env,
)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Isolate the store: fresh tmp SQLite DB + cold in-memory cache per test."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'session-env.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    _reset_cache()
    yield engine
    _reset_cache()
    engine.dispose()


def _reset_cache() -> None:
    with session_env._lock:
        session_env._session_forwarded_env.clear()


def _db_rows(engine):
    with sqlite3.connect(engine.url.database) as conn:
        return conn.execute("SELECT session_name, env_vars FROM session_env").fetchall()


def _insert_raw_row(engine, session_name: str, env_vars_payload: str) -> None:
    with sqlite3.connect(engine.url.database) as conn:
        conn.execute(
            "INSERT INTO session_env (session_name, env_vars, updated_at) VALUES (?, ?, ?)",
            (session_name, env_vars_payload, "2026-07-21T00:00:00+00:00"),
        )


def test_get_returns_empty_dict_for_unknown_session():
    assert get_session_env("cao-unknown-xyz") == {}


def test_set_and_get_roundtrip():
    set_session_env("cao-roundtrip", {"FOO": "bar", "BAZ": "qux"})
    try:
        assert get_session_env("cao-roundtrip") == {"FOO": "bar", "BAZ": "qux"}
    finally:
        clear_session_env("cao-roundtrip")


def test_get_returns_a_copy_not_the_internal_dict():
    """Caller mutation of the returned dict must not leak into the store."""
    set_session_env("cao-copy", {"K": "v"})
    try:
        got = get_session_env("cao-copy")
        got["K"] = "tampered"
        got["NEW"] = "x"
        assert get_session_env("cao-copy") == {"K": "v"}
    finally:
        clear_session_env("cao-copy")


def test_set_with_empty_dict_clears_mapping():
    """Passing an empty dict drops the entry — avoids two ways to say "none"."""
    set_session_env("cao-empty", {"X": "1"})
    set_session_env("cao-empty", {})
    assert get_session_env("cao-empty") == {}


def test_clear_is_idempotent():
    clear_session_env("cao-never-set")  # must not raise
    set_session_env("cao-clear", {"X": "1"})
    clear_session_env("cao-clear")
    clear_session_env("cao-clear")  # second call — still must not raise
    assert get_session_env("cao-clear") == {}


def test_overwrite_replaces_previous_mapping():
    """A second set fully replaces the prior mapping (not merge)."""
    set_session_env("cao-overwrite", {"A": "1", "B": "2"})
    set_session_env("cao-overwrite", {"C": "3"})
    try:
        assert get_session_env("cao-overwrite") == {"C": "3"}
    finally:
        clear_session_env("cao-overwrite")


class TestPersistence:
    """Write-through durability: SQLite is the source of truth, memory a cache."""

    def test_set_persists_row(self, store):
        set_session_env("cao-persist", {"PATH": "/shim/bin", "ZDOTDIR": "/zsh"})
        rows = _db_rows(store)
        assert len(rows) == 1
        name, payload = rows[0]
        assert name == "cao-persist"
        assert json.loads(payload) == {"PATH": "/shim/bin", "ZDOTDIR": "/zsh"}

    def test_cold_cache_simulated_restart_serves_persisted_env(self, store):
        """Post-restart path: cold memory, new read serves the DB row and
        repopulates the cache."""
        set_session_env("cao-restart", {"PATH": "/shim/bin"})
        _reset_cache()  # simulate server restart: memory wiped, DB survives
        assert get_session_env("cao-restart") == {"PATH": "/shim/bin"}
        # Cache repopulated — a second read does not need the DB.
        with session_env._lock:
            assert session_env._session_forwarded_env["cao-restart"] == {"PATH": "/shim/bin"}

    def test_empty_dict_deletes_row_and_never_stores_empty_row(self, store):
        set_session_env("cao-wipe", {"X": "1"})
        assert len(_db_rows(store)) == 1
        set_session_env("cao-wipe", {})
        assert _db_rows(store) == []
        # Setting {} on an unknown session must not create a row either.
        set_session_env("cao-never", {})
        assert _db_rows(store) == []

    def test_clear_deletes_row(self, store):
        set_session_env("cao-clear-db", {"X": "1"})
        clear_session_env("cao-clear-db")
        assert _db_rows(store) == []
        _reset_cache()  # row is gone, so even a cold read stays empty
        assert get_session_env("cao-clear-db") == {}

    def test_overwrite_updates_row_in_place(self, store):
        set_session_env("cao-upsert", {"A": "1"})
        set_session_env("cao-upsert", {"B": "2"})
        rows = _db_rows(store)
        assert len(rows) == 1
        assert json.loads(rows[0][1]) == {"B": "2"}

    def test_updated_at_recorded(self, store):
        set_session_env("cao-ts", {"A": "1"})
        with sqlite3.connect(store.url.database) as conn:
            updated_at = conn.execute(
                "SELECT updated_at FROM session_env WHERE session_name = 'cao-ts'"
            ).fetchone()[0]
        assert updated_at  # non-empty ISO timestamp


class TestFailClosed:
    """Unreadable state must raise; only a genuinely missing row returns {}."""

    def test_corrupt_json_raises(self, store):
        _insert_raw_row(store, "cao-corrupt", "this is not json{")
        with pytest.raises(SessionEnvStoreError, match="corrupt"):
            get_session_env("cao-corrupt")

    def test_non_object_json_raises(self, store):
        _insert_raw_row(store, "cao-list", '["not", "a", "dict"]')
        with pytest.raises(SessionEnvStoreError, match="corrupt"):
            get_session_env("cao-list")

    def test_missing_table_raises(self, store):
        """A migrated DB missing the session_env table is unreadable state."""
        with sqlite3.connect(store.url.database) as conn:
            conn.execute("DROP TABLE session_env")
        with pytest.raises(SessionEnvStoreError):
            get_session_env("cao-anything")

    def test_locked_db_raises_after_bounded_retry(self, store, monkeypatch):
        """DB errors are retried a bounded number of times, then raise."""
        attempts = 0

        class _BrokenSession:
            def __init__(self, *args, **kwargs):
                nonlocal attempts
                attempts += 1
                raise RuntimeError("database is locked")

        monkeypatch.setattr(database, "SessionLocal", _BrokenSession)
        monkeypatch.setattr(session_env, "_RETRY_DELAY_SECONDS", 0)
        with pytest.raises(SessionEnvStoreError, match="unavailable"):
            get_session_env("cao-locked")
        assert attempts == session_env._MAX_ATTEMPTS

    def test_set_raises_when_db_unwritable(self, store, monkeypatch):
        """A failed upsert must not update the cache — nothing is silently lost."""

        class _BrokenSession:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("disk I/O error")

        monkeypatch.setattr(database, "SessionLocal", _BrokenSession)
        monkeypatch.setattr(session_env, "_RETRY_DELAY_SECONDS", 0)
        with pytest.raises(SessionEnvStoreError):
            set_session_env("cao-nowrite", {"X": "1"})
        with session_env._lock:
            assert "cao-nowrite" not in session_env._session_forwarded_env

    def test_clear_warns_but_does_not_raise_on_db_failure(self, store, monkeypatch, caplog):
        """clear runs on teardown paths where raising would mask the real error;
        it logs, still evicts the cache, and never raises."""
        set_session_env("cao-clearfail", {"X": "1"})

        class _BrokenSession:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("database is locked")

        monkeypatch.setattr(database, "SessionLocal", _BrokenSession)
        monkeypatch.setattr(session_env, "_RETRY_DELAY_SECONDS", 0)
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.session_env"):
            clear_session_env("cao-clearfail")  # must not raise
        assert "cao-clearfail" in caplog.text
        with session_env._lock:
            assert "cao-clearfail" not in session_env._session_forwarded_env

    def test_missing_row_is_legitimate_empty(self, store):
        """No row + working DB = the no-forwarded-env case; proceeds with {}."""
        assert _db_rows(store) == []
        assert get_session_env("cao-legit-empty") == {}


class TestReconcile:
    """Startup reconcile: dead-session rows go, live-session rows stay."""

    def test_removes_dead_keeps_live(self, store):
        set_session_env("cao-live", {"A": "1"})
        set_session_env("cao-dead", {"B": "2"})
        result = reconcile_session_env(lambda name: name == "cao-live")
        assert result["removed"] == ["cao-dead"]
        assert result["kept"] == ["cao-live"]
        assert [r[0] for r in _db_rows(store)] == ["cao-live"]
        # Cache stays consistent with the DB.
        with session_env._lock:
            assert "cao-dead" not in session_env._session_forwarded_env
            assert session_env._session_forwarded_env["cao-live"] == {"A": "1"}

    def test_probe_failure_keeps_row(self, store):
        """Fail toward retention: an unverifiable session never loses its row."""
        set_session_env("cao-maybe", {"A": "1"})

        def _flaky(name):
            raise RuntimeError("tmux server not responding")

        result = reconcile_session_env(_flaky)
        assert result["removed"] == []
        assert result["kept"] == ["cao-maybe"]
        assert len(_db_rows(store)) == 1

    def test_empty_store_is_noop(self, store):
        assert reconcile_session_env(lambda name: False) == {"removed": [], "kept": []}
