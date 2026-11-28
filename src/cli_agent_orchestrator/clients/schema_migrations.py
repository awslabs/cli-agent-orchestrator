"""Ordered schema upgrades with a recorded, verified 2.5 baseline.

While HEAD is 1, the legacy chain runs on every start; when v2 lands it freezes.
New schema changes merged from main then need numbered, idempotent migrations,
and legacy changes made before v2 require a corresponding manifest update.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Iterator

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients.schema_manifest import (
    NONUNIQUE_INDEXES_V1,
    SCHEMA_MANIFEST_V1,
    TOLERATED_UNIQUE_INDEXES,
)
from cli_agent_orchestrator.utils import server_lock

logger = logging.getLogger(__name__)
SCHEMA_MIGRATE_LOCK_TIMEOUT_S = constants.SCHEMA_MIGRATE_LOCK_TIMEOUT_S
RECORD_TABLE = "cao_schema_migrations"
CREATE_RECORD = """CREATE TABLE IF NOT EXISTS cao_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started','applied')),
    cao_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    applied_at TEXT,
    backup_path TEXT
)"""


class SchemaError(RuntimeError):
    """A database cannot safely be opened by this binary."""


class SchemaTooNewError(SchemaError):
    """The recorded version is not understood by this binary."""


class UnrecognisedSchemaError(SchemaError):
    """The database does not have a recognised CAO layout."""


class SchemaRetryableError(SchemaError):
    """A transient SQLite failure prevents checking the database."""


class MigrationInterruptedError(SchemaError):
    """An unfinished non-rerunnable migration needs manual recovery."""


class MigrationFailedError(SchemaError):
    """A migration failed before its version was stamped."""


class SchemaBackupError(SchemaError):
    """A consistent snapshot could not be made."""


class MigrationLockTimeout(SchemaError):
    """Another process is still upgrading the database."""


class ServerRunningError(SchemaError):
    """A running server must upgrade the database itself."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]
    transactional: bool
    rerunnable: bool


def _apply_baseline(connection: sqlite3.Connection) -> None:
    from cli_agent_orchestrator.clients import database

    database._apply_legacy_schema()
    _verify_manifest(connection)


MIGRATIONS = (Migration(1, "baseline_2_5", _apply_baseline, False, True),)
HEAD = MIGRATIONS[-1].version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _writer_version() -> str:
    try:
        return version("cli-agent-orchestrator")
    except PackageNotFoundError:
        return "unknown"


@contextmanager
def _migration_lock(path: Path) -> Iterator[int]:
    lock = path.parent / constants.SCHEMA_MIGRATE_LOCK_NAME
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + SCHEMA_MIGRATE_LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    holder = os.read(descriptor, 128).decode("ascii", errors="replace").strip()
                    raise MigrationLockTimeout(
                        f"Another CAO process ({holder or 'holder unknown'}) is upgrading "
                        f"the database under schema lock {lock}; retry when it finishes"
                    ) from error
                time.sleep(0.05)
        _lock_phase(descriptor, "checking")
        yield descriptor
    finally:
        os.close(descriptor)


def _lock_phase(descriptor: int, phase: str) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, f"pid {os.getpid()}, phase {phase}".encode("ascii"))
    os.ftruncate(descriptor, os.lseek(descriptor, 0, os.SEEK_CUR))


def _verify_manifest(connection: sqlite3.Connection) -> None:
    problems = []
    for table, (columns, indexes) in SCHEMA_MANIFEST_V1.items():
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        ):
            problems.append(table)
            continue
        existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        problems.extend(f"{table}.{column}" for column in columns if column not in existing)
        unique = {row[1] for row in connection.execute(f'PRAGMA index_list("{table}")') if row[2]}
        for name in indexes:
            if name not in unique:
                if name in TOLERATED_UNIQUE_INDEXES:
                    logger.warning("Missing %s; run cao memory repair --apply", name)
                else:
                    problems.append(f"{table}.{name}")
        for name in NONUNIQUE_INDEXES_V1.get(table, ()):
            if name not in {row[1] for row in connection.execute(f'PRAGMA index_list("{table}")')}:
                logger.warning("Missing non-unique index %s; queries may scan", name)
    if problems:
        raise ValueError("Missing schema: " + ", ".join(problems))


def _backup(source: sqlite3.Connection, path: Path, from_version: int) -> Path:
    backup_dir = path.parent / "backups"
    destination = backup_dir / (
        f"{path.stem}.v{from_version}-to-v{HEAD}."
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.db"
    )
    try:
        size = path.stat().st_size
        if shutil.disk_usage(path.parent).free < 2 * size:
            raise SchemaBackupError(
                f"Insufficient free space for schema backup of {path}: need at least "
                f"{2 * size} bytes; clear space, including old backups in {backup_dir}, and retry"
            )
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(destination)) as copy:
                with copy:
                    source.backup(copy)
                    if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise SchemaBackupError(
                            f"Source database {path} failed backup integrity check; "
                            "restore a known-good copy or recover with sqlite3 .recover "
                            "(see docs/updating.md)"
                        )
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
    except (OSError, sqlite3.Error) as error:
        raise SchemaBackupError(f"Cannot back up database before upgrade: {error}") from error


def _check_server() -> None:
    lock_path = server_lock.SERVER_LOCK_FILE
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            holder = server_lock.read_holder(lock_path)
            detail = f" (held by pid {holder.pid}, version {holder.version})" if holder else ""
            raise ServerRunningError(f"restart cao-server to finish the upgrade{detail}") from error
    finally:
        os.close(descriptor)


def _change_counter(path: Path) -> int:
    with path.open("rb") as database_file:
        database_file.seek(24)
        return int.from_bytes(database_file.read(4), "big")


def _classify(connection: sqlite3.Connection) -> tuple[int, bool, str | None]:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        return 0, True, None
    if RECORD_TABLE not in tables:
        if "terminals" not in tables:
            raise UnrecognisedSchemaError(
                "Unrecognised database schema; restore a CAO backup (see docs/updating.md)"
            )
        return 0, False, None
    rows = connection.execute(
        "SELECT version, name, state, cao_version, backup_path "
        "FROM cao_schema_migrations ORDER BY version"
    ).fetchall()
    known = {step.version: step for step in MIGRATIONS}
    for step_version, name, state, writer, backup_path in rows:
        if step_version not in known or step_version > HEAD or name != known[step_version].name:
            raise SchemaTooNewError(
                f"Schema version {step_version} written by CAO {writer} exceeds or is unknown "
                f"to this binary (HEAD {HEAD}); upgrade CAO, or restore the backup made "
                f"before that upgrade ({backup_path or 'no recorded backup'}); see docs/updating.md"
            )
        if state == "started" and not known[step_version].rerunnable:
            raise MigrationInterruptedError(
                f"Migration {step_version} was interrupted; restore "
                f"{backup_path or 'a known-good backup'} (see docs/updating.md)"
            )
    applied = max((row[0] for row in rows if row[2] == "applied"), default=0)
    if any(
        step not in {row[0] for row in rows if row[2] == "applied"}
        for step in range(1, applied + 1)
    ):
        raise SchemaTooNewError(
            "Unknown gap in schema history; restore a matching backup (see docs/updating.md)"
        )
    pending_backup = next((row[4] for row in rows if row[2] == "started"), None)
    if applied < HEAD and pending_backup is None and "cao_schema_migration_backups" in tables:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(cao_schema_migration_backups)")
        }
        if "change_counter" in columns:
            backup = connection.execute(
                "SELECT backup_path, change_counter FROM cao_schema_migration_backups "
                "WHERE target_version=? AND from_version<=?",
                (HEAD, applied),
            ).fetchone()
            if (
                backup
                and Path(backup[0]).is_file()
                and backup[1] == _change_counter(constants.DATABASE_FILE)
            ):
                pending_backup = backup[0]
    return applied, False, pending_backup


def _schema_read_error(error: sqlite3.DatabaseError, path: Path) -> SchemaError:
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    if code in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_ERROR):
        return UnrecognisedSchemaError(
            f"Cannot read CAO schema in {path}: {error}; restore a known-good "
            "backup (see docs/updating.md)"
        )
    return SchemaRetryableError(
        f"Cannot read CAO schema in {path}: {error}; retry when SQLite is available "
        "(see docs/updating.md)"
    )


def _classify_safe(connection: sqlite3.Connection, path: Path) -> tuple[int, bool, str | None]:
    try:
        return _classify(connection)
    except sqlite3.DatabaseError as error:
        raise _schema_read_error(error, path) from error
    except SchemaError as error:
        raise type(error)(f"{path}: {error}") from error


def check() -> None:
    """Read-only compatibility check before uvicorn owns the startup lifecycle."""
    path = constants.DATABASE_FILE
    if not path.exists():
        return
    with _migration_lock(path):
        try:
            with closing(
                sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                _classify_safe(connection, path)
        except SchemaRetryableError as error:
            if (
                getattr(error.__cause__, "sqlite_errorcode", 0) == sqlite3.SQLITE_READONLY_ROLLBACK
                or (getattr(error.__cause__, "sqlite_errorcode", 0) & 0xFF) == sqlite3.SQLITE_BUSY
            ):
                return
            raise
        except sqlite3.DatabaseError as error:
            if (
                getattr(error, "sqlite_errorcode", 0) == sqlite3.SQLITE_READONLY_ROLLBACK
                or (getattr(error, "sqlite_errorcode", 0) & 0xFF) == sqlite3.SQLITE_BUSY
            ):
                return
            raise _schema_read_error(error, path) from error


def migrate(*, caller: str = "cli") -> None:
    """Refuse incompatible files before running any legacy schema operation."""
    path = constants.DATABASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _migration_lock(path) as lock_descriptor:
        try:
            connection = sqlite3.connect(path, isolation_level=None)
        except sqlite3.DatabaseError as error:
            raise _schema_read_error(error, path) from error
        with closing(connection), connection:
            current, fresh, pending_backup = _classify_safe(connection, path)
            steps = [step for step in MIGRATIONS if step.version > current]
            if caller != "server" and any(step.version > 1 for step in steps):
                _check_server()
            backup_path = pending_backup
            if not fresh and steps and backup_path is None:
                _lock_phase(lock_descriptor, "backing up")
                try:
                    backup_path = str(_backup(connection, path, current))
                except SchemaBackupError:
                    raise
                except Exception as error:
                    raise SchemaBackupError(f"Cannot back up database: {error}") from error
                counter = _change_counter(path)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS cao_schema_migration_backups ("
                        "target_version INTEGER PRIMARY KEY, from_version INTEGER NOT NULL, "
                        "backup_path TEXT NOT NULL, change_counter INTEGER)"
                    )
                    columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(cao_schema_migration_backups)"
                        )
                    }
                    if "change_counter" not in columns:
                        connection.execute(
                            "ALTER TABLE cao_schema_migration_backups ADD COLUMN change_counter INTEGER"
                        )
                    connection.execute(
                        "INSERT OR REPLACE INTO cao_schema_migration_backups "
                        "(target_version, from_version, backup_path, change_counter) "
                        "VALUES (?, ?, ?, ?)",
                        (HEAD, current, backup_path, counter + 1),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            connection.execute(CREATE_RECORD)
            for step in steps:
                _lock_phase(lock_descriptor, f"applying v{step.version}")
                try:
                    if step.transactional:
                        connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT OR REPLACE INTO cao_schema_migrations "
                        "(version, name, state, cao_version, started_at, applied_at, backup_path) "
                        "VALUES (?, ?, 'started', ?, ?, NULL, ?)",
                        (step.version, step.name, _writer_version(), _now(), backup_path),
                    )
                    step.apply(connection)
                    connection.execute(
                        "UPDATE cao_schema_migrations SET state='applied', applied_at=? "
                        "WHERE version=?",
                        (_now(), step.version),
                    )
                    if (
                        step.version == HEAD
                        and backup_path is not None
                        and connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='cao_schema_migration_backups'"
                        ).fetchone()
                    ):
                        connection.execute(
                            "DELETE FROM cao_schema_migration_backups WHERE target_version=?",
                            (HEAD,),
                        )
                    if step.transactional:
                        connection.execute("COMMIT")
                except Exception as error:
                    if step.transactional and connection.in_transaction:
                        connection.execute("ROLLBACK")
                    if isinstance(error, sqlite3.DatabaseError):
                        refusal = _schema_read_error(error, path)
                        if isinstance(refusal, SchemaRetryableError):
                            raise refusal from error
                    raise MigrationFailedError(
                        f"Migration {step.version} ({step.name}) failed: {error}; "
                        f"restore {backup_path or 'a known-good backup'} "
                        f"(see docs/updating.md); database: {path}"
                    ) from error
            if HEAD == 1 and current == 1:
                try:
                    _apply_baseline(connection)
                except Exception as error:
                    if isinstance(error, sqlite3.DatabaseError):
                        refusal = _schema_read_error(error, path)
                        if isinstance(refusal, SchemaRetryableError):
                            raise refusal from error
                    raise MigrationFailedError(
                        f"Baseline verification failed: {error}; restore "
                        f"{backup_path or 'a known-good backup'} "
                        f"(see docs/updating.md); database: {path}"
                    ) from error
            from cli_agent_orchestrator.clients.database import _restrict_db_file_permissions

            _restrict_db_file_permissions()
