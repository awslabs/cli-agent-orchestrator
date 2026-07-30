"""Exact refusal-to-callback recovery lifecycle and one-shot invariants."""

from __future__ import annotations

import hashlib
import json
from datetime import timezone

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryCompletionRequest,
    CallbackRecoveryRequest,
    MessageStatus,
)
from cli_agent_orchestrator.services import (
    callback_recovery,
    companion_receipts,
    control_input_service,
    model_turn_receipt_contract,
)
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_MANAGED_ACP_PANE,
)
from cli_agent_orchestrator.services.control_input_journal import (
    ControlInputBinding,
    ControlInputJournal,
)

SOURCE = "worker01"
GENERATION = "worker-generation-1"
SUPERVISOR = "super01"
SUPERVISOR_GENERATION = "supervisor-generation-1"
SESSION_NAME = "cao-test"
PROVIDER_SESSION = "provider-session-1"
CONTROL = "refused-control-1"
SUMMARY = "report is complete"
REPORT_PATH = "/tmp/worktree/report.md"
CALLBACK_LINE = (
    f"[conduct-report] status=done task=task-1 report={REPORT_PATH} " f"summary={SUMMARY}"
)


@pytest.fixture
def recovery_context(isolated_memory_db, tmp_path, monkeypatch):
    now = "2026-07-30T12:00:00Z"
    with database.SessionLocal() as db:
        db.add_all(
            [
                database.TerminalModel(
                    id=SUPERVISOR,
                    tmux_session=SESSION_NAME,
                    tmux_window="supervisor",
                    provider="codex",
                    generation=SUPERVISOR_GENERATION,
                ),
                database.TerminalModel(
                    id=SOURCE,
                    tmux_session=SESSION_NAME,
                    tmux_window="worker",
                    provider="codex",
                    caller_id=SUPERVISOR,
                    generation=GENERATION,
                ),
                database.ManagedLaunchReservationModel(
                    reservation_id="reservation-1",
                    terminal_id=SOURCE,
                    generation=GENERATION,
                    session_name=SESSION_NAME,
                    provider="codex",
                    agent_profile="worker",
                    caller_id=SUPERVISOR,
                    working_directory="/tmp/worktree",
                    state="admitted",
                    request_json=json.dumps({"execution_mode": "acp"}),
                    observations_json="[]",
                    readiness_json=json.dumps({"provider_session_id": PROVIDER_SESSION}),
                    admission_json="{}",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db.commit()

    journal = ControlInputJournal(tmp_path / "control-input.sqlite3")
    request_sha256 = "c" * 64
    journal.open_intent(
        ControlInputBinding(
            request_id=CONTROL,
            terminal_id=SOURCE,
            pane_id="%1",
            window_id="@1",
            pane_pid=4242,
            generation=GENERATION,
            request_sha256=request_sha256,
        )
    )
    journal.mark_refused(CONTROL, reason_code=REASON_MANAGED_ACP_PANE)
    monkeypatch.setattr(control_input_service, "get_control_input_journal", lambda: journal)
    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path / "companion")
    occurrence = callback_recovery.refusal_occurrence(CONTROL)
    body = CallbackRecoveryRequest(
        operation_id="operation-1",
        project="project-1",
        task_id="task-1",
        run_id="task-1",
        source_terminal_id=SOURCE,
        source_generation=GENERATION,
        expected_provider="codex",
        expected_provider_session_id=PROVIDER_SESSION,
        expected_execution_mode="acp",
        supervisor_id=SUPERVISOR,
        supervisor_session=SESSION_NAME,
        refusal_control_id=CONTROL,
        refusal_occurrence_sha256=occurrence["refusal_occurrence_sha256"],
        refusal_request_sha256=request_sha256,
        callback_occurrence_id="task-1-r1",
        callback_status="done",
        callback_summary=SUMMARY,
        callback_message_sha256=hashlib.sha256(CALLBACK_LINE.encode()).hexdigest(),
        report_path=REPORT_PATH,
        report_sha256="d" * 64,
        source_head="e" * 40,
        publishing_lease_state="absent",
        publishing_lease_sha256="f" * 64,
        manifest_path="/tmp/state/run.json",
        manifest_sha256="1" * 64,
        finalization_identity_sha256="2" * 64,
    )
    return body


def _record_turn(admission):
    message = admission.message
    created = message.created_at.replace(tzinfo=timezone.utc)
    receipt = model_turn_receipt_contract.build_receipt(
        message_id=message.id,
        message_sha256=message.message_sha256,
        message_created_at=created,
        sender_id=SUPERVISOR,
        sender_generation=message.sender_generation,
        receiver_id=SOURCE,
        receiver_generation=GENERATION,
        provider="codex",
        provider_session_id=PROVIDER_SESSION,
        provider_turn_id="turn-1",
        submitted_at=created,
    )
    companion_receipts.record_message_ack(
        SOURCE,
        GENERATION,
        message_id=message.id,
        ack=receipt,
    )
    return receipt


def test_exact_retry_is_one_row_and_different_operation_cannot_repeat(recovery_context):
    first = callback_recovery.admit(recovery_context)
    replay = callback_recovery.admit(recovery_context)
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.message.id == first.message.id
    changed_line = CALLBACK_LINE.replace(SUMMARY, "changed callback bytes")
    different = recovery_context.model_copy(
        update={
            "operation_id": "operation-2",
            "callback_summary": "changed callback bytes",
            "callback_message_sha256": hashlib.sha256(changed_line.encode()).hexdigest(),
        }
    )
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="one-shot"):
        callback_recovery.admit(different)
    with database.SessionLocal() as db:
        assert db.query(database.CallbackRecoveryModel).count() == 1
        assert db.query(database.InboxModel).count() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_generation", "replacement-generation"),
        ("expected_provider_session_id", "replacement-session"),
        ("supervisor_id", "other-supervisor"),
        ("supervisor_session", "other-session"),
        ("refusal_occurrence_sha256", "0" * 64),
        ("refusal_request_sha256", "9" * 64),
    ],
)
def test_identity_conflicts_are_durable_zero_row_refusals(recovery_context, field, value):
    body = recovery_context.model_copy(update={field: value})
    with pytest.raises(callback_recovery.CallbackRecoveryRefused):
        callback_recovery.admit(body)
    operation_key = callback_recovery._operation_key(body)
    stored = callback_recovery.get(operation_key)
    assert stored["state"] == callback_recovery.STATE_REFUSED
    with database.SessionLocal() as db:
        assert db.query(database.InboxModel).count() == 0
    with pytest.raises(callback_recovery.CallbackRecoveryRefused):
        callback_recovery.admit(body)


def test_callback_digest_conflict_is_a_durable_zero_byte_refusal(recovery_context):
    body = recovery_context.model_copy(update={"callback_message_sha256": "0" * 64})
    with pytest.raises(callback_recovery.CallbackRecoveryRefused, match="canonical"):
        callback_recovery.admit(body)
    with database.SessionLocal() as db:
        row = db.query(database.CallbackRecoveryModel).one()
        assert row.state == callback_recovery.STATE_REFUSED
        assert row.reason_code == "callback-digest-mismatch"
        assert db.query(database.InboxModel).count() == 0


def test_turn_receipt_is_strict_revalidated_and_completion_binds_callback_row(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    assert callback_recovery.turn_receipt(admission.operation["operation_key"]) is None
    expected_receipt = _record_turn(admission)
    assert callback_recovery.turn_receipt(admission.operation["operation_key"]) == expected_receipt
    with database.SessionLocal() as db:
        callback = database.InboxModel(
            sender_id=SOURCE,
            receiver_id=SUPERVISOR,
            message=CALLBACK_LINE,
            status=MessageStatus.DELIVERED.value,
        )
        db.add(callback)
        db.commit()
        db.refresh(callback)
        callback_id = callback.id
        callback_created = callback.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    completed = callback_recovery.complete(
        admission.operation["operation_key"],
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback_id,
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback_created,
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    assert completed["state"] == callback_recovery.STATE_COMPLETED
    assert completed["callback_message_id"] == callback_id
    replay = callback_recovery.complete(
        admission.operation["operation_key"],
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback_id,
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback_created,
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    assert replay["state"] == callback_recovery.STATE_COMPLETED


def test_stored_receipt_is_revalidated_and_duplicate_json_fails_closed(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback_recovery.turn_receipt(admission.operation["operation_key"])
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, admission.operation["operation_key"])
        row.provider_turn_receipt_json = (
            '{"schema":"cao-model-turn-receipt-v1",' '"schema":"cao-model-turn-receipt-v1"}'
        )
        db.commit()
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="malformed"):
        callback_recovery.turn_receipt(admission.operation["operation_key"])


def test_ambiguous_delivery_holds_terminal_and_cannot_be_retried(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    callback_recovery.mark_delivery_ambiguous(
        admission.operation["operation_key"], reason_code="submit-ambiguous"
    )
    assert callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION)
    with pytest.raises(callback_recovery.CallbackRecoveryRefused, match="ambiguous"):
        callback_recovery.admit(recovery_context)


def test_v2_reservation_and_terminal_are_authoritative(recovery_context):
    now = "2026-07-30T12:00:00Z"
    with database.SessionLocal() as db:
        db.query(database.ManagedLaunchReservationModel).delete()
        db.query(database.TerminalModel).filter(database.TerminalModel.id == SOURCE).delete()
        db.add(
            database.ManagedLaunchV2ReservationModel(
                reservation_id="reservation-v2",
                terminal_id=SOURCE,
                generation=GENERATION,
                protocol_vintage="v2",
                session_name=SESSION_NAME,
                provider="codex",
                agent_profile="worker",
                caller_id=SUPERVISOR,
                working_directory="/tmp/worktree",
                obligation_generation="obligation-1",
                task_id="task-1",
                run_id="task-1",
                launch_nonce_digest="3" * 64,
                state="admitted",
                request_json="{}",
                binding_json=json.dumps({"native_session_id": PROVIDER_SESSION}),
                admission_json="{}",
                execution_mode="acp",
                execution_mode_source="request",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            database.ManagedLaunchV2TerminalModel(
                id=SOURCE,
                tmux_session=SESSION_NAME,
                tmux_window="worker",
                provider="codex",
                caller_id=SUPERVISOR,
                generation=GENERATION,
                protocol_vintage="v2",
            )
        )
        db.commit()
    admitted = callback_recovery.admit(recovery_context)
    assert admitted.operation["state"] == callback_recovery.STATE_PENDING
    assert admitted.message.expected_provider_session_id == PROVIDER_SESSION
