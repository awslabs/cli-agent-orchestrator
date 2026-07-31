"""Outcome capture service tests (self-learning Phase 1).

- **AC1** — ``record_outcome()`` persists a row and round-trips through
  ``list_outcomes()`` with filters (session, agent_profile, workflow).
- **AC2** — Learning disabled: writes raise ``LearningDisabledError`` before
  validation; reads return ``[]``; nothing is persisted.
- **AC3** — Validation: required fields, score bounds, note/label caps.
- **AC4** — Listing is newest-first and limit-capped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, WorkflowOutcomeModel
from cli_agent_orchestrator.services.outcome_service import (
    MAX_NOTES_CHARS,
    LearningDisabledError,
    OutcomeService,
)


def _make_svc(db_path: Path) -> OutcomeService:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return OutcomeService(db_engine=engine)


@pytest.fixture
def enabled() -> Any:
    with patch(
        "cli_agent_orchestrator.services.outcome_service._is_learning_enabled",
        return_value=True,
    ):
        yield


@pytest.fixture
def disabled() -> Any:
    with patch(
        "cli_agent_orchestrator.services.outcome_service._is_learning_enabled",
        return_value=False,
    ):
        yield


# ---------------------------------------------------------------------------
# AC1 — record + list round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_record_and_list(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        rec = svc.record_outcome(
            session_name="ssis-batch-1",
            task_label="convert package CustomerETL",
            success=False,
            workflow_name="ssis-migration",
            agent_profile="transformer",
            source_terminal_id="term-1",
            score=40,
            friction_notes="Lookup component with partial cache not mapped.",
        )
        assert rec["id"]
        assert rec["success"] is False
        assert rec["score"] == 40

        out = svc.list_outcomes(session_name="ssis-batch-1")
        assert len(out) == 1
        assert out[0]["task_label"] == "convert package CustomerETL"
        assert out[0]["agent_profile"] == "transformer"
        assert out[0]["created_at"]  # ISO string

    def test_filters(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        for agent, wf in [
            ("transformer", "ssis"),
            ("improver", "ssis"),
            ("transformer", "pentaho"),
        ]:
            svc.record_outcome(
                session_name="s1",
                task_label=f"task {agent} {wf}",
                success=True,
                agent_profile=agent,
                workflow_name=wf,
            )
        assert len(svc.list_outcomes(agent_profile="transformer")) == 2
        assert len(svc.list_outcomes(workflow_name="pentaho")) == 1
        assert len(svc.list_outcomes(session_name="s1", agent_profile="improver")) == 1
        assert svc.list_outcomes(session_name="other") == []

    def test_optional_fields_default_none(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        rec = svc.record_outcome(session_name="s1", task_label="t", success=True)
        assert rec["workflow_name"] is None
        assert rec["agent_profile"] is None
        assert rec["score"] is None
        assert rec["friction_notes"] == ""


# ---------------------------------------------------------------------------
# AC2 — disabled behavior
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_record_raises_before_validation(self, tmp_path: Path, disabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        # Invalid inputs (empty session/task) must still surface the
        # canonical disabled error — guard order mirrors memory store().
        with pytest.raises(LearningDisabledError):
            svc.record_outcome(session_name="", task_label="", success=True)

    def test_record_persists_nothing(self, tmp_path: Path, disabled: Any) -> None:
        db_path = tmp_path / "o.db"
        svc = _make_svc(db_path)
        with pytest.raises(LearningDisabledError):
            svc.record_outcome(session_name="s1", task_label="t", success=True)
        with svc._get_db_session() as db:
            assert db.query(WorkflowOutcomeModel).count() == 0

    def test_list_returns_empty(self, tmp_path: Path, disabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        assert svc.list_outcomes() == []


# ---------------------------------------------------------------------------
# AC3 — validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_requires_session_name(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        with pytest.raises(ValueError, match="session_name"):
            svc.record_outcome(session_name="  ", task_label="t", success=True)

    def test_requires_task_label(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        with pytest.raises(ValueError, match="task_label"):
            svc.record_outcome(session_name="s", task_label="  ", success=True)

    def test_score_bounds(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        for bad in (-1, 101):
            with pytest.raises(ValueError, match="score"):
                svc.record_outcome(session_name="s", task_label="t", success=True, score=bad)

    def test_score_rejects_bool(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        with pytest.raises(ValueError, match="score"):
            svc.record_outcome(session_name="s", task_label="t", success=True, score=True)

    def test_notes_capped(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        rec = svc.record_outcome(
            session_name="s",
            task_label="t",
            success=True,
            friction_notes="x" * (MAX_NOTES_CHARS + 500),
        )
        assert len(rec["friction_notes"]) == MAX_NOTES_CHARS


# ---------------------------------------------------------------------------
# AC4 — ordering and limits
# ---------------------------------------------------------------------------


class TestListing:
    def test_newest_first_and_limit(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        for i in range(5):
            svc.record_outcome(session_name="s", task_label=f"task-{i}", success=True)
        out = svc.list_outcomes(limit=3)
        assert len(out) == 3
        # created_at has second precision in SQLite; ids differ, so check
        # the newest task is present and the oldest two are cut.
        labels = {o["task_label"] for o in out}
        assert "task-4" in labels
        assert "task-0" not in labels

    def test_limit_capped(self, tmp_path: Path, enabled: Any) -> None:
        svc = _make_svc(tmp_path / "o.db")
        svc.record_outcome(session_name="s", task_label="t", success=True)
        # Absurd limits are clamped, not errors.
        assert len(svc.list_outcomes(limit=100_000)) == 1
        assert len(svc.list_outcomes(limit=0)) == 1


# ---------------------------------------------------------------------------
# Coverage completions — guard fallback, label truncation
# ---------------------------------------------------------------------------


class TestGuardFallback:
    def test_is_learning_enabled_fails_closed_on_error(self) -> None:
        from cli_agent_orchestrator.services import outcome_service

        with patch(
            "cli_agent_orchestrator.services.settings_service.is_learning_enabled",
            side_effect=RuntimeError("settings unreadable"),
        ):
            assert outcome_service._is_learning_enabled() is False

    def test_task_label_truncated(self, tmp_path: Path, enabled: Any) -> None:
        from cli_agent_orchestrator.services.outcome_service import MAX_TASK_LABEL_CHARS

        svc = _make_svc(tmp_path / "o.db")
        rec = svc.record_outcome(
            session_name="s",
            task_label="t" * (MAX_TASK_LABEL_CHARS + 100),
            success=True,
        )
        assert len(rec["task_label"]) == MAX_TASK_LABEL_CHARS
