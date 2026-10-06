"""Durable ``initial_delivery`` outcome on the terminals table (PR #566, round 9)."""

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'terminals.db'}", connect_args={"check_same_thread": False}
    )
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    try:
        yield engine
    finally:
        engine.dispose()


def _create(terminal_id, **kwargs):
    return db_mod.create_terminal(terminal_id, "cao-s", f"w-{terminal_id}", "kiro_cli", **kwargs)


class TestInitialDeliveryColumn:
    def test_round_trips_through_create_and_metadata(self, real_db):
        created = _create("pend", initial_delivery={"state": "pending"})
        assert created["initial_delivery"] == {"state": "pending"}
        assert db_mod.get_terminal_metadata("pend")["initial_delivery"] == {"state": "pending"}

    def test_absent_by_default(self, real_db):
        created = _create("plain")
        assert created["initial_delivery"] is None
        assert db_mod.get_terminal_metadata("plain")["initial_delivery"] is None

    def test_update_settles_a_pending_row(self, real_db):
        _create("pend", initial_delivery={"state": "pending"})
        assert db_mod.update_terminal_initial_delivery(
            "pend", {"state": "delivered"}, only_if_pending=True
        )
        assert db_mod.get_terminal_metadata("pend")["initial_delivery"] == {"state": "delivered"}

    def test_conditional_update_does_not_rewrite_a_settled_or_absent_outcome(self, real_db):
        _create("done", initial_delivery={"state": "delivered"})
        _create("plain")
        failed = {"state": "failed", "kind": "interrupted"}
        assert (
            db_mod.update_terminal_initial_delivery("done", failed, only_if_pending=True) is False
        )
        assert (
            db_mod.update_terminal_initial_delivery("plain", failed, only_if_pending=True) is False
        )
        assert db_mod.update_terminal_initial_delivery("ghost", failed) is False
        assert db_mod.get_terminal_metadata("done")["initial_delivery"] == {"state": "delivered"}
        assert db_mod.get_terminal_metadata("plain")["initial_delivery"] is None

    def test_lists_only_pending_rows(self, real_db):
        _create("a", initial_delivery={"state": "pending"})
        _create("b", initial_delivery={"state": "delivered"})
        _create("c", initial_delivery={"state": "failed", "kind": "task_not_started"})
        _create("d")
        assert db_mod.list_pending_initial_delivery_terminal_ids() == ["a"]


class TestMigrationAddsTheColumn:
    def test_existing_database_gains_initial_delivery_idempotently(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator import constants

        db_file = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT, tmux_window TEXT, "
            "provider TEXT, agent_profile TEXT, last_active DATETIME)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(constants, "DATABASE_FILE", db_file)

        db_mod._migrate_terminals_schema()
        db_mod._migrate_terminals_schema()  # second run must be a no-op

        conn = sqlite3.connect(str(db_file))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}
        conn.close()
        assert "initial_delivery" in columns
        assert "deferred_init_failure" in columns
