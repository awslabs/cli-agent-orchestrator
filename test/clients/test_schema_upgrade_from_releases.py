"""Open real release DDL, preserving rows and taking a checked snapshot."""

import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as database_module

SCHEMAS = Path(__file__).resolve().parents[1] / "fixtures/db_schemas"
RELEASES = (
    "v2.4.1",
    "v2.5.0",
    "v2.5.1",
    "v2.5.2",
    "v2.5.4",
    "v2.5.5",
    "v2.4.1-to-v2.5.5",
    "3.0-beta1",
)


def seed_all_tables(connection):
    snapshots = {}
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        writable = [
            column for column in columns if not (column[1] == "id" and "INT" in column[2].upper())
        ]
        names = [column[1] for column in writable]
        markers = ", ".join("?" for _ in names)
        quoted = ", ".join(f'"{name}"' for name in names)
        for number in (1, 2):
            values = [
                (
                    0.5
                    if "FLOAT" in column[2].upper() or "REAL" in column[2].upper()
                    else (
                        number
                        if "INT" in column[2].upper() or "BOOL" in column[2].upper()
                        else f"seed-{number}-{table}-{column[1]}"
                    )
                )
                for column in writable
            ]
            connection.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({markers})', values)
        keys = [column[1] for column in columns if column[5]]
        assert keys
        snapshots[table] = table_rows(connection, table, keys)
    return snapshots


def table_rows(connection, table, keys):
    selected = ", ".join(f'"{key}"' for key in keys)
    return (
        connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0],
        connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY {selected}').fetchall(),
    )


def assert_seeded(connection, snapshots):
    for table, (count, primary_keys) in snapshots.items():
        keys = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")') if row[5]]
        assert table_rows(connection, table, keys) == (count, primary_keys)


@pytest.mark.parametrize("release", RELEASES)
def test_released_schema_converges_with_snapshot(release, tmp_path, monkeypatch):
    path = tmp_path / "released.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((SCHEMAS / f"{release}.sql").read_text())
        snapshots = seed_all_tables(connection)
        schema_before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    try:
        database_module.init_db()
        with sqlite3.connect(path) as connection:
            assert_seeded(connection, snapshots)
            version, backup = connection.execute(
                "SELECT version, backup_path FROM cao_schema_migrations WHERE state='applied'"
            ).fetchone()
            assert version == 1
        with sqlite3.connect(backup) as copy:
            assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert_seeded(copy, snapshots)
            assert (
                copy.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchall()
                == schema_before
            )
        assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(backup).stat().st_mode) == 0o600
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
        database_module.init_db()
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    finally:
        engine.dispose()
