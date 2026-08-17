"""M3-E ORM/migration parity, column evolution, restart persistence, rollback."""

from __future__ import annotations

import sqlite3
import uuid

from sqlalchemy import Integer, create_engine
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateColumn

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

#: Frozen. The columns M3-E shipped that SQLite cannot ``ALTER TABLE ADD`` —
#: the primary key and every NOT NULL column with no default — which is to say
#: exactly the columns any store carrying this table already has.
#:
#: **Never add a name here.** This set is what makes "added after M3-E"
#: checkable: a column added to the model later is absent from an upgraded
#: store, so the migration has to be able to ALTER it in, and a name added here
#: would silently license the one shape the migration cannot repair.
_M3E_UNADDABLE_COLUMNS = frozenset(
    {
        "handoff_id",
        "schema_version",
        "session_name",
        "task_occurrence_id",
        "from_agent_id",
        "to_agent_id",
        "from_incarnation_id",
        "from_terminal_id",
        "donor_revision",
        "packet_digest",
        "packet_control_id",
        "quiescence_json",
        "quiescence_digest",
        "delivery_state",
        "state",
        "initiated_by",
        "created_at",
        "updated_at",
    }
)

_MODEL = database.TaskOccurrenceHandoffModel


def _model_columns():
    return {column.name for column in _MODEL.__table__.columns}


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


# ---------------------------------------------------------------------------
# column evolution on a store that already carries the table (cond-0433)
# ---------------------------------------------------------------------------


def _create_at_shape(conn, names):
    """Create the handoff table carrying only ``names``, typed from the model."""
    specs = [
        str(CreateColumn(column).compile(dialect=sqlite_dialect()))
        for column in _MODEL.__table__.columns
        if column.name in names
    ]
    conn.execute(
        f"CREATE TABLE {_MODEL.__tablename__} ({', '.join(specs)}, PRIMARY KEY (handoff_id))"
    )


def _row_at_shape(names):
    """One row filling every column ``names`` requires, typed from the model."""
    return {
        column.name: (7 if isinstance(column.type, Integer) else f"{column.name}-original")
        for column in _MODEL.__table__.columns
        if column.name in names and not column.nullable
    }


def _store_at_older_shape(tmp_path, monkeypatch, names):
    """A store carrying the handoff table at ``names``, with one row in it."""
    path = tmp_path / "older-shape.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
        _create_at_shape(conn, names)
        row = _row_at_shape(names)
        conn.execute(
            f"INSERT INTO {_MODEL.__tablename__} ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{path}")
    return path, row


def test_a_store_that_already_has_the_table_gains_the_columns_it_lacks(tmp_path, monkeypatch):
    """The defect this closes: ``CREATE TABLE IF NOT EXISTS`` is a silent no-op
    against an existing table, so a store created at an older shape kept that
    shape forever — and the hold at ``task_handoff.hold_refusal`` is fail-closed
    on every managed write, so one missing column refuses every steer, tell and
    operator message on that installation, handoff party or not.

    The older shape here is the minimal legal one: the primary key and the NOT
    NULL columns, which are the only ones SQLite could never append later.
    """
    path, row = _store_at_older_shape(tmp_path, monkeypatch, _M3E_UNADDABLE_COLUMNS)
    with sqlite3.connect(str(path)) as conn:
        assert _columns(conn, "task_occurrence_handoffs") == set(_M3E_UNADDABLE_COLUMNS)

    database._migrate_task_occurrence_handoffs()

    with sqlite3.connect(str(path)) as conn:
        present = _columns(conn, "task_occurrence_handoffs")
        missing = _model_columns() - present
        assert not missing, (
            "the migration left an existing store short of "
            f"{sorted(missing)}; every managed write on it would be refused as "
            "handoff-held"
        )
        assert _PARTIAL_INDEXES <= _indexes(conn, "task_occurrence_handoffs")
        stored = dict(
            zip(
                [d[0] for d in conn.execute("SELECT * FROM task_occurrence_handoffs").description],
                conn.execute("SELECT * FROM task_occurrence_handoffs").fetchone(),
            )
        )
    # The pre-existing row keeps its bytes, and every appended nullable column
    # reads NULL rather than a fabricated value.
    assert {name: stored[name] for name in row} == row
    appended = present - set(row)
    assert appended
    assert all(
        stored[column.name] is None
        for column in _MODEL.__table__.columns
        if column.name in appended and column.nullable
    )


def test_reconciling_an_older_shape_store_is_idempotent(tmp_path, monkeypatch):
    """A rerun adds nothing and rewrites nothing.

    Asserted on the row as well as the shape: a migration that dropped and
    rebuilt the table would keep the columns identical and lose the row.
    """
    path, row = _store_at_older_shape(tmp_path, monkeypatch, _M3E_UNADDABLE_COLUMNS)
    database._migrate_task_occurrence_handoffs()
    with sqlite3.connect(str(path)) as conn:
        first = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'task_occurrence_handoffs'"
        ).fetchone()[0]

    database._migrate_task_occurrence_handoffs()

    with sqlite3.connect(str(path)) as conn:
        assert (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'task_occurrence_handoffs'"
            ).fetchone()[0]
            == first
        )
        surviving = conn.execute("SELECT handoff_id FROM task_occurrence_handoffs").fetchall()
    assert surviving == [(row["handoff_id"],)]


def test_a_blocked_column_is_logged_at_error_with_its_consequence(
    tmp_path, monkeypatch, caplog
):
    """init_db() deliberately continues past an unappendable column.

    A typed refusal beats a dead installation, so the migration swallows the
    reconcile's raise. But that leaves the store a column short, and the
    fail-closed hold then refuses every managed write on it as undecidable —
    a degraded installation, not a routine event. It has to be logged at
    error with the column named and the consequence spelled out; a routine
    warning is how the one installation that hits this gets missed.
    """
    import logging

    # A store whose handoff table lacks ``donor_revision`` — NOT NULL with no
    # default, so SQLite cannot ALTER it in and the reconcile must raise.
    _store_at_older_shape(
        tmp_path, monkeypatch, set(_M3E_UNADDABLE_COLUMNS) - {"donor_revision"}
    )

    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.clients.database"):
        database._migrate_task_occurrence_handoffs()  # must not raise

    blocked = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "donor_revision" in record.message
        and "column short" in record.message
    ]
    assert blocked, (
        "an unappendable column left the store a column short without an "
        "error-level trace naming it and its consequence; captured: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


def test_no_column_added_after_m3e_is_beyond_alter_table():
    """The gate on a *future* column, which is what makes the repair a gate.

    A column added to ``TaskOccurrenceHandoffModel`` is absent from every store
    that already carries the table, so the migration has to be able to ALTER it
    in. SQLite only appends a column whose value for the stored rows is
    determined — nullable, or NOT NULL with a constant default — and appends no
    PRIMARY KEY or UNIQUE column at all. A new column outside that set cannot
    reach an upgraded store, and the fail-closed hold turns that into a total
    loss of managed input there, so it must be caught here rather than in
    production.

    The check runs in both directions, because the frozen set is a
    hand-maintained literal. A *relaxing* drift — an existing NOT NULL column
    flipped to nullable, or given a server_default — leaves the name in the
    frozen set while the model's addability moves on, and a name-set
    difference in one direction stays empty and passes. Asserting the
    (name, addability) pairs forces the drift to be named here, whichever
    side of the line it moves.
    """
    unaddable = {
        column.name
        for column in _MODEL.__table__.columns
        if database._sqlite_add_column_spec(column) is None
    }
    added_later = sorted(unaddable - _M3E_UNADDABLE_COLUMNS)
    assert not added_later, (
        f"{added_later} cannot be added to a store that already has "
        "task_occurrence_handoffs. Make the column nullable, give it a "
        "server_default, or rebuild the table explicitly — do not widen "
        "_M3E_UNADDABLE_COLUMNS."
    )
    relaxed = sorted(_M3E_UNADDABLE_COLUMNS - unaddable)
    assert not relaxed, (
        f"{relaxed} shipped with M3-E as unappendable but the model now lets "
        "SQLite ALTER them in — a nullability or server_default change on an "
        "existing column. If that change is deliberate, remove the name from "
        "_M3E_UNADDABLE_COLUMNS; do not leave it frozen over a shape the model "
        "no longer has."
    )


def test_the_raw_ddl_declares_every_column_the_model_does(tmp_path):
    """The migration's own ``CREATE TABLE`` is the shape a store that predates
    the table is created at, so it and the model must not drift apart."""
    path = tmp_path / "raw-ddl.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute(database._TASK_OCCURRENCE_HANDOFFS_DDL)
        declared = _columns(conn, "task_occurrence_handoffs")
    model = _model_columns()
    assert declared == model, (
        "_TASK_OCCURRENCE_HANDOFFS_DDL and TaskOccurrenceHandoffModel disagree: "
        f"declared only by the model={sorted(model - declared)}, "
        f"declared only by the DDL={sorted(declared - model)}"
    )


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
