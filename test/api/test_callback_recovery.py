"""HTTP boundary behavior for dedicated callback recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime

import pytest
from starlette.requests import Request

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryRequest,
    InboxMessage,
    MessageStatus,
)
from cli_agent_orchestrator.services import (
    callback_recovery,
    companion_receipts,
    control_input_service,
)
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_MANAGED_ACP_PANE,
)
from cli_agent_orchestrator.services.control_input_journal import (
    ControlInputBinding,
    ControlInputJournal,
)


def _body() -> dict:
    return {
        "operation_id": "operation-1",
        "project": "project-1",
        "task_id": "task-1",
        "run_id": "task-1",
        "source_terminal_id": "worker01",
        "source_generation": "generation-1",
        "expected_provider": "codex",
        "expected_provider_session_id": "provider-session-1",
        "expected_execution_mode": "acp",
        "supervisor_id": "super01",
        "supervisor_session": "cao-test",
        "supervisor_generation": "supervisor-generation",
        "supervisor_pane_id": "%7",
        "refusal_control_id": "control-1",
        "refusal_occurrence_sha256": "a" * 64,
        "refusal_request_sha256": "b" * 64,
        "callback_occurrence_id": "task-1-r1",
        "callback_status": "done",
        "callback_summary": "complete",
        "callback_message_sha256": "c" * 64,
        "report_path": "/tmp/report.md",
        "report_sha256": "d" * 64,
        "source_head": "e" * 40,
        "publishing_lease_state": "absent",
        "publishing_lease_sha256": "f" * 64,
        "manifest_path": "/tmp/run.json",
        "manifest_sha256": "1" * 64,
        "finalization_identity_sha256": "2" * 64,
    }


def _admission() -> callback_recovery.RecoveryAdmission:
    message = InboxMessage(
        id=7,
        sender_id="super01",
        receiver_id="worker01",
        message="recovery prompt",
        status=MessageStatus.PENDING,
        created_at=datetime(2026, 7, 30, 12, 0, 0),
        message_sha256="3" * 64,
        sender_generation="supervisor-generation",
        expected_receiver_generation="generation-1",
        expected_provider_session_id="provider-session-1",
        expected_execution_mode="acp",
        expected_provider="codex",
        callback_recovery_key="operation-key",
    )
    return callback_recovery.RecoveryAdmission(
        operation={
            "state": callback_recovery.STATE_PENDING,
            "operation_key": "operation-key",
            "operation_id": "operation-1",
            "callback_occurrence_id": "task-1-r1",
            "report_sha256": "d" * 64,
            "source_head": "e" * 40,
        },
        message=message,
        replayed=False,
    )


def test_source_path_mismatch_is_a_zero_byte_refusal(client, monkeypatch):
    called = []
    monkeypatch.setattr(callback_recovery, "admit", lambda *_args: called.append(True))
    response = client.post("/terminals/abcdef12/callback-recoveries", json=_body())
    assert response.status_code == 409
    assert response.json()["proven_zero_bytes"] is True
    assert called == []


def test_lifecycle_v2_is_visible_but_default_off_and_refuses_before_admission(client, monkeypatch):
    """The rollout gate is a request-bound, pre-row zero-byte refusal."""
    monkeypatch.delenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", raising=False)
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    request = CallbackRecoveryRequest(**body)
    expected_key, expected_digest = callback_recovery.operation_identity(request)
    called = []
    monkeypatch.setattr(callback_recovery, "admit", lambda *_args: called.append(True))

    capability = client.get("/managed/recovery-capabilities")
    assert capability.status_code == 200
    assert capability.json()["callback_recovery"] == {
        "lifecycle_version": 2,
        "enabled": False,
        "request_schema": "cao-callback-recovery-request-v1",
        "operation_schema": "cao-callback-recovery-operation-v2",
        "callback_lookup_schema": "cao-callback-recovery-callback-lookup-v1",
        "providers": [],
        "pending_sweep": {
            "enabled": True,
            "interval_seconds": 30,
            "grace_seconds": 30,
        },
    }
    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)

    assert response.status_code == 503
    assert response.json() == {
        "schema": "cao-callback-recovery-lifecycle-disabled-v1",
        "outcome": "callback-recovery-disabled",
        "reason_code": "lifecycle-capability-disabled",
        "operation_key": expected_key,
        "request_sha256": expected_digest,
        "proven_zero_bytes": True,
    }
    assert called == []


@pytest.mark.parametrize("value", ["1", "TRUE", "yes"])
def test_lifecycle_v2_enable_values_admit(value, client, monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", value)
    monkeypatch.setattr(
        main.recovery_capabilities, "callback_recovery_admission_allowed", lambda _provider: True
    )
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    called = []
    monkeypatch.setattr(
        callback_recovery, "admit", lambda *_args: called.append(True) or _admission()
    )
    monkeypatch.setattr(main.inbox_service, "deliver_pending", lambda *_args, **_kwargs: None)

    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)

    assert response.status_code == 200
    assert called


def test_live_provider_authority_refuses_before_admission(client, monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
    monkeypatch.setattr(database, "callback_recovery_migration_ready", lambda: True)
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    called = []
    monkeypatch.setattr(callback_recovery, "admit", lambda *_args: called.append(True))

    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)

    assert response.status_code == 503
    assert response.json()["reason_code"] == "lifecycle-capability-disabled"
    assert called == []


def test_rebind_conflict_never_claims_zero_bytes(client, monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
    monkeypatch.setattr(
        main.recovery_capabilities, "callback_recovery_admission_allowed", lambda _provider: True
    )

    def conflict(_body):
        raise callback_recovery.CallbackRecoveryConflict("already used")

    monkeypatch.setattr(callback_recovery, "admit", conflict)
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)
    assert response.status_code == 409
    assert response.json()["outcome"] == "conflict"
    assert response.json()["proven_zero_bytes"] is False


def test_ambiguous_replay_never_claims_zero_bytes(client, monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
    monkeypatch.setattr(
        main.recovery_capabilities, "callback_recovery_admission_allowed", lambda _provider: True
    )

    def ambiguous(_body):
        raise callback_recovery.CallbackRecoveryAmbiguous("provider effect remains possible")

    monkeypatch.setattr(callback_recovery, "admit", ambiguous)
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)
    assert response.status_code == 409
    assert response.json()["outcome"] == "ambiguous"
    assert response.json()["proven_zero_bytes"] is False


def test_callback_receipt_lookup_is_read_only(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        callback_recovery,
        "callback_lookup",
        lambda key: calls.append(key)
        or {
            "schema": "cao-callback-recovery-callback-lookup-v1",
            "operation_key": "operation-key",
            "request_sha256": "a" * 64,
            "callback": {
                "schema": "cao-callback-registration-receipt-v1",
                "operation_key": "operation-key",
                "request_sha256": "a" * 64,
                "callback_message_id": 777,
                "callback_message_sha256": "b" * 64,
                "callback_created_at": "2026-07-30T12:00:00.000000Z",
                "sender_id": "abcdef12",
                "receiver_id": "1234abcd",
                "source_generation": "worker-generation",
                "supervisor_generation": "supervisor-generation",
                "supervisor_pane_id": "%7",
                "callback_occurrence_id": "task-1-r1",
                "registered_at": "2026-07-30T12:00:01.000000Z",
            },
        },
    )
    response = client.get("/callback-recoveries/operation-key/callback")
    assert response.status_code == 200
    assert response.json() == {
        "schema": "cao-callback-recovery-callback-lookup-v1",
        "operation_key": "operation-key",
        "request_sha256": "a" * 64,
        "callback": {
            "schema": "cao-callback-registration-receipt-v1",
            "operation_key": "operation-key",
            "request_sha256": "a" * 64,
            "callback_message_id": 777,
            "callback_message_sha256": "b" * 64,
            "callback_created_at": "2026-07-30T12:00:00.000000Z",
            "sender_id": "abcdef12",
            "receiver_id": "1234abcd",
            "source_generation": "worker-generation",
            "supervisor_generation": "supervisor-generation",
            "supervisor_pane_id": "%7",
            "callback_occurrence_id": "task-1-r1",
            "registered_at": "2026-07-30T12:00:01.000000Z",
        },
    }
    assert calls == ["operation-key"]


def test_callback_receipt_lookup_returns_typed_null_not_204(client, monkeypatch):
    monkeypatch.setattr(
        callback_recovery,
        "callback_lookup",
        lambda _key: {
            "schema": "cao-callback-recovery-callback-lookup-v1",
            "operation_key": "operation-key",
            "request_sha256": "b" * 64,
            "callback": None,
        },
    )

    response = client.get("/callback-recoveries/operation-key/callback")

    assert response.status_code == 200
    assert response.json()["callback"] is None


def test_real_http_handler_admits_authoritative_reservation(
    client,
    isolated_memory_db,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
    monkeypatch.setattr(
        main.recovery_capabilities, "callback_recovery_admission_allowed", lambda _provider: True
    )
    now = "2026-07-30T12:00:00Z"
    with database.SessionLocal() as db:
        db.add_all(
            [
                database.TerminalModel(
                    id="super01",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="codex",
                    generation="supervisor-generation",
                    pane_id="%7",
                ),
                database.TerminalModel(
                    id="abcdef12",
                    tmux_session="cao-test",
                    tmux_window="worker",
                    provider="codex",
                    caller_id="super01",
                    generation="generation-1",
                ),
                database.ManagedLaunchReservationModel(
                    reservation_id="reservation-1",
                    terminal_id="abcdef12",
                    generation="generation-1",
                    session_name="cao-test",
                    provider="codex",
                    agent_profile="worker",
                    caller_id="super01",
                    working_directory="/tmp/worktree",
                    state="admitted",
                    request_json=json.dumps(
                        {
                            "execution_mode": "acp",
                            "project": "project-1",
                            "task_id": "task-1",
                        }
                    ),
                    observations_json="[]",
                    readiness_json=json.dumps({"provider_session_id": "provider-session-1"}),
                    admission_json=json.dumps(
                        {
                            "context": {
                                "project": "project-1",
                                "task_id": "task-1",
                                "run_id": "task-1",
                            }
                        }
                    ),
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db.commit()
    journal = ControlInputJournal(tmp_path / "control.sqlite3")
    journal.open_intent(
        ControlInputBinding(
            request_id="control-1",
            terminal_id="abcdef12",
            pane_id="%1",
            window_id="@1",
            pane_pid=4242,
            generation="generation-1",
            request_sha256="b" * 64,
        )
    )
    journal.mark_refused("control-1", reason_code=REASON_MANAGED_ACP_PANE)
    monkeypatch.setattr(control_input_service, "get_control_input_journal", lambda: journal)
    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path / "companion")
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    occurrence = callback_recovery.refusal_occurrence("control-1")
    body["refusal_occurrence_sha256"] = occurrence["refusal_occurrence_sha256"]
    body["callback_message_sha256"] = hashlib.sha256(
        (
            "[conduct-report] status=done task=task-1 " "report=/tmp/report.md summary=complete"
        ).encode()
    ).hexdigest()

    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["outcome"] == callback_recovery.STATE_PENDING
    assert payload["receiver_generation"] == "generation-1"
    with database.SessionLocal() as db:
        assert db.query(database.CallbackRecoveryModel).count() == 1
        assert db.query(database.InboxModel).count() == 1


def test_dedicated_callback_handler_registers_without_delivery(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        callback_recovery,
        "create_callback",
        lambda *_args: {
            "message_id": 777,
            "sender_id": "abcdef12",
            "receiver_id": "1234abcd",
            "created_at": "2026-07-30T12:00:00.000000Z",
            "callback_occurrence_id": "task-1-r1",
            "replayed": False,
        },
    )
    deliveries = []
    monkeypatch.setattr(
        main.inbox_service,
        "deliver_pending",
        lambda *args, **kwargs: deliveries.append((args, kwargs)),
    )
    response = client.post(
        "/callback-recoveries/operation-key/callback",
        json={
            "callback_token": "x" * 32,
            "sender_id": "abcdef12",
            "receiver_id": "1234abcd",
            "callback_occurrence_id": "task-1-r1",
            "message": (
                "[conduct-report] status=done task=task-1 " "report=/tmp/report.md summary=complete"
            ),
        },
    )
    assert response.status_code == 200
    assert response.json()["message_id"] == 777
    assert deliveries == []


@pytest.mark.asyncio
async def test_slow_bridge_delivery_is_offloaded_from_event_loop(monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
    monkeypatch.setattr(
        main.recovery_capabilities, "callback_recovery_admission_allowed", lambda _provider: True
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_delivery(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(callback_recovery, "admit", lambda _body: _admission())
    monkeypatch.setattr(main.inbox_service, "deliver_pending", slow_delivery)
    monkeypatch.setattr(main, "get_plugin_registry", lambda _request: None)
    request = Request({"type": "http", "method": "POST", "path": "/"})
    task = asyncio.create_task(
        main.create_callback_recovery_endpoint(
            request,
            "worker01",
            CallbackRecoveryRequest(**_body()),
            [],
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    # The request is waiting on a worker thread; the actual health handler
    # must remain responsive on the event loop.
    monkeypatch.setattr(main, "get_backend", object)
    health = await asyncio.wait_for(main.health_check(), timeout=1)
    assert health["status"] == "ok"
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result["operation_key"] == "operation-key"
