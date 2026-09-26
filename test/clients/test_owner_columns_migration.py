"""The ``owner`` columns on ``flows`` and ``terminals`` (#745, criterion 14).

Two properties matter here and neither is about the ALTER succeeding:

1. An upgrade of a database that already holds flows and terminals leaves those
   rows with a NULL owner and keeps working. NULL reads as *unknown*, which the
   revocation gate treats as not-revoked, so a server that upgrades mid-week does
   not silently stop firing every schedule it already had.
2. The owner is its OWN column rather than a key in ``terminals.metadata``. That
   dict is written by the running agent through the ``update_metadata`` MCP tool,
   so an owner stored there would be an owner the agent could rewrite -- and it
   is read to decide whether work may start.
"""

import sqlite3

import pytest

from cli_agent_orchestrator.clients import database as db_mod

PRE_745_TERMINALS = (
    "CREATE TABLE terminals ("
    "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, tmux_window TEXT NOT NULL, "
    "provider TEXT NOT NULL, agent_profile TEXT, working_directory TEXT, "
    "allowed_tools TEXT, shell_command TEXT, caller_id TEXT, engine TEXT, "
    '"group" TEXT, "metadata" TEXT, last_active TIMESTAMP)'
)

PRE_745_FLOWS = (
    "CREATE TABLE flows ("
    "name TEXT PRIMARY KEY, file_path TEXT NOT NULL, schedule TEXT NOT NULL, "
    "agent_profile TEXT NOT NULL, provider TEXT, script TEXT, "
    "last_run TIMESTAMP, next_run TIMESTAMP, enabled BOOLEAN)"
)


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database in its pre-#745 shape, with one flow and one terminal in it."""
    db_file = tmp_path / "pre745.db"
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(PRE_745_TERMINALS)
        conn.execute(PRE_745_FLOWS)
        conn.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
            "VALUES ('abc12345', 'cao-s', 'w-0', 'kiro_cli')"
        )
        conn.execute(
            "INSERT INTO flows (name, file_path, schedule, agent_profile, enabled) "
            "VALUES ('nightly', '/tmp/f.md', '0 2 * * *', 'developer', 1)"
        )
        conn.commit()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False)
    return db_file


def _columns(db_file, table):
    with sqlite3.connect(str(db_file)) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_existing_rows_gain_a_null_owner_rather_than_an_invented_one(legacy_db):
    db_mod._migrate_add_flow_owner()
    db_mod._migrate_add_terminal_owner()

    with sqlite3.connect(str(legacy_db)) as conn:
        flows = conn.execute("SELECT name, owner FROM flows").fetchall()
        terminals = conn.execute("SELECT id, owner FROM terminals").fetchall()
    # NOT backfilled with the local principal: nobody recorded an owner for
    # these, and writing one would assert something untrue.
    assert flows == [("nightly", None)]
    assert terminals == [("abc12345", None)]


def test_both_migrations_are_idempotent(legacy_db):
    for _ in range(2):
        db_mod._migrate_add_flow_owner()
        db_mod._migrate_add_terminal_owner()

    assert _columns(legacy_db, "flows").count("owner") == 1
    assert _columns(legacy_db, "terminals").count("owner") == 1


def test_owner_is_not_stored_in_the_agent_writable_metadata_bag(legacy_db):
    """Pins the separation, not just the column's existence.

    ``update_metadata`` lets a running agent replace ``terminals.metadata``
    wholesale. If the owner lived in there, an agent could name its own owner --
    so a future refactor that "simplifies" it back into metadata must fail here.
    """
    db_mod._migrate_add_terminal_owner()
    assert "owner" in _columns(legacy_db, "terminals")

    import inspect

    source = inspect.getsource(db_mod.update_terminal_metadata)
    assert "owner" not in source, "the metadata writer must not touch the owner column"


def test_a_migrated_row_reads_back_through_the_application_path(legacy_db, monkeypatch):
    """Read through ``get_terminal_metadata``, not raw SQL.

    The gate resolves ownership through that function, so an upgraded row has to
    answer ``owner=None`` there -- a column nobody surfaces would leave the gate
    reading nothing at all.
    """
    import sqlalchemy

    db_mod._migrate_add_terminal_owner()
    engine = sqlalchemy.create_engine(f"sqlite:///{legacy_db}")
    monkeypatch.setattr(db_mod, "engine", engine, raising=False)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sqlalchemy.orm.sessionmaker(bind=engine),
        raising=False,
    )

    row = db_mod.get_terminal_metadata("abc12345")
    assert row is not None
    assert row["owner"] is None


def test_owner_round_trips_when_the_server_records_one(legacy_db, monkeypatch):
    import sqlalchemy

    db_mod._migrate_add_terminal_owner()
    engine = sqlalchemy.create_engine(f"sqlite:///{legacy_db}")
    monkeypatch.setattr(db_mod, "engine", engine, raising=False)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sqlalchemy.orm.sessionmaker(bind=engine),
        raising=False,
    )

    created = db_mod.create_terminal(
        "owned001",
        "cao-s",
        "w-1",
        "kiro_cli",
        owner="https://idp.example/#auth0|abc",
    )
    assert created["owner"] == "https://idp.example/#auth0|abc"
    assert db_mod.get_terminal_metadata("owned001")["owner"] == "https://idp.example/#auth0|abc"
