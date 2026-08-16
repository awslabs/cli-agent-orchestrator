"""COND-0230 M10-A store: ORM/migration parity, idempotence, restart.

The M10 route-observation operation table composes onto the canonical
``init_db`` lifecycle that M3-D settled. It is purely additive: it creates one
new table, adds no column to any existing one, and an older binary that has
never heard of M10 reads exactly the schema it had before.
"""

from __future__ import annotations

import contextlib
import gc
import sqlite3
import uuid
import warnings

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import route_observation as ro
from cli_agent_orchestrator.services.canonical_json import canonical_sha256

_RO_COLUMNS = {
    "operation_id",
    "schema_version",
    "request_digest",
    "target_terminal_id",
    "target_generation",
    "native_session_id",
    "provider",
    "provider_version",
    "provider_artifact_sha256",
    "requester_terminal_id",
    "requester_generation",
    "state",
    "detail",
    "final_event_json",
    "final_event_digest",
    "receipt_json",
    "receipt_digest",
    "inbox_message_id",
    "created_at",
    "updated_at",
}

_TARGET_INDEX = "ix_route_observation_operations_active_target"


@contextlib.contextmanager
def _sqlite(path):
    """A sqlite3 connection that is actually closed on exit."""
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _migrated_db(tmp_path, monkeypatch):
    """A pre-M10 store brought forward by the additive migration alone."""
    path = tmp_path / "legacy.db"
    with _sqlite(path) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO terminals (id) VALUES ('kept-row')")
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite:///{path}")
    db_module._migrate_route_observation_operations()
    return path


def _orm_schema(path):
    """Build the ORM schema at ``path`` and dispose the engine."""
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    return path


def test_migration_matches_the_orm_schema_column_for_column(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    with _sqlite(path) as conn:
        assert _columns(conn, "route_observation_operations") == _RO_COLUMNS

    with _sqlite(_orm_schema(tmp_path / "orm.db")) as conn:
        assert _columns(conn, "route_observation_operations") == _RO_COLUMNS


def test_active_owner_uniqueness_exists_in_both_schemas(tmp_path, monkeypatch):
    """The one nonterminal owner per exact target tuple is a storage fact."""
    path = _migrated_db(tmp_path, monkeypatch)
    for store in (path, _orm_schema(tmp_path / "orm.db")):
        with _sqlite(store) as conn:
            names = _indexes(conn, "route_observation_operations")
            unique = {
                row[1]
                for row in conn.execute("PRAGMA index_list(route_observation_operations)")
                if row[2]
            }
            assert _TARGET_INDEX in names
            assert _TARGET_INDEX in unique
            # The partial WHERE is what scopes uniqueness to nonterminal rows.
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (_TARGET_INDEX,),
            ).fetchone()
            assert "WHERE state = 'requested'" in ddl[0]


def test_the_index_rejects_a_second_active_owner_at_the_storage_layer(tmp_path, monkeypatch):
    """Two concurrent claims for one tuple can never both stay ``requested``.

    The service also detects the owner before inserting, but the storage
    layer is where the guarantee is actually held: whatever process wins the
    write first, the second ``requested`` row for the same exact tuple cannot
    exist.
    """
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(f"sqlite:///{tmp_path / 'uniqueness.db'}")
    try:
        Base.metadata.create_all(bind=engine)
        with sessionmaker(bind=engine)() as session:
            fields = dict(
                schema_version=ro.SCHEMA_VERSION,
                request_digest="e" * 64,
                target_terminal_id="t",
                target_generation="g",
                native_session_id="s",
                provider="codex",
                provider_version="0.147",
                provider_artifact_sha256="f" * 64,
                requester_terminal_id="r",
                requester_generation="rg",
                state=ro.STATE_REQUESTED,
                created_at="2026-08-16T00:00:00Z",
                updated_at="2026-08-16T00:00:00Z",
            )
            session.add(database.RouteObservationOperationModel(operation_id="op-a", **fields))
            session.commit()
            with pytest.raises(IntegrityError):
                session.add(database.RouteObservationOperationModel(operation_id="op-b", **fields))
                session.commit()
    finally:
        engine.dispose()


def test_no_speculative_indexes_are_added(tmp_path, monkeypatch):
    """Only the indexes a read/ownership path actually uses exist.

    ``inbox_message_id`` is deliberately not unique — no query reads by it and
    each operation mints its own inbox row.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    for store in (path, _orm_schema(tmp_path / "orm.db")):
        with _sqlite(store) as conn:
            indexes = _indexes(conn, "route_observation_operations")
        assert indexes == {_TARGET_INDEX} | {
            "sqlite_autoindex_route_observation_operations_1"  # PRIMARY KEY
        }


def test_migration_is_idempotent_and_preserves_existing_rows(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    from cli_agent_orchestrator.clients import database as db_module

    db_module._migrate_route_observation_operations()
    db_module._migrate_route_observation_operations()
    with _sqlite(path) as conn:
        assert _columns(conn, "route_observation_operations") == _RO_COLUMNS
        # The pre-existing row and table survive the additive step untouched.
        assert conn.execute("SELECT id FROM terminals").fetchall() == [("kept-row",)]


def test_init_db_runs_the_route_observation_migration(tmp_path, monkeypatch):
    called: list[str] = []
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(
        db_module, "_migrate_route_observation_operations", lambda: called.append("m10")
    )
    for name in dir(db_module):
        if name.startswith("_migrate_") and name != "_migrate_route_observation_operations":
            monkeypatch.setattr(db_module, name, lambda *a, **k: None)
    monkeypatch.setattr(db_module, "_restrict_db_file_permissions", lambda: None)
    monkeypatch.setattr(db_module.Base.metadata, "create_all", lambda **kwargs: None)
    db_module.init_db()
    assert called == ["m10"]


def test_the_rollback_route_leaves_every_other_table_untouched(tmp_path, monkeypatch):
    """Additive means additive: no M3-D, callback, or inbox table changes here."""
    path = _migrated_db(tmp_path, monkeypatch)
    with _sqlite(path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "route_observation_operations" in tables
    # Only the table this migration owns was created.
    assert tables == {"terminals", "route_observation_operations"}
    assert "inbox" not in tables


def _restart_cycle(path, monkeypatch):
    """Claim + terminalize on one engine, re-read on a fresh engine."""
    request = _request()
    with _engine(path) as engine:
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(
            database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        ro.claim(request)
        event = {"kind": "restart-probe"}
        written = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=event,
            receipt={
                "schema": ro.RECEIPT_SCHEMA,
                "kind": "observed-closed",
                "operation_id": request.operation_id,
                "request_digest": request.request_digest(),
                "requester_terminal_id": request.requester_terminal_id,
                "requester_generation": request.requester_generation,
                "target_terminal_id": request.target_terminal_id,
                "target_generation": request.target_generation,
                "native_session_id": request.native_session_id,
                "provider": request.provider,
                "provider_version": request.provider_version,
                "provider_artifact_sha256": request.provider_artifact_sha256,
                "observation": {"kind": "provider-surface", "observed_at": "restart"},
                "close_proof": {"kind": "owned-close", "outcome": "closed"},
                "final_event_digest": canonical_sha256(event),
                "committed_at": "2026-08-16T00:00:00Z",
            },
        )

    with _engine(path) as engine:
        monkeypatch.setattr(
            database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        reread = ro.get(request.operation_id)
        assert reread is not None
    return written, reread


@contextlib.contextmanager
def _engine(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    try:
        yield engine
    finally:
        engine.dispose()


def _request():
    return ro.RouteObservationRequest(
        operation_id=str(uuid.uuid4()),
        target_terminal_id="term-restart",
        target_generation="gen-restart",
        native_session_id="sess-restart",
        provider="codex",
        provider_version="0.147.0",
        provider_artifact_sha256="d" * 64,
        requester_terminal_id="term-requester",
        requester_generation="gen-requester",
    )


def test_records_survive_a_restart(tmp_path, monkeypatch):
    written, reread = _restart_cycle(tmp_path / "restart.db", monkeypatch)
    assert reread["operation_id"] == written["operation_id"]
    assert reread["state"] == ro.RESULT_OBSERVED_CLOSED
    assert reread["final_event_digest"] == written["final_event_digest"]
    assert reread["receipt_digest"] == written["receipt_digest"]
    assert reread["inbox_message_id"] == written["inbox_message_id"]
    assert reread["created_at"] == written["created_at"]


def test_the_restart_path_leaks_no_sqlite_connections(tmp_path, monkeypatch):
    """Every connection this module opens is closed before the test ends."""
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        _restart_cycle(tmp_path / "leak.db", monkeypatch)
        _migrated_db(tmp_path, monkeypatch)
        gc.collect()
    leaked = [str(entry.message) for entry in caught if entry.category is ResourceWarning]
    assert leaked == []


def test_wait_admission_columns_do_not_leak_into_the_m10_table(tmp_path, monkeypatch):
    """The table is the operation store, not a wait-admission message store."""
    path = _migrated_db(tmp_path, monkeypatch)
    with _sqlite(path) as conn:
        assert _columns(conn, "route_observation_operations") == _RO_COLUMNS
    assert "admission_id" not in _RO_COLUMNS
    assert "message_json" not in _RO_COLUMNS
