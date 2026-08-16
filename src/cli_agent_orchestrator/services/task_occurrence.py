"""The durable task/round occurrence seam (cond-0380 M3-D).

M3-D is the sole authority for what a *task* is. Everything below it —
M3-A's stable agents and incarnations, M3-B's exact reincarnations, M3-C's
cohort receipts and wake requests — deals in physical effects, and this module
consumes those only as opaque evidence. It never creates a second physical
roster and never reinterprets what a fork effect did.

Four rules shape the schema, and each of them is a bug that has a name:

**A terminal generation is not a task id, and neither is a native
conversation.** Both name a disposable physical effect. A stable agent
outlives many of each, and an exact reincarnation deliberately keeps the same
native conversation across a new generation. Using either as the task identity
means a resumed pane silently inherits the previous round's task, or a report
that arrives after a restore is attributed to the wrong occurrence. The
occurrence id is minted here and carries the exact effect-incarnation
reference *alongside* it, so a reader can still say which physical effect
produced which evidence.

**Stable-agent reuse must never reopen a finalized occurrence.** A stable
agent is precisely the thing that survives a pane, so "the same agent is back"
is the most likely way a finished task gets re-run. One partial unique index —
``agent_id`` where ``state = 'open'`` — makes at most one occurrence open per
agent, and a finalized row is outside that index, so reuse opens a *new*
occurrence rather than reviving a closed one. ``open_occurrence`` refuses the
finalized id explicitly rather than letting the caller discover it as an
integrity error.

**Current and finalized are separate.** A supervisor keeps revising an open
occurrence's boundary evidence and its summary/artifact seed as the round
progresses; finalizing copies the accepted values into a write-once family. If
they shared columns, a late current update would rewrite what a finished
occurrence reported — and that is exactly the update a crashed-then-retried
worker sends.

**Unknown extensions are preserved, not interpreted and not redispatched.**
An extension carries an opaque versioned payload and names its decider. A
build that does not recognise the kind — including a future build's
completion claim — keeps it verbatim and routes it to that decider. Turning an
undecided extension back into a dispatch would replay work whose owner has not
ruled on it; treating a future completion claim as completion would finalize
an occurrence on evidence this build cannot read.

What this module stores is references and digests: occurrence ids, incarnation
ids, SHA-256 digests of reports/checkpoints/summaries/artifacts, and bounded
artifact *references*. It stores no task text, no provider output, no
environment values, and no secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar, cast

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_lifecycle as sl

_T = TypeVar("_T")

SCHEMA_VERSION = "cao-m3d-task-occurrence-v1"

STATE_OPEN = "open"
STATE_FINALIZED = "finalized"
STATES = frozenset({STATE_OPEN, STATE_FINALIZED})

#: How complete the summary/artifact seed a fresh fallback would inherit is.
#: The vocabulary is closed and the value is always explicit — an absent
#: quality would be read as "fine", and a fresh worker started on a seed
#: nobody characterised is a worker guessing at what it is continuing.
SEED_COMPLETE = "complete"
SEED_TRUNCATED = "truncated"
SEED_EMPTY = "empty"
SEED_QUALITIES = frozenset({SEED_COMPLETE, SEED_TRUNCATED, SEED_EMPTY})

#: Only ``complete`` is enough to start a fresh worker without asking anybody.
#: ``truncated`` is the dangerous one: it looks like context and is missing
#: the part that mattered.
SEED_SUFFICIENT = frozenset({SEED_COMPLETE})

DISPOSITION_REPORTED = "reported"
DISPOSITION_ABANDONED = "abandoned"
DISPOSITION_SUPERSEDED = "superseded"
DISPOSITION_LOST = "lost"
FINAL_DISPOSITIONS = frozenset(
    {DISPOSITION_REPORTED, DISPOSITION_ABANDONED, DISPOSITION_SUPERSEDED, DISPOSITION_LOST}
)

ROUTING_PENDING = "pending-decider"
ROUTING_ROUTED = "routed"
ROUTING_STATES = frozenset({ROUTING_PENDING, ROUTING_ROUTED})

#: Extension kinds this build understands well enough to act on. Everything
#: else is preserved and routed; nothing else is *interpreted*. The set is
#: deliberately empty in this slice: M3-D defines the carrier, and the deciders
#: that own each kind register their own meaning.
RECOGNIZED_EXTENSION_KINDS: frozenset[str] = frozenset()

MAX_TEXT_LEN = 512
MAX_DETAIL_LEN = 2000
MAX_SESSION_LEN = 128
MAX_EXTENSION_BYTES = 16384
MAX_EXTENSIONS_PER_OCCURRENCE = 64
MAX_ARTIFACTS = 64
MAX_ROUND_INDEX = 1_000_000

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")


class TaskOccurrenceError(RuntimeError):
    code = "task-occurrence-error"


class TaskOccurrenceInvalid(TaskOccurrenceError):
    code = "task-occurrence-invalid"


class TaskOccurrenceConflict(TaskOccurrenceError):
    code = "task-occurrence-conflict"


class TaskOccurrenceNotFound(TaskOccurrenceError):
    code = "task-occurrence-not-found"


class TaskOccurrenceUnavailable(TaskOccurrenceError):
    code = "task-occurrence-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _require_text(value: Any, *, field_name: str, max_len: int = MAX_TEXT_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise TaskOccurrenceInvalid(f"{field_name} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise TaskOccurrenceInvalid(f"{field_name} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field_name: str, max_len: int = MAX_TEXT_LEN) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field_name=field_name, max_len=max_len)


def _require_uuid(value: Any, *, field_name: str) -> str:
    text_value = _require_text(value, field_name=field_name, max_len=64)
    try:
        if str(uuid.UUID(text_value)) != text_value:
            raise ValueError
    except ValueError as exc:
        raise TaskOccurrenceInvalid(
            f"{field_name} must be a canonical lowercase UUID; got {text_value!r}"
        ) from exc
    return text_value


def _require_digest(value: Any, *, field_name: str) -> str:
    text_value = _require_text(value, field_name=field_name, max_len=64)
    if _SHA256_RE.fullmatch(text_value) is None:
        raise TaskOccurrenceInvalid(
            f"{field_name} must be 64 lowercase hex characters; got {text_value!r}"
        )
    return text_value


def _optional_digest(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_digest(value, field_name=field_name)


def _normalise_session_name(value: Any) -> str:
    raw = _require_text(value, field_name="session_name", max_len=MAX_SESSION_LEN)
    name = sl.normalise_session_name(raw)
    if len(name) > MAX_SESSION_LEN:
        raise TaskOccurrenceInvalid(
            f"session_name normalises to more than {MAX_SESSION_LEN} characters"
        )
    return name


def _anchor_caller_transaction(db: Any) -> None:
    """Give SQLite a real outer transaction before a savepoint.

    Same reason as the cohort journal's copy: SQLAlchemy's Session
    transaction is lazy on SQLite, so a ``begin_nested`` issued first becomes
    the outer transaction and RELEASE commits it — which would make the
    caller's later rollback unable to undo this write.
    """
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


# ---------------------------------------------------------------------------
# the seed: what a fresh fallback would inherit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactReference:
    """One digest-bound artifact a successor may read.

    The digest is mandatory. An artifact reference with no digest is a path a
    successor would read at whatever state it happens to be in, which is the
    difference between "continue from this checkpoint" and "continue from
    whatever is on disk now".
    """

    artifact_id: str
    kind: str
    reference: str
    content_digest: str
    bytes_len: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", _require_text(self.artifact_id, field_name="artifact_id")
        )
        object.__setattr__(self, "kind", _require_text(self.kind, field_name="kind"))
        object.__setattr__(
            self, "reference", _require_text(self.reference, field_name="reference", max_len=4096)
        )
        object.__setattr__(
            self,
            "content_digest",
            _require_digest(self.content_digest, field_name="content_digest"),
        )
        if self.bytes_len is not None and (
            not isinstance(self.bytes_len, int)
            or isinstance(self.bytes_len, bool)
            or self.bytes_len < 0
        ):
            raise TaskOccurrenceInvalid("bytes_len must be a non-negative integer when present")

    def payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "reference": self.reference,
            "content_digest": self.content_digest,
            "bytes_len": self.bytes_len,
        }


@dataclass(frozen=True)
class TaskSeed:
    """The task-summary / artifact seed, and how complete it honestly is.

    ``quality`` is required rather than derived, because the only component
    that can tell truncation from completeness is the one that produced the
    summary. Deriving it from "is the digest present?" would call a summary
    that was cut off at a byte limit ``complete``, and a fresh worker started
    on that summary would continue confidently from the wrong place.

    ``summary_digest`` names a summary held elsewhere; this module stores the
    digest and never the prose.
    """

    quality: str
    summary_digest: Optional[str] = None
    artifacts: tuple[ArtifactReference, ...] = ()
    produced_by: Optional[str] = None
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.quality not in SEED_QUALITIES:
            raise TaskOccurrenceInvalid(
                f"seed quality must be one of {sorted(SEED_QUALITIES)}; got {self.quality!r}"
            )
        object.__setattr__(
            self,
            "summary_digest",
            _optional_digest(self.summary_digest, field_name="summary_digest"),
        )
        if not isinstance(self.artifacts, tuple):
            object.__setattr__(self, "artifacts", tuple(self.artifacts or ()))
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise TaskOccurrenceInvalid(f"a seed carries at most {MAX_ARTIFACTS} artifacts")
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactReference):
                raise TaskOccurrenceInvalid(
                    f"artifacts must be ArtifactReference; got {type(artifact).__name__}"
                )
        ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(set(ids)) != len(ids):
            raise TaskOccurrenceInvalid("artifact_id must be unique within one seed")
        object.__setattr__(
            self, "produced_by", _optional_text(self.produced_by, field_name="produced_by")
        )
        object.__setattr__(
            self, "note", _optional_text(self.note, field_name="note", max_len=MAX_DETAIL_LEN)
        )
        # An empty seed that names a summary or artifacts is a contradiction,
        # and it is the direction that under-reports risk: a caller who filled
        # in evidence and left the quality at its harmless-looking default.
        if self.quality == SEED_EMPTY and (self.summary_digest or self.artifacts):
            raise TaskOccurrenceInvalid(
                "an empty seed carries no summary digest and no artifacts; label a seed with "
                "content complete or truncated"
            )
        if self.quality != SEED_EMPTY and not self.summary_digest and not self.artifacts:
            raise TaskOccurrenceInvalid(
                f"a {self.quality!r} seed must carry a summary digest or at least one artifact"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "summary_digest": self.summary_digest,
            "artifacts": [artifact.payload() for artifact in self.artifacts],
            "produced_by": self.produced_by,
            "note": self.note,
        }

    @property
    def artifact_seed_digest(self) -> Optional[str]:
        if not self.artifacts:
            return None
        return digest(sorted((artifact.payload() for artifact in self.artifacts), key=str))

    @property
    def sufficient_for_fresh_start(self) -> bool:
        """Whether a fresh fallback may be started on this seed unattended.

        Deliberately strict. ``truncated`` reads as context and is missing the
        part that mattered, so a fresh worker started on it would continue
        from a story it only half has — which is the "guessing" this whole
        slice is arranged to prevent.
        """
        return self.quality in SEED_SUFFICIENT


EMPTY_SEED = TaskSeed(SEED_EMPTY)


def decode_seed(payload: Any) -> Optional[TaskSeed]:
    """Rebuild a stored seed. Returns ``None`` for an unreadable carrier.

    A seed nobody can decode is not an empty seed — it is an unknown one, and
    the caller must treat it as insufficient rather than as "no context
    needed".
    """
    if not isinstance(payload, Mapping):
        return None
    try:
        artifacts = tuple(
            ArtifactReference(
                artifact_id=cast(str, item.get("artifact_id")),
                kind=cast(str, item.get("kind")),
                reference=cast(str, item.get("reference")),
                content_digest=cast(str, item.get("content_digest")),
                bytes_len=item.get("bytes_len"),
            )
            for item in payload.get("artifacts") or ()
            if isinstance(item, Mapping)
        )
        return TaskSeed(
            quality=cast(str, payload.get("quality")),
            summary_digest=payload.get("summary_digest"),
            artifacts=artifacts,
            produced_by=payload.get("produced_by"),
            note=payload.get("note"),
        )
    except (TaskOccurrenceError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# opening an occurrence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectIncarnation:
    """The exact physical effect executing an occurrence — never its identity.

    Consumed as opaque M3-A evidence. M3-D does not create incarnations, does
    not decide which one is current, and does not reinterpret what a fork
    effect did to one.
    """

    incarnation_id: str
    terminal_id: str
    generation: Optional[str] = None
    lineage_id: Optional[str] = None
    native_session_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "incarnation_id", _require_text(self.incarnation_id, field_name="incarnation_id")
        )
        object.__setattr__(
            self, "terminal_id", _require_text(self.terminal_id, field_name="terminal_id")
        )
        for name in ("generation", "lineage_id", "native_session_id"):
            object.__setattr__(
                self, name, _optional_text(getattr(self, name), field_name=name, max_len=512)
            )

    def payload(self) -> dict[str, Any]:
        return {
            "incarnation_id": self.incarnation_id,
            "terminal_id": self.terminal_id,
            "generation": self.generation,
            "lineage_id": self.lineage_id,
            "native_session_id": self.native_session_id,
        }


@dataclass(frozen=True)
class OpenRequest:
    """Open one task/round occurrence for one stable agent.

    ``task_occurrence_id`` is caller-minted so a lost response is retried as
    the *same* occurrence and adopts, rather than opening a second one for the
    same round.
    """

    task_occurrence_id: str
    session_name: str
    agent_id: str
    round_index: int
    dispatch_digest: str
    incarnation: EffectIncarnation
    dispatch_provenance: Optional[Mapping[str, Any]] = None
    seed: TaskSeed = EMPTY_SEED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_occurrence_id",
            _require_uuid(self.task_occurrence_id, field_name="task_occurrence_id"),
        )
        object.__setattr__(self, "session_name", _normalise_session_name(self.session_name))
        object.__setattr__(self, "agent_id", _require_uuid(self.agent_id, field_name="agent_id"))
        if (
            not isinstance(self.round_index, int)
            or isinstance(self.round_index, bool)
            or self.round_index < 0
            or self.round_index > MAX_ROUND_INDEX
        ):
            raise TaskOccurrenceInvalid(
                f"round_index must be an integer in [0, {MAX_ROUND_INDEX}]; "
                f"got {self.round_index!r}"
            )
        object.__setattr__(
            self,
            "dispatch_digest",
            _require_digest(self.dispatch_digest, field_name="dispatch_digest"),
        )
        if not isinstance(self.incarnation, EffectIncarnation):
            raise TaskOccurrenceInvalid(
                f"incarnation must be an EffectIncarnation; got {type(self.incarnation).__name__}"
            )
        if not isinstance(self.seed, TaskSeed):
            raise TaskOccurrenceInvalid(f"seed must be a TaskSeed; got {type(self.seed).__name__}")
        if self.dispatch_provenance is not None:
            if not isinstance(self.dispatch_provenance, Mapping):
                raise TaskOccurrenceInvalid("dispatch_provenance must be a mapping when present")
            encoded = canonical_json(dict(self.dispatch_provenance))
            if len(encoded.encode("utf-8")) > MAX_EXTENSION_BYTES:
                raise TaskOccurrenceInvalid(
                    f"dispatch_provenance must encode to at most {MAX_EXTENSION_BYTES} bytes"
                )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_occurrence_id": self.task_occurrence_id,
            "session_name": self.session_name,
            "agent_id": self.agent_id,
            "round_index": self.round_index,
            "dispatch_digest": self.dispatch_digest,
            "incarnation": self.incarnation.payload(),
        }


def _occurrence_dict(row: Any) -> dict[str, Any]:
    return {
        "task_occurrence_id": row.task_occurrence_id,
        "schema_version": row.schema_version,
        "session_name": row.session_name,
        "agent_id": row.agent_id,
        "round_index": int(row.round_index or 0),
        "dispatch_digest": row.dispatch_digest,
        "dispatch_provenance": _parse_json(row.dispatch_provenance_json),
        "incarnation_id": row.incarnation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "lineage_id": row.lineage_id,
        "native_session_id": row.native_session_id,
        "state": row.state,
        "current": {
            "boundary_digest": row.current_boundary_digest,
            "report_digest": row.current_report_digest,
            "checkpoint_digest": row.current_checkpoint_digest,
            "provenance": _parse_json(row.current_provenance_json),
            "summary_seed_digest": row.current_summary_seed_digest,
            "artifact_seed_digest": row.current_artifact_seed_digest,
            "seed_quality": row.current_seed_quality,
            "seed": _parse_json(row.current_seed_json),
        },
        "finalized": {
            "disposition": row.final_disposition,
            "boundary_digest": row.finalized_boundary_digest,
            "report_digest": row.finalized_report_digest,
            "checkpoint_digest": row.finalized_checkpoint_digest,
            "provenance": _parse_json(row.finalized_provenance_json),
            "summary_seed_digest": row.finalized_summary_seed_digest,
            "artifact_seed_digest": row.finalized_artifact_seed_digest,
            "seed_quality": row.finalized_seed_quality,
            "seed": _parse_json(row.finalized_seed_json),
            "finalized_by": row.finalized_by,
            "finalized_at": row.finalized_at,
        },
        "revision": int(row.revision or 0),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _extension_dict(row: Any) -> dict[str, Any]:
    return {
        "task_occurrence_id": row.task_occurrence_id,
        "extension_id": row.extension_id,
        "extension_kind": row.extension_kind,
        "extension_version": row.extension_version,
        "decider": row.decider,
        "payload_digest": row.payload_digest,
        "payload": _parse_json(row.payload_json),
        "claims_final": bool(row.claims_final),
        "recognized": bool(row.recognized),
        "routing_state": row.routing_state,
        "routed_at": row.routed_at,
        "routed_receipt": row.routed_receipt,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _row_by_id(db: Any, task_occurrence_id: str) -> Any:
    return (
        db.query(database.TaskOccurrenceModel)
        .filter(database.TaskOccurrenceModel.task_occurrence_id == task_occurrence_id)
        .one_or_none()
    )


def _open_row_for_agent(db: Any, agent_id: str) -> Any:
    return (
        db.query(database.TaskOccurrenceModel)
        .filter(
            database.TaskOccurrenceModel.agent_id == agent_id,
            database.TaskOccurrenceModel.state == STATE_OPEN,
        )
        .one_or_none()
    )


def _open_once(db: Any, request: OpenRequest) -> dict[str, Any]:
    existing = _row_by_id(db, request.task_occurrence_id)
    if existing is not None:
        # A finalized occurrence is never reopened, and saying so by id is
        # far clearer than letting the caller trip the partial unique index.
        if existing.state == STATE_FINALIZED:
            raise TaskOccurrenceConflict(
                f"task occurrence {request.task_occurrence_id} is finalized "
                f"({existing.final_disposition!r}); a stable agent that is reused opens a new "
                "occurrence and never reopens a finished one"
            )
        stored = {
            "session_name": existing.session_name,
            "agent_id": existing.agent_id,
            "round_index": int(existing.round_index or 0),
            "dispatch_digest": existing.dispatch_digest,
            "incarnation_id": existing.incarnation_id,
            "terminal_id": existing.terminal_id,
            "generation": existing.generation,
        }
        requested = {
            "session_name": request.session_name,
            "agent_id": request.agent_id,
            "round_index": request.round_index,
            "dispatch_digest": request.dispatch_digest,
            "incarnation_id": request.incarnation.incarnation_id,
            "terminal_id": request.incarnation.terminal_id,
            "generation": request.incarnation.generation,
        }
        if stored != requested:
            raise TaskOccurrenceConflict(
                f"task occurrence {request.task_occurrence_id} already exists with different "
                "immutable content"
            )
        record = _occurrence_dict(existing)
        record["adopted"] = True
        return record

    live = _open_row_for_agent(db, request.agent_id)
    if live is not None:
        raise TaskOccurrenceConflict(
            f"stable agent {request.agent_id} already has open task occurrence "
            f"{live.task_occurrence_id} (round {live.round_index}); one agent executes one task "
            "at a time"
        )
    same_round = (
        db.query(database.TaskOccurrenceModel)
        .filter(
            database.TaskOccurrenceModel.session_name == request.session_name,
            database.TaskOccurrenceModel.agent_id == request.agent_id,
            database.TaskOccurrenceModel.round_index == request.round_index,
        )
        .one_or_none()
    )
    if same_round is not None:
        raise TaskOccurrenceConflict(
            f"stable agent {request.agent_id} already recorded round {request.round_index} as "
            f"occurrence {same_round.task_occurrence_id} ({same_round.state}); a new round needs "
            "a new round index"
        )

    stamp = _now()
    seed = request.seed
    row = database.TaskOccurrenceModel(
        task_occurrence_id=request.task_occurrence_id,
        schema_version=SCHEMA_VERSION,
        session_name=request.session_name,
        agent_id=request.agent_id,
        round_index=request.round_index,
        dispatch_digest=request.dispatch_digest,
        dispatch_provenance_json=(
            canonical_json(dict(request.dispatch_provenance))
            if request.dispatch_provenance is not None
            else None
        ),
        incarnation_id=request.incarnation.incarnation_id,
        terminal_id=request.incarnation.terminal_id,
        generation=request.incarnation.generation,
        lineage_id=request.incarnation.lineage_id,
        native_session_id=request.incarnation.native_session_id,
        state=STATE_OPEN,
        current_boundary_digest=None,
        current_report_digest=None,
        current_checkpoint_digest=None,
        current_provenance_json=None,
        current_summary_seed_digest=seed.summary_digest,
        current_artifact_seed_digest=seed.artifact_seed_digest,
        current_seed_quality=seed.quality,
        current_seed_json=canonical_json(seed.payload()),
        revision=0,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    record = _occurrence_dict(row)
    record["adopted"] = False
    return record


def _with_session(fn: Callable[[Any], _T], db: Any, *, unavailable: str) -> _T:
    if db is not None:
        try:
            _anchor_caller_transaction(db)
            with db.begin_nested():
                return fn(db)
        except TaskOccurrenceError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise TaskOccurrenceUnavailable(f"{unavailable}: {exc}") from exc
    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = fn(session)
                session.commit()
                return result
        except TaskOccurrenceError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise TaskOccurrenceUnavailable(f"{unavailable}: {last_error}")


def open_occurrence(request: OpenRequest, db: Any = None) -> dict[str, Any]:
    """Open one task/round occurrence. Performs no dispatch and no effect."""
    if not isinstance(request, OpenRequest):
        raise TaskOccurrenceInvalid(f"request must be an OpenRequest; got {type(request).__name__}")
    return _with_session(
        lambda session: _open_once(session, request),
        db,
        unavailable="concurrent task-occurrence opens kept conflicting",
    )


# ---------------------------------------------------------------------------
# recording a boundary, and finalizing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundaryRecord:
    """One safe-boundary observation for an open occurrence.

    A boundary is only meaningful with evidence attached: a worker that says
    it reached a boundary and produced neither a report nor a checkpoint has
    told nobody anything, and a drain that accepted that would be a drain in
    name only. So at least one of the two digests is required.
    """

    task_occurrence_id: str
    expected_revision: int
    recorded_by: str
    report_digest: Optional[str] = None
    checkpoint_digest: Optional[str] = None
    provenance: Optional[Mapping[str, Any]] = None
    seed: Optional[TaskSeed] = None
    note: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_occurrence_id",
            _require_uuid(self.task_occurrence_id, field_name="task_occurrence_id"),
        )
        if (
            not isinstance(self.expected_revision, int)
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 0
        ):
            raise TaskOccurrenceInvalid("expected_revision must be a non-negative integer")
        object.__setattr__(
            self, "recorded_by", _require_text(self.recorded_by, field_name="recorded_by")
        )
        object.__setattr__(
            self, "report_digest", _optional_digest(self.report_digest, field_name="report_digest")
        )
        object.__setattr__(
            self,
            "checkpoint_digest",
            _optional_digest(self.checkpoint_digest, field_name="checkpoint_digest"),
        )
        if self.report_digest is None and self.checkpoint_digest is None:
            raise TaskOccurrenceInvalid(
                "a boundary requires a report digest or a checkpoint digest; a boundary with "
                "neither is a claim with no evidence"
            )
        if self.seed is not None and not isinstance(self.seed, TaskSeed):
            raise TaskOccurrenceInvalid("seed must be a TaskSeed when present")
        if self.provenance is not None and not isinstance(self.provenance, Mapping):
            raise TaskOccurrenceInvalid("provenance must be a mapping when present")
        object.__setattr__(
            self, "note", _optional_text(self.note, field_name="note", max_len=MAX_DETAIL_LEN)
        )

    @property
    def boundary_digest(self) -> str:
        """The digest that binds this boundary's evidence together.

        Derived rather than supplied so it cannot disagree with the evidence
        it is supposed to bind — a caller-minted boundary digest is a value a
        retry can copy forward while its report changes underneath it.
        """
        return digest(
            {
                "schema": SCHEMA_VERSION,
                "task_occurrence_id": self.task_occurrence_id,
                "report_digest": self.report_digest,
                "checkpoint_digest": self.checkpoint_digest,
                "seed": self.seed.payload() if self.seed is not None else None,
                "provenance": dict(self.provenance) if self.provenance is not None else None,
            }
        )


def _record_boundary_once(db: Any, request: BoundaryRecord) -> dict[str, Any]:
    row = _row_by_id(db, request.task_occurrence_id)
    if row is None:
        raise TaskOccurrenceNotFound(f"unknown task occurrence: {request.task_occurrence_id}")
    if row.state == STATE_FINALIZED:
        # Adopt an exact replay; refuse anything that would rewrite a
        # finished occurrence's evidence.
        if row.finalized_boundary_digest == request.boundary_digest:
            record = _occurrence_dict(row)
            record["adopted"] = True
            return record
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} is finalized; its boundary evidence "
            "is immutable"
        )
    if (
        row.current_boundary_digest == request.boundary_digest
        and int(row.revision or 0) > request.expected_revision
    ):
        record = _occurrence_dict(row)
        record["adopted"] = True
        return record
    if int(row.revision or 0) != request.expected_revision:
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} moved to revision {row.revision}; "
            f"expected {request.expected_revision}"
        )

    seed = request.seed
    values: dict[str, Any] = {
        "current_boundary_digest": request.boundary_digest,
        "current_report_digest": request.report_digest,
        "current_checkpoint_digest": request.checkpoint_digest,
        "current_provenance_json": (
            canonical_json(dict(request.provenance)) if request.provenance is not None else None
        ),
        "revision": request.expected_revision + 1,
        "updated_at": _now(),
    }
    if seed is not None:
        values.update(
            {
                "current_summary_seed_digest": seed.summary_digest,
                "current_artifact_seed_digest": seed.artifact_seed_digest,
                "current_seed_quality": seed.quality,
                "current_seed_json": canonical_json(seed.payload()),
            }
        )
    result = db.execute(
        sa_update(database.TaskOccurrenceModel)
        .where(
            database.TaskOccurrenceModel.task_occurrence_id == request.task_occurrence_id,
            database.TaskOccurrenceModel.revision == request.expected_revision,
            database.TaskOccurrenceModel.state == STATE_OPEN,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} moved concurrently; read and retry"
        )
    db.flush()
    db.refresh(row)
    record = _occurrence_dict(row)
    record["adopted"] = False
    return record


def record_boundary(request: BoundaryRecord, db: Any = None) -> dict[str, Any]:
    """CAS-record one boundary observation onto an open occurrence."""
    if not isinstance(request, BoundaryRecord):
        raise TaskOccurrenceInvalid(
            f"request must be a BoundaryRecord; got {type(request).__name__}"
        )
    return _with_session(
        lambda session: _record_boundary_once(session, request),
        db,
        unavailable="concurrent boundary writes kept conflicting",
    )


@dataclass(frozen=True)
class FinalizeRequest:
    task_occurrence_id: str
    expected_revision: int
    disposition: str
    finalized_by: str
    note: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_occurrence_id",
            _require_uuid(self.task_occurrence_id, field_name="task_occurrence_id"),
        )
        if (
            not isinstance(self.expected_revision, int)
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 0
        ):
            raise TaskOccurrenceInvalid("expected_revision must be a non-negative integer")
        if self.disposition not in FINAL_DISPOSITIONS:
            raise TaskOccurrenceInvalid(
                f"disposition must be one of {sorted(FINAL_DISPOSITIONS)}; "
                f"got {self.disposition!r}"
            )
        object.__setattr__(
            self, "finalized_by", _require_text(self.finalized_by, field_name="finalized_by")
        )
        object.__setattr__(
            self, "note", _optional_text(self.note, field_name="note", max_len=MAX_DETAIL_LEN)
        )


def _finalize_once(db: Any, request: FinalizeRequest) -> dict[str, Any]:
    row = _row_by_id(db, request.task_occurrence_id)
    if row is None:
        raise TaskOccurrenceNotFound(f"unknown task occurrence: {request.task_occurrence_id}")
    if row.state == STATE_FINALIZED:
        if row.final_disposition != request.disposition:
            raise TaskOccurrenceConflict(
                f"task occurrence {request.task_occurrence_id} is already finalized as "
                f"{row.final_disposition!r}; a finalized occurrence is write-once"
            )
        record = _occurrence_dict(row)
        record["adopted"] = True
        return record
    if int(row.revision or 0) != request.expected_revision:
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} moved to revision {row.revision}; "
            f"expected {request.expected_revision}"
        )
    # An occurrence with an undecided extension is not this build's to close.
    # Finalizing over it would resolve a claim whose decider has not ruled.
    pending = (
        db.query(database.TaskOccurrenceExtensionModel)
        .filter(
            database.TaskOccurrenceExtensionModel.task_occurrence_id == request.task_occurrence_id,
            database.TaskOccurrenceExtensionModel.routing_state == ROUTING_PENDING,
        )
        .order_by(database.TaskOccurrenceExtensionModel.extension_id)
        .all()
    )
    if pending and request.disposition == DISPOSITION_REPORTED:
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} carries "
            f"{len(pending)} extension(s) still awaiting their decider "
            f"({', '.join(item.decider for item in pending[:4])}); route them before reporting "
            "this occurrence complete"
        )

    stamp = _now()
    result = db.execute(
        sa_update(database.TaskOccurrenceModel)
        .where(
            database.TaskOccurrenceModel.task_occurrence_id == request.task_occurrence_id,
            database.TaskOccurrenceModel.revision == request.expected_revision,
            database.TaskOccurrenceModel.state == STATE_OPEN,
        )
        .values(
            state=STATE_FINALIZED,
            final_disposition=request.disposition,
            # The finalized family is a copy of the accepted current values,
            # not a reference to them: a later current write must not be able
            # to change what a finished occurrence reported.
            finalized_boundary_digest=row.current_boundary_digest,
            finalized_report_digest=row.current_report_digest,
            finalized_checkpoint_digest=row.current_checkpoint_digest,
            finalized_provenance_json=row.current_provenance_json,
            finalized_summary_seed_digest=row.current_summary_seed_digest,
            finalized_artifact_seed_digest=row.current_artifact_seed_digest,
            finalized_seed_quality=row.current_seed_quality,
            finalized_seed_json=row.current_seed_json,
            finalized_by=request.finalized_by,
            finalized_at=stamp,
            revision=request.expected_revision + 1,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} moved concurrently; read and retry"
        )
    db.flush()
    db.refresh(row)
    record = _occurrence_dict(row)
    record["adopted"] = False
    return record


def finalize_occurrence(request: FinalizeRequest, db: Any = None) -> dict[str, Any]:
    """Close one occurrence, preserving its current evidence write-once."""
    if not isinstance(request, FinalizeRequest):
        raise TaskOccurrenceInvalid(
            f"request must be a FinalizeRequest; got {type(request).__name__}"
        )
    return _with_session(
        lambda session: _finalize_once(session, request),
        db,
        unavailable="concurrent finalize writes kept conflicting",
    )


# ---------------------------------------------------------------------------
# opaque versioned extensions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtensionRecord:
    """One opaque versioned extension and the decider that owns it.

    ``payload`` is stored verbatim and never parsed for meaning here.
    ``claims_final`` is recorded as a *claim*, not honoured as a fact: a
    future build's completion claim is preserved and routed, and this build
    does not let it close an occurrence it cannot read.
    """

    task_occurrence_id: str
    extension_id: str
    extension_kind: str
    extension_version: str
    decider: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    claims_final: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_occurrence_id",
            _require_uuid(self.task_occurrence_id, field_name="task_occurrence_id"),
        )
        extension_id = _require_text(self.extension_id, field_name="extension_id", max_len=128)
        if _EXTENSION_ID_RE.fullmatch(extension_id) is None:
            raise TaskOccurrenceInvalid(
                f"extension_id must match {_EXTENSION_ID_RE.pattern}; got {extension_id!r}"
            )
        object.__setattr__(self, "extension_id", extension_id)
        kind = _require_text(self.extension_kind, field_name="extension_kind", max_len=128)
        if _KIND_RE.fullmatch(kind) is None:
            raise TaskOccurrenceInvalid(
                f"extension_kind must match {_KIND_RE.pattern}; got {kind!r}"
            )
        object.__setattr__(self, "extension_kind", kind)
        object.__setattr__(
            self,
            "extension_version",
            _require_text(self.extension_version, field_name="extension_version", max_len=64),
        )
        object.__setattr__(self, "decider", _require_text(self.decider, field_name="decider"))
        if not isinstance(self.payload, Mapping):
            raise TaskOccurrenceInvalid("extension payload must be a mapping")
        try:
            encoded = canonical_json(dict(self.payload))
        except (TypeError, ValueError) as exc:
            raise TaskOccurrenceInvalid("extension payload must be JSON-encodable") from exc
        if len(encoded.encode("utf-8")) > MAX_EXTENSION_BYTES:
            raise TaskOccurrenceInvalid(
                f"extension payload must encode to at most {MAX_EXTENSION_BYTES} bytes"
            )
        if not isinstance(self.claims_final, bool):
            raise TaskOccurrenceInvalid("claims_final must be a boolean")

    @property
    def payload_json(self) -> str:
        return canonical_json(dict(self.payload))

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()

    @property
    def recognized(self) -> bool:
        return self.extension_kind in RECOGNIZED_EXTENSION_KINDS


def _attach_extension_once(db: Any, request: ExtensionRecord) -> dict[str, Any]:
    row = _row_by_id(db, request.task_occurrence_id)
    if row is None:
        raise TaskOccurrenceNotFound(f"unknown task occurrence: {request.task_occurrence_id}")
    existing = (
        db.query(database.TaskOccurrenceExtensionModel)
        .filter(
            database.TaskOccurrenceExtensionModel.task_occurrence_id == request.task_occurrence_id,
            database.TaskOccurrenceExtensionModel.extension_id == request.extension_id,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.payload_digest != request.payload_digest:
            raise TaskOccurrenceConflict(
                f"extension {request.extension_id} already exists on task occurrence "
                f"{request.task_occurrence_id} with a different payload"
            )
        record = _extension_dict(existing)
        record["adopted"] = True
        return record
    count = (
        db.query(database.TaskOccurrenceExtensionModel)
        .filter(
            database.TaskOccurrenceExtensionModel.task_occurrence_id == request.task_occurrence_id
        )
        .count()
    )
    if count >= MAX_EXTENSIONS_PER_OCCURRENCE:
        raise TaskOccurrenceConflict(
            f"task occurrence {request.task_occurrence_id} already carries "
            f"{MAX_EXTENSIONS_PER_OCCURRENCE} extensions"
        )
    stamp = _now()
    extension = database.TaskOccurrenceExtensionModel(
        task_occurrence_id=request.task_occurrence_id,
        extension_id=request.extension_id,
        extension_kind=request.extension_kind,
        extension_version=request.extension_version,
        decider=request.decider,
        payload_digest=request.payload_digest,
        payload_json=request.payload_json,
        claims_final=int(request.claims_final),
        recognized=int(request.recognized),
        routing_state=ROUTING_PENDING,
        routed_at=None,
        routed_receipt=None,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(extension)
    db.flush()
    record = _extension_dict(extension)
    record["adopted"] = False
    return record


def attach_extension(request: ExtensionRecord, db: Any = None) -> dict[str, Any]:
    """Preserve one opaque extension against an occurrence.

    Attaching never interprets, never finalizes, and never dispatches. An
    unrecognised kind is stored exactly as received and marked for its
    decider.
    """
    if not isinstance(request, ExtensionRecord):
        raise TaskOccurrenceInvalid(
            f"request must be an ExtensionRecord; got {type(request).__name__}"
        )
    return _with_session(
        lambda session: _attach_extension_once(session, request),
        db,
        unavailable="concurrent extension writes kept conflicting",
    )


def pending_extensions(
    decider: Optional[str] = None,
    *,
    session_name: Optional[str] = None,
    db: Any = None,
) -> list[dict[str, Any]]:
    """Every extension still waiting on the decider that owns it."""
    name = _normalise_session_name(session_name) if session_name is not None else None

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.TaskOccurrenceExtensionModel).filter(
            database.TaskOccurrenceExtensionModel.routing_state == ROUTING_PENDING
        )
        if decider is not None:
            query = query.filter(database.TaskOccurrenceExtensionModel.decider == decider)
        rows = query.order_by(
            database.TaskOccurrenceExtensionModel.task_occurrence_id,
            database.TaskOccurrenceExtensionModel.extension_id,
        ).all()
        records = [_extension_dict(row) for row in rows]
        if name is None:
            return records
        occurrences = {
            occurrence.task_occurrence_id: occurrence.session_name
            for occurrence in session.query(database.TaskOccurrenceModel)
            .filter(database.TaskOccurrenceModel.session_name == name)
            .all()
        }
        return [record for record in records if record["task_occurrence_id"] in occurrences]

    try:
        if db is not None:
            return _list(db)
        with database.SessionLocal() as session:
            return _list(session)
    except TaskOccurrenceError:
        raise
    except SQLAlchemyError as exc:
        raise TaskOccurrenceUnavailable(
            f"extension read failed: {str(exc).splitlines()[0]}"
        ) from exc


def route_extension(
    task_occurrence_id: str,
    extension_id: str,
    *,
    routed_by: str,
    db: Any = None,
) -> dict[str, Any]:
    """Mark one extension delivered to its decider. Never a redispatch.

    Routing records that the decider now holds the extension. It does not
    apply the extension, does not finalize the occurrence, and does not turn
    the payload into new work — an extension whose owner has not ruled is not
    a task, and redispatching it would re-run whatever it describes.
    """
    task_occurrence_id = _require_uuid(task_occurrence_id, field_name="task_occurrence_id")
    extension_id = _require_text(extension_id, field_name="extension_id", max_len=128)
    routed_by = _require_text(routed_by, field_name="routed_by")

    def _route(session: Any) -> dict[str, Any]:
        row = (
            session.query(database.TaskOccurrenceExtensionModel)
            .filter(
                database.TaskOccurrenceExtensionModel.task_occurrence_id == task_occurrence_id,
                database.TaskOccurrenceExtensionModel.extension_id == extension_id,
            )
            .one_or_none()
        )
        if row is None:
            raise TaskOccurrenceNotFound(
                f"task occurrence {task_occurrence_id} carries no extension {extension_id!r}"
            )
        if row.routing_state == ROUTING_ROUTED:
            record = _extension_dict(row)
            record["adopted"] = True
            return record
        stamp = _now()
        receipt = digest(
            {
                "schema": SCHEMA_VERSION,
                "effect": "route-extension",
                "task_occurrence_id": task_occurrence_id,
                "extension_id": extension_id,
                "payload_digest": row.payload_digest,
                "decider": row.decider,
            }
        )
        result = session.execute(
            sa_update(database.TaskOccurrenceExtensionModel)
            .where(
                database.TaskOccurrenceExtensionModel.task_occurrence_id == task_occurrence_id,
                database.TaskOccurrenceExtensionModel.extension_id == extension_id,
                database.TaskOccurrenceExtensionModel.routing_state == ROUTING_PENDING,
            )
            .values(
                routing_state=ROUTING_ROUTED,
                routed_at=stamp,
                routed_receipt=receipt,
                updated_at=stamp,
            )
        )
        if result.rowcount != 1:
            raise TaskOccurrenceConflict(
                f"extension {extension_id} moved concurrently; read and retry"
            )
        session.flush()
        session.refresh(row)
        record = _extension_dict(row)
        record["adopted"] = False
        record["routed_by"] = routed_by
        return record

    return _with_session(_route, db, unavailable="concurrent extension routing kept conflicting")


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_occurrence(task_occurrence_id: str, db: Any = None) -> dict[str, Any]:
    """One occurrence with its extensions and a derived seed verdict."""
    task_occurrence_id = _require_uuid(task_occurrence_id, field_name="task_occurrence_id")

    def _get(session: Any) -> dict[str, Any]:
        row = _row_by_id(session, task_occurrence_id)
        if row is None:
            raise TaskOccurrenceNotFound(f"unknown task occurrence: {task_occurrence_id}")
        extensions = (
            session.query(database.TaskOccurrenceExtensionModel)
            .filter(database.TaskOccurrenceExtensionModel.task_occurrence_id == task_occurrence_id)
            .order_by(database.TaskOccurrenceExtensionModel.extension_id)
            .all()
        )
        record = _occurrence_dict(row)
        record["extensions"] = [_extension_dict(item) for item in extensions]
        record["seed_verdict"] = seed_verdict(record)
        return record

    try:
        if db is not None:
            return _get(db)
        with database.SessionLocal() as session:
            return _get(session)
    except TaskOccurrenceError:
        raise
    except SQLAlchemyError as exc:
        raise TaskOccurrenceUnavailable(
            f"task occurrence read failed: {str(exc).splitlines()[0]}"
        ) from exc


def seed_verdict(record: Mapping[str, Any]) -> dict[str, Any]:
    """Whether this occurrence's seed is enough to start a fresh successor.

    Derived once, here, rather than in the drain, the recovery path, the API,
    the CLI, and the dashboard. Five copies of "is truncated good enough?"
    would drift, and they would drift toward yes.
    """
    family = "finalized" if record.get("state") == STATE_FINALIZED else "current"
    block = record.get(family) or {}
    seed = decode_seed(block.get("seed"))
    quality = block.get("seed_quality")
    if seed is None:
        return {
            "family": family,
            "quality": quality,
            "sufficient_for_fresh_start": False,
            "reason": "the stored seed carrier could not be decoded",
        }
    if seed.sufficient_for_fresh_start:
        return {
            "family": family,
            "quality": seed.quality,
            "sufficient_for_fresh_start": True,
            "reason": None,
        }
    return {
        "family": family,
        "quality": seed.quality,
        "sufficient_for_fresh_start": False,
        "reason": (
            "the seed is empty: a fresh successor would have no summary and no artifacts"
            if seed.quality == SEED_EMPTY
            else "the seed is truncated: it reads as context while missing part of it"
        ),
    }


def list_occurrences(
    session_name: Optional[str] = None,
    *,
    agent_id: Optional[str] = None,
    state: Optional[str] = None,
    db: Any = None,
) -> list[dict[str, Any]]:
    """Occurrence projections, oldest first; never consults tmux."""
    name = _normalise_session_name(session_name) if session_name is not None else None
    if state is not None and state not in STATES:
        raise TaskOccurrenceInvalid(f"state must be one of {sorted(STATES)}; got {state!r}")

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.TaskOccurrenceModel)
        if name is not None:
            query = query.filter(database.TaskOccurrenceModel.session_name == name)
        if agent_id is not None:
            query = query.filter(database.TaskOccurrenceModel.agent_id == agent_id)
        if state is not None:
            query = query.filter(database.TaskOccurrenceModel.state == state)
        rows = query.order_by(
            database.TaskOccurrenceModel.created_at,
            database.TaskOccurrenceModel.task_occurrence_id,
        ).all()
        return [_occurrence_dict(row) for row in rows]

    try:
        if db is not None:
            return _list(db)
        with database.SessionLocal() as session:
            return _list(session)
    except TaskOccurrenceError:
        raise
    except SQLAlchemyError as exc:
        raise TaskOccurrenceUnavailable(
            f"task occurrence list failed: {str(exc).splitlines()[0]}"
        ) from exc


def open_occurrence_for_agent(agent_id: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The one open occurrence for this stable agent, or ``None``.

    Never returns a finalized occurrence. That is the point: a stable agent
    that comes back is a live identity with no task until somebody opens one,
    and returning its last finished task here is how a restored worker would
    silently resume finished work.
    """
    agent_id = _require_uuid(agent_id, field_name="agent_id")

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _open_row_for_agent(session, agent_id)
        return _occurrence_dict(row) if row is not None else None

    try:
        if db is not None:
            return _get(db)
        with database.SessionLocal() as session:
            return _get(session)
    except SQLAlchemyError as exc:
        raise TaskOccurrenceUnavailable(
            f"task occurrence read failed: {str(exc).splitlines()[0]}"
        ) from exc


def occurrence_history(session_name: str, agent_id: str, db: Any = None) -> dict[str, Any]:
    """Finalized history and the live occurrence, kept visibly separate.

    Isolation is the whole reason for the shape: a caller that wants "what is
    this agent doing" must not be handed a list where a finished round and the
    live one look the same.
    """
    records = list_occurrences(session_name, agent_id=agent_id, db=db)
    return {
        "session_name": _normalise_session_name(session_name),
        "agent_id": agent_id,
        "open": next((item for item in records if item["state"] == STATE_OPEN), None),
        "finalized": [item for item in records if item["state"] == STATE_FINALIZED],
    }


def dispatch_digest_for(payload: Mapping[str, Any]) -> str:
    """Digest a dispatch descriptor for provenance.

    Offered so callers do not each invent their own canonicalization and then
    disagree about whether two retries were the same dispatch.
    """
    if not isinstance(payload, Mapping):
        raise TaskOccurrenceInvalid("dispatch payload must be a mapping")
    return digest({"schema": SCHEMA_VERSION, "dispatch": dict(payload)})


def summary_digest_for(text: str) -> str:
    """Digest a supervisor summary without storing it.

    The summary itself lives wherever its owner keeps it; M3-D binds the
    successor to *this* text by digest so a successor cannot be handed a
    different summary than the one the record describes.
    """
    if not isinstance(text, str) or not text:
        raise TaskOccurrenceInvalid("summary text must be a non-empty string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_digest_for(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise TaskOccurrenceInvalid("artifact content must be bytes")
    return hashlib.sha256(bytes(data)).hexdigest()


def seed_from_artifacts(
    *,
    quality: str,
    summary_digest: Optional[str] = None,
    artifacts: Sequence[Mapping[str, Any]] = (),
    produced_by: Optional[str] = None,
    note: Optional[str] = None,
) -> TaskSeed:
    """Build a seed from plain mappings, for API/CLI callers."""
    return TaskSeed(
        quality=quality,
        summary_digest=summary_digest,
        artifacts=tuple(
            ArtifactReference(
                artifact_id=cast(str, item.get("artifact_id")),
                kind=cast(str, item.get("kind")),
                reference=cast(str, item.get("reference")),
                content_digest=cast(str, item.get("content_digest")),
                bytes_len=item.get("bytes_len"),
            )
            for item in artifacts
        ),
        produced_by=produced_by,
        note=note,
    )
