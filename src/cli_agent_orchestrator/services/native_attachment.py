"""Exclusive, crash-safe ownership of one provider-native session.

A provider session (a Kimi session id, say) is a single-writer resource:
two processes attached to it at once interleave turns, and neither can
tell that it happened.  Nothing downstream detects the damage, because
each attachment's own receipts look perfectly consistent.

This module is the only way to acquire one.  Attachments are keyed by
``(provider, native_session_id)`` — the provider's own identity, never a
CAO-side name — and held by exactly one owner
``(terminal_id, generation, execution_mode, pane_id, process_identity)``.

``execution_mode`` is part of the owner precisely so an ACP bridge and a
native TUI can never both hold one provider session: the second attach
sees a live owner and is refused rather than silently multiplexed.

State machine::

    declared -> starting -> attached -> draining -> detached
        |          |           |           |
        +----------+-----------+-----------+--> ambiguous  (frozen)

- **Intent is journaled before provider launch.**  ``declare`` writes the
  row *before* the provider process exists, so a crash at any later point
  leaves a durable claim that recovery must adjudicate.  The reverse
  order — launch, then record — loses the session id on a crash and
  leaves an unattributable live process holding it forever.
- **Release requires an exact no-survivor proof.**  A caller cannot
  release by asserting it is finished; it must present the exact owner,
  the exact published process identity, and an *empty, present* survivor
  observation.  Releasing on a hopeful "probably dead" is what lets a
  survivor and its replacement write to one session.
- **Ambiguity freezes and never releases.**  When ownership cannot be
  resolved, the owner is preserved and the row becomes terminal for
  automation.  A human resolves it.  Auto-releasing an ambiguous row is
  exactly the double-attach this module exists to prevent.

Every transition is a compare-and-swap on ``(key, epoch, exact owner)``
and bumps ``epoch``, so a lost update is refused rather than silently
winning last-write.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, cast

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em

#: Attachment states.  ``detached`` and ``ambiguous`` are the two ways an
#: attachment ends; only ``detached`` permits a later re-acquire.
DECLARED = "declared"
STARTING = "starting"
ATTACHED = "attached"
DRAINING = "draining"
DETACHED = "detached"
AMBIGUOUS = "ambiguous"

#: States in which an owner still holds the provider session.  A second
#: acquirer seeing any of these is refused.
LIVE_STATES = frozenset({DECLARED, STARTING, ATTACHED, DRAINING})
ATTACHMENT_STATES = frozenset(LIVE_STATES | {DETACHED, AMBIGUOUS})

#: How a native session id was obtained.  Closed, because each value
#: carries different obligations that ``declare`` validates.
ACQUISITION_ACP_BOOTSTRAP = "zero_prompt_acp_bootstrap"
ACQUISITION_RESUME = "pinned_resume"
ACQUISITION_METHODS = frozenset({ACQUISITION_ACP_BOOTSTRAP, ACQUISITION_RESUME})

INTENT_SCHEMA = "cao-native-attachment-intent-v1"
NO_SURVIVOR_PROOF_SCHEMA = "cao-native-attachment-no-survivor-v1"


class NativeAttachmentError(RuntimeError):
    """Base class for every native-attachment failure."""

    code = "native-attachment-error"


class NativeAttachmentInvalid(NativeAttachmentError):
    """A supplied value is malformed or an obligation is unmet."""

    code = "native-attachment-invalid"


class NativeAttachmentConflict(NativeAttachmentError):
    """Another owner holds the session, or the row is frozen ambiguous."""

    code = "native-attachment-conflict"


class NativeAttachmentNotFound(NativeAttachmentError):
    """No attachment exists for the requested key."""

    code = "native-attachment-not-found"


class NativeAttachmentUnavailable(NativeAttachmentError):
    """The attachment store could not be read or written.

    Raised rather than degraded: a native launch that cannot record
    exclusive ownership must never proceed to attach a provider session.
    """

    code = "native-attachment-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeAttachmentInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):  # pragma: no cover - written only by this module
        return None


def process_identity(*, pid: int, start_marker: str) -> dict[str, Any]:
    """Build the identity of one owning OS process.

    A bare pid is **not** identity.  Pids are recycled, so a stale pid can
    match an unrelated live process and forge a survivor — or, worse,
    match nothing and forge a *no*-survivor.  ``start_marker`` is the
    caller's process-start observation (the start timestamp from ``ps``,
    for instance); this module requires one and never invents it, because
    a marker it synthesized would prove nothing about the process.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise NativeAttachmentInvalid(f"pid must be a positive integer; got {pid!r}")
    return {"pid": pid, "start_marker": _require_text(start_marker, field="start_marker")}


def acquire_intent(
    *,
    acquisition_method: str,
    acquisition_receipt: Mapping[str, Any],
    admits_only_new_instructions: bool,
    replays_task_bytes: bool,
    bootstrap_sent_no_turn: Optional[bool] = None,
    bootstrap_detached_before_launch: Optional[bool] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Build the intent journaled *before* the provider is launched.

    The intent is not a description of what happened; it is the set of
    obligations the caller asserts and this module validates.  They are
    fields rather than comments so a caller that cannot truthfully assert
    them cannot declare an attachment at all.

    Both acquisition methods must assert ``admits_only_new_instructions``
    and ``not replays_task_bytes``: a resumed session already contains
    the old task, and re-sending those bytes runs the work twice inside a
    session whose transcript makes it look like one run.

    ``zero_prompt_acp_bootstrap`` must additionally assert that the
    bootstrap sent no task or turn and fully detached before the native
    launch.  A bootstrap that overlaps the native TUI is itself the
    double-attach this module prevents, and it would be holding the very
    session about to be claimed.
    """
    if acquisition_method not in ACQUISITION_METHODS:
        raise NativeAttachmentInvalid(
            f"acquisition_method must be one of {sorted(ACQUISITION_METHODS)}; "
            f"got {acquisition_method!r}"
        )
    if not isinstance(acquisition_receipt, Mapping) or not acquisition_receipt:
        raise NativeAttachmentInvalid("acquisition_receipt must be a non-empty mapping")
    if admits_only_new_instructions is not True:
        raise NativeAttachmentInvalid(
            "intent must assert admits_only_new_instructions=True; an attachment that "
            "may re-admit old instructions cannot be journaled"
        )
    if replays_task_bytes is not False:
        raise NativeAttachmentInvalid(
            "intent must assert replays_task_bytes=False; a resumed session already "
            "contains the original task and must never be re-sent it"
        )

    intent: dict[str, Any] = {
        "schema": INTENT_SCHEMA,
        "acquisition_method": acquisition_method,
        "acquisition_receipt": dict(acquisition_receipt),
        "admits_only_new_instructions": True,
        "replays_task_bytes": False,
    }

    if acquisition_method == ACQUISITION_ACP_BOOTSTRAP:
        if bootstrap_sent_no_turn is not True or bootstrap_detached_before_launch is not True:
            raise NativeAttachmentInvalid(
                "a zero-prompt ACP bootstrap must assert bootstrap_sent_no_turn=True and "
                "bootstrap_detached_before_launch=True; a bootstrap that sent a turn or "
                "still holds the session cannot hand it to a native TUI"
            )
        intent["bootstrap_sent_no_turn"] = True
        intent["bootstrap_detached_before_launch"] = True
    elif bootstrap_sent_no_turn is not None or bootstrap_detached_before_launch is not None:
        raise NativeAttachmentInvalid(
            f"bootstrap assertions are meaningless for {acquisition_method!r} and are refused "
            "rather than recorded as unverified evidence"
        )

    if note is not None:
        intent["note"] = _require_text(note, field="note")
    return intent


def no_survivor_proof(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    pane_id: Optional[str],
    process_identity: Optional[Mapping[str, Any]],
    survivors: list[Any],
    observed_at: str,
    observer: str,
) -> dict[str, Any]:
    """Build the proof that permits exactly one release.

    ``survivors`` is required and must be empty.  It is a *present,
    empty observation* rather than an omitted field on purpose: an absent
    key cannot be distinguished from an observation that was never made,
    and "we did not look" must never read as "nothing is there".

    An observer that cannot produce this — because the process tree was
    unreadable, or a candidate could not be excluded — must call
    :func:`mark_ambiguous` instead of weakening the proof.
    """
    if not isinstance(survivors, list):
        raise NativeAttachmentInvalid("survivors must be a list (present and empty to release)")
    return {
        "schema": NO_SURVIVOR_PROOF_SCHEMA,
        "provider": _require_text(provider, field="provider"),
        "native_session_id": _require_text(native_session_id, field="native_session_id"),
        "terminal_id": _require_text(terminal_id, field="terminal_id"),
        "generation": _require_text(generation, field="generation"),
        "execution_mode": em.validate_mode(execution_mode),
        "pane_id": pane_id,
        "process_identity": dict(process_identity) if process_identity is not None else None,
        "survivors": list(survivors),
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
    }


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "native_session_id": row.native_session_id,
        "state": row.state,
        "owner": {
            "terminal_id": row.owner_terminal_id,
            "generation": row.owner_generation,
            "execution_mode": row.owner_execution_mode,
            "pane_id": row.owner_pane_id,
            "process_identity": _parse_json(row.owner_process_identity_json),
        },
        "intent": _parse_json(row.intent_json),
        "release_proof": _parse_json(row.release_proof_json),
        "ambiguity_reason": row.ambiguity_reason,
        "epoch": row.epoch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _fetch(db: Any, provider: str, native_session_id: str) -> Any:
    return (
        db.query(database.NativeSessionAttachmentModel)
        .filter(
            database.NativeSessionAttachmentModel.provider == provider,
            database.NativeSessionAttachmentModel.native_session_id == native_session_id,
        )
        .one_or_none()
    )


def _describe_owner(row: Any) -> str:
    return (
        f"terminal={row.owner_terminal_id} generation={row.owner_generation} "
        f"mode={row.owner_execution_mode}"
    )


def _refuse_live_owner(row: Any, *, terminal_id: str, generation: str, mode: str) -> None:
    """Raise the conflict for a second acquirer, naming a mode crossing."""
    if row.owner_execution_mode != mode:
        raise NativeAttachmentConflict(
            f"{row.provider} session {row.native_session_id} is held in "
            f"{row.owner_execution_mode!r} mode by {_describe_owner(row)}; a {mode!r} attachment "
            "is refused — ACP and the native TUI never attach to one provider session"
        )
    raise NativeAttachmentConflict(
        f"{row.provider} session {row.native_session_id} is already attached by "
        f"{_describe_owner(row)} (state {row.state!r}); "
        f"terminal={terminal_id} generation={generation} is refused"
    )


def _assert_owner_matches(row: Any, *, terminal_id: str, generation: str, mode: str) -> None:
    if (
        row.owner_terminal_id != terminal_id
        or row.owner_generation != generation
        or row.owner_execution_mode != mode
    ):
        raise NativeAttachmentConflict(
            f"operation owner (terminal={terminal_id} generation={generation} mode={mode}) "
            f"does not hold {row.provider} session {row.native_session_id}; "
            f"held by {_describe_owner(row)}"
        )


def _guard_frozen(row: Any) -> None:
    if row.state == AMBIGUOUS:
        raise NativeAttachmentConflict(
            f"{row.provider} session {row.native_session_id} is frozen ambiguous "
            f"({row.ambiguity_reason!r}); it is terminal for automation and is never "
            "auto-released — a human must resolve the ownership"
        )


def declare(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    intent: Mapping[str, Any],
    pane_id: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    """Claim the session by CAS, journaling intent before provider launch.

    Returns ``(record, acquired)``.  ``acquired`` is ``True`` only for the
    call that took the claim; a repeat by the same owner — the crash-replay
    case, where the caller lost its memory but not its identity — returns
    the current row untouched and ``False`` rather than regressing state.

    A live claim held by anyone else is refused.  A frozen ambiguous row
    is refused permanently.  Only a ``detached`` row is re-acquirable, and
    that path preserves the prior release proof as evidence.
    """
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    terminal_id = _require_text(terminal_id, field="terminal_id")
    generation = _require_text(generation, field="generation")
    mode = em.validate_mode(execution_mode)
    if not isinstance(intent, Mapping) or intent.get("schema") != INTENT_SCHEMA:
        raise NativeAttachmentInvalid(
            f"intent must be built by acquire_intent (schema {INTENT_SCHEMA!r}); "
            "an unvalidated intent cannot be journaled"
        )
    if pane_id is not None:
        pane_id = _require_text(pane_id, field="pane_id")
    intent_json = _canonical(dict(intent))
    stamp = _now()

    try:
        with database.SessionLocal() as db:
            row = _fetch(db, provider, native_session_id)

            if row is None:
                db.add(
                    database.NativeSessionAttachmentModel(
                        provider=provider,
                        native_session_id=native_session_id,
                        state=DECLARED,
                        owner_terminal_id=terminal_id,
                        owner_generation=generation,
                        owner_execution_mode=mode,
                        owner_pane_id=pane_id,
                        owner_process_identity_json=None,
                        intent_json=intent_json,
                        release_proof_json=None,
                        ambiguity_reason=None,
                        epoch=0,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                try:
                    db.commit()
                except IntegrityError:
                    # Another acquirer inserted the same key between the read
                    # and this commit.  The primary key is the arbiter, so the
                    # loser re-reads and takes the ordinary refusal path rather
                    # than reporting the store as broken.
                    db.rollback()
                    row = _fetch(db, provider, native_session_id)
                    if row is None:  # pragma: no cover - key exists by construction
                        raise
                else:
                    return _row_dict(_fetch(db, provider, native_session_id)), True

            _guard_frozen(row)

            if row.state in LIVE_STATES:
                if (
                    row.owner_terminal_id == terminal_id
                    and row.owner_generation == generation
                    and row.owner_execution_mode == mode
                ):
                    return _row_dict(row), False
                _refuse_live_owner(row, terminal_id=terminal_id, generation=generation, mode=mode)

            # Detached: re-acquirable, but only by winning the CAS on the
            # exact observed epoch, so a concurrent re-acquire loses
            # visibly instead of overwriting the winner's ownership.
            observed_epoch = row.epoch
            updated = (
                db.query(database.NativeSessionAttachmentModel)
                .filter(
                    database.NativeSessionAttachmentModel.provider == provider,
                    database.NativeSessionAttachmentModel.native_session_id == native_session_id,
                    database.NativeSessionAttachmentModel.state == DETACHED,
                    database.NativeSessionAttachmentModel.epoch == observed_epoch,
                )
                .update(
                    {
                        "state": DECLARED,
                        "owner_terminal_id": terminal_id,
                        "owner_generation": generation,
                        "owner_execution_mode": mode,
                        "owner_pane_id": pane_id,
                        "owner_process_identity_json": None,
                        "intent_json": intent_json,
                        "ambiguity_reason": None,
                        "epoch": observed_epoch + 1,
                        "updated_at": stamp,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, provider, native_session_id)
                raise NativeAttachmentConflict(
                    f"lost the race to re-acquire {provider} session {native_session_id}; "
                    f"it is now {current.state!r} held by {_describe_owner(current)}"
                )
            return _row_dict(_fetch(db, provider, native_session_id)), True
    except NativeAttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed; never attach unrecorded
        raise NativeAttachmentUnavailable(f"native attachment declare failed: {exc}") from exc


def _transition(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    from_states: frozenset[str],
    to_state: str,
    extra: Optional[dict[str, Any]] = None,
    idempotent_from: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """CAS one attachment forward, refusing any non-owner and any regression."""
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    terminal_id = _require_text(terminal_id, field="terminal_id")
    generation = _require_text(generation, field="generation")
    mode = em.validate_mode(execution_mode)

    try:
        with database.SessionLocal() as db:
            row = _fetch(db, provider, native_session_id)
            if row is None:
                raise NativeAttachmentNotFound(
                    f"no attachment declared for {provider} session {native_session_id}"
                )
            _guard_frozen(row)
            _assert_owner_matches(row, terminal_id=terminal_id, generation=generation, mode=mode)
            if row.state in idempotent_from:
                return _row_dict(row)
            if row.state not in from_states:
                raise NativeAttachmentConflict(
                    f"{provider} session {native_session_id} is {row.state!r}; "
                    f"{to_state!r} requires one of {sorted(from_states)}"
                )

            observed_epoch = row.epoch
            values: dict[str, Any] = {
                "state": to_state,
                "epoch": observed_epoch + 1,
                "updated_at": _now(),
            }
            values.update(extra or {})
            # ``Query.update`` accepts column-name keys, but its parameter type
            # is a union and ``dict`` is invariant in its key type, so a
            # ``dict[str, Any]`` built up here is not assignable to it. The
            # keys really are column names — every caller below supplies them
            # as literals — so the cast narrows nothing that is not already true.
            updated = (
                db.query(database.NativeSessionAttachmentModel)
                .filter(
                    database.NativeSessionAttachmentModel.provider == provider,
                    database.NativeSessionAttachmentModel.native_session_id == native_session_id,
                    database.NativeSessionAttachmentModel.epoch == observed_epoch,
                    database.NativeSessionAttachmentModel.owner_terminal_id == terminal_id,
                    database.NativeSessionAttachmentModel.owner_generation == generation,
                    database.NativeSessionAttachmentModel.owner_execution_mode == mode,
                )
                .update(cast("dict[Any, Any]", values), synchronize_session=False)
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, provider, native_session_id)
                raise NativeAttachmentConflict(
                    f"concurrent modification of {provider} session {native_session_id}; "
                    f"expected epoch {observed_epoch}, now {current.epoch} "
                    f"in state {current.state!r}"
                )
            return _row_dict(_fetch(db, provider, native_session_id))
    except NativeAttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeAttachmentUnavailable(f"native attachment transition failed: {exc}") from exc


def mark_starting(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    pane_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record that the provider process is about to be started.

    Crossed *before* the process exists.  A crash here leaves ``starting``
    with no published identity — the state that says "a process may or
    may not be running", which recovery must resolve by proof or freeze.
    """
    extra: dict[str, Any] = {}
    if pane_id is not None:
        extra["owner_pane_id"] = _require_text(pane_id, field="pane_id")
    return _transition(
        provider=provider,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
        from_states=frozenset({DECLARED}),
        to_state=STARTING,
        extra=extra,
        idempotent_from=frozenset({STARTING}),
    )


def mark_attached(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    process_identity: Mapping[str, Any],
    pane_id: Optional[str] = None,
) -> dict[str, Any]:
    """Publish the proven identity of the process now holding the session.

    Until this commits, no identity is on record, so a later release
    cannot name one.  That asymmetry is deliberate: publication is what
    makes an exact-identity no-survivor proof possible at all.
    """
    if not isinstance(process_identity, Mapping):
        raise NativeAttachmentInvalid("process_identity must be a mapping")
    identity = dict(process_identity)
    if not isinstance(identity.get("pid"), int) or isinstance(identity.get("pid"), bool):
        raise NativeAttachmentInvalid("process_identity requires an integer pid")
    _require_text(identity.get("start_marker"), field="process_identity.start_marker")

    extra: dict[str, Any] = {"owner_process_identity_json": _canonical(identity)}
    if pane_id is not None:
        extra["owner_pane_id"] = _require_text(pane_id, field="pane_id")
    return _transition(
        provider=provider,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
        from_states=frozenset({STARTING}),
        to_state=ATTACHED,
        extra=extra,
    )


def mark_draining(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> dict[str, Any]:
    """Record that the owner is winding the session down but still holds it."""
    return _transition(
        provider=provider,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
        from_states=frozenset({ATTACHED}),
        to_state=DRAINING,
        idempotent_from=frozenset({DRAINING}),
    )


def _validate_release_proof(row: Any, proof: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a proof only when it names this exact owner and no survivor."""
    if not isinstance(proof, Mapping) or proof.get("schema") != NO_SURVIVOR_PROOF_SCHEMA:
        raise NativeAttachmentInvalid(
            f"release requires a proof built by no_survivor_proof "
            f"(schema {NO_SURVIVOR_PROOF_SCHEMA!r}); release without proof is refused"
        )
    mismatches = [
        field
        for field, expected in (
            ("provider", row.provider),
            ("native_session_id", row.native_session_id),
            ("terminal_id", row.owner_terminal_id),
            ("generation", row.owner_generation),
            ("execution_mode", row.owner_execution_mode),
            ("pane_id", row.owner_pane_id),
        )
        if proof.get(field) != expected
    ]
    if mismatches:
        raise NativeAttachmentConflict(
            f"no-survivor proof does not describe this attachment; mismatched {mismatches}; "
            "a proof about a different owner or pane releases nothing"
        )

    published = _parse_json(row.owner_process_identity_json)
    if proof.get("process_identity") != published:
        raise NativeAttachmentConflict(
            "no-survivor proof must name the exact published process identity "
            f"({published!r}); got {proof.get('process_identity')!r}"
        )

    survivors = proof.get("survivors")
    if not isinstance(survivors, list):
        raise NativeAttachmentInvalid(
            "no-survivor proof must carry a present survivors observation; an absent one "
            "is indistinguishable from never having looked"
        )
    if survivors:
        raise NativeAttachmentConflict(
            f"no-survivor proof reports {len(survivors)} survivor(s); the owner is still "
            "live and the attachment is not releasable"
        )
    return dict(proof)


def release(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    proof: Mapping[str, Any],
) -> dict[str, Any]:
    """Detach, but only on an exact old-process/no-survivor proof.

    The proof is validated against the stored owner *inside* the same
    transaction that performs the CAS, so a row that changed underneath
    the observation cannot be released by a proof describing its previous
    state.
    """
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    terminal_id = _require_text(terminal_id, field="terminal_id")
    generation = _require_text(generation, field="generation")
    mode = em.validate_mode(execution_mode)

    try:
        with database.SessionLocal() as db:
            row = _fetch(db, provider, native_session_id)
            if row is None:
                raise NativeAttachmentNotFound(
                    f"no attachment declared for {provider} session {native_session_id}"
                )
            _guard_frozen(row)
            _assert_owner_matches(row, terminal_id=terminal_id, generation=generation, mode=mode)
            if row.state == DETACHED:
                return _row_dict(row)
            validated = _validate_release_proof(row, proof)

            observed_epoch = row.epoch
            updated = (
                db.query(database.NativeSessionAttachmentModel)
                .filter(
                    database.NativeSessionAttachmentModel.provider == provider,
                    database.NativeSessionAttachmentModel.native_session_id == native_session_id,
                    database.NativeSessionAttachmentModel.epoch == observed_epoch,
                    database.NativeSessionAttachmentModel.state.in_(sorted(LIVE_STATES)),
                )
                .update(
                    {
                        "state": DETACHED,
                        "release_proof_json": _canonical(validated),
                        "epoch": observed_epoch + 1,
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, provider, native_session_id)
                raise NativeAttachmentConflict(
                    f"concurrent modification of {provider} session {native_session_id}; "
                    f"expected epoch {observed_epoch}, now {current.epoch} "
                    f"in state {current.state!r}"
                )
            return _row_dict(_fetch(db, provider, native_session_id))
    except NativeAttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeAttachmentUnavailable(f"native attachment release failed: {exc}") from exc


def mark_ambiguous(
    *,
    provider: str,
    native_session_id: str,
    reason: str,
) -> dict[str, Any]:
    """Freeze the attachment, preserving its owner, forever.

    Deliberately takes **no owner argument**.  Ambiguity is precisely the
    condition in which the caller cannot prove who owns the session, so
    requiring it to name the owner would either be unanswerable or invite
    a guess.  Recovery and the owner itself both reach this the same way.

    The recorded owner is preserved and the row never leaves this state
    through this module: a frozen attachment blocks every future claim on
    the session, which is the intended outcome.  Releasing it would hand
    the session to a new attacher while a possible survivor still holds
    it.
    """
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    reason = _require_text(reason, field="reason")

    try:
        with database.SessionLocal() as db:
            row = _fetch(db, provider, native_session_id)
            if row is None:
                raise NativeAttachmentNotFound(
                    f"no attachment declared for {provider} session {native_session_id}"
                )
            if row.state == AMBIGUOUS:
                return _row_dict(row)
            if row.state == DETACHED:
                raise NativeAttachmentConflict(
                    f"{provider} session {native_session_id} is detached with a recorded "
                    "no-survivor proof; there is no owner to freeze"
                )

            observed_epoch = row.epoch
            updated = (
                db.query(database.NativeSessionAttachmentModel)
                .filter(
                    database.NativeSessionAttachmentModel.provider == provider,
                    database.NativeSessionAttachmentModel.native_session_id == native_session_id,
                    database.NativeSessionAttachmentModel.epoch == observed_epoch,
                )
                .update(
                    {
                        "state": AMBIGUOUS,
                        "ambiguity_reason": reason,
                        "epoch": observed_epoch + 1,
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, provider, native_session_id)
                raise NativeAttachmentConflict(
                    f"concurrent modification of {provider} session {native_session_id}; "
                    f"expected epoch {observed_epoch}, now {current.epoch}"
                )
            return _row_dict(_fetch(db, provider, native_session_id))
    except NativeAttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeAttachmentUnavailable(f"native attachment freeze failed: {exc}") from exc


def get(provider: str, native_session_id: str) -> Optional[dict[str, Any]]:
    """The attachment record, or ``None`` when the session was never claimed."""
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    try:
        with database.SessionLocal() as db:
            row = _fetch(db, provider, native_session_id)
            return _row_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeAttachmentUnavailable(f"native attachment lookup failed: {exc}") from exc


def is_held(provider: str, native_session_id: str) -> bool:
    """True when any owner still holds the session (including frozen).

    A frozen ambiguous row counts as held: an unresolved attachment is
    exactly the case where a new attach is most dangerous.
    """
    record = get(provider, native_session_id)
    return record is not None and record["state"] != DETACHED
