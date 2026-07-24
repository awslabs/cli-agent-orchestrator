"""Provider-native control of a Kimi TUI running in a managed pane.

This is the Kimi-specific control adapter: the one path by which CAO
sends ordinary follow-up, a deliberate mid-turn steer, or a literal slash
command to a native Kimi session.  It is deliberately *not* generic pane
paste and *not* any ACP receipt kind.  Three facts drive its shape.

**A pane write is not provider acceptance.**  Writing bytes to a
terminal proves only that a terminal accepted bytes.  It says nothing
about whether the provider's composer had focus, whether the TUI was
mid-render, or whether the model ever saw the text.  So the transport
result and the provider observation are separate fields, reached by
separate calls, and no code path lets the first stand in for the second.
An operation that was posted but never observed reads as posted -- never
as accepted.

**The queue and the steer are different acts.**  Ordinary follow-up must
wait for the provider to be idle and land in the native queue; it must
not barge into a running turn.  A steer is the opposite: it is a
deliberate interruption of one exact turn, requested explicitly, and it
is meaningless when nothing is running.  Collapsing them into "send
text" is what makes a routine nudge silently derail a long turn, so they
are separate operations with separate ids, separate gates, and separate
evidence.

**Ambiguity is preserved, never retried.**  If the transport raises, the
bytes may or may not have landed.  Re-sending is the one action that can
turn an uncertainty into a duplicate, so it is impossible here: an
ambiguous operation freezes, resolves only through :func:`reconcile` with
evidence naming its exact id, and blocks further operations on that
session until it is resolved.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, cast

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services.canonical_json import canonical_sha256

#: The only provider this adapter speaks for.  Named rather than
#: parameterized: the gating, the composer behaviour, and the slash
#: syntax below are Kimi's, and a second provider needs its own adapter
#: rather than a flag in this one.
PROVIDER = "kimi_cli"

#: Schema tags.  Distinct from every ACP receipt kind on purpose -- a
#: consumer must never be able to satisfy an ACP receipt requirement with
#: a native control record, in either direction.
RECORD_SCHEMA = "cao-kimi-native-control-v1"
INTENT_SCHEMA = "cao-kimi-native-control-intent-v1"
TURN_OBSERVATION_SCHEMA = "cao-kimi-native-turn-observation-v1"
PROVIDER_OBSERVATION_SCHEMA = "cao-kimi-native-control-observation-v1"

KIND_QUEUE = "queue"
KIND_STEER = "steer"
KIND_CONTROL = "control"
OPERATION_KINDS = frozenset({KIND_QUEUE, KIND_STEER, KIND_CONTROL})

#: ``intended`` -> intent is durable, nothing has been typed.
#: ``posted``   -> bytes reached the transport.  Transport fact only.
#: ``accepted`` -> the provider was observed taking the input.
#: ``completed``-> the provider was observed finishing the operation.
#: ``refused``  -> a typed refusal, terminal.
#: ``ambiguous``-> the outcome is unknown and frozen until reconciled.
#:
#: This is the managed session-operation ladder with one state inserted.
#: ``intended`` is that ladder's ``queued`` under a different name --
#: this module already uses "queue" for an operation *kind*, and one word
#: meaning two things in the same record is how a reader ends up thinking
#: a queued message and a queued operation are the same event.
#: ``posted`` is the genuinely new state, and it exists because on a
#: native TUI "we sent it" and "the provider took it" are separate facts
#: with a real gap between them.
INTENDED = "intended"
POSTED = "posted"
ACCEPTED = "accepted"
COMPLETED = "completed"
REFUSED = "refused"
AMBIGUOUS = "ambiguous"
OPERATION_STATES = frozenset({INTENDED, POSTED, ACCEPTED, COMPLETED, REFUSED, AMBIGUOUS})

#: States that need nothing further.  ``ambiguous`` is deliberately absent:
#: it is frozen, not finished, and it still blocks the session.
RESOLVED_STATES = frozenset({COMPLETED, REFUSED})

#: Typed refusal reasons.  Each names a fact that was checked, so a
#: consumer can tell "the provider said no" from "CAO would not ask".
REFUSED_ACTIVE_TURN = "active_turn_in_progress"
REFUSED_NO_ACTIVE_TURN = "no_active_turn"
REFUSED_TURN_MISMATCH = "turn_mismatch"
REFUSED_UNSUPPORTED_CONTROL = "unsupported_control"
REFUSED_ATTACHMENT = "attachment_not_owned"
REFUSED_UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"
REFUSED_PROVIDER = "provider_refused"
REFUSAL_REASONS = frozenset(
    {
        REFUSED_ACTIVE_TURN,
        REFUSED_NO_ACTIVE_TURN,
        REFUSED_TURN_MISMATCH,
        REFUSED_UNSUPPORTED_CONTROL,
        REFUSED_ATTACHMENT,
        REFUSED_UNRESOLVED_AMBIGUITY,
        REFUSED_PROVIDER,
    }
)

#: The one control command the lifecycle contract names by itself.  Every
#: other control -- including route controls -- must be advertised by the
#: provider before it can be sent, and so has no constant here.  Even
#: this one is capability-gated; it is named only because the contract
#: names it.
CONTROL_COMPACT = "/compact"

#: Characters that must never reach a provider composer through this
#: adapter.  Refusing ESC as a class is deliberate and is *not* a
#: restatement of any specific escape sequence: bracketed-paste
#: sentinels, cursor moves, and menu-opening sequences all begin with
#: ESC, so refusing the class covers them without this module keeping a
#: second copy of a sentinel vocabulary that belongs to the
#: cross-provider control-input contract.  CR and LF are refused with it
#: because an embedded newline submits the composer early -- sending
#: half a message and leaving the remainder as a stray line.
_FORBIDDEN_CHARACTERS = ("\x1b", "\r", "\n")


class NativeControlError(RuntimeError):
    """Base class for every native control failure."""

    code = "kimi-native-control-error"


class NativeControlInvalid(NativeControlError):
    """The request is malformed; nothing was journaled and nothing sent.

    Distinct from a refusal on purpose.  A refusal is an operational
    answer that is worth keeping ("the provider was busy"); this is a
    caller bug, and durably recording it as an operation would put
    fiction in the evidence trail.
    """

    code = "kimi-native-control-invalid"


class NativeControlConflict(NativeControlError):
    """A caller-minted id was reused for a materially different request."""

    code = "kimi-native-control-conflict"


class NativeControlNotFound(NativeControlError):
    """No operation exists for the given id."""

    code = "kimi-native-control-not-found"


class NativeControlUnavailable(NativeControlError):
    """The durable store could not be read or written; fail closed."""

    code = "kimi-native-control-unavailable"


class NativeControlTransport(Protocol):
    """The pane-writing capability this adapter borrows.

    Two methods rather than one, because the contract requires the
    submitting keystroke to be explicit.  A single ``send(text)`` would
    let a trailing newline inside the payload do the submitting, which is
    exactly the accident that turns a half-typed message into a sent one.

    Neither method returns anything meaningful.  That is the point: there
    is no value a transport could return that would constitute provider
    acceptance, so the interface offers none to be misread.
    """

    def send_literal(self, text: str) -> None:
        """Write ``text`` to the pane exactly, with no paste wrapper."""

    def send_enter(self) -> None:
        """Send the submitting key as its own separate keystroke."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeControlInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def assert_artifact_free(text: str, *, field: str = "text") -> str:
    """Return ``text`` if it can be typed literally with no artifacts.

    Refuses rather than strips.  Stripping would silently change the
    message a human asked to send, and a payload carrying an escape
    sequence means the caller built it through a paste path this adapter
    exists to replace -- a fact worth surfacing, not sanitizing away.
    """
    if not isinstance(text, str) or not text:
        raise NativeControlInvalid(f"{field} must be a non-empty string; got {text!r}")
    for forbidden in _FORBIDDEN_CHARACTERS:
        if forbidden in text:
            raise NativeControlInvalid(
                f"{field} contains {forbidden!r}, which must never reach a provider "
                f"composer; native control types one literal line and sends Enter separately"
            )
    return text


def turn_observation(
    *,
    active_turn_id: Optional[str],
    observed_at: str,
    observer: str,
) -> dict[str, Any]:
    """Build the idle/busy observation that gates a queue or a steer.

    ``active_turn_id`` is required and may be ``None``; ``None`` means
    "observed idle", not "did not look".  The distinction is the whole
    value of the field: an omitted observation and an idle one are
    indistinguishable once stored, and "we did not check" must never be
    able to satisfy an idle gate.

    This adapter never derives the observation itself.  Deciding whether
    a TUI is mid-turn is a live-surface judgement belonging to whatever
    watches the pane; a guess manufactured here would be the least
    informed one in the system.
    """
    if active_turn_id is not None:
        active_turn_id = _require_text(active_turn_id, field="active_turn_id")
    return {
        "schema": TURN_OBSERVATION_SCHEMA,
        "active_turn_id": active_turn_id,
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
    }


def provider_observation(
    *,
    operation_id: str,
    observed_at: str,
    observer: str,
    evidence: Mapping[str, Any],
    entered_turn_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the provider-side fact that can move an operation forward.

    Carries ``operation_id`` inside the observation, not merely alongside
    it, so resolving a lost response requires evidence that names the
    exact operation.  An observation that only says "something was
    accepted" cannot be pointed at whichever operation is convenient.
    """
    if not isinstance(evidence, Mapping) or not evidence:
        raise NativeControlInvalid("evidence must be a non-empty mapping")
    if entered_turn_id is not None:
        entered_turn_id = _require_text(entered_turn_id, field="entered_turn_id")
    return {
        "schema": PROVIDER_OBSERVATION_SCHEMA,
        "operation_id": _require_text(operation_id, field="operation_id"),
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
        "entered_turn_id": entered_turn_id,
        "evidence": dict(evidence),
    }


def _intent(
    *,
    kind: str,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    turn_id: Optional[str],
    payload_sha256: str,
    turn_observation_record: Mapping[str, Any],
) -> dict[str, Any]:
    """The record written before anything is typed.

    Everything needed to adjudicate a crash between this write and the
    keystroke is here: which operation, against which exact session and
    generation, in which mode, with which payload, and what the pane
    looked like at the moment the decision was made.
    """
    return {
        "schema": INTENT_SCHEMA,
        "kind": kind,
        "operation_id": operation_id,
        "provider": PROVIDER,
        "native_session_id": native_session_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": execution_mode,
        "turn_id": turn_id,
        "payload_sha256": payload_sha256,
        "turn_observation": dict(turn_observation_record),
    }


def _row_dict(row: Any) -> dict[str, Any]:
    """Project one operation, keeping transport and provider facts apart."""
    return {
        "schema": RECORD_SCHEMA,
        "operation_id": row.operation_id,
        "kind": row.kind,
        "state": row.state,
        "provider": row.provider,
        "native_session_id": row.native_session_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "execution_mode": row.execution_mode,
        "turn_id": row.turn_id,
        "payload_sha256": row.payload_sha256,
        "intent": _parse_json(row.intent_json),
        # Transport truth and provider truth are separate keys that are
        # never derived from one another. ``posted`` says bytes were
        # written; only ``provider_accepted`` says the provider took them.
        "posted": row.posted_at is not None,
        "posted_at": row.posted_at,
        "transport": _parse_json(row.transport_json),
        "provider_accepted": row.state in {ACCEPTED, COMPLETED},
        "provider_completed": row.state == COMPLETED,
        "observation": _parse_json(row.observation_json),
        "refusal_reason": row.refusal_reason,
        "ambiguity_reason": row.ambiguity_reason,
        "is_resolved": row.state in RESOLVED_STATES,
        "epoch": row.epoch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _fetch(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.KimiNativeControlOperationModel)
        .filter(database.KimiNativeControlOperationModel.operation_id == operation_id)
        .one_or_none()
    )


def _assert_same_request(row: Any, *, kind: str, binding: Mapping[str, Any]) -> None:
    """Refuse a reused operation id that carries different content.

    Replaying the identical request is how a caller recovers from a lost
    response, so it must be free.  Reusing the id for different bytes or
    a different session is the opposite: it would overwrite the evidence
    of what was actually sent.
    """
    mismatches = [
        name
        for name, expected in (("kind", kind), *binding.items())
        if getattr(row, name) != expected
    ]
    if mismatches:
        raise NativeControlConflict(
            f"operation {row.operation_id} already exists with a different "
            f"{', '.join(sorted(mismatches))}; a caller-minted id is immutable"
        )


def _assert_session_unblocked(*, native_session_id: str, operation_id: str) -> None:
    """Refuse a new operation while an earlier one is unresolved.

    An ambiguous operation may or may not have reached the provider.
    Sending anything further would make the transcript unreadable: if the
    ambiguous input did land, the session now holds two instructions
    whose order nobody can reconstruct.  So the session is held until the
    ambiguity is resolved by exact id.

    Checked after the intent is journaled, alongside the ownership check,
    so the blocked attempt leaves a durable typed refusal rather than
    vanishing as a raised error.  A caller that later asks why nothing
    happened finds the record.
    """
    try:
        with database.SessionLocal() as db:
            blocking = (
                db.query(database.KimiNativeControlOperationModel)
                .filter(
                    database.KimiNativeControlOperationModel.native_session_id == native_session_id,
                    database.KimiNativeControlOperationModel.state == AMBIGUOUS,
                    database.KimiNativeControlOperationModel.operation_id != operation_id,
                )
                .first()
            )
            blocking_id = None if blocking is None else blocking.operation_id
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not check for unresolved ambiguity: {exc}") from exc

    if blocking_id is not None:
        raise _Refusal(
            REFUSED_UNRESOLVED_AMBIGUITY,
            f"operation {blocking_id} on session {native_session_id} is ambiguous; "
            f"it must be reconciled by exact id before further input is sent",
        )


class _Refusal(Exception):
    """Internal signal: a durable, typed refusal rather than a caller bug."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _assert_attachment_owner(
    *,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> None:
    """Require this exact owner to hold the session, attached, right now.

    Control input is a side effect on a live provider session, so it
    borrows the same exclusive-ownership record that governs attaching to
    one.  Checking here rather than trusting the caller's binding is what
    stops an operation minted for a previous generation from typing into
    the pane that replaced it.
    """
    try:
        record = native_attachment.get(PROVIDER, native_session_id)
    except native_attachment.NativeAttachmentError as exc:
        raise NativeControlUnavailable(
            f"could not read the attachment record for {PROVIDER} session "
            f"{native_session_id}: {exc}"
        ) from exc

    if record is None:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"no attachment record for {PROVIDER} session {native_session_id}; "
            f"native control requires a live owned attachment",
        )
    if record["state"] != native_attachment.ATTACHED:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"{PROVIDER} session {native_session_id} is {record['state']!r}, not "
            f"{native_attachment.ATTACHED!r}; only an attached session accepts control input",
        )
    owner = record["owner"]
    actual = (
        owner["terminal_id"],
        owner["generation"],
        owner["execution_mode"],
    )
    expected = (terminal_id, generation, execution_mode)
    if actual != expected:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"{PROVIDER} session {native_session_id} is held by {actual}, not {expected}; "
            f"control input is refused rather than delivered to another owner's pane",
        )


def _validate_binding(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> dict[str, Any]:
    """Validate the identity every operation carries, before any effect."""
    try:
        mode = em.validate_mode(execution_mode)
    except em.ExecutionModeError as exc:
        # Re-typed rather than propagated so a caller of this module needs
        # to catch exactly one error family; the original message, which
        # names the closed mode set, is preserved.
        raise NativeControlInvalid(str(exc)) from exc
    if mode != em.NATIVE_TUI:
        raise NativeControlInvalid(
            f"native control requires execution_mode {em.NATIVE_TUI!r}; got {mode!r}. "
            f"An ACP session is controlled over its own receipt-bearing path, never by "
            f"typing into a pane"
        )
    return {
        "operation_id": _require_text(operation_id, field="operation_id"),
        "provider": PROVIDER,
        "native_session_id": _require_text(native_session_id, field="native_session_id"),
        "terminal_id": _require_text(terminal_id, field="terminal_id"),
        "generation": _require_text(generation, field="generation"),
        "execution_mode": mode,
    }


def _open(
    *,
    kind: str,
    binding: Mapping[str, Any],
    turn_id: Optional[str],
    payload_sha256: str,
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Journal the intent, or return the existing operation unchanged.

    Returns ``(record, is_new)``.  ``is_new`` is False for a replay, and
    a replay never reaches the transport -- that is where at-most-once
    lives.  The row is committed before this returns, so a crash on the
    very next instruction still leaves the intent durable.
    """
    row_values = {
        **binding,
        "kind": kind,
        "state": INTENDED,
        "turn_id": turn_id,
        "payload_sha256": payload_sha256,
        "intent_json": _canonical(
            _intent(
                kind=kind,
                operation_id=cast(str, binding["operation_id"]),
                native_session_id=cast(str, binding["native_session_id"]),
                terminal_id=cast(str, binding["terminal_id"]),
                generation=cast(str, binding["generation"]),
                execution_mode=cast(str, binding["execution_mode"]),
                turn_id=turn_id,
                payload_sha256=payload_sha256,
                turn_observation_record=observation,
            )
        ),
        "epoch": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    operation_id = cast(str, binding["operation_id"])

    try:
        with database.SessionLocal() as db:
            existing = _fetch(db, operation_id)
            if existing is not None:
                _assert_same_request(
                    existing,
                    kind=kind,
                    binding={**binding, "turn_id": turn_id, "payload_sha256": payload_sha256},
                )
                return _row_dict(existing), False

            db.add(database.KimiNativeControlOperationModel(**row_values))
            db.commit()
            return _row_dict(_fetch(db, operation_id)), True
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        # A primary-key collision means a concurrent caller opened the
        # same operation first. That is a replay, not a failure: re-read
        # and let the identity check decide.
        try:
            with database.SessionLocal() as db:
                existing = _fetch(db, operation_id)
                if existing is not None:
                    _assert_same_request(
                        existing,
                        kind=kind,
                        binding={**binding, "turn_id": turn_id, "payload_sha256": payload_sha256},
                    )
                    return _row_dict(existing), False
        except NativeControlError:
            raise
        except Exception:  # noqa: BLE001 - the original failure is the real one
            pass
        raise NativeControlUnavailable(f"could not journal control intent: {exc}") from exc


def _update(
    *,
    operation_id: str,
    from_states: frozenset[str],
    to_state: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """CAS one operation forward, refusing any regression or lost update."""
    try:
        with database.SessionLocal() as db:
            row = _fetch(db, operation_id)
            if row is None:
                raise NativeControlNotFound(f"no control operation {operation_id}")
            if row.state not in from_states:
                raise NativeControlConflict(
                    f"operation {operation_id} is {row.state!r}; {to_state!r} requires one of "
                    f"{sorted(from_states)}"
                )
            observed_epoch = row.epoch
            values: dict[str, Any] = {
                "state": to_state,
                "epoch": observed_epoch + 1,
                "updated_at": _now(),
            }
            values.update(extra or {})
            updated = (
                db.query(database.KimiNativeControlOperationModel).filter(
                    database.KimiNativeControlOperationModel.operation_id == operation_id,
                    database.KimiNativeControlOperationModel.epoch == observed_epoch,
                )
                # ``Query.update`` takes column-name keys, but its parameter
                # type is a union and ``dict`` is invariant in its key type,
                # so a ``dict[str, Any]`` is not assignable to it. Every key
                # below is supplied as a column-name literal.
                .update(cast("dict[Any, Any]", values), synchronize_session=False)
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, operation_id)
                raise NativeControlConflict(
                    f"concurrent modification of operation {operation_id}; expected epoch "
                    f"{observed_epoch}, now {current.epoch} in state {current.state!r}"
                )
            return _row_dict(_fetch(db, operation_id))
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"control operation update failed: {exc}") from exc


def _refuse(operation_id: str, refusal: _Refusal) -> dict[str, Any]:
    """Record a typed refusal against an operation that never got posted."""
    return _update(
        operation_id=operation_id,
        from_states=frozenset({INTENDED}),
        to_state=REFUSED,
        extra={
            "refusal_reason": refusal.reason,
            "observation_json": _canonical({"detail": refusal.detail}),
        },
    )


def _post(
    *,
    operation_id: str,
    payload: str,
    transport: NativeControlTransport,
) -> dict[str, Any]:
    """Type the payload, then send Enter as a separate explicit keystroke.

    Any transport failure -- at either boundary -- becomes ambiguous
    rather than failed.  A raised exception does not prove the bytes did
    not land, and treating it as proof of non-delivery is precisely what
    would justify a retry and produce a duplicate.  The Enter boundary is
    recorded separately because a payload that was typed but not
    submitted is a real, different state: it sits in the composer.
    """
    literal_digest = canonical_sha256(payload)
    try:
        transport.send_literal(payload)
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        return mark_ambiguous(
            operation_id=operation_id,
            reason=f"transport raised while writing the payload: {exc}",
        )

    try:
        transport.send_enter()
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        return mark_ambiguous(
            operation_id=operation_id,
            reason=(
                f"payload was written but the submitting Enter raised: {exc}; "
                f"the composer may hold unsubmitted text"
            ),
        )

    return _update(
        operation_id=operation_id,
        from_states=frozenset({INTENDED}),
        to_state=POSTED,
        extra={
            "posted_at": _now(),
            "transport_json": _canonical(
                {
                    "literal_sha256": literal_digest,
                    # Recorded as observed facts about what this adapter
                    # did, never as a claim about the provider. The payload
                    # was checked to be a single artifact-free line, and
                    # Enter went as its own call after the text returned.
                    "enter_sent_separately": True,
                    "payload_single_line": True,
                    "transport_contract": "literal-write-then-explicit-enter",
                }
            ),
        },
    )


def queue(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    text: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
) -> dict[str, Any]:
    """Send ordinary follow-up, gated on the provider being idle.

    Refuses while a turn is running rather than queueing optimistically.
    Kimi's own queue is the thing being honoured here: text typed during
    an active turn is not reliably queued, it is as likely to be consumed
    by whatever the TUI is showing, and a follow-up that lands mid-turn
    changes the running work instead of following it.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    payload = assert_artifact_free(text, field="text")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_QUEUE,
        binding=binding,
        turn_id=None,
        payload_sha256=canonical_sha256(payload),
        observation=observed,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        if observed["active_turn_id"] is not None:
            raise _Refusal(
                REFUSED_ACTIVE_TURN,
                f"turn {observed['active_turn_id']} is active; ordinary follow-up waits for "
                f"idle. A deliberate mid-turn message is a steer, requested explicitly",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], payload=payload, transport=transport)


def steer(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    turn_id: str,
    text: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
) -> dict[str, Any]:
    """Deliberately steer one exact active turn.

    Binds to ``turn_id`` and refuses if the observed turn is a different
    one or if nothing is running.  Without that binding a steer written
    for a turn that ended in the meantime would land in whatever turn
    started next -- arriving as an instruction about work it was never
    about.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    payload = assert_artifact_free(text, field="text")
    target_turn = _require_text(turn_id, field="turn_id")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_STEER,
        binding=binding,
        turn_id=target_turn,
        payload_sha256=canonical_sha256(payload),
        observation=observed,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        active = observed["active_turn_id"]
        if active is None:
            raise _Refusal(
                REFUSED_NO_ACTIVE_TURN,
                f"steer {operation_id} targets turn {target_turn} but the session was observed "
                f"idle; a steer with nothing to steer is refused, not downgraded to follow-up",
            )
        if active != target_turn:
            raise _Refusal(
                REFUSED_TURN_MISMATCH,
                f"steer {operation_id} targets turn {target_turn} but turn {active} is running; "
                f"the intended turn has already ended",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], payload=payload, transport=transport)


def control(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    command: str,
    advertised_commands: Sequence[str],
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
) -> dict[str, Any]:
    """Send a literal slash command, with an explicit separate Enter.

    ``advertised_commands`` is required and is the provider's own current
    capability list.  Nothing is hardcoded as universally supported --
    not even ``/compact`` -- because a command this build does not have
    is not a slash command to Kimi, it is a line of text, and sending it
    blind would post that text into the conversation as if it were a
    message.  An unadvertised command is a typed refusal with zero bytes
    written.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    payload = assert_artifact_free(command, field="command")
    if not payload.startswith("/"):
        raise NativeControlInvalid(
            f"control command {payload!r} does not start with '/'; ordinary text is sent "
            f"through queue() or steer(), which gate it correctly"
        )
    if isinstance(advertised_commands, (str, bytes)) or not isinstance(
        advertised_commands, Sequence
    ):
        # A bare string would pass a substring test and quietly advertise
        # every command that happens to be a prefix of another.
        raise NativeControlInvalid(
            "advertised_commands must be a sequence of command strings from the provider's "
            "own capability list, not a single string"
        )
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_CONTROL,
        binding=binding,
        turn_id=None,
        payload_sha256=canonical_sha256(payload),
        observation=observed,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        if payload not in set(advertised_commands):
            raise _Refusal(
                REFUSED_UNSUPPORTED_CONTROL,
                f"{payload!r} is not advertised by this session "
                f"({sorted(set(advertised_commands))}); sending it would post it as chat text",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], payload=payload, transport=transport)


def _validated_turn_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Require a well-formed, present observation before gating on it."""
    if not isinstance(observation, Mapping):
        raise NativeControlInvalid("observation must be a mapping built by turn_observation()")
    if observation.get("schema") != TURN_OBSERVATION_SCHEMA:
        raise NativeControlInvalid(
            f"observation schema must be {TURN_OBSERVATION_SCHEMA!r}; got "
            f"{observation.get('schema')!r}"
        )
    if "active_turn_id" not in observation:
        raise NativeControlInvalid(
            "observation must carry active_turn_id (None means observed idle); an absent key "
            "cannot be distinguished from an observation that was never made"
        )
    return dict(observation)


def record_observation(
    *,
    operation_id: str,
    observation: Mapping[str, Any],
    outcome: str,
) -> dict[str, Any]:
    """Record the provider-side fact that resolves a posted operation.

    This is the only way an operation becomes accepted, completed, or
    provider-refused.  It requires an observation naming this exact
    operation id, so nothing here can be satisfied by a general "the
    provider seems fine" signal.
    """
    if outcome not in {ACCEPTED, COMPLETED, REFUSED}:
        raise NativeControlInvalid(
            f"outcome must be one of {sorted({ACCEPTED, COMPLETED, REFUSED})}; got {outcome!r}"
        )
    evidence = _validated_provider_observation(observation, operation_id=operation_id)

    from_states = {
        ACCEPTED: frozenset({POSTED}),
        COMPLETED: frozenset({POSTED, ACCEPTED}),
        REFUSED: frozenset({POSTED, ACCEPTED}),
    }[outcome]
    extra: dict[str, Any] = {"observation_json": _canonical(evidence)}
    if outcome == REFUSED:
        extra["refusal_reason"] = REFUSED_PROVIDER
    return _update(
        operation_id=operation_id,
        from_states=from_states,
        to_state=outcome,
        extra=extra,
    )


def _validated_provider_observation(
    observation: Mapping[str, Any], *, operation_id: str
) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise NativeControlInvalid("observation must be a mapping built by provider_observation()")
    if observation.get("schema") != PROVIDER_OBSERVATION_SCHEMA:
        raise NativeControlInvalid(
            f"observation schema must be {PROVIDER_OBSERVATION_SCHEMA!r}; got "
            f"{observation.get('schema')!r}"
        )
    if observation.get("operation_id") != operation_id:
        raise NativeControlInvalid(
            f"observation names operation {observation.get('operation_id')!r} but was offered "
            f"for {operation_id!r}; evidence must name the exact operation it resolves"
        )
    return dict(observation)


def mark_ambiguous(*, operation_id: str, reason: str) -> dict[str, Any]:
    """Freeze an operation whose outcome cannot be known.

    Reachable from every pre-resolution state, including ``intended``: a
    crash between journaling and typing is exactly as unknown as a lost
    response after it.  Ambiguity is idempotent so a repeated report does
    not conflict, and it never overwrites a resolved outcome.
    """
    detail = _require_text(reason, field="reason")
    try:
        return _update(
            operation_id=operation_id,
            from_states=frozenset({INTENDED, POSTED, ACCEPTED, AMBIGUOUS}),
            to_state=AMBIGUOUS,
            extra={"ambiguity_reason": detail},
        )
    except NativeControlConflict as exc:
        raise NativeControlConflict(
            f"operation {operation_id} is already resolved and cannot become ambiguous: {exc}"
        ) from exc


def reconcile(
    *,
    operation_id: str,
    observation: Mapping[str, Any],
    outcome: str,
) -> dict[str, Any]:
    """Resolve an ambiguous operation by exact-id evidence only.

    The single lawful exit from ambiguity, and deliberately not a retry:
    it takes evidence and changes a record, and it never sends anything.
    A caller that cannot obtain evidence naming this operation leaves it
    ambiguous, which keeps the session blocked -- the correct outcome,
    because the alternative is guessing whether the earlier input landed.
    """
    if outcome not in {ACCEPTED, COMPLETED, REFUSED}:
        raise NativeControlInvalid(
            f"outcome must be one of {sorted({ACCEPTED, COMPLETED, REFUSED})}; got {outcome!r}"
        )
    evidence = _validated_provider_observation(observation, operation_id=operation_id)
    extra: dict[str, Any] = {"observation_json": _canonical(evidence)}
    if outcome == REFUSED:
        extra["refusal_reason"] = REFUSED_PROVIDER
    return _update(
        operation_id=operation_id,
        from_states=frozenset({AMBIGUOUS}),
        to_state=outcome,
        extra=extra,
    )


def get(operation_id: str) -> Optional[dict[str, Any]]:
    """Return one operation, or ``None`` if the id is unknown."""
    try:
        with database.SessionLocal() as db:
            row = _fetch(db, _require_text(operation_id, field="operation_id"))
            return None if row is None else _row_dict(row)
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read control operation: {exc}") from exc


def unresolved_ambiguity(native_session_id: str) -> Optional[dict[str, Any]]:
    """Return the ambiguous operation blocking a session, if any.

    Exposed so a caller can see *why* it is blocked and go find the
    evidence, rather than discovering the block only as a refusal.
    """
    try:
        with database.SessionLocal() as db:
            row = (
                db.query(database.KimiNativeControlOperationModel)
                .filter(
                    database.KimiNativeControlOperationModel.native_session_id
                    == _require_text(native_session_id, field="native_session_id"),
                    database.KimiNativeControlOperationModel.state == AMBIGUOUS,
                )
                .first()
            )
            return None if row is None else _row_dict(row)
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read control operations: {exc}") from exc
