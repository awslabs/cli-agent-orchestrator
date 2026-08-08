"""Exact refusal-to-callback recovery lifecycle and one-shot invariants."""

from __future__ import annotations

import hashlib
import json
import shlex
import threading
from datetime import timezone

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryCallbackRequest,
    CallbackRecoveryCompletionRequest,
    CallbackRecoveryRequest,
    CallbackRecoveryResolutionRequest,
    MessageStatus,
)
from cli_agent_orchestrator.services import (
    callback_recovery,
    callback_text_contract,
    companion_receipts,
    control_input_service,
    generation_fence,
    model_turn_receipt_contract,
    terminal_service,
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
SUPERVISOR_PANE_ID = "%7"
SESSION_NAME = "cao-test"
PROVIDER_SESSION = "provider-session-1"
CONTROL = "refused-control-1"
SUMMARY = "report is complete"
REPORT_PATH = "/tmp/worktree/report.md"
CALLBACK_LINE = (
    f"[conduct-report] status=done task=task-1 report={REPORT_PATH} " f"summary={SUMMARY}"
)
PAIRED_TOP_LEVEL_REQUEST_FIELDS = frozenset(
    {
        "operation_id",
        "project",
        "task_id",
        "run_id",
        "source_terminal_id",
        "source_generation",
        "expected_provider",
        "expected_provider_session_id",
        "expected_execution_mode",
        "supervisor_id",
        "supervisor_session",
        "supervisor_generation",
        "supervisor_pane_id",
        "callback_occurrence_id",
        "refusal_control_id",
        "refusal_occurrence_sha256",
        "refusal_request_sha256",
        "callback_status",
        "callback_summary",
        "callback_message_sha256",
        "report_path",
        "report_sha256",
        "source_head",
        "publishing_lease_state",
        "publishing_lease_sha256",
        "manifest_path",
        "manifest_sha256",
        "finalization_identity_sha256",
    }
)


def _response_loss_readback_is_adoptable(body, operation):
    """Mirror the paired strict identity proof without any nested-data fallback."""
    request = body.model_dump(mode="json")
    nested = operation.get("request") if isinstance(operation, dict) else None
    return (
        isinstance(nested, dict)
        and operation.get("schema") == callback_recovery.OPERATION_SCHEMA
        and operation.get("request_identity_schema") == callback_recovery.REQUEST_SCHEMA
        and nested == request
        and hashlib.sha256(
            json.dumps(nested, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        == operation.get("request_sha256")
        and {field: operation.get(field) for field in PAIRED_TOP_LEVEL_REQUEST_FIELDS} == request
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
                    generation=None,
                    callback_target_generation=SUPERVISOR_GENERATION,
                    pane_id=SUPERVISOR_PANE_ID,
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
        supervisor_generation=SUPERVISOR_GENERATION,
        supervisor_pane_id=SUPERVISOR_PANE_ID,
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


def _record_turn(admission, *, provider="codex"):
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
        provider=provider,
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


def _commit_callback(admission, callback):
    """Model the delivery adapter's exact post-effect journal transition."""
    key = admission.operation["operation_key"]
    callback_recovery.complete(
        key,
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=admission.operation["callback_message_sha256"],
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=admission.operation["finalization_identity_sha256"],
        ),
    )
    callback_recovery.claim_callback_effect(key, callback["message_id"])
    return callback_recovery.commit_callback_effect(key, callback["message_id"])


def test_generated_recovery_command_is_exact_and_route_unattested(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    command = admission.message.message.rsplit("\n\n", 1)[1]
    argv = shlex.split(command)
    assert argv[:3] == [
        f"CAO_CALLBACK_RECOVERY_KEY={admission.operation['operation_key']}",
        argv[1],
        "conduct",
    ]
    assert argv[1].startswith("CAO_CALLBACK_RECOVERY_TOKEN=")
    assert argv[2:] == [
        "conduct",
        "report",
        "--task",
        "task-1",
        "--project",
        "project-1",
        "--status",
        "done",
        "--report",
        REPORT_PATH,
        "--summary",
        SUMMARY,
    ]
    assert {"--model", "--effort", "--route-evidence"}.isdisjoint(argv)


def test_operation_readback_echoes_the_immutable_canonical_request(recovery_context):
    admission = callback_recovery.admit(recovery_context)

    operation = callback_recovery.get(admission.operation["operation_key"])

    assert operation["schema"] == "cao-callback-recovery-operation-v2"
    assert operation["request_identity_schema"] == "cao-callback-recovery-request-v1"
    assert operation["request"] == recovery_context.model_dump(mode="json")
    assert (
        operation["request_sha256"]
        == hashlib.sha256(
            json.dumps(operation["request"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert set(operation["request"]) == PAIRED_TOP_LEVEL_REQUEST_FIELDS
    assert {field: operation.get(field) for field in PAIRED_TOP_LEVEL_REQUEST_FIELDS} == operation[
        "request"
    ]


def test_response_loss_readback_is_adoptable_only_with_exact_identity(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]

    readback = callback_recovery.get(key)

    assert _response_loss_readback_is_adoptable(recovery_context, readback)
    replay = callback_recovery.admit(recovery_context)
    assert replay.replayed is True
    assert _response_loss_readback_is_adoptable(recovery_context, callback_recovery.get(key))
    assert not _response_loss_readback_is_adoptable(
        recovery_context, {**readback, "callback_summary": "changed"}
    )
    assert not _response_loss_readback_is_adoptable(
        recovery_context,
        {key: value for key, value in readback.items() if key != "callback_status"},
    )

    changed = recovery_context.model_copy(update={"report_sha256": "9" * 64})
    with pytest.raises(callback_recovery.CallbackRecoveryIdentityConflict):
        callback_recovery.admit(changed)
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, key)
        assert db.query(database.CallbackRecoveryModel).count() == 1
        assert db.query(database.InboxModel).count() == 1
        assert row.provider_turn_receipt_json is None
        assert row.callback_message_id is None


def test_request_storage_tampering_quarantines_the_operation(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, key)
        row.request_json = json.dumps({"foreign": True})
        db.commit()

    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="canonical request"):
        callback_recovery.get(key)


def test_operation_key_owner_conflict_returns_the_durable_owner_proof(recovery_context):
    admitted = callback_recovery.admit(recovery_context)
    conflicting = recovery_context.model_copy(update={"report_sha256": "9" * 64})

    with pytest.raises(callback_recovery.CallbackRecoveryIdentityConflict) as raised:
        callback_recovery.admit(conflicting)

    proof = raised.value.response
    assert proof["schema"] == "cao-callback-recovery-identity-conflict-v1"
    assert proof["reason_code"] == "operation-key-owned"
    assert proof["submitted_operation_key"] == admitted.operation["operation_key"]
    assert proof["owner_operation_key"] == admitted.operation["operation_key"]
    assert proof["owner_request_sha256"] == admitted.operation["request_sha256"]
    assert proof["proven_zero_provider_bytes_for_submitted_request"] is True


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


def test_callback_occurrence_is_independently_one_shot_across_refusals(
    recovery_context,
):
    callback_recovery.admit(recovery_context)
    journal = control_input_service.get_control_input_journal()
    second_control = "refused-control-2"
    second_request_sha256 = "8" * 64
    journal.open_intent(
        ControlInputBinding(
            request_id=second_control,
            terminal_id=SOURCE,
            pane_id="%1",
            window_id="@1",
            pane_pid=4242,
            generation=GENERATION,
            request_sha256=second_request_sha256,
        )
    )
    journal.mark_refused(second_control, reason_code=REASON_MANAGED_ACP_PANE)
    occurrence = callback_recovery.refusal_occurrence(second_control)
    distinct_refusal = recovery_context.model_copy(
        update={
            "operation_id": "operation-distinct-refusal",
            "refusal_control_id": second_control,
            "refusal_occurrence_sha256": occurrence["refusal_occurrence_sha256"],
            "refusal_request_sha256": second_request_sha256,
        }
    )
    with pytest.raises(
        callback_recovery.CallbackRecoveryConflict,
        match="callback occurrence already",
    ):
        callback_recovery.admit(distinct_refusal)
    with database.SessionLocal() as db:
        assert db.query(database.CallbackRecoveryModel).count() == 1


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
        ("expected_provider", "kimi_cli"),
        ("expected_provider_session_id", "replacement-session"),
        ("supervisor_id", "other-supervisor"),
        ("supervisor_session", "other-session"),
        ("supervisor_generation", "replacement-supervisor-generation"),
        ("supervisor_pane_id", "%99"),
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


def test_unclassified_refusal_is_persisted_as_ambiguity(
    recovery_context,
    monkeypatch,
):
    def unclassified(*_args, **_kwargs):
        raise callback_recovery.CallbackRecoveryRefused(
            "future refusal",
            reason_code="future-unclassified-refusal",
        )

    monkeypatch.setattr(callback_recovery, "_reservation_identity", unclassified)
    with pytest.raises(callback_recovery.CallbackRecoveryAmbiguous):
        callback_recovery.admit(recovery_context)
    with database.SessionLocal() as db:
        row = db.query(database.CallbackRecoveryModel).one()
        assert row.state == callback_recovery.STATE_AMBIGUOUS
        assert row.reason_code == "unclassified-refusal-manual-resolution-required"


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
    intent = callback_recovery.complete(
        admission.operation["operation_key"],
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback_id,
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback_created,
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    assert intent["state"] == callback_recovery.STATE_SUBMITTED
    assert intent["callback_attempt_state"] == callback_recovery.CALLBACK_ATTEMPT_REGISTERED
    completed = _commit_callback(admission, callback)
    assert completed["state"] == callback_recovery.STATE_COMPLETED
    assert completed["callback_message_id"] == callback_id
    assert completed["callback_consumed"] is True
    assert callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION) is False
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


def test_callback_lookup_returns_immutable_registration_receipt(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)

    lookup = callback_recovery.callback_lookup(admission.operation["operation_key"])

    assert lookup["schema"] == callback_recovery.CALLBACK_LOOKUP_SCHEMA
    assert lookup["operation_key"] == admission.operation["operation_key"]
    assert lookup["request_sha256"] == admission.operation["request_sha256"]
    registration = lookup["callback"]
    assert {key: value for key, value in registration.items() if key != "registered_at"} == {
        "schema": "cao-callback-registration-receipt-v1",
        "operation_key": admission.operation["operation_key"],
        "request_sha256": admission.operation["request_sha256"],
        "callback_message_id": callback["message_id"],
        "callback_message_sha256": recovery_context.callback_message_sha256,
        "callback_created_at": callback["created_at"],
        "sender_id": SOURCE,
        "receiver_id": SUPERVISOR,
        "source_generation": GENERATION,
        "supervisor_generation": SUPERVISOR_GENERATION,
        "supervisor_pane_id": SUPERVISOR_PANE_ID,
        "callback_occurrence_id": recovery_context.callback_occurrence_id,
    }
    assert isinstance(registration["registered_at"], str)
    assert registration["registered_at"].endswith("Z")


def test_completed_delivery_remains_closed_after_inbox_retention(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    _commit_callback(admission, callback)
    assert not callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION)
    with database.SessionLocal() as db:
        row = db.get(
            database.CallbackRecoveryModel,
            admission.operation["operation_key"],
        )
        assert row.callback_consumed_at is not None
        db.query(database.InboxModel).filter(
            database.InboxModel.id == callback["message_id"]
        ).delete()
        db.commit()
    assert not callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION)
    assert callback_recovery.get(admission.operation["operation_key"])["callback_consumed"]


def test_ordinary_supervisor_gets_distinct_callback_target_generation(
    isolated_memory_db,
):
    created = database.create_terminal(
        "normal01",
        "cao-normal",
        "supervisor",
        "codex",
        pane_id="%7",
    )
    generation = created["callback_target_generation"]
    assert generation
    assert generation != created["pane_id"]
    assert created["generation"] is None
    reread = database.get_terminal_metadata("normal01")
    assert reread["callback_target_generation"] == generation


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
    with pytest.raises(callback_recovery.CallbackRecoveryPending, match="not registered"):
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
        supervisor.pane_id = "replacement-supervisor-generation"
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
    operation = callback_recovery.get(key)
    assert operation["callback_attempt_state"] == (
        callback_recovery.CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED
    )
    assert callback_recovery.callback_lookup(key)["callback"] is None
    with database.SessionLocal() as db:
        assert (
            db.query(database.InboxModel)
            .filter(database.InboxModel.callback_completion_key == key)
            .count()
            == 0
        )
    with pytest.raises(callback_recovery.CallbackRecoveryRefused, match="already refused"):
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
    assert callback_recovery.terminal_has_open_recovery(SUPERVISOR, SUPERVISOR_GENERATION)


def test_claimed_callback_effect_is_ambiguous_and_never_auto_replayed(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    key = admission.operation["operation_key"]
    callback_recovery.complete(
        key,
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )

    callback_recovery.claim_callback_effect(key, callback["message_id"])
    callback_recovery.mark_callback_effect_ambiguous(key)

    operation = callback_recovery.get(key)
    assert operation["state"] == callback_recovery.STATE_SUBMITTED
    assert operation["callback_attempt_state"] == (
        callback_recovery.CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS
    )
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="not eligible"):
        callback_recovery.claim_callback_effect(key, callback["message_id"])


def test_callback_effect_requires_durable_completion_intent(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    key = admission.operation["operation_key"]

    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="completion intent"):
        callback_recovery.claim_callback_effect(key, callback["message_id"])

    callback_recovery.complete(
        key,
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    callback_recovery.claim_callback_effect(key, callback["message_id"])


def test_post_registration_supervisor_replacement_is_durable_zero_effect(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    key = admission.operation["operation_key"]
    callback_recovery.complete(
        key,
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, SUPERVISOR).pane_id = "replacement-pane"
        db.commit()

    with pytest.raises(callback_recovery.CallbackRecoveryRefused, match="no longer live"):
        callback_recovery.claim_callback_effect(key, callback["message_id"])

    operation = callback_recovery.get(key)
    assert operation["state"] == callback_recovery.STATE_SUBMITTED
    assert operation["callback_attempt_state"] == (
        callback_recovery.CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED
    )


def test_admin_disposition_releases_exact_undeliverable_callback(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    key = admission.operation["operation_key"]
    callback_recovery.complete(
        key,
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, SUPERVISOR).pane_id = "replacement-pane"
        db.commit()
    with pytest.raises(callback_recovery.CallbackRecoveryRefused):
        callback_recovery.claim_callback_effect(key, callback["message_id"])

    disposed = callback_recovery.dispose_callback_undeliverable(
        key,
        callback_recovery.CallbackRecoveryDispositionRequest(
            outcome="provider-effect-proven-callback-undeliverable",
            evidence_sha256="a" * 64,
            detail="admin verified callback cannot reach the original supervisor",
        ),
    )
    assert disposed["state"] == callback_recovery.STATE_CALLBACK_UNDELIVERABLE
    assert disposed["disposition"]["outcome"] == "provider-effect-proven-callback-undeliverable"
    assert disposed["disposition"]["certainty"] == "proven-zero-callback-effect"
    assert callback_recovery.get(key)["state"] == callback_recovery.STATE_CALLBACK_UNDELIVERABLE
    assert not callback_recovery.terminal_has_open_recovery(SOURCE, GENERATION)


def test_admin_disposition_retains_malformed_provider_receipt(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    key = admission.operation["operation_key"]
    callback_recovery.complete(
        key,
        CallbackRecoveryCompletionRequest(
            callback_message_id=callback["message_id"],
            callback_message_sha256=recovery_context.callback_message_sha256,
            callback_created_at=callback["created_at"],
            finalization_identity_sha256=recovery_context.finalization_identity_sha256,
        ),
    )
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, key)
        db.get(database.TerminalModel, SUPERVISOR).pane_id = "replacement-pane"
        row.provider_turn_receipt_json = '{"schema":"fabricated"}'
        db.commit()
    with pytest.raises(callback_recovery.CallbackRecoveryConflict, match="provider receipt"):
        callback_recovery.dispose_callback_undeliverable(
            key,
            callback_recovery.CallbackRecoveryDispositionRequest(
                outcome="provider-effect-proven-callback-undeliverable",
                evidence_sha256="a" * 64,
                detail="must retain malformed provider evidence",
            ),
        )
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, key)
        assert row.state == callback_recovery.STATE_SUBMITTED
        assert row.callback_admin_disposition_json is None


def test_closed_workspace_retirement_holds_source_and_supervisor_rows(
    recovery_context,
    monkeypatch,
):
    callback_recovery.admit(recovery_context)
    monkeypatch.setattr(
        terminal_service,
        "delete_terminal",
        lambda *_args, **_kwargs: pytest.fail("recovery-held row was deleted"),
    )

    with pytest.raises(
        terminal_service.TerminalGenerationMismatchError,
        match="open callback-recovery",
    ):
        terminal_service.retire_closed_workspace_session(SESSION_NAME)

    with database.SessionLocal() as db:
        assert db.get(database.TerminalModel, SOURCE) is not None
        assert db.get(database.TerminalModel, SUPERVISOR) is not None


def test_closed_workspace_retirement_uses_exact_managed_cleanup(
    recovery_context,
    monkeypatch,
):
    monkeypatch.setattr(
        terminal_service.provider_manager,
        "cleanup_provider",
        lambda _terminal_id: None,
    )
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", lambda _terminal_id: None)
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "clear_terminal",
        lambda _terminal_id: None,
    )

    retired = terminal_service.retire_closed_workspace_session(SESSION_NAME)

    assert set(retired) == {SOURCE, SUPERVISOR}
    with database.SessionLocal() as db:
        assert db.get(database.TerminalModel, SOURCE) is None
        assert db.get(database.TerminalModel, SUPERVISOR) is None


def test_observed_terminal_retirement_treats_concurrent_absence_as_complete(
    monkeypatch,
):
    observed = {
        "id": SOURCE,
        "generation": GENERATION,
        "pane_id": "%11",
        "tmux_session": SESSION_NAME,
    }
    metadata_reads = iter((observed, None))
    monkeypatch.setattr(
        terminal_service,
        "_get_terminal_metadata_any",
        lambda _terminal_id: next(metadata_reads),
    )
    monkeypatch.setattr(
        terminal_service,
        "_delete_terminal_claimed",
        lambda *_args, **_kwargs: pytest.fail("already-retired row was deleted again"),
    )

    assert terminal_service.retire_observed_terminal(
        SOURCE,
        expected_session=SESSION_NAME,
        expected_pane_id="%11",
    )


def test_completed_replay_survives_prompt_inbox_retention(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    _commit_callback(admission, callback)
    with database.SessionLocal() as db:
        db.query(database.InboxModel).filter(
            database.InboxModel.id.in_([admission.message.id, callback["message_id"]])
        ).delete()
        db.commit()
    replay = callback_recovery.get(admission.operation["operation_key"])
    assert replay["state"] == callback_recovery.STATE_COMPLETED
    exact_admit_replay = callback_recovery.admit(recovery_context)
    assert exact_admit_replay.replayed is True
    assert exact_admit_replay.message is None
    assert exact_admit_replay.operation["admission_response"]["message_id"] == (
        admission.message.id
    )
    assert _publish_callback(admission) == {
        **callback,
        "replayed": True,
    }


def test_callback_receipt_and_exact_post_replay_survive_inbox_retention(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    _record_turn(admission)
    callback = _publish_callback(admission)
    key = admission.operation["operation_key"]
    with database.SessionLocal() as db:
        stored = db.get(database.InboxModel, callback["message_id"])
        assert stored.expected_receiver_generation == SUPERVISOR_GENERATION
        callback_message = database._inbox_message_from_row(stored)
        assert callback_recovery.current_delivery_binding_matches(callback_message)
        supervisor = db.get(database.TerminalModel, SUPERVISOR)
        supervisor.pane_id = "replacement-supervisor-generation"
        db.commit()
        assert not callback_recovery.current_delivery_binding_matches(callback_message)
        db.delete(stored)
        db.commit()
    receipt = callback_recovery.callback_receipt(key)
    assert receipt == {**callback, "replayed": True}
    replay = _publish_callback(admission)
    assert replay == receipt
    with database.SessionLocal() as db:
        assert (
            db.query(database.InboxModel)
            .filter(database.InboxModel.callback_completion_key == key)
            .count()
            == 0
        )


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
    with pytest.raises(callback_recovery.CallbackRecoveryAmbiguous, match="ambiguous"):
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


def test_zero_effect_resolution_reconciles_existing_strict_receipt(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]
    callback_recovery.mark_delivery_ambiguous(key, reason_code="submit-ambiguous")
    _record_turn(admission)
    resolution = CallbackRecoveryResolutionRequest(
        outcome="proven-zero-provider-effect",
        evidence_sha256="6" * 64,
        detail="operator inspected the exact provider session journal",
    )
    with pytest.raises(
        callback_recovery.CallbackRecoveryConflict,
        match="durable provider receipt",
    ):
        callback_recovery.resolve_ambiguity(key, resolution)
    stored = callback_recovery.get(key)
    assert stored["state"] == callback_recovery.STATE_SUBMITTED
    assert stored["proven_zero_bytes"] is False


def test_receipt_vs_zero_effect_resolution_has_one_monotonic_winner(
    recovery_context,
):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]
    resolution = CallbackRecoveryResolutionRequest(
        outcome="proven-zero-provider-effect",
        evidence_sha256="7" * 64,
        detail="operator inspected the exact provider session journal",
    )
    start = threading.Barrier(2)
    results: list[str] = []

    def provider() -> None:
        try:
            with generation_fence.admission_critical_section(
                companion_receipts.COMPANION_DIR,
                SOURCE,
                GENERATION,
            ):
                callback_recovery.assert_provider_delivery_admissible(
                    key,
                    terminal_id=SOURCE,
                    generation=GENERATION,
                    message_id=str(admission.message.id),
                )
                start.wait()
                _record_turn(admission)
            results.append("receipt")
        except callback_recovery.CallbackRecoveryError:
            results.append("fenced")

    def resolver() -> None:
        start.wait()
        callback_recovery.mark_delivery_ambiguous(key, reason_code="submit-ambiguous")
        try:
            callback_recovery.resolve_ambiguity(key, resolution)
            results.append("resolved")
        except callback_recovery.CallbackRecoveryConflict:
            results.append("receipt-observed")

    threads = [threading.Thread(target=provider), threading.Thread(target=resolver)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    stored = callback_recovery.get(key)
    assert stored["state"] == callback_recovery.STATE_SUBMITTED
    assert sorted(results) == ["receipt", "receipt-observed"]
    assert callback_recovery.turn_receipt(key) is not None


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


def test_inflight_receipt_wins_concurrent_zero_effect_refusal(recovery_context):
    admission = callback_recovery.admit(recovery_context)
    key = admission.operation["operation_key"]
    receipt_locked = threading.Event()
    release_receipt = threading.Event()
    refusal_errors = []

    def publish_receipt():
        with generation_fence.admission_critical_section(
            companion_receipts.COMPANION_DIR, SOURCE, GENERATION
        ):
            receipt_locked.set()
            assert release_receipt.wait(timeout=3)
            _record_turn(admission)

    def refuse():
        assert receipt_locked.wait(timeout=2)
        try:
            callback_recovery.mark_delivery_refused(
                key,
                reason_code="w13-fenced-before-provider-io",
                proven_before_provider_io=True,
            )
        except callback_recovery.CallbackRecoveryConflict as exc:
            refusal_errors.append(str(exc))

    publisher = threading.Thread(target=publish_receipt)
    refuser = threading.Thread(target=refuse)
    publisher.start()
    refuser.start()
    assert receipt_locked.wait(timeout=2)
    release_receipt.set()
    publisher.join(timeout=3)
    refuser.join(timeout=3)

    assert not publisher.is_alive()
    assert not refuser.is_alive()
    assert refusal_errors == ["cannot refuse recovery delivery after a durable provider receipt"]
    assert callback_recovery.turn_receipt(key) is not None
    assert callback_recovery.get(key)["state"] == callback_recovery.STATE_SUBMITTED


def test_kimi_acp_reservation_is_eligible(recovery_context):
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, SOURCE).provider = "kimi_cli"
        db.get(database.ManagedLaunchReservationModel, "reservation-1").provider = "kimi_cli"
        db.commit()
    body = recovery_context.model_copy(update={"expected_provider": "kimi_cli"})
    admission = callback_recovery.admit(body)
    _record_turn(admission, provider="kimi_cli")
    callback = _publish_callback(admission)
    completed = _commit_callback(admission, callback)
    assert completed["state"] == callback_recovery.STATE_COMPLETED


def test_non_acp_authoritative_mode_is_rejected(recovery_context):
    with database.SessionLocal() as db:
        reservation = db.get(database.ManagedLaunchReservationModel, "reservation-1")
        request = json.loads(reservation.request_json)
        request["execution_mode"] = "native_tui"
        reservation.request_json = json.dumps(request)
        db.commit()
    with pytest.raises(
        callback_recovery.CallbackRecoveryRefused,
        match="identity contradicts",
    ):
        callback_recovery.admit(recovery_context)


@pytest.mark.parametrize(
    ("terminal_id", "generation"),
    [
        (SOURCE, GENERATION),
        (SUPERVISOR, SUPERVISOR_GENERATION),
    ],
)
def test_recovery_admission_serializes_with_generation_teardown_claim(
    recovery_context,
    terminal_id,
    generation,
):
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def admit():
        started.set()
        outcome.append(callback_recovery.admit(recovery_context))
        finished.set()

    with callback_recovery.generation_lifecycle_claim(terminal_id, generation):
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


def test_opposing_admission_and_session_retirement_use_one_lock_order(
    recovery_context,
    monkeypatch,
):
    """Source sorts after supervisor, the historical deadlock ordering."""
    start = threading.Barrier(3)
    results = []
    errors = []
    monkeypatch.setattr(
        callback_recovery,
        "_admit_locked",
        lambda _body: results.append("admitted"),
    )
    monkeypatch.setattr(
        terminal_service,
        "_delete_terminal_claimed",
        lambda *_args, **_kwargs: True,
    )

    def run_admission():
        try:
            start.wait(timeout=2)
            callback_recovery.admit(recovery_context)
        except Exception as exc:
            errors.append(exc)

    def run_retirement():
        try:
            start.wait(timeout=2)
            terminal_service.retire_closed_workspace_session(SESSION_NAME)
            results.append("retired")
        except Exception as exc:
            errors.append(exc)

    admission = threading.Thread(target=run_admission)
    retirement = threading.Thread(target=run_retirement)
    admission.start()
    retirement.start()
    start.wait(timeout=2)
    admission.join(timeout=3)
    retirement.join(timeout=3)

    assert not admission.is_alive()
    assert not retirement.is_alive()
    assert errors == []
    assert set(results) == {"admitted", "retired"}


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
    _record_turn(admitted)
    callback = _publish_callback(admitted)
    completed = _commit_callback(admitted, callback)
    assert completed["state"] == callback_recovery.STATE_COMPLETED


class TestAsyncSessionClaimCancellation:
    """A task cancelled around the off-loop flock acquire must never orphan the
    eventual descriptor. The worker itself releases it when the caller abandons
    the wait; the lock-serialized handoff guarantees exactly one owner."""

    def test_flock_failure_closes_the_unowned_descriptor(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator.services import callback_recovery

        closed: list[int] = []
        monkeypatch.setattr(callback_recovery.os, "open", lambda *args, **kwargs: 73)

        def flock_fails(*args, **kwargs):
            raise OSError("flock failed")

        monkeypatch.setattr(callback_recovery.fcntl, "flock", flock_fails)
        monkeypatch.setattr(callback_recovery.os, "close", closed.append)

        with pytest.raises(OSError, match="flock failed"):
            callback_recovery._flock_acquire(tmp_path / "claim.lock")

        assert closed == [73]

    @pytest.mark.asyncio
    async def test_cancel_during_off_loop_acquire_releases_eventual_descriptor(self, monkeypatch):
        import asyncio
        import threading

        from cli_agent_orchestrator.services import callback_recovery

        acquire_started = threading.Event()
        allow_acquire_to_return = threading.Event()
        released = threading.Event()

        def delayed_acquire(_path):
            acquire_started.set()
            assert allow_acquire_to_return.wait(timeout=5)
            return 77

        def record_release(descriptor):
            assert descriptor == 77
            released.set()

        monkeypatch.setattr(callback_recovery, "_flock_acquire", delayed_acquire)
        monkeypatch.setattr(callback_recovery, "_flock_release", record_release)

        async def hold_claim():
            async with callback_recovery.async_session_lifecycle_claim(
                "TmuxBackend", "cao-cancel-during-acquire"
            ):
                pytest.fail("a cancelled acquisition must never enter the claim")

        task = asyncio.create_task(hold_claim())
        assert await asyncio.to_thread(acquire_started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The worker is still blocked acquiring; let it land and self-release.
        allow_acquire_to_return.set()
        for _ in range(100):
            if released.is_set():
                break
            await asyncio.sleep(0.01)
        assert released.is_set(), "eventual flock descriptor was orphaned after cancellation"

    def test_handoff_releases_exactly_once_in_both_race_orderings(self, monkeypatch):
        """The lock-serialized handoff guarantees exactly one owner whether the
        caller abandons before or after the worker lands the descriptor."""
        from cli_agent_orchestrator.services import callback_recovery

        releases: list = []
        monkeypatch.setattr(callback_recovery, "_flock_acquire", lambda _path: 42)
        monkeypatch.setattr(callback_recovery, "_flock_release", lambda fd: releases.append(fd))

        # Ordering A: caller already abandoned -> worker hands nothing off and
        # releases the descriptor it just acquired itself.
        handoff_a = callback_recovery._AcquireHandoff()
        handoff_a.abandoned = True
        callback_recovery._acquire_or_abandon("/tmp/whatever", handoff_a)
        assert handoff_a.descriptor is None
        assert releases == [42]

        # Ordering B: worker hands off first -> caller owns and releases; the
        # worker does not.
        releases.clear()
        handoff_b = callback_recovery._AcquireHandoff()
        callback_recovery._acquire_or_abandon("/tmp/whatever", handoff_b)
        assert handoff_b.descriptor == 42
        assert releases == []
        with handoff_b.lock:
            handoff_b.abandoned = True
            owned = handoff_b.descriptor
        callback_recovery._safe_flock_release(owned)
        assert releases == [42]

    @pytest.mark.asyncio
    async def test_cancel_in_the_body_releases_the_descriptor_once(self, monkeypatch):
        import asyncio
        import threading

        from cli_agent_orchestrator.services import callback_recovery

        releases: list = []
        in_body = threading.Event()
        monkeypatch.setattr(callback_recovery, "_flock_acquire", lambda _path: 9)
        monkeypatch.setattr(callback_recovery, "_flock_release", lambda fd: releases.append(fd))

        async def hold_claim():
            async with callback_recovery.async_session_lifecycle_claim(
                "TmuxBackend", "cao-cancel-in-body"
            ):
                in_body.set()
                await asyncio.sleep(5)

        task = asyncio.create_task(hold_claim())
        assert await asyncio.to_thread(in_body.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if releases:
                break
            await asyncio.sleep(0.01)
        assert releases == [9], "descriptor not released exactly once after body cancellation"

    @pytest.mark.asyncio
    async def test_a_cancelled_waiter_never_enters_and_the_next_task_progresses(self, monkeypatch):
        import asyncio
        import threading

        from cli_agent_orchestrator.services import callback_recovery

        acquire_started = threading.Event()
        allow_acquire_to_return = threading.Event()

        def delayed_acquire(_path):
            acquire_started.set()
            assert allow_acquire_to_return.wait(timeout=5)
            return 1

        monkeypatch.setattr(callback_recovery, "_flock_acquire", delayed_acquire)
        monkeypatch.setattr(callback_recovery, "_flock_release", lambda fd: None)

        async def hold():
            async with callback_recovery.async_session_lifecycle_claim(
                "TmuxBackend", "cao-cancel-progress"
            ):
                await asyncio.sleep(5)

        waiter = asyncio.create_task(hold())
        assert await asyncio.to_thread(acquire_started.wait, 5)
        waiter.cancel()  # cancelled while still acquiring -> never enters the body
        with pytest.raises(asyncio.CancelledError):
            await waiter
        allow_acquire_to_return.set()
        for _ in range(20):
            await asyncio.sleep(0)
        # A fresh task for the same session must be able to enter after release.
        cm = callback_recovery.async_session_lifecycle_claim("TmuxBackend", "cao-cancel-progress")
        fresh = asyncio.create_task(cm.__aenter__())
        # It should not be blocked forever; give the worker time to self-release.
        try:
            await asyncio.wait_for(fresh, timeout=5)
        except asyncio.TimeoutError:
            pytest.fail("the cancelled waiter's abandonment blocked the next task")
        # Clean up: release what fresh acquired.
        await cm.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_an_exception_in_the_body_releases_descriptor_and_gate(self, monkeypatch):
        """A non-cancellation exception must still release the descriptor and the
        gate so the next task is not blocked and the lock is not orphaned."""
        import asyncio

        from cli_agent_orchestrator.services import callback_recovery

        releases: list = []
        monkeypatch.setattr(callback_recovery, "_flock_acquire", lambda _path: 5)
        monkeypatch.setattr(callback_recovery, "_flock_release", lambda fd: releases.append(fd))

        cm = callback_recovery.async_session_lifecycle_claim("TmuxBackend", "cao-exc-body")
        await cm.__aenter__()
        with pytest.raises(ValueError, match="boom"):
            # Simulate the claim body raising.
            try:
                raise ValueError("boom")
            finally:
                await cm.__aexit__(ValueError, ValueError("boom"), None)

        for _ in range(100):
            if releases:
                break
            await asyncio.sleep(0.01)
        assert releases == [5]
        # The gate is free: a fresh acquire for the same session enters promptly.
        again_cm = callback_recovery.async_session_lifecycle_claim("TmuxBackend", "cao-exc-body")
        again = asyncio.create_task(again_cm.__aenter__())
        try:
            await asyncio.wait_for(again, timeout=5)
        except asyncio.TimeoutError:
            pytest.fail("the body exception leaked the gate and blocked the next task")
        await again_cm.__aexit__(None, None, None)
