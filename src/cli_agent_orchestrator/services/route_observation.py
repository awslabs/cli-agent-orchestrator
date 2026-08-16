"""Dark COND-0230 M10 route observe/close/result transaction core.

This is the fork-owned operation journal underneath a future provider-effect
route lane, and deliberately nothing else. The capability is **dark**:
``enabled()`` is always ``False``, no provider surface is observed, no pane
input or ``Escape`` is issued, no consumer, route, or CLI is attached, and no
wake is ever delivered or reconciled. What exists here is the record that has
to be right *before* anything is allowed to act, because once an effect lane is
attached it is this journal it obeys.

One operation id and one deterministic canonical request digest are bound to
the exact target tuple (target terminal id + generation, native session id,
provider, provider version, provider artifact SHA-256) and the exact requester
(requester terminal id + generation).

Two guarantees, and the failure each one prevents:

**Exact replay, divergent refusal.** A retry under the same operation id
re-reads the stored record, never a second row and never a re-decision. A
retry whose canonical bytes differ from the stored request is refused before
any mutation, so one operation id cannot be reused to smuggle a different
target or requester into an already-journaled operation.

**One nonterminal owner per exact target tuple.** At most one operation holds a
target as ``requested``; the partial unique index in the schema is the durable
form of that. A different operation id that loses the tuple is itself stored as
an immutable ``zero-effect-refusal`` (terminal, no provider effect, no receipt,
its own requester still woken) with the winning operation id kept only as
non-authoritative ``detail``. Response loss is recovered by re-reading the same
operation id; an ambiguous stored result is never re-driven into a second
effect.

The terminal transaction is atomic: the immutable closed-vocabulary result, the
canonical final event and its digest, the optional positive
``cao-route-observation-receipt-v1``, and one deterministic wake claim in the
existing inbox table (``expected_receiver_generation`` pinned to the captured
requester generation) commit together or not at all. The only extra row at
terminal commit is that inbox claim; there is no event, receipt, outbox, or
wake table.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, TypeVar

from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services.canonical_json import (
    build_canonical,
    canonical_sha256,
    encode_canonical,
)

_T = TypeVar("_T")

SCHEMA_VERSION = "cao-m10-route-observation-op-v1"
RECEIPT_SCHEMA = "cao-route-observation-receipt-v1"
WAKE_SCHEMA_VERSION = "cao-m10-route-observation-wake-v1"

#: The one nonterminal state; the terminal vocabulary below is closed.
STATE_REQUESTED = "requested"

RESULT_OBSERVED_CLOSED = "observed-closed"
RESULT_ZERO_EFFECT_REFUSAL = "zero-effect-refusal"
RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT = "ambiguous-after-possible-effect"

TERMINAL_RESULTS = frozenset(
    {
        RESULT_OBSERVED_CLOSED,
        RESULT_ZERO_EFFECT_REFUSAL,
        RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
    }
)

#: Exact fixed field order for the canonical request and wake bytes, so two
#: independent encoders always derive identical SHA-256 digests.
REQUEST_FIELDS = (
    "schema_version",
    "operation_id",
    "target_terminal_id",
    "target_generation",
    "native_session_id",
    "provider",
    "provider_version",
    "provider_artifact_sha256",
    "requester_terminal_id",
    "requester_generation",
)
WAKE_FIELDS = (
    "wake_version",
    "operation_id",
    "request_digest",
    "result_kind",
    "requester_terminal_id",
    "requester_generation",
    "target_terminal_id",
    "target_generation",
    "native_session_id",
    "provider",
    "provider_version",
    "provider_artifact_sha256",
    "final_event_digest",
)

MAX_ID_LEN = 128
MAX_VERSION_LEN = 96

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%/-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class RouteObservationError(RuntimeError):
    code = "route-observation-error"


class RouteObservationInvalid(RouteObservationError):
    code = "route-observation-invalid"


class RouteObservationConflict(RouteObservationError):
    code = "route-observation-conflict"


class RouteObservationUnavailable(RouteObservationError):
    code = "route-observation-unavailable"


#: Internal signal that the write raced a terminal transition; the whole
#: attempt — including the already-flushed inbox claim — must roll back and
#: the operation be re-read as whichever terminal resolution won.
class _TerminalReplay(RouteObservationError):
    code = "_terminal-replay"


def enabled() -> bool:
    """Whether this build may exercise the M10 route-observation capability.

    Always ``False`` in this slice: the journal exists to be trustworthy before
    any provider surface is observed, closed, or woken.
    """
    return False


# ---------------------------------------------------------------------------
# small validators (this contract owns its own vocabulary)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RouteObservationInvalid(f"{field} must be a non-empty string; got {value!r}")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise RouteObservationInvalid(
            f"{field} must be a canonical lowercase UUID; got {value!r}"
        ) from exc
    return value


def _require_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RouteObservationInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > MAX_ID_LEN:
        raise RouteObservationInvalid(f"{field} must be at most {MAX_ID_LEN} characters")
    if _ID_RE.fullmatch(value) is None:
        raise RouteObservationInvalid(f"{field} is not a well-formed identifier; got {value!r}")
    return value


def _require_version(value: Any, *, field: str) -> str:
    """A bounded, nonempty provider version banner."""
    if not isinstance(value, str) or not value:
        raise RouteObservationInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > MAX_VERSION_LEN:
        raise RouteObservationInvalid(f"{field} must be at most {MAX_VERSION_LEN} characters")
    return value


def _require_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RouteObservationInvalid(f"{field} must be 64 lowercase hex characters; got {value!r}")
    return value


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RouteObservationInvalid(f"{field} must be a mapping; got {type(value).__name__}")
    if not value:
        raise RouteObservationInvalid(f"{field} must not be empty")
    return value


def _anchor_caller_transaction(db: Any) -> None:
    """Give SQLite a real outer transaction before a savepoint.

    SQLAlchemy's Session transaction is lazy on SQLite, so a ``begin_nested``
    issued first on the caller's fresh session would become the outer
    transaction and RELEASE would commit it — which would make the caller's
    later rollback unable to undo this write.
    """
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteObservationRequest:
    """The exact binding one operation is valid for, in closed field order.

    ``operation_id`` is caller-minted and is the replay key: a lost response is
    retried as the *same* operation and re-reads the same record. Every target
    and requester component is exact; a changed byte is a changed request under
    the same operation id and is refused.
    """

    operation_id: str
    target_terminal_id: str
    target_generation: str
    native_session_id: str
    provider: str
    provider_version: str
    provider_artifact_sha256: str
    requester_terminal_id: str
    requester_generation: str

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "operation_id", _require_uuid(self.operation_id, field="operation_id"))
        setattr_(
            self,
            "target_terminal_id",
            _require_identifier(self.target_terminal_id, field="target_terminal_id"),
        )
        setattr_(
            self,
            "target_generation",
            _require_identifier(self.target_generation, field="target_generation"),
        )
        setattr_(
            self,
            "native_session_id",
            _require_identifier(self.native_session_id, field="native_session_id"),
        )
        setattr_(self, "provider", _require_identifier(self.provider, field="provider"))
        setattr_(
            self,
            "provider_version",
            _require_version(self.provider_version, field="provider_version"),
        )
        setattr_(
            self,
            "provider_artifact_sha256",
            _require_sha256(self.provider_artifact_sha256, field="provider_artifact_sha256"),
        )
        setattr_(
            self,
            "requester_terminal_id",
            _require_identifier(self.requester_terminal_id, field="requester_terminal_id"),
        )
        setattr_(
            self,
            "requester_generation",
            _require_identifier(self.requester_generation, field="requester_generation"),
        )

    def canonical(self) -> dict[str, Any]:
        fields = [("schema_version", SCHEMA_VERSION)]
        for key in REQUEST_FIELDS[1:]:
            fields.append((key, getattr(self, key)))
        return build_canonical(fields)

    def request_digest(self) -> str:
        return canonical_sha256(self.canonical())


def _wake_message(
    request: RouteObservationRequest, *, result: str, event_digest: str
) -> dict[str, Any]:
    """The deterministic wake payload for one terminal result.

    Canonical, bounded, and fully derived from the operation binding so a later
    delivery lane can re-derive it without any other persisted state.
    """
    values = {
        "wake_version": WAKE_SCHEMA_VERSION,
        "operation_id": request.operation_id,
        "request_digest": request.request_digest(),
        "result_kind": result,
        "requester_terminal_id": request.requester_terminal_id,
        "requester_generation": request.requester_generation,
        "target_terminal_id": request.target_terminal_id,
        "target_generation": request.target_generation,
        "native_session_id": request.native_session_id,
        "provider": request.provider,
        "provider_version": request.provider_version,
        "provider_artifact_sha256": request.provider_artifact_sha256,
        "final_event_digest": event_digest,
    }
    return build_canonical((field, values[field]) for field in WAKE_FIELDS)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "schema_version": row.schema_version,
        "operation_id": row.operation_id,
        "request_digest": row.request_digest,
        "target": {
            "terminal_id": row.target_terminal_id,
            "generation": row.target_generation,
        },
        "native_session_id": row.native_session_id,
        "provider": row.provider,
        "provider_version": row.provider_version,
        "provider_artifact_sha256": row.provider_artifact_sha256,
        "requester": {
            "terminal_id": row.requester_terminal_id,
            "generation": row.requester_generation,
        },
        "state": row.state,
        "terminal": _is_terminal(row.state),
        "detail": row.detail,
        "final_event_digest": row.final_event_digest,
        "final_event_json": row.final_event_json,
        "receipt_digest": row.receipt_digest,
        "receipt_json": row.receipt_json,
        "inbox_message_id": row.inbox_message_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _is_terminal(state: str) -> bool:
    return state in TERMINAL_RESULTS


def _by_operation(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.RouteObservationOperationModel)
        .filter(database.RouteObservationOperationModel.operation_id == operation_id)
        .one_or_none()
    )


def _active_owner(db: Any, request: RouteObservationRequest) -> Any:
    """The one nonterminal operation currently owning the exact target tuple."""
    model = database.RouteObservationOperationModel
    return (
        db.query(model)
        .filter(
            model.target_terminal_id == request.target_terminal_id,
            model.target_generation == request.target_generation,
            model.native_session_id == request.native_session_id,
            model.provider == request.provider,
            model.provider_version == request.provider_version,
            model.provider_artifact_sha256 == request.provider_artifact_sha256,
            model.state == STATE_REQUESTED,
        )
        .one_or_none()
    )


def _read(fn: Callable[[Any], _T], db: Any, unavailable: str) -> _T:
    try:
        if db is not None:
            return fn(db)
        with database.SessionLocal() as session:
            return fn(session)
    except RouteObservationError:
        raise
    except SQLAlchemyError as exc:
        raise RouteObservationUnavailable(f"{unavailable}: {str(exc).splitlines()[0]}") from exc


def get(operation_id: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The stored operation for one id, or ``None``."""

    def _one(session: Any) -> Optional[dict[str, Any]]:
        row = _by_operation(session, operation_id)
        return _row_dict(row) if row is not None else None

    return _read(_one, db, "route-observation read failed")


def list_operations(db: Any = None) -> list[dict[str, Any]]:
    """Every stored operation, oldest first."""

    def _list(session: Any) -> list[dict[str, Any]]:
        model = database.RouteObservationOperationModel
        rows = session.query(model).order_by(model.created_at, model.operation_id).all()
        return [_row_dict(row) for row in rows]

    return _read(_list, db, "route-observation list failed")


# ---------------------------------------------------------------------------
# the durable write
# ---------------------------------------------------------------------------

_MAX_WRITE_ATTEMPTS = 5
_RETRY_DELAY_S = 0.02


def _with_session(fn: Callable[[Any], _T], db: Any, *, unavailable: str) -> _T:
    """Run ``fn`` inside one all-or-none SQLite transaction.

    Pass an open Session via ``db`` to make the write part of the caller's own
    transaction (a savepoint plus the caller's commit/rollback bind the rows
    atomically). The caller-owned path uses exactly one savepoint: the same
    transaction cannot obtain a fresh snapshot of a concurrently won terminal
    transition, so a write race there is surfaced rather than re-run.
    Otherwise a fresh session is opened, written, and committed, and SQLite
    writer/lock races are bounded by a small retry loop.
    """
    if db is not None:
        _anchor_caller_transaction(db)
        try:
            with db.begin_nested():
                return fn(db)
        except _TerminalReplay as exc:
            raise RouteObservationConflict(
                f"{exc}; re-read the operation outside this transaction"
            ) from exc
        except RouteObservationError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise RouteObservationUnavailable(f"{unavailable}: {str(exc).splitlines()[0]}") from exc

    last_error = None
    for _attempt in range(_MAX_WRITE_ATTEMPTS):
        with database.SessionLocal() as session:
            try:
                with session.begin_nested():
                    result = fn(session)
                session.commit()
                return result
            except _TerminalReplay:
                continue
            except RouteObservationError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_error = exc
                time.sleep(_RETRY_DELAY_S)
    raise RouteObservationUnavailable(f"{unavailable}: {last_error}")


def _row(request: RouteObservationRequest, *, state: str, created_at: str) -> Any:
    return database.RouteObservationOperationModel(
        operation_id=request.operation_id,
        schema_version=SCHEMA_VERSION,
        request_digest=request.request_digest(),
        target_terminal_id=request.target_terminal_id,
        target_generation=request.target_generation,
        native_session_id=request.native_session_id,
        provider=request.provider,
        provider_version=request.provider_version,
        provider_artifact_sha256=request.provider_artifact_sha256,
        requester_terminal_id=request.requester_terminal_id,
        requester_generation=request.requester_generation,
        state=state,
        created_at=created_at,
        updated_at=created_at,
    )


def _add_wake_claim(
    db: Any, request: RouteObservationRequest, *, result: str, event_digest: str
) -> int:
    """Insert the deterministic exact-requester wake claim; return its inbox id.

    Only the claim is written here — nothing delivers, reconciles, or expires
    it, and the inbox fence simply carries the exact captured requester.
    """
    message = encode_canonical(_wake_message(request, result=result, event_digest=event_digest))
    inbox = database.InboxModel(
        sender_id=request.target_terminal_id,
        receiver_id=request.requester_terminal_id,
        message=message.decode("utf-8"),
        status=MessageStatus.PENDING.value,
        message_sha256=hashlib.sha256(message).hexdigest(),
        sender_generation=request.target_generation,
        expected_receiver_generation=request.requester_generation,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(inbox)
    db.flush()
    return int(inbox.id)


def claim(request: RouteObservationRequest, db: Any = None) -> dict[str, Any]:
    """Journal one operation id under the exact target/requester binding.

    An identical retry under the same operation id replays the stored record;
    a changed request under the same id conflicts before any mutation. A
    different operation id on a target tuple already owned terminates as an
    immutable ``zero-effect-refusal`` and is written in the same atomic
    transaction as its requester's deterministic wake claim.
    """
    if not isinstance(request, RouteObservationRequest):
        raise RouteObservationInvalid(
            f"request must be a RouteObservationRequest; got {type(request).__name__}"
        )
    return _with_session(
        lambda session: _claim(session, request),
        db,
        unavailable="route-observation claim failed",
    )


def _claim(db: Any, request: RouteObservationRequest) -> dict[str, Any]:
    existing = _by_operation(db, request.operation_id)
    if existing is not None:
        if existing.request_digest != request.request_digest():
            raise RouteObservationConflict(
                f"operation {request.operation_id} already holds a different request "
                f"(digest {existing.request_digest})"
            )
        record = _row_dict(existing)
        record["replayed"] = True
        return record

    owner = _active_owner(db, request)
    if owner is not None:
        return _losing_refusal(db, request, winning_operation_id=owner.operation_id)

    created_at = _now()
    row = _row(request, state=STATE_REQUESTED, created_at=created_at)
    db.add(row)
    db.flush()  # the partial active-owner index backstops a concurrent claim
    record = _row_dict(row)
    record["replayed"] = False
    return record


def _losing_refusal(
    db: Any, request: RouteObservationRequest, *, winning_operation_id: str
) -> dict[str, Any]:
    """A different operation id on an owned tuple becomes an immutable refusal.

    Inserted terminal (outside the active-owner index), it keeps the winning id
    only as non-authoritative detail and still claims its own requester's wake.
    """
    now = _now()
    refusal_event = build_canonical(
        (
            ("schema_version", SCHEMA_VERSION),
            ("result", RESULT_ZERO_EFFECT_REFUSAL),
            ("operation_id", request.operation_id),
            ("detail", winning_operation_id),
            ("committed_at", now),
        )
    )
    event_digest = canonical_sha256(refusal_event)
    inbox_id = _add_wake_claim(
        db, request, result=RESULT_ZERO_EFFECT_REFUSAL, event_digest=event_digest
    )
    row = _row(request, state=RESULT_ZERO_EFFECT_REFUSAL, created_at=now)
    row.detail = winning_operation_id
    row.final_event_json = encode_canonical(refusal_event).decode("utf-8")
    row.final_event_digest = event_digest
    row.inbox_message_id = inbox_id
    row.updated_at = now
    db.add(row)
    db.flush()
    record = _row_dict(row)
    record["replayed"] = False
    return record


def _require_binding_receipt(
    receipt: Any,
    request: RouteObservationRequest,
    *,
    event_digest: str,
) -> Mapping[str, Any]:
    """A positive receipt must re-state the exact operation/request binding.

    Every published binding field is compared byte-for-byte to the operation's
    own truth before any write; a missing or divergent field is refused.
    """
    bound = _require_mapping(receipt, field="receipt")
    if bound.get("schema") != RECEIPT_SCHEMA:
        raise RouteObservationInvalid(f"receipt schema must be {RECEIPT_SCHEMA!r}")
    if bound.get("kind") != RESULT_OBSERVED_CLOSED:
        raise RouteObservationInvalid(f"receipt kind must be {RESULT_OBSERVED_CLOSED!r}")
    truth = {
        "operation_id": request.operation_id,
        "request_digest": request.request_digest(),
        "requester_terminal_id": request.requester_terminal_id,
        "requester_generation": request.requester_generation,
        "target_terminal_id": request.target_terminal_id,
        "target_generation": request.target_generation,
        "native_session_id": request.native_session_id,
        "provider": request.provider,
        "provider_version": request.provider_version,
        "provider_artifact_sha256": request.provider_artifact_sha256,
        "final_event_digest": event_digest,
    }
    for field, expected in truth.items():
        if bound.get(field) != expected:
            raise RouteObservationInvalid(f"receipt {field} must be {expected!r}")
    observation = bound.get("observation")
    if not isinstance(observation, Mapping) or not observation:
        raise RouteObservationInvalid("receipt observation must be a nonempty mapping")
    close_proof = bound.get("close_proof")
    if not isinstance(close_proof, Mapping) or not close_proof:
        raise RouteObservationInvalid("receipt close_proof must be a nonempty mapping")
    committed_at = bound.get("committed_at")
    if not isinstance(committed_at, str) or not committed_at:
        raise RouteObservationInvalid("receipt committed_at must be a nonempty string")
    return bound


def complete(
    request: RouteObservationRequest,
    *,
    result: str,
    final_event: Mapping[str, Any],
    receipt: Optional[Mapping[str, Any]] = None,
    db: Any = None,
) -> dict[str, Any]:
    """Terminalize a claimed operation, atomically, in one transaction.

    Records the closed-vocabulary ``result``, the canonical ``final_event`` and
    its digest, the optional positive receipt (valid only for
    ``observed-closed``), and the requester's deterministic wake claim in the
    existing inbox store — all committed together or not at all.

    A retry after response loss replays the stored terminal record; a divergent
    terminal attempt (different event or receipt bytes) conflicts instead of
    overwriting.
    """
    if not isinstance(request, RouteObservationRequest):
        raise RouteObservationInvalid(
            f"request must be a RouteObservationRequest; got {type(request).__name__}"
        )
    if result not in TERMINAL_RESULTS:
        raise RouteObservationInvalid(
            f"result must be one of {sorted(TERMINAL_RESULTS)}; got {result!r}"
        )
    event = _require_mapping(final_event, field="final_event")
    event_json = encode_canonical(event).decode("utf-8")
    event_digest = canonical_sha256(event)

    receipt_json = None
    receipt_digest = None
    if result == RESULT_OBSERVED_CLOSED:
        if receipt is None:
            raise RouteObservationInvalid(
                f"result {RESULT_OBSERVED_CLOSED} requires the positive receipt"
            )
        receipt = _require_binding_receipt(receipt, request, event_digest=event_digest)
        receipt_json = encode_canonical(receipt).decode("utf-8")
        receipt_digest = canonical_sha256(receipt)
    elif receipt is not None:
        raise RouteObservationInvalid(f"result {result!r} rejects a positive receipt")

    return _with_session(
        lambda session: _complete(
            session,
            request,
            result=result,
            event_json=event_json,
            event_digest=event_digest,
            receipt_json=receipt_json,
            receipt_digest=receipt_digest,
        ),
        db,
        unavailable="route-observation terminal commit failed",
    )


def _complete(
    db: Any,
    request: RouteObservationRequest,
    *,
    result: str,
    event_json: str,
    event_digest: str,
    receipt_json: Optional[str],
    receipt_digest: Optional[str],
) -> dict[str, Any]:
    existing = _by_operation(db, request.operation_id)
    if existing is None:
        raise RouteObservationConflict(f"no claimed operation {request.operation_id}")
    if existing.request_digest != request.request_digest():
        raise RouteObservationConflict(
            f"operation {request.operation_id} holds a different request; a divergent "
            "replay is refused"
        )
    if _is_terminal(existing.state):
        if result != existing.state:
            raise RouteObservationConflict(
                f"operation {request.operation_id} is already {existing.state}, not {result}"
            )
        if existing.final_event_digest != event_digest:
            raise RouteObservationConflict(
                f"operation {request.operation_id} already terminalized with a different "
                "final event"
            )
        if existing.receipt_digest != receipt_digest:
            raise RouteObservationConflict(
                f"operation {request.operation_id} already terminalized with a different receipt"
            )
        record = _row_dict(existing)
        record["replayed"] = True
        return record

    # Nonterminal: the single terminal transition. The inbox claim is written
    # first and the operation row CAS-updated so a concurrent terminalizer is
    # detected rather than overwritten; both share one transaction.
    inbox_id = _add_wake_claim(db, request, result=result, event_digest=event_digest)
    updated = (
        db.query(database.RouteObservationOperationModel)
        .filter(
            database.RouteObservationOperationModel.operation_id == request.operation_id,
            database.RouteObservationOperationModel.state == STATE_REQUESTED,
        )
        .update(
            {
                "state": result,
                "final_event_json": event_json,
                "final_event_digest": event_digest,
                "receipt_json": receipt_json,
                "receipt_digest": receipt_digest,
                "inbox_message_id": inbox_id,
                "updated_at": _now(),
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        raise _TerminalReplay(f"operation {request.operation_id} was terminalized concurrently")
    db.refresh(existing)
    record = _row_dict(existing)
    record["replayed"] = False
    return record
