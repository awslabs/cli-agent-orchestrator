"""Promotion service tests (Phase 2).

- **AC1** — plan(): only agent-scope, promotable-type, sufficiently-recalled
  memories become candidates; already-promoted identical text is excluded;
  changed text becomes an update.
- **AC2** — apply(): disabled raises PromotionDisabledError before file
  access; enabled writes the Learned Patterns block; audit is best-effort.
- **AC3** — lesson text extraction takes the newest timestamped section and
  skips See Also.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.learned_patterns import read_lessons
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.promotion_service import (
    PromotionDisabledError,
    PromotionService,
)

PROFILE = """---
name: transformer
role: developer
---

# TRANSFORMER AGENT

## Role
Convert SSIS packages.
"""


def _engine(db_path: Path) -> Any:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


def _ctx(agent_profile: str = "transformer") -> dict:
    return {
        "terminal_id": "term-p2",
        "session_name": "sess-p2",
        "agent_profile": agent_profile,
        "provider": "claude_code",
        "cwd": "/home/user/proj-p2",
    }


@pytest.fixture
def stack(tmp_path: Path) -> Any:
    """An isolated memory store + promotion service + profile file."""
    engine = _engine(tmp_path / "p2.db")
    mem = MemoryService(base_dir=tmp_path / "mem", db_engine=engine)
    mem._get_terminal_context = lambda tid: _ctx()  # type: ignore[method-assign]
    promo = PromotionService(memory_base_dir=tmp_path / "mem", db_engine=engine)
    profile = tmp_path / "transformer.md"
    profile.write_text(PROFILE, encoding="utf-8")
    return mem, promo, profile, engine


def _store_lesson(
    mem: MemoryService,
    key: str,
    content: str,
    *,
    memory_type: str = "feedback",
    access_count: int = 5,
    engine: Any = None,
) -> None:
    """Store an agent-scope lesson and set its access_count directly."""
    with patch(
        "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
        return_value=True,
    ):
        asyncio.run(
            mem.store(
                content=content,
                scope="agent",
                memory_type=memory_type,
                key=key,
                terminal_context=_ctx(),
            )
        )
    with mem._get_db_session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=key, scope="agent").one()
        row.access_count = access_count
        db.commit()


ENABLED_TARGET = "cli_agent_orchestrator.services.promotion_service._is_promotion_enabled"


# ---------------------------------------------------------------------------
# AC1 — plan eligibility
# ---------------------------------------------------------------------------


class TestPlan:
    def test_eligible_lesson_becomes_add_candidate(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "broadcast-joins", "Use broadcast joins for Lookups.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        assert len(plan.candidates) == 1
        cand = plan.candidates[0]
        assert cand.key == "broadcast-joins"
        assert cand.action == "add"
        assert "broadcast joins" in cand.text

    def test_low_access_count_excluded(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "rarely-recalled", "Never reinforced.", access_count=1)
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        assert plan.empty

    def test_min_access_count_configurable(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "once-recalled", "Reinforced once.", access_count=1)
        plan = promo.plan(agent_profile="transformer", profile_path=profile, min_access_count=1)
        assert len(plan.candidates) == 1

    def test_other_agent_excluded(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "transformer-lesson", "Mine.")
        plan = promo.plan(agent_profile="improver", profile_path=profile)
        assert plan.empty

    def test_reference_type_excluded(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "some-url", "See https://internal/wiki.", memory_type="reference")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        assert plan.empty

    def test_already_promoted_identical_excluded(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "k1", "Stable lesson.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        with patch(ENABLED_TARGET, return_value=True):
            promo.apply(plan)
        plan2 = promo.plan(agent_profile="transformer", profile_path=profile)
        assert plan2.empty

    def test_changed_text_becomes_update(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "k1", "Original lesson.")
        with patch(ENABLED_TARGET, return_value=True):
            promo.apply(promo.plan(agent_profile="transformer", profile_path=profile))
        # Re-store with new content (appends a newer timestamped section).
        _store_lesson(mem, "k1", "Revised lesson.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        assert len(plan.candidates) == 1
        assert plan.candidates[0].action == "update"
        assert plan.candidates[0].text == "Revised lesson."


# ---------------------------------------------------------------------------
# AC2 — apply gating and writes
# ---------------------------------------------------------------------------


class TestApply:
    def test_disabled_raises_before_touching_file(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "k1", "Lesson.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        before = profile.read_text()
        with patch(ENABLED_TARGET, return_value=False):
            with pytest.raises(PromotionDisabledError):
                promo.apply(plan)
        assert profile.read_text() == before

    def test_apply_writes_block(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "k1", "Lesson one.")
        _store_lesson(mem, "k2", "Lesson two.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        with patch(ENABLED_TARGET, return_value=True):
            report = promo.apply(plan)
        assert sorted(report.added) == ["k1", "k2"]
        lessons = read_lessons(profile)
        assert lessons["k1"] == "Lesson one."
        assert lessons["k2"] == "Lesson two."
        # Rest of the profile intact.
        assert "## Role" in profile.read_text()

    def test_empty_plan_is_noop(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        with patch(ENABLED_TARGET, return_value=True):
            report = promo.apply(plan)
        assert not (report.added or report.updated)

    def test_audit_event_type_is_whitelisted(self, stack: Any) -> None:
        """The promotion audit entry must not be silently dropped.

        Regression: 'instruction_promotion' was missing from the audit event
        whitelist, so every apply()'s audit entry was rejected as
        unknown_event.
        """
        from cli_agent_orchestrator.services.audit_log import (
            AUDIT_EVENT_WHITELIST,
            NOWAIT_AUDIT_EVENTS,
        )

        # _audit uses write_audit_nowait, so the NOWAIT set is the one that matters.
        assert "instruction_promotion" in NOWAIT_AUDIT_EVENTS
        assert "instruction_promotion" in AUDIT_EVENT_WHITELIST


# ---------------------------------------------------------------------------
# AC3 — lesson text extraction
# ---------------------------------------------------------------------------


class TestLessonText:
    def test_takes_newest_section_skips_see_also(self, stack: Any, tmp_path: Path) -> None:
        mem, promo, profile, engine = stack
        wiki = tmp_path / "topic.md"
        wiki.write_text(
            "# k1\n<!-- id: x | scope: agent | type: feedback | tags: -->\n\n"
            "## 2026-07-01T00:00:00\nOld lesson text.\n\n"
            "## 2026-07-20T00:00:00\nNewest lesson text.\n\n"
            "## See Also\n- [[other-topic]]\n",
            encoding="utf-8",
        )
        assert promo._lesson_text(wiki) == "Newest lesson text."

    def test_missing_file_returns_empty(self, stack: Any, tmp_path: Path) -> None:
        mem, promo, profile, engine = stack
        assert promo._lesson_text(tmp_path / "ghost.md") == ""


# ---------------------------------------------------------------------------
# Coverage completions — guard fallbacks, plan skips, apply abort, audit
# ---------------------------------------------------------------------------


class TestGuardFallback:
    def test_is_promotion_enabled_fails_closed_on_import_error(self) -> None:
        from cli_agent_orchestrator.services import promotion_service

        with patch(
            "cli_agent_orchestrator.services.settings_service.is_instruction_promotion_enabled",
            side_effect=RuntimeError("settings unreadable"),
        ):
            assert promotion_service._is_promotion_enabled() is False

    def test_is_promotion_enabled_reads_settings(self) -> None:
        from cli_agent_orchestrator.services import promotion_service

        with patch(
            "cli_agent_orchestrator.services.settings_service.is_instruction_promotion_enabled",
            return_value=True,
        ):
            assert promotion_service._is_promotion_enabled() is True


class TestPlanSkips:
    def test_oversized_lesson_skipped_in_plan(self, stack: Any) -> None:
        from cli_agent_orchestrator.services.learned_patterns import MAX_LESSON_CHARS

        mem, promo, profile, engine = stack
        _store_lesson(mem, "too-long", "y" * (MAX_LESSON_CHARS + 50))
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        assert plan.empty  # oversized lessons are excluded, not errors

    def test_missing_wiki_file_skipped_in_plan(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "ghost-lesson", "Some lesson.")
        # Delete the wiki file behind the metadata row.
        with mem._get_db_session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="ghost-lesson").one()
            Path(row.file_path).unlink()
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        assert plan.empty


class TestApplyAbort:
    def test_apply_wraps_learned_patterns_error(self, stack: Any) -> None:
        from cli_agent_orchestrator.services.learned_patterns import LearnedPatternsError
        from cli_agent_orchestrator.services.promotion_service import PromotionCandidate

        mem, promo, profile, engine = stack
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        # Inject a candidate that will fail apply-time validation (bad key
        # simulates text/key mutation between plan and apply).
        plan.candidates.append(
            PromotionCandidate(key="BAD KEY", text="x", access_count=5, action="add")
        )
        with patch(ENABLED_TARGET, return_value=True):
            with pytest.raises(LearnedPatternsError, match="promotion aborted"):
                promo.apply(plan)

    def test_audit_failure_never_blocks_promotion(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "k1", "Lesson.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        with (
            patch(ENABLED_TARGET, return_value=True),
            patch(
                "cli_agent_orchestrator.services.audit_log.write_audit_nowait",
                side_effect=RuntimeError("audit down"),
            ),
        ):
            report = promo.apply(plan)  # must not raise
        assert report.added == ["k1"]

    def test_audit_success_path_emits_event(self, stack: Any) -> None:
        mem, promo, profile, engine = stack
        _store_lesson(mem, "k1", "Lesson.")
        plan = promo.plan(agent_profile="transformer", profile_path=profile)
        with (
            patch(ENABLED_TARGET, return_value=True),
            patch("cli_agent_orchestrator.services.audit_log.write_audit_nowait") as mock_audit,
        ):
            promo.apply(plan)
        assert mock_audit.call_count == 1
        assert mock_audit.call_args.args[0] == "instruction_promotion"
