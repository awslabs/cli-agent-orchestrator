"""Identity binding and idempotency for the narrow managed inbox protocol."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import BoundInboxMessageRequest
from cli_agent_orchestrator.services import bound_inbox_message

RECEIVER = "worker01"
RECEIVER_GENERATION = "worker-generation-1"
SENDER = "super01"
SENDER_GENERATION = "supervisor-generation-1"
SESSION = "provider-session-1"
MESSAGE = "ordinary supervisor update"


@pytest.fixture
def bound_request(isolated_memory_db):
    now = "2026-07-30T12:00:00Z"
    request_json = json.dumps({"execution_mode": "acp"})
    readiness_json = json.dumps({"provider_session_id": SESSION})
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=SENDER,
                tmux_session="cao-test",
                tmux_window="supervisor",
                provider="codex",
                generation=SENDER_GENERATION,
            )
        )
        db.add(
            database.ManagedLaunchReservationModel(
                reservation_id="reservation-1",
                terminal_id=RECEIVER,
                generation=RECEIVER_GENERATION,
                session_name="cao-test",
                provider="codex",
                agent_profile="worker",
                caller_id=SENDER,
                working_directory="/tmp/worktree",
                state="admitted",
                request_json=request_json,
                observations_json="[]",
                readiness_json=readiness_json,
                admission_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()
    return BoundInboxMessageRequest(
        operation_id="operation-1",
        sender_id=SENDER,
        sender_generation=SENDER_GENERATION,
        message=MESSAGE,
        message_sha256=hashlib.sha256(MESSAGE.encode()).hexdigest(),
        expected_receiver_generation=RECEIVER_GENERATION,
        expected_provider_session_id=SESSION,
        expected_execution_mode="acp",
    )


def test_atomic_identity_mismatch_creates_no_row(bound_request):
    bad = bound_request.model_copy(
        update={"expected_receiver_generation": "replacement-generation"}
    )
    with pytest.raises(bound_inbox_message.BoundInboxConflict):
        bound_inbox_message.enqueue(RECEIVER, bad)
    with database.SessionLocal() as db:
        assert db.query(database.InboxModel).count() == 0


def test_exact_retry_returns_same_server_row(bound_request):
    first = bound_inbox_message.enqueue(RECEIVER, bound_request)
    again = bound_inbox_message.enqueue(RECEIVER, bound_request)
    assert first.replayed is False
    assert again.replayed is True
    assert again.message.id == first.message.id
    with database.SessionLocal() as db:
        assert db.query(database.InboxModel).count() == 1


def test_operation_id_rebind_is_refused(bound_request):
    bound_inbox_message.enqueue(RECEIVER, bound_request)
    changed = bound_request.model_copy(
        update={
            "message": MESSAGE + " changed",
            "message_sha256": hashlib.sha256((MESSAGE + " changed").encode()).hexdigest(),
        }
    )
    with pytest.raises(bound_inbox_message.BoundInboxConflict):
        bound_inbox_message.enqueue(RECEIVER, changed)


def test_concurrent_retry_creates_one_row(bound_request):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: bound_inbox_message.enqueue(RECEIVER, bound_request),
                range(2),
            )
        )
    assert {result.message.id for result in results} == {results[0].message.id}
    assert sorted(result.replayed for result in results) == [False, True]
    with database.SessionLocal() as db:
        assert db.query(database.InboxModel).count() == 1


def test_delivery_revalidation_refuses_replacement(bound_request):
    result = bound_inbox_message.enqueue(RECEIVER, bound_request)
    assert bound_inbox_message.current_delivery_binding_matches(result.message)
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchReservationModel)
            .filter(database.ManagedLaunchReservationModel.terminal_id == RECEIVER)
            .one()
        )
        row.readiness_json = json.dumps({"provider_session_id": "replacement-session"})
        db.commit()
    assert not bound_inbox_message.current_delivery_binding_matches(result.message)


def test_server_operation_lookup_needs_no_live_lifecycle(bound_request):
    result = bound_inbox_message.enqueue(RECEIVER, bound_request)
    with database.SessionLocal() as db:
        db.query(database.ManagedLaunchReservationModel).delete()
        db.commit()
    stored = bound_inbox_message.get(RECEIVER, bound_request.operation_id)
    assert stored.id == result.message.id


def test_existing_inbox_schema_migrates_additively(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE inbox (id INTEGER PRIMARY KEY, sender_id TEXT NOT NULL, "
            "receiver_id TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at DATETIME)"
        )
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_inbox_bound_message_schema()
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(inbox)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(inbox)")}
    assert {
        "operation_id",
        "message_sha256",
        "sender_generation",
        "expected_receiver_generation",
        "expected_provider_session_id",
        "expected_execution_mode",
    } <= columns
    assert "ix_inbox_operation_id" in indexes
