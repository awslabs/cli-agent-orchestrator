"""Durable, idempotent physical-reincarnation operation journal + the narrow
shared per-session effect seam (cond-0378 B2).

B2 is the retry/race integrity seam a later B3 executor and the later M3-C
Stop/Pause cohort consume.  It owns two things and nothing else:

- **The operation journal.**  ``claim_operation`` claims the winning
  operation for one exact source slot — stable agent, exact retired prior
  incarnation, lifecycle epoch, post-B1 roster revision — in one short
  database transaction, binding every immutable fact the M3-B design names:
  caller-minted operation id and canonical request digest, canonical session
  name, role/profile family, current lineage, harness and same-harness
  native session id, the exact B1 restore-contract id/digest/schema, and the
  requested route/provider/model/effort/execution-mode facts plus a bounded
  compatibility-cell reference/digest (recorded, never inferred as passing
  here).  An exact operation-id/request replay adopts; changed immutable
  input under one operation id conflicts; a concurrent different id for the
  same slot can never create a second winner — the loser observes a typed
  slot conflict naming the durable winner, which the query surface
  (``get_operation`` / ``get_operation_by_slot``) returns so response loss
  is resolved by GET/query/adoption, never a second POST/claim.

- **The shared session-effect seam.**  ``authorize_effect_intent``
  CAS-records the next physical effect intent only while the exact operation
  is the winner, the requested step is the EXACT next step after the current
  journal phase (skips, reversals, and concurrent out-of-order steps refuse),
  the caller's expected phase matches the current phase, the session
  lifecycle is still the bound epoch and is not ``stopped``, the fork-owned
  session barrier has not been claimed by Stop, and the bound
  stable-agent/source/restore facts still agree with the operation.  Every
  later phase authorization rechecks the same facts.  An exact replay of an
  already-recorded intent — and an exact operation claim replay — adopts the
  durable truth FIRST, so response loss followed by Stop/lifecycle/roster
  drift converges instead of falsely conflicting.  One logical physical step
  has exactly one intent: a different effect id for an already-won step
  surfaces the durable winner through a typed conflict and
  ``get_effect_intent_by_step``.  ``claim_session_barrier`` /
  ``get_session_barrier`` expose the narrow durable barrier primitive M3-C
  will claim during Stop.  Effect-intent-wins-then-barrier preserves the
  in-flight intent for later M3-C reconciliation; barrier-wins-then-effect
  admits no later phase.  Barriers never expire and are never cleared
  automatically.

This journal/CAS is deliberately NOT a new abstract identity-claim ceremony:
native identity was already captured by M3-A, and B2 adds only the smallest
durable winner and effect-boundary state needed to prevent duplicate physical
actions after a good-faith retry, race, or response loss.  The supervisor's
ordinary authority to resurrect its own dormant or lost workers through the
later B3 primitive is preserved — no routine human gate, no credentials, no
hostile-local-process defenses.  Harness/model routing stays replaceable:
exact restoration preserves the actual harness/native session, while any
route/model/effort/mode variation remains a proven same-harness
capability-cell question rather than a hard-coded provider matrix.

B2 performs NO tmux, provider, native attachment, terminal creation, input,
Stop/Pause, conductor, or task/supervisor effect.  The effect ``steps`` this
module accepts are recorded intent labels with zero physical behavior; the
closed vocabulary mirrors the accepted M3-B physical sequence so B3 does not
invent names.  Never hold a Python lock, flock, database transaction, or SQL
row lock over future tmux/provider I/O: every call commits its short
transaction and returns the durable record BEFORE the caller performs any
physical I/O.

Storage: three additive ORM tables (``reincarnation_operations``,
``reincarnation_effect_intents``, ``session_effect_barriers``) created by
``clients/database.py`` (``create_all`` for fresh databases,
``_migrate_operation_journal`` for existing ones).  All mutation runs inside
one SQLite transaction; no file or database lock is ever held across
provider, tmux, or network I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

#: Versioned identity of the operation-journal record itself.  A reader that
#: sees an unknown version in a stored record must degrade truthfully — the
#: read path returns stored bytes without re-validating them against this
#: binary's schema, and the effect seam refuses with a typed conflict.
SCHEMA_VERSION = "cao-m3-operation-journal-v1"

#: Closed vocabulary of recorded physical-effect intent labels.  These are
#: the accepted M3-B sequence steps, recorded as durable intent BEFORE any
#: physical I/O; this slice performs none of them.
EFFECT_STEP_FENCE_PRIOR = "fence_prior"
EFFECT_STEP_REAP_PRIOR = "reap_prior"
EFFECT_STEP_RELEASE_ATTACHMENT = "release_attachment"
EFFECT_STEP_ACQUIRE_NATIVE = "acquire_native"
EFFECT_STEP_CREATE_PANE = "create_pane"
EFFECT_STEP_LAUNCH_RESUME = "launch_resume"
EFFECT_STEP_VERIFY_IDENTITY = "verify_identity"
EFFECT_STEP_ADMIT_INPUT = "admit_input"
EFFECT_STEPS = frozenset(
    {
        EFFECT_STEP_FENCE_PRIOR,
        EFFECT_STEP_REAP_PRIOR,
        EFFECT_STEP_RELEASE_ATTACHMENT,
        EFFECT_STEP_ACQUIRE_NATIVE,
        EFFECT_STEP_CREATE_PANE,
        EFFECT_STEP_LAUNCH_RESUME,
        EFFECT_STEP_VERIFY_IDENTITY,
        EFFECT_STEP_ADMIT_INPUT,
    }
)

#: Journal phases of the winning operation.  ``claimed`` means the winner is
#: bound with no physical effect authorized yet; every later phase is the
#: last authorized physical step, in the exact accepted sequence.  The seam
#: authorizes only the NEXT step after the current phase, so skips,
#: reversals, and concurrent out-of-order steps are refused while exact
#: replays adopt.  Later B3 slices may extend the vocabulary; an unknown
#: stored phase is a typed refusal, never a silent pass.
PHASE_CLAIMED = "claimed"

#: The accepted M3-B physical sequence, in order.  Each value is both a
#: closed effect-step label and a journal phase: authorizing a step advances
#: the operation's phase to that step.
_EFFECT_STEP_ORDER = (
    PHASE_CLAIMED,
    EFFECT_STEP_FENCE_PRIOR,
    EFFECT_STEP_REAP_PRIOR,
    EFFECT_STEP_RELEASE_ATTACHMENT,
    EFFECT_STEP_ACQUIRE_NATIVE,
    EFFECT_STEP_CREATE_PANE,
    EFFECT_STEP_LAUNCH_RESUME,
    EFFECT_STEP_VERIFY_IDENTITY,
    EFFECT_STEP_ADMIT_INPUT,
)

PHASES = frozenset(_EFFECT_STEP_ORDER)


def _next_phase(phase: str) -> Optional[str]:
    """The one step that may be authorized after ``phase``, or None when the
    sequence is complete or the phase is unknown."""
    if phase not in PHASES:
        return None
    try:
        return _EFFECT_STEP_ORDER[_EFFECT_STEP_ORDER.index(phase) + 1]
    except IndexError:
        return None


#: Barrier states.  ``open`` is the truthful absence of a Stop claim; a
#: claimed barrier is the durable linearization point after which no later
#: reincarnation effect phase may begin.
BARRIER_OPEN = "open"
BARRIER_CLAIMED = "claimed"
BARRIER_STATES = frozenset({BARRIER_OPEN, BARRIER_CLAIMED})

MAX_AGENT_ID_LEN = 64  # canonical lowercase UUID
MAX_SESSION_LEN = 128
MAX_TEXT_LEN = 512
MAX_MODEL_LEN = 256
MAX_EFFECT_PAYLOAD_KEYS = 8
MAX_EFFECT_PAYLOAD_VALUE_LEN = 512
MAX_EFFECT_PAYLOAD_BYTES = 2048

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class OperationJournalError(RuntimeError):
    """Base error for operation-journal operations."""

    code = "operation-journal-error"


class OperationJournalInvalid(OperationJournalError):
    """A supplied request/intent value is malformed or violates the schema."""

    code = "operation-journal-invalid"


class OperationJournalConflict(OperationJournalError):
    """The claim/intent conflicts with immutable identity already on record,
    or the operation is no longer in an authorizable state."""

    code = "operation-journal-conflict"


class OperationJournalNotFound(OperationJournalError):
    """No operation/intent/barrier exists for the requested identity."""

    code = "operation-journal-not-found"


class OperationJournalUnavailable(OperationJournalError):
    """The operation-journal store could not be read or written.

    A SQLite unique/busy race may leave the caller's transaction unusable;
    the caller rolls it back and retries the whole caller-owned call, which
    then adopts/conflicts against the winner's committed row.
    """

    code = "operation-journal-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_text(value: Any, *, field: str, max_len: int = MAX_TEXT_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise OperationJournalInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise OperationJournalInvalid(f"{field} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field: str, max_len: int = MAX_TEXT_LEN) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field=field, max_len=max_len)


def _require_uuid(value: Any, *, field: str) -> str:
    text_value: str = _require_text(value, field=field, max_len=MAX_AGENT_ID_LEN)
    try:
        if str(uuid.UUID(text_value)) != text_value:
            raise ValueError
    except ValueError as exc:
        raise OperationJournalInvalid(
            f"{field} must be a canonical lowercase UUID; got {text_value!r}"
        ) from exc
    return text_value


def _require_digest(value: Any, *, field: str) -> str:
    text_value: str = _require_text(value, field=field, max_len=64)
    if _SHA256_RE.fullmatch(text_value) is None:
        raise OperationJournalInvalid(
            f"{field} must be 64 lowercase hex characters; got {text_value!r}"
        )
    return text_value


def _canonical_json(value: Any) -> str:
    """Compact canonical JSON for byte-comparable mappings."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _validate_effect_payload(value: Any) -> dict[str, str]:
    """Bound and redact the recorded effect-intent payload.

    The payload is a flat mapping of string references/digests — bounded key
    count, value length, and serialized bytes.  This is cooperative schema
    discipline (an obviously mislabeled value is refused); it is not a secret
    scanner.  Task text, provider output, secrets, and arbitrary environment
    values never belong here by design.
    """
    if not isinstance(value, dict):
        raise OperationJournalInvalid(
            f"effect_payload must be a flat mapping of strings; got {value!r}"
        )
    if len(value) > MAX_EFFECT_PAYLOAD_KEYS:
        raise OperationJournalInvalid(
            f"effect_payload may carry at most {MAX_EFFECT_PAYLOAD_KEYS} entries"
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise OperationJournalInvalid(f"effect_payload key {key!r} must be a non-empty string")
        if not isinstance(item, str) or not item:
            raise OperationJournalInvalid(
                f"effect_payload.{key} must be a non-empty string; got {item!r}"
            )
        if len(item) > MAX_EFFECT_PAYLOAD_VALUE_LEN:
            raise OperationJournalInvalid(
                f"effect_payload.{key} must be at most {MAX_EFFECT_PAYLOAD_VALUE_LEN} characters"
            )
        result[key] = item
    if len(_canonical_json(result)) > MAX_EFFECT_PAYLOAD_BYTES:
        raise OperationJournalInvalid(
            f"effect_payload must serialize to at most {MAX_EFFECT_PAYLOAD_BYTES} bytes"
        )
    return result


@dataclass(frozen=True)
class OperationRequest:
    """The immutable, canonical request one reincarnation operation binds.

    Every field is a durable identity fact or a bounded reference/digest —
    never a secret value, never fabricated.  ``restore_contract_schema``
    must be the exact B1 schema version; the compatibility cell is recorded
    as a reference/digest and is never inferred as passing here.
    """

    operation_id: str
    session_name: str
    agent_id: str
    roster_revision: int
    role: str
    profile_family: str
    lineage_id: str
    harness: str
    native_session_id: str
    prior_terminal_id: str
    prior_generation: Optional[str]
    prior_incarnation_id: str
    lifecycle_epoch: int
    lifecycle_observation: str
    restore_contract_id: str
    restore_contract_digest: str
    restore_contract_schema: str
    route_provider: Optional[str] = None
    model_requested: Optional[str] = None
    effort_requested: Optional[str] = None
    execution_mode_requested: Optional[str] = None
    compatibility_cell_ref: Optional[str] = None
    compatibility_cell_digest: Optional[str] = None
    #: Kept last so the constructor stays keyword-friendly; it is part of the
    #: canonical payload and digest.
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise OperationJournalInvalid(
                f"schema_version must be {SCHEMA_VERSION!r}; got {self.schema_version!r}"
            )
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        # The canonical CAO session name: every entry point normalises with
        # the lifecycle normalizer, so bare and canonical spellings converge
        # to one stored/digested request and one lifecycle row.  The NORMALISED
        # form is bounded, so adding the ``cao-`` prefix cannot exceed the
        # limit.
        raw_session = _require_text(
            self.session_name, field="session_name", max_len=MAX_SESSION_LEN
        )
        normalised_session = sl.normalise_session_name(raw_session)
        if len(normalised_session) > MAX_SESSION_LEN:
            raise OperationJournalInvalid(
                f"session_name normalises to {len(normalised_session)} characters, "
                f"exceeding the {MAX_SESSION_LEN}-character limit"
            )
        object.__setattr__(self, "session_name", normalised_session)
        object.__setattr__(self, "agent_id", _require_uuid(self.agent_id, field="agent_id"))
        if (
            not isinstance(self.roster_revision, int)
            or isinstance(self.roster_revision, bool)
            or self.roster_revision < 0
        ):
            raise OperationJournalInvalid(
                f"roster_revision must be a non-negative integer; got {self.roster_revision!r}"
            )
        if self.role not in roster.ROLES:
            raise OperationJournalInvalid(
                f"role must be one of {sorted(roster.ROLES)}; got {self.role!r}"
            )
        object.__setattr__(
            self, "profile_family", _require_text(self.profile_family, field="profile_family")
        )
        object.__setattr__(self, "lineage_id", _require_uuid(self.lineage_id, field="lineage_id"))
        object.__setattr__(self, "harness", _require_text(self.harness, field="harness"))
        object.__setattr__(
            self,
            "native_session_id",
            _require_text(self.native_session_id, field="native_session_id", max_len=512),
        )
        object.__setattr__(
            self,
            "prior_terminal_id",
            _require_text(self.prior_terminal_id, field="prior_terminal_id", max_len=64),
        )
        object.__setattr__(
            self,
            "prior_generation",
            _optional_text(self.prior_generation, field="prior_generation", max_len=128),
        )
        object.__setattr__(
            self,
            "prior_incarnation_id",
            _require_uuid(self.prior_incarnation_id, field="prior_incarnation_id"),
        )
        if (
            not isinstance(self.lifecycle_epoch, int)
            or isinstance(self.lifecycle_epoch, bool)
            or self.lifecycle_epoch < 0
        ):
            raise OperationJournalInvalid(
                f"lifecycle_epoch must be a non-negative integer; got {self.lifecycle_epoch!r}"
            )
        if self.lifecycle_observation not in sl.LIFECYCLES:
            raise OperationJournalInvalid(
                f"lifecycle_observation must be one of {sorted(sl.LIFECYCLES)}; "
                f"got {self.lifecycle_observation!r}"
            )
        object.__setattr__(
            self,
            "restore_contract_id",
            _require_text(self.restore_contract_id, field="restore_contract_id", max_len=64),
        )
        object.__setattr__(
            self,
            "restore_contract_digest",
            _require_digest(self.restore_contract_digest, field="restore_contract_digest"),
        )
        if self.restore_contract_schema != rc.SCHEMA_VERSION:
            raise OperationJournalInvalid(
                f"restore_contract_schema must be {rc.SCHEMA_VERSION!r}; "
                f"got {self.restore_contract_schema!r}"
            )
        object.__setattr__(
            self,
            "route_provider",
            _optional_text(self.route_provider, field="route_provider", max_len=128),
        )
        object.__setattr__(
            self,
            "model_requested",
            _optional_text(self.model_requested, field="model_requested", max_len=MAX_MODEL_LEN),
        )
        object.__setattr__(
            self,
            "effort_requested",
            _optional_text(self.effort_requested, field="effort_requested", max_len=MAX_MODEL_LEN),
        )
        if self.execution_mode_requested is not None:
            try:
                mode = em.validate_mode(
                    self.execution_mode_requested, field="execution_mode_requested"
                )
            except em.ExecutionModeInvalid as exc:
                raise OperationJournalInvalid(str(exc)) from exc
            object.__setattr__(self, "execution_mode_requested", mode)
        # Partial compatibility evidence is refused: the reference and its
        # digest travel together or not at all.  The pair is still only
        # recorded — never inferred as a passing verdict here.
        if (self.compatibility_cell_ref is None) != (self.compatibility_cell_digest is None):
            raise OperationJournalInvalid(
                "compatibility_cell_ref and compatibility_cell_digest must be both "
                "present or both absent; partial compatibility evidence is refused"
            )
        object.__setattr__(
            self,
            "compatibility_cell_ref",
            _optional_text(
                self.compatibility_cell_ref, field="compatibility_cell_ref", max_len=512
            ),
        )
        object.__setattr__(
            self,
            "compatibility_cell_digest",
            (
                _require_digest(self.compatibility_cell_digest, field="compatibility_cell_digest")
                if self.compatibility_cell_digest is not None
                else None
            ),
        )

    def digest(self) -> str:
        return request_digest(self)


def _payload_dict(request: OperationRequest) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": request.operation_id,
        "session_name": request.session_name,
        "agent_id": request.agent_id,
        "roster_revision": request.roster_revision,
        "role": request.role,
        "profile_family": request.profile_family,
        "lineage_id": request.lineage_id,
        "harness": request.harness,
        "native_session_id": request.native_session_id,
        "prior_terminal_id": request.prior_terminal_id,
        "prior_generation": request.prior_generation,
        "prior_incarnation_id": request.prior_incarnation_id,
        "lifecycle_epoch": request.lifecycle_epoch,
        "lifecycle_observation": request.lifecycle_observation,
        "restore_contract_id": request.restore_contract_id,
        "restore_contract_digest": request.restore_contract_digest,
        "restore_contract_schema": request.restore_contract_schema,
        "route_provider": request.route_provider,
        "model_requested": request.model_requested,
        "effort_requested": request.effort_requested,
        "execution_mode_requested": request.execution_mode_requested,
        "compatibility_cell_ref": request.compatibility_cell_ref,
        "compatibility_cell_digest": request.compatibility_cell_digest,
    }


def canonical_payload(request: OperationRequest) -> str:
    return json.dumps(_payload_dict(request), sort_keys=True, separators=(",", ":"))


def request_digest(request: OperationRequest) -> str:
    return hashlib.sha256(canonical_payload(request).encode("utf-8")).hexdigest()


def decode_stored_request(payload: Any) -> Optional["OperationRequest"]:
    """Rebuild a complete typed ``OperationRequest`` from a stored payload.

    The payload runs through the SAME constructor used at claim time, which
    validates every required fact, mode, and identity — so a stored record
    missing a required field, carrying a malformed value, an unknown schema
    version, or a non-object shape cannot decode.  Returns ``None`` for any
    shape this binary cannot fully validate; never raises.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return OperationRequest(**payload)
    except (OperationJournalInvalid, TypeError, ValueError):
        return None


def stored_operation_refusal(record: dict[str, Any]) -> Optional[str]:
    """Total refusal reason for one STORED operation row record at the effect
    boundary (the seam's gate).

    ``record`` is the full stored row dict from the read surface — the parsed
    request payload (``record["request"]``), the raw canonical JSON
    (``record["request_json"]``), and every duplicated indexed column.
    Verifies, before any mutation:

    1. complete shape — the stored payload decodes into a full typed
       ``OperationRequest`` through the same constructor used at claim time;
    2. canonical bytes — the stored JSON equals the decoded request's
       canonical serialization AND hashes to the stored digest, so a
       semantically valid but non-canonical representation (or a
       content/digest divergence) is refused;
    3. column consistency — every duplicated row column equals the decoded
       canonical payload, so an accidental column-only edit cannot produce
       contradictory reads;
    4. known phase — an unknown stored phase is a typed refusal, never a
       silent pass.

    Returns ``None`` only for a complete, canonical, digest-consistent record
    whose row and JSON agree.  Never raises on malformed input; every
    malformed/unknown/divergent shape maps to a typed reason.  This is
    bounded partial-write/schema-drift safety at the effect boundary, not a
    general corruption framework or hostile tamper protection.
    """
    stored_json = record.get("request_json")
    stored_digest = record.get("request_digest")
    payload = record.get("request")
    if stored_json is None or stored_digest is None:
        return "stored operation record is missing its canonical request JSON or digest"
    request = decode_stored_request(payload)
    if request is None:
        return (
            "stored operation request payload does not decode into a complete "
            "validated request (missing/malformed fields, unknown schema, or "
            "non-object shape)"
        )
    canonical = canonical_payload(request)
    if stored_json != canonical:
        return (
            "stored operation request JSON is not the decoded request's canonical "
            "serialization; a non-canonical or divergent representation is refused"
        )
    if hashlib.sha256(stored_json.encode("utf-8")).hexdigest() != stored_digest:
        return (
            "stored operation request JSON does not hash to the stored digest; "
            "content/digest divergence"
        )
    canonical_map = _payload_dict(request)
    for column_key, canonical_value in canonical_map.items():
        if record.get(column_key) != canonical_value:
            return (
                f"stored operation row column {column_key!r} ({record.get(column_key)!r}) "
                f"differs from the decoded request's canonical payload "
                f"({canonical_value!r}); contradictory row/JSON copies"
            )
    if record.get("phase") not in PHASES:
        return (
            f"stored operation phase {record.get('phase')!r} is not a known journal "
            f"phase; expected one of {sorted(PHASES)}"
        )
    return None


def _operation_row_dict(row: Any) -> dict[str, Any]:
    return {
        "operation_id": row.operation_id,
        "request_digest": row.request_digest,
        "schema_version": row.schema_version,
        "session_name": row.session_name,
        "agent_id": row.agent_id,
        "roster_revision": row.roster_revision,
        "role": row.role,
        "profile_family": row.profile_family,
        "lineage_id": row.lineage_id,
        "harness": row.harness,
        "native_session_id": row.native_session_id,
        "prior_terminal_id": row.prior_terminal_id,
        "prior_generation": row.prior_generation,
        "prior_incarnation_id": row.prior_incarnation_id,
        "lifecycle_epoch": row.lifecycle_epoch,
        "lifecycle_observation": row.lifecycle_observation,
        "restore_contract_id": row.restore_contract_id,
        "restore_contract_digest": row.restore_contract_digest,
        "restore_contract_schema": row.restore_contract_schema,
        "route_provider": row.route_provider,
        "model_requested": row.model_requested,
        "effort_requested": row.effort_requested,
        "execution_mode_requested": row.execution_mode_requested,
        "compatibility_cell_ref": row.compatibility_cell_ref,
        "compatibility_cell_digest": row.compatibility_cell_digest,
        "phase": row.phase,
        "request": _parse_json(row.request_json),
        "request_json": row.request_json,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _intent_row_dict(row: Any) -> dict[str, Any]:
    return {
        "effect_id": row.effect_id,
        "operation_id": row.operation_id,
        "effect_step": row.effect_step,
        "effect_digest": row.effect_digest,
        "effect_payload": _parse_json(row.effect_payload_json),
        "effect_payload_json": row.effect_payload_json,
        "recorded_at": row.recorded_at,
    }


def _barrier_row_dict(row: Any) -> dict[str, Any]:
    return {
        "session_name": row.session_name,
        "state": row.state,
        "claimed_by": row.claimed_by,
        "reason": row.reason,
        "epoch": row.epoch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _operation_by_id(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.ReincarnationOperationModel)
        .filter(database.ReincarnationOperationModel.operation_id == operation_id)
        .one_or_none()
    )


def _operation_by_slot(
    db: Any, agent_id: str, prior_incarnation_id: str, lifecycle_epoch: int, roster_revision: int
) -> Any:
    return (
        db.query(database.ReincarnationOperationModel)
        .filter(
            database.ReincarnationOperationModel.agent_id == agent_id,
            database.ReincarnationOperationModel.prior_incarnation_id == prior_incarnation_id,
            database.ReincarnationOperationModel.lifecycle_epoch == lifecycle_epoch,
            database.ReincarnationOperationModel.roster_revision == roster_revision,
        )
        .one_or_none()
    )


def _lifecycle_in_session(db: Any, session_name: str) -> tuple[str, int]:
    """The declared lifecycle + epoch for the session, read inside the
    caller's transaction (no nested session).

    Absence means ``working`` at epoch 0, exactly like the session-lifecycle
    read surface; an unknown stored lifecycle value is returned as-is so the
    equality checks below refuse it truthfully.
    """
    name = sl.normalise_session_name(session_name)
    if not sa_inspect(db.get_bind()).has_table(database.SessionLifecycleModel.__tablename__):
        return sl.WORKING, 0
    row = (
        db.query(database.SessionLifecycleModel)
        .filter(database.SessionLifecycleModel.session_name == name)
        .one_or_none()
    )
    if row is None:
        return sl.WORKING, 0
    return row.lifecycle, int(row.epoch or 0)


def _barrier_row(db: Any, session_name: str) -> Any:
    name = sl.normalise_session_name(session_name)
    if not sa_inspect(db.get_bind()).has_table(database.SessionEffectBarrierModel.__tablename__):
        return None
    return (
        db.query(database.SessionEffectBarrierModel)
        .filter(database.SessionEffectBarrierModel.session_name == name)
        .one_or_none()
    )


def _barrier_claimed(db: Any, session_name: str) -> bool:
    row = _barrier_row(db, session_name)
    return row is not None and row.state == BARRIER_CLAIMED


def operation_store_present(db: Any = None) -> bool:
    """Whether the operation-journal tables exist on this store (read/capability).

    Dark and additive: a store that has never run the migration has no
    journal.  This is a READ surface for operators/audit — it is NOT an
    effect gate: the claim/effect seams fail closed after this build's
    initialization rather than bypassing on absence.
    """

    def _check(session: Any) -> bool:
        inspector = sa_inspect(session.get_bind())
        return bool(inspector.has_table(database.ReincarnationOperationModel.__tablename__))

    if db is not None:
        return _check(db)
    with database.SessionLocal() as session:
        return _check(session)


# ---------------------------------------------------------------------------
# claim: one winning operation per exact source slot
# ---------------------------------------------------------------------------


def _claim_once(db: Any, request: OperationRequest) -> dict[str, Any]:
    """One claim pass inside the caller's transaction/savepoint.

    An exact operation-id/request replay adopts the committed durable winner
    FIRST — before any live-state gate — so response loss followed by a Stop
    barrier, lifecycle drift, or roster drift still converges on the committed
    truth instead of a false conflict.  A malformed/corrupt stored winner is a
    bounded typed refusal, never an adoption.  A different operation id
    already occupying the winner slot surfaces the durable winner.  Only for
    a genuinely new claim are every authoritative precondition (roster,
    restore-contract, lifecycle, barrier) verified before the slot is
    consumed, so a mismatched request can never occupy the slot.
    """
    payload = canonical_payload(request)
    digest = request.digest()

    # 1. Exact replay of the committed operation id adopts the durable truth
    #    without any new mutation or live-state revalidation.  Changed content
    #    under the same id still conflicts; a corrupt stored winner is a typed
    #    refusal, never an adoption.
    existing = _operation_by_id(db, request.operation_id)
    if existing is not None:
        if existing.request_json != payload:
            raise OperationJournalConflict(
                f"operation {request.operation_id} already exists with different request "
                "content; reusing an operation id with changed immutable input conflicts "
                "rather than overwriting"
            )
        record = _operation_row_dict(existing)
        refusal = stored_operation_refusal(record)
        if refusal is not None:
            raise OperationJournalConflict(
                f"stored operation {request.operation_id} cannot be adopted: {refusal}"
            )
        return {
            "operation": record,
            "adopted": True,
        }

    # 2. A different operation id for an already-won slot surfaces the durable
    #    winner deterministically (query/adopt it, never claim a second).
    winner = _operation_by_slot(
        db,
        agent_id=request.agent_id,
        prior_incarnation_id=request.prior_incarnation_id,
        lifecycle_epoch=request.lifecycle_epoch,
        roster_revision=request.roster_revision,
    )
    if winner is not None:
        raise OperationJournalConflict(
            f"the source slot (agent {request.agent_id}, prior incarnation "
            f"{request.prior_incarnation_id}, lifecycle epoch {request.lifecycle_epoch}, "
            f"roster revision {request.roster_revision}) is already claimed by winning "
            f"operation {winner.operation_id}; query or adopt the durable winner — a "
            "concurrent operation id never creates a second winner"
        )

    # 3. New claim: every authoritative precondition, verified before the
    #    winner slot is consumed.
    agent = (
        db.query(database.StableAgentModel)
        .filter(database.StableAgentModel.agent_id == request.agent_id)
        .one_or_none()
    )
    if agent is None:
        raise OperationJournalConflict(
            f"no stable agent {request.agent_id!r} is recorded; an operation must "
            "bind the exact M3-A roster identity"
        )
    if sl.normalise_session_name(agent.session_name) != request.session_name:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id} is recorded under session "
            f"{agent.session_name!r}, not {request.session_name!r}; an operation never "
            "crosses sessions"
        )
    if agent.role != request.role:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id} is recorded with role {agent.role!r}, "
            f"not {request.role!r}"
        )
    if agent.profile_family != request.profile_family:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id} is recorded with profile family "
            f"{agent.profile_family!r}, not {request.profile_family!r}"
        )
    if int(agent.revision or 0) != request.roster_revision:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id} is at roster revision {agent.revision}, "
            f"not {request.roster_revision}; the operation binds the exact post-B1 "
            "revision and a drifted revision is refused"
        )
    if agent.disposition != roster.DISPOSITION_DORMANT:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id} is {agent.disposition!r}, not "
            f"{roster.DISPOSITION_DORMANT!r}; reincarnation binds the exact retired "
            "prior incarnation of a dormant agent"
        )
    if agent.current_incarnation_id != request.prior_incarnation_id:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id}'s current incarnation is "
            f"{agent.current_incarnation_id!r}, not {request.prior_incarnation_id!r}; "
            "the prior incarnation must be the exact current source"
        )
    if agent.current_lineage_id != request.lineage_id:
        raise OperationJournalConflict(
            f"stable agent {request.agent_id}'s current lineage is "
            f"{agent.current_lineage_id!r}, not {request.lineage_id!r}"
        )

    incarnation = (
        db.query(database.StableAgentIncarnationModel)
        .filter(database.StableAgentIncarnationModel.incarnation_id == request.prior_incarnation_id)
        .one_or_none()
    )
    if incarnation is None:
        raise OperationJournalConflict(
            f"no stable-agent incarnation {request.prior_incarnation_id} is recorded; "
            "an operation binds the exact retired prior incarnation"
        )
    if incarnation.disposition != roster.INCARNATION_RETIRED:
        raise OperationJournalConflict(
            f"prior incarnation {incarnation.incarnation_id} is {incarnation.disposition!r}, "
            f"not {roster.INCARNATION_RETIRED!r}; only the exact retired source can be "
            "reincarnated"
        )
    if incarnation.agent_id != request.agent_id:
        raise OperationJournalConflict(
            f"prior incarnation {incarnation.incarnation_id} belongs to stable agent "
            f"{incarnation.agent_id!r}, not {request.agent_id!r}"
        )
    if incarnation.lineage_id != request.lineage_id:
        raise OperationJournalConflict(
            f"prior incarnation {incarnation.incarnation_id} is bound to lineage "
            f"{incarnation.lineage_id!r}, not {request.lineage_id!r}"
        )
    if incarnation.terminal_id != request.prior_terminal_id:
        raise OperationJournalConflict(
            f"prior incarnation {incarnation.incarnation_id} is terminal "
            f"{incarnation.terminal_id!r}, not {request.prior_terminal_id!r}"
        )
    if incarnation.generation != request.prior_generation:
        raise OperationJournalConflict(
            f"prior incarnation {incarnation.incarnation_id} is generation "
            f"{incarnation.generation!r}, not {request.prior_generation!r}"
        )

    lineage = (
        db.query(database.StableAgentLineageModel)
        .filter(database.StableAgentLineageModel.lineage_id == request.lineage_id)
        .one_or_none()
    )
    if lineage is None:
        raise OperationJournalConflict(f"no stable-agent lineage {request.lineage_id} is recorded")
    if lineage.harness != request.harness:
        raise OperationJournalConflict(
            f"lineage {lineage.lineage_id} belongs to harness {lineage.harness!r}, "
            f"not {request.harness!r}; native ids never cross harness domains"
        )
    if lineage.native_session_id != request.native_session_id:
        raise OperationJournalConflict(
            f"lineage {lineage.lineage_id} is bound to native session "
            f"{lineage.native_session_id!r}, not {request.native_session_id!r}; an exact "
            "reincarnation never invents or renames the provider-native session"
        )

    contract = rc.get_contract_by_incarnation(
        terminal_id=request.prior_terminal_id, generation=request.prior_generation, db=db
    )
    if contract is None:
        raise OperationJournalConflict(
            f"no immutable restore contract is recorded for source "
            f"{request.prior_terminal_id}/{request.prior_generation}; publish one "
            "before claiming an exact reincarnation"
        )
    if contract["contract_id"] != request.restore_contract_id:
        raise OperationJournalConflict(
            f"restore contract for source {request.prior_terminal_id}/"
            f"{request.prior_generation} is {contract['contract_id']!r}, not "
            f"{request.restore_contract_id!r}"
        )
    if contract["contract_digest"] != request.restore_contract_digest:
        raise OperationJournalConflict(
            f"restore contract digest mismatch for source "
            f"{request.prior_terminal_id}/{request.prior_generation}: recorded "
            f"{contract['contract_digest']}, expected {request.restore_contract_digest}"
        )
    if contract["schema_version"] != request.restore_contract_schema:
        raise OperationJournalConflict(
            f"restore contract schema is {contract['schema_version']!r}, not "
            f"{request.restore_contract_schema!r}"
        )
    stored_mismatch = rc.stored_record_refusal(contract, incarnation, lineage)
    if stored_mismatch is not None:
        raise OperationJournalConflict(
            f"stored restore contract cannot authorize the operation: {stored_mismatch}"
        )

    lifecycle, epoch = _lifecycle_in_session(db, request.session_name)
    if lifecycle != request.lifecycle_observation:
        raise OperationJournalConflict(
            f"session {request.session_name} is declared {lifecycle!r}, not "
            f"{request.lifecycle_observation!r}; the operation binds the exact declared "
            "lifecycle observation"
        )
    if epoch != request.lifecycle_epoch:
        raise OperationJournalConflict(
            f"session {request.session_name} is at lifecycle epoch {epoch}, not "
            f"{request.lifecycle_epoch}; the operation binds the exact epoch"
        )
    if lifecycle == sl.STOPPED:
        raise OperationJournalConflict(
            f"session {request.session_name} is {sl.STOPPED!r}; its panes have been "
            "collected and no reincarnation operation may begin before an "
            "operator-authorized Resume"
        )
    if _barrier_claimed(db, request.session_name):
        raise OperationJournalConflict(
            f"session {request.session_name} has a claimed Stop barrier; no "
            "reincarnation operation may begin after Stop claimed the session"
        )

    stamp = _now()
    row = database.ReincarnationOperationModel(
        operation_id=request.operation_id,
        request_digest=digest,
        schema_version=SCHEMA_VERSION,
        session_name=request.session_name,
        agent_id=request.agent_id,
        roster_revision=request.roster_revision,
        role=request.role,
        profile_family=request.profile_family,
        lineage_id=request.lineage_id,
        harness=request.harness,
        native_session_id=request.native_session_id,
        prior_terminal_id=request.prior_terminal_id,
        prior_generation=request.prior_generation,
        prior_incarnation_id=request.prior_incarnation_id,
        lifecycle_epoch=request.lifecycle_epoch,
        lifecycle_observation=request.lifecycle_observation,
        restore_contract_id=request.restore_contract_id,
        restore_contract_digest=request.restore_contract_digest,
        restore_contract_schema=request.restore_contract_schema,
        route_provider=request.route_provider,
        model_requested=request.model_requested,
        effort_requested=request.effort_requested,
        execution_mode_requested=request.execution_mode_requested,
        compatibility_cell_ref=request.compatibility_cell_ref,
        compatibility_cell_digest=request.compatibility_cell_digest,
        phase=PHASE_CLAIMED,
        request_json=payload,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    return {
        "operation": _operation_row_dict(row),
        "adopted": False,
    }


def claim_operation(request: OperationRequest, db: Any = None) -> dict[str, Any]:
    """Create or adopt the winning reincarnation operation for one exact
    source slot.

    The request is verified against the authoritative roster, restore-
    contract, and lifecycle rows BEFORE the winner slot is consumed: a
    mismatched request refuses with zero mutation — it can never occupy the
    slot.  An exact operation-id/request replay adopts the existing record;
    changed immutable input under one operation id conflicts; a different
    operation id for an already-claimed slot surfaces a typed conflict naming
    the durable winner (query/adopt it, never claim a second winner).

    ``db`` — when supplied, the write runs directly in the caller's
    transaction (no savepoint), so the caller's commit/rollback is the atomic
    boundary.  A concurrent unique-slot or SQLite lock race raises a typed
    ``OperationJournalUnavailable``; the caller's transaction may be unusable
    after the race, so the caller must roll back the whole outer transaction
    and retry the entire caller-owned call.  When omitted, a standalone
    transaction is opened and committed, and a concurrent race is retried —
    the retry reads the winner's committed row and surfaces the typed slot
    conflict or adoption deterministically.
    """
    if not isinstance(request, OperationRequest):
        raise OperationJournalInvalid(f"request must be an OperationRequest; got {request!r}")
    decoded = decode_stored_request(_payload_dict(request))
    if decoded is None:
        raise OperationJournalInvalid(
            "the request's current state does not validate as a complete operation "
            "request; a field was mutated after construction"
        )

    if db is not None:
        try:
            return _claim_once(db, decoded)
        except (IntegrityError, OperationalError) as exc:
            raise OperationJournalUnavailable(
                f"concurrent operation-journal write refused; the caller's transaction "
                f"may be unusable after the race — roll it back and retry the whole "
                f"caller-owned call: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _claim_once(session, decoded)
                session.commit()
                return result
        except IntegrityError as exc:
            # A concurrent writer won the slot between our read and write.
            # Retry in a fresh session so the next pass reads the winner's
            # committed row and surfaces the typed slot conflict/adoption.
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            # SQLite read->write upgrade contention; roll back and retry.
            last_error = exc
            time.sleep(0.05)
    raise OperationJournalUnavailable(
        f"concurrent operation-journal writes kept conflicting; refusing after retry: "
        f"{last_error}"
    )


# ---------------------------------------------------------------------------
# the shared session-effect seam
# ---------------------------------------------------------------------------


def _authorize_once(
    db: Any,
    operation_id: str,
    *,
    effect_id: str,
    effect_step: str,
    effect_payload: dict[str, str],
    expected_phase: Optional[str],
) -> dict[str, Any]:
    """One effect-intent authorization pass inside the caller's transaction.

    An EXACT replay of an already-recorded intent adopts the durable truth
    FIRST — before any phase, barrier, or lifecycle gate — so a response-loss
    retry with the original call arguments converges even after the phase
    advanced or Stop/lifecycle drift landed.  Changed content under the same
    effect id conflicts.  One logical physical step has exactly one intent: a
    different effect id for an already-won step surfaces the durable winner
    through a typed conflict and the step read path.

    Only a genuinely NEW intent runs the full gate: the operation is the
    winner; the caller's expected phase is MANDATORY and must equal the
    current journal phase (a delayed caller that never observed a transition
    can never become valid after the fact); the requested step is the EXACT
    next step after that phase (skips and reversals refuse); the session
    lifecycle is still the bound epoch and is not ``stopped``; the fork-owned
    session barrier is unclaimed; and the bound stable-agent/source/restore
    facts still agree with the operation.  The phase CAS is a checked
    one-winner transition performed FIRST in the transaction: a lost CAS
    raises the typed conflict before any intent row exists.  Only then is the
    intent durably recorded and the phase advanced to the authorized step.
    """
    if expected_phase not in PHASES:
        raise OperationJournalInvalid(
            f"expected_phase must be one of {sorted(PHASES)}; got {expected_phase!r}"
        )

    row = _operation_by_id(db, operation_id)
    if row is None:
        raise OperationJournalNotFound(f"unknown operation: {operation_id}")
    record = _operation_row_dict(row)
    refusal = stored_operation_refusal(record)
    if refusal is not None:
        raise OperationJournalConflict(
            f"stored operation {operation_id} cannot authorize an effect: {refusal}"
        )

    current_phase = row.phase

    # 1. Exact replay of an already-recorded intent adopts the durable truth
    #    with zero new mutation, regardless of phase/gate drift since the
    #    original authorization.
    payload = _canonical_json({"effect_step": effect_step, "effect_payload": effect_payload})
    effect_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    existing = (
        db.query(database.ReincarnationEffectIntentModel)
        .filter(database.ReincarnationEffectIntentModel.effect_id == effect_id)
        .one_or_none()
    )
    if existing is not None:
        # The exact replay compares the operation, step, STORED canonical
        # payload bytes, and digest: a corrupted stored JSON copy (or a
        # drifted digest column) is a bounded typed conflict, never adopted.
        if (
            existing.operation_id == operation_id
            and existing.effect_step == effect_step
            and existing.effect_payload_json == payload
            and existing.effect_digest == effect_digest
        ):
            intent = _intent_row_dict(existing)
            return {
                "intent": intent,
                "operation": _operation_row_dict(row),
                "adopted": True,
            }
        raise OperationJournalConflict(
            f"effect {effect_id} is already recorded with different intent content; "
            "reusing an effect id with changed payload conflicts rather than overwriting"
        )

    # 2. One logical physical step has exactly one intent: a different
    #    caller-minted id for an already-won step surfaces the durable winner
    #    through the typed conflict and the step read path — never a second
    #    intent that could perform the physical step twice.
    step_winner = (
        db.query(database.ReincarnationEffectIntentModel)
        .filter(
            database.ReincarnationEffectIntentModel.operation_id == operation_id,
            database.ReincarnationEffectIntentModel.effect_step == effect_step,
        )
        .one_or_none()
    )
    if step_winner is not None:
        raise OperationJournalConflict(
            f"logical step {effect_step!r} of operation {operation_id} is already won "
            f"by effect {step_winner.effect_id}; query or adopt the durable step "
            "winner — one logical physical step never gains two intents"
        )

    # 3. Every genuinely new intent must declare the journal phase it
    #    observed: the expected phase must equal the operation's current
    #    phase, so a delayed caller that never observed a transition can never
    #    become valid after the fact.  (The convenient first-step default is
    #    ``claimed``; later steps require the exact prior step phase.)
    if expected_phase != current_phase:
        raise OperationJournalConflict(
            f"operation {operation_id} is in journal phase {current_phase!r}, not "
            f"{expected_phase!r}; a new effect intent is authorized only from the "
            "exact phase the caller observed"
        )
    next_step = _next_phase(current_phase)
    if next_step is None:
        raise OperationJournalConflict(
            f"operation {operation_id} is in journal phase {current_phase!r}; the "
            "accepted sequence is complete and no further effect intent can be "
            "authorized"
        )
    if effect_step != next_step:
        raise OperationJournalConflict(
            f"effect step {effect_step!r} is not the next step after journal phase "
            f"{current_phase!r}; the accepted sequence is "
            f"{' -> '.join(_EFFECT_STEP_ORDER)} and skips or reversals are refused"
        )

    # 4. The operation must still be the slot's winner (structurally
    #    guaranteed by the unique index; a corrupted/duplicated store refuses
    #    truthfully).
    winner = _operation_by_slot(
        db,
        agent_id=row.agent_id,
        prior_incarnation_id=row.prior_incarnation_id,
        lifecycle_epoch=row.lifecycle_epoch,
        roster_revision=row.roster_revision,
    )
    if winner is None or winner.operation_id != operation_id:
        raise OperationJournalConflict(
            f"operation {operation_id} is not the winning operation for its source "
            "slot; only the durable winner may authorize physical effects"
        )

    lifecycle, epoch = _lifecycle_in_session(db, row.session_name)
    if lifecycle == sl.STOPPED:
        raise OperationJournalConflict(
            f"session {row.session_name} is {sl.STOPPED!r}; no reincarnation effect "
            "phase may begin after Stop"
        )
    if epoch != row.lifecycle_epoch:
        raise OperationJournalConflict(
            f"session {row.session_name} moved to lifecycle epoch {epoch} since the "
            f"operation was claimed at epoch {row.lifecycle_epoch}; lifecycle-epoch "
            "drift refuses the effect phase"
        )
    if _barrier_claimed(db, row.session_name):
        raise OperationJournalConflict(
            f"session {row.session_name} has a claimed Stop barrier; no later "
            "reincarnation effect phase may begin"
        )

    agent = (
        db.query(database.StableAgentModel)
        .filter(database.StableAgentModel.agent_id == row.agent_id)
        .one_or_none()
    )
    if agent is None:
        raise OperationJournalConflict(
            f"stable agent {row.agent_id} is no longer recorded; the operation's "
            "bound roster facts no longer agree"
        )
    if sl.normalise_session_name(agent.session_name) != row.session_name:
        raise OperationJournalConflict(
            f"stable agent {row.agent_id} is recorded under session "
            f"{agent.session_name!r}, not the operation's {row.session_name!r}"
        )
    if int(agent.revision or 0) != row.roster_revision:
        raise OperationJournalConflict(
            f"stable agent {row.agent_id} moved to roster revision {agent.revision} "
            f"since the operation was claimed at {row.roster_revision}; roster "
            "revision drift refuses the effect phase"
        )
    if agent.disposition != roster.DISPOSITION_DORMANT:
        raise OperationJournalConflict(
            f"stable agent {row.agent_id} is {agent.disposition!r}, not "
            f"{roster.DISPOSITION_DORMANT!r}; a successor that re-livens the agent "
            "refuses every later effect phase"
        )
    if agent.current_incarnation_id != row.prior_incarnation_id:
        raise OperationJournalConflict(
            f"stable agent {row.agent_id}'s current incarnation is "
            f"{agent.current_incarnation_id!r}, not the operation's prior "
            f"{row.prior_incarnation_id!r}"
        )
    if agent.current_lineage_id != row.lineage_id:
        raise OperationJournalConflict(
            f"stable agent {row.agent_id}'s current lineage is "
            f"{agent.current_lineage_id!r}, not the operation's {row.lineage_id!r}"
        )

    incarnation = (
        db.query(database.StableAgentIncarnationModel)
        .filter(database.StableAgentIncarnationModel.incarnation_id == row.prior_incarnation_id)
        .one_or_none()
    )
    if incarnation is None or incarnation.disposition != roster.INCARNATION_RETIRED:
        raise OperationJournalConflict(
            f"prior incarnation {row.prior_incarnation_id} is no longer the retired "
            "source; the operation's bound source facts no longer agree"
        )

    lineage = (
        db.query(database.StableAgentLineageModel)
        .filter(database.StableAgentLineageModel.lineage_id == row.lineage_id)
        .one_or_none()
    )
    if lineage is None:
        raise OperationJournalConflict(f"lineage {row.lineage_id} is no longer recorded")
    if lineage.harness != row.harness or lineage.native_session_id != row.native_session_id:
        raise OperationJournalConflict(
            f"lineage {row.lineage_id} no longer agrees with the operation's harness/"
            f"native identity ({row.harness}/{row.native_session_id}); native ids never "
            "cross harness domains and are never renamed mid-operation"
        )

    contract = rc.get_contract_by_incarnation(
        terminal_id=row.prior_terminal_id, generation=row.prior_generation, db=db
    )
    if contract is None:
        raise OperationJournalConflict(
            f"restore contract for source {row.prior_terminal_id}/{row.prior_generation} "
            "is no longer recorded; the operation's bound contract facts no longer agree"
        )
    if (
        contract["contract_id"] != row.restore_contract_id
        or contract["contract_digest"] != row.restore_contract_digest
        or contract["schema_version"] != row.restore_contract_schema
    ):
        raise OperationJournalConflict(
            f"restore contract for source {row.prior_terminal_id}/{row.prior_generation} "
            "no longer matches the operation's bound contract id/digest/schema; a "
            "changed or corrupt contract refuses the effect phase"
        )
    # A stored contract whose canonical JSON was corrupted (content/digest or
    # row/JSON divergence) is invisible to the column compare above; the B1
    # stored-record gate catches it the same way the dormant transition does.
    stored_mismatch = rc.stored_record_refusal(contract, incarnation, lineage)
    if stored_mismatch is not None:
        raise OperationJournalConflict(
            f"stored restore contract cannot authorize the effect phase: {stored_mismatch}"
        )

    stamp = _now()
    # The phase CAS is a CHECKED one-winner transition, performed FIRST in the
    # same short transaction: exactly one concurrent caller may advance the
    # phase from the observed expected phase.  A lost CAS raises the typed
    # conflict (naming the durable step winner when visible) BEFORE any intent
    # row exists; the intent is appended only after the CAS wins, so an insert
    # failure rolls the whole transaction back and the phase never represents
    # an intent it did not win.
    result = db.execute(
        sa_update(database.ReincarnationOperationModel)
        .where(
            database.ReincarnationOperationModel.operation_id == operation_id,
            database.ReincarnationOperationModel.phase == expected_phase,
        )
        .values(phase=effect_step, updated_at=stamp)
    )
    if result.rowcount != 1:
        # Another writer won the one-winner transition for this exact next
        # step.  Surface the durable step winner when visible; otherwise a
        # typed lost-CAS conflict — never an orphan intent row.
        step_winner = (
            db.query(database.ReincarnationEffectIntentModel)
            .filter(
                database.ReincarnationEffectIntentModel.operation_id == operation_id,
                database.ReincarnationEffectIntentModel.effect_step == effect_step,
            )
            .one_or_none()
        )
        if step_winner is not None:
            raise OperationJournalConflict(
                f"logical step {effect_step!r} of operation {operation_id} is already "
                f"won by effect {step_winner.effect_id}; query or adopt the durable "
                "step winner — one logical physical step never gains two intents"
            )
        raise OperationJournalConflict(
            f"operation {operation_id} is no longer in journal phase "
            f"{expected_phase!r}; the phase CAS lost and no intent was recorded — "
            "re-read the operation and retry from its current phase"
        )
    db.add(
        database.ReincarnationEffectIntentModel(
            effect_id=effect_id,
            operation_id=operation_id,
            effect_step=effect_step,
            effect_digest=effect_digest,
            effect_payload_json=payload,
            recorded_at=stamp,
        )
    )
    db.flush()
    db.refresh(row)
    intent = (
        db.query(database.ReincarnationEffectIntentModel)
        .filter(database.ReincarnationEffectIntentModel.effect_id == effect_id)
        .one()
    )
    return {
        "intent": _intent_row_dict(intent),
        "operation": _operation_row_dict(row),
        "adopted": False,
    }


def authorize_effect_intent(
    operation_id: str,
    *,
    effect_id: str,
    effect_step: str,
    effect_payload: dict[str, str],
    expected_phase: str = PHASE_CLAIMED,
    db: Any = None,
) -> dict[str, Any]:
    """The shared session-effect seam: CAS-record the next physical effect
    intent for the winning operation.

    An exact replay of an already-recorded intent adopts the durable truth
    with the ORIGINAL call arguments — even after the phase advanced or a
    barrier/lifecycle change landed.  One logical step has exactly one
    intent; a different effect id for an already-won step surfaces the
    durable winner through a typed conflict and ``get_effect_intent_by_step``.

    A genuinely NEW intent must declare the journal phase it observed:
    ``expected_phase`` is mandatory and must equal the operation's current
    phase (the convenient first-step default is ``claimed``; every later step
    requires the exact prior step phase), and the requested step must be the
    exact next step after that phase.  The phase CAS is a checked one-winner
    transition performed first in the same short transaction — a lost CAS
    raises the typed conflict and no intent row survives.  The record is the
    linearization point, not a lock: the caller runs its tmux/provider effect
    after this call returns, and if the barrier is claimed in between, the
    intent stays preserved for M3-C to adopt/drain or force-reap.  ``db`` and
    race semantics follow ``claim_operation``.
    """
    effect_id = _require_uuid(effect_id, field="effect_id")
    if effect_step not in EFFECT_STEPS:
        raise OperationJournalInvalid(
            f"effect_step must be one of {sorted(EFFECT_STEPS)}; got {effect_step!r}"
        )
    effect_payload = _validate_effect_payload(effect_payload)

    if db is not None:
        try:
            return _authorize_once(
                db,
                operation_id,
                effect_id=effect_id,
                effect_step=effect_step,
                effect_payload=effect_payload,
                expected_phase=expected_phase,
            )
        except (IntegrityError, OperationalError) as exc:
            raise OperationJournalUnavailable(
                f"concurrent effect-intent write refused; the caller's transaction "
                f"may be unusable after the race — roll it back and retry the whole "
                f"caller-owned call: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _authorize_once(
                    session,
                    operation_id,
                    effect_id=effect_id,
                    effect_step=effect_step,
                    effect_payload=effect_payload,
                    expected_phase=expected_phase,
                )
                session.commit()
                return result
        except IntegrityError as exc:
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05)
    raise OperationJournalUnavailable(
        f"concurrent effect-intent writes kept conflicting; refusing after retry: {last_error}"
    )


# ---------------------------------------------------------------------------
# the fork-owned session barrier primitive (M3-C consumes this later)
# ---------------------------------------------------------------------------


def _claim_barrier_once(
    db: Any, session_name: str, *, claimed_by: str, reason: Optional[str]
) -> dict[str, Any]:
    """One barrier-claim pass inside the caller's transaction.

    The first claim wins; a replayed claim adopts the existing record and
    never overwrites the first claimer.  A claimed barrier never expires and
    is never cleared automatically — only a later operator-authorized Resume
    lifecycle (M3-C/M3-F scope) may open the stopped campaign again.
    """
    name = sl.normalise_session_name(session_name)
    row = _barrier_row(db, name)
    if row is not None:
        if row.state == BARRIER_CLAIMED:
            record = _barrier_row_dict(row)
            record["adopted"] = True
            return record
        if row.state != BARRIER_OPEN:
            raise OperationJournalConflict(
                f"session {name} barrier is in unknown state {row.state!r}; only "
                f"{sorted(BARRIER_STATES)} are known"
            )
        stamp = _now()
        result = db.execute(
            sa_update(database.SessionEffectBarrierModel)
            .where(
                database.SessionEffectBarrierModel.session_name == name,
                database.SessionEffectBarrierModel.state == BARRIER_OPEN,
            )
            .values(
                state=BARRIER_CLAIMED,
                claimed_by=claimed_by,
                reason=reason,
                epoch=int(row.epoch or 0) + 1,
                updated_at=stamp,
            )
        )
        if result.rowcount != 1:
            # A concurrent claim won the CAS; adopt the committed state.
            db.refresh(row)
            record = _barrier_row_dict(row)
            record["adopted"] = True
            return record
        db.refresh(row)
        record = _barrier_row_dict(row)
        record["adopted"] = False
        return record

    stamp = _now()
    db.add(
        database.SessionEffectBarrierModel(
            session_name=name,
            state=BARRIER_CLAIMED,
            claimed_by=claimed_by,
            reason=reason,
            epoch=0,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    db.flush()
    record = _barrier_row_dict(
        db.query(database.SessionEffectBarrierModel)
        .filter(database.SessionEffectBarrierModel.session_name == name)
        .one()
    )
    record["adopted"] = False
    return record


def claim_session_barrier(
    session_name: str, *, claimed_by: str, reason: Optional[str] = None, db: Any = None
) -> dict[str, Any]:
    """Claim the durable fork-owned per-session barrier (Stop's side of the
    linearization).  The first claim wins; a replay adopts.  ``db`` and race
    semantics follow ``claim_operation``.
    """
    claimed_by = _require_text(claimed_by, field="claimed_by", max_len=MAX_TEXT_LEN)
    reason = _optional_text(reason, field="reason", max_len=MAX_TEXT_LEN)

    if db is not None:
        try:
            return _claim_barrier_once(db, session_name, claimed_by=claimed_by, reason=reason)
        except (IntegrityError, OperationalError) as exc:
            raise OperationJournalUnavailable(
                f"concurrent barrier write refused; the caller's transaction may be "
                f"unusable after the race — roll it back and retry the whole "
                f"caller-owned call: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _claim_barrier_once(
                    session, session_name, claimed_by=claimed_by, reason=reason
                )
                session.commit()
                return result
        except IntegrityError as exc:
            # A concurrent claim created the row first; retry adopts it.
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05)
    raise OperationJournalUnavailable(
        f"concurrent barrier claims kept conflicting; refusing after retry: {last_error}"
    )


# ---------------------------------------------------------------------------
# read / audit surface (deliberately small)
# ---------------------------------------------------------------------------


def get_operation(operation_id: str, db: Any = None) -> dict[str, Any]:
    """The exact winning operation record (response-loss GET surface)."""

    def _get(session: Any) -> dict[str, Any]:
        row = _operation_by_id(session, operation_id)
        if row is None:
            raise OperationJournalNotFound(f"unknown operation: {operation_id}")
        return _operation_row_dict(row)

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def get_operation_by_slot(
    agent_id: str,
    prior_incarnation_id: str,
    lifecycle_epoch: int,
    roster_revision: int,
    db: Any = None,
) -> Optional[dict[str, Any]]:
    """The winning operation for one exact source slot, or None.

    The slot is the exact uniqueness key: (stable agent, prior incarnation,
    lifecycle epoch, roster revision).  Response loss after a claim resolves
    here — the durable winner, never a second claim.
    """

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _operation_by_slot(
            session,
            agent_id=agent_id,
            prior_incarnation_id=prior_incarnation_id,
            lifecycle_epoch=lifecycle_epoch,
            roster_revision=roster_revision,
        )
        return _operation_row_dict(row) if row is not None else None

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def list_operations(
    agent_id: Optional[str] = None, session_name: Optional[str] = None, db: Any = None
) -> list[dict[str, Any]]:
    """Append-only operation history, oldest first; optionally scoped."""

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.ReincarnationOperationModel)
        if agent_id is not None:
            query = query.filter(database.ReincarnationOperationModel.agent_id == agent_id)
        if session_name is not None:
            query = query.filter(database.ReincarnationOperationModel.session_name == session_name)
        rows = query.order_by(
            database.ReincarnationOperationModel.created_at,
            database.ReincarnationOperationModel.operation_id,
        ).all()
        return [_operation_row_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)


def get_effect_intent(effect_id: str, db: Any = None) -> dict[str, Any]:
    """One recorded effect intent (M3-C reconciliation read surface)."""

    def _get(session: Any) -> dict[str, Any]:
        row = (
            session.query(database.ReincarnationEffectIntentModel)
            .filter(database.ReincarnationEffectIntentModel.effect_id == effect_id)
            .one_or_none()
        )
        if row is None:
            raise OperationJournalNotFound(f"unknown effect intent: {effect_id}")
        return _intent_row_dict(row)

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def get_effect_intent_by_step(
    operation_id: str, effect_step: str, db: Any = None
) -> Optional[dict[str, Any]]:
    """The one durable intent for a logical physical step, or None.

    The step read path a response-loss/reconstruction retry uses after a
    typed step conflict: one logical step has exactly one intent, and the
    winner is always readable here by (operation, step) — never re-claimed
    under a second effect id.
    """

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = (
            session.query(database.ReincarnationEffectIntentModel)
            .filter(
                database.ReincarnationEffectIntentModel.operation_id == operation_id,
                database.ReincarnationEffectIntentModel.effect_step == effect_step,
            )
            .one_or_none()
        )
        return _intent_row_dict(row) if row is not None else None

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def list_effect_intents(operation_id: str, db: Any = None) -> list[dict[str, Any]]:
    """The recorded effect intents of one operation, oldest first."""

    def _list(session: Any) -> list[dict[str, Any]]:
        rows = (
            session.query(database.ReincarnationEffectIntentModel)
            .filter(database.ReincarnationEffectIntentModel.operation_id == operation_id)
            .order_by(
                database.ReincarnationEffectIntentModel.recorded_at,
                database.ReincarnationEffectIntentModel.effect_id,
            )
            .all()
        )
        return [_intent_row_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)


def get_session_barrier(session_name: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The durable session barrier, or None (None is the truthful unclaimed
    state — the seam treats absence exactly like ``open``)."""

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _barrier_row(session, session_name)
        return _barrier_row_dict(row) if row is not None else None

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def list_session_barriers(db: Any = None) -> list[dict[str, Any]]:
    """Every durable session barrier, oldest first."""

    def _list(session: Any) -> list[dict[str, Any]]:
        if not sa_inspect(session.get_bind()).has_table(
            database.SessionEffectBarrierModel.__tablename__
        ):
            return []
        rows = (
            session.query(database.SessionEffectBarrierModel)
            .order_by(
                database.SessionEffectBarrierModel.created_at,
                database.SessionEffectBarrierModel.session_name,
            )
            .all()
        )
        return [_barrier_row_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)
