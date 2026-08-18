"""Admission delivery for a managed Muse native-TUI session.

A sibling of the Kimi and Claude native control adapters, restricted to
the one operation the managed-v2 admission path needs: the ``queue``
operation that delivers an admitted task into an idle Muse composer
exactly once.  Muse's composer facts on the installed 0.1.0-R708.1 build
are read from the binary's own keymap strings: ``Enter`` submits the
composer message, and ``ctrl+j`` (also ``shift+enter``/``ctrl+m``) inserts
a newline without submitting.  ``ctrl+j`` is pinned because it is a single
control byte every terminal transmits identically, whereas the shift/alt
chords are terminal keybindings a managed pane cannot assume.

Deliberately *not* a generalisation of the Claude adapter: the schemas,
refusal reasons, and composer facts are Muse's own, and a Claude or Kimi
record must never satisfy a Muse check.  What is shared is the discipline
— intent journaled before any keystroke, ambiguity recorded rather than
retried, transport truth never read as provider truth.

The adapter is resolved by the managed-v2 admission path only
(``managed_launch_v2._admission_control_adapter``).  The generic
``/control-input`` surface resolves through ``native_control_adapter``,
which stays closed for ``muse_cli``: steer chords, slash controls, and
operator messages for Muse are unproven facts on the installed build, so
those kinds do not exist here and the surface refuses them.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional, Protocol, cast

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import (
    installed_bundle_facts,
    native_attachment,
    provider_contracts,
)
from cli_agent_orchestrator.services.canonical_json import canonical_sha256
from cli_agent_orchestrator.services.execution_mode import NATIVE_TUI

logger = logging.getLogger(__name__)

PROVIDER = "muse_cli"

#: The executable name, which is what the version-pin tables are keyed by.
PROVIDER_EXECUTABLE = provider_contracts.PROVIDER_MUSE

RECORD_SCHEMA = "cao-muse-native-control-v1"
INTENT_SCHEMA = "cao-muse-native-control-intent-v1"
TURN_OBSERVATION_SCHEMA = "cao-muse-native-turn-observation-v1"
PROVIDER_OBSERVATION_SCHEMA = "cao-muse-native-control-observation-v1"
KEYSTROKE_PLAN_SCHEMA = "cao-muse-native-keystroke-plan-v1"

KIND_QUEUE = "queue"
#: The closed vocabulary of this adapter.  Muse steer/chord, slash-control,
#: and operator-message facts are unproven on the installed build, so no
#: other kind may be opened — the generic control surface stays closed for
#: Muse because there is nothing here it could safely do.
KINDS = (KIND_QUEUE,)

INTENDED = "intended"
POSTED = "posted"
ACCEPTED = "accepted"
COMPLETED = "completed"
REFUSED = "refused"
AMBIGUOUS = "ambiguous"

#: States an operation may stop in. ``AMBIGUOUS`` is deliberately absent:
#: an operation that may or may not have been delivered is not finished,
#: it is *unresolved*, and treating it as terminal is how a duplicate gets
#: authorised.
RESOLVED_STATES = frozenset({COMPLETED, REFUSED})

REFUSED_ACTIVE_TURN = "active_turn_in_progress"
REFUSED_NO_ACTIVE_TURN = "no_active_turn"
REFUSED_TURN_MISMATCH = "turn_mismatch"
REFUSED_UNSUPPORTED_CONTROL = "unsupported_control"
REFUSED_ATTACHMENT = "attachment_not_owned"
REFUSED_UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"
REFUSED_UNPROVEN_COMPOSER_NEWLINE = "composer_newline_unproven"

#: Never permitted inside literal composer text. ESC would let a payload
#: synthesise its own escape sequences; CR and LF would submit at a point
#: the caller did not choose. Line breaks are composer *keystrokes* here,
#: never characters in the literal write.
_FORBIDDEN_CHARACTERS = ("\x1b", "\r", "\n")

ENCODING_SINGLE_LINE = "single-line-then-enter"
ENCODING_SOFT_NEWLINE = "soft-newline-lines-then-enter"

#: Per-build composer facts, read from the installed binary's own keymap
#: strings.  Keyed by exact version, never a range — which key inserts a
#: newline is a fact about one build, and a range would assert it about
#: builds nobody read.
#:
#: The installed 0.1.0-R708.1 binary advertises ``submit`` on ``enter``
#: ("Submit the composer message") and ``newline`` on ``shift+enter`` /
#: ``ctrl+j`` / ``ctrl+m`` ("Insert a newline without submitting").  ``C-j``
#: is pinned because it is a single control byte every terminal transmits
#: identically, while the shift chord is a terminal keybinding a managed
#: pane cannot assume.  Whether Muse alters the composer's contents on
#: submit was NOT established by reading the build, so
#: ``submit_normalization_proven`` is false and the plan only states what
#: the model received when the answer does not depend on it.  An Enter
#: that arrives before the renderer is ready is not known to be swallowed
#: on this build, so no submit settle is asserted; the idle gate before
#: the queue and the composer-visible submit observation are the
#: submission barriers here.
_PROVEN_COMPOSER_NEWLINE: dict[str, dict[str, Any]] = {
    "0.1.0": {
        "keystroke": "C-j",
        "submit_settle_seconds": 0.0,
        "evidence": {
            "binary_version": "0.1.0-R708.1",
            "keymap_submit": "enter: Submit the composer message",
            "keymap_newline": "ctrl+j: Insert a newline without submitting",
        },
        "submit_normalization_proven": False,
    },
}

#: The settle an unproven build waits before its Enter. A missing pin
#: selects the safe end of the observed range — the longest proven
#: interval for this provider — which for Muse is ``0.0``: zero is this
#: provider's *proven* value, not a null placeholder, because no Enter
#: swallow has been observed on the build that was read. If a future
#: proven build ever requires a settle, this floor rises with the table.
#: The plan marks the value with ``submit_settle_proven: False`` so a
#: receipt reader can tell this floor from a measurement.
_SUBMIT_SETTLE_FLOOR_SECONDS = max(
    float(entry["submit_settle_seconds"]) for entry in _PROVEN_COMPOSER_NEWLINE.values()
)


class NativeControlError(RuntimeError):
    """Base class for every Muse native control failure."""


class NativeControlInvalid(NativeControlError):
    """The request could not be attempted; nothing was written."""


class NativeControlConflict(NativeControlError):
    """The request collided with an existing operation or state."""


class NativeControlNotFound(NativeControlError):
    """The named operation does not exist."""


class NativeControlUnavailable(NativeControlError):
    """A store this adapter needs could not be reached."""


class NativeControlTransport(Protocol):
    """Literal input into the exact composer, with no semantics of its own."""

    def send_literal(self, text: str) -> None:
        """Write payload text; must never submit and never contain a break."""
        ...

    def send_enter(self) -> None:
        """Send the submitting key; writes no payload."""
        ...

    def send_key(self, keystroke: str) -> None:
        """Send one named composer key (e.g. ``C-j``)."""
        ...


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    import json

    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeControlInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def assert_artifact_free(text: str, *, field: str = "text") -> str:
    """Refuse a payload carrying bytes that would stop being payload."""
    if not isinstance(text, str):
        raise NativeControlInvalid(f"{field} must be a string; got {type(text).__name__}")
    for forbidden in _FORBIDDEN_CHARACTERS:
        if forbidden in text:
            raise NativeControlInvalid(
                f"{field} contains {forbidden!r}, which must never reach a Muse composer; "
                "native control types literal lines and sends composer keystrokes for "
                "the breaks between them"
            )
    return text


def split_submission_terminator(text: str) -> tuple[str, Optional[str]]:
    """Separate a single trailing line terminator from the content.

    A caller that ends a message with a newline means "and then submit",
    which is the Enter this adapter sends explicitly. Typing the newline
    *and* sending Enter would be two submissions.
    """
    for terminator in ("\r\n", "\n", "\r"):
        if text.endswith(terminator):
            return text[: -len(terminator)], terminator
    return text, None


def plan_composer_keystrokes(
    text: str,
    *,
    provider_version: Optional[str] = None,
    field: str = "text",
) -> dict[str, Any]:
    """Decide every keystroke before one is sent.

    Built whole and up front so it can be journaled before any composer
    I/O, and so an undeliverable payload is refused with nothing typed
    rather than discovered half way through a message.

    ``payload_sha256`` covers the caller's original bytes and no encoding
    decision here can change it; ``composer_sha256`` is what the composer
    will hold at submit.  ``model_input_sha256`` is stated only when this
    build's submit-time normalization cannot change the answer — this
    build's normalization is unproven, so the digest is recorded only for
    payloads invariant under trimming.

    Newlines are *structure*, never literal input: the payload is split
    into lines and the breaks become the pinned C-j composer keystrokes, so
    the whole payload is typed as literal lines with C-j between them and
    one final Enter — never flattened, pasted, split across turns, or sent
    with a second Enter.  ESC and CR stay prohibited inside every line.
    """
    if not isinstance(text, str):
        raise NativeControlInvalid(f"{field} must be a string; got {type(text).__name__}")

    content, terminator = split_submission_terminator(text)
    if not content:
        raise NativeControlInvalid(
            f"{field} is only a line terminator, so there is no content to deliver; "
            f"the terminator is the submit keystroke, not a message"
        )

    lines = content.split("\n")
    for forbidden in _FORBIDDEN_CHARACTERS:
        if forbidden == "\n":
            continue
        if forbidden in content:
            raise NativeControlInvalid(
                f"{field} contains {forbidden!r}, which must never reach a provider "
                f"composer; native control types literal lines and sends composer "
                f"keystrokes for the breaks between them"
            )

    pin = _PROVEN_COMPOSER_NEWLINE.get(
        provider_contracts.normalized_version(provider_version or "")
    )
    hint = None
    if pin is None:
        # The §3 override: the keymap fact the pin recorded as evidence is
        # read from the installed inner binary of the exact build being
        # driven.  Only a build whose binary yields no hint falls through
        # to the refusal.
        hint = installed_bundle_facts.newline_keystroke_hint(PROVIDER, provider_version)
    undeliverable = None
    if len(lines) > 1 and pin is None and hint is None:
        undeliverable = (
            f"{field} spans {len(lines)} lines but no composer newline keystroke is proven "
            f"for provider version {provider_version!r} and none could be read from the "
            f"installed bundle; refusing rather than splitting the "
            f"message across turns, pasting it, or flattening the newlines out of it"
        )

    composer_image = "\n".join(lines)
    trim_invariant = _is_trim_invariant(composer_image)
    normalization_proven = bool(pin and pin.get("submit_normalization_proven"))
    keystroke = pin["keystroke"] if pin is not None else (hint.keystroke if hint else None)
    evidence = pin["evidence"] if pin is not None else None
    if pin is None and hint is not None:
        evidence = {
            "bundle_read": hint.description,
            "bundle_path": hint.bundle_path,
            "bundle_version": hint.bundle_version,
        }

    plan: dict[str, Any] = {
        "schema": KEYSTROKE_PLAN_SCHEMA,
        "encoding": ENCODING_SINGLE_LINE if len(lines) == 1 else ENCODING_SOFT_NEWLINE,
        "payload_sha256": canonical_sha256(text),
        "composer_sha256": canonical_sha256(composer_image),
        "model_input_sha256": (canonical_sha256(composer_image) if trim_invariant else None),
        "model_input_is_composer_exact": True if trim_invariant else None,
        "submit_normalization_proven": normalization_proven,
        "composer_image_trim_invariant": trim_invariant,
        "provider_normalization": None,
        "line_count": len(lines),
        "trailing_terminator": terminator,
        "provider_version": provider_version,
        "soft_newline_keystroke": keystroke,
        "submit_settle_seconds": (
            _SUBMIT_SETTLE_FLOOR_SECONDS if pin is None else float(pin["submit_settle_seconds"])
        ),
        # A floor is not a measurement. False names "the longest proven
        # interval for this provider, because nothing is proven for this
        # build"; True names "proven on this exact build". The value
        # alone cannot say which, and a receipt that read a floor as
        # evidence would be asserting something nobody observed.
        "submit_settle_proven": pin is not None,
        # Where the newline keystroke came from: the stage-verified
        # per-build table, a runtime read of the installed bundle, or
        # nowhere (the refusal case).
        "composer_keystroke_source": (
            "proven-build-table"
            if pin is not None
            else ("installed-bundle-hint" if hint is not None else None)
        ),
        "composer_evidence": evidence,
        "final_enter": True,
        "deliverable": undeliverable is None,
        "undeliverable_reason": undeliverable,
    }
    plan["plan_sha256"] = canonical_sha256(_canonical(plan))
    plan["lines"] = lines
    return plan


def _journalable_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The plan without the message text, for durable storage."""
    return {key: value for key, value in plan.items() if key != "lines"}


def _is_trim_invariant(text: str) -> bool:
    return text == text.strip()


def turn_observation(
    *,
    active_turn_id: Optional[str],
    observed_at: str,
    observer: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """A schema-bound statement about what the pane was doing.

    ``active_turn_id`` may be ``None``, which means "observed idle" and
    never "did not look".  The keyword is ``observer`` to match the Kimi
    and Claude adapters exactly: a caller that dispatches between them by
    provider passes the same keywords to all of them.
    """
    return {
        "schema": TURN_OBSERVATION_SCHEMA,
        "provider": PROVIDER,
        "active_turn_id": active_turn_id,
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
        "evidence": dict(evidence or {}),
    }


def provider_observation(
    *,
    state: str,
    observed_at: str,
    observer: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """What the provider was seen to do with an operation, after the write."""
    if state not in (ACCEPTED, COMPLETED, REFUSED):
        raise NativeControlInvalid(
            f"provider observation state must be one of "
            f"{[ACCEPTED, COMPLETED, REFUSED]}; got {state!r}"
        )
    return {
        "schema": PROVIDER_OBSERVATION_SCHEMA,
        "provider": PROVIDER,
        "state": state,
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
        "evidence": dict(evidence or {}),
    }


def _validated_turn_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise NativeControlInvalid("turn observation must be a mapping")
    if observation.get("schema") != TURN_OBSERVATION_SCHEMA:
        raise NativeControlInvalid(
            f"turn observation must carry schema {TURN_OBSERVATION_SCHEMA!r}; "
            f"got {observation.get('schema')!r}. A Claude or Kimi observation is not a "
            f"Muse observation, and neither is an unlabelled dict"
        )
    if observation.get("provider") != PROVIDER:
        raise NativeControlInvalid(
            f"turn observation is for provider {observation.get('provider')!r}, not {PROVIDER!r}"
        )
    active = observation.get("active_turn_id")
    if active is not None and not isinstance(active, str):
        raise NativeControlInvalid("active_turn_id must be a string or None")
    return dict(observation)


def _validated_provider_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise NativeControlInvalid("provider observation must be a mapping")
    if observation.get("schema") != PROVIDER_OBSERVATION_SCHEMA:
        raise NativeControlInvalid(
            f"provider observation must carry schema {PROVIDER_OBSERVATION_SCHEMA!r}; "
            f"got {observation.get('schema')!r}"
        )
    if observation.get("provider") != PROVIDER:
        raise NativeControlInvalid(
            f"provider observation is for {observation.get('provider')!r}, not {PROVIDER!r}"
        )
    state = observation.get("state")
    if state not in (ACCEPTED, COMPLETED, REFUSED):
        raise NativeControlInvalid(f"provider observation state {state!r} is not recordable")
    return dict(observation)


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
    keystroke_plan: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": INTENT_SCHEMA,
        "provider": PROVIDER,
        "kind": kind,
        "operation_id": operation_id,
        "native_session_id": native_session_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": execution_mode,
        "turn_id": turn_id,
        "payload_sha256": payload_sha256,
        "turn_observation": dict(turn_observation_record),
        "keystroke_plan": None if keystroke_plan is None else _journalable_plan(keystroke_plan),
        "intended_at": _now(),
    }


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA,
        "provider": PROVIDER,
        "operation_id": row.operation_id,
        "kind": row.kind,
        "state": row.state,
        "native_session_id": row.native_session_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "execution_mode": row.execution_mode,
        "turn_id": row.turn_id,
        "payload_sha256": row.payload_sha256,
        "intent": _parse_json(row.intent_json),
        "transport": _parse_json(row.transport_json),
        "observation": _parse_json(row.observation_json),
        "posted_at": row.posted_at,
        "refusal_reason": row.refusal_reason,
        "ambiguity_reason": row.ambiguity_reason,
        "epoch": row.epoch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        # Derived, not stored: "did bytes reach the pane" and "did the
        # provider accept them" are different questions and a reader must
        # not be able to answer the second from the first.
        "posted": row.state in (POSTED, ACCEPTED, COMPLETED),
        "provider_accepted": row.state in (ACCEPTED, COMPLETED),
    }


def _fetch(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.MuseNativeControlOperationModel)
        .filter(database.MuseNativeControlOperationModel.operation_id == operation_id)
        .first()
    )


class _Refusal(Exception):
    """A typed refusal, recorded durably rather than raised to the caller."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _validate_binding(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> dict[str, Any]:
    binding = {
        "operation_id": _require_text(operation_id, field="operation_id"),
        "native_session_id": _require_text(native_session_id, field="native_session_id"),
        "terminal_id": _require_text(terminal_id, field="terminal_id"),
        "generation": _require_text(generation, field="generation"),
        "execution_mode": _require_text(execution_mode, field="execution_mode"),
    }
    if binding["execution_mode"] != NATIVE_TUI:
        raise NativeControlInvalid(
            f"muse native control operates only in {NATIVE_TUI!r}; "
            f"got {binding['execution_mode']!r}"
        )
    return binding


def _assert_same_request(row: Any, *, kind: str, binding: Mapping[str, Any]) -> None:
    """A replay must be the same request, not merely the same id.

    Reusing an operation id for different content would make at-most-once
    delivery mean nothing: the second request would be silently answered
    with the first one's outcome.
    """
    mismatches = []
    if row.kind != kind:
        mismatches.append(f"kind {row.kind!r} != {kind!r}")
    for field in ("native_session_id", "terminal_id", "generation", "execution_mode", "turn_id"):
        if getattr(row, field) != binding.get(field):
            mismatches.append(f"{field} {getattr(row, field)!r} != {binding.get(field)!r}")
    if row.payload_sha256 != binding.get("payload_sha256"):
        mismatches.append("payload digest differs")
    if mismatches:
        raise NativeControlConflict(
            f"operation {row.operation_id} already exists as a different request "
            f"({'; '.join(mismatches)}); an operation id is not reusable"
        )


def _assert_session_unblocked(*, native_session_id: str, operation_id: str) -> None:
    """Refuse every new operation while one is unresolved."""
    blocking = unresolved_ambiguity(native_session_id)
    if blocking is not None and blocking["operation_id"] != operation_id:
        raise _Refusal(
            REFUSED_UNRESOLVED_AMBIGUITY,
            f"operation {blocking['operation_id']} is unresolved for session "
            f"{native_session_id} ({blocking['ambiguity_reason']}); reconcile it with "
            f"evidence before sending anything else",
        )


def _assert_attachment_owner(
    *,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> None:
    """Only this generation's attachment may write to this session."""
    try:
        attachment = native_attachment.get(PROVIDER, native_session_id)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"could not read the native attachment for {native_session_id}: {exc}",
        ) from exc
    if attachment is None:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"no native attachment for session {native_session_id}; nothing may be written "
            f"to a session this process does not hold",
        )
    owner = attachment.get("owner") or {}
    expected = (terminal_id, generation, execution_mode)
    actual = (owner.get("terminal_id"), owner.get("generation"), owner.get("execution_mode"))
    if actual != expected:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"session {native_session_id} is held by {actual}, not {expected}; a superseded "
            f"generation never writes to its successor's session",
        )


def _assert_deliverable(plan: Mapping[str, Any]) -> None:
    """Refuse a payload this build cannot be made to accept.

    Checked after ownership, because "you do not hold this session" is
    the more fundamental answer, and before the idle gate, because a busy
    provider is transient while an unproven keystroke is permanent.
    """
    if not plan.get("deliverable", True):
        raise _Refusal(
            REFUSED_UNPROVEN_COMPOSER_NEWLINE,
            cast(str, plan["undeliverable_reason"]),
        )


def _open(
    *,
    kind: str,
    binding: Mapping[str, Any],
    turn_id: Optional[str],
    payload_sha256: str,
    observation: Mapping[str, Any],
    keystroke_plan: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], bool]:
    """Journal the intent, or return the existing operation unchanged.

    Returns ``(record, is_new)``. ``is_new`` is False for a replay, and a
    replay never reaches the transport — that is where at-most-once
    lives. The row is committed before this returns, so a crash on the
    very next instruction still leaves the intent durable.
    """
    operation_id = cast(str, binding["operation_id"])
    row_values = {
        **binding,
        "provider": PROVIDER,
        "kind": kind,
        "state": INTENDED,
        "turn_id": turn_id,
        "payload_sha256": payload_sha256,
        "intent_json": _canonical(
            _intent(
                kind=kind,
                operation_id=operation_id,
                native_session_id=cast(str, binding["native_session_id"]),
                terminal_id=cast(str, binding["terminal_id"]),
                generation=cast(str, binding["generation"]),
                execution_mode=cast(str, binding["execution_mode"]),
                turn_id=turn_id,
                payload_sha256=payload_sha256,
                turn_observation_record=observation,
                keystroke_plan=keystroke_plan,
            )
        ),
        "epoch": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    identity = {**binding, "turn_id": turn_id, "payload_sha256": payload_sha256}

    try:
        with database.SessionLocal() as db:
            existing = _fetch(db, operation_id)
            if existing is not None:
                _assert_same_request(existing, kind=kind, binding=identity)
                return _row_dict(existing), False
            db.add(database.MuseNativeControlOperationModel(**row_values))
            db.commit()
            return _row_dict(_fetch(db, operation_id)), True
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        try:
            with database.SessionLocal() as db:
                existing = _fetch(db, operation_id)
                if existing is not None:
                    _assert_same_request(existing, kind=kind, binding=identity)
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
                db.query(database.MuseNativeControlOperationModel)
                .filter(
                    database.MuseNativeControlOperationModel.operation_id == operation_id,
                    database.MuseNativeControlOperationModel.epoch == observed_epoch,
                )
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


class ComposerWriteInterrupted(NativeControlError):
    """A keystroke plan stopped part-way, so what landed is not known."""

    def __init__(self, detail: str, *, enter_attempted: bool) -> None:
        super().__init__(detail)
        self.detail = detail
        self.enter_attempted = enter_attempted


def execute_composer_plan(
    *,
    plan: Mapping[str, Any],
    transport: NativeControlTransport,
    submit: bool = True,
    deadline_monotonic: Optional[float] = None,
) -> dict[str, Any]:
    """Type one already-planned payload into a composer, then submit it.

    Exactly one Enter is sent, ever, for exactly one turn.  The breaks
    inside a multi-line payload are composer keystrokes (``C-j``), not
    submissions, so a message with ten newlines is still one turn.

    Raises:
        NativeControlInvalid: The plan is undeliverable.  Nothing typed.
        ComposerWriteInterrupted: The transport raised part-way through.
    """
    if not plan.get("deliverable", True):
        raise NativeControlInvalid(
            f"refusing to type an undeliverable plan: {plan['undeliverable_reason']}"
        )
    lines = list(plan["lines"])
    newline_key = plan["soft_newline_keystroke"]
    index = 0

    try:
        for index, line in enumerate(lines):
            if index:
                # The break comes first, so a blank content line is a
                # break with nothing typed after it rather than a special
                # case that could be silently dropped.
                transport.send_key(newline_key)
            if line:
                transport.send_literal(line)
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        raise ComposerWriteInterrupted(
            f"transport raised while typing line {index + 1} of {len(lines)}: {exc}; "
            f"no Enter was sent, so the composer may hold a partial message",
            enter_attempted=False,
        ) from exc

    if not submit:
        return {"lines_typed": len(lines), "enter_sent": False}

    settle = float(plan.get("submit_settle_seconds") or 0.0)
    if settle > 0:
        remaining = None if deadline_monotonic is None else deadline_monotonic - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise ComposerWriteInterrupted(
                "the payload was typed but the overall write deadline expired before "
                "the submit settle; the composer may hold unsubmitted text",
                enter_attempted=False,
            )
        if remaining is not None and settle > remaining:
            time.sleep(remaining)
            raise ComposerWriteInterrupted(
                "the payload was typed but the overall write deadline expired during "
                "the submit settle; the composer may hold unsubmitted text",
                enter_attempted=False,
            )
        time.sleep(settle)

    try:
        transport.send_enter()
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        raise ComposerWriteInterrupted(
            f"payload was written but the submitting Enter raised: {exc}; "
            f"the composer may hold unsubmitted text",
            enter_attempted=True,
        ) from exc

    return {"lines_typed": len(lines), "enter_sent": True}


def _post(
    *,
    operation_id: str,
    plan: Mapping[str, Any],
    transport: NativeControlTransport,
) -> dict[str, Any]:
    """Execute the keystroke plan, then record what this adapter did.

    Any transport failure, at any boundary, becomes ambiguous rather than
    failed. A raised exception does not prove the bytes did not land, and
    treating it as proof of non-delivery is precisely what would justify a
    retry and produce a duplicate.
    """
    try:
        execute_composer_plan(plan=plan, transport=transport)
    except ComposerWriteInterrupted as exc:
        return mark_ambiguous(operation_id=operation_id, reason=exc.detail)

    return _update(
        operation_id=operation_id,
        from_states=frozenset({INTENDED}),
        to_state=POSTED,
        extra={
            "posted_at": _now(),
            "transport_json": _canonical(
                {
                    "enter_sent_separately": True,
                    "enter_count": 1,
                    "transport_contract": "literal-lines-composer-breaks-then-explicit-enter",
                    "encoding": plan["encoding"],
                    "plan_sha256": plan["plan_sha256"],
                    "payload_sha256": plan["payload_sha256"],
                    "composer_sha256": plan["composer_sha256"],
                    "model_input_sha256": plan["model_input_sha256"],
                    "model_input_is_composer_exact": plan["model_input_is_composer_exact"],
                    "submit_normalization_proven": plan["submit_normalization_proven"],
                    "composer_image_trim_invariant": plan["composer_image_trim_invariant"],
                    "line_count": plan["line_count"],
                    "payload_single_line": plan["line_count"] == 1,
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
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Ordinary first delivery, gated on the provider being idle.

    The only operation kind this adapter opens — it is the managed-v2
    admission's queue.  Refuses while a turn is running rather than
    queueing optimistically, and refuses anything whose composer
    keystrokes this build cannot prove.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    plan = plan_composer_keystrokes(text, provider_version=provider_version, field="text")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_QUEUE,
        binding=binding,
        turn_id=None,
        payload_sha256=plan["payload_sha256"],
        observation=observed,
        keystroke_plan=plan,
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
        _assert_deliverable(plan)
        if observed["active_turn_id"] is not None:
            raise _Refusal(
                REFUSED_ACTIVE_TURN,
                f"turn {observed['active_turn_id']} is active; ordinary delivery waits for "
                f"idle. A deliberate mid-turn message is a steer, which Muse cannot prove "
                f"and is refused",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def record_observation(
    *,
    operation_id: str,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Record what the provider was seen to do with an already-posted write.

    Separate from posting on purpose: ``posted`` is this adapter's own
    account of bytes it wrote, and provider acceptance is somebody else's
    observation of the provider.
    """
    validated = _validated_provider_observation(observation)
    return _update(
        operation_id=operation_id,
        from_states=frozenset({POSTED, ACCEPTED}),
        to_state=validated["state"],
        extra={"observation_json": _canonical(validated)},
    )


def mark_ambiguous(*, operation_id: str, reason: str) -> dict[str, Any]:
    """Record that an operation may or may not have been delivered.

    Reachable from any pre-resolution state, because uncertainty can
    arrive at any point — including after a post, when a later reading
    fails to confirm what happened. It is never reachable *out of* by a
    retry; only :func:`reconcile` resolves it.
    """
    return _update(
        operation_id=operation_id,
        from_states=frozenset({INTENDED, POSTED, ACCEPTED}),
        to_state=AMBIGUOUS,
        extra={"ambiguity_reason": _require_text(reason, field="reason")},
    )


def reconcile(
    *,
    operation_id: str,
    resolution: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one ambiguous operation with evidence naming it exactly."""
    if resolution not in RESOLVED_STATES:
        raise NativeControlInvalid(
            f"reconcile resolution must be one of {sorted(RESOLVED_STATES)}; got {resolution!r}"
        )
    if not isinstance(evidence, Mapping):
        raise NativeControlInvalid("reconcile evidence must be a mapping")
    if evidence.get("operation_id") != operation_id:
        raise NativeControlInvalid(
            f"reconcile evidence must name operation {operation_id!r}; it names "
            f"{evidence.get('operation_id')!r}"
        )
    return _update(
        operation_id=operation_id,
        from_states=frozenset({AMBIGUOUS}),
        to_state=resolution,
        extra={"observation_json": _canonical(dict(evidence))},
    )


def get(operation_id: str) -> Optional[dict[str, Any]]:
    try:
        with database.SessionLocal() as db:
            row = _fetch(db, operation_id)
            return None if row is None else _row_dict(row)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read control operation: {exc}") from exc


def unresolved_ambiguity(native_session_id: str) -> Optional[dict[str, Any]]:
    """The oldest unresolved operation for a session, if any."""
    try:
        with database.SessionLocal() as db:
            row = (
                db.query(database.MuseNativeControlOperationModel)
                .filter(
                    database.MuseNativeControlOperationModel.native_session_id == native_session_id,
                    database.MuseNativeControlOperationModel.state == AMBIGUOUS,
                )
                .order_by(database.MuseNativeControlOperationModel.created_at)
                .first()
            )
            return None if row is None else _row_dict(row)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read session ambiguity: {exc}") from exc
