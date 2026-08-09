"""Shared wire contract for the identity-bound control-input path.

One contract is shared by the server that delivers control input and by
every client that asks for it, so neither side can invent a meaning the
other does not hold.  It fixes four things and nothing else: the protocol
identifier, the closed set of typed outcomes, the closed set of refusal
reasons, and how a transport-level result becomes one of those outcomes.

Invariants this module encodes:

- Every control call resolves to exactly one of ``accepted``,
  ``refused``, ``ambiguous``, or ``unsupported``.  There is no untyped
  result and no silent success.
- ``refused`` is the only outcome that proves zero bytes reached the
  pane, because every refusal is decided before the first write.  It is
  therefore the only outcome a caller may follow with a fresh attempt.
- ``ambiguous`` is terminal for automation.  A lost or truncated
  response is resolved by an exact-request-id query, never by re-sending
  the same control.
- A control call made against a server that does not implement this
  protocol resolves to ``unsupported``.  It never degrades to ordinary
  paste delivery, raw key injection, or a best-effort retry on some
  other endpoint: a control the operator believes was delivered exactly
  once must never be delivered twice, or as different bytes, by a
  fallback path.

Constraint this contract places on the server implementation: the
control routes must never answer ``404`` for a terminal that is merely
unknown, expired, or unowned — those are typed ``refused`` results
carried in a ``200`` body.  ``404`` is reserved for "this server has no
control route at all", which is the one fact a client cannot otherwise
observe.  Overloading it would make an old server indistinguishable from
a missing terminal and hand callers a reason to guess.

Failure mode prevented: without a shared closed vocabulary, a caller
that loses a response has no honest answer available and reaches for the
ordinary input path, which is exactly how one requested control becomes
two delivered ones.

Reconciliation with the conductor client: the request digest here is
byte-identical to ``conduct/lib/control_input.py``'s ``request_digest``
— same domain string, same fixed field order, same nine identity fields,
same canonical encoder rules.  That is deliberate and it is not a
convenience.  Two independently-reasonable digests would each be correct
in isolation and would disagree only when a conflict actually needed
detecting, so the divergence would surface as a spurious rebind refusal
on exactly the request whose identity mattered most.  The definition is
therefore adopted from the side that committed it first rather than
re-derived here.

The physical write target — the canonical tmux server socket identity,
pane id, window id, and pane pid — is deliberately *not* in the preimage.
The client has never been told those values, so a preimage containing
them is one only the server can compute: it would look like agreement and
fail only under conflict, which is worse than no digest at all.  They are
instead mandatory server-side identity, resolved from the terminal and
re-verified under the pane lease before the first byte, and recorded as
their own binding columns in the journal.  A pane that moved is therefore
still a refusal and still a rebind; it is just decided by re-verification
against tmux rather than by a hash of facts the caller could only have
guessed.

Why the *server* socket is part of that target (§24.7): a tmux pane id is
scoped to one server, and several servers routinely run on one host.  A
pane id alone therefore names a pane on whichever server the writer's
process happens to resolve — and ``%3`` on a private fixture server is a
different pane from ``%3`` on the operator's default server, with no
error and no observable difference at the pane-id level.  Binding the
server's ``#{socket_path}`` realpath alongside the pane id is what makes
the target complete, and checking it at the writer boundary is what makes
the completeness enforced rather than assumed.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping, Optional, Union

from cli_agent_orchestrator.services.canonical_json import build_canonical, canonical_sha256

CONTROL_INPUT_PROTOCOL = "cao-control-input-v1"
CONTROL_INPUT_SCHEMA_VERSION = 1

# --- Typed outcomes -------------------------------------------------------

ACCEPTED = "accepted"
REFUSED = "refused"
AMBIGUOUS = "ambiguous"
UNSUPPORTED = "unsupported"

CONTROL_INPUT_OUTCOMES = frozenset({ACCEPTED, REFUSED, AMBIGUOUS, UNSUPPORTED})

# Only a refusal is decided before any pane write, so only a refusal
# leaves the pane provably untouched and permits a fresh attempt.  An
# accepted control has already happened; an ambiguous one may or may not
# have, which is precisely why it may not be repeated.
REATTEMPTABLE_OUTCOMES = frozenset({REFUSED})

# --- Provider-visible submission observation (cond-0026) -------------------

# What the provider visibly did with a submitted control, observed through
# the pane rather than inferred from transport acknowledgement.  Distinct
# from ``text_sent``/``enter_sent``: those prove tmux accepted bytes, while
# this proves the composer took them (or could not be proven to).  A
# control whose transport succeeded can still rest unsubmitted in the
# composer — the cond-0026 defect — so transport acceptance is never read
# as submission.
SUBMISSION_SUBMITTED = "submitted"
SUBMISSION_UNSUBMITTED = "unsubmitted"
SUBMISSION_UNKNOWN = "unknown"

SUBMISSION_OBSERVED_VALUES = frozenset(
    {SUBMISSION_SUBMITTED, SUBMISSION_UNSUBMITTED, SUBMISSION_UNKNOWN}
)

# ``submitted`` is an observation of the composer boundary only: the
# composer was seen to give up the control text.  It is not provider
# completion — the provider may still refuse, error, or never answer, and
# reconciling that remains an operator act exactly as before.  Nothing in
# this vocabulary may ever be mapped onto a provider-completion claim.
# ``unsubmitted`` and ``unknown`` never upgrade any verdict: a control
# whose submission is unproven stays ``ambiguous`` and is never re-driven.

# --- Refusal reasons ------------------------------------------------------

REASON_UNKNOWN_TERMINAL = "unknown-terminal"
REASON_IDENTITY_MISMATCH = "identity-mismatch"
REASON_STALE_GENERATION = "stale-generation"
REASON_LINEAGE_UNPROVEN = "lineage-unproven"
REASON_PANE_DEAD = "pane-dead"
REASON_PANE_BUSY = "pane-busy"
REASON_ILLEGAL_CONTROL_BYTES = "illegal-control-bytes"
REASON_MULTILINE_REJECTED = "multiline-rejected"
REASON_PROVIDER_UNSUPPORTED = "provider-unsupported"
REASON_MANAGED_ACP_PANE = "managed-acp-pane"
REASON_REQUEST_REBOUND = "request-rebound"
REASON_CONTROL_ROUTE_ABSENT = "control-route-absent"
REASON_PROTOCOL_MISMATCH = "protocol-mismatch"
REASON_RESPONSE_LOST = "response-lost"
REASON_WRITE_INCOMPLETE = "write-incomplete"
# The process that owned an in-flight request died.  Two reasons rather
# than one, because the durable record proves two different things
# depending on where it died, and a single reason would have to be read
# alongside the state to know which.  A reason that cannot be trusted on
# its own is exactly the hazard the outcome binding below exists to
# remove.
REASON_OWNER_LOST_BEFORE_WRITE = "owner-lost-before-write"
REASON_OWNER_LOST_MID_WRITE = "owner-lost-mid-write"
# The submitting Enter was acknowledged by tmux (or was deliberately
# withheld when the text never became compose-visible) but provider-visible
# submission could not be proven: the composer never showed the control as
# taken.  Bytes may have reached the pane, so this is terminal for
# automation -- a re-attempt could double-deliver -- and it is never
# upgraded by a later observation.  Bound to ``ambiguous`` because no
# zero-byte proof exists once the text write was acknowledged.
REASON_SUBMISSION_UNPROVEN = "submission-unproven"
# Canonical tmux server socket identity (§24.7).  Three reasons rather
# than one because a caller acts differently on each: unbound is a
# terminal that predates the binding and can never pass until it is
# re-created, unreadable is an observation that may succeed on the next
# attempt, and mismatch means the pane in front of the writer belongs to
# a different tmux server than the one this control was bound to — the
# case where a single reason would hide a cross-server delivery behind
# the same words as a transient read failure.
REASON_SERVER_IDENTITY_UNBOUND = "server-identity-unbound"
REASON_SERVER_IDENTITY_UNREADABLE = "server-identity-unreadable"
REASON_SERVER_IDENTITY_MISMATCH = "server-identity-mismatch"
# A v2 ``chord`` the server will not press for this provider/build.  Decided
# against the pinned steer-chord table before any write, so it is proven
# zero bytes and a caller may retry with an allowed chord (or none).
REASON_UNSUPPORTED_CHORD = "unsupported-chord"
# A blocking tmux call in the write path exceeded its bound, or the overall
# write deadline elapsed, before any pane byte was written.  Reattemptable:
# nothing reached the pane, and the next attempt may succeed on a healthy
# server.  A timeout on or after a pane-write call is ``ambiguous`` (the
# post-claim reason set), never this one.
REASON_WRITE_DEADLINE = "write-deadline"
# A v3 ``key`` event naming a key outside the normalized name set
# (:data:`SEQUENCE_KEY_NAMES`).  Decided against the pinned set before any
# write, so it is proven zero bytes and a caller may retry with a named key
# (or none).  Unsupported modifiers land here too: they are refused, never
# silently dropped and never approximated into a key that was not asked for.
REASON_UNSUPPORTED_KEY = "unsupported-key"
# A v3 event whose ``type`` this schema version does not define (forwards
# compatibility: a newer event form must fail closed, not be delivered as
# something it is not).  Decided before any write, so it is proven zero
# bytes.
REASON_UNREPRESENTABLE_EVENT = "unrepresentable-event"
# The bound pane's tmux copy-mode state could not be proven safe for a
# payload write: the pane was proven in copy mode and the exit control was
# not accepted or not confirmed, or the mode state could not be observed at
# all.  Decided before any payload byte — the copy-mode-exit control is the
# only keystroke that may have been sent, and it is payload to nobody — so
# this carries the zero-bytes proof and is reattemptable under the refused
# rule.  It never means delivery, transport success, or provider submission.
REASON_COPY_MODE_ACTIVE = "copy-mode-active"
# A v4 ``payload_class`` declaration the server cannot honour as made: the
# value is not the one declared class this schema defines (or is not a
# string at all), or a declared command's events do not match the command
# grammar.  Decided before any write, so it is proven zero bytes and the
# caller may retry with a corrected (or absent) declaration.  Never
# approximated into prose and never executed partially: a declaration the
# server cannot read is not licence to guess the caller meant prose.
REASON_MALFORMED_COMMAND_DECLARATION = "malformed-command-declaration"
# A declared command-class control found the composer holding content (or
# its emptiness could not be proven) at the pre-write observation under
# the pane-input lease: submitting the command would concatenate it with
# the queued prefill and deliver it as ordinary prompt text (the r5
# evidence).  Decided before any command byte, the prefill is untouched,
# and the refusal is reattemptable — the operator clears or submits the
# prefill and tries again.  Blind clearing is prohibited: no keystroke
# ritual may be specified as a clear, because prefill has been observed
# to survive Escape.
REASON_COMPOSER_NONEMPTY = "composer-nonempty"
# An M3/W13 permanent generation fence is decided before the first provider
# byte.  It is intentionally a normal typed refusal, not an ambiguous
# transport failure: callers must advance to a successor, never retry this
# parked generation.
REASON_GENERATION_FENCED = "generation-fenced"

# Every reason is bound to the one outcome it can honestly carry.
#
# The binding is the enforcement point for the rule that makes
# REATTEMPTABLE_OUTCOMES safe: a refusal is decided before the first
# byte.  ``response-lost`` and ``write-incomplete`` describe *post*-
# attempt uncertainty, so carrying either with ``refused`` would license
# a caller to re-send bytes that may already have reached the pane —
# precisely the double delivery this lane exists to prevent.  Binding
# them to ``ambiguous`` here means no call site can make that mistake,
# including one written later by someone who never read this comment.
REASON_OUTCOMES: "dict[str, str]" = {
    REASON_UNKNOWN_TERMINAL: REFUSED,
    REASON_IDENTITY_MISMATCH: REFUSED,
    REASON_STALE_GENERATION: REFUSED,
    REASON_LINEAGE_UNPROVEN: REFUSED,
    REASON_PANE_DEAD: REFUSED,
    REASON_PANE_BUSY: REFUSED,
    REASON_ILLEGAL_CONTROL_BYTES: REFUSED,
    REASON_MULTILINE_REJECTED: REFUSED,
    REASON_PROVIDER_UNSUPPORTED: REFUSED,
    REASON_MANAGED_ACP_PANE: REFUSED,
    REASON_REQUEST_REBOUND: REFUSED,
    REASON_OWNER_LOST_BEFORE_WRITE: REFUSED,
    # All three are decided before the first byte, so all three carry the
    # zero-bytes proof that makes `refused` re-attemptable.  A server
    # identity that cannot be read is refused rather than made ambiguous
    # for exactly that reason: the write never started.
    REASON_SERVER_IDENTITY_UNBOUND: REFUSED,
    REASON_SERVER_IDENTITY_UNREADABLE: REFUSED,
    REASON_SERVER_IDENTITY_MISMATCH: REFUSED,
    REASON_UNSUPPORTED_CHORD: REFUSED,
    REASON_UNSUPPORTED_KEY: REFUSED,
    REASON_UNREPRESENTABLE_EVENT: REFUSED,
    REASON_COPY_MODE_ACTIVE: REFUSED,
    REASON_MALFORMED_COMMAND_DECLARATION: REFUSED,
    REASON_COMPOSER_NONEMPTY: REFUSED,
    REASON_GENERATION_FENCED: REFUSED,
    REASON_WRITE_DEADLINE: REFUSED,
    REASON_CONTROL_ROUTE_ABSENT: UNSUPPORTED,
    REASON_PROTOCOL_MISMATCH: UNSUPPORTED,
    REASON_RESPONSE_LOST: AMBIGUOUS,
    REASON_WRITE_INCOMPLETE: AMBIGUOUS,
    REASON_OWNER_LOST_MID_WRITE: AMBIGUOUS,
    REASON_SUBMISSION_UNPROVEN: AMBIGUOUS,
}

CONTROL_INPUT_REASON_CODES = frozenset(REASON_OUTCOMES)

# No reason may license a re-attempt unless its outcome does.  Asserted
# at import so the two tables can never drift apart unnoticed.
assert all(outcome in CONTROL_INPUT_OUTCOMES for outcome in REASON_OUTCOMES.values())


def outcome_for_reason(reason_code: str) -> str:
    """The one outcome ``reason_code`` may be reported with.

    Raises:
        ValueError: The reason is not in the closed set.  An unknown
            reason is refused rather than defaulted, because every
            default is wrong in one direction: defaulting to ``refused``
            invents a licence to retry, and defaulting to ``ambiguous``
            strands a request that was genuinely never written.
    """
    try:
        return REASON_OUTCOMES[reason_code]
    except KeyError:
        raise ValueError(f"Unknown control-input reason code: {reason_code!r}") from None


def is_reattemptable(outcome: str) -> bool:
    """Whether a caller may send this control again."""
    return outcome in REATTEMPTABLE_OUTCOMES


# --- Control target identity ---------------------------------------------

# A tmux pane id is '%' followed by a decimal counter.  Anything else is
# refused before it can reach a '-t' argument, where ':' and '.' are
# target delimiters and a leading '-' is an option.  The write primitive,
# the arbiter, and the journal share this one definition so they cannot
# disagree about what a legal control target is — a pane the arbiter
# would lock but the writer would reject, or the reverse, is a hole.
PANE_ID_PATTERN = re.compile(r"^%[0-9]{1,10}$")


def is_valid_pane_id(pane_id: object) -> bool:
    """Whether ``pane_id`` is a syntactically legal tmux pane id."""
    return isinstance(pane_id, str) and PANE_ID_PATTERN.fullmatch(pane_id) is not None


# --- Canonical tmux server socket identity (§24.7) ------------------------


def normalize_server_identity(value: object) -> Optional[str]:
    """The comparable canonical form of one tmux server's socket identity.

    A tmux server's only durable identity is the socket it answers on, so
    the socket path is the identity — but the *same* server reports paths
    that differ as text: ``/tmp/x/server.sock`` and
    ``/private/tmp/x/server.sock`` are one socket on macOS, and comparing
    them as raw strings would refuse a write to the very server it was
    bound to.  Every value therefore passes through ``realpath`` on the
    way in and on the way out, so both sides of every comparison are the
    same spelling of the same thing.

    ``realpath`` is used rather than ``resolve(strict=True)`` on purpose:
    it is purely textual for the leaf, so a socket file that has since
    been unlinked still normalises to the identity it had.  A binding that
    silently stopped being comparable the moment its server exited would
    fail open exactly when the pane id it guards became reusable.

    Returns None for anything that cannot name a server — absent, empty,
    not a string, or relative.  None means "no identity", never "any
    identity": :func:`server_identity_refusal` treats it as a refusal.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not os.path.isabs(text):
        return None
    return os.path.realpath(text)


def server_identity_refusal(*, bound: object, observed: object) -> Optional["tuple[str, str]"]:
    """Reason and detail for refusing this target, or None to proceed.

    The one place the server-identity decision is made.  The writer
    primitive and the control-input service both call it, so the byte-level
    refusal and the typed wire outcome can never disagree about whether a
    given pair was acceptable — a disagreement there would be a write the
    service believed it had refused.

    Order is deliberate: an unbound target is reported as unbound even when
    the live observation also failed, because "this terminal never recorded
    which server it lives on" is a durable fact the caller must act on,
    while an unreadable observation invites a re-attempt that would never
    succeed for it.
    """
    bound_identity = normalize_server_identity(bound)
    observed_identity = normalize_server_identity(observed)
    if bound_identity is None:
        return (
            REASON_SERVER_IDENTITY_UNBOUND,
            "this terminal has no canonical tmux server identity bound to it, so a "
            "write could only be aimed at whichever server this process happens to "
            "resolve; a pane id is scoped to one server and names a different pane "
            "on every other one",
        )
    if observed_identity is None:
        return (
            REASON_SERVER_IDENTITY_UNREADABLE,
            f"the tmux server owning this pane did not report a usable socket "
            f"identity, so it cannot be shown to be the bound "
            f"{bound_identity!r}; nothing is written on an unproven target",
        )
    if observed_identity != bound_identity:
        return (
            REASON_SERVER_IDENTITY_MISMATCH,
            f"this pane belongs to the tmux server at {observed_identity!r}, not the "
            f"bound {bound_identity!r}; the same pane id exists on both servers and "
            "names a different pane on each, so the write would land in a stranger's "
            "composer",
        )
    return None


# --- The request digest ---------------------------------------------------

# Domain separation for the request digest.  Deliberately a different
# string from CONTROL_INPUT_PROTOCOL, so the same bytes under a different
# purpose cannot collide with a control-input request digest and so
# domain separation cannot be satisfied by accident.
CONTROL_INPUT_DIGEST_DOMAIN = "cao-control-input-request-v1"

# Schema v2 adds exactly one field, ``chord``, and travels under its own
# domain so a v2 request can never collide with a v1 one.  The domain is
# the cross-repo contract: both sides pin it, and the golden vector in the
# activation spec asserts the bytes (see the v2 test class).
CONTROL_INPUT_DIGEST_DOMAIN_V2 = "cao-control-input-request-v2"

# Schema of the wire request, versioned separately from the protocol
# identifier because the digest's field set may need to move without the
# protocol name moving.
CONTROL_INPUT_REQUEST_SCHEMA_VERSION = 1
CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V2 = 2

# The identity a request may be bound to, in the fixed order the digest
# encodes.  Every field is named explicitly and an absent expectation is
# written as an explicit null rather than omitted: a request that expects
# nothing and a request whose expectation was dropped in transit must not
# produce the same digest.
IDENTITY_FIELDS = (
    "terminal_id",
    "terminal_incarnation",
    "terminal_generation",
    "pane_birth_id",
    "provider_process_id",
    "provider",
    "native_session_id",
    "execution_mode",
    "session_name",
)

# Fixed field order for the digest preimage.  Never lexicographic: the
# order is part of the contract each side reproduces.
REQUEST_DIGEST_FIELD_ORDER = (
    "domain",
    "schema_version",
    "control_id",
    "text",
    "enter",
    "expected_identity",
)

# v2 splices ``chord`` between ``enter`` and ``expected_identity``.  Kept as
# its own tuple so the v1 order above (and the v1 golden vector that pins
# it) is untouched: a v2-capable server must not change what a v1 request
# digests to.
REQUEST_DIGEST_FIELD_ORDER_V2 = (
    "domain",
    "schema_version",
    "control_id",
    "text",
    "enter",
    "chord",
    "expected_identity",
)


def normalize_expected_identity(identity: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the expectation in fixed field order, absences as null.

    An unknown key is refused rather than ignored, because a misspelled
    field name silently becomes "no expectation for the field I meant":
    the request would bind to nothing and be accepted against the wrong
    pane, which is the exact outcome identity binding exists to prevent.
    """
    if identity is None:
        identity = {}
    if not isinstance(identity, Mapping):
        raise ValueError(f"expected_identity must be a mapping, got {type(identity).__name__}")
    unknown = sorted(key for key in identity if key not in IDENTITY_FIELDS)
    if unknown:
        raise ValueError(
            f"unknown expected-identity field(s) {unknown!r}; known fields are "
            f"{list(IDENTITY_FIELDS)!r}. Rejected rather than ignored: an ignored "
            "misspelling silently drops the binding."
        )
    normalized: Dict[str, Any] = {}
    for name in IDENTITY_FIELDS:
        value = identity.get(name)
        if value is None:
            normalized[name] = None
        elif isinstance(value, bool):
            raise ValueError(f"expected-identity field {name!r} must not be a boolean")
        elif isinstance(value, int):
            if value < 0:
                raise ValueError(
                    f"expected-identity field {name!r} must be non-negative, got {value!r}"
                )
            normalized[name] = value
        elif isinstance(value, str):
            if value == "":
                raise ValueError(
                    f"expected-identity field {name!r} is an empty string; use an absent "
                    "field to mean 'no expectation'"
                )
            normalized[name] = value
        else:
            raise ValueError(
                f"expected-identity field {name!r} must be a string, a non-negative "
                f"integer, or absent; got {type(value).__name__}"
            )
    return normalized


def _validate_chord(chord: Any) -> Optional[str]:
    """Normalise a v2 ``chord`` field to its canonical wire form.

    A chord is optional: ``None`` means "this v2 request names no chord",
    which is a valid v2 shape (a future caller may speak v2 for another
    reason).  A present chord is a non-empty string naming one
    provider-pinned composer chord.  The empty string is refused rather
    than read as an absent chord, because "I sent an empty chord" and "I
    sent no chord" must not produce the same digest -- a caller reading a
    digest mismatch would otherwise be unable to tell an absent feature
    from a malformed one.

    Allowlist membership is *not* decided here.  This function pins the
    wire type only; whether a given chord is licensed for a given provider
    and build is a service-layer fact decided against the proven composer
    evidence, so a digest can be computed for any syntactically valid v2
    request (including one a server will then refuse) without inventing a
    binding the two sides could disagree about.
    """
    if chord is None:
        return None
    if not isinstance(chord, str):
        raise ValueError(f"chord must be a string or null, got {type(chord).__name__}")
    if chord == "":
        raise ValueError(
            "chord is an empty string; use null to mean 'no chord', because an "
            "empty chord and an absent chord must not digest alike"
        )
    return chord


def control_input_request_digest(
    *,
    control_id: str,
    text: str,
    enter: bool,
    expected_identity: Optional[Mapping[str, Any]],
    schema_version: int = CONTROL_INPUT_REQUEST_SCHEMA_VERSION,
) -> str:
    """Digest binding one control id to the exact control it authorises.

    The preimage is one canonical object in :data:`REQUEST_DIGEST_FIELD_ORDER`,
    SHA-256 over its exact bytes.  It covers the target identity as well
    as the payload, so a replayed control id carrying the same text but
    pointed at a different pane is as detectable as one carrying
    different text.  Both are the same failure: a control the operator
    authorised once being applied somewhere, or to something, they did
    not authorise.

    Only wire fields participate.  Anything either side keeps privately —
    a journal's own bookkeeping, a client's command classification — is
    excluded, because a digest one side cannot reproduce is worse than no
    digest: it looks like agreement and fails only under conflict.

    This is byte-identical to the conductor's ``request_digest``; see the
    reconciliation note in the module docstring.
    """
    if not isinstance(enter, bool):
        raise ValueError(f"enter must be a bool, got {type(enter).__name__}")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("request schema_version must be an integer")
    return canonical_sha256(
        build_canonical(
            [
                ("domain", CONTROL_INPUT_DIGEST_DOMAIN),
                ("schema_version", schema_version),
                ("control_id", control_id),
                ("text", text),
                ("enter", enter),
                ("expected_identity", normalize_expected_identity(expected_identity)),
            ]
        )
    )


def control_input_request_digest_v2(
    *,
    control_id: str,
    text: str,
    enter: bool,
    chord: Optional[str],
    expected_identity: Optional[Mapping[str, Any]],
) -> str:
    """The v2 digest: the same binding as v1, plus the ``chord`` field.

    The preimage is :data:`REQUEST_DIGEST_FIELD_ORDER_V2` under the v2
    domain.  ``chord`` participates so a request that names a chord is a
    different request from one that names none (or a different chord), and a
    caller that authorised a chord control is bound to exactly that chord
    the way it is bound to exactly that text.

    Byte-identical to the conductor's v2 ``request_digest`` by the same
    reconciliation that governs v1: the field order, the domain, and the
    canonical encoder are the contract, asserted by a cross-implementation
    golden vector on each side.

    Allowlist membership is *not* checked here -- a digest must be computable
    for any syntactically valid v2 request, including one a server will then
    refuse, so the two sides never disagree about which requests exist.
    """
    if not isinstance(enter, bool):
        raise ValueError(f"enter must be a bool, got {type(enter).__name__}")
    normalised_chord = _validate_chord(chord)
    return canonical_sha256(
        build_canonical(
            [
                ("domain", CONTROL_INPUT_DIGEST_DOMAIN_V2),
                ("schema_version", CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V2),
                ("control_id", control_id),
                ("text", text),
                ("enter", enter),
                ("chord", normalised_chord),
                ("expected_identity", normalize_expected_identity(expected_identity)),
            ]
        )
    )


# --- Schema v3: ordered structured event sequences ------------------------

# Schema v3 replaces the v1/v2 payload fields with an ordered ``events``
# array and travels under its own domain, so a sequence request can never
# collide with a v1 or v2 one.  A v3 request carries ``events`` *or* the
# v1/v2 fields, never both; v1/v2 domains, digests, and behavior are
# byte-identical regardless.
CONTROL_INPUT_DIGEST_DOMAIN_V3 = "cao-control-input-request-v3"
CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V3 = 3

# Fixed field order for the v3 digest preimage.  ``events`` sits where the
# v1/v2 payload fields sat: after the control id, before the identity.
REQUEST_DIGEST_FIELD_ORDER_V3 = (
    "domain",
    "schema_version",
    "control_id",
    "events",
    "expected_identity",
)

SEQUENCE_EVENT_TYPE_TEXT = "text"
SEQUENCE_EVENT_TYPE_KEY = "key"
SEQUENCE_EVENT_TYPE_CHORD = "chord"
SEQUENCE_EVENT_TYPES = frozenset(
    {SEQUENCE_EVENT_TYPE_TEXT, SEQUENCE_EVENT_TYPE_KEY, SEQUENCE_EVENT_TYPE_CHORD}
)

# The normalized key-name set a ``key`` event may name (cond-0175 §5,
# extended in place by the native-TUI-console track §3.2): the exact
# representable control keystrokes, and nothing else.  A name outside
# this set — including any modifier combination it does not list — is
# refused with ``unsupported-key`` before any write rather than dropped or
# approximated.  Membership is a service-layer decision; it is deliberately
# *not* enforced by the digest path, for the same reason chord allowlist
# membership is not: a digest must be computable for a request the server
# will then refuse.
#
# The eleven navigation/editing keys extend the deployed five under the
# same request schema v3 (design D1): a key name is an opaque string
# inside ``events``, so the digest preimage shape is unchanged, an old
# server typed-refuses a new key ``unsupported-key`` with zero bytes, and
# a new client gates on the advertised ``sequence.keys``.  ``BTab``,
# modified arrows (``C-Up``), and ``F1``-``F12`` stay outside the set: the
# tmux mechanism exists but no managed provider has evidence of consuming
# them, so they are refused until a registry pin admits them.
SEQUENCE_KEY_NAMES = frozenset(
    {
        # Deployed (cond-0175).
        "Escape",
        "C-c",
        "C-s",
        "Enter",
        "Backspace",
        # Navigation/editing (native-TUI-console §3.2, P1).
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Delete",
        "Insert",
        "Tab",
    }
)

# Hard caps, both checked as shape errors before anything is journaled or
# written: at most 32 events per sequence, and at most 512 UTF-8 bytes of
# text across the whole sequence (the v1 single-text limit reused as the
# aggregate).  A sequence is a short control burst, not a script.
MAX_SEQUENCE_EVENTS = 32
MAX_SEQUENCE_TEXT_BYTES = 512

# Per-event outcome vocabulary.  One sequence is one at-most-once operation,
# but its events land in order, so a failure mid-sequence has a per-event
# boundary that must be recorded honestly rather than flattened into the
# whole-sequence outcome:
#
# - ``sent``: tmux acknowledged this event's write.
# - ``attempted``: the write was initiated and may have reached the pane;
#   whether it did is not knowable.  The sequence as a whole is
#   ``ambiguous`` the moment any event is in this state.
# - ``skipped``: never initiated — an earlier event's failure stopped the
#   sequence first, so zero bytes are proven for this event.
# - ``refused``: the sequence was refused before any write, so zero bytes
#   are proven for every event.
EVENT_OUTCOME_SENT = "sent"
EVENT_OUTCOME_ATTEMPTED = "attempted"
EVENT_OUTCOME_SKIPPED = "skipped"
EVENT_OUTCOME_REFUSED = "refused"
SEQUENCE_EVENT_OUTCOMES = frozenset(
    {
        EVENT_OUTCOME_SENT,
        EVENT_OUTCOME_ATTEMPTED,
        EVENT_OUTCOME_SKIPPED,
        EVENT_OUTCOME_REFUSED,
    }
)


def _validate_sequence_event(event: Any) -> Dict[str, Any]:
    """Normalise one sequence event to its canonical wire shape.

    Structural validation only.  The returned mapping has the fixed key
    order the digest encodes: ``type`` first, then the one payload field
    the type carries.  An unknown *bare* type normalises to its name alone
    so a server can still digest (and then typed-refuse) an event form a
    newer schema version defines; an unknown type carrying other fields is
    a shape error, because there is no canonical order for fields this
    schema does not define and guessing one would fork the digest.
    """
    if not isinstance(event, Mapping):
        raise ValueError(f"a sequence event must be an object, got {type(event).__name__}")
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type == "":
        raise ValueError("a sequence event requires a non-empty string 'type'")
    if event_type not in SEQUENCE_EVENT_TYPES:
        if len(event) != 1:
            raise ValueError(
                f"event type {event_type!r} is not defined by this schema version, so "
                "no canonical field order exists for its other fields; a bare "
                "{'type': ...} probe is the only digestible unknown form"
            )
        return {"type": event_type}
    allowed = {"type", event_type}
    unknown = sorted(key for key in event if key not in allowed)
    if unknown:
        raise ValueError(
            f"a {event_type!r} event carries unknown field(s) {unknown!r}; rejected "
            "rather than ignored, because an ignored field silently drops part of "
            "the request the digest is supposed to bind"
        )
    if event_type == SEQUENCE_EVENT_TYPE_TEXT:
        text = event.get("text")
        if not isinstance(text, str) or text == "":
            raise ValueError("a 'text' event requires a non-empty string 'text'")
        encoded = len(text.encode("utf-8"))
        if encoded > MAX_SEQUENCE_TEXT_BYTES:
            raise ValueError(
                f"a 'text' event is {encoded} UTF-8 bytes, over the "
                f"{MAX_SEQUENCE_TEXT_BYTES}-byte per-event limit"
            )
        return {"type": event_type, "text": text}
    if event_type == SEQUENCE_EVENT_TYPE_KEY:
        key = event.get("key")
        if not isinstance(key, str) or key == "":
            raise ValueError("a 'key' event requires a non-empty string 'key'")
        return {"type": event_type, "key": key}
    # chord events reuse the v2 wire-field validation exactly — except that
    # the field is mandatory here: a v2 request may name no chord, but a
    # chord *event* without a chord is no event.
    chord = event.get("chord")
    if chord is None:
        raise ValueError("a 'chord' event requires a non-empty string 'chord'")
    return {"type": event_type, "chord": _validate_chord(chord)}


def normalize_sequence_events(events: Any) -> List[Dict[str, Any]]:
    """Normalise an ordered ``events`` array to its canonical wire shape.

    Raises ``ValueError`` on any malformed shape: a request that cannot be
    normalised cannot be digested, and an undigestible control is no
    control.  Printable/content screening of text events and all membership
    decisions (key names, chord allowlists) happen at the service layer —
    this function pins structure and caps so the digest below is computable
    for every syntactically valid sequence.
    """
    if not isinstance(events, (list, tuple)):
        raise ValueError(f"events must be an array, got {type(events).__name__}")
    if len(events) == 0:
        raise ValueError("events must name at least one event; an empty sequence is no control")
    if len(events) > MAX_SEQUENCE_EVENTS:
        raise ValueError(
            f"a sequence carries {len(events)} events, over the {MAX_SEQUENCE_EVENTS}-event "
            "limit; a sequence is a short control burst, not a script"
        )
    normalised = [_validate_sequence_event(event) for event in events]
    aggregate = sum(
        len(event["text"].encode("utf-8"))
        for event in normalised
        if event["type"] == SEQUENCE_EVENT_TYPE_TEXT
    )
    if aggregate > MAX_SEQUENCE_TEXT_BYTES:
        raise ValueError(
            f"the sequence carries {aggregate} UTF-8 bytes of text across its events, "
            f"over the {MAX_SEQUENCE_TEXT_BYTES}-byte aggregate limit"
        )
    return normalised


def control_input_request_digest_v3(
    *,
    control_id: str,
    events: Any,
    expected_identity: Optional[Mapping[str, Any]],
) -> str:
    """The v3 digest: the same binding as v1/v2, over an ordered event array.

    The preimage is :data:`REQUEST_DIGEST_FIELD_ORDER_V3` under the v3
    domain.  Event order participates, so ``[Escape, Enter]`` and
    ``[Enter, Escape]`` are different requests — they are different
    controls, and a caller authorised exactly one of them.

    Byte-identical to the conductor's v3 ``request_digest`` by the same
    reconciliation that governs v1/v2: the field order, the domain, the
    per-event canonical shape, and the canonical encoder are the contract,
    asserted by a cross-implementation golden vector on each side.

    Membership is *not* checked here — a digest must be computable for any
    syntactically valid v3 request, including one a server will then
    refuse, so the two sides never disagree about which requests exist.
    """
    return canonical_sha256(
        build_canonical(
            [
                ("domain", CONTROL_INPUT_DIGEST_DOMAIN_V3),
                ("schema_version", CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V3),
                ("control_id", control_id),
                ("events", normalize_sequence_events(events)),
                ("expected_identity", normalize_expected_identity(expected_identity)),
            ]
        )
    )


# --- Schema v4: the declaration carrier (command, interactive) --------------
#
# Schema v4 is v3 plus exactly one optional additive field, ``payload_class``.
# Its defined values are ``"command"`` (native-TUI-console §4.1, r7) and
# ``"interactive"`` (§6.7, r15 — the armed manual streaming capture's declared
# intent, which bypasses only the provider-turn readiness gate and the kimi
# dispatch grace).  The field travels under its own domain so a declared
# request can never collide with an undeclared one: a request that declares
# command-class is a different request from one that does not, and a field
# that did not participate would digest a declared and an undeclared request
# of the same id and events alike (rebound blindness) — the same reason
# ``chord`` participates in v2.  v1/v2/v3 requests and their domains are
# byte-unchanged; a request with ``payload_class`` absent is a v3 request
# and digests under the v3 domain exactly as before.
CONTROL_INPUT_DIGEST_DOMAIN_V4 = "cao-control-input-request-v4"
CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4 = 4

# The declared payload classes.  ``None`` (absent) means prose.  Command
# detection is NEVER derived from payload shape: a batch whose text happens
# to begin with ``/`` (e.g. a streamed utterance split so a batch starts
# ``/tmp/x``) is undeclared prose and never enters the composer guard.
PAYLOAD_CLASS_COMMAND = "command"
#: §6.7 (r15): the armed manual streaming capture's declaration — ordinary
#: v3-valid sequence grammar (never command grammar), bypassing only the
#: provider IDLE/COMPLETED turn-state refusal and the kimi dispatch grace.
#: Only the armed capture surface declares it; automation never does.
PAYLOAD_CLASS_INTERACTIVE = "interactive"
DECLARED_PAYLOAD_CLASSES = frozenset({PAYLOAD_CLASS_COMMAND, PAYLOAD_CLASS_INTERACTIVE})

# Fixed field order for the v4 digest preimage: the v3 order with
# ``payload_class`` spliced between ``events`` and ``expected_identity``.
REQUEST_DIGEST_FIELD_ORDER_V4 = (
    "domain",
    "schema_version",
    "control_id",
    "events",
    "payload_class",
    "expected_identity",
)


def command_declaration_violation(events: List[Dict[str, Any]]) -> Optional[str]:
    """Why these normalized events fail the declared-command grammar, or None.

    The grammar a declared command-class request must match (§4.1): exactly
    one ``text`` event whose text begins with ``/``, optionally followed by
    one ``key:Enter`` (the fused submitting Enter — the registry Compact
    shape).  Anything else under a declaration is malformed: it is never
    approximated into prose and never executed partially.  Takes the
    *normalized* events so the check reads exactly what the digest bound.
    """
    first = events[0]
    if first["type"] != SEQUENCE_EVENT_TYPE_TEXT or not first["text"].startswith("/"):
        return (
            "a declared command is exactly one text event whose text begins with "
            "'/', optionally followed by one Enter key; the first event is not that text"
        )
    if len(events) == 1:
        return None
    if (
        len(events) == 2
        and events[1]["type"] == SEQUENCE_EVENT_TYPE_KEY
        and events[1]["key"] == "Enter"
    ):
        return None
    return (
        "a declared command carries at most one Enter key after its command text "
        "(the fused submitting Enter); further events make the declaration malformed"
    )


def control_input_request_digest_v4(
    *,
    control_id: str,
    events: Any,
    payload_class: Any,
    expected_identity: Optional[Mapping[str, Any]],
) -> str:
    """The v4 digest: the v3 binding plus the declared ``payload_class``.

    The preimage is :data:`REQUEST_DIGEST_FIELD_ORDER_V4` under the v4
    domain.  ``payload_class`` participates for the same reason ``chord``
    does in v2: a request that declares command-class is a different
    request from one that does not, and a non-participating field would
    digest a declared and an undeclared request of the same id and events
    alike.  ``None`` spells the absent declaration (prose) as an explicit
    null, so "v4 with no declaration" and "declared" can never collide.

    Declaration *validity* is not checked here — a digest must be
    computable for any syntactically declarable v4 request, including one
    the server will then refuse ``malformed-command-declaration``, so the
    two sides never disagree about which requests exist.  The field's wire
    type is pinned (string or null): anything else is not declarable and
    raises, the way a non-string ``chord`` raises in v2.
    """
    if payload_class is not None and not isinstance(payload_class, str):
        raise ValueError(
            f"payload_class must be a string or null, got {type(payload_class).__name__}"
        )
    return canonical_sha256(
        build_canonical(
            [
                ("domain", CONTROL_INPUT_DIGEST_DOMAIN_V4),
                ("schema_version", CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4),
                ("control_id", control_id),
                ("events", normalize_sequence_events(events)),
                ("payload_class", payload_class),
                ("expected_identity", normalize_expected_identity(expected_identity)),
            ]
        )
    )


# --- Bracketed-paste sentinels -------------------------------------------

# DECSET 2004 paste framing.  tmux emits these only for a pane whose
# application advertised ?2004h; a pane that never advertised it receives
# them as ordinary bytes and renders them as ^[[200~ / ^[[201~ inside the
# composer.  The control path never emits them under any condition.
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

# The same two sequences in their single-byte C1 CSI spelling.  U+009B is
# the 8-bit form of ``ESC [``, and a terminal in 8-bit mode treats the two
# spellings identically.  Screening only the ESC form would leave the C1
# form as a working way to smuggle the identical framing through — and a
# screen with a known bypass is not a screen.
BRACKETED_PASTE_START_C1 = "\x9b200~"
BRACKETED_PASTE_END_C1 = "\x9b201~"

BRACKETED_PASTE_SENTINELS = (
    BRACKETED_PASTE_START,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START_C1,
    BRACKETED_PASTE_END_C1,
)


def contains_bracketed_paste_sentinel(text: Union[str, bytes]) -> bool:
    """Whether ``text`` already carries a paste sentinel.

    Checked on both the control path and the ordinary path.  Sentinel
    bytes inside a payload are never harmless: a caller-supplied
    ``\\x1b[201~`` closes an ordinary bracketed paste early, so the
    remainder is interpreted as keystrokes rather than pasted text.
    """
    if isinstance(text, bytes):
        # Two encodings per sentinel.  A byte stream that came off a
        # terminal carries C1 as the raw byte 0x9b, while the same text
        # encoded as UTF-8 carries it as 0xc2 0x9b.  Screening only one
        # spelling would leave the other as a working bypass.
        return any(
            sentinel.encode("utf-8") in text or sentinel.encode("latin-1") in text
            for sentinel in BRACKETED_PASTE_SENTINELS
        )
    return any(sentinel in text for sentinel in BRACKETED_PASTE_SENTINELS)


# --- Transport classification --------------------------------------------

# 404 means the route does not exist on this server, which is the sole
# honest signal that the peer predates this protocol.  405/501 carry the
# same meaning for a server that routes the path but does not implement
# the method.
_UNSUPPORTED_STATUSES = frozenset({404, 405, 501})

# The request was rejected before the handler could touch a pane.
_REFUSED_STATUSES = frozenset({400, 401, 403, 409, 422, 429})

# The request may or may not have reached the pane before the transport
# gave up.  Guessing "nothing happened" here is how a control gets sent
# twice, so these are ambiguous by construction.
_AMBIGUOUS_STATUSES = frozenset({408, 425, 500, 502, 503, 504})


def classify_transport_status(
    status_code: Optional[int],
    *,
    protocol_mismatch: bool = False,
) -> Optional[str]:
    """Map a transport result onto a typed outcome, or ``None``.

    ``None`` is returned only for ``200``, which means the response body
    carries the authoritative typed outcome and this function must not
    second-guess it.  Every other result — including no response at all,
    passed as ``status_code=None`` — resolves to an outcome here.

    Args:
        status_code: HTTP status observed, or ``None`` when the response
            was never received (timeout, reset, or dropped connection).
        protocol_mismatch: True when a ``422`` was produced by the
            protocol-version literal rather than by the request body.  A
            server that rejects this protocol's identifier does not
            implement it, which is ``unsupported`` rather than a refusal
            the caller could fix.

    Returns:
        One of the outcome constants, or ``None`` to read the body.
    """
    if status_code is None:
        # No response is not evidence of no delivery.
        return AMBIGUOUS
    if status_code == 200:
        return None
    if status_code in _UNSUPPORTED_STATUSES:
        return UNSUPPORTED
    if status_code == 422 and protocol_mismatch:
        return UNSUPPORTED
    if status_code in _REFUSED_STATUSES:
        return REFUSED
    if status_code in _AMBIGUOUS_STATUSES:
        return AMBIGUOUS
    if 400 <= status_code < 500:
        return REFUSED
    # 1xx/3xx/5xx and anything unrecognised: the pane state is unknown,
    # so the only truthful answer is the one that forbids a retry.
    return AMBIGUOUS
