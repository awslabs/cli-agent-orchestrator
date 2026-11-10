"""``terminals.identity_verified`` on databases that predate it (#1074).

A database at the schema before the column must open through the real
``init_db()`` and gain it as a nullable column with no default: every existing
row reads ``None`` (a legacy row the Kiro input gate grandfathers), never
``False`` (a row this version wrote and has not verified).
"""

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from cli_agent_orchestrator.clients import database as db_mod


def _base_schema_db(path):
    """Every current table, with ``terminals`` created without the column."""
    table = db_mod.Base.metadata.tables["terminals"]
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    base_ddl = ddl.replace("\tidentity_verified BOOLEAN, \n", "")
    assert base_ddl != ddl and "identity_verified" not in base_ddl
    engine = create_engine(f"sqlite:///{path}")
    try:
        db_mod.Base.metadata.create_all(
            engine, tables=[t for t in db_mod.Base.metadata.sorted_tables if t is not table]
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(base_ddl)
    finally:
        engine.dispose()
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider, agent_profile, "
            "deferred_init_failure) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("aaaa1111", "cao-s", "developer-0", "kiro_cli", "developer", None),
                (
                    "bbbb2222",
                    "cao-s",
                    "developer-1",
                    "kiro_cli",
                    "developer",
                    '{"phase": "deferred_init", "kind": "interrupted_init"}',
                ),
                ("cccc3333", "cao-s", "reviewer-0", "claude_code", "reviewer", None),
            ],
        )


@pytest.fixture
def base_schema_home(tmp_path, monkeypatch):
    """Point the module's engine, sessions and migrators at a base-schema file."""
    path = tmp_path / "base.db"
    _base_schema_db(path)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=False)
    try:
        yield path
    finally:
        engine.dispose()


def _column(path):
    with sqlite3.connect(str(path)) as conn:
        rows = conn.execute("PRAGMA table_info(terminals)").fetchall()
    return [r for r in rows if r[1] == "identity_verified"]


def test_a_base_schema_database_opens_and_gains_a_nullable_column(base_schema_home):
    assert _column(base_schema_home) == []

    db_mod.init_db()
    db_mod.init_db()  # a restart: idempotent

    [(_, _, column_type, notnull, default, _)] = _column(base_schema_home)
    assert column_type == "BOOLEAN"
    assert notnull == 0 and default is None


def test_existing_rows_read_none_and_keep_their_failures(base_schema_home):
    db_mod.init_db()

    for terminal_id in ("aaaa1111", "bbbb2222", "cccc3333"):
        assert db_mod.get_terminal_metadata(terminal_id)["identity_verified"] is None
    failed = db_mod.get_terminal_metadata("bbbb2222")
    assert failed["deferred_init_failure"] == {"phase": "deferred_init", "kind": "interrupted_init"}


def test_new_rows_store_what_the_create_passes_and_only_true_is_ever_marked(base_schema_home):
    db_mod.init_db()

    kiro = db_mod.create_terminal("dddd4444", "cao-s", "w-4", "kiro_cli", identity_verified=False)
    other = db_mod.create_terminal("eeee5555", "cao-s", "w-5", "claude_code")

    assert kiro["identity_verified"] is False
    assert other["identity_verified"] is None
    assert db_mod.get_terminal_metadata("dddd4444")["identity_verified"] is False
    assert db_mod.get_terminal_metadata("eeee5555")["identity_verified"] is None
    assert db_mod.mark_terminal_identity_verified("dddd4444") is True
    assert db_mod.get_terminal_metadata("dddd4444")["identity_verified"] is True
    assert db_mod.mark_terminal_identity_verified("ffff6666") is False
