"""UTC timestamp regressions for terminal activity and inbox messages."""

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    TerminalModel,
    create_inbox_message,
    get_terminal_metadata,
    list_pending_receiver_ids_older_than,
    list_terminals_by_session,
    update_last_active,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services.ui_state_service import _isoformat


@pytest.fixture(autouse=True)
def taipei_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=8)
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", session):
        yield session


def seed_terminal(db, terminal_id="t1", session_name="cao-s", last_active=None):
    with db() as session:
        session.add(
            TerminalModel(
                id=terminal_id,
                tmux_session=session_name,
                tmux_window="w",
                provider="claude_code",
                last_active=last_active,
            )
        )
        session.commit()


def assert_utc_now(value):
    assert value.utcoffset() == timedelta(0)
    assert abs(value - datetime.now(timezone.utc)) < timedelta(seconds=60)


def test_database_timestamps_are_aware_utc(db):
    seed_terminal(db)
    assert_utc_now(get_terminal_metadata("t1")["last_active"])

    assert update_last_active("t1")
    assert_utc_now(get_terminal_metadata("t1")["last_active"])

    [terminal] = list_terminals_by_session("cao-s")
    assert jsonable_encoder(terminal)["last_active"].endswith("+00:00")

    seed_terminal(db, "rx", "cao-rx")
    message = create_inbox_message("tx", "rx", "hello")
    assert_utc_now(message.created_at)


def test_models_serialize_naive_server_times_as_utc():
    value = datetime(2026, 10, 1, 13, 38, 12)
    terminal = Terminal(
        id="abcdef12",
        name="w",
        provider="claude_code",
        session_name="cao-s",
        last_active=value,
    )
    message = InboxMessage(
        id=1,
        sender_id="a",
        receiver_id="b",
        message="m",
        status=MessageStatus.PENDING,
        created_at=value,
    )

    assert json.loads(terminal.model_dump_json())["last_active"].endswith(("Z", "+00:00"))
    assert json.loads(message.model_dump_json())["created_at"].endswith(("Z", "+00:00"))
    assert _isoformat(value) == "2026-10-01T13:38:12+00:00"


def test_reconciliation_cutoff_uses_utc(db):
    now = datetime.now(timezone.utc)
    seed_terminal(db, "old", "old-session")
    seed_terminal(db, "new", "new-session")
    with db() as session:
        for receiver, age in (("old", 120), ("new", 5)):
            session.add(
                InboxModel(
                    sender_id="a",
                    receiver_id=receiver,
                    message="m",
                    status=MessageStatus.PENDING.value,
                    created_at=now - timedelta(seconds=age),
                )
            )
        session.commit()

    assert list_pending_receiver_ids_older_than(30) == ["old"]


@patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
@patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
@patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
@patch("cli_agent_orchestrator.services.cleanup_service.status_monitor")
@patch("cli_agent_orchestrator.services.cleanup_service.fifo_manager")
def test_retention_cutoff_uses_utc(_fifo, _status, log_dir, terminal_log_dir, db):
    from cli_agent_orchestrator.services import cleanup_service

    log_dir.exists.return_value = False
    terminal_log_dir.exists.return_value = False
    now = datetime.now(timezone.utc)
    seed_terminal(db, "keep", last_active=now - timedelta(days=7) + timedelta(hours=1))
    seed_terminal(db, "drop", last_active=now - timedelta(days=7) - timedelta(hours=1))

    with patch.object(cleanup_service, "SessionLocal", db):
        cleanup_service.cleanup_old_data()

    with db() as session:
        assert [row.id for row in session.query(TerminalModel).all()] == ["keep"]
