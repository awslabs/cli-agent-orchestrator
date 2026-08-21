"""Instruction promotion: agent-scope memory lessons → profile Learned Patterns (Phase 2).

The conservative promotion rule (signal-free fallback): a lesson qualifies
once it has been *recalled* enough times without being contradicted —
``access_count >= min_access_count`` — because recall frequency is the one
reinforcement signal the memory subsystem already tracks. Workflows with a
real fitness metric (validator scores, benchmark numbers) should gate
apply() on that metric externally; this service never decides "did quality
improve", it only moves already-reinforced lessons.

Two-step, dry-run-first design (mirroring wiki_healer):
- ``plan()`` — read-only. Finds eligible agent-scope memories and diffs
  them against the profile's current Learned Patterns block.
- ``apply()`` — executes a plan via ``learned_patterns.apply_deltas``
  (itemized deltas, atomic write). Gated on
  ``is_instruction_promotion_enabled()`` and audit-logged.

Safety invariants:
- Only ``agent``-scope memories of type ``feedback``/``project`` are
  considered — the same scopes the retrospector writes.
- Promotion never deletes memories; the wiki stays the source of truth.
- Profile files are only ever edited inside the delimited block, and only
  profiles that already exist (never created).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from cli_agent_orchestrator.services.learned_patterns import (
    MAX_LESSON_CHARS,
    LearnedPatternsError,
    apply_deltas,
    read_lessons,
)

logger = logging.getLogger(__name__)

# A lesson must have been recalled at least this many times before it is
# eligible — recall count is the reinforcement signal (each recall means a
# curator or agent found it relevant again).
DEFAULT_MIN_ACCESS_COUNT = 3

PROMOTABLE_TYPES = ("feedback", "project")

PROMOTION_DISABLED_MESSAGE = (
    "instruction promotion is disabled. Set memory.instruction_promotion_enabled=true "
    "in settings.json (requires memory.learning_enabled=true) to enable."
)


class PromotionDisabledError(RuntimeError):
    """Raised when apply() is called while instruction promotion is disabled."""


def _is_promotion_enabled() -> bool:
    """Module-level guard, lazily imported; fails closed (opt-in feature)."""
    try:
        from cli_agent_orchestrator.services.settings_service import (
            is_instruction_promotion_enabled,
        )

        return is_instruction_promotion_enabled()
    except Exception:
        return False


@dataclass
class PromotionCandidate:
    """One lesson eligible for promotion into a profile file."""

    key: str
    text: str
    access_count: int
    action: str  # "add" | "update"


@dataclass
class PromotionPlan:
    """Read-only promotion proposal for one agent profile."""

    agent_profile: str
    profile_path: Path
    candidates: List[PromotionCandidate] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.candidates


@dataclass
class PromotionReport:
    """Content-free result of an apply() run."""

    agent_profile: str
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


class PromotionService:
    """Plan and apply lesson promotion for agent profiles.

    Follows the MemoryService engine/base_dir injection pattern so tests run
    against isolated stores.
    """

    def __init__(self, memory_base_dir: Optional[Path] = None, db_engine: Any = None):
        from cli_agent_orchestrator.services.memory_service import MemoryService

        self._memory = MemoryService(base_dir=memory_base_dir, db_engine=db_engine)

    def plan(
        self,
        *,
        agent_profile: str,
        profile_path: Path,
        min_access_count: int = DEFAULT_MIN_ACCESS_COUNT,
    ) -> PromotionPlan:
        """Build a read-only promotion plan for ``agent_profile``.

        Eligibility: agent-scope memories keyed to this profile, of a
        promotable type, recalled at least ``min_access_count`` times, whose
        text fits the lesson cap. Already-promoted lessons with identical
        text are excluded; changed text becomes an "update" candidate.
        """
        plan = PromotionPlan(agent_profile=agent_profile, profile_path=profile_path)

        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._memory._get_db_session() as db:
            rows = (
                db.query(MemoryMetadataModel)
                .filter(
                    MemoryMetadataModel.scope == "agent",
                    MemoryMetadataModel.scope_id == agent_profile,
                    MemoryMetadataModel.memory_type.in_(PROMOTABLE_TYPES),
                    MemoryMetadataModel.access_count >= min_access_count,
                )
                .order_by(MemoryMetadataModel.access_count.desc())
                .all()
            )
            eligible = [
                {"key": r.key, "file_path": r.file_path, "access_count": r.access_count}
                for r in rows
            ]

        current = read_lessons(profile_path) if profile_path.is_file() else {}

        for row in eligible:
            text = self._lesson_text(Path(row["file_path"]))
            if not text:
                continue
            if len(text) > MAX_LESSON_CHARS:
                logger.info(
                    "promotion: lesson %r exceeds %d chars; skipping (compact it first)",
                    row["key"],
                    MAX_LESSON_CHARS,
                )
                continue
            existing = current.get(row["key"])
            if existing == text:
                continue  # already promoted, unchanged
            plan.candidates.append(
                PromotionCandidate(
                    key=row["key"],
                    text=text,
                    access_count=row["access_count"],
                    action="update" if existing is not None else "add",
                )
            )
        return plan

    def apply(self, plan: PromotionPlan) -> PromotionReport:
        """Execute a promotion plan. Requires instruction promotion enabled.

        Raises:
            PromotionDisabledError: when the promotion flag (or a parent
                flag) is off — checked before any file access.
            FileNotFoundError: when the profile file does not exist.
        """
        if not _is_promotion_enabled():
            raise PromotionDisabledError(PROMOTION_DISABLED_MESSAGE)

        report = PromotionReport(agent_profile=plan.agent_profile)
        if plan.empty:
            return report

        adds = {c.key: c.text for c in plan.candidates}
        try:
            result = apply_deltas(plan.profile_path, add=adds)
        except LearnedPatternsError as e:
            # Validation surprises at apply time (e.g. text mutated between
            # plan and apply) abort the whole run — partial promotion would
            # make the report lie.
            raise LearnedPatternsError(f"promotion aborted: {e}") from e

        report.added = result.added
        report.updated = result.updated
        report.skipped = result.skipped

        self._audit(plan, report)
        return report

    def _lesson_text(self, wiki_path: Path) -> str:
        """Extract the promotable lesson text from a wiki topic file.

        Takes the body of the LAST ``## <timestamp>`` section — the most
        recent entry, which for retrospector-written topics is the current
        form of the lesson. Skips the ``## See Also`` section the compiler
        may append. Returns "" when the file is missing or empty.
        """
        try:
            content = wiki_path.read_text(encoding="utf-8")
        except OSError:
            return ""

        sections: list[tuple[str, list[str]]] = []
        current_heading: Optional[str] = None
        current_lines: list[str] = []
        for line in content.splitlines():
            if line.startswith("## "):
                if current_heading is not None:
                    sections.append((current_heading, current_lines))
                current_heading = line[3:].strip()
                current_lines = []
            elif current_heading is not None:
                current_lines.append(line)
        if current_heading is not None:
            sections.append((current_heading, current_lines))

        for heading, lines in reversed(sections):
            if heading.lower() == "see also":
                continue
            text = " ".join(" ".join(lines).split())
            if text:
                return text
        return ""

    def _audit(self, plan: PromotionPlan, report: PromotionReport) -> None:
        """Audit-log the promotion (content-free: keys and counts only)."""
        try:
            from cli_agent_orchestrator.services.audit_log import write_audit_nowait

            write_audit_nowait(
                "instruction_promotion",
                f"promoted lessons into profile {plan.agent_profile}",
                profile=plan.agent_profile,
                added=",".join(report.added) or "-",
                updated=",".join(report.updated) or "-",
                skipped=",".join(report.skipped) or "-",
            )
        except Exception as e:  # noqa: BLE001 — audit failure never blocks promotion
            logger.debug(f"promotion audit log failed: {e}")
