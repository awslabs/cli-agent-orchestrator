"""State the runtime channel keeps in SQLite, and what a pane may inherit (#745)."""

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as database
import cli_agent_orchestrator.constants as constants
from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.runtime_channel.token import TOKEN_ENV


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "cao.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    return path, engine


def test_a_row_records_the_runtime_that_runs_it(db):
    _, engine = db
    database.Base.metadata.create_all(engine)
    database.create_terminal(
        "aaaa0001", "cao-a", "dev-a", "mock_cli", "developer", runtime_id="rt-1"
    )
    database.create_terminal("aaaa0002", "cao-b", "dev-b", "mock_cli", "developer")

    assert database.get_terminal_metadata("aaaa0001")["runtime_id"] == "rt-1"
    assert database.get_terminal_metadata("aaaa0002")["runtime_id"] is None
    assert database.session_is_remote("cao-a") is True
    assert database.session_is_remote("cao-b") is False
    assert database.list_terminal_ids_on_runtime("rt-1") == ["aaaa0001"]
    assert database.list_terminal_ids_on_runtime("rt-2") == []


def test_an_existing_database_gains_the_column_once(db):
    path, _ = db
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT, tmux_window TEXT,"
            " provider TEXT, agent_profile TEXT, last_active DATETIME)"
        )
        conn.execute("INSERT INTO terminals (id, tmux_session) VALUES ('old00001', 'cao-old')")

    database._migrate_terminals_schema()
    database._migrate_terminals_schema()

    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(terminals)")]
        assert columns.count("runtime_id") == 1
        assert conn.execute("SELECT runtime_id FROM terminals").fetchall() == [(None,)]


def test_a_runtime_creates_only_the_pane_tables(db):
    _, engine = db
    database.init_runtime_db()
    assert set(inspect(engine).get_table_names()) == set(database.RUNTIME_TABLES)


def test_the_runtime_token_never_reaches_a_pane():
    assert TmuxClient._is_blocked_env_key(TOKEN_ENV) is True
    env = {}
    TmuxClient._merge_extra_env(env, {TOKEN_ENV: "secret", "OK": "y"})
    assert env == {"OK": "y"}
