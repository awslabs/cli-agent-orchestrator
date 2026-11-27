"""Open real release DDL, preserving rows and taking a checked snapshot."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as database_module


SCHEMAS = Path(__file__).resolve().parents[1] / "fixtures/db_schemas"
RELEASES = (
    "v2.5.0",
    "v2.5.1",
    "v2.5.2",
    "v2.5.4",
    "v2.5.5",
    "v2.4.1-to-v2.5.5",
    "3.0-beta1",
)


@pytest.mark.parametrize("release", RELEASES)
def test_released_schema_converges_with_snapshot(release, tmp_path, monkeypatch):
    path = tmp_path / "released.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((SCHEMAS / f"{release}.sql").read_text())
        connection.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider, agent_profile) "
            "VALUES ('fixture-1', 'fixture', 'window', 'codex', 'developer')"
        )
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    try:
        database_module.init_db()
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT id FROM terminals").fetchall() == [("fixture-1",)]
            version, backup = connection.execute(
                "SELECT version, backup_path FROM cao_schema_migrations WHERE state='applied'"
            ).fetchone()
            assert version == 1
        with sqlite3.connect(backup) as copy:
            assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert copy.execute("SELECT id FROM terminals").fetchall() == [("fixture-1",)]
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
        database_module.init_db()
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    finally:
        engine.dispose()
