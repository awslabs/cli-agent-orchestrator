"""Admission and control for a managed Claude native-TUI session.

A sibling of the Kimi native control adapter, not a generalisation of it.
That module says outright that its provider is named rather than
parameterised and that a second provider needs its own adapter, and this
is that adapter. The reason is not tidiness: the two providers differ in
the facts that decide whether a keystroke arrives — which key inserts a
composer newline, how long the renderer needs before an Enter counts as
a submit, what the provider does to the text at submit time — and a
shared implementation would have to hold those as data and would
eventually apply one provider's facts to the other. The schemas are
distinct for the same reason: a Kimi record must not be able to satisfy
a Claude check.

What is shared is the discipline, and it is the whole point of the
module:

* **At most once.** The intent is journaled, committed, before any
  keystroke. A replay finds the existing row and returns it without
  reaching the transport, so a retried request cannot type twice.
* **Ambiguity is not failure.** Any exception from the transport means
  the bytes may have landed. That is recorded as ambiguous and the
  session is blocked, because a retry is exactly what "it might have
  sent" does not license. Only :func:`reconcile`, with evidence naming
  the exact operation, resolves it.
* **Transport truth is not provider truth.** ``posted`` says bytes were
  written. Whether Claude accepted them is a separate observation
  recorded separately, because deriving one from the other is how a
  system starts reporting deliveries that never happened.
* **Nothing is inferred from a caller's word.** Turn state arrives as an
  observation record with its own schema; a boolean parameter would be
  the caller asserting the very thing being checked.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple, cast

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import (
    installed_bundle_facts,
    native_attachment,
    provider_contracts,
)
from cli_agent_orchestrator.services.canonical_json import canonical_sha256
from cli_agent_orchestrator.services.execution_mode import NATIVE_TUI

logger = logging.getLogger(__name__)

#: The canonical provider key, as published on every shared surface.
#: ``claude`` is the *executable*; the provider is ``claude_code``.
PROVIDER = "claude_code"

#: The executable name, which is what the version-pin tables are keyed by.
PROVIDER_EXECUTABLE = provider_contracts.PROVIDER_CLAUDE

RECORD_SCHEMA = "cao-claude-native-control-v1"
INTENT_SCHEMA = "cao-claude-native-control-intent-v1"
TURN_OBSERVATION_SCHEMA = "cao-claude-native-turn-observation-v1"
PROVIDER_OBSERVATION_SCHEMA = "cao-claude-native-control-observation-v1"
KEYSTROKE_PLAN_SCHEMA = "cao-claude-native-keystroke-plan-v1"

KIND_QUEUE = "queue"
KIND_STEER = "steer"
KIND_CONTROL = "control"
#: Lane C (design §8.3): one text+image operator message, journaled and
#: gated exactly like a queue operation but replaying on the caller's
#: whole-request digest.
KIND_OPERATOR_MESSAGE = "operator-message"
KINDS = (KIND_QUEUE, KIND_STEER, KIND_CONTROL, KIND_OPERATOR_MESSAGE)

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
#: Lane C: the operator-message pre-write hook refused at the last safe
#: pre-write point (after the journal claim and every deliverability/idle
#: gate, immediately before the transport write), so zero bytes were
#: typed.  These name the image-attachment store's answers there; the
#: word "attachment" alone already means provider-session ownership in
#: this module (``REFUSED_ATTACHMENT``).
REFUSED_IMAGE_UNKNOWN = "image_attachment_unknown"
REFUSED_IMAGE_NOT_READY = "image_attachment_not_ready"

#: Slash commands this adapter will type. Held as an allow-list because a
#: slash command is indistinguishable from message text at the transport,
#: so anything not named here would be delivered to the model as literal
#: text that merely looks like a command.
CONTROL_COMPACT = "/compact"
ADVERTISED_CONTROLS = (CONTROL_COMPACT,)

#: Never permitted inside literal composer text. ESC would let a payload
#: synthesise its own escape sequences; CR and LF would submit at a point
#: the caller did not choose. Line breaks are composer *keystrokes* here,
#: never characters in the literal write.
_FORBIDDEN_CHARACTERS = ("\x1b", "\r", "\n")

ENCODING_SINGLE_LINE = "single-line-then-enter"
ENCODING_SOFT_NEWLINE = "soft-newline-lines-then-enter"

#: Per-build composer facts, proven against the installed bundle. Keyed by
#: exact version, never a range — which key inserts a newline is a fact
#: about one build, and a range would assert it about builds nobody read.
#:
#: ``C-j`` is the pinned newline keystroke because the installed build
#: advertises it in its own composer hint list (``ctrl+j for newline``)
#: and because it is a single control byte every terminal transmits
#: identically. The alternatives that bundle also mentions —
#: ``shift+enter`` and ``option+enter`` — are *terminal keybindings that
#: Claude installs via ``/terminal-setup``, so whether they exist depends
#: on the user's terminal profile; a managed pane cannot assume them. The
#: two-key ``\`` + Return form is a text edit and a submit, not one
#: keystroke.
#:
#: ``submit_settle_seconds`` is not padding. The Ink renderer is already
#: known in this codebase to swallow an Enter that arrives too soon,
#: leaving the message sitting unsubmitted in the prompt box with no
#: error — see ``ClaudeCodeProvider.paste_submit_delay``, whose 2.0s was
#: chosen from that observed behaviour. The same figure is used here for
#: the same reason.
_PROVEN_COMPOSER_NEWLINE: dict[str, dict[str, Any]] = {
    "2.1.220": {
        "keystroke": "C-j",
        "submit_settle_seconds": 2.0,
        "evidence": {
            "bundle_sha256": ("8addc857f3fe64d5a0368af9ee50321b50afb4a6918ba3ef018ab84f5dbbe081"),
            "composer_hint": "ctrl+j for newline",
            "settle_source": "ClaudeCodeProvider.paste_submit_delay (observed Ink swallow)",
        },
        # Whether this build alters the composer's contents on submit was
        # NOT established by reading it. Recorded as unproven rather than
        # assumed either way: claiming "no normalization" would be an
        # assertion nobody verified, and claiming a trim would invent one.
        # The plan below only states what the model received when the
        # answer does not depend on it.
        "submit_normalization_proven": False,
    },
}

#: The settle an unproven build waits before its Enter. A missing pin
#: selects the safe end of the observed range — the longest proven
#: interval for this provider — never the null value: ``0.0`` is exactly
#: the timing the Ink note above documents as swallowing an Enter, so a
#: zero fallback would leave an unproven build's message sitting in the
#: prompt box unsubmitted, with no error. Waiting longer than a build
#: needs costs latency; waiting shorter loses the message silently. The
#: plan marks the value with ``submit_settle_proven: False`` so a
#: receipt reader can tell this floor from a measurement.
_SUBMIT_SETTLE_FLOOR_SECONDS = max(
    float(entry["submit_settle_seconds"]) for entry in _PROVEN_COMPOSER_NEWLINE.values()
)

#: Whitespace as JavaScript's own ``String.prototype.trim`` defines it.
#: Used only to decide whether a payload is *invariant* under trimming —
#: if it is, then whether Claude trims or not cannot change what the model
#: receives, and the question can be answered without having proven it.
_JS_TRIM_CHARACTERS = frozenset(
    "\t\n\x0b\x0c\r \xa0     　﻿" + "".join(chr(code) for code in range(0x2000, 0x200B))
)


class NativeControlError(RuntimeError):
    """Base class for every Claude native control failure."""


class NativeControlInvalid(NativeControlError):
    """The request could not be formed into a lawful operation."""


class NativeControlConflict(NativeControlError):
    """The operation exists but not in the state this call requires."""


class NativeControlNotFound(NativeControlError):
    """No such operation."""


class NativeControlUnavailable(NativeControlError):
    """The durable store could not answer; nothing was delivered."""


class NativeControlTransport(Protocol):
    """The three writes this adapter is allowed to make to a pane."""

    def send_literal(self, text: str) -> int: ...

    def send_enter(self) -> None: ...

    def send_key(self, keystroke: str) -> None: ...


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_json(raw: Optional[str]) -> Optional[Any]:
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


def _is_trim_invariant(text: str) -> bool:
    """Whether trimming could change this string at all."""
    if not text:
        return True
    return text[0] not in _JS_TRIM_CHARACTERS and text[-1] not in _JS_TRIM_CHARACTERS


def assert_artifact_free(text: str, *, field: str = "text") -> str:
    """Refuse anything that would arrive as a rendering artifact.

    The bracketed-paste sentinels specifically: tmux sanitises control
    bytes out of a paste buffer, so an ESC written into one arrives at the
    pane as the seven printable characters ``^[[200~`` — which a composer
    types out as visible text and then submits to the model. This adapter
    never pastes, and refusing the sentinels here means a payload cannot
    carry a pre-rendered one either.
    """
    _require_text(text, field=field)
    if "\x1b[200~" in text or "\x1b[201~" in text or "^[[200~" in text or "^[[201~" in text:
        raise NativeControlInvalid(
            f"{field} contains a bracketed-paste sentinel; native control types literal "
            f"text and never pastes, so a sentinel could only arrive as visible junk"
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

    On digests: ``payload_sha256`` covers the caller's original bytes and
    no encoding decision here can change it. ``composer_sha256`` is what
    the composer will hold at submit.

    ``model_input_sha256`` is what the model receives — and this build's
    submit-time normalization has not been read, so it is recorded only
    when the answer does not depend on that. A payload with no leading or
    trailing whitespace is invariant under trimming, so whether Claude
    trims cannot change what the model gets and the digest is stated. A
    payload that is *not* invariant would receive different text under
    the two possibilities, so the digest is ``None`` and
    ``submit_normalization_proven`` is false. Recording a guess there
    would be worse than recording nothing: a receipt that names a digest
    is read as evidence.
    """
    assert_artifact_free(text, field=field)

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

    # Normalized through the one normalizer the version pin already uses.
    # The durable binding records the provider's banner verbatim --
    # "2.1.220 (Claude Code)" -- because that is what the provider printed,
    # while this table is keyed by the bare build. A raw strip therefore
    # missed on an installed, pinned, proven build and refused it as
    # unproven, with zero task bytes, even though check_pinned_version had
    # already accepted the same string at bind time by normalizing it.
    #
    # Normalizing the lookup INPUT is not loosening the pin. The keys stay
    # bare exact builds, so an unproven version still misses. Adding the
    # banner form as a second key would have turned a table of proven
    # builds into a table of spellings, and the next banner variant would
    # be unproven again.
    pin = _PROVEN_COMPOSER_NEWLINE.get(
        provider_contracts.normalized_version(provider_version or "")
    )
    hint = None
    if pin is None:
        # The §3 override: the hint the pin recorded as evidence is read
        # from the installed bundle of the exact build being driven.  A
        # successful read licenses the keystroke; only a build whose
        # bundle yields no hint falls through to the refusal.
        hint = installed_bundle_facts.newline_keystroke_hint(PROVIDER, provider_version)
    undeliverable = None
    if len(lines) > 1 and pin is None and hint is None:
        # Recorded on the plan rather than raised. The payload is well
        # formed; what is missing is a proven keystroke for the installed
        # build, which is an operational fact about this session. The
        # caller turns it into a durable typed refusal so a lost response
        # is answerable by exact id. Splitting the message across turns,
        # pasting it, and flattening the newlines away are each a way of
        # appearing to deliver something that was not delivered, so none
        # of them is the alternative.
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
        # Stated only when it cannot be wrong. See the docstring.
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
        # nowhere (the refusal case).  A bundle read is evidence about
        # this build's own bytes; it is still not the live multiline
        # acceptance a table row asserts, and the plan says which it is.
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


def turn_observation(
    *,
    active_turn_id: Optional[str],
    observed_at: str,
    observer: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """A schema-bound statement about what the pane was doing.

    Exists so the idle and steer gates consult an observation with a
    provenance rather than a caller's boolean. ``observer`` names who
    looked, so a receipt reader can tell a screen reading from a guess.

    ``active_turn_id`` may be ``None``, which means "observed idle" and
    never "did not look". An omitted observation and an idle one would be
    indistinguishable once stored, and "we did not check" must not be
    able to satisfy an idle gate — which is why the field is required
    rather than defaulted.

    The keyword is ``observer`` to match the Kimi adapter exactly. The
    two modules are deliberately separate, but a caller that dispatches
    between them by provider passes the same keywords to both, so a
    gratuitous rename here would be a runtime failure at the one call
    site that has to work for both providers.
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
            f"got {observation.get('schema')!r}. A Kimi observation is not a "
            f"Claude observation, and neither is an unlabelled dict"
        )
    if observation.get("provider") != PROVIDER:
        raise NativeControlInvalid(
            f"turn observation is for provider {observation.get('provider')!r}, not {PROVIDER!r}"
        )
    active = observation.get("active_turn_id")
    if active is not None and not isinstance(active, str):
        raise NativeControlInvalid("active_turn_id must be a string or None")
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
        db.query(database.ClaudeNativeControlOperationModel)
        .filter(database.ClaudeNativeControlOperationModel.operation_id == operation_id)
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
            f"claude native control operates only in {NATIVE_TUI!r}; "
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
    """Refuse every new operation while one is unresolved.

    An ambiguous operation may have delivered. Anything sent after it
    would be a second message whose ordering relative to the first is
    unknown, so the session is closed to new work until a reconcile says
    what happened.
    """
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
    provider is transient while an unproven keystroke is permanent —
    reporting the transient reason would invite a retry that can never
    succeed.
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
        # Written from the module constant rather than taken from the
        # binding, which does not carry one. The column is NOT NULL, so
        # the store is what would catch a caller-supplied provider being
        # absent — but it would catch it at the INSERT, which is after the
        # point where this module still has a typed answer to give.
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
            db.add(database.ClaudeNativeControlOperationModel(**row_values))
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
                db.query(database.ClaudeNativeControlOperationModel)
                .filter(
                    database.ClaudeNativeControlOperationModel.operation_id == operation_id,
                    database.ClaudeNativeControlOperationModel.epoch == observed_epoch,
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
    """A keystroke plan stopped part-way, so what landed is not known.

    Carries the boundary it stopped at, because "typed but not
    submitted" is a real recoverable state while "may have submitted" is
    not, and a caller that cannot tell them apart has to assume the
    worse one.
    """

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

    The provider-specific half of every native write, and deliberately
    free of any store: the caller journals the outcome in whatever record
    is its own at-most-once authority.  Native task admission journals it
    on the reservation's control operation; the identity-bound
    control-input path journals it on the control request.  Two callers,
    one keystroke sequence — because the sequence is where the proven
    composer newline and the submit settle live, and a second copy of it
    would be a second set of composer facts to keep true.

    Exactly one Enter is sent, ever, for exactly one turn.  The breaks
    inside a multi-line payload are composer keystrokes, not submissions,
    so a message with ten newlines is still one turn.

    ``submit=False`` types the payload and stops.  It is not a partial
    write: nothing was submitted and the caller knows it, which is a
    different fact from an interrupted one.

    Raises:
        NativeControlInvalid: The plan is undeliverable.  Nothing typed.
        ComposerWriteInterrupted: The transport raised part-way through.
    """
    if not plan.get("deliverable", True):
        # Belt and braces: reaching the transport with an undeliverable
        # plan would be a bug here, not a caller error, and it must not be
        # discovered by half-typing a message.
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

    # Let the renderer settle before the submit. The Ink TUI is known to
    # swallow an Enter that arrives too soon, which produces no error and
    # no turn — the message simply sits in the prompt box unsent.
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
    retry and produce a duplicate. Which boundary was reached is recorded,
    because "typed but not submitted" is a real recoverable state while
    "may have submitted" is not.
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
                    # Observed facts about what this adapter did, never a
                    # claim about what the provider made of them.
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
    """Ordinary follow-up, gated on the provider being idle.

    Refuses while a turn is running rather than queueing optimistically.
    Text typed during an active turn is not reliably held for later — it
    is as likely to be consumed by whatever the TUI is currently showing,
    and a follow-up that lands mid-turn changes the running work instead
    of following it.
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
                f"turn {observed['active_turn_id']} is active; ordinary follow-up waits for "
                f"idle. A deliberate mid-turn message is a steer, requested explicitly",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def operator_message(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    text: str,
    payload_sha256: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
    provider_version: Optional[str] = None,
    pre_write: Optional[Callable[[], Optional[Tuple[str, str]]]] = None,
) -> dict[str, Any]:
    """Submit one operator message (long text, multi-line, image references).

    Lane C's typed operation (design §8.3): the same journaling, gating,
    and at-most-once discipline as :func:`queue`, with two deliberate
    differences:

    - ``payload_sha256`` is supplied by the caller and digests the *whole
      request* (draft text + attachment ids + token map), not just the
      provider-bound text.  The replay identity of an operator message is
      everything the operator sent, so a same-id request whose attachments
      changed is a conflict, not a replay.
    - ``text`` arrives already reference-substituted by the
      operator-message service (staged image paths per the registry's
      ``reference_template``).  This adapter types what it is given; which
      bytes name an image is the service's contract.

    The caller (operator-message service) holds the pane-input lease, has
    already re-proven identity under it, and gates idle itself; the
    observed-idle assertion here is the same defense in depth queue keeps.

    ``pre_write`` is the caller's last-safe-point hook (Lane C r1: the
    image-attachment ``ready → submitted(operation_id)`` binding).  It runs
    after the journal claim and every deliverability/idle gate, immediately
    before the transport write.  Its answer is ``None`` to proceed or a
    ``(refusal_reason, detail)`` pair — one of the ``REFUSED_IMAGE_*``
    reasons — which is journaled as a typed refusal with zero bytes typed.
    Once the hook succeeds the write proceeds; an ambiguity after that
    point freezes the operation exactly as any post-claim uncertainty does.
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
        kind=KIND_OPERATOR_MESSAGE,
        binding=binding,
        turn_id=None,
        payload_sha256=payload_sha256,
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
                f"turn {observed['active_turn_id']} is active; an operator message waits "
                "for idle, exactly as the control-input readiness gate requires",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    if pre_write is not None:
        hook_refusal = pre_write()
        if hook_refusal is not None:
            reason, detail = hook_refusal
            if reason not in (REFUSED_IMAGE_UNKNOWN, REFUSED_IMAGE_NOT_READY):
                raise NativeControlInvalid(
                    f"the pre-write hook answered with refusal reason {reason!r}, "
                    "which is not one of the image-binding refusal reasons"
                )
            return _refuse(binding["operation_id"], _Refusal(reason, detail))

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


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
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Deliberately steer one exact active turn.

    Binds to ``turn_id`` and refuses if the observed turn is a different
    one or if nothing is running. Without that binding, a steer written
    for a turn that ended in the meantime would land in whatever turn
    started next — arriving as an instruction about work it was never
    about.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    plan = plan_composer_keystrokes(text, provider_version=provider_version, field="text")
    target_turn = _require_text(turn_id, field="turn_id")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_STEER,
        binding=binding,
        turn_id=target_turn,
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

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def control(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    command: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Send one advertised slash command.

    Only commands in :data:`ADVERTISED_CONTROLS` are typed. At the
    transport a slash command is indistinguishable from message text, so
    an unrecognised one would not fail — it would be delivered to the
    model as prose that happens to start with a slash.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    requested = _require_text(command, field="command")
    plan = plan_composer_keystrokes(requested, provider_version=provider_version, field="command")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_CONTROL,
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
        if requested not in ADVERTISED_CONTROLS:
            raise _Refusal(
                REFUSED_UNSUPPORTED_CONTROL,
                f"{requested!r} is not an advertised control for {PROVIDER}; advertised: "
                f"{list(ADVERTISED_CONTROLS)}. An unadvertised command would be typed into "
                f"the composer as ordinary text and sent to the model",
            )
        _assert_deliverable(plan)
        if observed["active_turn_id"] is not None:
            raise _Refusal(
                REFUSED_ACTIVE_TURN,
                f"turn {observed['active_turn_id']} is active; a control command waits for idle",
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
    observation of the provider. Folding them together would let a
    successful write imply an accepted turn.
    """
    validated = _validated_provider_observation(observation)
    return _update(
        operation_id=operation_id,
        from_states=frozenset({POSTED, ACCEPTED}),
        to_state=validated["state"],
        extra={"observation_json": _canonical(validated)},
    )


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
    """Resolve one ambiguous operation with evidence naming it exactly.

    The evidence must name this operation id. An operator resolving "the
    ambiguous one" without saying which would, with two outstanding, close
    the wrong one — and closing an ambiguity is what re-opens the session
    to new writes.
    """
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
                db.query(database.ClaudeNativeControlOperationModel)
                .filter(
                    database.ClaudeNativeControlOperationModel.native_session_id
                    == native_session_id,
                    database.ClaudeNativeControlOperationModel.state == AMBIGUOUS,
                )
                .order_by(database.ClaudeNativeControlOperationModel.created_at)
                .first()
            )
            return None if row is None else _row_dict(row)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read session ambiguity: {exc}") from exc
