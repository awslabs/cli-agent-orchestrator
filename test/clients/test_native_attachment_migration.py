"""The native-attachment table reaches databases that predate it.

Fresh databases get the table from ``Base.metadata.create_all`` via the
ORM model; this migration is the only path for a database created before
native execution mode existed. Both paths must yield one schema, or a
native launch would record ownership into a shape the ORM cannot read.
"""

from __future__ import annotations

import sqlite3

import pytest

from cli_agent_orchestrator.clients import database

TABLE = "native_session_attachments"


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database file with every table except the attachment store."""
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
    database._migrate_native_session_attachments()
    assert set(_columns(legacy_db)) == {
        "provider",
        "native_session_id",
        "state",
        "owner_terminal_id",
        "owner_generation",
        "owner_execution_mode",
        "owner_pane_id",
        "owner_process_identity_json",
        "intent_json",
        "release_proof_json",
        "ambiguity_reason",
        "epoch",
        "created_at",
        "updated_at",
    }


def test_the_key_is_the_provider_session_pair(legacy_db):
    """Exclusivity is enforced by the primary key, not by application code."""
    database._migrate_native_session_attachments()
    assert _primary_key(legacy_db) == ["provider", "native_session_id"]


def test_migration_is_idempotent(legacy_db):
    database._migrate_native_session_attachments()
    with sqlite3.connect(str(legacy_db)) as conn:
        conn.execute(
            f"INSERT INTO {TABLE} VALUES "
            "('kimi_cli','s1','declared','t1','g1','native_tui',NULL,NULL,'{}',NULL,NULL,0,'x','x')"
        )
    database._migrate_native_session_attachments()
    with sqlite3.connect(str(legacy_db)) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1


def test_migrated_ddl_matches_the_orm_model(legacy_db, tmp_path):
    """A row written through either path must be readable through the other."""
    from sqlalchemy import create_engine

    database._migrate_native_session_attachments()

    fresh = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{fresh}")
    database.NativeSessionAttachmentModel.__table__.create(bind=engine)
    engine.dispose()

    assert _columns(legacy_db) == _columns(fresh)
    assert _primary_key(legacy_db) == _primary_key(fresh)


def test_init_db_applies_the_migration():
    """The migration is unreachable unless init_db actually calls it."""
    import inspect

    source = inspect.getsource(database.init_db)
    assert "_migrate_native_session_attachments()" in source
