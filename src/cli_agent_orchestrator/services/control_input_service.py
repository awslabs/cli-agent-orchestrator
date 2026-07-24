"""The one path that types a control into a provider's composer.

This is the service behind ``POST /terminals/{id}/control-input``.  It
exists because the ordinary delivery path cannot make the promise a
control needs: that the exact characters the operator wrote reached
exactly one pane, exactly once, or that the caller was told truthfully
that it did not.

Everything here is arranged around one ordering, and the order is the
guarantee rather than an implementation detail:

1. Screen the payload.  Nothing that could synthesise its own escape
   sequence or submit at a point the caller did not choose gets past
   this step, and nothing has been written when it fails.
2. Resolve the server's own view of the terminal's identity, then
   compare it with the caller's declared expectation.  A caller that
   expected a different pane, generation, or provider is refused here —
   before the journal, before the lease, before any byte.
3. Open the durable intent.  From this point a lost response has an
   answer, because the record exists whether or not the caller ever sees
   the reply.
4. Take the pane lease, and re-verify the physical identity *under* it.
   The gap between "checked" and "wrote" is where a pane that died and
   was replaced would otherwise let a control land in a stranger's
   composer, and the lease is what makes the re-check meaningful.
5. Claim the write, then write.  The claim commits first, so a crash
   afterwards is durably visible as "may have been written" instead of
   being silently indistinguishable from "never started".

The refusals are typed and the vocabulary is closed, so a caller never
has to infer intent from prose.  A refusal — and only a refusal — proves
zero bytes reached the pane and licenses another attempt.

What this path deliberately does not do: fall back.  There is no
degradation to a tmux buffer paste, no raw key injection, no second
endpoint to try.  A control the operator believes was delivered once
must never be delivered twice or as different bytes, and every fallback
is a way for that to happen quietly.

Managed provider sessions are refused rather than typed into.  Their
panes run a bridge process, not a composer; the zero-keystroke contract
that governs them is not something this path is entitled to bypass, and
their control surface is the generation-bound managed operations API.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from cli_agent_orchestrator.clients.tmux import TmuxLiteralSendError
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    CONTROL_INPUT_DIGEST_DOMAIN,
    CONTROL_INPUT_OUTCOMES,
    CONTROL_INPUT_PROTOCOL,
    CONTROL_INPUT_REASON_CODES,
    CONTROL_INPUT_REQUEST_SCHEMA_VERSION,
    IDENTITY_FIELDS,
    REASON_CONTROL_ROUTE_ABSENT,
    REASON_IDENTITY_MISMATCH,
    REASON_ILLEGAL_CONTROL_BYTES,
    REASON_LINEAGE_UNPROVEN,
    REASON_MANAGED_ACP_PANE,
    REASON_MULTILINE_REJECTED,
    REASON_OWNER_LOST_BEFORE_WRITE,
    REASON_PANE_BUSY,
    REASON_PANE_DEAD,
    REASON_PROTOCOL_MISMATCH,
    REASON_REQUEST_REBOUND,
    REASON_STALE_GENERATION,
    REASON_UNKNOWN_TERMINAL,
    REASON_WRITE_INCOMPLETE,
    REFUSED,
    contains_bracketed_paste_sentinel,
    control_input_request_digest,
    is_reattemptable,
    normalize_expected_identity,
    outcome_for_reason,
)
from cli_agent_orchestrator.services.control_input_journal import (
    STATE_AMBIGUOUS,
    WRITING,
    ControlInputBinding,
    ControlInputJournal,
    ControlInputRebound,
    ControlInputRecord,
    ControlInputTransitionRefused,
    outcome_for_state,
)
from cli_agent_orchestrator.services.pane_input_arbiter import PaneBusyError, pane_input_lease

logger = logging.getLogger(__name__)

# A control input is a command or one short line, never a document.  The
# bound is on UTF-8 bytes rather than characters so it means the same
# thing to the client that computed it and to this server, which writes
# bytes.  Identical to the conductor client's MAX_TEXT_BYTES on purpose:
# a limit the two sides disagree about is a request one of them believes
# it sent.
MAX_TEXT_BYTES = 512

# The caller-supplied control id, validated against a strict alphabet
# before it is used as a durable key.  Identical to the conductor's
# _CONTROL_ID_RE: the id is the handle a lost response is resolved by, so
# both sides must agree on which ids exist at all.
CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# How the provider is driven.  ``native_tui`` is a pane running the
# provider's own terminal UI, which has a composer this path may type
# into.  ``acp`` is a pane driven through the managed bridge protocol
# (ACP or its provider-specific equivalent), which has no composer and
# whose control surface is the generation-bound managed operations API.
EXECUTION_MODE_NATIVE_TUI = "native_tui"
EXECUTION_MODE_ACP = "acp"


class ControlInputRequestInvalid(ValueError):
    """The request is malformed, so no typed outcome can describe it.

    Reserved for shape violations — an unusable control id, text that is
    absent or over the byte bound, an expectation naming a field that
    does not exist.  These carry no reason code on purpose: reason codes
    exist for the failures a caller must tell apart in order to act, and
    a malformed request has one action regardless of which field was
    wrong.  The transport carries them as ``422``, which the shared
    contract already classifies as ``refused``: nothing was written.
    """


# --- Resolved identity ----------------------------------------------------


@dataclass(frozen=True)
class ResolvedControlIdentity:
    """What this server can actually prove about one control target.

    Split deliberately into two kinds of fact.  The nine
    :data:`IDENTITY_FIELDS` are the *declarable* identity: a caller may
    state an expectation for each, and the digest covers what it stated.
    ``pane_id``/``window_id``/``pane_pid`` are the physical write target,
    resolved here and re-verified under the pane lease; they are not in
    the digest, because a caller cannot compute a preimage over facts it
    was never told.

    A field this server cannot prove is ``None`` rather than filled in
    from something adjacent.  ``provider_process_id`` is the clearest
    case: ``pane_pid`` is the pane's root process and the provider runs
    as a descendant of it, so equating them would let a caller believe it
    had bound to a process identity it had not.
    """

    terminal_id: str
    terminal_incarnation: Optional[str]
    terminal_generation: Optional[str]
    provider: Optional[str]
    native_session_id: Optional[str]
    execution_mode: str
    session_name: Optional[str]
    provider_process_id: Optional[int] = None
    pane_id: Optional[str] = None
    window_id: Optional[str] = None
    pane_pid: Optional[int] = None
    pane_dead: bool = False
    managed: bool = False
    # What the terminal record claims its pane is, kept separately from
    # the live-verified ``pane_id`` so "this terminal never recorded an
    # immutable identity" stays distinguishable from "the pane it
    # recorded is gone".  They are different refusals and a caller acts
    # differently on each.
    recorded_pane_id: Optional[str] = None

    def expected_identity_view(self) -> Dict[str, Any]:
        """The nine declarable fields, in the digest's fixed order.

        ``pane_birth_id`` is the tmux pane id under its declarable name:
        tmux mints ``%N`` once and never re-uses it for the life of the
        server, which is what makes it a birth id rather than a position.
        """
        view: Dict[str, Any] = {
            "terminal_id": self.terminal_id,
            "terminal_incarnation": self.terminal_incarnation,
            "terminal_generation": self.terminal_generation,
            "pane_birth_id": self.pane_id,
            "provider_process_id": self.provider_process_id,
            "provider": self.provider,
            "native_session_id": self.native_session_id,
            "execution_mode": self.execution_mode,
            "session_name": self.session_name,
        }
        # The view is the contract's field set exactly; a drift here
        # would be a digest both sides compute differently.
        assert tuple(view) == IDENTITY_FIELDS
        return view

    def as_dict(self) -> Dict[str, Any]:
        payload = self.expected_identity_view()
        payload["pane"] = {
            "pane_id": self.pane_id,
            "window_id": self.window_id,
            "pane_pid": self.pane_pid,
            "dead": self.pane_dead,
        }
        return payload


def _terminal_metadata(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Legacy or v2 terminal metadata; an uninstalled v2 surface is absent."""
    from sqlalchemy.exc import OperationalError

    from cli_agent_orchestrator.clients.database import (
        get_terminal_metadata,
        get_terminal_metadata_v2,
    )

    metadata = get_terminal_metadata(terminal_id)
    if metadata is not None:
        return metadata
    try:
        return get_terminal_metadata_v2(terminal_id)
    except OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return None


def _managed_identity(terminal_id: str) -> Optional[Dict[str, Any]]:
    """The managed reservation for this terminal, if it has one."""
    from cli_agent_orchestrator.services import managed_launch

    return managed_launch.managed_control_identity(terminal_id)


def _tmux_client() -> Any:
    """The tmux client, or None when the backend is not tmux.

    A control target is a tmux pane id.  Under any other backend there is
    no pane to bind to, and a control that cannot be bound is refused
    rather than approximated.
    """
    from cli_agent_orchestrator.backends.registry import get_backend
    from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

    if not isinstance(get_backend(), TmuxBackend):
        return None
    from cli_agent_orchestrator.clients.tmux import tmux_client

    return tmux_client


def resolve_control_identity(terminal_id: str) -> Optional[ResolvedControlIdentity]:
    """This server's own view of ``terminal_id``, or None if unknown.

    Live pane facts are read from tmux by immutable pane id, never by
    window name: a name is a mutable label a worker can reassign, and a
    control bound to a label is a control bound to nothing.
    """
    metadata = _terminal_metadata(terminal_id)
    if metadata is None:
        return None

    managed = _managed_identity(terminal_id)
    generation = metadata.get("generation")
    if managed is not None and managed.get("generation"):
        generation = managed["generation"]

    recorded = metadata.get("pane_id")
    recorded_pane_id = recorded if isinstance(recorded, str) and recorded else None
    pane_id: Optional[str] = None
    window_id: Optional[str] = None
    pane_pid: Optional[int] = None
    pane_dead = False
    client = _tmux_client()
    if client is not None and recorded_pane_id is not None:
        live = client.pane_control_identity(pane_id=recorded_pane_id)
        if live is not None:
            # Only a pane tmux confirms right now becomes the live
            # identity.  An unreadable server leaves it unresolved rather
            # than assuming the recorded pane is still there.
            pane_id = live.pane_id
            window_id = live.window_id
            pane_pid = live.pane_pid
            pane_dead = live.dead

    return ResolvedControlIdentity(
        terminal_id=terminal_id,
        # The fork's one durable non-reusable token is `generation`.
        # Reporting it twice, once as an incarnation, would present a
        # single fact as two independent checks.
        terminal_incarnation=None,
        terminal_generation=generation,
        provider=metadata.get("provider"),
        # A native TUI pane has no provider-native session id this server
        # observes; the managed surface has one, and managed panes are
        # refused by this path rather than typed into.
        native_session_id=None,
        execution_mode=EXECUTION_MODE_ACP if managed is not None else EXECUTION_MODE_NATIVE_TUI,
        session_name=metadata.get("tmux_session"),
        # Unprovable here: pane_pid is the pane's root process, the
        # provider is a descendant of it, and a guess would be worse
        # than an absence.
        provider_process_id=None,
        pane_id=pane_id,
        window_id=window_id,
        pane_pid=pane_pid,
        pane_dead=pane_dead,
        managed=managed is not None,
        recorded_pane_id=recorded_pane_id,
    )


# --- Screening ------------------------------------------------------------


def screen_control_text(text: str) -> Optional[Tuple[str, str]]:
    """Reason and detail for text this path will not type, or None.

    Order is deliberate.  Paste framing is screened first because it is
    the exact failure this lane exists to remove and deserves the precise
    answer, even though the control-character scan below would also catch
    its ESC.  Nothing is normalised or stripped: the text is typed byte
    for byte as written, or it is refused.
    """
    if contains_bracketed_paste_sentinel(text):
        return (
            REASON_ILLEGAL_CONTROL_BYTES,
            "the text carries bracketed-paste framing; it is refused rather than "
            "stripped, because a payload that closes a paste early turns its own "
            "remainder into keystrokes",
        )
    for index, char in enumerate(text):
        point = ord(char)
        if char in ("\r", "\n"):
            return (
                REASON_MULTILINE_REJECTED,
                f"the text carries a line break at offset {index}; a control input is "
                "one line plus one explicit Enter, and an embedded break would submit "
                "at a point the caller did not choose",
            )
        if point < 0x20 or point == 0x7F or 0x80 <= point <= 0x9F:
            return (
                REASON_ILLEGAL_CONTROL_BYTES,
                f"the text carries the control character U+{point:04X} at offset "
                f"{index}; the terminal would interpret it rather than type it",
            )
    return None


def _require_shape(control_id: Any, text: Any, enter: Any, request_digest: Any) -> None:
    """Refuse a request no typed outcome could honestly describe."""
    if not isinstance(control_id, str) or not CONTROL_ID_PATTERN.match(control_id):
        raise ControlInputRequestInvalid(
            f"invalid control_id {control_id!r}: must match {CONTROL_ID_PATTERN.pattern}"
        )
    if not isinstance(text, str) or text == "":
        raise ControlInputRequestInvalid("text must be a non-empty string")
    encoded = len(text.encode("utf-8"))
    if encoded > MAX_TEXT_BYTES:
        raise ControlInputRequestInvalid(
            f"text is {encoded} UTF-8 bytes, over the {MAX_TEXT_BYTES}-byte control-input "
            "limit; a control input is a command or one short line, not a document"
        )
    if not isinstance(enter, bool):
        raise ControlInputRequestInvalid("enter must be a boolean and must be stated explicitly")
    if request_digest is not None and (
        not isinstance(request_digest, str) or not _DIGEST_PATTERN.match(request_digest)
    ):
        raise ControlInputRequestInvalid(
            "request_digest must be 64 lowercase hex characters when supplied"
        )


def screen_expected_identity(
    expected: Dict[str, Any], resolved: ResolvedControlIdentity
) -> Optional[Tuple[str, str]]:
    """Reason and detail for an expectation this server cannot honour.

    Ordered from the coarsest disagreement to the finest, so the caller
    is told the fact that actually explains the refusal rather than the
    first field that happened to differ.
    """
    declared_terminal = expected.get("terminal_id")
    if declared_terminal is not None and declared_terminal != resolved.terminal_id:
        return (
            REASON_IDENTITY_MISMATCH,
            f"the request is addressed to terminal {resolved.terminal_id!r} but expects "
            f"{declared_terminal!r}",
        )

    if resolved.execution_mode == EXECUTION_MODE_ACP:
        return (
            REASON_MANAGED_ACP_PANE,
            "this terminal is driven through the managed provider bridge; its pane runs "
            "a bridge process rather than a composer, and its controls are the "
            "generation-bound managed operations",
        )
    declared_mode = expected.get("execution_mode")
    if declared_mode is not None and declared_mode != resolved.execution_mode:
        return (
            REASON_IDENTITY_MISMATCH,
            f"the terminal runs in {resolved.execution_mode!r}, not the expected "
            f"{declared_mode!r}",
        )

    declared_generation = expected.get("terminal_generation")
    if declared_generation is not None and declared_generation != resolved.terminal_generation:
        return (
            REASON_STALE_GENERATION,
            "the expected generation is not the terminal's live generation; the terminal "
            "the caller meant has been replaced",
        )

    # Two fields this server cannot prove for a native TUI pane.  A
    # declared value is refused rather than ignored: silently accepting an
    # expectation nobody checked is how a caller comes to believe it bound
    # to something it did not.
    if expected.get("terminal_incarnation") is not None:
        return (
            REASON_LINEAGE_UNPROVEN,
            "this server exposes no terminal incarnation token distinct from the "
            "generation, so an incarnation expectation cannot be verified",
        )
    if expected.get("provider_process_id") is not None:
        return (
            REASON_LINEAGE_UNPROVEN,
            "this server cannot prove the provider's process id; the pane pid is the "
            "pane's root process and the provider is a descendant of it",
        )

    declared_provider = expected.get("provider")
    if declared_provider is not None and declared_provider != resolved.provider:
        return (
            REASON_IDENTITY_MISMATCH,
            f"the terminal's provider is {resolved.provider!r}, not the expected "
            f"{declared_provider!r}",
        )
    declared_native = expected.get("native_session_id")
    if declared_native is not None and declared_native != resolved.native_session_id:
        return (
            REASON_IDENTITY_MISMATCH,
            "this server observes no provider-native session id for a native TUI pane, "
            "so the expected native session id cannot match",
        )
    declared_session = expected.get("session_name")
    if declared_session is not None and declared_session != resolved.session_name:
        return (
            REASON_IDENTITY_MISMATCH,
            f"the terminal is in tmux session {resolved.session_name!r}, not the expected "
            f"{declared_session!r}",
        )
    declared_pane = expected.get("pane_birth_id")
    if declared_pane is not None and declared_pane != resolved.pane_id:
        return (
            REASON_IDENTITY_MISMATCH,
            f"the terminal's pane is {resolved.pane_id!r}, not the expected " f"{declared_pane!r}",
        )
    return None


# --- The journal singleton ------------------------------------------------

_journal: Optional[ControlInputJournal] = None
_journal_guard = threading.Lock()


def control_input_journal_path() -> Path:
    """Where the durable request journal lives.

    Resolved at call time from ``CAO_HOME_DIR`` so an isolated state root
    takes effect without a module reload.
    """
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    return Path(CAO_HOME_DIR) / "db" / "control-input.sqlite3"


def get_control_input_journal() -> ControlInputJournal:
    global _journal
    with _journal_guard:
        if _journal is None:
            _journal = ControlInputJournal(control_input_journal_path())
        return _journal


def reset_control_input_journal() -> None:
    """Drop the cached journal (tests / isolated state roots)."""
    global _journal
    with _journal_guard:
        _journal = None


# --- Results --------------------------------------------------------------


@dataclass(frozen=True)
class ControlInputResult:
    """One control call's answer, in the shape the wire carries it.

    ``outcome`` is ``None`` only while a request is genuinely still in
    flight.  Inventing one of the four outcomes for that state would be a
    lie in whichever direction the caller happened to need: ``refused``
    would license a duplicate write, ``ambiguous`` would strand a request
    that is about to succeed.
    """

    control_id: str
    outcome: Optional[str]
    reason_code: Optional[str] = None
    detail: str = ""
    state: Optional[str] = None
    terminal_id: Optional[str] = None
    request_digest: Optional[str] = None
    resolved_identity: Optional[Dict[str, Any]] = None
    text_sent: bool = False
    enter_sent: bool = False
    chunks_sent: Optional[int] = None
    enter_attempted: Optional[bool] = None
    http_status: int = 200

    def __post_init__(self) -> None:
        if self.outcome is not None and self.outcome not in CONTROL_INPUT_OUTCOMES:
            raise ValueError(f"unknown control-input outcome: {self.outcome!r}")
        if self.reason_code is not None:
            # The one place a reason and an outcome meet on the wire, so
            # the one place the binding must hold before a caller reads
            # a re-attempt licence out of a mis-paired answer.
            if self.reason_code not in CONTROL_INPUT_REASON_CODES:
                raise ValueError(f"unknown control-input reason: {self.reason_code!r}")
            bound = outcome_for_reason(self.reason_code)
            if self.outcome is not None and bound != self.outcome:
                raise ValueError(
                    f"reason {self.reason_code!r} carries outcome {bound!r}, not "
                    f"{self.outcome!r}"
                )

    def as_response(self) -> Dict[str, Any]:
        return {
            "protocol": CONTROL_INPUT_PROTOCOL,
            "request_schema_version": CONTROL_INPUT_REQUEST_SCHEMA_VERSION,
            "control_id": self.control_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "reattemptable": self.outcome is not None and is_reattemptable(self.outcome),
            "in_flight": self.outcome is None,
            "state": self.state,
            "terminal_id": self.terminal_id,
            "detail": self.detail,
            "request_digest": self.request_digest,
            "resolved_identity": self.resolved_identity,
            # Proven transport facts only: true means tmux acknowledged
            # the write.  An ambiguous outcome leaves both false and
            # reports what may have happened in chunks_sent /
            # enter_attempted instead of guessing.
            "text_sent": self.text_sent,
            "enter_sent": self.enter_sent,
            "chunks_sent": self.chunks_sent,
            "enter_attempted": self.enter_attempted,
        }


def _refusal(
    control_id: str,
    reason: str,
    detail: str,
    *,
    terminal_id: Optional[str] = None,
    resolved: Optional[ResolvedControlIdentity] = None,
    digest: Optional[str] = None,
    state: Optional[str] = None,
) -> ControlInputResult:
    return ControlInputResult(
        control_id=control_id,
        outcome=outcome_for_reason(reason),
        reason_code=reason,
        detail=detail,
        state=state,
        terminal_id=terminal_id,
        request_digest=digest,
        resolved_identity=None if resolved is None else resolved.as_dict(),
    )


def _from_record(
    record: ControlInputRecord,
    *,
    resolved: Optional[ResolvedControlIdentity] = None,
    detail: str = "",
) -> ControlInputResult:
    """The answer a durable record already licenses, with nothing added."""
    outcome = outcome_for_state(record.state)
    return ControlInputResult(
        control_id=record.request_id,
        outcome=outcome,
        reason_code=record.reason_code,
        detail=detail
        or (
            "this request is already in flight under another writer; resolve it by the "
            "exact control id rather than sending it again"
            if outcome is None
            else "recorded outcome for this control id"
        ),
        state=record.state,
        terminal_id=record.terminal_id,
        request_digest=record.request_sha256,
        resolved_identity=None if resolved is None else resolved.as_dict(),
        text_sent=outcome == ACCEPTED,
        enter_sent=outcome == ACCEPTED and bool(record.enter_attempted),
        chunks_sent=record.chunks_sent,
        enter_attempted=record.enter_attempted,
    )


# --- Delivery -------------------------------------------------------------


def _record_refusal(
    journal: ControlInputJournal,
    control_id: str,
    reason: str,
    detail: str,
    *,
    terminal_id: str,
    resolved: ResolvedControlIdentity,
    digest: str,
) -> ControlInputResult:
    """Record a refusal decided after the intent, and answer with it.

    The journal write is not bookkeeping: a caller whose response is lost
    must get the same refusal when it asks again, and it can only do that
    if the refusal was durable before the answer was sent.
    """
    try:
        record = journal.mark_refused(control_id, reason_code=reason, evidence_digest=digest)
    except ControlInputTransitionRefused:
        # Another writer resolved this record first.  Its answer is the
        # durable one and stands; overwriting it with this one would tell
        # two callers two different things about the same pane write.
        existing = journal.find(control_id)
        if existing is not None:
            return _from_record(existing, resolved=resolved)
        raise
    return _refusal(
        control_id,
        reason,
        detail,
        terminal_id=terminal_id,
        resolved=resolved,
        digest=digest,
        state=record.state,
    )


def deliver_control_input(
    terminal_id: str,
    *,
    control_id: Any,
    text: Any,
    enter: Any = True,
    expected_identity: Optional[Mapping[str, Any]] = None,
    request_digest: Optional[str] = None,
    protocol: Optional[str] = None,
    lease_timeout: float = 0.0,
    journal: Optional[ControlInputJournal] = None,
) -> ControlInputResult:
    """Type one control into one pane, once, or say truthfully why not.

    Blocking: runs tmux subprocesses and SQLite transactions, so an async
    caller must dispatch it to a worker thread.

    Args:
        terminal_id: The terminal whose provider composer to type into.
        control_id: Caller-chosen durable id for this control.  It is the
            handle a lost response is resolved by, so it must be unique
            per control the caller intends to send, and identical across
            that control's retries.
        text: The literal single-line text, typed byte for byte.
        enter: Whether to submit with one explicit Enter.  Stated, never
            inferred from the text, because submission is the irreversible
            half of a control.
        expected_identity: What the caller believes it is addressing.  Any
            of the nine identity fields may be declared; each is checked
            before the first byte and a disagreement is a refusal.
        request_digest: The caller's own digest of this request.  When
            supplied it must equal the server's, which is what turns a
            corrupted or partially-substituted request into a refusal
            rather than a control nobody authorised.
        protocol: The protocol literal the caller speaks.
        lease_timeout: Seconds to wait for the pane lease.  The default
            of 0 refuses immediately rather than queueing.
        journal: Journal override (tests / isolated state roots).

    Returns:
        A typed result.  ``refused`` — and only ``refused`` — proves zero
        bytes reached the pane and permits sending the control again.

    Raises:
        ControlInputRequestInvalid: The request is malformed.  Nothing was
            written and no typed outcome could honestly describe it.
    """
    # The protocol literal is checked before the request shape, because a
    # caller speaking a protocol this server does not implement may be
    # sending a body with entirely different rules; validating it against
    # this protocol's rules would report a field error for what is really
    # a version mismatch, and 'fix your field' invites a retry that can
    # never succeed.
    if protocol is not None and protocol != CONTROL_INPUT_PROTOCOL:
        return ControlInputResult(
            control_id=control_id if isinstance(control_id, str) else "",
            outcome=outcome_for_reason(REASON_PROTOCOL_MISMATCH),
            reason_code=REASON_PROTOCOL_MISMATCH,
            detail=(
                f"this server implements {CONTROL_INPUT_PROTOCOL!r}, not the requested "
                f"{protocol!r}; there is no fallback path, because delivering a control "
                "under a protocol neither side agreed on is how the same control gets "
                "sent twice or as different bytes"
            ),
            terminal_id=terminal_id,
            http_status=422,
        )

    _require_shape(control_id, text, enter, request_digest)
    try:
        expected = normalize_expected_identity(expected_identity)
    except ValueError as exc:
        raise ControlInputRequestInvalid(str(exc)) from exc

    screened = screen_control_text(text)
    if screened is not None:
        return _refusal(control_id, screened[0], screened[1], terminal_id=terminal_id)

    digest = control_input_request_digest(
        control_id=control_id, text=text, enter=enter, expected_identity=expected
    )
    if request_digest is not None and request_digest != digest:
        return _refusal(
            control_id,
            REASON_REQUEST_REBOUND,
            "the caller's request digest does not match the request that arrived; the "
            "control the caller authorised is not the control this server would send",
            terminal_id=terminal_id,
            digest=digest,
        )

    resolved = resolve_control_identity(terminal_id)
    if resolved is None:
        # Deliberately a typed refusal rather than a 404: the route
        # exists, and a 404 here would be indistinguishable from a server
        # that has no control route at all, which a caller must treat as
        # 'unsupported' instead of 'wrong terminal'.
        return _refusal(
            control_id,
            REASON_UNKNOWN_TERMINAL,
            f"no terminal {terminal_id!r} is known to this server",
            terminal_id=terminal_id,
            digest=digest,
        )

    client = _tmux_client()
    if client is None:
        # Not a refusal: a refusal invites a re-attempt, and no re-attempt
        # under this backend can ever find a pane to bind to.
        return _refusal(
            control_id,
            REASON_CONTROL_ROUTE_ABSENT,
            "this server is not running the tmux backend, so there is no pane to bind a "
            "control to; the route exists but can serve no terminal here",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )

    mismatch = screen_expected_identity(expected, resolved)
    if mismatch is not None:
        return _refusal(
            control_id,
            mismatch[0],
            mismatch[1],
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )

    if resolved.recorded_pane_id is None:
        return _refusal(
            control_id,
            REASON_LINEAGE_UNPROVEN,
            "this terminal has never recorded an immutable pane identity, so there is "
            "nothing a control could be bound to; a control sent by window name would "
            "be bound to a mutable label rather than to a pane",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )
    if resolved.pane_id is None or resolved.pane_dead:
        return _refusal(
            control_id,
            REASON_PANE_DEAD,
            f"pane {resolved.recorded_pane_id!r} is gone or dead; tmux never re-uses a "
            "pane id, so this is the end of that pane rather than a pane to wait for",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )
    if resolved.window_id is None or resolved.pane_pid is None:
        return _refusal(
            control_id,
            REASON_LINEAGE_UNPROVEN,
            "the pane's window and root process could not both be observed, so the "
            "binding this control would be re-verified against cannot be formed",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )

    book = get_control_input_journal() if journal is None else journal
    binding = ControlInputBinding(
        request_id=control_id,
        terminal_id=terminal_id,
        pane_id=resolved.pane_id,
        window_id=resolved.window_id,
        pane_pid=resolved.pane_pid,
        request_sha256=digest,
        generation=resolved.terminal_generation,
    )
    try:
        record = book.open_intent(binding)
    except ControlInputRebound as exc:
        return _refusal(
            control_id,
            REASON_REQUEST_REBOUND,
            str(exc),
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )

    if record.state == WRITING:
        # Someone claimed this write.  If that owner is gone, the record
        # is stranded and the sweep resolves it to the truthful terminal
        # answer; if the owner is alive, the sweep leaves it and the reply
        # below is 'in flight', which is also truthful.  Either way this
        # caller must not write: the claim is already taken.
        book.sweep_stranded()
        record = book.get(control_id)
    if record.is_terminal:
        # The at-most-once replay.  A retried request after a lost
        # response is answered from the record rather than re-executed.
        return _from_record(record, resolved=resolved)

    holder = f"control-input:{control_id}"
    try:
        with pane_input_lease(resolved.pane_id, holder=holder, timeout=lease_timeout):
            return _deliver_under_lease(
                book,
                client,
                binding,
                text=text,
                enter=enter,
                terminal_id=terminal_id,
                resolved=resolved,
                digest=digest,
            )
    except PaneBusyError as exc:
        # Raised by the acquisition only; nothing inside the block can
        # produce it.  Nothing was written, so the control may be sent
        # again — which is why the journal re-arms a refused record.
        return _record_refusal(
            book,
            control_id,
            REASON_PANE_BUSY,
            f"another writer holds pane {resolved.pane_id}: {exc}",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )


def _deliver_under_lease(
    journal: ControlInputJournal,
    client: Any,
    binding: ControlInputBinding,
    *,
    text: str,
    enter: bool,
    terminal_id: str,
    resolved: ResolvedControlIdentity,
    digest: str,
) -> ControlInputResult:
    """Re-verify, claim, and write, all while holding the pane lease.

    The re-verification has to happen here rather than before the lease.
    Outside it, a pane can die and its terminal be replaced between the
    check and the write, and the control would land in a stranger's
    composer with every earlier check having passed honestly.
    """
    control_id = binding.request_id
    live = client.pane_control_identity(pane_id=binding.pane_id)
    if live is None or live.dead:
        return _record_refusal(
            journal,
            control_id,
            REASON_PANE_DEAD,
            f"pane {binding.pane_id} is gone or dead as of the write lease",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )
    if live.window_id != binding.window_id or live.pane_pid != binding.pane_pid:
        return _record_refusal(
            journal,
            control_id,
            REASON_IDENTITY_MISMATCH,
            f"pane {binding.pane_id} now reports window {live.window_id!r} and root pid "
            f"{live.pane_pid}, not the bound {binding.window_id!r} / {binding.pane_pid}; "
            "the pane this control was bound to is not the pane in front of it now",
            terminal_id=terminal_id,
            resolved=resolved,
            digest=digest,
        )

    claim = journal.claim_write(control_id)
    if not claim.granted:
        # Exactly one caller ever writes for a control id.  A caller
        # holding a refused claim must not write even when the record
        # looks abandoned: that owner may be mid-write this instant.
        return _from_record(claim.record, resolved=resolved)

    try:
        chunks = client.send_literal_line(binding.pane_id, text, submit=enter)
    except TmuxLiteralSendError as exc:
        journal.mark_ambiguous(
            control_id,
            reason_code=REASON_WRITE_INCOMPLETE,
            chunks_sent=exc.chunks_sent,
            enter_attempted=exc.enter_attempted,
            evidence_digest=digest,
        )
        return ControlInputResult(
            control_id=control_id,
            outcome=AMBIGUOUS,
            reason_code=REASON_WRITE_INCOMPLETE,
            detail=(
                f"the write failed part-way through: {exc}. What reached the pane is "
                "bounded by chunks_sent and enter_attempted but is not knowable exactly, "
                "so this control must not be sent again"
            ),
            state=STATE_AMBIGUOUS,
            terminal_id=terminal_id,
            request_digest=digest,
            resolved_identity=resolved.as_dict(),
            chunks_sent=exc.chunks_sent,
            enter_attempted=exc.enter_attempted,
        )
    except ValueError as exc:
        # Unreachable by construction: this service screens the text more
        # strictly than the primitive does, so a rejection here means the
        # two screens disagree, which is a bug in this file.  It is still
        # recorded rather than raised, because an unrecorded claim leaves
        # the record in 'writing' until a sweep guesses at it.  The
        # journal cannot record a refusal after a claim by design, so the
        # honest encoding is ambiguous with chunks_sent=0 — which tells
        # the caller nothing landed while still withholding the retry
        # licence that only a provable refusal may grant.
        logger.error("control-input screening disagreement for %s: %s", control_id, exc)
        journal.mark_ambiguous(
            control_id,
            reason_code=REASON_WRITE_INCOMPLETE,
            chunks_sent=0,
            enter_attempted=False,
            evidence_digest=digest,
        )
        return ControlInputResult(
            control_id=control_id,
            outcome=AMBIGUOUS,
            reason_code=REASON_WRITE_INCOMPLETE,
            detail=f"the write primitive rejected an already-screened control: {exc}",
            state=STATE_AMBIGUOUS,
            terminal_id=terminal_id,
            request_digest=digest,
            resolved_identity=resolved.as_dict(),
            chunks_sent=0,
            enter_attempted=False,
        )

    record = journal.mark_delivered(
        control_id, chunks_sent=chunks, enter_attempted=enter, evidence_digest=digest
    )
    return ControlInputResult(
        control_id=control_id,
        outcome=ACCEPTED,
        detail=(
            f"typed {chunks} literal write(s) into pane {binding.pane_id}"
            + (" and submitted with one Enter" if enter else " without submitting")
        ),
        state=record.state,
        terminal_id=terminal_id,
        request_digest=digest,
        resolved_identity=resolved.as_dict(),
        text_sent=True,
        enter_sent=enter,
        chunks_sent=chunks,
        enter_attempted=enter,
    )


# --- Resolving a lost response --------------------------------------------


def lookup_control_input(
    control_id: Any,
    *,
    journal: Optional[ControlInputJournal] = None,
) -> ControlInputResult:
    """What happened to one control id, for a caller whose reply was lost.

    Keyed by the control id alone and not by terminal.  The id is the
    journal's primary key, and scoping the lookup to a terminal would let
    a caller that asked about the wrong terminal be told 'nothing was
    written' about a control that was — the single most dangerous wrong
    answer this surface can give.  The terminal the control was actually
    bound to is in the reply.

    An id with no record answers ``refused``.  That is not a guess: the
    intent commits before any pane I/O, so the absence of a record is
    positive proof of the absence of a write.
    """
    if not isinstance(control_id, str) or not CONTROL_ID_PATTERN.match(control_id):
        raise ControlInputRequestInvalid(
            f"invalid control_id {control_id!r}: must match {CONTROL_ID_PATTERN.pattern}"
        )
    book = get_control_input_journal() if journal is None else journal
    # A request whose owning process died is stranded in a non-terminal
    # state and would otherwise answer 'in flight' forever.  Resolving it
    # here is what makes the crash window answerable by asking.
    book.sweep_stranded()
    record = book.find(control_id)
    if record is None:
        return ControlInputResult(
            control_id=control_id,
            outcome=REFUSED,
            reason_code=REASON_OWNER_LOST_BEFORE_WRITE,
            detail=(
                "no control-input record exists for this id on this server. The intent "
                "is committed before the first byte, so this proves nothing was written "
                "and the control may be sent again"
            ),
        )
    return _from_record(record)


# --- Capability advertisement ---------------------------------------------


def control_input_capabilities() -> Dict[str, Any]:
    """What this server implements, readable without sending a control.

    A caller cannot discover support by trying: a probe that succeeds has
    already typed something into somebody's composer.  This surface is
    therefore not a convenience — it is the only way to ask the question
    without answering it destructively.  On a server that predates the
    protocol the route is absent, and its ``404`` is the honest signal
    that resolves to a typed ``unsupported``.
    """
    return {
        "protocol": CONTROL_INPUT_PROTOCOL,
        "request_schema_version": CONTROL_INPUT_REQUEST_SCHEMA_VERSION,
        "digest_domain": CONTROL_INPUT_DIGEST_DOMAIN,
        "identity_fields": list(IDENTITY_FIELDS),
        "outcomes": sorted(CONTROL_INPUT_OUTCOMES),
        "reason_codes": sorted(CONTROL_INPUT_REASON_CODES),
        "max_text_bytes": MAX_TEXT_BYTES,
        "control_id_pattern": CONTROL_ID_PATTERN.pattern,
        # Stated so a caller never has to infer them from behaviour.
        "literal_write": True,
        "bracketed_paste": False,
        "enter_required": True,
        "execution_modes": [EXECUTION_MODE_NATIVE_TUI],
    }
