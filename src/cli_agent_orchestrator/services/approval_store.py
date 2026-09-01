"""The durable plan-approval record (issue #583 Bolt 2, unit ``approval-store``).

FR-8's re-approval mechanism, and nothing more. One row per approved plan, keyed by the ``plan_id`` that
``plan_identifier.compute`` derives from a run's execution-affecting fields: a changed plan produces a
different ``plan_id``, finds no row, and is refused until approved in its own right.

THIS MODULE DECIDES NOTHING ABOUT EXECUTION. ``components.md`` is explicit that ``PlanApprovalBinding``
"consumes the manifest; decides nothing about execution" — it answers "does an approval exist for this
``plan_id``?" and stops. Whether that permits a run is ``approval-gate``'s call (unit 7). Two places deciding
whether a run may proceed means the second one drifts, and a drifted authorisation check is the kind that looks
correct while permitting something.

THERE IS NO UPDATE, NO DELETE AND NO REPLACING UPSERT, AND THAT ABSENCE IS THE CENTRAL CONTROL. An update path
would let a caller point an existing approval at a changed plan: the row would read as approved, its timestamp
would look recent, and **work nobody reviewed would execute with a genuine approval behind it** — not a bypass
of the gate, but a corruption of the thing the gate consults. ``grant`` therefore uses ``INSERT OR IGNORE``, so
write-once is a property of the statement rather than of a caller remembering to look first.

ABSENCE MEANS UNAPPROVED, AND THAT IS WHY EVERY FAULT HERE IS SAFE. A missing table, a silent migration
failure, a database error — all converge on "no row found". Because absence answers False rather than raising or
defaulting open, each of those becomes a REFUSED RUN rather than an authorisation bypass. Fail-closed, the same
direction pass 2A established for an absent manifest.

``plan_id`` IS OPAQUE: stored and compared verbatim, never parsed, never normalised. A normalisation is how two
distinct plans could come to share one approval, and ``plan-v1:``'s versioned prefix exists precisely so a
future scheme is DISTINGUISHABLE rather than silently comparable.

ONE CONSEQUENCE AN OPERATOR WILL MEET, so it is written down. ``plan_id`` does not exist until
``manifest-freeze`` computes it at run start, so a plan that has never been started cannot have been approved —
**the first run of any new plan is refused by design.** The loop is: run refused, ``plan_id`` reported, approve,
re-run. That friction is temporary rather than intrinsic: Bolt 3's authoring sequence presents the plan and
takes approval BEFORE running. ``approval-gate``'s refusal message MUST carry the ``plan_id``, or the operator
has nothing to act on.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cli_agent_orchestrator.clients.database import _migrate_workflow_plan_approval
from cli_agent_orchestrator.constants import DATABASE_FILE


@dataclass(frozen=True)
class PlanApproval:
    """One ``workflow_plan_approval`` row. Frozen: an approval is a record, not a mutable state."""

    plan_id: str
    approved_at: str
    approved_by: str


def _now() -> str:
    """UTC ISO timestamp, matching ``workflow_run.started_at``'s convention so the two compare alike."""
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    """Open a connection, ensuring the table exists first.

    THE MIGRATOR RUNS ON EVERY CONNECT, AND THAT IS NOT BELT-AND-BRACES. Relying on ``init_db`` alone is
    verifiably insufficient: ``workflow_journal``'s own ``_MIGRATED_PATHS`` comment records that **five test
    modules repoint ``DATABASE_FILE`` to a temporary path mid-process**, which is exactly why the journal does
    not trust the lifespan either. Without this call the table would be absent precisely where this unit's tests
    and ``approval-gate``'s run, and every lookup would answer "not approved" — refusing every run.

    DELIBERATELY NOT MEMOISED, unlike the journal. It needed ``_MIGRATED_PATHS`` because it re-ran two migrators
    plus DDL on EVERY journal call; this issues one idempotent ``CREATE TABLE IF NOT EXISTS`` before an
    operation that happens once per run start. A cache would also require the column verification the journal
    learned it needed — because a migrator that swallows failure "RETURNS NORMALLY when it fails" and so
    establishes nothing — which is a lot of machinery for three columns. Revisit if this table ever lands on a
    hot path.
    """
    _migrate_workflow_plan_approval()
    return sqlite3.connect(str(DATABASE_FILE))


def grant(plan_id: str, approved_by: str) -> None:
    """Record an approval for ``plan_id``. Write-once: a repeated grant is a no-op.

    ``INSERT OR IGNORE`` rather than a plain ``INSERT`` so a second grant for an already-approved plan neither
    errors nor overwrites the original ``approved_at`` / ``approved_by``. Harmless to repeat, impossible to
    rewrite.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO workflow_plan_approval (plan_id, approved_at, approved_by) "
            "VALUES (?, ?, ?)",
            (plan_id, _now(), approved_by),
        )


def is_approved(plan_id: str) -> bool:
    """Does an approval exist for ``plan_id``? TOTAL — never raises, and absence answers ``False``.

    The one question this module answers. It reports a fact; it does not decide whether a run may proceed.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM workflow_plan_approval WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        # A database fault is not permission. Answering False keeps every failure mode fail-closed:
        # the run is refused rather than admitted on the strength of an error.
        return False


def get_approval(plan_id: str) -> Optional[PlanApproval]:
    """The approval record for ``plan_id``, or ``None``. TOTAL — never raises.

    FOR DIAGNOSIS, NOT FOR DECIDING. "When, and by whom" is the first question a refused operator asks;
    ``approval-gate`` should still route on :func:`is_approved`.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT plan_id, approved_at, approved_by FROM workflow_plan_approval WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return PlanApproval(plan_id=row[0], approved_at=row[1], approved_by=row[2])
