"""What a session is doing, declared rather than inferred.

A session had no state because a session had no row.  It was the tmux
session *name*, denormalised onto every terminal and stored nowhere by
itself, so the only way to ask "is this session healthy?" was to look at
its terminals and guess.

That guess is unanswerable.  "No worker has moved in six hours" reads
identically for a campaign that finished, a fleet an operator paused, and
a supervisor that died mid-goal.  Everything downstream inherited the
ambiguity — most consequentially the fire marshal, which cannot be given
autonomous authority while a deliberately quiet session is
indistinguishable from a wedged one.

The states
==========

``working`` · ``pausing`` · ``paused`` · ``complete`` · ``stopped``

``pausing`` is a real state rather than a flag because settling a fleet
takes minutes and the UI must be able to say so.  It is deliberately
**not** a marshal suppressor: a supervisor that never settles a pause is
exactly the unresponsive-supervisor case the marshal exists for, so
``pausing`` carries its own deadline and hands the session back rather
than hiding it.

``complete`` is a **declaration, not a teardown**.  A mistaken
``complete`` that tore the fleet down would destroy the evidence needed
to tell that it was mistaken.

Who declares what
=================

The supervisor owns the transitions that need judgement — ``complete``,
and the flip into ``paused``.  An operator *requests* a pause; only the
supervisor knows whether the work is at a resumable boundary, so only the
supervisor can say the fleet has settled.

A declared state is trusted until explicitly changed.  It does not expire
and carries no heartbeat, deliberately: a continuous liveness check on a
paused supervisor is itself a stall detector, and two systems that can
disagree about one session's health is worse than one system that can be
stale.

Absence means ``working``
=========================

A session with no row is reported as ``working``, not as unknown.  Every
session that has ever run predates this table, and treating those as
undeclared-therefore-quiet would suppress the marshal across the entire
existing fleet on the day it shipped.  The same reasoning makes an
unreadable store report ``working`` with a flag rather than raising: a
session whose state cannot be read must look like one that needs
watching.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import inspect as sa_inspect

from cli_agent_orchestrator.clients import database

logger = logging.getLogger(__name__)

WORKING = "working"
PAUSING = "pausing"
PAUSED = "paused"
COMPLETE = "complete"
STOPPED = "stopped"

LIFECYCLES = frozenset({WORKING, PAUSING, PAUSED, COMPLETE, STOPPED})

#: What a ``stopped`` session returns to.  ``pausing`` is excluded: a
#: pause that had not settled when the session was stopped is not a state
#: worth returning to, and resuming into it would restart a deadline
#: nobody is waiting on any more.
RESTORE_TARGETS = frozenset({WORKING, COMPLETE, PAUSED})

CAMPAIGN = "campaign"
SERVICE = "service"
KINDS = frozenset({CAMPAIGN, SERVICE})

#: The states in which the marshal does not fire.  ``pausing`` is absent
#: on purpose and ``working`` is absent because "working" alone is not
#: evidence of progress — the marshal judges that itself.
MARSHAL_SUPPRESSING = frozenset({PAUSED, COMPLETE, STOPPED})

#: How long a pause may sit unsettled before the session returns to the
#: marshal's domain.  Settling is a fleet-wide conversation and genuinely
#: takes minutes; an hour is long enough that a slow settle is not an
#: incident and short enough that a supervisor that died mid-pause is
#: found the same working day.
DEFAULT_PAUSE_DEADLINE_SECONDS = 3600


class SessionLifecycleError(RuntimeError):
    """Base class for every session-lifecycle failure."""

    code = "session-lifecycle-error"


class SessionLifecycleInvalid(SessionLifecycleError):
    """A supplied value is malformed or not a legal transition."""

    code = "session-lifecycle-invalid"


class SessionLifecycleConflict(SessionLifecycleError):
    """The row moved since it was read, or the transition is refused."""

    code = "session-lifecycle-conflict"


class SessionLifecycleUnavailable(SessionLifecycleError):
    """The store could not be written.

    Reads degrade to ``working``; writes do not degrade at all. Silently
    failing to record a stop would leave an operator believing a fleet was
    collected when it is still running.
    """

    code = "session-lifecycle-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionLifecycleInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def normalise_session_name(value: Any) -> str:
    """The key every entry point must agree on.

    A session created as ``repair`` runs as ``cao-repair``: the launch path
    prepends the prefix after the caller has handed over the bare name, and
    every in-repo caller of ``POST /sessions`` passes it unnormalised.
    Storing declared state under whatever the caller happened to type
    would silently split one session across two rows, and both defences in
    this subsystem read only one of them.

    Concretely, without this a stop on ``repair`` writes a row the boot
    reconcile never finds when it looks up ``cao-repair``, so it collects
    the forwarded env of the very session that was hibernated — the exact
    failure ``reconcile_session_env``'s exemption exists to prevent.
    """
    from cli_agent_orchestrator.constants import SESSION_PREFIX

    name = _require_text(value, field="session_name")
    return name if name.startswith(SESSION_PREFIX) else f"{SESSION_PREFIX}{name}"


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "session_name": row.session_name,
        "lifecycle": row.lifecycle,
        "restore_to": row.restore_to,
        "archived": bool(row.archived),
        "kind": row.kind,
        "declared_by": row.declared_by,
        "note": row.note,
        "pause_requested_at": row.pause_requested_at,
        "pause_deadline_at": row.pause_deadline_at,
        "epoch": row.epoch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "declared": True,
    }


def _undeclared(session_name: str) -> dict[str, Any]:
    """The answer for a session nobody has declared anything about.

    ``working`` rather than unknown, and ``declared: False`` so a reader
    that cares about the difference can still tell. Every session that
    predates this table lands here, which is why the default must be the
    one that keeps the marshal watching.
    """
    return {
        "session_name": session_name,
        "lifecycle": WORKING,
        "restore_to": None,
        "archived": False,
        "kind": CAMPAIGN,
        "declared_by": None,
        "note": None,
        "pause_requested_at": None,
        "pause_deadline_at": None,
        "epoch": 0,
        "created_at": None,
        "updated_at": None,
        "declared": False,
    }


def _table_present(db: Any) -> bool:
    return sa_inspect(db.get_bind()).has_table(database.SessionLifecycleModel.__tablename__)


def _fetch(db: Any, session_name: str) -> Any:
    return (
        db.query(database.SessionLifecycleModel)
        .filter(database.SessionLifecycleModel.session_name == session_name)
        .one_or_none()
    )


def describe(session_name: str) -> dict[str, Any]:
    """The declared state of one session, defaulting to ``working``.

    Never raises for an ordinary read failure. The marshal calls this to
    decide whether to stay silent, and an exception there would either
    crash the wake or — worse, if caught carelessly — be read as "nothing
    declared, therefore suppressed". Both are wrong; a session whose state
    cannot be read must look like a session that needs watching.
    """
    session_name = normalise_session_name(session_name)
    try:
        with database.SessionLocal() as db:
            if not _table_present(db):
                return _undeclared(session_name)
            row = _fetch(db, session_name)
            return _row_dict(row) if row is not None else _undeclared(session_name)
    except Exception as exc:  # noqa: BLE001 - a read must not break a caller
        logger.warning("Session lifecycle unreadable for %s: %s", session_name, exc)
        record = _undeclared(session_name)
        record["unreadable"] = str(exc)
        return record


def divergence(record: Mapping[str, Any]) -> Optional[str]:
    """Whether a declaration contradicts the fleet it describes.

    A declared state is trusted until explicitly changed — that is the
    design, and it is why nothing here heartbeats. But trust is not the
    same as silence: a session declared ``stopped`` whose panes are still
    running is marshal-suppressed while burning quota, and the record
    being *visible* does not make it *visibly wrong*.

    So this reports the contradiction without resolving it. Nothing
    auto-corrects the declaration, because the whole point is that only a
    human or a supervisor may change it — but every surface that renders
    the state can now say the two disagree.
    """
    lifecycle = record.get("lifecycle")
    if lifecycle not in (STOPPED, COMPLETE):
        return None
    try:
        from cli_agent_orchestrator.services import terminal_projection

        live = terminal_projection.live_terminals(record["session_name"])
    except Exception:  # noqa: BLE001 - an unreadable fleet is not a contradiction
        return None
    if not live:
        return None
    return (
        f"declared {lifecycle} but {len(live)} terminal(s) are still live; "
        "the panes were never collected, and this session is suppressed to the fire marshal "
        "while they keep running"
    )


def suppresses_marshal(session_name: str) -> tuple[bool, dict[str, Any]]:
    """Whether the marshal should stay silent, and the record that decided.

    Returns the record alongside the verdict so the caller can put it in
    its evidence rather than re-reading. An unreadable store returns
    ``False`` with ``unreadable`` set — the marshal is report-only, so a
    false alarm costs one investigation while a missed deadlock costs
    days, and silence is indistinguishable from health.
    """
    record = describe(session_name)
    if record.get("unreadable"):
        return False, record
    return record["lifecycle"] in MARSHAL_SUPPRESSING, record


def list_sessions(
    *,
    lifecycles: Optional[frozenset[str]] = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Every declared session, newest declaration first.

    Only returns rows that exist. A caller wanting "every tmux session
    plus its state" joins this against the session service, because this
    table deliberately knows nothing about which sessions are live — that
    is precisely the fact it is not allowed to infer.
    """
    if lifecycles is not None:
        unknown = sorted(set(lifecycles) - LIFECYCLES)
        if unknown:
            raise SessionLifecycleInvalid(
                f"unknown lifecycle(s) {unknown}; expected a subset of {sorted(LIFECYCLES)}"
            )
    try:
        with database.SessionLocal() as db:
            if not _table_present(db):
                return []
            query = db.query(database.SessionLifecycleModel)
            if lifecycles is not None:
                query = query.filter(
                    database.SessionLifecycleModel.lifecycle.in_(sorted(lifecycles))
                )
            if not include_archived:
                query = query.filter(database.SessionLifecycleModel.archived == 0)
            rows = query.order_by(database.SessionLifecycleModel.updated_at.desc()).all()
            return [_row_dict(row) for row in rows]
    except SessionLifecycleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SessionLifecycleUnavailable(f"session lifecycle listing failed: {exc}") from exc


def _write(
    session_name: str,
    *,
    expected_epoch: Optional[int],
    values: Mapping[str, Any],
    derive=None,
) -> dict[str, Any]:
    """Upsert one session's state under a compare-and-swap on epoch.

    ``expected_epoch`` is optional because most transitions are made by
    somebody holding the only opinion that matters — an operator pressing
    a button, a supervisor declaring its own fleet settled. It is honoured
    when supplied so a caller that read, decided, and then wrote cannot
    overwrite a decision made in between.

    ``derive`` is handed the record as it stands and returns extra values
    to merge. It exists because several transitions depend on the state
    they are replacing — a stop must record what it would restore to — and
    that state is only known here, inside the read that the write is
    compared against. An earlier version had the caller build those values
    before calling in, which silently wrote nothing: the dict was
    evaluated before the read that was supposed to inform it.
    """
    stamp = _now()
    from cli_agent_orchestrator.services.callback_recovery import (
        session_lifecycle_write_claim,
    )

    try:
        # cond-0221: take the per-session lifecycle write claim so this
        # read-then-CAS cannot interleave with another mutation, and — via the
        # re-entrant thread-local — so a ``stop_session`` that already holds it
        # across collection does not block its own ``_write``. Reads never take
        # it; the module stays backend- and tmux-free.
        with session_lifecycle_write_claim(session_name), database.SessionLocal() as db:
            if not _table_present(db):
                database.SessionLifecycleModel.__table__.create(bind=db.get_bind())
            row = _fetch(db, session_name)

            if row is None:
                if expected_epoch not in (None, 0):
                    raise SessionLifecycleConflict(
                        f"session {session_name} has no declared state, so it cannot be at "
                        f"epoch {expected_epoch}"
                    )
                values = {**values, **(derive(_undeclared(session_name)) or {} if derive else {})}
                db.add(
                    database.SessionLifecycleModel(
                        session_name=session_name,
                        lifecycle=values.get("lifecycle", WORKING),
                        restore_to=values.get("restore_to"),
                        archived=int(bool(values.get("archived", False))),
                        kind=values.get("kind", CAMPAIGN),
                        declared_by=values.get("declared_by"),
                        note=values.get("note"),
                        pause_requested_at=values.get("pause_requested_at"),
                        pause_deadline_at=values.get("pause_deadline_at"),
                        epoch=1,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                db.commit()
                return _row_dict(_fetch(db, session_name))

            if derive is not None:
                values = {**values, **(derive(_row_dict(row)) or {})}
            observed = row.epoch
            if expected_epoch is not None and observed != expected_epoch:
                raise SessionLifecycleConflict(
                    f"session {session_name} moved to epoch {observed} since it was read at "
                    f"epoch {expected_epoch}; the declaration describes a state it no longer has"
                )

            payload = dict(values)
            if "archived" in payload:
                payload["archived"] = int(bool(payload["archived"]))
            payload["epoch"] = observed + 1
            payload["updated_at"] = stamp
            updated = (
                db.query(database.SessionLifecycleModel)
                .filter(
                    database.SessionLifecycleModel.session_name == session_name,
                    database.SessionLifecycleModel.epoch == observed,
                )
                .update(payload, synchronize_session=False)
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, session_name)
                raise SessionLifecycleConflict(
                    f"concurrent modification of session {session_name}; expected epoch "
                    f"{observed}, now {current.epoch if current else 'gone'}"
                )
            return _row_dict(_fetch(db, session_name))
    except SessionLifecycleError:
        raise
    except Exception as exc:  # noqa: BLE001 - a write never degrades
        raise SessionLifecycleUnavailable(f"session lifecycle write failed: {exc}") from exc


def declare(
    session_name: str,
    lifecycle: str,
    *,
    declared_by: str,
    note: Optional[str] = None,
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Record a straightforward state, with the name of whoever decided.

    Refuses ``stopped`` — stopping collects panes and must record what it
    would restore to, so it has its own entry point rather than being a
    value somebody can pass here by accident.

    Also refuses to *leave* ``stopped``: a stopped session's panes are
    collected, so declaring it ``working``/``complete`` would leave a
    collected fleet declared live. Reviving a stopped session is resume's
    job — it relaunches the panes first — not a bare declaration's. This is
    the typed refusal that makes a declare racing a ``stop_session`` (cond-0221
    P1) wait-and-conflict rather than overwrite the stop.
    """
    session_name = normalise_session_name(session_name)
    declared_by = _require_text(declared_by, field="declared_by")
    if lifecycle not in LIFECYCLES:
        raise SessionLifecycleInvalid(
            f"lifecycle must be one of {sorted(LIFECYCLES)}; got {lifecycle!r}"
        )
    if lifecycle == STOPPED:
        raise SessionLifecycleInvalid(
            "use stop() to record a stopped session; it must capture what a resume would "
            "restore to, and a bare declaration cannot"
        )

    def _derive(record: Mapping[str, Any]) -> dict[str, Any]:
        if record["lifecycle"] == STOPPED:
            raise SessionLifecycleConflict(
                f"session {session_name} is stopped; its panes have been collected, so it "
                f"cannot be declared {lifecycle!r}. Resume it (which relaunches the fleet) "
                "or delete it to release the name."
            )
        return {}

    values: dict[str, Any] = {
        "lifecycle": lifecycle,
        "declared_by": declared_by,
        "note": note,
    }
    if lifecycle != PAUSING:
        values["pause_requested_at"] = None
        values["pause_deadline_at"] = None
    return _write(session_name, expected_epoch=expected_epoch, derive=_derive, values=values)


def request_pause(
    session_name: str,
    *,
    requested_by: str,
    deadline_seconds: int = DEFAULT_PAUSE_DEADLINE_SECONDS,
    note: Optional[str] = None,
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Ask for a pause. Does not grant one.

    The operator's half of the protocol. The session enters ``pausing``
    immediately so the UI can disable the button — settling takes minutes,
    and a button that still looks pressable invites a second press — but
    only the supervisor can say the fleet actually settled.

    The deadline is recorded here rather than checked here. Nothing in
    this module expires it; ``pausing`` simply never suppressed the
    marshal, so an unsettled pause is already visible to the thing whose
    job it is to notice.
    """
    session_name = normalise_session_name(session_name)
    requested_by = _require_text(requested_by, field="requested_by")
    if not isinstance(deadline_seconds, int) or deadline_seconds <= 0:
        raise SessionLifecycleInvalid(
            f"deadline_seconds must be a positive integer; got {deadline_seconds!r}"
        )
    requested_at = datetime.now(timezone.utc)

    def _derive(record: Mapping[str, Any]) -> dict[str, Any]:
        if record["lifecycle"] in (COMPLETE, STOPPED):
            raise SessionLifecycleConflict(
                f"session {session_name} is {record['lifecycle']!r}; there is no running fleet "
                "to settle"
            )
        if record["lifecycle"] == PAUSING and record.get("pause_deadline_at"):
            # A re-request keeps the ORIGINAL deadline. Restarting it would
            # make the bound defeatable by retry — an operator pressing the
            # button again, or a UI that re-sends, would postpone the moment
            # the session goes back to the marshal indefinitely.
            return {
                "pause_requested_at": record["pause_requested_at"],
                "pause_deadline_at": record["pause_deadline_at"],
            }
        return {}

    return _write(
        session_name,
        expected_epoch=expected_epoch,
        derive=_derive,
        values={
            "lifecycle": PAUSING,
            "declared_by": requested_by,
            "note": note,
            "pause_requested_at": requested_at.isoformat().replace("+00:00", "Z"),
            "pause_deadline_at": (requested_at + timedelta(seconds=deadline_seconds))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )


def settle_pause(
    session_name: str,
    *,
    declared_by: str,
    note: Optional[str] = None,
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """The supervisor's half: the fleet is settled, the session is paused.

    Refused from anything but ``pausing``. A supervisor announcing a
    settled fleet for a session nobody asked to pause is either confused
    or answering a request that was already cancelled, and both should be
    visible rather than absorbed.
    """
    session_name = normalise_session_name(session_name)
    declared_by = _require_text(declared_by, field="declared_by")

    def _derive(record: Mapping[str, Any]) -> dict[str, Any]:
        if record["lifecycle"] != PAUSING:
            raise SessionLifecycleConflict(
                f"session {session_name} is {record['lifecycle']!r}, not {PAUSING!r}; a pause "
                "can only be settled after it was requested"
            )
        return {}

    return _write(
        session_name,
        expected_epoch=expected_epoch,
        derive=_derive,
        values={
            "lifecycle": PAUSED,
            "declared_by": declared_by,
            "note": note,
            "pause_requested_at": None,
            "pause_deadline_at": None,
        },
    )


def pause_is_overdue(record: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    """Whether a pending pause has outlived its deadline.

    A pure predicate over a record, so the marshal and the UI can ask the
    same question without either of them writing. Nothing flips a state on
    expiry: the session was never suppressed while ``pausing``, so there
    is no suppression to withdraw — the deadline exists to tell an
    investigator that the pause request is itself the evidence.
    """
    if record.get("lifecycle") != PAUSING:
        return False
    deadline = record.get("pause_deadline_at")
    if not isinstance(deadline, str):
        # A `pausing` row with no readable deadline is already wrong, and
        # the two ways to be wrong are not symmetric: reporting it not-overdue
        # keeps the wedge flag muted forever, which is the failure this
        # deadline exists to bound. Overdue is the safe reading.
        return True
    try:
        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:  # pragma: no cover - written with an explicit Z
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) > parsed


def stop(
    session_name: str,
    *,
    declared_by: str,
    restore_to: Optional[str] = None,
    note: Optional[str] = None,
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Record that a session's panes have been collected.

    ``restore_to`` defaults to the state the session was in, because that
    is the only moment the fact is known. It is recorded even though no
    provider can be resumed today: throwing it away would mean a session
    stopped this week could not be brought back correctly once resume
    lands, and the cost of keeping it is one column.

    A pending pause is deliberately not preserved. ``pausing`` is not a
    restore target — resuming into it would restart a deadline nobody is
    waiting on — so a session stopped mid-settle restores to ``working``.
    """
    session_name = normalise_session_name(session_name)
    declared_by = _require_text(declared_by, field="declared_by")
    if restore_to is not None and restore_to not in RESTORE_TARGETS:
        raise SessionLifecycleInvalid(
            f"restore_to must be one of {sorted(RESTORE_TARGETS)}; got {restore_to!r}"
        )

    def _derive(record: Mapping[str, Any]) -> dict[str, Any]:
        if restore_to is not None:
            return {"restore_to": restore_to}
        current = record["lifecycle"]
        if current == STOPPED:
            # Re-stopping keeps the original target rather than collapsing
            # it to `stopped`, which is not a thing to restore to.
            return {"restore_to": record.get("restore_to") or WORKING}
        return {"restore_to": current if current in RESTORE_TARGETS else WORKING}

    return _write(
        session_name,
        expected_epoch=expected_epoch,
        derive=_derive,
        values={
            "lifecycle": STOPPED,
            "declared_by": declared_by,
            "note": note,
            "pause_requested_at": None,
            "pause_deadline_at": None,
        },
    )


def set_archived(
    session_name: str,
    archived: bool,
    *,
    declared_by: str,
    note: Optional[str] = None,
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Hide or unhide a session, without losing what it was.

    Archiving is a visibility flag rather than a state precisely so that
    archiving a *complete* session does not lose the fact that it was
    complete. It forces a stop, recording the pre-stop lifecycle as
    ``restore_to``, so an unarchived session comes back as what it was.
    """
    session_name = normalise_session_name(session_name)
    declared_by = _require_text(declared_by, field="declared_by")
    if not archived:
        return _write(
            session_name,
            expected_epoch=expected_epoch,
            values={"archived": False, "declared_by": declared_by, "note": note},
        )

    def _derive(record: Mapping[str, Any]) -> dict[str, Any]:
        current = record["lifecycle"]
        if current == STOPPED:
            return {"restore_to": record.get("restore_to") or WORKING}
        return {"restore_to": current if current in RESTORE_TARGETS else WORKING}

    return _write(
        session_name,
        expected_epoch=expected_epoch,
        derive=_derive,
        values={
            "archived": True,
            "lifecycle": STOPPED,
            "declared_by": declared_by,
            "note": note,
            "pause_requested_at": None,
            "pause_deadline_at": None,
        },
    )


def set_kind(
    session_name: str,
    kind: str,
    *,
    declared_by: str,
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Declare how this session's health should be judged at all.

    A ``service`` session — a long-lived memory curator accepting messages
    from several campaigns — must never be measured by campaign criteria.
    "No work item has advanced in six hours" is a stall for a campaign and
    the normal condition for a curator, and a health model that cannot
    tell them apart produces a false alarm every six hours forever.
    """
    session_name = normalise_session_name(session_name)
    declared_by = _require_text(declared_by, field="declared_by")
    if kind not in KINDS:
        raise SessionLifecycleInvalid(f"kind must be one of {sorted(KINDS)}; got {kind!r}")
    return _write(
        session_name,
        expected_epoch=expected_epoch,
        values={"kind": kind, "declared_by": declared_by},
    )


def forget(session_name: str) -> bool:
    """Drop a session's declared state entirely.

    For a session being deleted, so a later session reusing the name does
    not inherit a stranger's declaration. Returns whether a row was
    removed.
    """
    session_name = normalise_session_name(session_name)
    from cli_agent_orchestrator.services.callback_recovery import (
        session_lifecycle_write_claim,
    )

    try:
        with session_lifecycle_write_claim(session_name), database.SessionLocal() as db:
            if not _table_present(db):
                return False
            removed = (
                db.query(database.SessionLifecycleModel)
                .filter(database.SessionLifecycleModel.session_name == session_name)
                .delete(synchronize_session=False)
            )
            db.commit()
            return bool(removed)
    except Exception as exc:  # noqa: BLE001
        raise SessionLifecycleUnavailable(f"session lifecycle delete failed: {exc}") from exc
