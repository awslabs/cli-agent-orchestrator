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
from typing import Any, Dict, Mapping, Optional, Union

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
    REASON_WRITE_DEADLINE: REFUSED,
    REASON_CONTROL_ROUTE_ABSENT: UNSUPPORTED,
    REASON_PROTOCOL_MISMATCH: UNSUPPORTED,
    REASON_RESPONSE_LOST: AMBIGUOUS,
    REASON_WRITE_INCOMPLETE: AMBIGUOUS,
    REASON_OWNER_LOST_MID_WRITE: AMBIGUOUS,
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
