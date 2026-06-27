"""Wiki Healer (Phase 4 U1).

Self-healing for the memory wiki. Consumes the frozen ``LintIssue`` records
produced by :mod:`cli_agent_orchestrator.services.wiki_lint` and, under an
explicit ``--apply`` gate, repairs three classes of finding:

- ``orphan_page``    — delete the wiki file + drop its index.md line + SQLite row.
- ``contradiction``  — keep the newer article (by ``updated_at``), ``forget`` the loser.
- ``stale_claim``    — strip the paragraph naming the stale path/symbol, atomic rewrite.
  Only the ``file not found:`` and ``symbol not found in source:`` sub-types are
  healable; the ``related_keys references missing key:`` sub-type (emitted by the
  graph detector under the ``stale_claim`` type) is reported ``skipped`` — there is
  no prose paragraph to strip.

``poison_frequency`` is gated behind BOTH ``--apply`` AND ``--aggressive`` (a
dual gate); ``graph_density`` is flag-only and never mutates.

Design invariants (see issue #297):

1. Dry-run default. ``apply=False`` mutates nothing.
2. SQL row authoritative on mutation — contradiction/poison re-read the DB row
   by ``(key, scope, scope_id)`` and trust the DB, never the LintIssue payload.
3. Atomic per-issue-type batch — each group's DB writes run in one transaction.
4. Concurrency guard — a dedicated ``.heal.lock`` (fcntl.flock, LOCK_NB).
5. Bounded blast radius — ``MAX_HEAL_ACTIONS`` run-level cap + per-type caps.
6. Recovery field — ``stale_claim`` stashes the pre-strip paragraph (size-capped).
7. Full audit trail — every applied mutation emits an AWAITED audit event.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.services.audit_log import write_audit
from cli_agent_orchestrator.services.memory_service import (
    MemoryDisabledError,
    MemoryService,
    _is_memory_enabled,
)
from cli_agent_orchestrator.services.wiki_lint import ISSUE_CAPS, LintIssue

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants & caps
# -----------------------------------------------------------------------------

MAX_HEAL_ACTIONS = 200  # hard run-level cap across all issue types
STALE_CLAIM_PRESTRIP_PARAGRAPH_MAX_BYTES = 2048  # audit recovery field size cap

# Issue types this healer mutates / reports, in deterministic batch order.
_MUTATING_TYPES = ("orphan_page", "contradiction", "stale_claim", "poison_frequency")

# Map a mutating issue_type → the action issue_type recorded on success.
_ACTION_TYPE = {
    "orphan_page": "orphan_pruned",
    "contradiction": "contradiction_resolved",
    "stale_claim": "stale_claim_pruned",
    "poison_frequency": "poison_access_zeroed",
}

# Description parse patterns for stale_claim.
_FILE_NOT_FOUND_RE = re.compile(r"file not found: (\S+)")
_SYMBOL_NOT_FOUND_RE = re.compile(r"symbol not found in source: (\S+)")


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------


class HealError(RuntimeError):
    """Base error for the healer."""


class HealConflictError(HealError):
    """Raised when the ``.heal.lock`` cannot be acquired (another heal runs)."""


# -----------------------------------------------------------------------------
# Frozen dataclasses
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HealAction:
    """Single applied (or planned) mutation outcome."""

    issue_type: str  # orphan_pruned|contradiction_resolved|stale_claim_pruned|poison_access_zeroed
    key: str
    related_key: Optional[str] = None
    description: str = ""  # human summary of what was done / would be done
    status: str = "applied"  # "applied"|"skipped"|"error"|"planned"
    pre_strip_paragraph: Optional[str] = None  # only for stale_claim; size-capped
    # True iff this action already performed an irreversible filesystem mutation
    # (file unlink / index rewrite / atomic article rewrite) BEFORE the batch
    # db.commit(). If that commit then fails and the DB rolls back, the on-disk
    # change persists — the batch handler emits a ``heal_partial_mutation`` audit
    # so a real file change is never lost from the forensic trail (invariant #7).
    # DB-only healers (poison) leave this False: a rollback fully restores them.
    fs_mutated: bool = False
    # Buffered audit payload (event_type, summary, fields). Populated by the
    # healers instead of emitting inline; the batch loop flushes it only AFTER
    # the group's db.commit() succeeds, so a rolled-back mutation never leaves a
    # false audit record (invariant #7, fixed). None when there is nothing to
    # audit (e.g. a skipped no-op that produced no side effect).
    audit: Optional[tuple] = None  # (event_type: str, summary: str, fields: dict)


@dataclass(frozen=True)
class HealReport:
    """Aggregate heal run summary."""

    scope: str
    scope_id: Optional[str]
    apply: bool
    aggressive: bool
    actor: str = "cli"  # who triggered the run — forensic anchor on heal_run_completed
    actions: list = field(default_factory=list)  # list[HealAction]
    dry_run_summary: Optional[str] = None  # non-None only when apply=False
    truncated_by_type: dict = field(default_factory=dict)  # {"stale_claim": 45}
    truncated_run_level: int = 0  # total suppressed due to MAX_HEAL_ACTIONS
    total_suppressed: int = 0  # truncated_by_type.sum() + truncated_run_level


# -----------------------------------------------------------------------------
# Lock handling
# -----------------------------------------------------------------------------


def _heal_lock_path(svc: MemoryService, scope: str, scope_id: Optional[str]) -> Path:
    return svc._get_project_dir(scope, scope_id) / "wiki" / ".heal.lock"


def _acquire_lock(lock_path: Path) -> int:
    """Acquire the non-blocking heal lock; raise HealConflictError on contention."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as e:
        raise HealError(f"lock open failed: {type(e).__name__}") from e
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise HealConflictError(
                "another heal is running; lock acquire failed (EWOULDBLOCK)"
            ) from e
        raise HealError(f"lock acquire failed: {type(e).__name__}") from e
    return lock_fd


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# Stale-claim paragraph strip
# -----------------------------------------------------------------------------


def _parse_stale_identifier(description: str) -> Optional[str]:
    """Extract the stale path/symbol from a stale_claim description, or None."""
    m = _FILE_NOT_FOUND_RE.search(description or "")
    if m:
        return m.group(1).strip()
    m = _SYMBOL_NOT_FOUND_RE.search(description or "")
    if m:
        return m.group(1).strip()
    return None


def _cap_pre_strip(paragraph: str) -> str:
    """Size-cap the recovery paragraph to STALE_CLAIM_PRESTRIP_PARAGRAPH_MAX_BYTES."""
    enc = paragraph.encode("utf-8")
    if len(enc) <= STALE_CLAIM_PRESTRIP_PARAGRAPH_MAX_BYTES:
        return paragraph
    return (
        enc[:STALE_CLAIM_PRESTRIP_PARAGRAPH_MAX_BYTES].decode("utf-8", errors="ignore")
        + "[…truncated]"
    )


def _strip_stale_paragraph(content: str, stale_id: str) -> tuple[str, Optional[str]]:
    """Strip the first paragraph that names ``stale_id``.

    Returns ``(new_content, pre_strip_paragraph)``. ``pre_strip_paragraph`` is
    None when no paragraph matched (content unchanged). A paragraph is a
    maximal run of non-blank lines bounded by blank lines / EOF.
    """
    lines = content.split("\n")

    # Group lines into paragraphs: list of (lines, start_idx).
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                paragraphs.append(current)
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(current)

    # Path-aware boundaries (P2a). A plain ``\b...\b`` cannot anchor an
    # identifier that starts or ends with a non-word char, so detector-emitted
    # paths like ``./src/gone.py`` or ``/tmp/repo/src/gone.py`` never matched.
    # Use lookarounds over the path-token alphabet instead:
    #   leading  (?<![\w./-]) — not glued to a preceding path char (so
    #            ``src/gone.py`` won't match inside ``mysrc/gone.py``);
    #   trailing (?![\w/-])   — not glued to a following word/``/``/``-`` (so
    #            ``gone.py`` won't match inside ``gone.python``), while a
    #            trailing ``.`` is allowed so sentence-final ``…gone.py.`` matches.
    pattern = re.compile(r"(?<![\w./-])" + re.escape(stale_id) + r"(?![\w/-])")
    target_idx: Optional[int] = None
    for i, para in enumerate(paragraphs):
        if any(pattern.search(ln) for ln in para):
            target_idx = i
            break

    if target_idx is None:
        return content, None

    pre_strip = "\n".join(paragraphs[target_idx])
    remaining = [p for i, p in enumerate(paragraphs) if i != target_idx]
    if remaining:
        rebuilt = "\n\n".join("\n".join(p) for p in remaining).strip() + "\n"
    else:
        rebuilt = ""
    return rebuilt, pre_strip


# -----------------------------------------------------------------------------
# Per-issue healers (mutating; called only when apply gate is satisfied)
# -----------------------------------------------------------------------------


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_exists(db: Any, key: str, scope: str, scope_id: Optional[str]) -> bool:
    """True iff a metadata row exists for (key, scope, scope_id) on ``db``."""
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    return (
        db.query(MemoryMetadataModel)
        .filter(
            MemoryMetadataModel.key == key,
            MemoryMetadataModel.scope == scope,
            (
                MemoryMetadataModel.scope_id == scope_id
                if scope_id is not None
                else MemoryMetadataModel.scope_id.is_(None)
            ),
        )
        .first()
    ) is not None


# Mirror the index-entry shape parsed by wiki_lint._detect_orphan_pages.
_INDEX_KEY_RE = re.compile(r"\[([a-z0-9-]{1,60})\]")


def _index_has_key(svc: MemoryService, scope: str, scope_id: Optional[str], key: str) -> bool:
    """True iff ``key`` appears as an index entry for this (scope, scope_id).

    Best-effort read of the container's ``index.md``. On any read error we
    return ``True`` (treat as present) so a transient failure never green-lights
    a deletion — the orphan guard must fail safe.
    """
    try:
        index_path = svc.get_index_path(scope, scope_id)
        if not index_path.exists():
            return False
        for line in index_path.read_text(encoding="utf-8").splitlines():
            m = _INDEX_KEY_RE.search(line)
            if m and m.group(1) == key:
                return True
        return False
    except OSError:
        return True


def _delete_row(db: Any, key: str, scope: str, scope_id: Optional[str]) -> int:
    """Delete the metadata row for (key, scope, scope_id) on the SHARED session.

    Does NOT commit — the caller's per-issue-type batch transaction owns the
    commit/rollback boundary (design invariant #3).
    """
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    deleted: int = (
        db.query(MemoryMetadataModel)
        .filter(
            MemoryMetadataModel.key == key,
            MemoryMetadataModel.scope == scope,
            (
                MemoryMetadataModel.scope_id == scope_id
                if scope_id is not None
                else MemoryMetadataModel.scope_id.is_(None)
            ),
        )
        .delete()
    )
    return deleted


async def _heal_orphan(
    svc: MemoryService, issue: LintIssue, scope: str, scope_id: Optional[str], db: Any
) -> HealAction:
    """Delete wiki file + index line + SQLite row for one orphan_page issue.

    The SQLite row delete runs on the SHARED ``db`` session (no per-issue
    commit); the batch transaction is committed once by the caller.
    """
    try:
        key = svc._sanitize_key(issue.key)
    except ValueError:
        return HealAction(
            "orphan_pruned", issue.key, status="skipped", description="key failed sanitisation"
        )
    try:
        wiki_path = svc.get_wiki_path(scope, scope_id, key)
    except ValueError:
        return HealAction(
            "orphan_pruned", key, status="skipped", description="path traversal guard rejected key"
        )

    # Defensive orphan re-check (cross-project safety). The LintIssue carries no
    # scope_id, and run_lint(scope="project") returns orphans across ALL project
    # containers; the healer reconstructs wiki_path using the CURRENT (scope,
    # scope_id). A key collision (project B has an orphan key that is a LIVE
    # memory in project A) would otherwise delete a valid file. An orphan is, by
    # definition, absent from BOTH the index and SQLite for its own container —
    # so if either is present for THIS (scope, scope_id) the file is not
    # orphaned here and we refuse to unlink it.
    if _row_exists(db, key, scope, scope_id) or _index_has_key(svc, scope, scope_id, key):
        return HealAction(
            "orphan_pruned",
            key,
            status="skipped",
            description="not orphaned in this scope (SQLite row or index entry present); refusing delete",
        )

    if not wiki_path.exists():
        # Already gone — still drop any stale index/metadata. Track whether the
        # best-effort index removal succeeded so the description does not claim
        # cleanup that did not happen.
        index_removed = True
        try:
            svc._update_index(scope, scope_id, key, "", "", "", _now_ts(), "remove")
        except Exception as e:
            index_removed = False
            logger.warning("orphan_pruned index remove failed key=%s: %s", key, type(e).__name__)
        _delete_row(db, key, scope, scope_id)
        desc = (
            "file already absent; index/metadata cleaned"
            if index_removed
            else "file already absent; metadata row removed (index removal failed, see logs)"
        )
        return HealAction(
            "orphan_pruned",
            key,
            status="applied",
            description=desc,
            # The index rewrite (when it succeeded) is an FS mutation that a DB
            # rollback cannot undo; flag it so a failed commit still audits.
            fs_mutated=index_removed,
            audit=(
                "orphan_pruned",
                f"deleted orphan wiki file: {key}",
                {
                    "key": key,
                    "scope": scope,
                    "scope_id": scope_id or "",
                    "file_path": str(wiki_path),
                },
            ),
        )

    try:
        wiki_path.unlink()
        svc._update_index(scope, scope_id, key, "", "", "", _now_ts(), "remove")
        _delete_row(db, key, scope, scope_id)
    except Exception as e:
        logger.warning("orphan_pruned failed key=%s: %s", key, type(e).__name__)
        return HealAction(
            "orphan_pruned", key, status="error", description=f"delete failed: {type(e).__name__}"
        )

    return HealAction(
        "orphan_pruned",
        key,
        status="applied",
        description=f"deleted {wiki_path.name}, index line, metadata row",
        fs_mutated=True,
        audit=(
            "orphan_pruned",
            f"deleted orphan wiki file: {key}",
            {
                "key": key,
                "scope": scope,
                "scope_id": scope_id or "",
                "file_path": str(wiki_path),
            },
        ),
    )


async def _heal_contradiction(
    svc: MemoryService, issue: LintIssue, scope: str, scope_id: Optional[str], db: Any
) -> HealAction:
    """Keep newer article (by updated_at), forget the loser. SQL-row authoritative."""
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    key_a = issue.key
    key_b = issue.related_key
    if not key_b:
        return HealAction(
            "contradiction_resolved",
            key_a,
            status="skipped",
            description="contradiction missing related_key",
        )

    # SQL-row authoritative: re-read both rows on the SHARED batch session.
    def _read(k: str) -> Optional[Any]:
        return (
            db.query(MemoryMetadataModel)
            .filter(
                MemoryMetadataModel.key == k,
                MemoryMetadataModel.scope == scope,
                (
                    MemoryMetadataModel.scope_id == scope_id
                    if scope_id is not None
                    else MemoryMetadataModel.scope_id.is_(None)
                ),
            )
            .first()
        )

    row_a = _read(key_a)
    row_b = _read(key_b)
    if row_a is None or row_b is None:
        return HealAction(
            "contradiction_resolved",
            key_a,
            related_key=key_b,
            status="skipped",
            description="one or both rows missing; nothing to resolve",
        )

    ua = row_a.updated_at
    ub = row_b.updated_at
    if ua.tzinfo is None:
        ua = ua.replace(tzinfo=timezone.utc)
    if ub.tzinfo is None:
        ub = ub.replace(tzinfo=timezone.utc)

    # Keep the newer article. On a same-second tie (DB updated_at is
    # second-resolution, so ties are plausible) fall back to a DETERMINISTIC
    # tiebreak — keep the lexicographically-smaller key — so the choice of which
    # memory survives is reproducible and never depends on argument order.
    if ua > ub or (ua == ub and key_a <= key_b):
        winner_key, loser_key = key_a, key_b
        winner_ts, loser_ts = ua, ub
    else:
        winner_key, loser_key = key_b, key_a
        winner_ts, loser_ts = ub, ua

    # Forget the loser. The DB-row delete runs on the SHARED session (deferred
    # commit / batch transaction, invariant #3); the non-DB side effects (wiki
    # file + index line) are applied immediately. The memory-disabled kill
    # switch is enforced at heal() entry; re-check defensively here so a
    # disabled state never produces a silent mutation.
    if not _is_memory_enabled():
        raise MemoryDisabledError(
            "memory is disabled (memory.enabled=false); refusing to heal wiki"
        )
    try:
        try:
            loser_key_s = svc._sanitize_key(loser_key)
            loser_path = svc.get_wiki_path(scope, scope_id, loser_key_s)
        except ValueError:
            loser_key_s, loser_path = loser_key, None
        if loser_path is not None and loser_path.exists():
            loser_path.unlink()
        svc._update_index(scope, scope_id, loser_key_s, "", "", "", _now_ts(), "remove")
        _delete_row(db, loser_key_s, scope, scope_id)
    except Exception as e:
        logger.warning(
            "contradiction_resolved forget failed key=%s: %s", loser_key, type(e).__name__
        )
        return HealAction(
            "contradiction_resolved",
            winner_key,
            related_key=loser_key,
            status="error",
            description=f"forget failed: {type(e).__name__}",
        )

    return HealAction(
        "contradiction_resolved",
        winner_key,
        related_key=loser_key,
        status="applied",
        description=f"kept {winner_key}, forgot {loser_key}",
        fs_mutated=True,
        audit=(
            "contradiction_resolved",
            f"kept {winner_key}, forgot {loser_key}",
            {
                "winner_key": winner_key,
                "loser_key": loser_key,
                "scope": scope,
                "scope_id": scope_id or "",
                "winner_updated_at": winner_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "loser_updated_at": loser_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        ),
    )


async def _heal_stale_claim(
    svc: MemoryService, issue: LintIssue, scope: str, scope_id: Optional[str], db: Any
) -> HealAction:
    """Strip the paragraph naming the stale path/symbol; atomic rewrite.

    The primary mutation is the atomic file rewrite. The metadata ``updated_at``
    stamp is a best-effort, non-critical follow-up via ``_upsert_metadata`` (its
    own short transaction) and is intentionally NOT part of the batch
    transaction — a stamp failure must never roll back a successful rewrite.
    """
    stale_id = _parse_stale_identifier(issue.description)
    if stale_id is None:
        return HealAction(
            "stale_claim_pruned",
            issue.key,
            status="skipped",
            description="description format unrecognised; no strip",
        )

    try:
        key = svc._sanitize_key(issue.key)
        wiki_path = svc.get_wiki_path(scope, scope_id, key)
    except ValueError:
        return HealAction(
            "stale_claim_pruned",
            issue.key,
            status="skipped",
            description="key failed sanitisation / traversal guard",
        )

    if not wiki_path.exists():
        return HealAction(
            "stale_claim_pruned", key, status="skipped", description="article vanished before strip"
        )

    try:
        content = wiki_path.read_text(encoding="utf-8")
    except OSError as e:
        return HealAction(
            "stale_claim_pruned",
            key,
            status="error",
            description=f"read failed: {type(e).__name__}",
        )

    new_content, pre_strip = _strip_stale_paragraph(content, stale_id)
    capped_pre_strip = _cap_pre_strip(pre_strip) if pre_strip is not None else None

    if pre_strip is None:
        # No paragraph matched — content unchanged, nothing mutated (P3). Emit NO
        # audit: ``stale_claim_pruned`` is a mutation event, and recording it for
        # a no-op contradicts the "every applied mutation emits" invariant and
        # pollutes forensic review with phantom prunes. The skipped status is
        # surfaced in the report; the audit stream stays mutation-only.
        return HealAction(
            "stale_claim_pruned",
            key,
            status="skipped",
            description=f"stale id {stale_id} not found in article",
            pre_strip_paragraph=None,
            audit=None,
        )

    tmp_path = wiki_path.parent / f".{wiki_path.stem}.strip.tmp"
    try:
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(str(tmp_path), str(wiki_path))
    except OSError as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return HealAction(
            "stale_claim_pruned",
            key,
            status="error",
            description=f"atomic rewrite failed: {type(e).__name__}",
        )

    # Stamp the metadata updated_at so the article is seen as freshly changed.
    try:
        svc._upsert_metadata(
            key=key,
            memory_type=svc._memory_type_from_file(new_content),
            scope=scope,
            scope_id=scope_id,
            file_path=str(wiki_path),
            tags=svc._tags_from_file(new_content),
            source_provider=None,
            source_terminal_id=None,
            token_estimate=len(new_content) // 4,
            preserve_provenance=True,
        )
    except Exception as e:
        logger.debug("stale_claim metadata stamp failed key=%s: %s", key, type(e).__name__)

    return HealAction(
        "stale_claim_pruned",
        key,
        status="applied",
        description=f"stripped paragraph naming {stale_id}",
        pre_strip_paragraph=capped_pre_strip,
        fs_mutated=True,
        audit=(
            "stale_claim_pruned",
            f"stripped stale reference to {stale_id} in article {key}",
            {
                "key": key,
                "scope": scope,
                "scope_id": scope_id or "",
                "stale_identifier": stale_id,
                # Already byte-capped by _cap_pre_strip(); audit_log re-caps defensively.
                "pre_strip_paragraph": capped_pre_strip or "",
            },
        ),
    )


async def _heal_poison(
    svc: MemoryService, issue: LintIssue, scope: str, scope_id: Optional[str], db: Any
) -> HealAction:
    """Zero access_count + last_accessed_at for one poison_frequency row.

    SQL-row authoritative: re-read and mutate the row on the SHARED batch
    session (no per-issue commit; the caller's batch transaction owns the
    commit/rollback boundary).
    """
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    key = issue.key
    try:
        row = (
            db.query(MemoryMetadataModel)
            .filter(
                MemoryMetadataModel.key == key,
                MemoryMetadataModel.scope == scope,
                (
                    MemoryMetadataModel.scope_id == scope_id
                    if scope_id is not None
                    else MemoryMetadataModel.scope_id.is_(None)
                ),
            )
            .first()
        )
        if row is None:
            return HealAction(
                "poison_access_zeroed",
                key,
                status="skipped",
                description="row missing; nothing to zero",
            )
        old_count = int(row.access_count or 0)
        row.access_count = 0
        row.last_accessed_at = None
    except Exception as e:
        logger.warning("poison_access_zeroed failed key=%s: %s", key, type(e).__name__)
        return HealAction(
            "poison_access_zeroed",
            key,
            status="error",
            description=f"zero failed: {type(e).__name__}",
        )

    return HealAction(
        "poison_access_zeroed",
        key,
        status="applied",
        description=f"reset access_count {old_count} → 0",
        audit=(
            "poison_access_zeroed",
            f"reset access_count from {old_count} to 0",
            {
                "key": key,
                "scope": scope,
                "scope_id": scope_id or "",
                "access_count_was": str(old_count),
            },
        ),
    )


_HEALERS = {
    "orphan_page": _heal_orphan,
    "contradiction": _heal_contradiction,
    "stale_claim": _heal_stale_claim,
    "poison_frequency": _heal_poison,
}


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------


def _plan_description(issue: LintIssue) -> str:
    """Human-readable 'Would ...' line for a dry-run action."""
    t = issue.issue_type
    if t == "orphan_page":
        return f"Would delete orphan wiki file: {issue.key}"
    if t == "contradiction":
        return f"Would resolve contradiction {issue.key} vs {issue.related_key} (keep newer)"
    if t == "stale_claim":
        sid = _parse_stale_identifier(issue.description)
        return f"Would strip paragraph naming {sid or '<unparseable>'} in {issue.key}"
    if t == "poison_frequency":
        return f"Would zero access_count of {issue.key}"
    return f"Would heal {t}: {issue.key}"


async def heal(
    issues: list,
    *,
    scope: str,
    scope_id: Optional[str],
    apply: bool = False,
    aggressive: bool = False,
    actor: str = "cli",
    svc: Optional[MemoryService] = None,
) -> HealReport:
    """Heal a wiki from lint findings.

    Dry-run by default (``apply=False`` mutates nothing). ``poison_frequency``
    is gated behind BOTH ``apply`` AND ``aggressive``. ``graph_density`` and
    ``lint_error`` bookkeeping rows are never healed.
    """
    # Memory-disabled kill switch: refuse to mutate (or even plan) when memory
    # is turned off. Raised early so the critical security signal propagates to
    # the caller instead of being swallowed by a per-issue try/except.
    if not _is_memory_enabled():
        raise MemoryDisabledError(
            "memory is disabled (memory.enabled=false); refusing to heal wiki"
        )

    if svc is None:
        svc = MemoryService()

    # 1. Filter: drop lint_error bookkeeping; drop graph_density (flag-only);
    #    drop poison_frequency unless dual gate satisfied.
    poison_allowed = apply and aggressive
    by_type: dict = {t: [] for t in _MUTATING_TYPES}
    for issue in issues:
        if not isinstance(issue, LintIssue):
            continue
        t = issue.issue_type
        if t == "lint_error" or t == "graph_density":
            continue
        if t == "poison_frequency" and not poison_allowed:
            continue
        if t not in by_type:
            continue
        # Cross-container guard (P1): run_lint() returns findings across ALL
        # containers for a scope, but the healer mutates using THIS run's
        # (scope, scope_id). A finding whose source container differs from the
        # target must never be applied — it would corrupt a same-key memory in a
        # different project.
        src_scope_id = getattr(issue, "scope_id", None)
        if src_scope_id != scope_id:
            # Allow scope_id-less stale_claim findings only when they are unhealable
            # (e.g. "related_keys references missing key") so they can be surfaced as skipped.
            if not (
                t == "stale_claim"
                and src_scope_id is None
                and _parse_stale_identifier(issue.description) is None
            ):
                continue
        by_type[t].append(issue)

    actions: list = []
    truncated_by_type: dict = {}
    truncated_run_level = 0
    actions_applied = 0

    # Dry-run: no lock, no mutations. Build the plan (respecting caps so the
    # plan matches what an apply would do).
    if not apply:
        for t in _MUTATING_TYPES:
            cap = ISSUE_CAPS.get(t)  # contradiction has no per-type cap
            for issue in by_type[t]:
                if actions_applied >= MAX_HEAL_ACTIONS:
                    truncated_run_level += 1
                    continue
                if cap is not None and (
                    sum(
                        1
                        for a in actions
                        if a.status != "skipped" and _action_src_type(a.issue_type) == t
                    )
                    >= cap
                ):
                    truncated_by_type[t] = truncated_by_type.get(t, 0) + 1
                    continue
                # A stale_claim whose description the healer cannot parse
                # (e.g. the "related_keys references missing key:" sub-type
                # emitted by the graph detector) is a no-op at apply time. Mark
                # it skipped in the plan too, so the dry-run never advertises a
                # strip that apply would silently decline.
                if t == "stale_claim" and _parse_stale_identifier(issue.description) is None:
                    actions.append(
                        HealAction(
                            _ACTION_TYPE[t],
                            issue.key,
                            related_key=issue.related_key,
                            description="description format unrecognised; would skip",
                            status="skipped",
                        )
                    )
                    continue
                actions.append(
                    HealAction(
                        _ACTION_TYPE[t],
                        issue.key,
                        related_key=issue.related_key,
                        description=_plan_description(issue),
                        status="planned",
                    )
                )
                actions_applied += 1
        total_suppressed = sum(truncated_by_type.values()) + truncated_run_level
        n_applicable = sum(len(v) for v in by_type.values())
        n_planned = sum(1 for a in actions if a.status == "planned")
        summary = (
            f"Would apply {n_planned} of {n_applicable} actionable findings"
            f" ({total_suppressed} suppressed by caps)."
        )
        if not poison_allowed and any(
            i.issue_type == "poison_frequency" for i in issues if isinstance(i, LintIssue)
        ):
            summary += " Poison issues gated behind --apply --aggressive."
        report = HealReport(
            scope=scope,
            scope_id=scope_id,
            apply=False,
            aggressive=aggressive,
            actor=actor,
            actions=actions,
            dry_run_summary=summary,
            truncated_by_type=truncated_by_type,
            truncated_run_level=truncated_run_level,
            total_suppressed=total_suppressed,
        )
        await _emit_run_completed(report)
        return report

    # 2. Apply path: acquire the non-blocking heal lock for the whole run.
    #    Each issue-type group's DB writes run in ONE transaction on a shared
    #    session (invariant #3): a commit failure rolls THAT group back and
    #    marks its actions error; other groups proceed independently.
    lock_fd = _acquire_lock(_heal_lock_path(svc, scope, scope_id))
    try:
        for t in _MUTATING_TYPES:
            cap = ISSUE_CAPS.get(t)
            healer = _HEALERS[t]
            n_this_type = 0
            group_start = len(actions)
            db = svc._get_db_session()
            try:
                for issue in by_type[t]:
                    if actions_applied >= MAX_HEAL_ACTIONS:
                        truncated_run_level += 1
                        continue
                    if cap is not None and n_this_type >= cap:
                        truncated_by_type[t] = truncated_by_type.get(t, 0) + 1
                        continue
                    action = await healer(svc, issue, scope, scope_id, db)
                    actions.append(action)
                    # Only actionable work consumes the blast-radius budget; a
                    # skipped no-op (unparseable description, missing row, …)
                    # mutated nothing and must not crowd out a real fix.
                    if action.status != "skipped":
                        n_this_type += 1
                        actions_applied += 1
                db.commit()
            except Exception as e:
                # Roll the whole group back at the DB level. A successful action
                # in this group may already have performed an irreversible
                # filesystem mutation (file unlink / index rewrite / atomic
                # rewrite) that the rollback cannot undo. We do NOT emit the
                # normal mutation audit (the DB state it described was rolled
                # back), but for each such action we DO emit a dedicated
                # ``heal_partial_mutation`` event so a real on-disk change is
                # never lost from the forensic trail (P2b / invariant #7).
                logger.warning("heal batch group commit failed type=%s: %s", t, type(e).__name__)
                try:
                    db.rollback()
                except Exception:
                    pass
                rolled = actions[group_start:]
                del actions[group_start:]
                for a in rolled:
                    if a.status == "applied" and a.fs_mutated:
                        ev_key = a.audit[2].get("key", a.key) if a.audit else a.key
                        await write_audit(
                            "heal_partial_mutation",
                            (
                                f"commit failed ({type(e).__name__}) after filesystem "
                                f"mutation for {a.issue_type}:{a.key}; DB rolled back, "
                                f"on-disk change persists"
                            ),
                            issue_type=a.issue_type,
                            key=ev_key,
                            scope=scope,
                            scope_id=scope_id or "",
                            db_rolled_back="true",
                            fs_partial="true",
                            error=type(e).__name__,
                        )
                    actions.append(
                        HealAction(
                            a.issue_type,
                            a.key,
                            related_key=a.related_key,
                            description=(
                                f"commit failed ({type(e).__name__}); DB rolled back; "
                                f"filesystem changes may be partial: {a.description}"
                            ),
                            status="error",
                            pre_strip_paragraph=a.pre_strip_paragraph,
                            fs_mutated=a.fs_mutated,
                        )
                    )
                continue
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            # Commit succeeded — NOW flush the group's buffered audit events.
            # Emitting only post-commit guarantees the audit log never records a
            # mutation that was rolled back. Gate on status=="applied" (P3): a
            # buffered audit on a skipped/errored action describes a no-op, and a
            # mutation event type must never narrate a non-mutation.
            for a in actions[group_start:]:
                if a.status == "applied" and a.audit is not None:
                    event_type, summary, fields = a.audit
                    await write_audit(event_type, summary, **fields)
    finally:
        _release_lock(lock_fd)

    total_suppressed = sum(truncated_by_type.values()) + truncated_run_level
    report = HealReport(
        scope=scope,
        scope_id=scope_id,
        apply=True,
        aggressive=aggressive,
        actor=actor,
        actions=actions,
        dry_run_summary=None,
        truncated_by_type=truncated_by_type,
        truncated_run_level=truncated_run_level,
        total_suppressed=total_suppressed,
    )
    await _emit_run_completed(report)
    return report


def _action_src_type(action_type: str) -> str:
    """Reverse-map an action issue_type back to its lint source type."""
    for src, act in _ACTION_TYPE.items():
        if act == action_type:
            return src
    return action_type


async def _emit_run_completed(report: HealReport) -> None:
    breakdown = ",".join(f"{k}:{v}" for k, v in sorted(report.truncated_by_type.items())) or "none"
    n_applied = sum(1 for a in report.actions if a.status == "applied")
    await write_audit(
        "heal_run_completed",
        f"heal completed: {n_applied} actions applied, {report.total_suppressed} suppressed",
        scope=report.scope,
        scope_id=report.scope_id or "",
        actor=report.actor,
        apply="true" if report.apply else "false",
        aggressive="true" if report.aggressive else "false",
        n_actions=str(len(report.actions)),
        n_truncated=str(report.total_suppressed),
        truncation_breakdown=breakdown,
    )
