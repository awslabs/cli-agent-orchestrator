"""The native control-operation table reaches databases that predate it.

Fresh databases get the table from ``Base.metadata.create_all`` via the
ORM model; this migration is the only path for a database created before
native control input existed. Both paths must yield one schema, because
an operation journaled through a shape the ORM cannot read is an
operation nobody can adjudicate after a crash.
"""

from __future__ import annotations

import sqlite3

import pytest

from cli_agent_orchestrator.clients import database

TABLE = "kimi_native_control_operations"


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database file with every table except the control-operation store."""
    path = tmp_path / "cli-agent-orchestrator.db"
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=False)
    return path


def _columns(path) -> dict[str, str]:
    with sqlite3.connect(str(path)) as conn:
        return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def _primary_key(path) -> list[str]:
    with sqlite3.connect(str(path)) as conn:
        rows = [row for row in conn.execute(f"PRAGMA table_info({TABLE})") if row[5]]
    return [row[1] for row in sorted(rows, key=lambda row: row[5])]


def test_migration_creates_the_table_on_a_database_that_lacks_it(legacy_db):
    assert _columns(legacy_db) == {}
    database._migrate_kimi_native_control_operations()
    assert set(_columns(legacy_db)) == {
        "operation_id",
        "kind",
        "state",
        "provider",
        "native_session_id",
        "terminal_id",
        "generation",
        "execution_mode",
        "turn_id",
        "payload_sha256",
        "intent_json",
        "transport_json",
        "observation_json",
        "posted_at",
        "refusal_reason",
        "ambiguity_reason",
        "epoch",
        "created_at",
        "updated_at",
    }


def test_the_key_is_the_caller_minted_operation_id(legacy_db):
    """At-most-once rests on the key, not on application bookkeeping."""
    database._migrate_kimi_native_control_operations()
    assert _primary_key(legacy_db) == ["operation_id"]


def test_migration_is_idempotent(legacy_db):
    database._migrate_kimi_native_control_operations()
    with sqlite3.connect(str(legacy_db)) as conn:
        conn.execute(
            f"INSERT INTO {TABLE} (operation_id, kind, state, provider, native_session_id, "
            "terminal_id, generation, execution_mode, payload_sha256, intent_json, epoch, "
            "created_at, updated_at) VALUES "
            "('op1','queue','intended','kimi_cli','s1','t1','g1','native_tui','d','{}',0,'x','x')"
        )
    database._migrate_kimi_native_control_operations()
    with sqlite3.connect(str(legacy_db)) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1


def test_migrated_ddl_matches_the_orm_model(legacy_db, tmp_path):
    """A row written through either path must be readable through the other."""
    from sqlalchemy import create_engine

    database._migrate_kimi_native_control_operations()

    fresh = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{fresh}")
    database.KimiNativeControlOperationModel.__table__.create(bind=engine)
    engine.dispose()

    assert _columns(legacy_db) == _columns(fresh)
    assert _primary_key(legacy_db) == _primary_key(fresh)


def test_init_db_applies_the_migration():
    """The migration is unreachable unless init_db actually calls it."""
    import inspect

    source = inspect.getsource(database.init_db)
    assert "_migrate_kimi_native_control_operations()" in source
