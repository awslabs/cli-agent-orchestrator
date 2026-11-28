"""Schema record, refusal, backup, and transaction boundaries (#775)."""

import hashlib
import multiprocessing
import os
import signal
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


def _leave_hot_journal(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA cache_size=2")
    connection.execute("BEGIN")
    connection.execute("UPDATE hot_probe SET value='uncommitted' || value")
    os.kill(os.getpid(), signal.SIGKILL)


def _sqlite_failure(code):
    error = sqlite3.OperationalError("temporary sqlite failure")
    error.sqlite_errorcode = code
    return error


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


def test_gap_in_applied_history_is_refused(schema_db, monkeypatch):
    database_module.init_db()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            *migrations.MIGRATIONS,
            migrations.Migration(3, "third", lambda connection: None, True, False),
        ),
    )
    monkeypatch.setattr(migrations, "HEAD", 3)
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES (3, 'third', 'applied', 'test', 'now', 'now', NULL)"
        )
    with pytest.raises(migrations.SchemaTooNewError, match="gap"):
        database_module.init_db()


def test_renamed_baseline_is_refused(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("UPDATE cao_schema_migrations SET name='different'")
    with pytest.raises(migrations.SchemaTooNewError):
        database_module.init_db()


def test_started_baseline_reuses_backup(schema_db, monkeypatch):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    original = migrations.MIGRATIONS[0]

    def fail_once(connection):
        raise RuntimeError("retry baseline")

    monkeypatch.setattr(migrations, "MIGRATIONS", (replace(original, apply=fail_once),))
    with pytest.raises(migrations.MigrationFailedError):
        database_module.init_db()
    monkeypatch.setattr(migrations, "MIGRATIONS", (original,))
    database_module.init_db()
    assert len(list((schema_db.parent / "backups").glob("*.db"))) == 1


@pytest.mark.parametrize("code", [sqlite3.SQLITE_READONLY_ROLLBACK, sqlite3.SQLITE_BUSY])
def test_check_retries_hot_journal_during_migration(schema_db, monkeypatch, code):
    database_module.init_db()
    original = migrations._classify_safe

    def transient_read(connection, path):
        raise _sqlite_failure(code)

    monkeypatch.setattr(migrations, "_classify_safe", transient_read)
    migrations.check()
    monkeypatch.setattr(migrations, "_classify_safe", original)
    database_module.init_db()


def test_real_hot_journal_is_deferred_until_migration(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("CREATE TABLE hot_probe (value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO hot_probe VALUES (?)",
            [(f"committed-{index}" + "x" * 400,) for index in range(3000)],
        )
    child = multiprocessing.get_context("spawn").Process(
        target=_leave_hot_journal, args=(schema_db,)
    )
    child.start()
    child.join(timeout=30)
    assert child.exitcode == -signal.SIGKILL
    journal = schema_db.with_name(schema_db.name + "-journal")
    assert journal.exists()
    with pytest.raises(sqlite3.OperationalError) as refusal:
        with sqlite3.connect(schema_db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("SELECT count(*) FROM hot_probe").fetchone()
    assert refusal.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK
    migrations.check()
    assert journal.exists()
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        assert connection.execute("SELECT count(*) FROM hot_probe").fetchone() == (3000,)
        assert connection.execute(
            "SELECT count(*) FROM hot_probe WHERE value LIKE 'uncommitted%'"
        ).fetchone() == (0,)
    assert not journal.exists()


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_IOERR],
)
def test_stamped_baseline_transient_failure_never_advises_restore(schema_db, monkeypatch, code):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cao_schema_migration_backups (target_version INTEGER PRIMARY KEY, from_version INTEGER NOT NULL, backup_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO cao_schema_migration_backups VALUES (1, 0, '/old/backup.db')"
        )
    monkeypatch.setattr(
        database_module,
        "_apply_legacy_schema",
        lambda: (_ for _ in ()).throw(_sqlite_failure(code)),
    )
    with pytest.raises(migrations.SchemaRetryableError, match="retry") as refusal:
        database_module.init_db()
    assert "restore" not in str(refusal.value).lower()
    assert "/old/backup.db" not in str(refusal.value)


def test_applied_upgrade_expires_backup_record(schema_db):
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        connection.execute("DELETE FROM cao_schema_migrations")
    database_module.init_db()
    with sqlite3.connect(schema_db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cao_schema_migration_backups"
        ).fetchone() == (0,)


def test_transactional_step_transient_failure_never_advises_restore(schema_db, monkeypatch):
    database_module.init_db()
    step = migrations.Migration(
        2,
        "second",
        lambda connection: (_ for _ in ()).throw(_sqlite_failure(sqlite3.SQLITE_IOERR)),
        True,
        False,
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, step))
    monkeypatch.setattr(migrations, "HEAD", 2)
    with pytest.raises(migrations.SchemaRetryableError, match="retry") as refusal:
        database_module.init_db(caller="server")
    assert "restore" not in str(refusal.value).lower()


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PERM, sqlite3.SQLITE_FULL, sqlite3.SQLITE_NOMEM],
)
def test_open_failure_does_not_advise_restore(schema_db, monkeypatch, code):
    database_module.init_db()
    monkeypatch.setattr(
        migrations.sqlite3,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(_sqlite_failure(code)),
    )
    with pytest.raises(migrations.SchemaRetryableError, match="retry") as refusal:
        migrations.check()
    assert "restore" not in str(refusal.value).lower()


def test_changed_database_does_not_reuse_old_backup(schema_db, monkeypatch):
    database_module.init_db()
    step = migrations.Migration(
        2,
        "second",
        lambda connection: (_ for _ in ()).throw(RuntimeError("retry later")),
        True,
        False,
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, step))
    monkeypatch.setattr(migrations, "HEAD", 2)
    with pytest.raises(migrations.MigrationFailedError):
        database_module.init_db(caller="server")
    with sqlite3.connect(schema_db) as connection:
        connection.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider, agent_profile) VALUES ('new', 'session', 'window', 'codex', 'developer')"
        )
    with pytest.raises(migrations.MigrationFailedError):
        database_module.init_db(caller="server")
    backups = list((schema_db.parent / "backups").glob("*.db"))
    assert len(backups) == 2
    with sqlite3.connect(max(backups, key=lambda path: path.stat().st_mtime_ns)) as connection:
        assert connection.execute("SELECT count(*) FROM terminals WHERE id='new'").fetchone() == (
            1,
        )


def test_missing_recorded_backup_is_replaced(schema_db, monkeypatch):
    database_module.init_db()
    step = migrations.Migration(
        2,
        "second",
        lambda connection: (_ for _ in ()).throw(RuntimeError("retry later")),
        True,
        False,
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, step))
    monkeypatch.setattr(migrations, "HEAD", 2)
    with pytest.raises(migrations.MigrationFailedError):
        database_module.init_db(caller="server")
    with sqlite3.connect(schema_db) as connection:
        backup_path = connection.execute(
            "SELECT backup_path FROM cao_schema_migration_backups"
        ).fetchone()[0]
    os.unlink(backup_path)
    with pytest.raises(migrations.MigrationFailedError):
        database_module.init_db(caller="server")
    assert len(list((schema_db.parent / "backups").glob("*.db"))) == 1


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_IOERR],
)
def test_transient_schema_errors_do_not_advise_restore(schema_db, monkeypatch, code):
    def fail(connection):
        error = sqlite3.OperationalError("temporary sqlite failure")
        error.sqlite_errorcode = code
        raise error

    monkeypatch.setattr(migrations, "_classify", fail)
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(migrations.SchemaError, match="retry") as refusal:
            migrations._classify_safe(connection, schema_db)
    assert "restore" not in str(refusal.value).lower()


@pytest.mark.parametrize("suffix", ["%41", "#", "?"])
def test_check_handles_uri_special_characters(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"special{suffix}.db"
    with sqlite3.connect(path) as connection:
        connection.execute(migrations.CREATE_RECORD)
        connection.execute(
            "INSERT INTO cao_schema_migrations VALUES (2, 'future', 'applied', 'future', 'now', 'now', NULL)"
        )
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    with pytest.raises(migrations.SchemaTooNewError, match="future"):
        migrations.check()


def test_transactional_retry_reuses_backup(schema_db, monkeypatch):
    database_module.init_db()

    def fail(connection):
        raise RuntimeError("retry later")

    step = migrations.Migration(2, "second", fail, True, False)
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, step))
    monkeypatch.setattr(migrations, "HEAD", 2)
    with pytest.raises(migrations.MigrationFailedError):
        database_module.init_db()
    assert len(list((schema_db.parent / "backups").glob("*.db"))) == 1
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (*migrations.MIGRATIONS[:1], replace(step, apply=lambda connection: None)),
    )
    database_module.init_db()
    assert len(list((schema_db.parent / "backups").glob("*.db"))) == 1


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
    descriptor = migrations.os.open(
        schema_db.parent / ".schema-migrate.lock",
        migrations.os.O_CREAT | migrations.os.O_RDWR,
        0o600,
    )
    migrations._lock_phase(descriptor, "backing up")
    migrations.os.close(descriptor)
    monkeypatch.setattr(
        migrations.fcntl, "flock", lambda *args: (_ for _ in ()).throw(BlockingIOError())
    )
    with pytest.raises(migrations.MigrationLockTimeout, match=r"pid \d+, phase backing up"):
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
    assert len(list((schema_db.parent / "backups").glob("*.db"))) == 2


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
