"""3-factor composite scoring for memory recall.

Pure functions only. No I/O, no SQL, no clock dependency beyond ``datetime.now``
which is injectable via ``now=`` for deterministic tests. Callers (recall path
in ``MemoryService``) feed in already-normalised BM25 values, ``updated_at``,
and ``access_count``; this module does NOT compute BM25 or read from SQLite.

Composite formula:

    score = 0.50 * bm25_norm + 0.30 * recency + 0.20 * usage

All factors are clamped to ``[0.0, 1.0]`` so the composite is bounded
``[0.0, 1.0]`` for any input, with defensive clamps for negative wrap
and future-dated timestamps.
"""

import math
from datetime import datetime, timezone
from typing import Literal, Optional

# Public weights — frozen for U4. Adjusting requires a new spec unit.
W_BM25 = 0.50
W_RECENCY = 0.30
W_USAGE = 0.20

# Recency half-life: 30 days → recency=0.5.
RECENCY_HALF_LIFE_DAYS = 30.0

# Usage saturates at ~1.0 once access_count reaches USAGE_NORMALISER.
USAGE_NORMALISER = 100

# Whitelist for ``recall(sort_by=...)`` validation. Anything outside this
# set raises ``ValueError`` — no silent fallback.
SortBy = Literal["score", "recency", "usage"]
SORT_BY_VALUES: frozenset = frozenset({"score", "recency", "usage"})

# Scope precedence. Lower index = higher precedence in the
# final scope-stable sort that runs AFTER score sorting in ``recall()``.
SCOPE_PRECEDENCE: dict = {
    "session": 0,
    "project": 1,
    "global": 2,
    "agent": 3,
    "federated": 4,
}

# Cross-scope write authorisation table. Caller may write a target
# scope iff ``caller == target`` OR ``SCOPE_RANK[caller] > SCOPE_RANK[target]``
# (strict). ``agent`` and ``project`` share rank 1 — siblings; cross-sibling
# writes are rejected.
#
# ``federated`` is intentionally asymmetric: it has the LOWEST recall
# precedence (4 in SCOPE_PRECEDENCE — last on recall) yet write-rank 0,
# so it is writable by every caller except ``session`` (rank 0 can only
# write its own scope). This mirrors how ``session`` is write-rank 0 but
# has the HIGHEST recall precedence (0) — the two tables are independent.
SCOPE_RANK: dict = {
    "session": 0,
    "project": 1,
    "agent": 1,
    "global": 2,
    "federated": 0,
}


def score_memory(
    bm25_norm: float,
    updated_at: datetime,
    access_count: int,
    *,
    now: Optional[datetime] = None,
) -> float:
    """Composite 3-factor score in ``[0.0, 1.0]``.

    Defensive against:
        - negative ``access_count`` (manual DB edit, signed wrap-around) → T5
        - future-dated ``updated_at`` (clock skew, NTP drift) → T7
        - naive datetimes → treated as UTC (matches store path)
        - out-of-range ``bm25_norm`` → clamped to ``[0.0, 1.0]``

    Never raises for finite inputs. Returns ``0.0`` on degenerate timestamps
    rather than ``NaN``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    days_old = max(0.0, (now - updated_at).total_seconds() / 86400.0)
    recency = 1.0 / (1.0 + days_old / RECENCY_HALF_LIFE_DAYS)

    n = max(0, int(access_count))
    usage = min(1.0, math.log1p(n) / math.log1p(USAGE_NORMALISER))

    bm25 = max(0.0, min(1.0, float(bm25_norm)))

    return W_BM25 * bm25 + W_RECENCY * recency + W_USAGE * usage


def normalise_bm25_scores(raw_scores: dict) -> dict:
    """Per-batch max-normalise raw BM25 scores into ``[0.0, 1.0]``.

    ``raw_scores`` maps ``key -> raw_bm25_score``. Returns a NEW dict with
    values divided by the batch max. If the max is ``<= 0`` (degenerate
    corpus, all-zero batch), every value collapses to ``0.0``.

    This dict is purely local to ``recall()`` — it must not
    be stored on ``self`` or any class attribute (side-channel for raw BM25
    scores into tool results).
    """
    if not raw_scores:
        return {}
    mx = max(raw_scores.values())
    if mx <= 0:
        return {k: 0.0 for k in raw_scores}
    return {k: float(v) / float(mx) for k, v in raw_scores.items()}


def validate_sort_by(sort_by: str) -> str:
    """Whitelist sort-mode. Raises ``ValueError`` on unknown value."""
    if sort_by not in SORT_BY_VALUES:
        raise ValueError(f"sort_by must be one of {sorted(SORT_BY_VALUES)}; got {sort_by!r}")
    return sort_by


def scope_write_allowed(caller: str, target: str) -> bool:
    """Store-time scope guard.

    Returns True iff a caller running at ``caller`` scope is permitted to
    write a memory at ``target`` scope. The rule is: ``caller == target`` OR
    ``SCOPE_RANK[caller] > SCOPE_RANK[target]`` (strict). ``agent`` and
    ``project`` are siblings at rank 1; cross-sibling writes are rejected.
    Strictness matters for ``federated`` (rank 0): ``session`` (also rank 0)
    cannot write it because ``0 > 0`` is False, while any higher-rank caller
    can — i.e. writable by every caller except session.

    Unknown scopes (not in ``SCOPE_RANK``) deny by default — fail closed.
    """
    if caller not in SCOPE_RANK or target not in SCOPE_RANK:
        return False
    if caller == target:
        return True
    # Strictly broader rank required when names differ — this rejects
    # cross-sibling writes between ``project`` and ``agent`` (both rank 1)
    # by design: "Cross-sibling writes (project caller → agent
    # scope) are rejected."
    return bool(SCOPE_RANK[caller] > SCOPE_RANK[target])
