"""C1-C2 cohort-journal ORM/migration parity and restart persistence."""

from __future__ import annotations

import sqlite3
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import stable_agent_roster as roster

_OPERATION_COLUMNS = {
    "operation_id",
    "request_digest",
    "schema_version",
    "session_name",
    "operation_kind",
    "requested_mode",
    "current_mode",
    "initiator_kind",
    "initiated_by",
    "source_operation_id",
    "resume_target",
    "lifecycle_epoch",
    "lifecycle_observation",
    "roster_revision",
    "member_snapshot_digest",
    "state",
    "state_epoch",
    "request_json",
    "created_at",
    "updated_at",
}

_MEMBER_COLUMNS = {
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
}

_TRANSITION_COLUMNS = {
    "transition_id",
    "operation_id",
    "transition_digest",
    "transition_json",
    "from_state",
    "to_state",
    "from_mode",
    "to_mode",
    "from_state_epoch",
    "actor",
    "reason",
    "receipt_digest",
    "created_at",
}

_INDEXES = {
    "ix_session_cohort_operations_slot",
    "ix_session_cohort_operations_session",
    "ix_session_cohort_members_agent",
    "ix_session_cohort_transitions_epoch",
    "ix_session_cohort_transitions_operation",
}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'ix_session_cohort%'"
        )
    }


def test_create_all_and_raw_migration_produce_the_closed_schema(tmp_path, monkeypatch):
    fresh_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{fresh_path}")
    Base.metadata.create_all(bind=engine)
    with sqlite3.connect(str(fresh_path)) as conn:
        assert _OPERATION_COLUMNS <= _columns(conn, "session_cohort_operations")
        assert _MEMBER_COLUMNS <= _columns(conn, "session_cohort_members")
        assert _TRANSITION_COLUMNS <= _columns(conn, "session_cohort_transitions")
        assert _INDEXES <= _indexes(conn)
    engine.dispose()

    migrated_path = tmp_path / "migrated.db"
    with sqlite3.connect(str(migrated_path)) as conn:
        conn.execute("CREATE TABLE prior_data (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO prior_data VALUES ('keep', 'untouched')")
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", migrated_path)
    database._migrate_session_cohort_journal()
    database._migrate_session_cohort_journal()
    with sqlite3.connect(str(migrated_path)) as conn:
        assert conn.execute("SELECT value FROM prior_data WHERE id='keep'").fetchone() == (
            "untouched",
        )
        assert _OPERATION_COLUMNS <= _columns(conn, "session_cohort_operations")
        assert _MEMBER_COLUMNS <= _columns(conn, "session_cohort_members")
        assert _TRANSITION_COLUMNS <= _columns(conn, "session_cohort_transitions")
        assert _INDEXES <= _indexes(conn)


def test_operation_members_and_transitions_survive_engine_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "restart.db"
    first_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=first_engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=first_engine))
    bind = roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name="cao-restart",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id="native-restart",
            acquisition_method="chosen_session_id",
            terminal_id="term-restart",
            generation=str(uuid.uuid4()),
            pane_id="%81",
            pane_pid=8181,
            process_identity={"pid": 8181, "start_marker": "restart-marker"},
            execution_mode="native_tui",
            admitted=True,
        )
    )
    boundary = cohort.observe_boundary("cao-restart")
    request = cohort.OperationRequest(
        operation_id=str(uuid.uuid4()),
        session_name="cao-restart",
        operation_kind=cohort.KIND_STOP,
        requested_mode=cohort.MODE_FORCE,
        initiator_kind=cohort.INITIATOR_OPERATOR,
        initiated_by="colin",
        lifecycle_epoch=boundary["lifecycle_epoch"],
        lifecycle_observation=boundary["lifecycle_observation"],
        roster_revision=boundary["roster_revision"],
        member_snapshot_digest=boundary["member_snapshot_digest"],
    )
    operation = cohort.claim_operation(request)
    cohort.begin_stop_teardown(
        cohort.StopTeardownRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            actor="colin",
        )
    )
    first_engine.dispose()

    second_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=second_engine))
    stored = cohort.get_operation(operation["operation_id"])

    assert stored["state"] == cohort.STATE_TEARING_DOWN
    assert stored["members"][0]["agent_id"] == bind["agent"]["agent_id"]
    assert len(stored["transitions"]) == 1
    assert oj.get_session_barrier("cao-restart")["claimed_by"] == operation["operation_id"]
    second_engine.dispose()
