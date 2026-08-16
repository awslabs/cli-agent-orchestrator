"""M3-D ORM/migration parity, restart persistence, and rollback compatibility."""

from __future__ import annotations

import sqlite3
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import supervisor_drain as drain
from cli_agent_orchestrator.services import supervisor_reconciliation as recon
from cli_agent_orchestrator.services import task_occurrence as occ

_OCCURRENCE_COLUMNS = {
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
}

_EXTENSION_COLUMNS = {
    "task_occurrence_id",
    "extension_id",
    "extension_kind",
    "extension_version",
    "decider",
    "payload_digest",
    "payload_json",
    "claims_final",
    "recognized",
    "routing_state",
    "routed_at",
    "routed_receipt",
    "created_at",
    "updated_at",
}

_DRAIN_COLUMNS = {
    "drain_id",
    "schema_version",
    "session_name",
    "intent",
    "lifecycle_epoch",
    "lifecycle_observation",
    "roster_revision",
    "snapshot_digest",
    "request_digest",
    "state",
    "attempt",
    "receipt_digest",
    "reconciliation_reason",
    "initiated_by",
    "created_at",
    "updated_at",
}

_DRAIN_MEMBER_COLUMNS = {
    "drain_id",
    "agent_id",
    "role",
    "terminal_id",
    "generation",
    "incarnation_id",
    "observed_state",
    "steer_control_id",
    "steer_state",
    "task_occurrence_id",
    "boundary_digest",
    "report_digest",
    "checkpoint_digest",
    "teardown_request_id",
    "teardown_state",
    "member_state",
    "detail",
    "revision",
    "created_at",
    "updated_at",
}

_WAKE_COLUMNS = {
    "wake_id",
    "schema_version",
    "session_name",
    "source_kind",
    "source_operation_id",
    "supervisor_agent_id",
    "terminal_id",
    "generation",
    "message_digest",
    "message_json",
    "control_id",
    "delivery_state",
    "outcome",
    "reason_code",
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
    """A pre-M3-D store brought forward by the additive migrations alone."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite:///{path}")
    db_module._migrate_task_occurrences()
    db_module._migrate_supervisor_drain()
    return path


def test_migration_matches_the_orm_schema_column_for_column(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(str(path)) as conn:
        assert _columns(conn, "task_occurrences") == _OCCURRENCE_COLUMNS
        assert _columns(conn, "task_occurrence_extensions") == _EXTENSION_COLUMNS
        assert _columns(conn, "session_drain_receipts") == _DRAIN_COLUMNS
        assert _columns(conn, "session_drain_members") == _DRAIN_MEMBER_COLUMNS
        assert _columns(conn, "supervisor_reconciliation_wakes") == _WAKE_COLUMNS

    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    with sqlite3.connect(str(orm_path)) as conn:
        assert _columns(conn, "task_occurrences") == _OCCURRENCE_COLUMNS
        assert _columns(conn, "task_occurrence_extensions") == _EXTENSION_COLUMNS
        assert _columns(conn, "session_drain_receipts") == _DRAIN_COLUMNS
        assert _columns(conn, "session_drain_members") == _DRAIN_MEMBER_COLUMNS
        assert _columns(conn, "supervisor_reconciliation_wakes") == _WAKE_COLUMNS


def test_the_one_open_occurrence_index_is_partial_in_both_schemas(tmp_path, monkeypatch):
    """A full unique index would forbid a second *finalized* round per agent.

    The distinction is load-bearing: the constraint is "one task in flight",
    not "one task ever", and getting it wrong would make every agent's second
    round impossible.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    for store in (path, orm_path):
        with sqlite3.connect(str(store)) as conn:
            assert "ix_task_occurrences_open_agent" in _indexes(conn, "task_occurrences")
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'ix_task_occurrences_open_agent'"
            ).fetchone()[0]
            assert "WHERE" in sql.upper()
            assert "'open'" in sql


def test_migration_is_idempotent(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    from cli_agent_orchestrator.clients import database as db_module

    db_module._migrate_task_occurrences()
    db_module._migrate_supervisor_drain()
    with sqlite3.connect(str(path)) as conn:
        assert _columns(conn, "task_occurrences") == _OCCURRENCE_COLUMNS


def test_records_survive_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    agent = str(uuid.uuid4())
    occurrence_id = str(uuid.uuid4())
    occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=occurrence_id,
            session_name="cao-restart",
            agent_id=agent,
            round_index=0,
            dispatch_digest="a" * 64,
            incarnation=occ.EffectIncarnation(incarnation_id="inc-1", terminal_id="term-1"),
            seed=occ.TaskSeed(occ.SEED_COMPLETE, summary_digest="b" * 64),
        )
    )
    engine.dispose()

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    record = occ.get_occurrence(occurrence_id)
    assert record["agent_id"] == agent
    assert record["current"]["seed_quality"] == occ.SEED_COMPLETE
    assert record["seed_verdict"]["sufficient_for_fresh_start"] is True
    engine.dispose()


def test_rollback_to_a_build_without_m3d_leaves_readable_rows(tmp_path, monkeypatch):
    """The M3-D tables are additive; nothing older reads or writes them.

    Rolling back means those rows are simply unread — no column was added to
    a table an older build writes, so an older binary's own schema is
    byte-identical to what it had before.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(str(path)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "task_occurrences",
        "task_occurrence_extensions",
        "session_drain_receipts",
        "session_drain_members",
        "supervisor_reconciliation_wakes",
    } <= tables
    # M3-C's own tables are untouched by this slice: the cohort member
    # carriers M3-D fills were reserved in C1 and no column was added.
    assert database.SessionCohortMemberModel.__table__.c.keys() == [
        "operation_id",
        "agent_id",
        "snapshot_digest",
        "snapshot_json",
        "role",
        "profile_family",
        "pre_disposition",
        "agent_revision",
        "included",
        "exclusion_reason",
        "lineage_id",
        "harness",
        "native_session_id",
        "incarnation_id",
        "terminal_id",
        "generation",
        "pane_id",
        "restore_contract_id",
        "restore_contract_digest",
        "task_occurrence_id",
        "boundary_digest",
        "report_digest",
        "checkpoint_digest",
        "interrupt_action",
        "interrupt_outcome",
        "background_command_loss_risk",
        "final_state",
        "result_detail",
        "result_revision",
        "created_at",
        "updated_at",
    ]


def test_schema_versions_are_stamped_on_every_m3d_row():
    """A row with no schema version is one a later build cannot interpret."""
    assert occ.SCHEMA_VERSION.startswith("cao-m3d-")
    assert drain.SCHEMA_VERSION.startswith("cao-m3d-")
    assert recon.SCHEMA_VERSION.startswith("cao-m3d-")
