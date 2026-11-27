"""Schema record, refusal, backup, and transaction boundaries (#775)."""

import hashlib
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as database_module
from cli_agent_orchestrator.clients import schema_migrations as migrations
from cli_agent_orchestrator.clients.schema_manifest import SCHEMA_MANIFEST_V1


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
    assert [step.version for step in migrations.MIGRATIONS] == list(range(1, migrations.HEAD + 1))
    assert len({step.name for step in migrations.MIGRATIONS}) == len(migrations.MIGRATIONS)


def test_fresh_home_is_stamped_without_backup(schema_db):
    database_module.init_db()
    assert records(schema_db) == [(1, "applied", None)]
    assert not (schema_db.parent / "backups").exists()
    database_module.init_db()
    assert records(schema_db) == [(1, "applied", None)]


def test_manifest_matches_fresh_baseline(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'cao_schema_migrations'"
            )
        }
        assert actual == set(SCHEMA_MANIFEST_V1)
        for table, (columns, indexes) in SCHEMA_MANIFEST_V1.items():
            assert {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            } == columns
            present = {row[1] for row in connection.execute(f'PRAGMA index_list("{table}")')}
            assert indexes <= present


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


def test_too_new_refusal_names_recorded_backup(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES "
            "(2, 'future', 'applied', 'future-writer', 'now', 'now', '/local/older.db')"
        )
    with pytest.raises(migrations.SchemaTooNewError, match="/local/older.db"):
        database_module.init_db()


def test_foreign_schema_refused(schema_db):
    with sqlite3.connect(schema_db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(migrations.UnrecognisedSchemaError):
        database_module.init_db()


def test_manifest_missing_column_refuses_stamp(schema_db, monkeypatch):
    baseline = migrations.MIGRATIONS[0]
    original_migrator = database_module._migrate_terminals_schema
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
        connection.execute("ALTER TABLE terminals DROP COLUMN runner_id")
    monkeypatch.setattr(database_module, "_migrate_terminals_schema", lambda: None)
    with pytest.raises(migrations.MigrationFailedError, match="terminals.runner_id"):
        database_module.init_db()
    assert records(schema_db)[0][1] == "started"
    monkeypatch.setattr(database_module, "_migrate_terminals_schema", original_migrator)
    database_module.init_db()
    assert records(schema_db)[0][1] == "applied"
    assert baseline.rerunnable


def test_missing_required_unique_index_refuses_start(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DROP INDEX uq_memory_rel")
    monkeypatch.setattr(database_module, "_migrate_memory_relationships", lambda: None)
    with pytest.raises(migrations.MigrationFailedError, match="memory_relationships.uq_memory_rel"):
        database_module.init_db()


def test_missing_nonunique_index_only_warns(schema_db, monkeypatch, caplog):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DROP INDEX idx_memory_type")
    monkeypatch.setattr(database_module, "_migrate_memory_indexes", lambda: None)
    database_module.init_db()
    assert "Missing non-unique index idx_memory_type" in caplog.text


def test_duplicate_null_scope_memories_tolerate_unique_index(schema_db, caplog):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DROP INDEX uq_memory_key_scope_null")
        connection.executemany(
            "INSERT INTO memory_metadata "
            "(id, key, memory_type, scope, scope_id, file_path, tags, access_count) "
            "VALUES (?, 'duplicate', 'project', 'global', NULL, ?, '', 0)",
            (("fixture-one", "/note-one"), ("fixture-two", "/note-two")),
        )
    database_module.init_db()
    assert "cao memory repair --apply" in caplog.text


def test_failed_fresh_migration_names_recovery_without_none(schema_db, monkeypatch):
    broken = migrations.Migration(1, "baseline_2_5", lambda connection: 1 / 0, False, True)
    monkeypatch.setattr(migrations, "MIGRATIONS", (broken,))
    with pytest.raises(migrations.MigrationFailedError) as failure:
        database_module.init_db()
    assert "a known-good backup" in str(failure.value)
    assert "docs/updating.md" in str(failure.value)
    assert "backup: None" not in str(failure.value)


def test_started_nonrerunnable_step_is_refused(schema_db, monkeypatch):
    database_module.init_db()
    second = migrations.Migration(2, "unfinished", lambda connection: None, True, False)
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, second))
    monkeypatch.setattr(migrations, "HEAD", 2)
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES "
            "(2, 'unfinished', 'started', 'future', 'now', NULL, 'backup.db')"
        )
    with pytest.raises(migrations.MigrationInterruptedError, match="backup.db"):
        database_module.init_db()


def test_unknown_registry_version_is_refused(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("UPDATE cao_schema_migrations SET version=0, cao_version='strange'")
    with pytest.raises(migrations.SchemaTooNewError, match="strange"):
        database_module.init_db()


def test_insufficient_backup_space_refuses_early(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    disk = migrations.shutil.disk_usage(schema_db.parent)
    monkeypatch.setattr(migrations.shutil, "disk_usage", lambda path: disk._replace(free=0))
    with pytest.raises(migrations.SchemaBackupError, match="Insufficient free space"):
        database_module.init_db()
    assert not (schema_db.parent / "backups").exists()


def test_backup_space_requires_twice_file_size(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    size = schema_db.stat().st_size
    disk = migrations.shutil.disk_usage(schema_db.parent)
    monkeypatch.setattr(
        migrations.shutil, "disk_usage", lambda path: disk._replace(free=2 * size - 1)
    )
    with pytest.raises(migrations.SchemaBackupError, match="Insufficient free space"):
        database_module.init_db()
    assert not (schema_db.parent / "backups").exists()
    monkeypatch.setattr(migrations.shutil, "disk_usage", lambda path: disk._replace(free=2 * size))
    database_module.init_db()
    assert len(list((schema_db.parent / "backups").glob("*.db"))) == 1


def test_existing_backup_directory_is_restricted(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    directory = schema_db.parent / "backups"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    database_module.init_db()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert len(list(directory.glob("*.db"))) == 1


def test_failed_backup_removes_partial_copy(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    original = migrations.sqlite3.connect

    class BadCopy(sqlite3.Connection):
        def execute(self, statement, *args):
            if statement == "PRAGMA quick_check":
                return super().execute("SELECT 'corrupt'")
            return super().execute(statement, *args)

    def connect(path, *args, **kwargs):
        if path.parent.name == "backups":
            kwargs["factory"] = BadCopy
        return original(path, *args, **kwargs)

    monkeypatch.setattr(migrations.sqlite3, "connect", connect)
    for _ in range(2):
        with pytest.raises(migrations.SchemaBackupError, match="integrity"):
            database_module.init_db()
        assert list((schema_db.parent / "backups").glob("*.db")) == []


def test_unreadable_schema_raises_schema_error(schema_db):
    schema_db.write_bytes(b"not a sqlite database")
    with pytest.raises(migrations.UnrecognisedSchemaError, match="test.db"):
        database_module.init_db()


def test_invalid_record_shape_raises_schema_error(schema_db):
    with sqlite3.connect(schema_db) as connection:
        connection.execute("CREATE TABLE cao_schema_migrations (version INTEGER)")
    with pytest.raises(migrations.UnrecognisedSchemaError, match="test.db"):
        database_module.init_db()


def test_server_entry_refuses_before_uvicorn(schema_db, monkeypatch, capsys):
    from cli_agent_orchestrator.api import main

    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES "
            "(2, 'future', 'applied', 'future-writer', 'now', 'now', NULL)"
        )
    monkeypatch.setattr("sys.argv", ["cao-server"])
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("uvicorn started"))
    with pytest.raises(SystemExit) as refusal:
        main.main()
    assert refusal.value.code == 1
    output = capsys.readouterr().err
    assert "cao-server:" in output
    assert "Traceback" not in output


def test_schema_lock_timeout_names_active_upgrader(schema_db, monkeypatch):
    monkeypatch.setattr(migrations, "SCHEMA_MIGRATE_LOCK_TIMEOUT_S", 0)
    monkeypatch.setattr(
        migrations.fcntl, "flock", lambda *args: (_ for _ in ()).throw(BlockingIOError())
    )
    with pytest.raises(migrations.MigrationLockTimeout, match="retry when it finishes"):
        database_module.init_db()


def test_large_upgrade_allows_concurrent_starter():
    from cli_agent_orchestrator import constants

    assert constants.SCHEMA_MIGRATE_LOCK_TIMEOUT_S >= 60


def test_server_probe_does_not_take_exclusive_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(migrations.server_lock, "SERVER_LOCK_FILE", tmp_path / "server.lock")
    calls = []
    monkeypatch.setattr(
        migrations.fcntl, "flock", lambda descriptor, operation: calls.append(operation)
    )
    migrations._check_server()
    assert calls == [migrations.fcntl.LOCK_SH | migrations.fcntl.LOCK_NB]


def test_migration_and_backup_connections_close(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    original = migrations.sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(migrations.sqlite3, "connect", connect)
    database_module.init_db()
    assert len(connections) >= 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_schedule_and_decision_mcp_surface_schema_error(schema_db, monkeypatch):
    from cli_agent_orchestrator.cli.commands.init import init as init_command
    from cli_agent_orchestrator.cli.commands.schedule import schedule
    from cli_agent_orchestrator.ops_mcp_server.decision_tools import _result

    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES "
            "(2, 'future', 'applied', 'future-writer', 'now', 'now', NULL)"
        )
    result = CliRunner().invoke(schedule, ["list"])
    assert result.exit_code != 0
    assert "restore the backup" in result.output
    assert "Traceback" not in result.output
    init_result = CliRunner().invoke(init_command)
    assert init_result.exit_code != 0
    assert "restore the backup" in init_result.output
    assert "Traceback" not in init_result.output
    message = _result(database_module.init_db)
    assert message["success"] is False
    assert "restore the backup" in message["message"]


@pytest.mark.asyncio
async def test_server_lifespan_refuses_schema_error(monkeypatch, capsys):
    from cli_agent_orchestrator.api import main

    monkeypatch.setattr(main, "remote_auth_refusal", lambda: None)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "_prime_host_sampler_if_enabled", AsyncMock())
    monkeypatch.setattr(main.server_lock, "acquire_server_lock", lambda **kwargs: MagicMock())
    monkeypatch.setattr(main, "_runner_token_refusal", lambda: None)
    monkeypatch.setattr(
        main, "init_db", MagicMock(side_effect=migrations.SchemaTooNewError("restore the backup"))
    )
    with pytest.raises(SystemExit) as refusal:
        async with main.lifespan(main.app):
            pass
    assert refusal.value.code == 1
    assert "restore the backup" in capsys.readouterr().err


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
    monkeypatch.setattr(
        migrations, "MIGRATIONS", (*steps[:2], replace(steps[2], apply=lambda c: None))
    )
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
