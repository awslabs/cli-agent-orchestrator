"""Who a wait message is for, and whether it was ever admitted (M7 Stage 2).

This is the durable contract underneath a future wait/interrupt surface, and
nothing else. It is deliberately **dark**: the capability that would deliver,
suppress, or expire a wait message is disabled, no consumer is attached, the
M3-C stop-interruptor callback is untouched, and there is no route or CLI. What
exists here is the part that has to be right *before* anything is allowed to
act — because once a consumer exists, an admission record is what it obeys.

Three guarantees, and the failure each one prevents:

**Exact owner, exact generation.** A wait message names one stable agent's one
current incarnation: agent, incarnation id, terminal, generation, lineage,
native session id, and (optionally) the restore contract it was relaunched
from. Every part is compared exactly against the roster, including the parts
that are legitimately ``None``. A worker that was stopped and came back on a
new pane is a *different* incarnation, and a message composed against the old
one is denied rather than quietly re-aimed at whatever now holds the terminal
id. There is no system owner and no generation-less wait: a message with
nobody exact to belong to is not admitted at all.

**Ambiguity refuses.** If two live incarnations share a terminal id (legacy or
corrupt state), admission denies instead of picking the row that happens to
sort first. Picking would be a silent misdelivery into someone else's
conversation, which is the exact outcome this contract exists to make
impossible.

**Response loss replays, divergence refuses.** The admission id is derived from
the caller's durable operation id, so the process that committed a verdict and
died before reading it re-derives the same id and re-reads the same verdict —
including a denial, and including after the roster has moved on. A retry whose
canonical bytes differ from the stored request is not a replay; it is a second,
different message wearing the first one's operation id, and it is refused.

The receipt is a digest over the operation, the owner identity, the message,
and the verdict. It is recorded on every row, admitted or denied, so a later
owner can prove which decision it is acting on rather than re-deriving one.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, TypeVar

from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.canonical_json import (
    build_canonical,
    canonical_sha256,
    encode_canonical,
)

_T = TypeVar("_T")

SCHEMA_VERSION = "cao-m7-wait-admission-v1"

#: The wire schema of the message itself. Versioned separately from the row
#: schema because the envelope is what a future consumer parses; the two move
#: for different reasons and a shared version would hide that.
MESSAGE_SCHEMA_VERSION = "cao-m7-wait-message-v1"

CAPABILITY_NAME = "m7-wait-message-admission"
CAPABILITY_SCHEMA_VERSION = 1

KIND_EXPIRY = "expiry"
KIND_WORKER_WAKE = "worker-wake"
KIND_REPORT = "report"
KIND_DECISION = "decision"
MESSAGE_KINDS = frozenset({KIND_EXPIRY, KIND_WORKER_WAKE, KIND_REPORT, KIND_DECISION})

STATE_ADMITTED = "admitted"
STATE_DENIED = "denied"

DENY_OWNER_UNKNOWN = "owner-unknown"
DENY_OWNER_RETIRED = "owner-retired"
DENY_OWNER_AMBIGUOUS = "owner-ambiguous"
DENY_OWNER_REPLACED = "owner-replaced"
DENY_GENERATION_STALE = "owner-generation-stale"
DENY_IDENTITY_MISMATCH = "owner-identity-mismatch"
DENIAL_REASONS = frozenset(
    {
        DENY_OWNER_UNKNOWN,
        DENY_OWNER_RETIRED,
        DENY_OWNER_AMBIGUOUS,
        DENY_OWNER_REPLACED,
        DENY_GENERATION_STALE,
        DENY_IDENTITY_MISMATCH,
    }
)

#: Words that name a non-agent. A wait belongs to a real incarnation or to
#: nobody; there is no house account these could stand for.
RESERVED_OWNER_IDS = frozenset(
    {"*", "-", "all", "any", "cao", "cao-system", "none", "null", "root", "system"}
)

#: Fixed field order for the owner block. Every field is always present, as an
#: explicit null when unset: an absent key and a null key must not digest the
#: same, or "identity not established" and "identity not mentioned" would be
#: indistinguishable in a receipt.
OWNER_FIELDS = (
    "agent_id",
    "incarnation_id",
    "terminal_id",
    "generation",
    "lineage_id",
    "native_session_id",
    "restore_contract_id",
    "restore_contract_digest",
)

#: Fixed, closed field order for the message body. Closed on purpose: an
#: envelope that accepts unknown keys is one whose meaning drifts per caller.
BODY_FIELDS = ("reason_code", "payload_digest", "source_operation_id", "text")

MAX_TEXT_LEN = 512
MAX_ID_LEN = 128
MAX_DETAIL_LEN = 2000
MAX_SESSION_LEN = 128

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%/-]{0,127}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Derives the admission id from the caller's operation id, so a retry cannot
#: mint a second admission for one operation even before it reaches the store.
_ADMISSION_NAMESPACE = uuid.UUID("07000000-2000-4700-b7e2-000000000002")


class WaitAdmissionError(RuntimeError):
    code = "wait-admission-error"


class WaitAdmissionInvalid(WaitAdmissionError):
    code = "wait-admission-invalid"


class WaitAdmissionConflict(WaitAdmissionError):
    code = "wait-admission-conflict"


class WaitAdmissionUnavailable(WaitAdmissionError):
    code = "wait-admission-unavailable"


# ---------------------------------------------------------------------------
# the capability, and the fact that it is off
# ---------------------------------------------------------------------------

_DISABLED_REASON = (
    "M7 Stage 2 ships the durable wait-message admission contract only; no "
    "consumer, interruptor, suppression, expiry, route, or CLI is attached in "
    "this build"
)


def capability() -> dict[str, Any]:
    """What this build can actually do with a wait message: nothing.

    Published as a block rather than inferred from absence, so an operator or
    a later stage reads one truthful record instead of concluding "no endpoint
    exists, therefore it must be off". The four authority fields are the ones
    worth stating explicitly, because each is a thing M7 will eventually hold
    and does not hold yet.

    No verb taxonomy and no future-stage gates: enumerating what a later stage
    might enable would be vocabulary describing a surface that does not exist,
    and it cannot be kept honest by any test in this build.
    """
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "capability": CAPABILITY_NAME,
        "contract_schema_version": SCHEMA_VERSION,
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "enabled": False,
        "reason": _DISABLED_REASON,
        "message_kinds": sorted(MESSAGE_KINDS),
        "denial_reasons": sorted(DENIAL_REASONS),
        "consumer_attached": False,
        "stop_interruptor_attached": False,
        "public_surface": False,
        "recovery_authority": False,
        "action_authority": False,
        "completion_authority": False,
    }


# ---------------------------------------------------------------------------
# small validators (kept local so this contract owns its own vocabulary)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WaitAdmissionInvalid(f"{field} must be a non-empty string; got {value!r}")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise WaitAdmissionInvalid(
            f"{field} must be a canonical lowercase UUID; got {value!r}"
        ) from exc
    return value


def _require_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WaitAdmissionInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > MAX_ID_LEN:
        raise WaitAdmissionInvalid(f"{field} must be at most {MAX_ID_LEN} characters")
    if _ID_RE.fullmatch(value) is None:
        raise WaitAdmissionInvalid(f"{field} is not a well-formed identifier; got {value!r}")
    if value.strip().lower() in RESERVED_OWNER_IDS:
        raise WaitAdmissionInvalid(
            f"{field} {value!r} names no real incarnation; a wait message belongs to an "
            "exact owner or to nobody — there is no system owner"
        )
    return value


def _optional_identifier(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_identifier(value, field=field)


def _require_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise WaitAdmissionInvalid(f"{field} must be 64 lowercase hex characters; got {value!r}")
    return value


def _optional_digest(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_digest(value, field=field)


def _normalise_session_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise WaitAdmissionInvalid(f"session_name must be a non-empty string; got {value!r}")
    name = sl.normalise_session_name(value)
    if len(name) > MAX_SESSION_LEN:
        raise WaitAdmissionInvalid(
            f"session_name normalises to more than {MAX_SESSION_LEN} characters"
        )
    return name


def _anchor_caller_transaction(db: Any) -> None:
    """Give SQLite a real outer transaction before a savepoint.

    SQLAlchemy's Session transaction is lazy on SQLite, so a ``begin_nested``
    issued first becomes the outer transaction and RELEASE commits it — which
    would make the caller's later rollback unable to undo this write.
    """
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


# ---------------------------------------------------------------------------
# the exact owner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaitOwner:
    """The one incarnation a wait message belongs to.

    ``agent_id`` is a roster agent id (a UUID), not a word: "system" is not a
    spelling this accepts. ``generation`` is required — an incarnation without
    one cannot be told apart from its successor on the same terminal id, which
    is the whole thing this record is for.

    ``lineage_id`` / ``native_session_id`` may be ``None``, and that ``None``
    is a *claim* ("this agent has no established native identity"), compared
    exactly against the roster. It never means "whatever the roster says".
    """

    agent_id: str
    incarnation_id: str
    terminal_id: str
    generation: str
    lineage_id: Optional[str] = None
    native_session_id: Optional[str] = None
    restore_contract_id: Optional[str] = None
    restore_contract_digest: Optional[str] = None

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        # A roster agent id is a UUID, which no reserved word can spell: this
        # is where "there is no system owner" is enforced for the agent.
        setattr_(self, "agent_id", _require_uuid(self.agent_id, field="agent_id"))
        setattr_(
            self,
            "incarnation_id",
            _require_identifier(self.incarnation_id, field="incarnation_id"),
        )
        setattr_(self, "terminal_id", _require_identifier(self.terminal_id, field="terminal_id"))
        if self.generation is None:
            raise WaitAdmissionInvalid(
                "generation is required: a wait message is addressed to an exact "
                "generation, never to a terminal id that a successor may inherit"
            )
        setattr_(self, "generation", _require_identifier(self.generation, field="generation"))
        setattr_(self, "lineage_id", _optional_identifier(self.lineage_id, field="lineage_id"))
        setattr_(
            self,
            "native_session_id",
            _optional_identifier(self.native_session_id, field="native_session_id"),
        )
        setattr_(
            self,
            "restore_contract_id",
            _optional_identifier(self.restore_contract_id, field="restore_contract_id"),
        )
        setattr_(
            self,
            "restore_contract_digest",
            _optional_digest(self.restore_contract_digest, field="restore_contract_digest"),
        )
        if self.restore_contract_digest is not None and self.restore_contract_id is None:
            raise WaitAdmissionInvalid(
                "restore_contract_digest without restore_contract_id names no contract; "
                "supply both or neither"
            )

    def canonical(self) -> dict[str, Any]:
        """The owner block in fixed field order, nulls included."""
        return build_canonical((field, getattr(self, field)) for field in OWNER_FIELDS)

    def identity_digest(self) -> str:
        return canonical_sha256(self.canonical())


# ---------------------------------------------------------------------------
# the fixed, versioned message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaitMessage:
    """One wait message: an expiry, a worker wake, a report, or a decision.

    The body is a closed key set. ``text`` is what a consumer would eventually
    render; it is bounded here rather than at delivery time, because a message
    that is discovered to be too long only when someone tries to send it is a
    message nobody was ever told about.
    """

    message_id: str
    kind: str
    reason_code: str
    payload_digest: Optional[str] = None
    source_operation_id: Optional[str] = None
    text: Optional[str] = None

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "message_id", _require_uuid(self.message_id, field="message_id"))
        if self.kind not in MESSAGE_KINDS:
            raise WaitAdmissionInvalid(
                f"kind must be one of {sorted(MESSAGE_KINDS)}; got {self.kind!r}"
            )
        if not isinstance(self.reason_code, str) or _REASON_RE.fullmatch(self.reason_code) is None:
            raise WaitAdmissionInvalid(
                f"reason_code must be a short lowercase token; got {self.reason_code!r}"
            )
        setattr_(
            self, "payload_digest", _optional_digest(self.payload_digest, field="payload_digest")
        )
        if self.source_operation_id is not None:
            setattr_(
                self,
                "source_operation_id",
                _require_uuid(self.source_operation_id, field="source_operation_id"),
            )
        if self.text is not None:
            if not isinstance(self.text, str):
                raise WaitAdmissionInvalid(f"text must be a string when present; got {self.text!r}")
            if len(self.text.encode("utf-8")) > MAX_TEXT_LEN:
                raise WaitAdmissionInvalid(f"text must encode to at most {MAX_TEXT_LEN} bytes")

    def body(self) -> dict[str, Any]:
        return build_canonical((field, getattr(self, field)) for field in BODY_FIELDS)


def render_envelope(*, session_name: str, owner: WaitOwner, message: WaitMessage) -> dict[str, Any]:
    """The complete versioned envelope, in fixed field order."""
    if not isinstance(owner, WaitOwner):
        raise WaitAdmissionInvalid(f"owner must be a WaitOwner; got {type(owner).__name__}")
    if not isinstance(message, WaitMessage):
        raise WaitAdmissionInvalid(f"message must be a WaitMessage; got {type(message).__name__}")
    return build_canonical(
        (
            ("message_version", MESSAGE_SCHEMA_VERSION),
            ("message_id", message.message_id),
            ("kind", message.kind),
            ("session_name", _normalise_session_name(session_name)),
            ("owner", owner.canonical()),
            ("body", message.body()),
        )
    )


def envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    """Canonical bytes for one envelope: one message, one byte string."""
    return encode_canonical(envelope)


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    return canonical_sha256(envelope)


# ---------------------------------------------------------------------------
# the admission request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionRequest:
    """Admit one message for one exact owner, under one durable operation.

    ``operation_id`` is caller-minted and is the replay key: a lost response is
    retried as the *same* operation and re-reads its verdict, rather than
    asking a second time and possibly getting a second answer.
    """

    operation_id: str
    session_name: str
    owner: WaitOwner
    message: WaitMessage

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        object.__setattr__(self, "session_name", _normalise_session_name(self.session_name))
        if not isinstance(self.owner, WaitOwner):
            raise WaitAdmissionInvalid(
                f"owner must be a WaitOwner; got {type(self.owner).__name__}"
            )
        if not isinstance(self.message, WaitMessage):
            raise WaitAdmissionInvalid(
                f"message must be a WaitMessage; got {type(self.message).__name__}"
            )

    def envelope(self) -> dict[str, Any]:
        return render_envelope(
            session_name=self.session_name, owner=self.owner, message=self.message
        )

    def canonical(self) -> dict[str, Any]:
        return build_canonical(
            (
                ("schema_version", SCHEMA_VERSION),
                ("operation_id", self.operation_id),
                ("session_name", self.session_name),
                ("envelope", self.envelope()),
            )
        )

    def request_digest(self) -> str:
        return canonical_sha256(self.canonical())


def admission_id_for(operation_id: str) -> str:
    """The admission id one operation will always derive."""
    return str(uuid.uuid5(_ADMISSION_NAMESPACE, _require_uuid(operation_id, field="operation_id")))


def receipt_digest_for(record: Mapping[str, Any]) -> str:
    """The receipt binding operation, owner, message, and verdict.

    Recomputable from the stored row, so a holder can check that the receipt it
    was handed describes the decision it is about to act on.
    """
    return canonical_sha256(
        build_canonical(
            (
                ("schema_version", SCHEMA_VERSION),
                ("effect", "wait-message-admission"),
                ("admission_id", record["admission_id"]),
                ("operation_id", record["operation_id"]),
                ("message_id", record["message_id"]),
                ("owner_identity_digest", record["owner_identity_digest"]),
                ("message_digest", record["message_digest"]),
                ("admission_state", record["admission_state"]),
                ("denial_reason", record.get("denial_reason")),
            )
        )
    )


# ---------------------------------------------------------------------------
# ownership verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Verdict:
    denial_reason: Optional[str]
    detail: Optional[str]


_ADMITTED = _Verdict(None, None)


def _verify_owner(db: Any, owner: WaitOwner) -> _Verdict:
    """Compare the claimed owner against the roster, exactly.

    Ordered from "no owner at all" outward, so the reason an operator reads is
    the first thing that was actually wrong rather than a downstream symptom.
    """
    try:
        agent = roster.get_agent(owner.agent_id, db=db)
    except roster.StableAgentNotFound:
        return _Verdict(DENY_OWNER_UNKNOWN, f"no stable agent {owner.agent_id}")
    except roster.StableAgentError as exc:
        return _Verdict(DENY_OWNER_UNKNOWN, f"stable agent unreadable: {exc}")

    incarnation = agent.get("current_incarnation")
    if not incarnation:
        return _Verdict(
            DENY_OWNER_UNKNOWN,
            f"stable agent {owner.agent_id} holds no current incarnation to wait",
        )
    if incarnation.get("disposition") not in roster.LIVE_INCARNATION_DISPOSITIONS:
        return _Verdict(
            DENY_OWNER_RETIRED,
            f"incarnation {incarnation.get('incarnation_id')} is "
            f"{incarnation.get('disposition')}, not live",
        )

    # Ambiguity before identity: if the terminal id resolves to more than one
    # live incarnation, no comparison below can be trusted to mean what it
    # looks like. The public terminal-only read already refuses that case
    # rather than picking a historical row, so this borrows its judgement
    # instead of re-deriving one.
    try:
        roster.get_incarnation_by_terminal(owner.terminal_id, db=db)
    except roster.StableAgentConflict as exc:
        return _Verdict(DENY_OWNER_AMBIGUOUS, str(exc))

    if incarnation.get("incarnation_id") != owner.incarnation_id:
        return _Verdict(
            DENY_OWNER_REPLACED,
            f"stable agent {owner.agent_id} is now incarnation "
            f"{incarnation.get('incarnation_id')}, not {owner.incarnation_id}",
        )
    if (
        incarnation.get("terminal_id") != owner.terminal_id
        or incarnation.get("generation") != owner.generation
    ):
        return _Verdict(
            DENY_GENERATION_STALE,
            f"incarnation {owner.incarnation_id} is "
            f"{incarnation.get('terminal_id')}/{incarnation.get('generation')}, not "
            f"{owner.terminal_id}/{owner.generation}",
        )

    lineage = agent.get("current_lineage") or {}
    lineage_id = lineage.get("lineage_id") if lineage else None
    # Compared with ``!=`` including None on either side: a claimed None must
    # not match an established id, and a claimed id must not match a truthfully
    # missing one.
    if lineage_id != owner.lineage_id:
        return _Verdict(
            DENY_IDENTITY_MISMATCH,
            f"stable agent {owner.agent_id} is on lineage {lineage_id!r}, "
            f"not {owner.lineage_id!r}",
        )
    native_session_id = lineage.get("native_session_id") if lineage else None
    if native_session_id != owner.native_session_id:
        return _Verdict(
            DENY_IDENTITY_MISMATCH,
            f"lineage {lineage_id!r} holds native session id {native_session_id!r}, "
            f"not {owner.native_session_id!r}",
        )
    if incarnation.get("lineage_id") != owner.lineage_id:
        return _Verdict(
            DENY_IDENTITY_MISMATCH,
            f"incarnation {owner.incarnation_id} is bound to lineage "
            f"{incarnation.get('lineage_id')!r}, not {owner.lineage_id!r}",
        )

    return _verify_restore_identity(db, owner)


def _verify_restore_identity(db: Any, owner: WaitOwner) -> _Verdict:
    """A claimed restore contract must describe this exact incarnation.

    Claiming none is fine — most waits have no restore provenance. Claiming one
    that belongs to a different incarnation is an owner assembled from the
    wrong recovery record, and admitting it would attach a message to a
    conversation the contract does not describe.
    """
    if owner.restore_contract_id is None:
        return _ADMITTED
    row = (
        db.query(database.RestoreContractModel)
        .filter(database.RestoreContractModel.contract_id == owner.restore_contract_id)
        .one_or_none()
    )
    if row is None:
        return _Verdict(DENY_IDENTITY_MISMATCH, f"no restore contract {owner.restore_contract_id}")
    if (
        row.agent_id != owner.agent_id
        or row.terminal_id != owner.terminal_id
        or row.generation != owner.generation
        or row.lineage_id != owner.lineage_id
        or row.native_session_id != owner.native_session_id
    ):
        return _Verdict(
            DENY_IDENTITY_MISMATCH,
            f"restore contract {owner.restore_contract_id} describes "
            f"{row.agent_id}/{row.terminal_id}/{row.generation}, not this incarnation",
        )
    if (
        owner.restore_contract_digest is not None
        and row.contract_digest != owner.restore_contract_digest
    ):
        return _Verdict(
            DENY_IDENTITY_MISMATCH,
            f"restore contract {owner.restore_contract_id} digests to "
            f"{row.contract_digest}, not {owner.restore_contract_digest}",
        )
    return _ADMITTED


# ---------------------------------------------------------------------------
# the durable write
# ---------------------------------------------------------------------------


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "admission_id": row.admission_id,
        "schema_version": row.schema_version,
        "message_schema_version": row.message_schema_version,
        "operation_id": row.operation_id,
        "message_id": row.message_id,
        "session_name": row.session_name,
        "message_kind": row.message_kind,
        "owner": {
            "agent_id": row.owner_agent_id,
            "incarnation_id": row.owner_incarnation_id,
            "terminal_id": row.owner_terminal_id,
            "generation": row.owner_generation,
            "lineage_id": row.owner_lineage_id,
            "native_session_id": row.owner_native_session_id,
            "restore_contract_id": row.owner_restore_contract_id,
            "restore_contract_digest": row.owner_restore_contract_digest,
        },
        "owner_identity_digest": row.owner_identity_digest,
        "request_digest": row.request_digest,
        "message_digest": row.message_digest,
        "message_json": row.message_json,
        "admission_state": row.admission_state,
        "denial_reason": row.denial_reason,
        "detail": row.detail,
        "receipt_digest": row.receipt_digest,
        "created_at": row.created_at,
    }


def _by_operation(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.WaitMessageAdmissionModel)
        .filter(database.WaitMessageAdmissionModel.operation_id == operation_id)
        .one_or_none()
    )


def _by_message(db: Any, message_id: str) -> Any:
    return (
        db.query(database.WaitMessageAdmissionModel)
        .filter(database.WaitMessageAdmissionModel.message_id == message_id)
        .one_or_none()
    )


def _with_session(fn: Callable[[Any], _T], db: Any, *, unavailable: str) -> _T:
    if db is not None:
        try:
            _anchor_caller_transaction(db)
            with db.begin_nested():
                return fn(db)
        except WaitAdmissionError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise WaitAdmissionUnavailable(f"{unavailable}: {exc}") from exc
    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = fn(session)
                session.commit()
                return result
        except WaitAdmissionError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise WaitAdmissionUnavailable(f"{unavailable}: {last_error}")


def _admit_once(db: Any, request: AdmissionRequest) -> dict[str, Any]:
    admission_id = admission_id_for(request.operation_id)
    request_digest = request.request_digest()

    existing = _by_operation(db, request.operation_id)
    if existing is not None:
        # Response loss: the same operation, byte-identical, re-reads its own
        # verdict. Deliberately no re-evaluation — the roster may legitimately
        # have moved since, and one operation must not hold two answers.
        if existing.request_digest != request_digest:
            raise WaitAdmissionConflict(
                f"operation {request.operation_id} already admitted a different message "
                f"(stored request digest {existing.request_digest}); a divergent replay is "
                "refused — mint a new operation id for a new message"
            )
        record = _row_dict(existing)
        record["adopted"] = True
        return record

    clash = _by_message(db, request.message.message_id)
    if clash is not None:
        raise WaitAdmissionConflict(
            f"message {request.message.message_id} was already admitted under operation "
            f"{clash.operation_id}; one message is admitted once"
        )

    verdict = _verify_owner(db, request.owner)
    envelope = request.envelope()
    record = {
        "admission_id": admission_id,
        "operation_id": request.operation_id,
        "message_id": request.message.message_id,
        "owner_identity_digest": request.owner.identity_digest(),
        "message_digest": envelope_digest(envelope),
        "admission_state": STATE_ADMITTED if verdict.denial_reason is None else STATE_DENIED,
        "denial_reason": verdict.denial_reason,
    }
    row = database.WaitMessageAdmissionModel(
        admission_id=admission_id,
        schema_version=SCHEMA_VERSION,
        message_schema_version=MESSAGE_SCHEMA_VERSION,
        operation_id=request.operation_id,
        message_id=request.message.message_id,
        session_name=request.session_name,
        message_kind=request.message.kind,
        owner_agent_id=request.owner.agent_id,
        owner_incarnation_id=request.owner.incarnation_id,
        owner_terminal_id=request.owner.terminal_id,
        owner_generation=request.owner.generation,
        owner_lineage_id=request.owner.lineage_id,
        owner_native_session_id=request.owner.native_session_id,
        owner_restore_contract_id=request.owner.restore_contract_id,
        owner_restore_contract_digest=request.owner.restore_contract_digest,
        owner_identity_digest=record["owner_identity_digest"],
        request_digest=request_digest,
        message_digest=record["message_digest"],
        message_json=envelope_bytes(envelope).decode("utf-8"),
        admission_state=record["admission_state"],
        denial_reason=verdict.denial_reason,
        detail=(verdict.detail or None) and verdict.detail[:MAX_DETAIL_LEN],
        receipt_digest=receipt_digest_for(record),
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    result = _row_dict(row)
    result["adopted"] = False
    return result


def admit(request: AdmissionRequest, db: Any = None) -> dict[str, Any]:
    """Record the one admission verdict for one operation's wait message.

    Returns the durable record with ``adopted`` set when this call replayed an
    existing one. Recording a verdict is the whole effect: nothing is
    delivered, suppressed, expired, or interrupted, because the capability
    that would dispatch a wait message is off.

    Malformed input and divergent replays raise; an owner that does not match
    the roster is a durable *denial*, not an exception, so the refusal is
    replayable and carries a receipt of its own.

    ``db`` — pass an open Session to make the admission part of the caller's
    own transaction. The write happens in a savepoint, so the caller's
    rollback removes it: an owner that records a decision and this admission
    together never keeps one without the other.
    """
    if not isinstance(request, AdmissionRequest):
        raise WaitAdmissionInvalid(
            f"request must be an AdmissionRequest; got {type(request).__name__}"
        )
    return _with_session(
        lambda session: _admit_once(session, request),
        db,
        unavailable="wait-message admission failed",
    )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_admission(operation_id: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The durable verdict for one operation, or ``None``."""

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _by_operation(session, operation_id)
        return _row_dict(row) if row is not None else None

    return _read(_get, db, "wait-message admission read failed")


def get_admission_by_message(message_id: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The durable verdict a message was admitted under, or ``None``."""

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _by_message(session, message_id)
        return _row_dict(row) if row is not None else None

    return _read(_get, db, "wait-message admission read failed")


def list_admissions(session_name: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    """Every admission, oldest first; optionally scoped to one session."""
    name = _normalise_session_name(session_name) if session_name is not None else None

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.WaitMessageAdmissionModel)
        if name is not None:
            query = query.filter(database.WaitMessageAdmissionModel.session_name == name)
        rows = query.order_by(
            database.WaitMessageAdmissionModel.created_at,
            database.WaitMessageAdmissionModel.admission_id,
        ).all()
        return [_row_dict(row) for row in rows]

    return _read(_list, db, "wait-message admission list failed")


def _read(fn: Callable[[Any], _T], db: Any, unavailable: str) -> _T:
    try:
        if db is not None:
            return fn(db)
        with database.SessionLocal() as session:
            return fn(session)
    except SQLAlchemyError as exc:
        raise WaitAdmissionUnavailable(f"{unavailable}: {str(exc).splitlines()[0]}") from exc
