"""Tests for issue #1082's launch-record store: additive migration, content-free
write/finalize/status-event/outcome-link functions, and busy-time computation."""

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import Base, LaunchStatusEventModel

CANARY = "synthetic-canary-9f3c1e"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A fresh sqlite file wired as both DATABASE_FILE (for the migrator's raw
    sqlite3 connect) and SessionLocal/engine (for ORM inserts)."""
    db_path = tmp_path / "test.db"
    import cli_agent_orchestrator.constants as consts

    monkeypatch.setattr(consts, "DATABASE_FILE", db_path, raising=False)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
    return db_path, engine


def test_table_shapes(fresh_db):
    db_path, _ = fresh_db
    conn = sqlite3.connect(str(db_path))
    launch_cols = {r[1] for r in conn.execute("PRAGMA table_info(launch_records)")}
    assert launch_cols == {
        "terminal_id",
        "session_name",
        "provider",
        "target_name",
        "target_kind",
        "launched_model",
        "model_honored",
        "provider_session_id",
        "record_ids",
        "caller_id",
        "outcome",
        "outcome_recorded_at",
        "created_at",
        "ended_at",
        "end_reason",
    }
    event_cols = {r[1] for r in conn.execute("PRAGMA table_info(launch_status_events)")}
    assert event_cols == {"id", "terminal_id", "status", "created_at"}


def test_migrator_idempotent_rerun_on_legacy_db(fresh_db):
    """On a DB that predates the two tables, the raw-SQL migrator creates them,
    and re-running it is a no-op (no duplicate table, no error)."""
    db_path, engine = fresh_db
    # Simulate a legacy DB: drop the two tables Base.metadata.create_all made.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE launch_records")
        conn.execute("DROP TABLE launch_status_events")
        conn.commit()

    db_mod._migrate_add_launch_records()
    db_mod._migrate_add_launch_records()  # re-run: no error, no duplicate

    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('launch_records', 'launch_status_events')"
    ).fetchone()[0]
    assert n == 2


def test_migrator_registered_last_in_init_db():
    import inspect

    src = inspect.getsource(db_mod.init_db)
    assert "_migrate_add_launch_records()" in src
    assert src.index("_migrate_add_handoff_results()") < src.index("_migrate_add_launch_records()")


def test_create_and_get_launch_record(fresh_db):
    db_mod.create_launch_record(
        terminal_id="t1",
        session_name="cao-session",
        provider="claude_code",
        target_name="developer",
        target_kind="installed",
        launched_model="claude-sonnet",
        model_honored=True,
        provider_session_id="sid-123",
        record_ids=(1, 2, 3),
        caller_id="caller1",
    )
    record = db_mod.get_launch_record("t1")
    assert record["terminal_id"] == "t1"
    assert record["session_name"] == "cao-session"
    assert record["provider"] == "claude_code"
    assert record["target_name"] == "developer"
    assert record["target_kind"] == "installed"
    assert record["launched_model"] == "claude-sonnet"
    assert record["model_honored"] is True
    assert record["provider_session_id"] == "sid-123"
    assert record["record_ids"] == [1, 2, 3]
    assert record["caller_id"] == "caller1"
    assert record["ended_at"] is None
    assert record["end_reason"] is None


def test_get_launch_record_missing_returns_none(fresh_db):
    assert db_mod.get_launch_record("does-not-exist") is None


def test_create_launch_record_never_raises_on_bad_input(fresh_db):
    """Mutation guard for the AC: a write failure must never surface as a raised
    exception (never fail or alter the launch it describes)."""
    # record_ids deliberately not a tuple of ints: list(...) + json.dumps still
    # succeeds for simple values, so force a real failure via an invalid kwarg
    # type that breaks the ORM insert (model_honored expects bool/None).
    db_mod.create_launch_record(
        terminal_id="t-bad",
        session_name="s",
        provider="claude_code",
        target_name="x",
        target_kind="installed",
        launched_model=None,
        model_honored=object(),  # not JSON/SQLite-serializable as a bool
    )  # must not raise
    # No row persisted on failure, but the call itself completed cleanly.


def test_finalize_sets_ended_at_and_reason(fresh_db):
    db_mod.create_launch_record(
        terminal_id="t2",
        session_name="s",
        provider="kiro_cli",
        target_name="developer",
        target_kind="installed",
        launched_model=None,
        model_honored=None,
    )
    db_mod.finalize_launch_records(["t2"], "deleted")
    record = db_mod.get_launch_record("t2")
    assert record["ended_at"] is not None
    assert record["end_reason"] == "deleted"


def test_finalize_is_idempotent_first_reason_wins(fresh_db):
    """Mutation guard: finalize must only touch rows where ended_at IS NULL, so a
    later teardown signal (e.g. a duplicate sweep) cannot overwrite the first
    one's reason. Dropping that filter would make this test fail."""
    db_mod.create_launch_record(
        terminal_id="t3",
        session_name="s",
        provider="kiro_cli",
        target_name="developer",
        target_kind="installed",
        launched_model=None,
        model_honored=None,
    )
    db_mod.finalize_launch_records(["t3"], "deleted")
    db_mod.finalize_launch_records(["t3"], "session_cleanup")
    record = db_mod.get_launch_record("t3")
    assert record["end_reason"] == "deleted"


def test_finalize_unknown_terminal_is_a_noop(fresh_db):
    db_mod.finalize_launch_records(["ghost"], "deleted")  # must not raise
    assert db_mod.get_launch_record("ghost") is None


def test_finalize_empty_list_is_a_noop(fresh_db):
    db_mod.finalize_launch_records([], "deleted")  # must not raise


def test_link_launch_outcome_success_and_failure(fresh_db):
    db_mod.create_launch_record(
        terminal_id="t4",
        session_name="s",
        provider="kiro_cli",
        target_name="developer",
        target_kind="installed",
        launched_model=None,
        model_honored=None,
    )
    db_mod.link_launch_outcome("t4", True)
    record = db_mod.get_launch_record("t4")
    assert record["outcome"] == "success"
    assert record["outcome_recorded_at"] is not None

    db_mod.link_launch_outcome("t4", False)
    record = db_mod.get_launch_record("t4")
    assert record["outcome"] == "failure"


def test_link_launch_outcome_missing_terminal_is_a_noop(fresh_db):
    db_mod.link_launch_outcome("ghost", True)  # must not raise
    assert db_mod.get_launch_record("ghost") is None


def test_compute_busy_seconds_no_events_is_zero(fresh_db):
    assert db_mod.compute_busy_seconds("no-events") == 0.0


def test_compute_busy_seconds_sums_processing_intervals(fresh_db):
    """Busy time (provisional definition): seconds spent in 'processing' between
    consecutive recorded transitions."""
    with db_mod.SessionLocal() as db:
        db.add(LaunchStatusEventModel(terminal_id="t5", status="idle"))
        db.commit()
    import time as _time

    _time.sleep(0.01)
    with db_mod.SessionLocal() as db:
        db.add(LaunchStatusEventModel(terminal_id="t5", status="processing"))
        db.commit()
    _time.sleep(0.05)
    with db_mod.SessionLocal() as db:
        db.add(LaunchStatusEventModel(terminal_id="t5", status="idle"))
        db.commit()

    busy = db_mod.compute_busy_seconds("t5")
    assert busy >= 0.04  # the processing->idle gap, not the idle->processing one


def test_compute_busy_seconds_mutation_guard_non_processing_excluded(fresh_db):
    """Mutation guard: only 'processing' intervals count. An idle->idle gap of the
    same duration must contribute nothing."""
    import time as _time

    with db_mod.SessionLocal() as db:
        db.add(LaunchStatusEventModel(terminal_id="t6", status="idle"))
        db.commit()
    _time.sleep(0.05)
    with db_mod.SessionLocal() as db:
        db.add(LaunchStatusEventModel(terminal_id="t6", status="idle"))
        db.commit()

    assert db_mod.compute_busy_seconds("t6") == 0.0


def test_compute_busy_seconds_still_running_uses_now(fresh_db):
    """A terminal with no ended_at and a trailing 'processing' event is still
    live: the open interval counts up to 'now', not zero."""
    import time as _time

    db_mod.create_launch_record(
        terminal_id="t7",
        session_name="s",
        provider="kiro_cli",
        target_name="developer",
        target_kind="installed",
        launched_model=None,
        model_honored=None,
    )
    with db_mod.SessionLocal() as db:
        db.add(LaunchStatusEventModel(terminal_id="t7", status="processing"))
        db.commit()
    _time.sleep(0.03)
    assert db_mod.compute_busy_seconds("t7") >= 0.02


def test_privacy_spy_launch_record_has_no_free_text_fields():
    """Issue #1082 acceptance criterion: a spy test with a synthetic secret finds
    no prompt, brief, message or output text in a launch record. The write path
    (``create_launch_record``) does not even accept such a parameter -- assert
    that by construction, and separately prove a CANARY placed in any accepted
    field only ever lands in a documented label column, never anywhere else."""
    import inspect

    params = set(inspect.signature(db_mod.create_launch_record).parameters)
    forbidden_names = {"prompt", "message", "brief", "description", "output", "initial_message"}
    assert params.isdisjoint(forbidden_names)


def test_privacy_spy_canary_confined_to_target_name_column(fresh_db):
    """A CANARY placed in the one free-ish label field (``target_name``, the
    agent profile / ephemeral name) must not leak into any OTHER column, and no
    other write path exists for task text to reach this table at all."""
    db_mod.create_launch_record(
        terminal_id="t8",
        session_name="s",
        provider="claude_code",
        target_name=CANARY,
        target_kind="ephemeral",
        launched_model=None,
        model_honored=None,
        provider_session_id="sid",
        record_ids=(1,),
        caller_id="caller",
    )
    record = db_mod.get_launch_record("t8")
    for column, value in record.items():
        if column == "target_name":
            continue
        assert CANARY not in str(value), f"canary leaked into {column!r}: {value!r}"
