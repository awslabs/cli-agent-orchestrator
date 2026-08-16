"""M7 Stage 2 store: ORM/migration parity, idempotence, restart, rollback.

The M7 admission table composes onto the canonical ``init_db`` lifecycle that
M3-D settled. It is purely additive: no M3-D column moves, and an older binary
that has never heard of M7 reads exactly the schema it had before.
"""

from __future__ import annotations

import sqlite3
import uuid

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
    "dispatch_state",
    "denial_reason",
    "detail",
    "receipt_digest",
    "created_at",
    "updated_at",
}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _migrated_db(tmp_path, monkeypatch):
    """A pre-M7 store brought forward by the additive migration alone."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite:///{path}")
    db_module._migrate_wait_message_admissions()
    return path


def test_migration_matches_the_orm_schema_column_for_column(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(str(path)) as conn:
        assert _columns(conn, "wait_message_admissions") == _ADMISSION_COLUMNS

    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    with sqlite3.connect(str(orm_path)) as conn:
        assert _columns(conn, "wait_message_admissions") == _ADMISSION_COLUMNS


def test_operation_and_message_identity_are_unique_in_both_schemas(tmp_path, monkeypatch):
    """Durable identity is what makes a retry a replay instead of a second effect."""
    path = _migrated_db(tmp_path, monkeypatch)
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    for store in (path, orm_path):
        with sqlite3.connect(str(store)) as conn:
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


def test_migration_is_idempotent(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    from cli_agent_orchestrator.clients import database as db_module

    db_module._migrate_wait_message_admissions()
    db_module._migrate_wait_message_admissions()
    with sqlite3.connect(str(path)) as conn:
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


def test_records_survive_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    operation_id = str(uuid.uuid4())
    owner = wa.WaitOwner(
        agent_id=str(uuid.uuid4()),
        incarnation_id="inc-restart",
        terminal_id="term-restart",
        generation=str(uuid.uuid4()),
    )
    first = wa.admit(
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
    engine.dispose()

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    record = wa.get_admission(operation_id)
    assert record["receipt_digest"] == first["receipt_digest"]
    assert record["dispatch_state"] == wa.DISPATCH_REFUSED
    engine.dispose()


def test_rollback_to_a_build_without_m7_leaves_the_older_schema_untouched(tmp_path, monkeypatch):
    """Additive means additive: no M3-D or M3-C table gains a column here."""
    path = _migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(str(path)) as conn:
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
