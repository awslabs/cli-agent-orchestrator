"""The schema lock serializes processes and prevents partial upgrades."""

import fcntl
import multiprocessing
import os
import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients import database as database_module
from cli_agent_orchestrator.clients import schema_migrations as migrations


def _initialize(path):
    from sqlalchemy import create_engine

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database

    constants.DATABASE_FILE = path
    database.engine = create_engine(f"sqlite:///{path}")
    database.init_db()


def test_eight_processes_take_one_backup(tmp_path, monkeypatch):
    path = tmp_path / "shared.db"
    with sqlite3.connect(path) as connection:
        fixture = Path(__file__).resolve().parents[1] / "fixtures/db_schemas/v2.5.5.sql"
        connection.executescript(fixture.read_text())
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    processes = [
        multiprocessing.get_context("spawn").Process(target=_initialize, args=(path,))
        for _ in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=90)
        assert process.exitcode == 0
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM cao_schema_migrations WHERE state='applied'"
            ).fetchone()[0]
            == 1
        )
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_lock_timeout_names_lock(tmp_path, monkeypatch):
    path = tmp_path / "shared.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    monkeypatch.setattr(migrations, "SCHEMA_MIGRATE_LOCK_TIMEOUT_S", 0.1)
    lock_path = tmp_path / ".schema-migrate.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migrations.MigrationLockTimeout, match="schema-migrate.lock"):
            _initialize(path)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    _initialize(path)


def test_server_lock_blocks_post_baseline_cli(tmp_path, monkeypatch):
    path = tmp_path / "shared.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    _initialize(path)
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            *migrations.MIGRATIONS,
            migrations.Migration(2, "test", lambda connection: None, True, False),
        ),
    )
    monkeypatch.setattr(migrations, "HEAD", 2)
    server_lock_path = tmp_path / "server.lock"
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.server_lock.SERVER_LOCK_FILE", server_lock_path
    )
    descriptor = os.open(server_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migrations.ServerRunningError, match="restart cao-server"):
            database_module.init_db()
        database_module.init_db(caller="server")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
