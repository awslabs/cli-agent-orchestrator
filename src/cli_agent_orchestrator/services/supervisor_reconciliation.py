"""What a resumed supervisor is told, and the one time it is told (M3-D).

M3-C guarantees there is *exactly one* wake per Resume and that it comes last,
after every member's outcome is durable. It deliberately owns nothing about
what the wake says. This module owns the content and the delivery, and the two
guarantees it adds are:

**Exactly once, durably.** M3-C's wake id is derived from the Resume operation,
so a retry re-derives it; the ledger row here is keyed by that operation and
records whether the message was actually delivered. A retried Resume adopts the
recorded wake instead of composing and sending a second one. "Exactly once" that
only holds inside one process is not a guarantee at all — the interesting case
is the process that died between sending and recording.

**Exact, not approximate.** The message is rendered deterministically from the
durable cohort record, so a replay is byte-identical and the stored
``message_json`` is what the supervisor actually received. It lists what the
supervisor cannot see for itself: which workers came back on their own
conversation, which came back fresh (and how complete the seed they were given
is), which were interrupted or parked, and which did not come back at all.

**Resume-paused sends nothing.** Not a byte. That is structural in M3-C —
``execute_resume_paused`` has no waker parameter — and this module refuses a
paused target as well, so a caller that wires the waker into the wrong surface
gets a typed refusal rather than a keystroke into a fleet an operator asked to
leave still.

The message never replays a worker's input, never restates a task, and never
tells the supervisor what to do next. It is a description of the fleet's
physical outcome; deciding what that means for the campaign is the supervisor's
job and inventing a next step for it would be M3-D setting goals.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import cohort_resume, control_input_service
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import task_occurrence as occurrence
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    CONTROL_INPUT_PROTOCOL,
)

_T = TypeVar("_T")

SCHEMA_VERSION = "cao-m3d-supervisor-reconciliation-v1"

SOURCE_RESUME_AND_START = "resume-and-start"
SOURCE_PAUSED_TO_WORKING = "paused-to-working"
SOURCE_KINDS = frozenset({SOURCE_RESUME_AND_START, SOURCE_PAUSED_TO_WORKING})

DELIVERY_CLAIMED = "claimed"
DELIVERY_DELIVERED = "delivered"
DELIVERY_UNDELIVERED = "undelivered"
DELIVERY_STATES = frozenset({DELIVERY_CLAIMED, DELIVERY_DELIVERED, DELIVERY_UNDELIVERED})

CATEGORY_EXACT = "restored-exact"
CATEGORY_FRESH = "restored-fresh"
CATEGORY_FAILED = "failed"
CATEGORY_UNRESUMABLE = "unresumable"
CATEGORY_INTERRUPTED = "interrupted"
CATEGORY_PARKED = "parked"
CATEGORY_OTHER = "other"

#: The order the message renders categories in. Fixed, because a message whose
#: sections move around is one an operator cannot diff between two Resumes.
CATEGORY_ORDER = (
    CATEGORY_EXACT,
    CATEGORY_FRESH,
    CATEGORY_INTERRUPTED,
    CATEGORY_PARKED,
    CATEGORY_FAILED,
    CATEGORY_UNRESUMABLE,
    CATEGORY_OTHER,
)

_CATEGORY_FOR_FINAL = {
    cohort.FINAL_RESTORED_EXACT: CATEGORY_EXACT,
    cohort.FINAL_RESTORED_FRESH: CATEGORY_FRESH,
    cohort.FINAL_FAILED: CATEGORY_FAILED,
    cohort.FINAL_UNRESUMABLE: CATEGORY_UNRESUMABLE,
    cohort.FINAL_INTERRUPTED: CATEGORY_INTERRUPTED,
    cohort.FINAL_PARKED: CATEGORY_PARKED,
}

_LABEL = {
    CATEGORY_EXACT: "exact",
    CATEGORY_FRESH: "fresh",
    CATEGORY_INTERRUPTED: "interrupted",
    CATEGORY_PARKED: "parked",
    CATEGORY_FAILED: "failed",
    CATEGORY_UNRESUMABLE: "unresumable",
    CATEGORY_OTHER: "other",
}

#: The categories a supervisor has to act on. They get named workers in the
#: line; the rest get counts, because the line has a hard 512-byte budget and
#: spending it on the workers that came back fine is spending it on nothing.
NAMED_CATEGORIES = (CATEGORY_FRESH, CATEGORY_FAILED, CATEGORY_UNRESUMABLE)

_WAKE_NAMESPACE = uuid.UUID("03800000-d000-4d00-a7e3-000000000003")

#: The control path's hard limit. The renderer fits inside it deterministically
#: rather than discovering it at delivery time, because a message that is
#: refused for length is a supervisor that was told nothing.
MAX_MESSAGE_BYTES = control_input_service.MAX_TEXT_BYTES


class SupervisorReconciliationError(RuntimeError):
    code = "supervisor-reconciliation-error"


class SupervisorReconciliationInvalid(SupervisorReconciliationError):
    code = "supervisor-reconciliation-invalid"


class SupervisorReconciliationConflict(SupervisorReconciliationError):
    code = "supervisor-reconciliation-conflict"


class SupervisorReconciliationNotFound(SupervisorReconciliationError):
    code = "supervisor-reconciliation-not-found"


class SupervisorReconciliationUnavailable(SupervisorReconciliationError):
    code = "supervisor-reconciliation-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def paused_to_working_wake_id(source_operation_id: str) -> str:
    """The wake id for a paused fleet an operator started.

    Derived from the source operation for the same reason M3-C derives its
    Resume wake id: a retried start recomputes it and adopts the delivered
    wake rather than bumping the supervisor twice.
    """
    return str(uuid.uuid5(_WAKE_NAMESPACE, source_operation_id))


@dataclass(frozen=True)
class WorkerOutcome:
    """One worker's physical outcome, as the supervisor will hear it."""

    agent_id: str
    role: str
    category: str
    terminal_id: Optional[str] = None
    harness: Optional[str] = None
    final_state: Optional[str] = None
    seed_quality: Optional[str] = None
    seed_sufficient: Optional[bool] = None

    def payload(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "category": self.category,
            "terminal_id": self.terminal_id,
            "harness": self.harness,
            "final_state": self.final_state,
            "seed_quality": self.seed_quality,
            "seed_sufficient": self.seed_sufficient,
        }

    @property
    def short(self) -> str:
        """A stable short handle: the terminal if there is one, else the agent.

        Terminal ids are what an operator and a supervisor both already use to
        talk about a worker; a bare UUID prefix is a last resort.
        """
        return self.terminal_id or self.agent_id[:8]


def _seed_facts(agent_id: str) -> tuple[Optional[str], Optional[bool]]:
    """The seed a fresh successor for this agent was given, and whether it is enough.

    Read from the agent's own occurrence record rather than recomputed, so the
    supervisor is told the same completeness the recovery path acted on.
    """
    try:
        open_record = occurrence.open_occurrence_for_agent(agent_id)
    except occurrence.TaskOccurrenceError:
        return None, None
    if open_record is None:
        return None, None
    verdict = occurrence.seed_verdict(open_record)
    return verdict.get("quality"), bool(verdict.get("sufficient_for_fresh_start"))


def classify_members(record: Mapping[str, Any]) -> list[WorkerOutcome]:
    """Turn a durable cohort projection into the outcomes the wake reports.

    Excluded members are dropped: they were already dormant at the boundary
    and were never part of this Resume, so naming them would tell the
    supervisor about workers it did not lose.
    """
    outcomes: list[WorkerOutcome] = []
    for member in record.get("members") or ():
        if not member.get("included"):
            continue
        category = _CATEGORY_FOR_FINAL.get(member["final_state"], CATEGORY_OTHER)
        seed_quality: Optional[str] = None
        seed_sufficient: Optional[bool] = None
        if category == CATEGORY_FRESH:
            seed_quality, seed_sufficient = _seed_facts(member["agent_id"])
        outcomes.append(
            WorkerOutcome(
                agent_id=member["agent_id"],
                role=member.get("role") or roster.ROLE_WORKER,
                category=category,
                terminal_id=member.get("terminal_id"),
                harness=member.get("harness"),
                final_state=member.get("final_state"),
                seed_quality=seed_quality,
                seed_sufficient=seed_sufficient,
            )
        )
    return sorted(outcomes, key=lambda item: (CATEGORY_ORDER.index(item.category), item.agent_id))


def render_message(
    *,
    session_name: str,
    source_kind: str,
    operation_id: str,
    resume_target: str,
    outcomes: Sequence[WorkerOutcome],
) -> dict[str, Any]:
    """Render the exact single-line wake and its structured record.

    Deterministic all the way down, including the truncation: the same inputs
    always produce the same bytes, which is what lets a replay prove it is
    re-sending the identical message rather than a new one that happens to
    look similar.
    """
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.category] = counts.get(outcome.category, 0) + 1

    head = (
        f"[CAO reconcile {operation_id[:8]}] {session_name} resumed to {resume_target}"
        f" ({source_kind})."
    )
    tally = " ".join(
        f"{_LABEL[category]}={counts[category]}"
        for category in CATEGORY_ORDER
        if counts.get(category)
    )
    named: list[str] = []
    for category in NAMED_CATEGORIES:
        members = [outcome for outcome in outcomes if outcome.category == category]
        if not members:
            continue
        for outcome in members:
            if category == CATEGORY_FRESH:
                quality = outcome.seed_quality or "unknown"
                named.append(f"{outcome.short}:fresh/seed={quality}")
            else:
                named.append(f"{outcome.short}:{_LABEL[category]}")
    tail = "Reconcile against the durable record before dispatching; do not replay their input."

    parts = [head]
    if tally:
        parts.append(tally + ".")
    truncated = False
    if named:
        detail, truncated = _fit_named(head, tally, named, tail)
        if detail:
            parts.append(detail)
    parts.append(tail)
    text = " ".join(part for part in parts if part)
    return {
        "schema_version": SCHEMA_VERSION,
        "text": text,
        "truncated": truncated,
        "session_name": session_name,
        "source_kind": source_kind,
        "operation_id": operation_id,
        "resume_target": resume_target,
        "counts": counts,
        "outcomes": [outcome.payload() for outcome in outcomes],
    }


def _fit_named(head: str, tally: str, named: Sequence[str], tail: str) -> tuple[str, bool]:
    """Fit as many named workers as the byte budget allows, in order.

    Truncation drops from the end and says so with a marker, so a supervisor
    reading a shortened line knows there is more in the durable record rather
    than believing it saw the whole list.
    """
    fixed = len(head.encode("utf-8")) + 1 + len(tail.encode("utf-8"))
    if tally:
        fixed += len(tally.encode("utf-8")) + 2
    budget = MAX_MESSAGE_BYTES - fixed - 1
    if budget <= 0:
        return "", bool(named)
    kept: list[str] = []
    used = 0
    for item in named:
        candidate = len(item.encode("utf-8")) + (1 if kept else 0)
        # Leave room for the truncation marker if anything will be dropped.
        reserve = 2 if len(kept) + 1 < len(named) else 0
        if used + candidate + reserve > budget:
            break
        kept.append(item)
        used += candidate
    if not kept:
        return "", True
    if len(kept) == len(named):
        return " ".join(kept) + ".", False
    return " ".join(kept) + " +.", True


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "wake_id": row.wake_id,
        "schema_version": row.schema_version,
        "session_name": row.session_name,
        "source_kind": row.source_kind,
        "source_operation_id": row.source_operation_id,
        "supervisor_agent_id": row.supervisor_agent_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "message_digest": row.message_digest,
        "message": occurrence._parse_json(row.message_json),
        "control_id": row.control_id,
        "delivery_state": row.delivery_state,
        "outcome": row.outcome,
        "reason_code": row.reason_code,
        "detail": row.detail,
        "receipt_digest": row.receipt_digest,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _wake_by_source(db: Any, source_operation_id: str) -> Any:
    return (
        db.query(database.SupervisorReconciliationWakeModel)
        .filter(
            database.SupervisorReconciliationWakeModel.source_operation_id == source_operation_id
        )
        .one_or_none()
    )


def _with_session(fn: Callable[[Any], _T], db: Any, *, unavailable: str) -> _T:
    if db is not None:
        try:
            occurrence._anchor_caller_transaction(db)
            with db.begin_nested():
                return fn(db)
        except SupervisorReconciliationError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise SupervisorReconciliationUnavailable(f"{unavailable}: {exc}") from exc
    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = fn(session)
                session.commit()
                return result
        except SupervisorReconciliationError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise SupervisorReconciliationUnavailable(f"{unavailable}: {last_error}")


def _supervisor_target(record: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Where the wake is delivered: the supervisor's *current* incarnation.

    Not the one the cohort snapshot captured. An exact restore deliberately
    brings the same conversation back on a new pane and generation, so
    addressing the pre-stop terminal would type into a pane that no longer
    exists — or, far worse, into whatever inherited its id.
    """
    supervisors = [
        member
        for member in record.get("members") or ()
        if member.get("included") and member.get("role") == roster.ROLE_SUPERVISOR
    ]
    if len(supervisors) != 1:
        return None
    agent_id = supervisors[0]["agent_id"]
    try:
        agent = roster.get_agent(agent_id)
    except roster.StableAgentError:
        return None
    incarnation = agent.get("current_incarnation")
    if (
        not incarnation
        or incarnation.get("disposition") not in roster.LIVE_INCARNATION_DISPOSITIONS
    ):
        return {"agent_id": agent_id, "terminal_id": None, "generation": None, "pane_id": None}
    lineage = agent.get("current_lineage") or {}
    return {
        "agent_id": agent_id,
        "terminal_id": incarnation.get("terminal_id"),
        "generation": incarnation.get("generation"),
        "pane_id": incarnation.get("pane_id"),
        "harness": lineage.get("harness"),
        "native_session_id": lineage.get("native_session_id"),
    }


def _claim_once(
    db: Any,
    *,
    wake_id: str,
    session_name: str,
    source_kind: str,
    source_operation_id: str,
    target: Optional[Mapping[str, Any]],
    message: Mapping[str, Any],
) -> dict[str, Any]:
    existing = _wake_by_source(db, source_operation_id)
    if existing is not None:
        if existing.wake_id != wake_id:
            raise SupervisorReconciliationConflict(
                f"operation {source_operation_id} already has reconciliation wake "
                f"{existing.wake_id}; one operation wakes its supervisor once"
            )
        record = _row_dict(existing)
        record["adopted"] = True
        return record
    stamp = _now()
    digest = occurrence.digest(dict(message))
    row = database.SupervisorReconciliationWakeModel(
        wake_id=wake_id,
        schema_version=SCHEMA_VERSION,
        session_name=session_name,
        source_kind=source_kind,
        source_operation_id=source_operation_id,
        supervisor_agent_id=(target or {}).get("agent_id"),
        terminal_id=(target or {}).get("terminal_id"),
        generation=(target or {}).get("generation"),
        message_digest=digest,
        message_json=occurrence.canonical_json(dict(message)),
        control_id=wake_id,
        delivery_state=DELIVERY_CLAIMED,
        outcome=None,
        reason_code=None,
        detail=None,
        receipt_digest=None,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    record = _row_dict(row)
    record["adopted"] = False
    return record


def _settle_once(
    db: Any,
    *,
    wake_id: str,
    delivery_state: str,
    outcome: Optional[str],
    reason_code: Optional[str],
    detail: Optional[str],
) -> dict[str, Any]:
    row = (
        db.query(database.SupervisorReconciliationWakeModel)
        .filter(database.SupervisorReconciliationWakeModel.wake_id == wake_id)
        .one_or_none()
    )
    if row is None:
        raise SupervisorReconciliationNotFound(f"unknown reconciliation wake: {wake_id}")
    if row.delivery_state == DELIVERY_DELIVERED:
        # Delivered is final. A later failure to *record* the delivery must
        # never downgrade the durable fact that the supervisor was told.
        record = _row_dict(row)
        record["adopted"] = True
        return record
    stamp = _now()
    receipt = (
        occurrence.digest(
            {
                "schema": SCHEMA_VERSION,
                "effect": "supervisor-reconciliation-wake",
                "wake_id": wake_id,
                "message_digest": row.message_digest,
                "outcome": outcome,
            }
        )
        if delivery_state == DELIVERY_DELIVERED
        else None
    )
    result = db.execute(
        sa_update(database.SupervisorReconciliationWakeModel)
        .where(
            database.SupervisorReconciliationWakeModel.wake_id == wake_id,
            database.SupervisorReconciliationWakeModel.delivery_state != DELIVERY_DELIVERED,
        )
        .values(
            delivery_state=delivery_state,
            outcome=outcome,
            reason_code=reason_code,
            detail=(detail or None) and detail[: occurrence.MAX_DETAIL_LEN],
            receipt_digest=receipt,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise SupervisorReconciliationConflict(
            f"reconciliation wake {wake_id} moved concurrently; read the durable record"
        )
    db.flush()
    db.refresh(row)
    record = _row_dict(row)
    record["adopted"] = False
    return record


def get_wake(source_operation_id: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The durable wake for one operation, or ``None`` if none was claimed."""

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _wake_by_source(session, source_operation_id)
        return _row_dict(row) if row is not None else None

    try:
        if db is not None:
            return _get(db)
        with database.SessionLocal() as session:
            return _get(session)
    except SQLAlchemyError as exc:
        raise SupervisorReconciliationUnavailable(
            f"reconciliation wake read failed: {str(exc).splitlines()[0]}"
        ) from exc


def list_wakes(session_name: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    name = None
    if session_name is not None:
        name = sl.normalise_session_name(session_name)

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.SupervisorReconciliationWakeModel)
        if name is not None:
            query = query.filter(database.SupervisorReconciliationWakeModel.session_name == name)
        rows = query.order_by(
            database.SupervisorReconciliationWakeModel.created_at,
            database.SupervisorReconciliationWakeModel.wake_id,
        ).all()
        return [_row_dict(row) for row in rows]

    try:
        if db is not None:
            return _list(db)
        with database.SessionLocal() as session:
            return _list(session)
    except SQLAlchemyError as exc:
        raise SupervisorReconciliationUnavailable(
            f"reconciliation wake list failed: {str(exc).splitlines()[0]}"
        ) from exc


def _deliver(target: Mapping[str, Any], control_id: str, text: str) -> Any:
    expected = {
        "terminal_id": target.get("terminal_id"),
        "terminal_generation": target.get("generation"),
        "pane_birth_id": target.get("pane_id"),
        "provider": target.get("harness"),
        "native_session_id": target.get("native_session_id"),
    }
    return control_input_service.deliver_control_input(
        str(target["terminal_id"]),
        control_id=control_id,
        text=text,
        enter=True,
        expected_identity={key: value for key, value in expected.items() if value is not None},
        protocol=CONTROL_INPUT_PROTOCOL,
    )


def deliver_reconciliation_wake(
    *,
    wake_id: str,
    record: Mapping[str, Any],
    source_kind: str = SOURCE_RESUME_AND_START,
    deliverer: Any = None,
    db: Any = None,
) -> dict[str, Any]:
    """Compose and deliver the one reconciliation wake for this operation.

    The rendered message is a *proposal*: it is what gets persisted the first
    time this operation claims a wake, and it is ignored on every attempt
    after that. The claim is the message. Only the delivery *target* is
    re-resolved per attempt, because the supervisor's pane legitimately moves
    while its words do not.

    Blocking: the delivery path runs tmux subprocesses, so an async caller
    dispatches it to a worker thread. ``deliverer`` is the seam a test or an
    owner replaces; it receives ``(target, control_id, text)`` and returns a
    ``ControlInputResult``.
    """
    if source_kind not in SOURCE_KINDS:
        raise SupervisorReconciliationInvalid(
            f"source_kind must be one of {sorted(SOURCE_KINDS)}; got {source_kind!r}"
        )
    resume_target = record.get("resume_target") or sl.WORKING
    if resume_target == sl.PAUSED:
        raise SupervisorReconciliationInvalid(
            "a Resume-paused sends zero input; there is no reconciliation wake to deliver"
        )
    operation_id = str(record["operation_id"])
    outcomes = classify_members(record)
    message = render_message(
        session_name=str(record["session_name"]),
        source_kind=source_kind,
        operation_id=operation_id,
        resume_target=str(resume_target),
        outcomes=outcomes,
    )
    target = _supervisor_target(record)
    claimed = _with_session(
        lambda session: _claim_once(
            session,
            wake_id=wake_id,
            session_name=str(record["session_name"]),
            source_kind=source_kind,
            source_operation_id=operation_id,
            target=target,
            message=message,
        ),
        db,
        unavailable="concurrent reconciliation wake claims kept conflicting",
    )
    if claimed["delivery_state"] == DELIVERY_DELIVERED:
        # Already told. Adopting rather than re-sending is the whole reason
        # the ledger exists.
        return claimed

    if target is None:
        return _with_session(
            lambda session: _settle_once(
                session,
                wake_id=wake_id,
                delivery_state=DELIVERY_UNDELIVERED,
                outcome=None,
                reason_code="supervisor-not-identified",
                detail=(
                    "the cohort has no single included supervisor; there is nobody to reconcile "
                    "with and the fleet must not be told by guessing"
                ),
            ),
            db,
            unavailable="concurrent reconciliation wake settles kept conflicting",
        )
    if not target.get("terminal_id"):
        return _with_session(
            lambda session: _settle_once(
                session,
                wake_id=wake_id,
                delivery_state=DELIVERY_UNDELIVERED,
                outcome=None,
                reason_code="supervisor-pane-absent",
                detail="the supervisor has no live incarnation to deliver a wake into",
            ),
            db,
            unavailable="concurrent reconciliation wake settles kept conflicting",
        )

    # The *claimed* bytes, never the freshly rendered ones. A wake is claimed
    # under one control id, and the delivering seam's at-most-once contract is
    # keyed on that id — so a resend carrying different text would either be
    # suppressed as a duplicate of a message the supervisor never saw, or land
    # as a second, differently-worded version of the same wake. Live facts move
    # between the claim and the resend (a member re-recorded, a fresh worker's
    # seed appearing); the message must not. What the ledger stores is what was
    # sent, which is the only reason "the supervisor was told X" is checkable.
    claimed_text = str((claimed.get("message") or message)["text"])
    send = deliverer or _deliver
    try:
        result = send(target, claimed["control_id"], claimed_text)
    except Exception as exc:  # noqa: BLE001 - an undelivered wake reconciles
        return _with_session(
            lambda session: _settle_once(
                session,
                wake_id=wake_id,
                delivery_state=DELIVERY_UNDELIVERED,
                outcome=None,
                reason_code="delivery-raised",
                detail=str(exc)[: occurrence.MAX_TEXT_LEN],
            ),
            db,
            unavailable="concurrent reconciliation wake settles kept conflicting",
        )
    delivered = getattr(result, "outcome", None) == ACCEPTED
    return _with_session(
        lambda session: _settle_once(
            session,
            wake_id=wake_id,
            delivery_state=DELIVERY_DELIVERED if delivered else DELIVERY_UNDELIVERED,
            outcome=getattr(result, "outcome", None),
            reason_code=getattr(result, "reason_code", None),
            detail=getattr(result, "detail", None),
        ),
        db,
        unavailable="concurrent reconciliation wake settles kept conflicting",
    )


def make_waker(*, deliverer: Any = None) -> cohort_resume.SupervisorWaker:
    """The M3-D authority M3-C's Resume-and-start asks for.

    M3-C hands over the durable operation, its member results, and the one
    wake id it derived; everything the supervisor is told is decided here. A
    wake that did not land is reported as undelivered, which leaves M3-C's
    Resume in reconciliation — the fleet is back and nobody has been told, and
    that is not a settled campaign.
    """

    async def _wake(
        operation: Mapping[str, Any], results: Sequence[Mapping[str, Any]], identifier: str
    ) -> cohort_resume.SupervisorWake:
        import asyncio

        del results  # the durable projection is the authority, not the caller's copy
        operation_id = operation.get("operation_id")
        if not operation_id:
            # A waker must never raise into the Resume path: an exception here
            # would abort a Resume whose members are already restored. "I could
            # not tell the supervisor" is an outcome; a traceback is not.
            return cohort_resume.SupervisorWake(
                False, detail="the Resume operation carries no id to reconcile against"
            )
        try:
            record = await asyncio.to_thread(cohort.get_operation, str(operation_id))
        except cohort.CohortJournalError as exc:
            return cohort_resume.SupervisorWake(False, detail=str(exc)[: occurrence.MAX_TEXT_LEN])
        try:
            wake = await asyncio.to_thread(
                deliver_reconciliation_wake,
                wake_id=identifier,
                record=record,
                source_kind=SOURCE_RESUME_AND_START,
                deliverer=deliverer,
            )
        except SupervisorReconciliationError as exc:
            return cohort_resume.SupervisorWake(False, detail=str(exc)[: occurrence.MAX_TEXT_LEN])
        return cohort_resume.SupervisorWake(
            wake["delivery_state"] == DELIVERY_DELIVERED,
            receipt_digest=wake["receipt_digest"],
            detail=(wake.get("detail") or wake.get("reason_code") or "")[: occurrence.MAX_TEXT_LEN],
        )

    return _wake


def wake_paused_to_working(
    session_name: str,
    *,
    source_operation_id: str,
    deliverer: Any = None,
    db: Any = None,
) -> dict[str, Any]:
    """The same one-wake contract for a *paused* fleet an operator starts.

    A fleet resumed paused has live panes and a supervisor that was
    deliberately told nothing. Starting it is the moment that changes, and it
    needs exactly the same guarantee: one message, derived id, adopted on
    replay. It reuses the Resume cohort's durable outcomes because those are
    still the true description of how each worker came back.
    """
    record = cohort.get_operation(source_operation_id, db=db)
    if record["operation_kind"] != cohort.KIND_RESUME:
        raise SupervisorReconciliationInvalid(
            f"operation {source_operation_id} is a {record['operation_kind']}; a paused-to-working "
            "wake describes how a Resume brought each worker back"
        )
    if record["session_name"] != sl.normalise_session_name(session_name):
        raise SupervisorReconciliationConflict(
            f"operation {source_operation_id} belongs to session {record['session_name']!r}, "
            f"not {session_name!r}"
        )
    if record["state"] != cohort.STATE_SETTLED:
        raise SupervisorReconciliationConflict(
            f"Resume {source_operation_id} is {record['state']!r}; only a settled Resume "
            "describes a fleet that is actually back"
        )
    lifecycle = sl.describe(session_name)
    if lifecycle.get("lifecycle") != sl.WORKING:
        raise SupervisorReconciliationConflict(
            f"session {session_name} is {lifecycle.get('lifecycle')!r}; a paused-to-working wake "
            "follows the declaration, it does not make it"
        )
    # The stored Resume targeted ``paused``; this wake describes the same
    # fleet now declared working, and says so rather than quoting the old
    # target back at the supervisor.
    return deliver_reconciliation_wake(
        wake_id=paused_to_working_wake_id(source_operation_id),
        record={**record, "resume_target": sl.WORKING},
        source_kind=SOURCE_PAUSED_TO_WORKING,
        deliverer=deliverer,
        db=db,
    )


__all__ = [
    "SOURCE_PAUSED_TO_WORKING",
    "SOURCE_RESUME_AND_START",
    "SupervisorReconciliationConflict",
    "SupervisorReconciliationError",
    "SupervisorReconciliationInvalid",
    "SupervisorReconciliationNotFound",
    "SupervisorReconciliationUnavailable",
    "WorkerOutcome",
    "classify_members",
    "deliver_reconciliation_wake",
    "get_wake",
    "list_wakes",
    "make_waker",
    "paused_to_working_wake_id",
    "render_message",
    "wake_paused_to_working",
]
