"""Atomic, idempotent inbox enqueue for one exact managed ACP generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    BoundInboxMessageRequest,
    InboxMessage,
    MessageStatus,
)


class BoundInboxError(RuntimeError):
    """Base error for the narrow managed-message protocol."""


class BoundInboxConflict(BoundInboxError):
    """The operation id or immutable lifecycle binding conflicts."""

    reason_code = "managed-identity-mismatch"


class BoundInboxNotFound(BoundInboxError):
    """No operation exists under the requested receiver/id pair."""


@dataclass(frozen=True)
class BoundInboxResult:
    message: InboxMessage
    replayed: bool


def _json_object(raw: Optional[str], *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise BoundInboxConflict(f"managed {field} is unreadable") from exc
    if not isinstance(value, dict):
        raise BoundInboxConflict(f"managed {field} is absent")
    return value


def _receiver_identity(db: Any, terminal_id: str) -> dict[str, str]:
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
        raise BoundInboxConflict("receiver does not resolve to exactly one managed reservation")
    if v1 is not None:
        request = _json_object(v1.request_json, field="v1 request")
        mode = request.get("execution_mode") or "acp"
        readiness = _json_object(v1.readiness_json, field="v1 readiness")
        session_id = readiness.get("provider_session_id")
        row = v1
    else:
        mode = v2.execution_mode or "acp"
        binding = _json_object(v2.binding_json, field="v2 binding")
        session_id = binding.get("native_session_id")
        row = v2
    if row.state != "admitted":
        raise BoundInboxConflict(f"receiver managed generation is {row.state!r}, not admitted")
    if mode != "acp":
        raise BoundInboxConflict(f"receiver execution mode is {mode!r}, not 'acp'")
    if not isinstance(session_id, str) or not session_id:
        raise BoundInboxConflict("receiver provider session identity is absent")
    return {
        "terminal_id": str(row.terminal_id),
        "generation": str(row.generation),
        "execution_mode": mode,
        "provider_session_id": session_id,
        "provider": str(row.provider),
    }


def _sender_generation(db: Any, terminal_id: str) -> str:
    generations = {
        str(value)
        for value in (
            db.query(database.TerminalModel.generation)
            .filter(database.TerminalModel.id == terminal_id)
            .scalar(),
            db.query(database.ManagedLaunchReservationModel.generation)
            .filter(database.ManagedLaunchReservationModel.terminal_id == terminal_id)
            .scalar(),
            db.query(database.ManagedLaunchV2ReservationModel.generation)
            .filter(database.ManagedLaunchV2ReservationModel.terminal_id == terminal_id)
            .scalar(),
        )
        if value
    }
    if len(generations) != 1:
        raise BoundInboxConflict("sender does not resolve to exactly one immutable generation")
    return next(iter(generations))


def _message(row: Any) -> InboxMessage:
    return database._inbox_message_from_row(row)


def _assert_exact_replay(row: Any, request: BoundInboxMessageRequest) -> None:
    expected = {
        "sender_id": request.sender_id,
        "receiver_id": row.receiver_id,
        "message": request.message,
        "message_sha256": request.message_sha256,
        "sender_generation": request.sender_generation,
        "expected_receiver_generation": request.expected_receiver_generation,
        "expected_provider_session_id": request.expected_provider_session_id,
        "expected_execution_mode": request.expected_execution_mode,
    }
    observed = {field: getattr(row, field) for field in expected}
    if observed != expected:
        raise BoundInboxConflict("operation id is already bound to a different message or identity")


def enqueue(receiver_id: str, request: BoundInboxMessageRequest) -> BoundInboxResult:
    """Compare live identity and enqueue in one SQLite write transaction."""
    if hashlib.sha256(request.message.encode("utf-8")).hexdigest() != request.message_sha256:
        raise BoundInboxConflict("message digest does not match the exact message bytes")
    with database.SessionLocal() as db:
        # Acquire the SQLite writer lock before either the identity comparison
        # or insert. Managed lifecycle changes use the same database, so no
        # replacement can commit between the comparison and the durable row.
        db.execute(text("BEGIN IMMEDIATE"))
        existing = (
            db.query(database.InboxModel)
            .filter(database.InboxModel.operation_id == request.operation_id)
            .one_or_none()
        )
        if existing is not None:
            if existing.receiver_id != receiver_id:
                raise BoundInboxConflict("operation id is already bound to a different receiver")
            _assert_exact_replay(existing, request)
            db.commit()
            return BoundInboxResult(_message(existing), replayed=True)

        identity = _receiver_identity(db, receiver_id)
        expected_identity = {
            "generation": request.expected_receiver_generation,
            "execution_mode": request.expected_execution_mode,
            "provider_session_id": request.expected_provider_session_id,
        }
        observed_identity = {field: identity[field] for field in expected_identity}
        if observed_identity != expected_identity:
            raise BoundInboxConflict(
                "current receiver identity does not match the requested binding"
            )
        if _sender_generation(db, request.sender_id) != request.sender_generation:
            raise BoundInboxConflict(
                "current sender generation does not match the requested binding"
            )
        row = database.InboxModel(
            sender_id=request.sender_id,
            receiver_id=receiver_id,
            message=request.message,
            status=MessageStatus.PENDING.value,
            operation_id=request.operation_id,
            message_sha256=request.message_sha256,
            sender_generation=request.sender_generation,
            expected_receiver_generation=request.expected_receiver_generation,
            expected_provider_session_id=request.expected_provider_session_id,
            expected_execution_mode=request.expected_execution_mode,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return BoundInboxResult(_message(row), replayed=False)


def get(receiver_id: str, operation_id: str) -> InboxMessage:
    """Read a server-journaled operation without consulting live lifecycle."""
    with database.SessionLocal() as db:
        row = (
            db.query(database.InboxModel)
            .filter(
                database.InboxModel.receiver_id == receiver_id,
                database.InboxModel.operation_id == operation_id,
            )
            .one_or_none()
        )
        if row is None:
            raise BoundInboxNotFound(
                f"no bound inbox operation {operation_id!r} for {receiver_id!r}"
            )
        return _message(row)


def current_delivery_binding_matches(message: InboxMessage) -> bool:
    """Revalidate a persisted row before provider I/O."""
    if not message.is_identity_bound:
        return True
    try:
        with database.SessionLocal() as db:
            identity = _receiver_identity(db, message.receiver_id)
        return (
            identity["generation"] == message.expected_receiver_generation
            and identity["provider_session_id"] == message.expected_provider_session_id
            and identity["execution_mode"] == message.expected_execution_mode
        )
    except BoundInboxError:
        return False


def binding_matches(
    terminal_id: str,
    *,
    generation: str,
    provider_session_id: str,
    execution_mode: str,
) -> bool:
    """Re-read the exact managed binding immediately before provider I/O."""
    try:
        with database.SessionLocal() as db:
            identity = _receiver_identity(db, terminal_id)
        return (
            identity["generation"] == generation
            and identity["provider_session_id"] == provider_session_id
            and identity["execution_mode"] == execution_mode
        )
    except BoundInboxError:
        return False
