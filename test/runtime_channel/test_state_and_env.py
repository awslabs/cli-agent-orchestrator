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


def test_a_stale_row_sweep_never_takes_a_remote_row(db):
    # The sweep drops rows a dead LOCAL tmux session left behind (local create,
    # flow recycling). A row whose terminal runs in a runtime is not one of them.
    _, engine = db
    database.Base.metadata.create_all(engine)
    database.create_terminal("aaaa0001", "cao-x", "dev-a", "mock_cli", "developer")
    database.create_terminal(
        "aaaa0002", "cao-x", "dev-b", "mock_cli", "developer", runtime_id="rt-1"
    )

    assert database.delete_terminals_by_session("cao-x") == 1
    assert [t["id"] for t in database.list_terminals_by_session("cao-x")] == ["aaaa0002"]


def test_the_retention_sweep_leaves_remote_rows_alone(db, monkeypatch):
    # An idle remote agent may be older than the retention window; its row is
    # the only handle on it, and the sweep cannot stop it in its runtime.
    from datetime import datetime, timedelta

    import cli_agent_orchestrator.services.cleanup_service as cleanup_service

    _, engine = db
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(cleanup_service, "SessionLocal", database.SessionLocal)
    database.create_terminal("aaaa0001", "cao-a", "dev-a", "mock_cli", "developer")
    database.create_terminal(
        "aaaa0002", "cao-b", "dev-b", "mock_cli", "developer", runtime_id="rt-1"
    )
    long_ago = datetime.now() - timedelta(days=3650)
    with database.SessionLocal() as s:
        s.query(database.TerminalModel).update({"last_active": long_ago})
        s.commit()

    cleanup_service.cleanup_old_data()

    assert database.get_terminal_metadata("aaaa0001") is None
    assert database.get_terminal_metadata("aaaa0002") is not None


def test_a_runtime_creates_only_the_pane_tables(db):
    _, engine = db
    database.init_runtime_db()
    assert set(inspect(engine).get_table_names()) == set(database.RUNTIME_TABLES)


def test_the_runtime_token_never_reaches_a_pane():
    assert TmuxClient._is_blocked_env_key(TOKEN_ENV) is True
    env = {}
    TmuxClient._merge_extra_env(env, {TOKEN_ENV: "secret", "OK": "y"})
    assert env == {"OK": "y"}


def test_nor_does_the_path_of_its_file():
    from cli_agent_orchestrator.runtime_channel.token import TOKEN_FILE_ENV

    assert TmuxClient._is_blocked_env_key(TOKEN_FILE_ENV) is True


def test_rows_whose_pane_died_with_the_last_container_are_dropped(db, monkeypatch):
    # The runtime's state volume outlives the container; its tmux does not.
    from cli_agent_orchestrator.backends import registry as backend_registry
    from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

    _, engine = db
    database.init_runtime_db()
    database.create_terminal("aaaa0001", "cao-live", "dev-a", "mock_cli", "developer")
    database.create_terminal("aaaa0002", "cao-gone", "dev-b", "mock_cli", "developer")

    class Tmux:
        def session_exists_strict(self, name):
            return name == "cao-live"

    monkeypatch.setattr(backend_registry, "_backend", Tmux())
    assert bridge_mod._drop_stale_rows() == 1
    assert [t["id"] for t in database.list_all_terminals()] == ["aaaa0001"]


def test_rows_are_kept_when_tmux_cannot_be_asked(db, monkeypatch):
    from cli_agent_orchestrator.backends import registry as backend_registry
    from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

    database.init_runtime_db()
    database.create_terminal("aaaa0001", "cao-a", "dev-a", "mock_cli", "developer")

    class Broken:
        def session_exists_strict(self, name):
            raise RuntimeError("tmux did not answer")

    monkeypatch.setattr(backend_registry, "_backend", Broken())
    assert bridge_mod._drop_stale_rows() == 0
    assert [t["id"] for t in database.list_all_terminals()] == ["aaaa0001"]


def test_an_idempotent_retry_is_answered_even_with_local_execution_off(monkeypatch):
    # A terminal created under a key before the server stopped running agents:
    # retrying that create recovers it, rather than being refused.
    import asyncio
    from types import SimpleNamespace

    from cli_agent_orchestrator.services import terminal_service as ts

    fingerprint = ts._request_fingerprint(
        "mock_cli", "developer", None, None, None, None, False, None, None, None, None, None, None
    )
    monkeypatch.setattr(
        ts,
        "get_idempotency_record",
        lambda key: SimpleNamespace(terminal_id="aaaa0001", request_fingerprint=fingerprint),
    )
    monkeypatch.setattr(
        ts,
        "get_terminal",
        lambda tid: {
            "id": tid,
            "name": "developer-aaaa",
            "provider": "mock_cli",
            "session_name": "cao-aaaa",
            "agent_profile": "developer",
            "status": "idle",
        },
    )
    monkeypatch.setenv("CAO_LOCAL_EXECUTION", "0")
    terminal = asyncio.run(
        ts.create_terminal(provider="mock_cli", agent_profile="developer", idempotency_key="k1")
    )
    assert terminal.id == "aaaa0001"
