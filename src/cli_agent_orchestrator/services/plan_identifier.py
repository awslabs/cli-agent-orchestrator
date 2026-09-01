"""Compute and classify a run's execution ``plan_id`` (issue #583, unit ``plan-identifier``).

One definition of "is this the same execution plan?", and a stored value whose provenance is readable
from the value itself. FR-8's re-approval rule routes on this module's output: a wrong field list
produces either a false match (a changed plan executing under a stale approval) or a permanent false
mismatch (re-approval demanded on every run).

RUN-LEVEL, AND DELIBERATELY NOT THE STEP FINGERPRINT'S FIELD SET. ``step_fingerprint.compute`` hashes
ten PER-STEP components; this module hashes FR-8's nine RUN-LEVEL fields. The two sets overlap and do
not coincide. The decisive reason they cannot be shared: script-tier steps are DISCOVERED BY EXECUTING
THE PYTHON, so no step inventory exists at approval time — a ``plan_id`` covering per-step fields would
need a list that cannot be produced when it is needed. A step's prompt still reaches this value
transitively, because it lives in the workflow source whose hash is component 1.

LEAF MODULE: imports ``hashlib``, ``json``, ``dataclasses`` and ``typing`` ONLY. No I/O, no state, no
configuration, and NO LOGGING OF ANY KIND. Both are security requirements rather than style
preferences, and both are PRESERVATION requirements — the pure-function shape already has them, and a
later "improvement" is what would take them away:

* NOTHING IS PERSISTED. ``compute`` takes fields, returns a digest, stores nothing. Its inputs include
  ``inputs`` — arbitrary user-supplied JSON, which is exactly where a credential lands in practice (a
  token passed as a workflow input, a key interpolated by a script). Storing the inputs would create a
  SECOND durable home for that text with its own redaction obligation and its own eviction gap. A hash
  needs no redaction, which is why this module can touch that text and remain the safest module in the
  Bolt.
* NO FIELD VALUE IS ECHOED into a log line, a message, or an exception. The previous point's whole
  benefit is destroyed by one helpful log line: the obvious diagnostic instinct ("log the fields so we
  can see what changed") moves a credential out of a hash and into a log file, which is typically
  world-readable on the host and often shipped off it. This module therefore has no logger and no
  ``print``, matching ``secret_gate.py``'s, ``step_result.py``'s and ``step_fingerprint.py``'s posture.

``sha256`` IS AN IDENTITY FUNCTION HERE, NOT A SECURITY BOUNDARY. The unit claims collision resistance
for identity and claims nothing about authentication: the value is stored in the same ``manifest_json``
envelope it describes, so anyone able to write that column can simply write whatever value makes an
approval match, without needing a collision. Integrity of the database file is the filesystem's job.

WHAT THIS MODULE DOES NOT DO. It does not assemble the field values (``manifest-freeze``), does not
carry or store the value (``manifest-envelope`` holds it opaquely inside the envelope;
``manifest-column`` owns the column), does not record approvals (``approval-store``), and decides
nothing about whether a run may proceed (``approval-gate``). It reports identity and provenance.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Optional

PLAN_SCHEME_PREFIX = "plan-v1:"
"""The scheme marker carried by every value ``compute`` emits.

NAMED RATHER THAN INLINED because ``compute`` writes it and ``scheme_of`` reads it: two literals is the
drift that would let one side stop recognising the other's output.

``plan-v1:`` RATHER THAN ``v1:``, deliberately. A stored ``plan_id`` and a stored ``call_fingerprint``
are both "short prefix + 64 lowercase hex", and ``step_fingerprint``'s stated reason for putting
provenance IN the value was that a row read by any future consumer is self-describing without a schema
lookup — which only pays off if the two namespaces cannot be confused. ``v1:`` is the single worst
available choice: it is exactly what a LEGACY step fingerprint would have been called had the narrow
three-field scheme carried a prefix, making it the one string most likely to be misread. The version is
kept so a later change to the field set has a way to announce itself.
"""

_NOT_SUPPLIED = "\x01not-supplied"
"""Substituted for a ``None`` scalar so ``None`` stays distinct from ``""`` (BR-2A3-3).

"No profile" and "the empty profile" are different plans. The leading ``\\x01`` is defence in depth, not
the argument: the guarantee is POSITIONAL, because length-prefixed framing makes every component's
extent explicit, so a real field whose value happened to equal this string still occupies its own framed
slot.
"""

# Separates a component's byte length from its bytes. The LENGTH PREFIX is what makes the encoding
# injective, and it is the one place this scheme deliberately DIVERGES from ``v2``.
#
# ``step_fingerprint.compute`` joins its ten components with a single ``\x00`` and records the
# consequence honestly: safe for ordinary field boundaries — ("a","b") cannot collide with ("ab","") —
# but NOT injective when a component itself contains ``\x00``, and closing it "means length-prefixed
# framing, which is a scheme change and therefore a ``v3:`` decision". ``v2`` already had rows on disk,
# so it had to defer.
#
# THIS SCHEME HAS NO ROWS ON DISK AND TAKES THE DECISION NOW. The reachability also differs: a step's
# fields are provider and agent names, while component 2 here is ARBITRARY USER-SUPPLIED JSON, so a
# component containing the separator is something a workflow author can simply write. Inheriting a known
# non-injectivity into a scheme with a free choice would be a defect adopted for cosmetic symmetry — and
# a collision between two plans means one plan executing under the other's approval.
_FRAME_SEP = ":"

# Fixed precision for numeric leaves, so ``600`` and ``600.0`` are ONE identity. An int reaching a
# float-typed field is entirely ordinary in Python, and rendering the value's repr would fork one plan's
# identity into two for no execution difference at all. Inlined rather than placed in ``constants.py``
# for the same reason as the separator: changing it changes every stored value, so it is part of the
# hash contract, not a tunable someone may legitimately revise.
_NUMBER_PRECISION = 6


@dataclass(frozen=True)
class PlanFields:
    """FR-8's nine execution-affecting run-level fields.

    THE FIELD ORDER IS PART OF THE HASH CONTRACT (BR-2A3-1). It is FR-8's own stated order, adopted
    rather than invented because it is the order the requirement is written in and therefore the order a
    reader will check the implementation against.

    REORDERING THEM SILENTLY INVALIDATES EVERY STORED VALUE. A stored ``plan-v1:`` value would stay
    ``plan-v1:`` while meaning something different, so ``scheme_of``'s classification cannot detect the
    break and equality cannot detect it — the two populations become indistinguishable WITHIN one
    scheme. Every existing approval would quietly stop matching and every run would demand re-approval
    for no reason. That is the one failure mode this scheme's versioning has no answer for, which is why
    a test pins the digest of a known field set.
    """

    source_hash: Optional[str]
    inputs: Any
    repo_baseline: Any
    provider: Optional[str]
    model: Optional[str]
    profile: Optional[str]
    permissions: Any
    limits: Any
    retry_policy: Any


def _text(value: Optional[str]) -> str:
    """Normalise an optional scalar, keeping ``None`` distinct from ``""`` (BR-2A3-3)."""
    return _NOT_SUPPLIED if value is None else value


def _number(value: Any) -> Any:
    """Render numeric leaves exactly while keeping integral int/float spellings equivalent (BR-2A3-7).

    Applied through :func:`_canonicalise` to the leaves of the structured fields. ``bool`` is checked
    FIRST because ``bool`` is a subclass of ``int`` in Python, and normalising ``True`` to ``"1"``
    would erase the distinction between a flag and a count.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.{_NUMBER_PRECISION}f}"
    return value


def _canonicalise(node: Any) -> Any:
    """Recursively normalise numeric leaves inside a structured field.

    Dict key ORDER is handled by ``json.dumps(sort_keys=True)`` rather than here; this walk exists only
    to give numbers one spelling at every depth.

    NOTE ON BOOLEANS (BR-2A3-8). No top-level field is a boolean — the nine are one hash, four
    structures and four strings — so this module writes no ``_flag`` helper, unlike
    ``step_fingerprint``. Booleans reaching this walk are INSIDE a structure and are left untouched,
    because ``json.dumps`` already renders them canonically as ``true`` / ``false``. Adding a helper for
    a field kind that does not exist would be dead code asserting a normalisation nothing calls.
    """
    if isinstance(node, dict):
        return {k: _canonicalise(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_canonicalise(v) for v in node]
    return _number(node)


def _structure(value: Any) -> str:
    """Render a structured field canonically: dict keys SORTED, list order PRESERVED (BR-2A3-6).

    The asymmetry is the point. Without ``sort_keys`` the same logical inputs supplied with keys in a
    different order would produce a different ``plan_id`` and force a re-approval for a plan that
    executes identically — a false positive FR-8 never asks for, and one that trains an operator to
    approve without reading. List order is preserved because a reordered permissions list or retry
    sequence may genuinely execute differently, so sorting would erase a real difference.

    ``default=str`` keeps the function TOTAL over values ``json`` cannot natively encode, rather than
    raising and taking a field value into the traceback with it.
    """
    return json.dumps(
        _canonicalise(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _frame(component: str) -> bytes:
    """Emit one component as ``<byte-length><sep><bytes>``, making the encoding injective."""
    encoded = component.encode("utf-8")
    return f"{len(encoded)}{_FRAME_SEP}".encode("utf-8") + encoded


def compute(fields: PlanFields) -> str:
    """Hash the nine components into ``"plan-v1:<64 lowercase hex>"``.

    NINE COMPONENTS IN A FIXED ORDER, AND THE TUPLE IS NEVER SHORTENED (BR-2A3-1/BR-2A3-2). A ``None``
    scalar becomes :data:`_NOT_SUPPLIED` rather than being dropped, so two plans differing only in WHICH
    field is absent cannot collide.

    TOTAL on well-typed input: it raises nothing, which is also why no exception path can carry a field
    value. A type violation surfaces as the ordinary ``TypeError`` rather than being silently normalised
    — hashing it would hide the caller's bug behind a plausible digest.

    THE OUTPUT IS FIXED-SIZE — 72 characters, no user bytes — so it can be neither an injection vector
    nor a growth vector, which is why NFR-1's bounding and redaction are inapplicable here by
    construction rather than deferred.
    """
    components = (
        _text(fields.source_hash),
        _structure(fields.inputs),
        _structure(fields.repo_baseline),
        _text(fields.provider),
        _text(fields.model),
        _text(fields.profile),
        _structure(fields.permissions),
        _structure(fields.limits),
        _structure(fields.retry_policy),
    )
    framed = b"".join(_frame(component) for component in components)
    return PLAN_SCHEME_PREFIX + hashlib.sha256(framed).hexdigest()


def scheme_of(stored: Optional[str]) -> Literal["plan-v1", "unknown", "absent"]:
    """Report what a stored value says about its own provenance. TOTAL — never raises.

    THREE ANSWERS, NOT TWO, and the middle one is why the prefix exists. Equality alone conflates "the
    plan changed" with "this value was computed under different rules and cannot be verified":
    comparing a ``plan-v1:`` value against an unknown-scheme value ALWAYS fails, so a bare comparison
    would report a changed plan for something nobody can assess. Those two situations need different
    remedies — a changed plan needs re-approval, an unverifiable one needs a human ruling — and
    conflating them pushes an operator toward approving something no one understood. ADR-583-5 records
    the same distinction for legacy step fingerprints: unverifiable, not divergent.

    ``unknown`` is UNREACHABLE IN BOLT 2 — there are no legacy ``plan_id`` rows. It is defined anyway so
    that a future field-set change is not left discovering that its only available operation is an
    equality test guaranteed to fail.

    This function reports provenance and decides nothing. What an ``unknown`` value MEANS for a run is
    ``approval-gate``'s call.
    """
    if not stored:
        return "absent"
    if stored.startswith(PLAN_SCHEME_PREFIX):
        return "plan-v1"
    return "unknown"
