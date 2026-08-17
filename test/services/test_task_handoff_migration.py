"""M3-E ORM/migration parity, restart persistence, and rollback compatibility."""

from __future__ import annotations

import sqlite3
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import task_handoff as th
from cli_agent_orchestrator.services import task_occurrence as occ

_HANDOFF_COLUMNS = {
    "handoff_id",
    "schema_version",
    "session_name",
    "task_occurrence_id",
    "from_agent_id",
    "to_agent_id",
    "from_incarnation_id",
    "from_terminal_id",
    "from_generation",
    "donor_revision",
    "packet_digest",
    "packet_control_id",
    "quiescence_json",
    "quiescence_digest",
    "delivery_state",
    "delivery_outcome",
    "delivery_receipt",
    "to_incarnation_id",
    "to_terminal_id",
    "to_generation",
    "successor_occurrence_id",
    "state",
    "receipt_digest",
    "detail",
    "initiated_by",
    "created_at",
    "updated_at",
    "settled_at",
}

_PARTIAL_INDEXES = {
    "ix_task_occurrence_handoffs_pending_occurrence",
    "ix_task_occurrence_handoffs_pending_donor",
    "ix_task_occurrence_handoffs_pending_recipient",
}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _migrated_db(tmp_path, monkeypatch):
    """A pre-M3-E store brought forward by the additive migration alone."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as db_module

    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite:///{path}")
    db_module._migrate_task_occurrences()
    db_module._migrate_task_occurrence_handoffs()
    return path


def _orm_db(tmp_path):
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    return orm_path


def test_migration_matches_the_orm_schema_column_for_column(tmp_path, monkeypatch):
    path = _migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(str(path)) as conn:
        assert _columns(conn, "task_occurrence_handoffs") == _HANDOFF_COLUMNS
    with sqlite3.connect(str(_orm_db(tmp_path))) as conn:
        assert _columns(conn, "task_occurrence_handoffs") == _HANDOFF_COLUMNS


def test_the_three_pending_indexes_are_partial_in_both_schemas(tmp_path, monkeypatch):
    """Full unique indexes would forbid a second handback for the same pair.

    The constraint is "one handoff in flight", not "one handoff ever". Getting
    it wrong would make a worker that was handed back once ineligible forever,
    which is exactly the reversibility this milestone exists to provide.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    for store in (path, _orm_db(tmp_path)):
        with sqlite3.connect(str(store)) as conn:
            assert _PARTIAL_INDEXES <= _indexes(conn, "task_occurrence_handoffs")
            for name in _PARTIAL_INDEXES:
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()[0]
                assert "WHERE" in sql.upper()
                assert "'pending'" in sql


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Re-running preserves rows, not merely the column set.

    A migration that dropped and recreated the table would keep the columns
    identical, so the row is what makes this test about idempotency rather
    than about shape.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    from cli_agent_orchestrator.clients import database as db_module

    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO task_occurrence_handoffs (handoff_id, schema_version, session_name, "
            "task_occurrence_id, from_agent_id, to_agent_id, from_incarnation_id, "
            "from_terminal_id, donor_revision, packet_digest, packet_control_id, "
            "quiescence_json, quiescence_digest, delivery_state, state, initiated_by, "
            "created_at, updated_at) VALUES "
            "('h1', 'v', 's', 'o', 'a', 'b', 'i', 't', 0, 'd', 'c', '{}', 'q', 'pending', "
            "'pending', 'me', 'now', 'now')"
        )

    db_module._migrate_task_occurrence_handoffs()
    with sqlite3.connect(str(path)) as conn:
        assert _columns(conn, "task_occurrence_handoffs") == _HANDOFF_COLUMNS
        surviving = conn.execute("SELECT handoff_id FROM task_occurrence_handoffs").fetchall()
    assert surviving == [("h1",)]


def test_records_survive_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())
    occurrence_id = str(uuid.uuid4())
    handoff_id = str(uuid.uuid4())
    occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=occurrence_id,
            session_name="cao-restart",
            agent_id=donor_agent,
            round_index=0,
            dispatch_digest="a" * 64,
            incarnation=occ.EffectIncarnation(incarnation_id="inc-1", terminal_id="term-1"),
        )
    )
    th.begin_handoff(
        th.BeginRequest(
            handoff_id=handoff_id,
            session_name="cao-restart",
            task_occurrence_id=occurrence_id,
            to_agent_id=recipient_agent,
            packet_digest="d" * 64,
            evidence=th.QuiescenceEvidence(
                incarnation_id="inc-1",
                terminal_id="term-1",
                turn_state=th.TURN_TERMINAL,
                observed_at="2026-08-16T12:00:00Z",
            ),
            initiated_by="supervisor",
        )
    )
    engine.dispose()

    reopened = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=reopened)
    )
    record = th.get_handoff(handoff_id)
    assert record["state"] == th.STATE_PENDING
    assert record["from_agent_id"] == donor_agent
    assert record["quiescence"]["turn_state"] == th.TURN_TERMINAL
    # The hold survives a restart, which is what makes it a durable suspension
    # rather than one process's in-memory opinion.
    assert th.hold_refusal(donor_agent) is not None
    reopened.dispose()


def test_rollback_to_a_build_without_m3e_leaves_the_occurrence_schema_untouched(
    tmp_path, monkeypatch
):
    """M3-E adds one table and changes nothing M3-D owns.

    This is the whole rollback story. Because no column and no state value was
    added to ``task_occurrences``, an older binary's schema and its reading of
    every existing row are byte-identical to what they were; the handoff rows
    are simply unread.
    """
    path = _migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(str(path)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "task_occurrence_handoffs" in tables
        occurrence_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'task_occurrences'"
        ).fetchone()[0]

    assert "handoff" not in occurrence_sql.lower()
    assert set(database.TaskOccurrenceModel.__table__.c.keys()) == {
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
    # And no third occurrence state was introduced.
    assert occ.STATES == {occ.STATE_OPEN, occ.STATE_FINALIZED}


def test_schema_versions_are_stamped_on_every_m3e_row(tmp_path, monkeypatch):
    """A row with no schema version is one a later build cannot interpret.

    Asserted on a real row rather than on the constant: NOT NULL catches a
    *missing* stamp, not a wrong one, so a `_begin_once` that wrote the
    occurrence service's version would satisfy the schema and this test alike.
    """
    path = tmp_path / "stamp.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    donor_agent = str(uuid.uuid4())
    occurrence_id = str(uuid.uuid4())
    occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=occurrence_id,
            session_name="cao-stamp",
            agent_id=donor_agent,
            round_index=0,
            dispatch_digest="a" * 64,
            incarnation=occ.EffectIncarnation(incarnation_id="inc-1", terminal_id="term-1"),
        )
    )
    record = th.begin_handoff(
        th.BeginRequest(
            handoff_id=str(uuid.uuid4()),
            session_name="cao-stamp",
            task_occurrence_id=occurrence_id,
            to_agent_id=str(uuid.uuid4()),
            packet_digest="d" * 64,
            evidence=th.QuiescenceEvidence(
                incarnation_id="inc-1",
                terminal_id="term-1",
                turn_state=th.TURN_TERMINAL,
                observed_at="2026-08-16T12:00:00Z",
            ),
            initiated_by="supervisor",
        )
    )
    assert record["schema_version"] == th.SCHEMA_VERSION
    assert th.SCHEMA_VERSION.startswith("cao-m3e-")
    assert record["schema_version"] != occ.SCHEMA_VERSION
    engine.dispose()
