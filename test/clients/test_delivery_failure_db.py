"""reply-delivery-failure: schema migration, models and the new database helpers.

The helpers are reached through the ``database`` module rather than imported by
name, so each test fails on its own missing name instead of the whole file
failing to collect.
"""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    close_delegation_edge,
    close_open_edges_for_terminal,
    create_delegation_edge,
    create_inbox_message,
    create_terminal,
    find_any_edge,
    find_open_edge,
    get_inbox_messages,
    list_open_edges_into,
    list_open_edges_out_of,
)
from cli_agent_orchestrator.models.delegation import DelegationEdge, EdgeSource, EdgeState
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.utils.sqlite_connection import closing_transaction


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", session):
        yield session


def _inbox_row(session, sender="w1", receiver="sup") -> int:
    with session() as s:
        row = InboxModel(sender_id=sender, receiver_id=receiver, message="hi", status="pending")
        s.add(row)
        s.commit()
        return row.id


# The two tables as they are before this change: every column the current code
# reads, none of the three this change adds.
_LEGACY_EDGES_DDL = (
    "CREATE TABLE delegation_edges (id INTEGER PRIMARY KEY, caller_id TEXT NOT NULL, "
    "worker_id TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL, "
    "reason TEXT NOT NULL, message_id INTEGER, created_at DATETIME, "
    "delivered_at DATETIME, closed_at DATETIME)"
)
_LEGACY_INBOX_DDL = (
    "CREATE TABLE inbox (id INTEGER PRIMARY KEY, sender_id TEXT NOT NULL, "
    "receiver_id TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, "
    "created_at DATETIME)"
)


def _columns(path, table):
    with closing_transaction(sqlite3.connect(path)) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


class TestMigration:
    """Design "Migration Plan": PRAGMA-gated, additive, idempotent."""

    def _legacy(self, path):
        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute(_LEGACY_EDGES_DDL)
            conn.execute(_LEGACY_INBOX_DDL)
            conn.execute(
                "INSERT INTO inbox (id, sender_id, receiver_id, message, status) "
                "VALUES (1, 'a', 'b', 'hi', 'pending')"
            )
            conn.execute(
                "INSERT INTO delegation_edges (id, caller_id, worker_id, source, state, reason) "
                "VALUES (1, 'sup', 'w1', 'assign', 'open', 'no_callback')"
            )

    def test_adds_columns_once_and_keeps_rows(self, tmp_path, monkeypatch):
        # Arrange
        path = tmp_path / "legacy.db"
        self._legacy(path)
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)

        # Act
        database._migrate_delivery_failure_columns()
        database._migrate_delivery_failure_columns()

        # Assert
        assert _columns(path, "delegation_edges").count("reply_message_id") == 1
        inbox_cols = _columns(path, "inbox")
        assert inbox_cols.count("delivery_attempts") == 1
        assert inbox_cols.count("last_error") == 1
        with closing_transaction(sqlite3.connect(path)) as conn:
            inbox_row = conn.execute("SELECT delivery_attempts, last_error FROM inbox").fetchone()
            edge_row = conn.execute(
                "SELECT state, reply_message_id FROM delegation_edges"
            ).fetchone()
        assert inbox_row == (0, None), "legacy rows read as 'no failed attempts'"
        assert edge_row == ("open", None), "legacy open edges read as 'no reply in flight'"

    def test_database_that_already_has_the_columns_is_left_as_is(self, tmp_path, monkeypatch):
        # Arrange: a DB written by this version, with values in the new columns.
        path = tmp_path / "current.db"
        self._legacy(path)
        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute("ALTER TABLE delegation_edges ADD COLUMN reply_message_id INTEGER")
            conn.execute(
                "ALTER TABLE inbox ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("ALTER TABLE inbox ADD COLUMN last_error TEXT")
            conn.execute("UPDATE inbox SET delivery_attempts = 2, last_error = 'lost read'")
            conn.execute("UPDATE delegation_edges SET reply_message_id = 41")
        before = (_columns(path, "inbox"), _columns(path, "delegation_edges"))
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)

        # Act
        database._migrate_delivery_failure_columns()

        # Assert
        assert (_columns(path, "inbox"), _columns(path, "delegation_edges")) == before
        with closing_transaction(sqlite3.connect(path)) as conn:
            inbox_row = conn.execute("SELECT delivery_attempts, last_error FROM inbox").fetchone()
            edge_row = conn.execute("SELECT reply_message_id FROM delegation_edges").fetchone()
        assert inbox_row == (2, "lost read")
        assert edge_row == (41,)

    def test_only_the_missing_column_of_a_partly_migrated_table_is_added(
        self, tmp_path, monkeypatch
    ):
        # Arrange: a crash between the two inbox ALTERs left one of them applied.
        path = tmp_path / "partial.db"
        self._legacy(path)
        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute(
                "ALTER TABLE inbox ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
            )
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)

        # Act
        database._migrate_delivery_failure_columns()

        # Assert
        inbox_cols = _columns(path, "inbox")
        assert inbox_cols.count("delivery_attempts") == 1
        assert inbox_cols.count("last_error") == 1
        assert _columns(path, "delegation_edges").count("reply_message_id") == 1

    def test_creates_nothing_without_the_tables(self, tmp_path, monkeypatch):
        # Arrange
        path = tmp_path / "empty.db"
        sqlite3.connect(path).close()
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)

        # Act
        database._migrate_delivery_failure_columns()

        # Assert
        with closing_transaction(sqlite3.connect(path)) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master").fetchall()
        assert tables == []

    def test_a_table_that_exists_is_migrated_when_the_other_does_not(self, tmp_path, monkeypatch):
        # Arrange
        path = tmp_path / "edges_only.db"
        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute(_LEGACY_EDGES_DDL)
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)

        # Act
        database._migrate_delivery_failure_columns()

        # Assert
        assert "reply_message_id" in _columns(path, "delegation_edges")
        with closing_transaction(sqlite3.connect(path)) as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master")]
        assert "inbox" not in names, "the migration must not create a missing table"


class TestInitDbOnALegacyDatabase:
    """init_db() opens a pre-change database and the ORM reads its rows."""

    @pytest.fixture
    def legacy_home(self, tmp_path, monkeypatch):
        path = tmp_path / "legacy.db"
        skip = {"inbox", "delegation_edges"}
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(
            engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in skip]
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(_LEGACY_EDGES_DDL)
            conn.exec_driver_sql(_LEGACY_INBOX_DDL)
            conn.exec_driver_sql(
                "INSERT INTO inbox (id, sender_id, receiver_id, message, status, created_at) "
                "VALUES (1, 'w1', 'sup', 'hi', 'pending', '2026-11-17 10:00:00.000000')"
            )
            conn.exec_driver_sql(
                "INSERT INTO delegation_edges (id, caller_id, worker_id, source, state, reason, "
                "created_at) VALUES (1, 'sup', 'w1', 'assign', 'open', 'no_callback', "
                "'2026-11-17 10:00:00.000000')"
            )
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=False)
        try:
            yield path
        finally:
            engine.dispose()

    def test_init_db_adds_the_columns_and_legacy_rows_read_as_untouched(self, legacy_home):
        # Act
        database.init_db()
        database.init_db()  # a restart: idempotent

        # Assert
        assert _columns(legacy_home, "delegation_edges").count("reply_message_id") == 1
        assert _columns(legacy_home, "inbox").count("delivery_attempts") == 1
        [message] = get_inbox_messages("sup")
        assert (message.delivery_attempts, message.last_error) == (0, None)
        assert find_open_edge("sup", "w1").reply_message_id is None


class TestModels:
    """Task 1 Step 3: new fields default to "nothing happened yet"."""

    def test_inbox_message_defaults_to_no_failed_attempts(self):
        message = InboxMessage(
            id=1,
            sender_id="w1",
            receiver_id="sup",
            message="hi",
            status=MessageStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        assert (message.delivery_attempts, message.last_error) == (0, None)

    def test_delegation_edge_defaults_to_no_reply_in_flight(self):
        edge = DelegationEdge(
            id=1,
            caller_id="sup",
            worker_id="w1",
            source=EdgeSource.ASSIGN,
            state=EdgeState.OPEN,
            reason="no_callback",
            created_at=datetime.now(timezone.utc),
        )
        assert edge.reply_message_id is None

    def test_delivery_outcome_values(self):
        from cli_agent_orchestrator.models.inbox import DeliveryOutcome

        assert {o.value for o in DeliveryOutcome} == {
            "delivered",
            "failed",
            "uncertain",
            "retrying",
        }


class TestInboxRowConversion:
    def test_created_message_reads_with_no_failed_attempts(self, db):
        create_terminal("sup", "cao-s", "sup", "claude_code")
        created = create_inbox_message("w1", "sup", "hi")
        [read] = get_inbox_messages("sup")
        assert (created.delivery_attempts, created.last_error) == (0, None)
        assert (read.delivery_attempts, read.last_error) == (0, None)

    def test_recorded_failure_is_visible_on_the_read_message(self, db):
        create_terminal("sup", "cao-s", "sup", "claude_code")
        created = create_inbox_message("w1", "sup", "hi")
        database.record_delivery_failure(created.id, "lost terminal.read")
        [read] = get_inbox_messages("sup")
        assert (read.delivery_attempts, read.last_error) == (1, "lost terminal.read")


class TestReplyInFlight:
    """delegation-callback rule 1 and design D1."""

    def test_mark_keeps_edge_open_and_records_message(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        assert database.mark_reply_in_flight(edge.id, 41) is True
        again = find_open_edge("sup", "w1")
        assert (again.state, again.reply_message_id) == (EdgeState.OPEN, 41)

    def test_later_reply_replaces_the_mark(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        assert database.mark_reply_in_flight(edge.id, 42) is True
        assert find_open_edge("sup", "w1").reply_message_id == 42

    def test_mark_on_a_closed_edge_is_refused(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        close_delegation_edge(edge.id, EdgeState.WORKER_ENDED)
        assert database.mark_reply_in_flight(edge.id, 41) is False
        assert find_any_edge("sup", "w1").reply_message_id is None

    def test_mark_on_an_unknown_edge_is_refused(self, db):
        assert database.mark_reply_in_flight(999, 41) is False

    def test_delivery_closes_as_replied(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        assert database.close_edges_replied_by([41]) == 1
        closed = find_any_edge("sup", "w1")
        assert closed.state == EdgeState.REPLIED
        assert closed.closed_at is not None, "a closed edge records when it closed"

    def test_delivery_of_an_unrelated_message_closes_nothing(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        assert database.close_edges_replied_by([40, 43]) == 0
        assert find_open_edge("sup", "w1").reply_message_id == 41

    def test_delivery_of_an_empty_batch_closes_nothing(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        assert database.close_edges_replied_by([]) == 0
        assert find_open_edge("sup", "w1") is not None

    def test_delivery_of_a_replaced_reply_closes_nothing(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        database.mark_reply_in_flight(edge.id, 42)
        assert database.close_edges_replied_by([41]) == 0
        assert find_open_edge("sup", "w1").reply_message_id == 42

    def test_second_delivery_of_the_same_reply_counts_nothing(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        database.close_edges_replied_by([41])
        assert database.close_edges_replied_by([41]) == 0
        assert find_any_edge("sup", "w1").state == EdgeState.REPLIED

    def test_clear_returns_edges_with_their_reply_and_reopens(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        cleared = database.clear_reply_in_flight([41])
        assert [(e.id, e.reply_message_id) for e in cleared] == [(edge.id, 41)]
        after = find_open_edge("sup", "w1")
        assert (after.state, after.reply_message_id) == (EdgeState.OPEN, None)

    def test_clear_of_an_empty_list_returns_nothing(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        assert database.clear_reply_in_flight([]) == []
        assert find_open_edge("sup", "w1").reply_message_id == 41

    def test_clear_of_a_replaced_reply_keeps_the_newer_mark(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        database.mark_reply_in_flight(edge.id, 42)
        assert database.clear_reply_in_flight([41]) == []
        assert find_open_edge("sup", "w1").reply_message_id == 42

    def test_clear_leaves_a_closed_edge_closed(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(edge.id, 41)
        database.close_edges_replied_by([41])
        assert database.clear_reply_in_flight([41]) == []
        assert find_any_edge("sup", "w1").state == EdgeState.REPLIED

    def test_exclude_reply_in_flight(self, db):
        in_flight = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("other", "w1", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(in_flight.id, 41)
        assert [e.caller_id for e in list_open_edges_into("w1")] == ["sup", "other"]
        assert [e.caller_id for e in list_open_edges_into("w1", exclude_reply_in_flight=True)] == [
            "other"
        ]


class TestWorkerEndedWithReplyInFlight:
    """Design D8: a worker that ends leaves a reply-in-flight edge to the delivery outcome."""

    def test_inbound_edge_with_reply_in_flight_stays_open(self, db):
        # Arrange
        in_flight = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        waiting = create_delegation_edge("other", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("w1", "w9", EdgeSource.ASSIGN)
        database.mark_reply_in_flight(in_flight.id, 41)

        # Act
        closed = close_open_edges_for_terminal("w1", EdgeState.WORKER_ENDED)

        # Assert
        assert [e.id for e in closed] == [waiting.id]
        assert [(e.id, e.reply_message_id) for e in list_open_edges_into("w1")] == [
            (in_flight.id, 41)
        ]
        assert list_open_edges_out_of("w1") == []


class TestRecordDeliveryFailure:
    def test_counts_attempts_and_keeps_last_error(self, db):
        message_id = _inbox_row(db)
        assert database.record_delivery_failure(message_id, "first") == 1
        assert database.record_delivery_failure(message_id, "second") == 2
        with db() as s:
            row = s.get(InboxModel, message_id)
            assert (row.delivery_attempts, row.last_error) == (2, "second")

    def test_counts_are_per_message(self, db):
        first = _inbox_row(db)
        second = _inbox_row(db)
        database.record_delivery_failure(first, "x")
        database.record_delivery_failure(first, "x")
        assert database.record_delivery_failure(second, "y") == 1

    def test_unknown_message_returns_zero(self, db):
        assert database.record_delivery_failure(999, "x") == 0

    @pytest.mark.parametrize(
        "error",
        ["", "ünïcødé 終わり ​", "x" * 10_000, "quote ' and \" and ; DROP TABLE inbox"],
        ids=["empty", "unicode", "long", "sql-ish"],
    )
    def test_error_text_is_stored_verbatim(self, db, error):
        message_id = _inbox_row(db)
        database.record_delivery_failure(message_id, error)
        with db() as s:
            assert s.get(InboxModel, message_id).last_error == error
