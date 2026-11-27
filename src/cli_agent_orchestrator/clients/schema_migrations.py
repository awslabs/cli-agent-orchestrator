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
from contextlib import contextmanager
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
def _migration_lock(path: Path) -> Iterator[None]:
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
                    raise MigrationLockTimeout(
                        f"Timed out waiting for schema lock {lock}"
                    ) from error
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


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
                    logger.warning("Missing %s; run cao memory repair", name)
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
                f"Insufficient free space for schema backup: need at least {2 * size} bytes"
            )
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with sqlite3.connect(destination) as copy:
            source.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SchemaBackupError(f"Schema backup failed quick_check: {destination}")
        return destination
    except (OSError, sqlite3.Error) as error:
        raise SchemaBackupError(f"Cannot back up database before upgrade: {error}") from error


def _check_server() -> None:
    lock_path = server_lock.SERVER_LOCK_FILE
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            holder = server_lock.read_holder(lock_path)
            detail = f" (held by pid {holder.pid}, version {holder.version})" if holder else ""
            raise ServerRunningError(f"restart cao-server to finish the upgrade{detail}") from error
    finally:
        os.close(descriptor)


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
            raise UnrecognisedSchemaError("Unrecognised database schema; restore a CAO backup")
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
                "before that upgrade"
            )
        if state == "started" and not known[step_version].rerunnable:
            raise MigrationInterruptedError(
                f"Migration {step_version} was interrupted; restore backup {backup_path}"
            )
    applied = max((row[0] for row in rows if row[2] == "applied"), default=0)
    if any(
        step not in {row[0] for row in rows if row[2] == "applied"}
        for step in range(1, applied + 1)
    ):
        raise SchemaTooNewError("Unknown gap in schema history; restore a matching backup")
    pending_backup = next((row[4] for row in rows if row[2] == "started"), None)
    return applied, False, pending_backup


def migrate(*, caller: str = "cli") -> None:
    """Refuse incompatible files before running any legacy schema operation."""
    path = constants.DATABASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _migration_lock(path):
        with sqlite3.connect(path, isolation_level=None) as connection:
            current, fresh, pending_backup = _classify(connection)
            steps = [step for step in MIGRATIONS if step.version > current]
            if caller != "server" and any(step.version > 1 for step in steps):
                _check_server()
            backup_path = pending_backup
            if not fresh and steps and backup_path is None:
                try:
                    backup_path = str(_backup(connection, path, current))
                except SchemaBackupError:
                    raise
                except Exception as error:
                    raise SchemaBackupError(f"Cannot back up database: {error}") from error
            connection.execute(CREATE_RECORD)
            for step in steps:
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
                    if step.transactional:
                        connection.execute("COMMIT")
                except Exception as error:
                    if step.transactional and connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise MigrationFailedError(
                        f"Migration {step.version} ({step.name}) failed: {error}; "
                        f"backup: {backup_path}"
                    ) from error
            if HEAD == 1 and current == 1:
                try:
                    _apply_baseline(connection)
                except Exception as error:
                    raise MigrationFailedError(
                        f"Baseline verification failed: {error}; backup: {backup_path}"
                    ) from error
            from cli_agent_orchestrator.clients.database import _restrict_db_file_permissions

            _restrict_db_file_permissions()
