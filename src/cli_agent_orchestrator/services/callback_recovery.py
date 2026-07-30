"""Dedicated, one-shot recovery for a callback stranded by an ACP refusal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryCompletionRequest,
    CallbackRecoveryRequest,
    InboxMessage,
    MessageStatus,
)
from cli_agent_orchestrator.services import (
    companion_receipts,
    control_input_service,
    model_turn_receipt_contract,
)
from cli_agent_orchestrator.services.control_input_contract import REASON_MANAGED_ACP_PANE

STATE_INTENT = "intent"
STATE_PENDING = "pending"
STATE_SUBMITTED = "recovery-submitted"
STATE_COMPLETED = "callback-completed"
STATE_REFUSED = "refused"
STATE_AMBIGUOUS = "ambiguous"
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_REFUSED, STATE_AMBIGUOUS})


class CallbackRecoveryError(RuntimeError):
    reason_code = "callback-recovery-invalid"


class CallbackRecoveryConflict(CallbackRecoveryError):
    reason_code = "callback-recovery-conflict"


class CallbackRecoveryRefused(CallbackRecoveryError):
    def __init__(self, detail: str, *, reason_code: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


class CallbackRecoveryNotFound(CallbackRecoveryError):
    reason_code = "callback-recovery-not-found"


class CallbackRecoveryPending(CallbackRecoveryError):
    reason_code = "callback-recovery-pending"


@dataclass(frozen=True)
class RecoveryAdmission:
    operation: dict[str, Any]
    message: InboxMessage
    replayed: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _request_payload(body: CallbackRecoveryRequest) -> dict[str, Any]:
    return body.model_dump(mode="json")


def _workflow_identity(body: CallbackRecoveryRequest) -> dict[str, Any]:
    return {
        "project": body.project,
        "task_id": body.task_id,
        "run_id": body.run_id,
        "source_terminal_id": body.source_terminal_id,
        "source_generation": body.source_generation,
        "expected_provider": body.expected_provider,
        "expected_provider_session_id": body.expected_provider_session_id,
        "expected_execution_mode": body.expected_execution_mode,
        "supervisor_id": body.supervisor_id,
        "supervisor_session": body.supervisor_session,
        "callback_occurrence_id": body.callback_occurrence_id,
    }


def _recovery_identity(body: CallbackRecoveryRequest) -> dict[str, Any]:
    return {
        **_workflow_identity(body),
        "refusal_control_id": body.refusal_control_id,
        "refusal_occurrence_sha256": body.refusal_occurrence_sha256,
        "refusal_request_sha256": body.refusal_request_sha256,
    }


def _operation_key(body: CallbackRecoveryRequest) -> str:
    return _digest(
        {
            "schema": "cao-callback-recovery-operation-key-v1",
            "workflow": _workflow_identity(body),
            "operation_id": body.operation_id,
        }
    )


def _json_object(raw: Optional[str], *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object) if raw is not None else None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CallbackRecoveryConflict(f"managed {field} is unreadable") from exc
    if not isinstance(value, dict):
        raise CallbackRecoveryConflict(f"managed {field} is absent")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reservation_identity(db: Any, terminal_id: str) -> tuple[Any, dict[str, str]]:
    v1 = (
        db.query(database.ManagedLaunchReservationModel)
        .filter(database.ManagedLaunchReservationModel.terminal_id == terminal_id)
        .one_or_none()
    )
    v2 = (
        db.query(database.ManagedLaunchV2ReservationModel)
        .filter(database.ManagedLaunchV2ReservationModel.terminal_id == terminal_id)
        .one_or_none()
    )
    if (v1 is None) == (v2 is None):
        raise CallbackRecoveryRefused(
            "source terminal does not resolve to exactly one managed reservation",
            reason_code="source-reservation-ambiguous",
        )
    row = v1 if v1 is not None else v2
    if v1 is not None:
        request = _json_object(v1.request_json, field="v1 request")
        mode = str(request.get("execution_mode") or "acp")
        readiness = _json_object(v1.readiness_json, field="v1 readiness")
        provider_session_id = readiness.get("provider_session_id")
    else:
        mode = str(v2.execution_mode or "acp")
        binding = _json_object(v2.binding_json, field="v2 binding")
        provider_session_id = binding.get("native_session_id")
    if row.state != "admitted":
        raise CallbackRecoveryRefused(
            f"source managed generation is {row.state!r}, not admitted",
            reason_code="source-not-admitted",
        )
    return row, {
        "terminal_id": str(row.terminal_id),
        "generation": str(row.generation),
        "provider": str(row.provider),
        "provider_session_id": str(provider_session_id or ""),
        "execution_mode": mode,
        "caller_id": str(row.caller_id or ""),
        "session_name": str(row.session_name or ""),
    }


def _terminal_row(db: Any, terminal_id: str) -> Any:
    v1 = (
        db.query(database.TerminalModel)
        .filter(database.TerminalModel.id == terminal_id)
        .one_or_none()
    )
    try:
        v2 = (
            db.query(database.ManagedLaunchV2TerminalModel)
            .filter(database.ManagedLaunchV2TerminalModel.id == terminal_id)
            .one_or_none()
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable vintage is not absence
        if "no such table" not in str(exc).lower():
            raise
        v2 = None
    if (v1 is None) == (v2 is None):
        raise CallbackRecoveryRefused(
            f"terminal {terminal_id!r} does not resolve to exactly one authoritative row",
            reason_code="terminal-identity-ambiguous",
        )
    return v1 if v1 is not None else v2


def _callback_line(body: CallbackRecoveryRequest) -> str:
    summary = re.sub(r"\s+", " ", body.callback_summary).strip()
    return (
        f"[conduct-report] status={body.callback_status} task={body.task_id} "
        f"report={body.report_path} summary={summary}"
    )[:900]


def _recovery_prompt(body: CallbackRecoveryRequest) -> str:
    command = " ".join(
        (
            "conduct report",
            "--task",
            shlex.quote(body.task_id),
            "--project",
            shlex.quote(body.project),
            "--status",
            body.callback_status,
            "--report",
            shlex.quote(body.report_path),
            "--summary",
            shlex.quote(re.sub(r"\s+", " ", body.callback_summary).strip()),
        )
    )
    return (
        "Your existing report artifact is complete, but its original callback "
        f"occurrence {body.callback_occurrence_id!r} is stranded. Run exactly "
        "this one route-unattested callback command now; do not edit the report, "
        "do not add --model, --effort, or --route-evidence, and do not run any "
        f"other command:\n\n{command}"
    )


def _refusal_occurrence_from_record(record: Any) -> tuple[str, dict[str, Any]]:
    matching = [
        event
        for event in record.events
        if event.get("to_state") == "refused"
        and event.get("reason_code") == REASON_MANAGED_ACP_PANE
    ]
    if not matching:
        raise CallbackRecoveryRefused(
            "the named control has no managed-ACP zero-byte refusal occurrence",
            reason_code="refusal-occurrence-absent",
        )
    event = matching[-1]
    occurrence = {
        "control_id": record.request_id,
        "terminal_id": record.terminal_id,
        "generation": record.generation,
        "request_sha256": record.request_sha256,
        "reason_code": REASON_MANAGED_ACP_PANE,
        "refused_at": event.get("at"),
        "occurrence_index": len(matching),
    }
    return _digest(occurrence), occurrence


def refusal_occurrence(control_id: str) -> dict[str, Any]:
    record = control_input_service.get_control_input_journal().find(control_id)
    if record is None or record.state != "refused" or record.reason_code != REASON_MANAGED_ACP_PANE:
        raise CallbackRecoveryNotFound(f"control {control_id!r} has no current managed-ACP refusal")
    digest, occurrence = _refusal_occurrence_from_record(record)
    return {"refusal_occurrence_sha256": digest, **occurrence}


def _operation_dict(row: Any) -> dict[str, Any]:
    return {
        "operation_key": row.operation_key,
        "operation_id": row.operation_id,
        "state": row.state,
        "reason_code": row.reason_code,
        "project": row.project,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "source_terminal_id": row.source_terminal_id,
        "source_generation": row.source_generation,
        "expected_provider": row.expected_provider,
        "expected_provider_session_id": row.expected_provider_session_id,
        "expected_execution_mode": row.expected_execution_mode,
        "supervisor_id": row.supervisor_id,
        "supervisor_session": row.supervisor_session,
        "refusal_control_id": row.refusal_control_id,
        "refusal_occurrence_sha256": row.refusal_occurrence_sha256,
        "callback_occurrence_id": row.callback_occurrence_id,
        "callback_message_sha256": row.callback_message_sha256,
        "report_path": row.report_path,
        "report_sha256": row.report_sha256,
        "source_head": row.source_head,
        "publishing_lease_state": row.publishing_lease_state,
        "publishing_lease_sha256": row.publishing_lease_sha256,
        "manifest_path": row.manifest_path,
        "manifest_sha256": row.manifest_sha256,
        "finalization_identity_sha256": row.finalization_identity_sha256,
        "inbox_message_id": row.inbox_message_id,
        "callback_message_id": row.callback_message_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _store_refusal(db: Any, row: Any, exc: CallbackRecoveryRefused) -> None:
    row.state = STATE_REFUSED
    row.reason_code = exc.reason_code
    row.updated_at = _now()
    db.commit()


def admit(body: CallbackRecoveryRequest) -> RecoveryAdmission:
    """Journal intent, authorize the refusal, and enqueue under one DB lock."""
    callback_line = _callback_line(body)
    payload = _request_payload(body)
    request_sha256 = _digest(payload)
    operation_key = _operation_key(body)
    recovery_identity_sha256 = _digest(_recovery_identity(body))
    workflow_sha256 = _digest(_workflow_identity(body))

    refusal: Optional[CallbackRecoveryRefused] = None
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        existing = (
            db.query(database.CallbackRecoveryModel)
            .filter(database.CallbackRecoveryModel.operation_key == operation_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise CallbackRecoveryConflict(
                    "operation id is already bound to different workflow or recovery bytes"
                )
            if existing.state == STATE_REFUSED:
                raise CallbackRecoveryRefused(
                    "this recovery operation is durably refused",
                    reason_code=str(existing.reason_code or "callback-recovery-refused"),
                )
            if existing.state == STATE_AMBIGUOUS:
                raise CallbackRecoveryRefused(
                    "this recovery operation is terminally ambiguous; manual resolution required",
                    reason_code="callback-recovery-ambiguous",
                )
            message_row = (
                db.query(database.InboxModel)
                .filter(database.InboxModel.id == existing.inbox_message_id)
                .one_or_none()
            )
            if message_row is None:
                raise CallbackRecoveryConflict("accepted recovery lost its immutable inbox row")
            db.commit()
            return RecoveryAdmission(
                _operation_dict(existing), database._inbox_message_from_row(message_row), True
            )

        consumed = (
            db.query(database.CallbackRecoveryModel)
            .filter(
                database.CallbackRecoveryModel.recovery_identity_sha256 == recovery_identity_sha256
            )
            .one_or_none()
        )
        if consumed is not None:
            raise CallbackRecoveryConflict(
                "this refusal/callback occurrence already has a one-shot recovery operation"
            )
        moment = _now()
        row = database.CallbackRecoveryModel(
            operation_key=operation_key,
            operation_id=body.operation_id,
            workflow_identity_sha256=workflow_sha256,
            recovery_identity_sha256=recovery_identity_sha256,
            state=STATE_INTENT,
            project=body.project,
            task_id=body.task_id,
            run_id=body.run_id,
            source_terminal_id=body.source_terminal_id,
            source_generation=body.source_generation,
            expected_provider=body.expected_provider,
            expected_provider_session_id=body.expected_provider_session_id,
            expected_execution_mode=body.expected_execution_mode,
            supervisor_id=body.supervisor_id,
            supervisor_session=body.supervisor_session,
            refusal_control_id=body.refusal_control_id,
            refusal_occurrence_sha256=body.refusal_occurrence_sha256,
            refusal_request_sha256=body.refusal_request_sha256,
            callback_occurrence_id=body.callback_occurrence_id,
            callback_message_sha256=body.callback_message_sha256,
            report_path=body.report_path,
            report_sha256=body.report_sha256,
            source_head=body.source_head,
            publishing_lease_state=body.publishing_lease_state,
            publishing_lease_sha256=body.publishing_lease_sha256,
            manifest_path=body.manifest_path,
            manifest_sha256=body.manifest_sha256,
            finalization_identity_sha256=body.finalization_identity_sha256,
            request_sha256=request_sha256,
            created_at=moment,
            updated_at=moment,
        )
        db.add(row)
        db.flush()
        try:
            if not os.path.isabs(body.report_path) or not os.path.isabs(body.manifest_path):
                raise CallbackRecoveryRefused(
                    "report and manifest paths must be absolute",
                    reason_code="artifact-path-invalid",
                )
            if (
                hashlib.sha256(callback_line.encode("utf-8")).hexdigest()
                != body.callback_message_sha256
            ):
                raise CallbackRecoveryRefused(
                    "callback digest does not match the canonical callback",
                    reason_code="callback-digest-mismatch",
                )
            _reservation, identity = _reservation_identity(db, body.source_terminal_id)
            expected_identity = {
                "generation": body.source_generation,
                "provider": body.expected_provider,
                "provider_session_id": body.expected_provider_session_id,
                "execution_mode": body.expected_execution_mode,
                "caller_id": body.supervisor_id,
                "session_name": body.supervisor_session,
            }
            observed_identity = {key: identity[key] for key in expected_identity}
            if observed_identity != expected_identity:
                raise CallbackRecoveryRefused(
                    "source reservation identity contradicts the requested recovery",
                    reason_code="source-identity-mismatch",
                )
            supervisor = _terminal_row(db, body.supervisor_id)
            if str(supervisor.tmux_session) != body.supervisor_session or getattr(
                supervisor, "caller_id", None
            ):
                raise CallbackRecoveryRefused(
                    "the authoritative supervisor is not the root caller in the reservation session",
                    reason_code="supervisor-authority-mismatch",
                )
            source = _terminal_row(db, body.source_terminal_id)
            if (
                str(source.tmux_session) != body.supervisor_session
                or str(source.caller_id or "") != body.supervisor_id
                or str(source.generation or "") != body.source_generation
            ):
                raise CallbackRecoveryRefused(
                    "source terminal caller/session/generation does not match the reservation",
                    reason_code="source-caller-mismatch",
                )
            control = control_input_service.get_control_input_journal().find(
                body.refusal_control_id
            )
            if (
                control is None
                or control.state != "refused"
                or control.reason_code != REASON_MANAGED_ACP_PANE
                or control.terminal_id != body.source_terminal_id
                or str(control.generation or "") != body.source_generation
                or control.request_sha256 != body.refusal_request_sha256
            ):
                raise CallbackRecoveryRefused(
                    "the named control is not the exact current managed-ACP refusal",
                    reason_code="refusal-identity-mismatch",
                )
            occurrence_sha256, _occurrence = _refusal_occurrence_from_record(control)
            if occurrence_sha256 != body.refusal_occurrence_sha256:
                raise CallbackRecoveryRefused(
                    "the named refusal occurrence digest does not match durable evidence",
                    reason_code="refusal-occurrence-mismatch",
                )
            prompt = _recovery_prompt(body)
            inbox = database.InboxModel(
                sender_id=body.supervisor_id,
                receiver_id=body.source_terminal_id,
                message=prompt,
                status=MessageStatus.PENDING.value,
                message_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                sender_generation=str(
                    getattr(supervisor, "generation", None)
                    or getattr(supervisor, "pane_id", None)
                    or "server-authorized-supervisor"
                ),
                expected_receiver_generation=body.source_generation,
                expected_provider_session_id=body.expected_provider_session_id,
                expected_execution_mode=body.expected_execution_mode,
                expected_provider=body.expected_provider,
                callback_recovery_key=operation_key,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(inbox)
            db.flush()
            row.inbox_message_id = inbox.id
            row.state = STATE_PENDING
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            db.refresh(inbox)
            return RecoveryAdmission(
                _operation_dict(row), database._inbox_message_from_row(inbox), False
            )
        except CallbackRecoveryRefused as exc:
            refusal = exc
            _store_refusal(db, row, exc)
    assert refusal is not None
    raise refusal


def get(operation_key: str) -> dict[str, Any]:
    with database.SessionLocal() as db:
        rows = (
            db.query(database.CallbackRecoveryModel)
            .filter(database.CallbackRecoveryModel.operation_key == operation_key)
            .all()
        )
        if len(rows) != 1:
            if not rows:
                raise CallbackRecoveryNotFound(f"no callback recovery {operation_key!r}")
            raise CallbackRecoveryConflict("duplicate callback recovery operation rows")
        result = _operation_dict(rows[0])
        if rows[0].inbox_message_id is not None:
            inbox = db.get(database.InboxModel, rows[0].inbox_message_id)
            if inbox is None:
                raise CallbackRecoveryConflict("accepted recovery lost its immutable inbox row")
            result.update(
                {
                    "message_created_at": inbox.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "recovery_prompt_sha256": inbox.message_sha256,
                    "sender_generation": inbox.sender_generation,
                }
            )
        return result


def mark_delivery_refused(operation_key: str, *, reason_code: str) -> None:
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        if row.state in TERMINAL_STATES:
            db.commit()
            return
        row.state = STATE_REFUSED
        row.reason_code = reason_code
        row.updated_at = _now()
        if row.inbox_message_id is not None:
            (
                db.query(database.InboxModel)
                .filter(
                    database.InboxModel.id == row.inbox_message_id,
                    database.InboxModel.status == MessageStatus.PENDING.value,
                )
                .update({database.InboxModel.status: MessageStatus.FAILED.value})
            )
        db.commit()


def mark_delivery_ambiguous(operation_key: str, *, reason_code: str) -> None:
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        if row.state in TERMINAL_STATES:
            db.commit()
            return
        row.state = STATE_AMBIGUOUS
        row.reason_code = reason_code
        row.updated_at = _now()
        db.commit()


def turn_receipt(operation_key: str) -> Optional[dict[str, str]]:
    """Return only an exact strict receipt revalidated against immutable rows."""
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        inbox = db.get(database.InboxModel, row.inbox_message_id)
        if inbox is None:
            raise CallbackRecoveryConflict("recovery inbox row is absent")
        receipt = companion_receipts.get_strict_message_ack(
            row.source_terminal_id, row.source_generation, inbox.id
        )
        if receipt is None:
            db.commit()
            return None
        expected = {
            "message_id": str(inbox.id),
            "message_sha256": inbox.message_sha256,
            "message_created_at": inbox.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "sender_id": row.supervisor_id,
            "sender_generation": inbox.sender_generation,
            "receiver_id": row.source_terminal_id,
            "receiver_generation": row.source_generation,
            "provider": row.expected_provider,
            "provider_session_id": row.expected_provider_session_id,
        }
        strict = model_turn_receipt_contract.validate_receipt(receipt, expected=expected)
        if row.provider_turn_receipt_json is not None:
            try:
                stored = json.loads(
                    row.provider_turn_receipt_json,
                    object_pairs_hook=_unique_object,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise CallbackRecoveryConflict("stored provider receipt is malformed") from exc
            model_turn_receipt_contract.validate_receipt(stored, expected=expected)
            if stored != strict:
                raise CallbackRecoveryConflict("stored provider receipt changed")
        else:
            row.provider_turn_receipt_json = json.dumps(strict, sort_keys=True)
            row.state = STATE_SUBMITTED
            row.updated_at = _now()
        db.commit()
        return strict


def complete(operation_key: str, body: CallbackRecoveryCompletionRequest) -> dict[str, Any]:
    """Bind the original callback row and close the recovery exactly once."""
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        if row.state == STATE_COMPLETED:
            stored = _json_object(row.completion_json, field="completion")
            incoming = body.model_dump(mode="json")
            if {key: stored.get(key) for key in incoming} != incoming:
                raise CallbackRecoveryConflict("completion replay contradicts stored callback")
            db.commit()
            return _operation_dict(row)
        if row.state != STATE_SUBMITTED:
            raise CallbackRecoveryPending(
                f"recovery prompt is {row.state!r}; callback completion is not admissible"
            )
        if body.finalization_identity_sha256 != row.finalization_identity_sha256:
            raise CallbackRecoveryConflict("finalization identity changed")
        callback = db.get(database.InboxModel, body.callback_message_id)
        if callback is None:
            raise CallbackRecoveryConflict("callback inbox row is absent")
        observed_digest = hashlib.sha256(str(callback.message).encode("utf-8")).hexdigest()
        try:
            if not body.callback_created_at.endswith("Z"):
                raise ValueError("callback timestamp is not canonical UTC Z")
            supplied_created = datetime.fromisoformat(
                body.callback_created_at.replace("Z", "+00:00")
            )
            stored_created = callback.created_at
            if stored_created.tzinfo is None:
                stored_created = stored_created.replace(tzinfo=timezone.utc)
            created_matches = supplied_created.astimezone(
                timezone.utc
            ) == stored_created.astimezone(timezone.utc)
        except (TypeError, ValueError):
            created_matches = False
        expected_callback = {
            "sender_id": row.source_terminal_id,
            "receiver_id": row.supervisor_id,
            "message_sha256": row.callback_message_sha256,
        }
        observed_callback = {
            "sender_id": callback.sender_id,
            "receiver_id": callback.receiver_id,
            "message_sha256": observed_digest,
        }
        if (
            observed_callback != expected_callback
            or body.callback_message_sha256 != observed_digest
            or not created_matches
        ):
            raise CallbackRecoveryConflict("callback row contradicts recovery identity")
        completion = {
            **body.model_dump(mode="json"),
            "schema": "cao-callback-recovery-completion-v1",
            "source_terminal_id": row.source_terminal_id,
            "source_generation": row.source_generation,
            "supervisor_id": row.supervisor_id,
            "callback_occurrence_id": row.callback_occurrence_id,
            "completed_at": _now(),
        }
        row.callback_message_id = callback.id
        row.completion_json = json.dumps(completion, sort_keys=True)
        row.state = STATE_COMPLETED
        row.reason_code = None
        row.updated_at = completion["completed_at"]
        db.commit()
        db.refresh(row)
        return _operation_dict(row)


def terminal_has_open_recovery(terminal_id: str, generation: Optional[str] = None) -> bool:
    with database.SessionLocal() as db:
        query = db.query(database.CallbackRecoveryModel).filter(
            database.CallbackRecoveryModel.source_terminal_id == terminal_id,
            database.CallbackRecoveryModel.state.in_(
                (STATE_INTENT, STATE_PENDING, STATE_SUBMITTED, STATE_AMBIGUOUS)
            ),
        )
        if generation is not None:
            query = query.filter(database.CallbackRecoveryModel.source_generation == generation)
        return query.first() is not None


def current_delivery_binding_matches(message: InboxMessage) -> bool:
    if message.callback_recovery_key is None:
        return False
    try:
        with database.SessionLocal() as db:
            _row, identity = _reservation_identity(db, message.receiver_id)
        return (
            identity["generation"] == message.expected_receiver_generation
            and identity["provider_session_id"] == message.expected_provider_session_id
            and identity["execution_mode"] == message.expected_execution_mode
            and identity["provider"] == message.expected_provider
        )
    except CallbackRecoveryError:
        return False


def binding_matches(
    terminal_id: str,
    *,
    generation: str,
    provider: str,
    provider_session_id: str,
    execution_mode: str,
) -> bool:
    try:
        with database.SessionLocal() as db:
            _row, identity = _reservation_identity(db, terminal_id)
        return (
            identity["generation"] == generation
            and identity["provider"] == provider
            and identity["provider_session_id"] == provider_session_id
            and identity["execution_mode"] == execution_mode
        )
    except CallbackRecoveryError:
        return False
