"""Exact refusal-to-callback recovery lifecycle and one-shot invariants."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import timezone

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryCallbackRequest,
    CallbackRecoveryCompletionRequest,
    CallbackRecoveryRequest,
    CallbackRecoveryResolutionRequest,
)
from cli_agent_orchestrator.services import (
    callback_recovery,
    callback_text_contract,
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


@pytest.mark.parametrize(
    ("fields", "expected_length", "expected_digest"),
    [
        (
            {
                "status": "done",
                "task_id": "task-1",
                "report_path": "/tmp/run/report.md",
                "summary": "\n  finished safely  \nignored second line",
            },
            90,
            "f05c9f1534e2c5fe087cd8b41a4579b69e684722e78e1a33c1e26b7c4f8af532",
        ),
        (
            {
                "status": "failed",
                "task_id": "task-2",
                "report_path": "/tmp/run/report.md",
                "summary": "api_key=plainvalue bearer abcdefghijklmnop",
            },
            120,
            "7dd44e09a80904df89778040361610a31326111f8eaa7b9aa8e4636b225584c7",
        ),
        (
            {
                "status": "blocked",
                "task_id": "task-3",
                "report_path": "/tmp/run/report.md",
                "summary": "x" * 1000,
            },
            900,
            "bdd5c964257634ace536573e0590f1e90fea4fa9469b0c366d7a5acde9adb379",
        ),
    ],
)
def test_cross_repository_canonical_text_and_digest_vectors(
    fields,
    expected_length,
    expected_digest,
):
    message = callback_text_contract.canonical_callback_text(**fields)
    assert len(message) == expected_length
    assert hashlib.sha256(message.encode()).hexdigest() == expected_digest


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
                    request_json=json.dumps(
                        {
                            "execution_mode": "acp",
                            "project": "project-1",
                            "task_id": "task-1",
                        }
                    ),
                    observations_json="[]",
                    readiness_json=json.dumps({"provider_session_id": PROVIDER_SESSION}),
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


def _publish_callback(admission):
    prompt = admission.message.message
    token_assignment = next(
        item for item in prompt.split() if item.startswith("CAO_CALLBACK_RECOVERY_TOKEN=")
    )
    token = token_assignment.split("=", 1)[1]
    return callback_recovery.create_callback(
        admission.operation["operation_key"],
        CallbackRecoveryCallbackRequest(
            callback_token=token,
            sender_id=SOURCE,
            receiver_id=SUPERVISOR,
            callback_occurrence_id="task-1-r1",
            message=CALLBACK_LINE,
        ),
    )


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
            "callback_occurrence_id": "task-1-r2",
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
        ("project", "other-project"),
        ("task_id", "other-task"),
        ("run_id", "other-run"),
    ],
)
def test_workflow_claims_must_match_authoritative_reservation(
    recovery_context,
    field,
    value,
):
    body = recovery_context.model_copy(update={field: value})
    if field == "task_id":
        callback_line = CALLBACK_LINE.replace("task=task-1", f"task={value}")
        body = body.model_copy(
            update={"callback_message_sha256": hashlib.sha256(callback_line.encode()).hexdigest()}
        )
    with pytest.raises(callback_recovery.CallbackRecoveryRefused, match="project/task/run"):
        callback_recovery.admit(body)
    stored = callback_recovery.get(callback_recovery._operation_key(body))
    assert stored["state"] == callback_recovery.STATE_REFUSED
    assert stored["proven_zero_bytes"] is True


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
    callback = _publish_callback(admission)
    callback_id = callback["message_id"]
    callback_created = callback["created_at"]
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


def test_generic_inbox_row_cannot_complete_recovery(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback_recovery.turn_receipt(admission.operation["operation_key"])
    with database.SessionLocal() as db:
        generic = database.InboxModel(
            sender_id=SOURCE,
            receiver_id=SUPERVISOR,
            message=CALLBACK_LINE,
            status="pending",
        )
        db.add(generic)
        db.commit()
        db.refresh(generic)
        created = generic.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        callback_id = generic.id
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="contradicts"):
        callback_recovery.complete(
            admission.operation["operation_key"],
            CallbackRecoveryCompletionRequest(
                callback_message_id=callback_id,
                callback_message_sha256=recovery_context.callback_message_sha256,
                callback_created_at=created,
                finalization_identity_sha256=(recovery_context.finalization_identity_sha256),
            ),
        )


def test_callback_token_and_original_supervisor_generation_are_mandatory(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    key = admission.operation["operation_key"]
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="token"):
        callback_recovery.create_callback(
            key,
            CallbackRecoveryCallbackRequest(
                callback_token="x" * 32,
                sender_id=SOURCE,
                receiver_id=SUPERVISOR,
                callback_occurrence_id="task-1-r1",
                message=CALLBACK_LINE,
            ),
        )
    with database.SessionLocal() as db:
        supervisor = db.get(database.TerminalModel, SUPERVISOR)
        supervisor.generation = "replacement-supervisor-generation"
        db.commit()
    prompt = admission.message.message
    token = next(
        item.split("=", 1)[1]
        for item in prompt.split()
        if item.startswith("CAO_CALLBACK_RECOVERY_TOKEN=")
    )
    with pytest.raises(
        callback_recovery.CallbackRecoveryRefused,
        match="original supervisor generation",
    ):
        callback_recovery.create_callback(
            key,
            CallbackRecoveryCallbackRequest(
                callback_token=token,
                sender_id=SOURCE,
                receiver_id=SUPERVISOR,
                callback_occurrence_id="task-1-r1",
                message=CALLBACK_LINE,
            ),
        )
    assert callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION)


def test_completed_replay_survives_prompt_inbox_retention(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    callback_recovery.complete(
        admission.operation["operation_key"],
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    with database.SessionLocal() as db:
        db.query(database.InboxModel).filter(
            database.InboxModel.id == admission.message.id
        ).delete()
        db.commit()
    replay = callback_recovery.get(admission.operation["operation_key"])
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


def test_ambiguous_resolution_is_evidence_bound_and_releases_lifecycle(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]
    callback_recovery.mark_delivery_ambiguous(key, reason_code="submit-ambiguous")
    resolution = CallbackRecoveryResolutionRequest(
        outcome="proven-zero-provider-effect",
        evidence_sha256="4" * 64,
        detail="operator inspected the exact provider session journal",
    )
    resolved = callback_recovery.resolve_ambiguity(key, resolution)
    assert resolved["state"] == callback_recovery.STATE_RESOLVED
    assert callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION) is False
    assert callback_recovery.resolve_ambiguity(key, resolution)["state"] == (
        callback_recovery.STATE_RESOLVED
    )
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="evidence"):
        callback_recovery.resolve_ambiguity(
            key,
            resolution.model_copy(update={"evidence_sha256": "5" * 64}),
        )


def test_receipt_and_refusal_transitions_are_monotonic(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]
    callback_recovery.mark_delivery_ambiguous(key, reason_code="response-loss")
    _record_turn(admission)
    assert callback_recovery.turn_receipt(key) is not None
    assert callback_recovery.get(key)["state"] == callback_recovery.STATE_SUBMITTED
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="cannot refuse"):
        callback_recovery.mark_delivery_refused(
            key,
            reason_code="w13-fenced-before-provider-io",
            proven_before_provider_io=True,
        )
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="ambiguous"):
        callback_recovery.mark_delivery_ambiguous(key, reason_code="late-race")
    assert callback_recovery.get(key)["state"] == callback_recovery.STATE_SUBMITTED


def test_kimi_acp_reservation_is_eligible(recovery_context):
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, SOURCE).provider = "kimi_cli"
        db.get(database.ManagedLaunchReservationModel, "reservation-1").provider = "kimi_cli"
        db.commit()
    body = recovery_context.model_copy(update={"expected_provider": "kimi_cli"})
    admission = callback_recovery.admit(body)
    assert admission.operation["expected_provider"] == "kimi_cli"


def test_recovery_admission_serializes_with_generation_teardown_claim(
    recovery_context,
):
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def admit():
        started.set()
        outcome.append(callback_recovery.admit(recovery_context))
        finished.set()

    with callback_recovery.generation_lifecycle_claim(SOURCE, GENERATION):
        worker = threading.Thread(target=admit)
        worker.start()
        assert started.wait(timeout=2)
        assert finished.wait(timeout=0.1) is False
    worker.join(timeout=2)
    assert finished.is_set()
    assert outcome[0].operation["state"] == callback_recovery.STATE_PENDING


def test_generation_lifecycle_claim_is_reentrant_for_session_teardown(
    recovery_context,
):
    nested = False
    with callback_recovery.generation_lifecycle_claim(SOURCE, GENERATION):
        with callback_recovery.generation_lifecycle_claim(SOURCE, GENERATION):
            nested = True
    assert nested


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
                request_json=json.dumps({"project": "project-1"}),
                binding_json=json.dumps(
                    {
                        "native_session_id": PROVIDER_SESSION,
                        "attempt_id": "attempt-1",
                        "fencing_token_id": "fence-1",
                    }
                ),
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
