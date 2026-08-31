"""End-to-end self-learning loop test (Phases 1+2 integration).

Simulates the full loop a real deployment runs across sessions:

    1. workers report outcomes        (OutcomeService — Phase 1)
    2. retrospector reads outcomes,
       stores lessons to agent scope  (MemoryService, simulated agent)
    3. lessons get recalled/reinforced across runs (access_count)
    4. promotion plan/apply moves
       reinforced lessons into the
       profile's Learned Patterns     (PromotionService — Phase 2)
    5. next session's injection sees
       the lesson in the profile file

Every stage runs against isolated tmp stores; the only mocked seams are
the enablement flags and terminal context (no server required).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.learned_patterns import read_lessons
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.outcome_service import OutcomeService
from cli_agent_orchestrator.services.promotion_service import PromotionService

TRANSFORMER_PROFILE = """---
name: transformer
role: developer
provider: claude_code
---

# TRANSFORMER AGENT

## Role
Convert SSIS packages to Glue PySpark.

## Critical Rules
1. Never fabricate output.
"""


def _ctx(agent: str = "transformer", session: str = "ssis-batch-1") -> dict:
    return {
        "terminal_id": "term-e2e",
        "session_name": session,
        "agent_profile": agent,
        "provider": "claude_code",
        "cwd": "/home/user/etl-project",
    }


@pytest.fixture
def stack(tmp_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'e2e.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    mem = MemoryService(base_dir=tmp_path / "mem", db_engine=engine)
    outcomes = OutcomeService(db_engine=engine)
    promo = PromotionService(memory_base_dir=tmp_path / "mem", db_engine=engine)
    profile = tmp_path / "agent-store" / "transformer.md"
    profile.parent.mkdir(parents=True)
    profile.write_text(TRANSFORMER_PROFILE, encoding="utf-8")
    return mem, outcomes, promo, profile


LEARNING_ON = patch(
    "cli_agent_orchestrator.services.outcome_service._is_learning_enabled",
    return_value=True,
)
MEMORY_ON = patch(
    "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
    return_value=True,
)
PROMOTION_ON = patch(
    "cli_agent_orchestrator.services.promotion_service._is_promotion_enabled",
    return_value=True,
)


def test_full_learning_loop(stack: Any) -> None:
    mem, outcomes, promo, profile = stack

    # ---- Stage 1: three packages run; transformer fails the same way twice
    with LEARNING_ON:
        for pkg, ok, notes in [
            ("CustomerETL", False, "Lookup with partial cache emitted invalid join."),
            ("OrdersETL", False, "Same partial-cache Lookup failure as CustomerETL."),
            ("InventoryETL", True, ""),
        ]:
            outcomes.record_outcome(
                session_name="ssis-batch-1",
                workflow_name="ssis-migration",
                task_label=f"convert package {pkg}",
                agent_profile="transformer",
                success=ok,
                score=40 if not ok else 95,
                friction_notes=notes,
            )

        # ---- Stage 2: retrospector reads outcomes and distills ONE lesson.
        # This runs the REAL retrospector path: the store_lesson MCP tool
        # called from a RETROSPECTOR terminal context, targeting the
        # transformer profile. (A prior version of this test called
        # MemoryService.store() with a synthetic transformer context, which
        # masked lessons landing under the retrospector's own scope — PR
        # #515 review finding.)
        recorded = outcomes.list_outcomes(session_name="ssis-batch-1")
        assert len(recorded) == 3
        failures = [o for o in recorded if not o["success"]]
        assert len(failures) == 2  # recurring pattern → lesson-worthy

    lesson_text = (
        "Use broadcast joins when converting SSIS Lookup components with "
        "partial cache; direct join emission produced invalid Glue code in "
        "CustomerETL and OrdersETL. Applies when: converting Lookup components."
    )
    from cli_agent_orchestrator.mcp_server import server as srv
    from cli_agent_orchestrator.mcp_server.server import store_lesson

    retro_ctx = _ctx(agent="retrospector")  # the CALLER is the retrospector
    with (
        MEMORY_ON,
        patch(
            "cli_agent_orchestrator.services.settings_service.is_learning_enabled",
            return_value=True,
        ),
        patch.object(srv, "_get_terminal_context_from_env", return_value=retro_ctx),
        patch(
            "cli_agent_orchestrator.services.memory_service.MemoryService",
            return_value=mem,
        ),
    ):
        result = asyncio.run(
            store_lesson(
                target_agent_profile="transformer",
                content=lesson_text,
                key="lookup-partial-cache-broadcast",
                tags=None,
            )
        )
    assert result["success"] is True, result
    # The lesson must land in the WORKER's scope, not the retrospector's —
    # both in the response and in the persisted metadata row.
    assert result["scope_id"] == "transformer"
    with mem._get_db_session() as db:
        persisted = (
            db.query(MemoryMetadataModel).filter_by(key="lookup-partial-cache-broadcast").one()
        )
        assert persisted.scope == "agent"
        assert persisted.scope_id == "transformer"

    # ---- Stage 3: later sessions recall the lesson (reinforcement).
    # recall() bumps access_count through the rate-limited batch path; for
    # determinism we set the reinforcement level directly, representing
    # "recalled in 4 subsequent package runs".
    with mem._get_db_session() as db:
        row = (
            db.query(MemoryMetadataModel)
            .filter_by(key="lookup-partial-cache-broadcast", scope="agent")
            .one()
        )
        row.access_count = 4
        db.commit()

    # ---- Stage 4: promotion plan finds it; apply writes the profile block
    plan = promo.plan(agent_profile="transformer", profile_path=profile)
    assert [c.key for c in plan.candidates] == ["lookup-partial-cache-broadcast"]
    assert plan.candidates[0].action == "add"

    with PROMOTION_ON:
        report = promo.apply(plan)
    assert report.added == ["lookup-partial-cache-broadcast"]

    # ---- Stage 5: the profile now carries the lesson for every future run
    content = profile.read_text()
    assert "## Learned Patterns" in content
    assert "broadcast joins" in content
    assert "Applies when: converting Lookup components." in content
    # Original profile untouched outside the block.
    assert "## Critical Rules" in content
    assert content.startswith("---\nname: transformer")

    # ---- Idempotence: re-running the loop does not duplicate anything
    plan2 = promo.plan(agent_profile="transformer", profile_path=profile)
    assert plan2.empty
    assert len(read_lessons(profile)) == 1


def test_loop_stops_at_disabled_gates(stack: Any) -> None:
    """Every stage fails safe when its flag is off: nothing propagates."""
    mem, outcomes, promo, profile = stack

    # Learning off → outcome writes rejected, reads empty.
    from cli_agent_orchestrator.services.outcome_service import LearningDisabledError

    with patch(
        "cli_agent_orchestrator.services.outcome_service._is_learning_enabled",
        return_value=False,
    ):
        with pytest.raises(LearningDisabledError):
            outcomes.record_outcome(session_name="s", task_label="t", success=True)
        assert outcomes.list_outcomes() == []

    # Store a reinforced lesson (memory itself on), then promotion off →
    # plan works (read-only) but apply refuses and the profile is untouched.
    with MEMORY_ON:
        asyncio.run(
            mem.store(
                content="A lesson.",
                scope="agent",
                memory_type="feedback",
                key="some-lesson",
                terminal_context=_ctx(),
            )
        )
    with mem._get_db_session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key="some-lesson").one()
        row.access_count = 10
        db.commit()

    from cli_agent_orchestrator.services.promotion_service import PromotionDisabledError

    plan = promo.plan(agent_profile="transformer", profile_path=profile)
    assert not plan.empty
    before = profile.read_text()
    with patch(
        "cli_agent_orchestrator.services.promotion_service._is_promotion_enabled",
        return_value=False,
    ):
        with pytest.raises(PromotionDisabledError):
            promo.apply(plan)
    assert profile.read_text() == before
