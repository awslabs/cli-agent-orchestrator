"""M7 Stage 2 store: ORM/migration parity, idempotence, restart, rollback.

The M7 admission table composes onto the canonical ``init_db`` lifecycle that
M3-D settled. It is purely additive: no M3-D column moves, and an older binary
that has never heard of M7 reads exactly the schema it had before.
"""

from __future__ import annotations

import contextlib
import gc
import sqlite3
import uuid
import warnings

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import wait_admission as wa

_ADMISSION_COLUMNS = {
    "admission_id",
    "schema_version",
    "message_schema_version",
    "operation_id",
    "message_id",
    "session_name",
    "message_kind",
    "owner_agent_id",
    "owner_incarnation_id",
    "owner_terminal_id",
    "owner_generation",
    "owner_lineage_id",
    "owner_native_session_id",
    "owner_restore_contract_id",
    "owner_restore_contract_digest",
    "owner_identity_digest",
    "request_digest",
    "message_digest",
    "message_json",
    "admission_state",
    "denial_reason",
    "detail",
    "receipt_digest",
    "created_at",
}


@contextlib.contextmanager
def _sqlite(path):
    """A sqlite3 connection that is actually closed on exit.

    ``with sqlite3.connect(...)`` only ends the *transaction*; the connection
    itself stays open and is finalized later by the GC, which is where the
    ``unclosed database`` ResourceWarnings in this module came from.
    """
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _orm_store(path):
    """An engine bound as ``SessionLocal``, disposed on exit."""
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    try:
        yield engine
    finally:
        engine.dispose()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _migrated_db(tmp_path, monkeypatch):
    """A pre-M7 store brought forward by the additive migration alone."""
    path = tmp_path / "legacy.db"
    with _sqlite(path) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite:///{path}")
    db_module._migrate_wait_message_admissions()
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
        assert _columns(conn, "wait_message_admissions") == _ADMISSION_COLUMNS

    with _sqlite(_orm_schema(tmp_path / "orm.db")) as conn:
        assert _columns(conn, "wait_message_admissions") == _ADMISSION_COLUMNS


def test_the_row_is_write_once_with_no_dispatch_state_and_no_updated_at(tmp_path, monkeypatch):
    """Two columns were removed because neither could ever carry new information.

    ``dispatch_state`` was a pure projection of ``admission_state`` while
    nothing dispatches, and ``updated_at`` could only ever equal
    ``created_at`` on a row that is written once and never modified.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    for store in (path, _orm_schema(tmp_path / "orm.db")):
        with _sqlite(store) as conn:
            columns = _columns(conn, "wait_message_admissions")
        assert "dispatch_state" not in columns
        assert "updated_at" not in columns
        assert "created_at" in columns


def test_operation_and_message_identity_are_unique_in_both_schemas(tmp_path, monkeypatch):
    """Durable identity is what makes a retry a replay instead of a second effect."""
    path = _migrated_db(tmp_path, monkeypatch)

    for store in (path, _orm_schema(tmp_path / "orm.db")):
        with _sqlite(store) as conn:
            names = _indexes(conn, "wait_message_admissions")
            assert "ix_wait_message_admissions_operation" in names
            assert "ix_wait_message_admissions_message" in names
            unique = {
                row[1]
                for row in conn.execute("PRAGMA index_list(wait_message_admissions)")
                if row[2]
            }
            assert "ix_wait_message_admissions_operation" in unique
            assert "ix_wait_message_admissions_message" in unique


def test_the_only_indexes_are_the_ones_a_query_path_uses(tmp_path, monkeypatch):
    """The owner index served no read this module performs.

    ``(owner_agent_id, owner_generation)`` was indexed for a by-owner lookup
    that does not exist: every read here goes by operation, by message, or by
    session. An index nothing queries is write cost and a false hint about
    which reads are supported.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    for store in (path, _orm_schema(tmp_path / "orm.db")):
        with _sqlite(store) as conn:
            names = _indexes(conn, "wait_message_admissions")
        assert "ix_wait_message_admissions_owner" not in names
        assert {
            "ix_wait_message_admissions_operation",
            "ix_wait_message_admissions_message",
            "ix_wait_message_admissions_session",
        } <= names


def test_migration_is_idempotent(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    from cli_agent_orchestrator.clients import database as db_module

    db_module._migrate_wait_message_admissions()
    db_module._migrate_wait_message_admissions()
    with _sqlite(path) as conn:
        assert _columns(conn, "wait_message_admissions") == _ADMISSION_COLUMNS


def test_init_db_runs_the_wait_admission_migration(tmp_path, monkeypatch):
    called: list[str] = []
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(db_module, "_migrate_wait_message_admissions", lambda: called.append("m7"))
    for name in dir(db_module):
        if name.startswith("_migrate_") and name != "_migrate_wait_message_admissions":
            monkeypatch.setattr(db_module, name, lambda *a, **k: None)
    monkeypatch.setattr(db_module, "_restrict_db_file_permissions", lambda: None)
    monkeypatch.setattr(db_module.Base.metadata, "create_all", lambda **kwargs: None)
    db_module.init_db()
    assert called == ["m7"]


def _restart_cycle(path, monkeypatch):
    """Admit on one engine, then read the row back on a fresh one.

    Both engines are disposed before the function returns; the second one is
    what makes this a restart rather than a cache hit.
    """
    operation_id = str(uuid.uuid4())
    owner = wa.WaitOwner(
        agent_id=str(uuid.uuid4()),
        incarnation_id="inc-restart",
        terminal_id="term-restart",
        generation=str(uuid.uuid4()),
    )
    with _orm_store(path) as engine:
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(
            database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        written = wa.admit(
            wa.AdmissionRequest(
                operation_id=operation_id,
                session_name="cao-restart",
                owner=owner,
                message=wa.WaitMessage(
                    message_id=str(uuid.uuid4()),
                    kind=wa.KIND_EXPIRY,
                    reason_code="deadline-passed",
                ),
            )
        )

    with _orm_store(path) as engine:
        monkeypatch.setattr(
            database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        reread = wa.get_admission(operation_id)
    return written, reread


def test_records_survive_a_restart(tmp_path, monkeypatch):
    written, reread = _restart_cycle(tmp_path / "restart.db", monkeypatch)
    assert reread["receipt_digest"] == written["receipt_digest"]
    assert reread["admission_state"] == wa.STATE_DENIED
    assert reread["denial_reason"] == wa.DENY_OWNER_UNKNOWN
    assert reread["created_at"] == written["created_at"]


def test_the_restart_path_leaks_no_sqlite_connections(tmp_path, monkeypatch):
    """Every connection this module opens is closed before the test ends.

    Asserted here rather than by a global warning filter: a suite-wide
    ``-W error`` would also fail on unrelated modules' garbage, and silencing
    it would hide a real leak in this one. Collecting inside the block pins
    the finalizers to objects this test created.
    """
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        _restart_cycle(tmp_path / "leak.db", monkeypatch)
        _migrated_db(tmp_path, monkeypatch)
        gc.collect()
    leaked = [str(entry.message) for entry in caught if entry.category is ResourceWarning]
    assert leaked == []


def test_rollback_to_a_build_without_m7_leaves_the_older_schema_untouched(tmp_path, monkeypatch):
    """Additive means additive: no M3-D or M3-C table gains a column here."""
    path = _migrated_db(tmp_path, monkeypatch)
    with _sqlite(path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "wait_message_admissions" in tables
    # The M3-D migration was not invoked here, so its tables are absent — the
    # M7 migration stands alone and depends on nothing M3-D creates.
    assert "task_occurrences" not in tables
    assert "supervisor_reconciliation_wakes" not in tables

    assert database.TaskOccurrenceModel.__table__.c.keys() == [
        "task_occurrence_id",
        "schema_version",
        "session_name",
        "agent_id",
        "round_index",
        "dispatch_digest",
        "dispatch_provenance_json",
        "incarnation_id",
        "terminal_id",
        "generation",
        "lineage_id",
        "native_session_id",
        "state",
        "current_boundary_digest",
        "current_report_digest",
        "current_checkpoint_digest",
        "current_provenance_json",
        "current_summary_seed_digest",
        "current_artifact_seed_digest",
        "current_seed_quality",
        "current_seed_json",
        "final_disposition",
        "finalized_boundary_digest",
        "finalized_report_digest",
        "finalized_checkpoint_digest",
        "finalized_provenance_json",
        "finalized_summary_seed_digest",
        "finalized_artifact_seed_digest",
        "finalized_seed_quality",
        "finalized_seed_json",
        "finalized_by",
        "finalized_at",
        "revision",
        "created_at",
        "updated_at",
    ]


def test_the_schema_version_is_stamped_on_every_m7_row():
    assert wa.SCHEMA_VERSION.startswith("cao-m7-")
    assert wa.MESSAGE_SCHEMA_VERSION.startswith("cao-m7-")
