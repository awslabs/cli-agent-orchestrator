"""Tests for issue #1082's launch-record store: the schema (surrogate key,
created by ``create_all`` on new and existing databases), content-free
write/finalize/status-event/outcome-link functions, and busy-time computation
per launch."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import Base

CANARY = "synthetic-canary-9f3c1e"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

# Column names that would mean task content had a place to land.
CONTENT_WORDS = ("prompt", "message", "brief", "description", "output", "transcript", "note")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A fresh sqlite file wired as SessionLocal/engine."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
    return db_path, engine


@pytest.fixture
def clock(monkeypatch):
    """Drive ``database._utcnow`` explicitly: ``clock.at(seconds)``."""

    class _Clock:
        now = T0

        def at(self, seconds: float) -> datetime:
            self.now = T0 + timedelta(seconds=seconds)
            return self.now

    c = _Clock()
    monkeypatch.setattr(db_mod, "_utcnow", lambda: c.now)
    return c


def _launch(terminal_id, **overrides):
    values = dict(
        terminal_id=terminal_id,
        session_name="s",
        provider="kiro_cli",
        target_name="developer",
        target_kind="installed",
        launched_model=None,
        model_honored=None,
    )
    values.update(overrides)
    return db_mod.create_launch_record(**values)


def _rows(terminal_id):
    with db_mod.SessionLocal() as db:
        rows = (
            db.query(db_mod.LaunchRecordModel)
            .filter_by(terminal_id=terminal_id)
            .order_by(db_mod.LaunchRecordModel.id.asc())
            .all()
        )
        return [db_mod._launch_record_dict(r) for r in rows]


def _event(terminal_id, status, at_seconds):
    db_mod.record_launch_status_event(
        terminal_id, status, observed_at=T0 + timedelta(seconds=at_seconds)
    )


def _columns(conn, table):
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexed_columns(conn, table):
    cols = set()
    for index in conn.execute(f"PRAGMA index_list({table})"):
        cols |= {r[2] for r in conn.execute(f"PRAGMA index_info({index[1]})")}
    return cols


def _assert_launch_schema(conn):
    launch = _columns(conn, "launch_records")
    # Required columns present; additions are fine (no full-field-list pin).
    assert set(launch) >= {
        "id",
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
        "outcome_reporter_id",
        "created_at",
        "ended_at",
        "end_reason",
    }
    # ``id`` is the integer primary key; terminal_id is indexed, not unique.
    assert launch["id"][2].upper() == "INTEGER" and launch["id"][5] == 1
    assert launch["terminal_id"][5] == 0
    assert "terminal_id" in _indexed_columns(conn, "launch_records")
    events = _columns(conn, "launch_status_events")
    assert set(events) >= {"id", "terminal_id", "status", "created_at"}
    for table in (launch, events):
        for column in table:
            assert not any(word in column for word in CONTENT_WORDS), column


def test_schema_shape_and_no_content_columns(fresh_db):
    db_path, _ = fresh_db
    with sqlite3.connect(str(db_path)) as conn:
        _assert_launch_schema(conn)


def test_init_db_creates_tables_on_an_existing_database(tmp_path, monkeypatch):
    """Migration probe: a database that predates the two tables (with data in
    it) gets them from ``init_db``'s ``create_all``, with the surrogate key;
    reruns are idempotent and existing rows survive."""
    db_path = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE launch_records")
        conn.execute("DROP TABLE launch_status_events")
        conn.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
            "VALUES ('pre00001', 's', 'w', 'kiro_cli')"
        )
    import cli_agent_orchestrator.constants as consts

    monkeypatch.setattr(consts, "DATABASE_FILE", db_path, raising=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

    db_mod.init_db()
    db_mod.init_db()  # rerun: no error, no duplicate

    with sqlite3.connect(str(db_path)) as conn:
        _assert_launch_schema(conn)
        assert conn.execute("SELECT id FROM terminals").fetchall() == [("pre00001",)]
    _launch("pre00001")
    _launch("pre00001")  # a reused id is a plain second row
    assert [r["terminal_id"] for r in _rows("pre00001")] == ["pre00001", "pre00001"]
    engine.dispose()


def test_create_and_get_launch_record(fresh_db):
    _launch(
        "t1",
        session_name="cao-session",
        provider="claude_code",
        launched_model="claude-sonnet",
        model_honored=True,
        provider_session_id="sid-123",
        record_ids=(1, 2, 3),
        caller_id="caller1",
    )
    record = db_mod.get_launch_record("t1")
    assert isinstance(record["id"], int)
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


def test_get_launch_record_returns_the_newest_launch(fresh_db):
    _launch("t1", caller_id="old")
    db_mod.finalize_launch_records(["t1"], "deleted")
    _launch("t1", caller_id="new")
    assert db_mod.get_launch_record("t1")["caller_id"] == "new"


def test_create_launch_record_never_raises_on_bad_input(fresh_db):
    """A write failure must never surface as a raised exception (never fail
    or alter the launch it describes)."""
    _launch("t-bad", model_honored=object())  # not storable as a bool: insert fails
    assert db_mod.get_launch_record("t-bad") is None


def test_finalize_sets_ended_at_and_reason(fresh_db, clock):
    _launch("t2")
    clock.at(30)
    db_mod.finalize_launch_records(["t2"], "deleted")
    record = db_mod.get_launch_record("t2")
    assert record["ended_at"] == T0 + timedelta(seconds=30)
    assert record["end_reason"] == "deleted"


def test_finalize_is_idempotent_first_reason_wins(fresh_db):
    """Mutation guard: finalize only touches rows where ended_at IS NULL, so a
    later teardown signal (e.g. a duplicate sweep) cannot overwrite the first
    one's reason."""
    _launch("t3")
    db_mod.finalize_launch_records(["t3"], "deleted")
    db_mod.finalize_launch_records(["t3"], "session_teardown")
    assert db_mod.get_launch_record("t3")["end_reason"] == "deleted"


def test_finalize_closes_only_the_newest_open_launch(fresh_db, clock):
    """Surrogate key: with two open rows under one id (an older launch whose
    best-effort finalize was lost), one teardown ends one launch -- the newest."""
    _launch("t4", caller_id="old")
    _launch("t4", caller_id="new")
    clock.at(10)
    db_mod.finalize_launch_records(["t4"], "pane_closed")
    old, new = _rows("t4")
    assert old["ended_at"] is None and old["end_reason"] is None
    assert new["end_reason"] == "pane_closed"


def test_create_launch_record_returns_the_new_row_id(fresh_db):
    first = _launch("t-ids")
    second = _launch("t-ids")
    assert [r["id"] for r in _rows("t-ids")] == [first, second]
    assert _launch("t-bad-id", model_honored=object()) is None  # write failed


def test_finalize_if_terminal_gone_stamps_only_an_open_record_without_a_row(fresh_db, clock):
    """The create's re-read: a live row keeps its record open, a gone row's
    open record is stamped, and an already-closed record keeps its reason."""
    from cli_agent_orchestrator.clients.database import TerminalModel

    with db_mod.SessionLocal() as db:
        db.add(TerminalModel(id="live0001", tmux_session="s", tmux_window="w", provider="codex"))
        db.commit()
    live = _launch("live0001")
    gone = _launch("gone0001")
    closed = _launch("gone0002")
    db_mod.finalize_launch_records(["gone0002"], "pane_closed")
    clock.at(5)

    assert db_mod.finalize_launch_record_if_terminal_gone(live, end_reason="deleted") is False
    assert db_mod.finalize_launch_record_if_terminal_gone(gone, end_reason="deleted") is True
    assert db_mod.finalize_launch_record_if_terminal_gone(closed, end_reason="deleted") is False

    assert db_mod.get_launch_record("live0001")["ended_at"] is None
    assert db_mod.get_launch_record("gone0001")["end_reason"] == "deleted"
    assert db_mod.get_launch_record("gone0001")["ended_at"] == T0 + timedelta(seconds=5)
    assert db_mod.get_launch_record("gone0002")["end_reason"] == "pane_closed"


def test_finalize_if_terminal_gone_stamps_only_the_given_record(fresh_db):
    """An older launch left open under the same id is not this create's."""
    older = _launch("gone0003", caller_id="older")
    newer = _launch("gone0003", caller_id="newer")
    assert db_mod.finalize_launch_record_if_terminal_gone(newer, end_reason="deleted") is True
    rows = _rows("gone0003")
    assert [(r["id"], r["end_reason"]) for r in rows] == [(older, None), (newer, "deleted")]


def test_finalize_if_terminal_gone_never_raises(fresh_db, monkeypatch):
    def broken_session():
        raise RuntimeError("db is down")

    monkeypatch.setattr(db_mod, "SessionLocal", broken_session)
    assert db_mod.finalize_launch_record_if_terminal_gone(1, end_reason="deleted") is False


def test_finalize_if_terminal_gone_requires_an_end_reason(fresh_db):
    with pytest.raises(TypeError, match="end_reason"):
        db_mod.finalize_launch_record_if_terminal_gone(1)


def test_finalize_unknown_terminal_is_a_noop(fresh_db):
    db_mod.finalize_launch_records(["ghost"], "deleted")  # must not raise
    assert db_mod.get_launch_record("ghost") is None


def test_finalize_empty_list_is_a_noop(fresh_db):
    db_mod.finalize_launch_records([], "deleted")  # must not raise


def test_end_reason_vocabulary():
    assert set(db_mod.LAUNCH_END_REASONS) == {
        "launch_failed",
        "deleted",
        "pane_closed",
        "stale_cleanup",
        "session_teardown",
    }


@pytest.mark.parametrize(
    "fn, args",
    [
        (db_mod.delete_terminal, ("t1",)),
        (db_mod.delete_terminals_by_session, ("s",)),
        (db_mod.delete_terminals_by_ids, (["t1"],)),
    ],
)
def test_teardown_functions_require_an_end_reason(fresh_db, fn, args):
    """Every teardown function that finalizes takes the reason as a required
    keyword, so a new call site has to choose one."""
    with pytest.raises(TypeError, match="end_reason"):
        fn(*args)


def test_delete_terminals_by_ids_finalizes(fresh_db):
    """The only teardown path for flow recycling and the session-teardown
    sweep: if it stopped finalizing, those records would stay open forever."""
    from cli_agent_orchestrator.clients.database import TerminalModel

    with db_mod.SessionLocal() as db:
        for tid in ("ids00001", "ids00002"):
            db.add(TerminalModel(id=tid, tmux_session="s", tmux_window=tid, provider="kiro_cli"))
        db.commit()
    _launch("ids00001")
    _launch("ids00002")

    assert db_mod.delete_terminals_by_ids(["ids00001", "ids00002"], end_reason="session_teardown")

    for tid in ("ids00001", "ids00002"):
        assert db_mod.get_launch_record(tid)["end_reason"] == "session_teardown"


def test_delete_terminals_by_session_finalizes_the_ids_it_collected(fresh_db):
    """A defensive path with no caller in src/ today: it still stamps each
    deleted row's launch, and only that session's."""
    from cli_agent_orchestrator.clients.database import TerminalModel

    with db_mod.SessionLocal() as db:
        for tid, session in (("ses00001", "s-a"), ("ses00002", "s-a"), ("ses00003", "s-b")):
            db.add(TerminalModel(id=tid, tmux_session=session, tmux_window=tid, provider="codex"))
        db.commit()
    for tid in ("ses00001", "ses00002", "ses00003"):
        _launch(tid)

    assert db_mod.delete_terminals_by_session("s-a", end_reason="session_teardown") == 2

    for tid in ("ses00001", "ses00002"):
        assert db_mod.get_launch_record(tid)["end_reason"] == "session_teardown"
    assert db_mod.get_launch_record("ses00003")["ended_at"] is None


def test_delete_terminals_by_ids_finalizes_only_the_rows_it_deleted(fresh_db):
    """An id whose row another teardown already removed is not finalized
    again: with the newest-open-row rule, a second stamp would close an older
    launch left open under the same id."""
    from cli_agent_orchestrator.clients.database import TerminalModel

    with db_mod.SessionLocal() as db:
        db.add(TerminalModel(id="ids00003", tmux_session="s", tmux_window="w", provider="kiro_cli"))
        db.commit()
    _launch("ids00003")
    _launch("ids00004", caller_id="older")  # an older launch left open
    _launch("ids00004", caller_id="newer")
    db_mod.finalize_launch_records(["ids00004"], "deleted")  # its row's own teardown

    deleted = db_mod.delete_terminals_by_ids(
        ["ids00003", "ids00004", "ids00003"], end_reason="session_teardown"
    )

    assert deleted == 1
    assert db_mod.get_launch_record("ids00003")["end_reason"] == "session_teardown"
    older, newer = _rows("ids00004")
    assert older["ended_at"] is None and older["end_reason"] is None
    assert newer["end_reason"] == "deleted"


def test_find_bound_launch_record(fresh_db):
    _launch("t5", caller_id="creator")
    record_id = db_mod.get_launch_record("t5")["id"]
    assert db_mod.find_bound_launch_record("t5", "t5") == (record_id, True)  # self
    assert db_mod.find_bound_launch_record("t5", "creator") == (record_id, True)
    assert db_mod.find_bound_launch_record("t5", "outsider") == (None, True)
    assert db_mod.find_bound_launch_record("nope", "creator") == (None, False)


@pytest.mark.parametrize("reporter", [None, ""])
def test_find_bound_launch_record_never_binds_a_missing_reporter(fresh_db, reporter):
    """A workflow step's record has no caller_id; an anonymous report must not
    match it as if None were its creator."""
    _launch("step0001", caller_id=None)
    assert db_mod.find_bound_launch_record("step0001", reporter) == (None, True)


def test_link_launch_outcome_by_record_id(fresh_db):
    _launch("t6", caller_id="creator")
    record_id = db_mod.get_launch_record("t6")["id"]

    assert db_mod.link_launch_outcome(record_id, True, "t6") is True
    record = db_mod.get_launch_record("t6")
    assert (record["outcome"], record["outcome_reporter_id"]) == ("success", "t6")
    assert record["outcome_recorded_at"] is not None

    assert db_mod.link_launch_outcome(record_id, False, "creator") is True
    record = db_mod.get_launch_record("t6")
    assert (record["outcome"], record["outcome_reporter_id"]) == ("failure", "creator")


def test_link_launch_outcome_missing_record_is_a_noop(fresh_db):
    assert db_mod.link_launch_outcome(999, True, "x") is False  # must not raise


def test_compute_busy_seconds_missing_record_is_zero(fresh_db):
    assert db_mod.compute_busy_seconds(999) == 0.0


def test_compute_busy_seconds_sums_processing_intervals(fresh_db, clock):
    """Busy time (provisional definition): seconds spent in 'processing'
    between consecutive recorded transitions. Only 'processing' counts."""
    _launch("t7")
    _event("t7", "idle", 0)
    _event("t7", "processing", 10)
    _event("t7", "idle", 70)
    _event("t7", "idle", 500)  # an idle->idle gap contributes nothing
    clock.at(1000)
    db_mod.finalize_launch_records(["t7"], "deleted")
    assert db_mod.compute_busy_seconds(db_mod.get_launch_record("t7")["id"]) == 60.0


def test_compute_busy_seconds_on_torn_down_launch_closes_at_ended_at(fresh_db, clock):
    """The AC's own scenario: a launch still processing at teardown counts up
    to its ``ended_at``, not to whenever the record is read."""
    _launch("t8")
    _event("t8", "processing", 100)
    clock.at(160)
    db_mod.finalize_launch_records(["t8"], "deleted")
    record_id = db_mod.get_launch_record("t8")["id"]
    clock.at(5000)  # read long after teardown
    assert db_mod.compute_busy_seconds(record_id) == 60.0


def test_compute_busy_seconds_still_running_uses_now(fresh_db, clock):
    _launch("t9")
    _event("t9", "processing", 100)
    clock.at(130)
    assert db_mod.compute_busy_seconds(db_mod.get_launch_record("t9")["id"]) == 30.0


def test_compute_busy_seconds_per_incarnation(fresh_db, clock):
    """Surrogate-key test 2: with events from two launches under one reused
    terminal id, each launch counts only its own window."""
    clock.at(0)
    _launch("reused01")
    _event("reused01", "processing", 10)
    _event("reused01", "idle", 40)  # first launch: 30 s
    clock.at(100)
    db_mod.finalize_launch_records(["reused01"], "deleted")
    clock.at(200)
    _launch("reused01")
    _event("reused01", "processing", 210)
    clock.at(215)
    db_mod.finalize_launch_records(["reused01"], "pane_closed")  # second: 5 s, open at end
    _event("reused01", "processing", 300)  # stray event after both ended

    first, second = _rows("reused01")
    assert db_mod.compute_busy_seconds(first["id"]) == 30.0
    assert db_mod.compute_busy_seconds(second["id"]) == 5.0


def test_compute_busy_seconds_unfinalized_older_launch_stops_at_the_next(fresh_db, clock):
    """If an older launch's best-effort finalize was lost, the next launch's
    ``created_at`` stands in for its end, so it never absorbs the newer
    launch's work, and the newer launch never absorbs the older one's."""
    clock.at(0)
    _launch("lost0001")
    _event("lost0001", "processing", 10)
    clock.at(50)
    _launch("lost0001")  # no finalize for the first
    _event("lost0001", "idle", 60)
    _event("lost0001", "processing", 70)
    clock.at(100)

    first, second = _rows("lost0001")
    assert db_mod.compute_busy_seconds(first["id"]) == 40.0
    assert db_mod.compute_busy_seconds(second["id"]) == 30.0


def test_privacy_spy_launch_record_has_no_free_text_parameter():
    """The write path does not even accept a prompt/brief/message/output."""
    import inspect

    params = set(inspect.signature(db_mod.create_launch_record).parameters)
    assert not any(word in p for p in params for word in CONTENT_WORDS)
