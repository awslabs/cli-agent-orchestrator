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
"""

from __future__ import annotations

from typing import Optional, Union

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

CONTROL_INPUT_REASON_CODES = frozenset(
    {
        REASON_UNKNOWN_TERMINAL,
        REASON_IDENTITY_MISMATCH,
        REASON_STALE_GENERATION,
        REASON_LINEAGE_UNPROVEN,
        REASON_PANE_DEAD,
        REASON_PANE_BUSY,
        REASON_ILLEGAL_CONTROL_BYTES,
        REASON_MULTILINE_REJECTED,
        REASON_PROVIDER_UNSUPPORTED,
        REASON_MANAGED_ACP_PANE,
        REASON_REQUEST_REBOUND,
        REASON_CONTROL_ROUTE_ABSENT,
        REASON_PROTOCOL_MISMATCH,
        REASON_RESPONSE_LOST,
        REASON_WRITE_INCOMPLETE,
    }
)

# --- Bracketed-paste sentinels -------------------------------------------

# DECSET 2004 paste framing.  tmux emits these only for a pane whose
# application advertised ?2004h; a pane that never advertised it receives
# them as ordinary bytes and renders them as ^[[200~ / ^[[201~ inside the
# composer.  The control path never emits them under any condition.
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
BRACKETED_PASTE_SENTINELS = (BRACKETED_PASTE_START, BRACKETED_PASTE_END)


def contains_bracketed_paste_sentinel(text: Union[str, bytes]) -> bool:
    """Whether ``text`` already carries a paste sentinel.

    Checked on both the control path and the ordinary path.  Sentinel
    bytes inside a payload are never harmless: a caller-supplied
    ``\\x1b[201~`` closes an ordinary bracketed paste early, so the
    remainder is interpreted as keystrokes rather than pasted text.
    """
    if isinstance(text, bytes):
        return any(sentinel.encode() in text for sentinel in BRACKETED_PASTE_SENTINELS)
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
