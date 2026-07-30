"""HTTP boundary behavior for dedicated callback recovery."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime

import pytest
from starlette.requests import Request

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.models.inbox import CallbackRecoveryRequest, InboxMessage, MessageStatus
from cli_agent_orchestrator.services import callback_recovery


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


def test_rebind_conflict_never_claims_zero_bytes(client, monkeypatch):
    def conflict(_body):
        raise callback_recovery.CallbackRecoveryConflict("already used")

    monkeypatch.setattr(callback_recovery, "admit", conflict)
    body = _body()
    body["source_terminal_id"] = "abcdef12"
    response = client.post("/terminals/abcdef12/callback-recoveries", json=body)
    assert response.status_code == 409
    assert response.json()["outcome"] == "conflict"
    assert response.json()["proven_zero_bytes"] is False


@pytest.mark.asyncio
async def test_slow_bridge_delivery_is_offloaded_from_event_loop(monkeypatch):
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
    # The request is waiting on a worker thread; this event-loop turn must run.
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result["operation_key"] == "operation-key"
