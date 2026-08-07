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
- **Ambiguity freezes and never releases *automatically*.**  When
  ownership cannot be resolved, the owner is preserved and the row
  becomes terminal for automation.  Auto-releasing an ambiguous row is
  exactly the double-attach this module exists to prevent.  A human
  resolves it, through :func:`adjudicate` — the single path out, which
  requires a named operator and re-refuses an owner still observably
  alive.  Until that function existed the sentence "a human resolves it"
  described no reachable code, and the only valve was editing the
  database by hand.

Every transition is a compare-and-swap on ``(key, epoch, exact owner)``
and bumps ``epoch``, so a lost update is refused rather than silently
winning last-write.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, cast

from sqlalchemy import inspect as sa_inspect
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
# A provider-native control plane minted the persistent session without
# submitting a model turn.  Codex app-server is such a control plane; calling
# it ACP would make the durable attachment receipt claim a route that was not
# used.
ACQUISITION_ZERO_TURN_BOOTSTRAP = "zero_turn_provider_bootstrap"
ACQUISITION_RESUME = "pinned_resume"
#: The id was *chosen* by this system and handed to the provider at launch
#: rather than obtained from it.  Distinct from the other two because both
#: of those describe acquiring an id the provider already had: a bootstrap
#: reads one back out of a transport, and a resume names one that exists.
#: Recording either of those for a freshly chosen id would make the
#: journaled receipt say the session pre-dated the launch when it did not.
ACQUISITION_CHOSEN_SESSION_ID = "chosen_session_id"
ACQUISITION_METHODS = frozenset(
    {
        ACQUISITION_ACP_BOOTSTRAP,
        ACQUISITION_ZERO_TURN_BOOTSTRAP,
        ACQUISITION_RESUME,
        ACQUISITION_CHOSEN_SESSION_ID,
    }
)

INTENT_SCHEMA = "cao-native-attachment-intent-v1"
NO_SURVIVOR_PROOF_SCHEMA = "cao-native-attachment-no-survivor-v1"
#: An operator's adjudication of a frozen row.  Deliberately a *different*
#: schema from the machine-checked no-survivor proof, and stored in the same
#: column, so a later reader can always tell which kind of evidence detached
#: a session.  Collapsing the two would make a human's judgement call
#: indistinguishable from an observed absence.
ADJUDICATION_SCHEMA = "cao-native-attachment-adjudication-v1"

#: The one outcome an operator may assert.  A closed literal rather than
#: free text: "the owner is gone" is the only claim that licenses handing
#: the session to a new attacher, and a typo must not be able to spell
#: something weaker that still passes.
ADJUDICATION_OUTCOME_OWNER_GONE = "owner-proven-gone"
ADJUDICATION_OUTCOMES = frozenset({ADJUDICATION_OUTCOME_OWNER_GONE})


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

    Either zero-turn bootstrap method must additionally assert that the
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

    if acquisition_method in {
        ACQUISITION_ACP_BOOTSTRAP,
        ACQUISITION_ZERO_TURN_BOOTSTRAP,
    }:
        if bootstrap_sent_no_turn is not True or bootstrap_detached_before_launch is not True:
            raise NativeAttachmentInvalid(
                "a zero-turn bootstrap must assert bootstrap_sent_no_turn=True and "
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
    expected_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Freeze the attachment, preserving its owner, forever.

    Deliberately takes **no owner argument**.  Ambiguity is precisely the
    condition in which the caller cannot prove who owns the session, so
    requiring it to name the owner would either be unanswerable or invite
    a guess.  Recovery and the owner itself both reach this the same way.

    The recorded owner is preserved, and the only way out is
    :func:`adjudicate`, which requires a named human.  A frozen attachment
    blocks every future claim on the session, which is the intended
    outcome.  Releasing it automatically would hand the session to a new
    attacher while a possible survivor still holds it.

    ``expected_epoch`` binds the freeze to a row somebody *observed*, and
    is optional because the two kinds of caller differ.  An owner freezing
    its own row — a launch that could not verify its pane — must succeed
    regardless of what else has happened, and has no epoch to name.  A
    caller that first looked at the row and then decided it was freezable
    is in a different position: between the look and the write the owner
    can die, a sweep can release the row, and a new launch can claim it,
    at which point an unbound freeze lands on a live launch that published
    no identity yet — blocking its ``mark_attached`` while its process
    keeps running, which is the precondition for exactly the double-attach
    this module exists to prevent.
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
            if expected_epoch is not None and row.epoch != expected_epoch:
                raise NativeAttachmentConflict(
                    f"{provider} session {native_session_id} moved to epoch {row.epoch} since "
                    f"it was observed at epoch {expected_epoch}; the freeze describes an owner "
                    "this row no longer has"
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


def adjudication(
    *,
    outcome: str,
    evidence_sha256: str,
    detail: str,
    operator: str,
    observed_at: str,
    observation: Mapping[str, Any],
    attests_live_process_is_not_the_owner: bool = False,
) -> dict[str, Any]:
    """Build one operator's answer to a frozen attachment.

    This is the human analogue of :func:`no_survivor_proof`, and it is
    deliberately *not* that function.  A no-survivor proof asserts an
    observation: the exact published process was looked for and was not
    there.  A frozen row is frozen precisely because that observation
    could not be made — most of them never published an identity at all —
    so a human cannot produce one, and letting them submit one anyway
    would put a guess into a field that every later reader treats as
    machine-checked fact.

    What the operator supplies instead is accountability: a closed
    ``outcome`` they must name exactly, a digest of the evidence they
    looked at, a bounded free-text ``detail`` explaining the call, and
    their own name.  ``observation`` is whatever the system could still
    see at adjudication time, recorded verbatim so a later reader can
    judge the call rather than only read its conclusion.
    """
    if outcome not in ADJUDICATION_OUTCOMES:
        raise NativeAttachmentInvalid(
            f"outcome must be one of {sorted(ADJUDICATION_OUTCOMES)}; got {outcome!r}"
        )
    evidence_sha256 = _require_text(evidence_sha256, field="evidence_sha256")
    if len(evidence_sha256) != 64 or any(c not in "0123456789abcdef" for c in evidence_sha256):
        raise NativeAttachmentInvalid(
            "evidence_sha256 must be 64 lowercase hex characters; an adjudication whose "
            "evidence cannot be identified later is an unattributable release"
        )
    detail = _require_text(detail, field="detail")
    if len(detail) > 500:
        raise NativeAttachmentInvalid(f"detail must be at most 500 characters; got {len(detail)}")
    if not isinstance(observation, Mapping):
        raise NativeAttachmentInvalid("observation must be a mapping")
    if attests_live_process_is_not_the_owner not in (True, False):
        raise NativeAttachmentInvalid("attests_live_process_is_not_the_owner must be a bool")
    return {
        "schema": ADJUDICATION_SCHEMA,
        "outcome": outcome,
        "evidence_sha256": evidence_sha256,
        "detail": detail,
        "operator": _require_text(operator, field="operator"),
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observation": dict(observation),
        # A separate assertion from the outcome, because it is about a
        # different thing: the outcome says the owner is gone, this says
        # the process still bearing its pid is somebody else.
        "attests_live_process_is_not_the_owner": bool(attests_live_process_is_not_the_owner),
    }


def _assert_adjudication_replay_matches(stored: Any, incoming: Mapping[str, Any]) -> None:
    """Refuse a replay that says something different from the stored record.

    Re-submitting the same adjudication is ordinary: an operator retries, a
    request is delivered twice.  Re-submitting a *different* one against an
    already-detached row is not, and silently accepting it would let the
    second caller believe their reasoning is what released the session when
    the first caller's is what is on record.
    """
    if not isinstance(stored, Mapping) or stored.get("schema") != ADJUDICATION_SCHEMA:
        raise NativeAttachmentConflict(
            "this attachment was already detached by a no-survivor proof rather than an "
            "adjudication; there is nothing frozen left to adjudicate"
        )
    differing = [
        field
        for field in (
            "outcome",
            "evidence_sha256",
            "detail",
            "operator",
            "attests_live_process_is_not_the_owner",
        )
        if stored.get(field) != incoming.get(field)
    ]
    if differing:
        raise NativeAttachmentConflict(
            f"adjudication replay contradicts the stored record; differing {differing}"
        )


#: Recorded in ``ambiguity_reason`` when an operator attests that the
#: process now holding the recorded pid is *not* the recorded owner.  A
#: recycled pid is the one case automation can never settle: the marker
#: says the process is a stranger, and automation may not trust the
#: marker, because a timezone change produces the same mismatch.  A human
#: can weigh that; this is how they say so, and it is stored so the next
#: reader can see the decision rather than infer it.
RECYCLED_PID_ATTESTATION = "operator_attests_live_pid_is_not_the_recorded_owner"


def _attestation_excludes(row: Any, record: Mapping[str, Any], survivors: list[Any]) -> bool:
    """True when the operator's attestation covers every survivor found.

    The attestation lives on the adjudication — the decision — rather than
    on the freeze that classified the row.  It was on the freeze first,
    and that was wrong in a way with a concrete cost: ``mark_ambiguous``
    preserves the *first* reason, so a row already frozen by its launcher
    (an unverifiable pane, say) that also happened to have a recycled pid
    silently dropped the attestation and could then never be adjudicated
    at all while the stranger process lived.

    A survivor counts as excluded only when its marker was actually read
    and genuinely differs.  An unreadable marker is ``None``, which is
    unequal to anything — treating that as "different" would let an
    attestation about a recycled pid release an owner nobody could
    identify either way.  A marker that *matches* is the recorded owner,
    alive, and no attestation makes that releasable.
    """
    if record.get("attests_live_process_is_not_the_owner") is not True:
        return False
    recorded = (_parse_json(row.owner_process_identity_json) or {}).get("start_marker")
    return all(
        isinstance(survivor, Mapping)
        and isinstance(survivor.get("start_marker"), str)
        and survivor["start_marker"] != recorded
        for survivor in survivors
    )


def adjudicate(
    *,
    provider: str,
    native_session_id: str,
    record: Mapping[str, Any],
    observed_epoch: int,
    live_survivors: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """The one path out of ``ambiguous``, and it is not automation.

    ``mark_ambiguous`` is terminal for every machine in this system.  It
    has to be: a frozen row is one whose ownership could not be resolved,
    and a program that resolves it anyway has simply lowered the standard
    of proof rather than met it.  But "a human resolves it" was, until
    this function, a sentence describing nothing — no route, no command,
    and :func:`release` refuses a frozen row at ``_guard_frozen`` before
    it ever looks at a proof.  The absence had a cost: every frozen
    session on an install stayed unresumable forever.

    ``live_survivors`` is the caller's *fresh* observation, and it is
    required to be present.  When the frozen owner published a process
    identity, an observer must go and look again before a human is
    allowed to override the freeze; a non-empty list refuses here.  When
    no identity was ever published — the common case, because most rows
    freeze before ``mark_attached`` — an honest observer can still only
    report an empty list, and it is the ``record``'s digest, detail and
    operator name that carry the decision.  That asymmetry is the point:
    the machine keeps its veto over an owner it can still see, and gives
    up only the judgement it was never able to make.

    ``observed_epoch`` is what binds those two halves together, and it is
    required rather than optional because an optional one leaves the hole
    open for the next caller.  :func:`release` is safe without it only
    because its proof names the exact owner and is re-validated against
    the stored row inside the writing transaction; an adjudication names
    no owner at all — it is a human's sentence — so the epoch is the only
    thing tying it to the row it was written about.  Without it, an
    operator's observation taken before a confirmation prompt can be
    applied *after* the session was released, re-claimed by a new launch,
    and frozen again with a live process on it.  The state would still
    read ``ambiguous`` and the stale survivor list would still read empty,
    and a running provider would lose its exclusive claim.
    """
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    if not isinstance(record, Mapping) or record.get("schema") != ADJUDICATION_SCHEMA:
        raise NativeAttachmentInvalid(
            f"record must be built by adjudication (schema {ADJUDICATION_SCHEMA!r}); "
            "an unvalidated adjudication cannot release a frozen session"
        )
    if not isinstance(live_survivors, list):
        raise NativeAttachmentInvalid(
            "adjudication requires a present live_survivors observation; an absent one is "
            "indistinguishable from never having looked, which is how the row froze"
        )
    validated = dict(record)

    try:
        with database.SessionLocal() as db:
            row = _fetch(db, provider, native_session_id)
            if row is None:
                raise NativeAttachmentNotFound(
                    f"no attachment declared for {provider} session {native_session_id}"
                )
            if row.state == DETACHED:
                _assert_adjudication_replay_matches(_parse_json(row.release_proof_json), validated)
                return _row_dict(row)
            if row.state != AMBIGUOUS:
                raise NativeAttachmentConflict(
                    f"{provider} session {native_session_id} is {row.state!r}, not "
                    f"{AMBIGUOUS!r}; a live attachment is released by proving its owner gone, "
                    "not by adjudicating it"
                )
            if row.epoch != observed_epoch:
                raise NativeAttachmentConflict(
                    f"{provider} session {native_session_id} moved to epoch {row.epoch} since "
                    f"the observation was taken at epoch {observed_epoch}; the adjudication "
                    "describes an owner this row no longer has"
                )
            if live_survivors and not _attestation_excludes(row, validated, live_survivors):
                raise NativeAttachmentConflict(
                    f"the frozen owner of {provider} session {native_session_id} is still "
                    f"observably alive ({len(live_survivors)} survivor(s)); an operator may "
                    "resolve an unresponsive owner, never a running one"
                )
            updated = (
                db.query(database.NativeSessionAttachmentModel)
                .filter(
                    database.NativeSessionAttachmentModel.provider == provider,
                    database.NativeSessionAttachmentModel.native_session_id == native_session_id,
                    database.NativeSessionAttachmentModel.epoch == observed_epoch,
                    database.NativeSessionAttachmentModel.state == AMBIGUOUS,
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
        raise NativeAttachmentUnavailable(f"native attachment adjudication failed: {exc}") from exc


def list_attachments(
    *,
    states: Optional[frozenset[str]] = None,
    providers: Optional[frozenset[str]] = None,
    owner_terminal_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Every attachment matching the filters, oldest claim first.

    The store had no enumerator at all, which is why the leak this
    function exists to expose could run for two weeks unseen: ``get`` and
    ``is_held`` both require you to already know the ``(provider,
    native_session_id)`` you are asking about, so nothing could answer
    "what is still held?" — the only question an operator or a sweeper
    actually has.

    Ordered by ``created_at`` so a reader sees the oldest unresolved
    claim first; that is the one most likely to be an orphan and least
    likely to be a launch in flight.

    An **absent table** returns an empty list rather than raising, and
    that is not a relaxation of the fail-closed rule the rest of this
    module follows.  The table is created by the migration ladder at
    server start, so its absence means no server has ever run against
    this store — and therefore that no claim has ever been taken.  Empty
    is the true answer, not a degraded one.  A table that exists and
    cannot be read still raises, because that is a different fact.
    """
    if states is not None:
        unknown = sorted(set(states) - ATTACHMENT_STATES)
        if unknown:
            raise NativeAttachmentInvalid(
                f"unknown attachment state(s) {unknown}; expected a subset of "
                f"{sorted(ATTACHMENT_STATES)}"
            )
    try:
        with database.SessionLocal() as db:
            table = database.NativeSessionAttachmentModel.__tablename__
            if not sa_inspect(db.get_bind()).has_table(table):
                return []
            query = db.query(database.NativeSessionAttachmentModel)
            if states is not None:
                query = query.filter(
                    database.NativeSessionAttachmentModel.state.in_(sorted(states))
                )
            if providers is not None:
                query = query.filter(
                    database.NativeSessionAttachmentModel.provider.in_(sorted(providers))
                )
            if owner_terminal_id is not None:
                query = query.filter(
                    database.NativeSessionAttachmentModel.owner_terminal_id == owner_terminal_id
                )
            rows = query.order_by(
                database.NativeSessionAttachmentModel.created_at,
                database.NativeSessionAttachmentModel.provider,
                database.NativeSessionAttachmentModel.native_session_id,
            ).all()
            return [_row_dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeAttachmentUnavailable(f"native attachment listing failed: {exc}") from exc


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
