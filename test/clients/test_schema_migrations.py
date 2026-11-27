"""Schema record, refusal, backup, and transaction boundaries (#775)."""

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as database_module
from cli_agent_orchestrator.clients import schema_migrations as migrations


@pytest.fixture
def schema_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    yield path
    engine.dispose()


def records(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT version, state, backup_path FROM cao_schema_migrations ORDER BY version"
        ).fetchall()


def test_registry_contiguous_and_unique():
    assert [step.version for step in migrations.MIGRATIONS] == list(
        range(1, migrations.HEAD + 1)
    )
    assert len({step.name for step in migrations.MIGRATIONS}) == len(migrations.MIGRATIONS)


def test_fresh_home_is_stamped_without_backup(schema_db):
    database_module.init_db()
    assert records(schema_db) == [(1, "applied", None)]
    assert not (schema_db.parent / "backups").exists()
    database_module.init_db()
    assert records(schema_db) == [(1, "applied", None)]


def test_newer_version_is_read_only(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES (2, 'future', 'applied', "
            "'future-writer', 'now', 'now', NULL)"
        )
    before = hashlib.sha256(schema_db.read_bytes()).hexdigest()
    with pytest.raises(migrations.SchemaTooNewError, match="restore the backup"):
        database_module.init_db()
    assert hashlib.sha256(schema_db.read_bytes()).hexdigest() == before


def test_foreign_schema_refused(schema_db):
    with sqlite3.connect(schema_db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(migrations.UnrecognisedSchemaError):
        database_module.init_db()


def test_manifest_missing_column_refuses_stamp(schema_db, monkeypatch):
    baseline = migrations.MIGRATIONS[0]
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
        connection.execute("ALTER TABLE terminals DROP COLUMN runner_id")
    monkeypatch.setattr(database_module, "_migrate_terminals_schema", lambda: None)
    with pytest.raises(migrations.MigrationFailedError, match="terminals.runner_id"):
        database_module.init_db()
    assert records(schema_db)[0][1] == "started"
    monkeypatch.undo()
    assert baseline.rerunnable


def test_transaction_rolls_back_failed_step(schema_db, monkeypatch):
    database_module.init_db()

    def second(connection):
        connection.execute("ALTER TABLE terminals ADD COLUMN test_v2 TEXT")

    def third(connection):
        connection.execute("ALTER TABLE terminals ADD COLUMN test_v3 TEXT")
        raise RuntimeError("broken step")

    steps = (
        migrations.MIGRATIONS[0],
        migrations.Migration(2, "test_v2", second, True, False),
        migrations.Migration(3, "test_v3", third, True, False),
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", steps)
    monkeypatch.setattr(migrations, "HEAD", 3)
    with pytest.raises(migrations.MigrationFailedError, match="test_v3"):
        database_module.init_db()
    assert records(schema_db)[-1][0:2] == (2, "applied")
    with sqlite3.connect(schema_db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(terminals)")}
    assert "test_v2" in columns and "test_v3" not in columns
    monkeypatch.setattr(migrations, "MIGRATIONS", (*steps[:2], replace(steps[2], apply=lambda c: None)))
    database_module.init_db()
    assert records(schema_db)[-1][0:2] == (3, "applied")


def test_backup_error_prevents_upgrade(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")

    def fail_backup(*args):
        raise OSError("backup failure")

    monkeypatch.setattr(migrations, "_backup", fail_backup)
    with pytest.raises(migrations.SchemaBackupError, match="backup failure"):
        database_module.init_db()
    assert records(schema_db) == []
