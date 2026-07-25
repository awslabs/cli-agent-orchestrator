"""Regression tests for wiki_lint event-loop responsiveness."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import settings_service, wiki_lint


@pytest.fixture
def db_engine(tmp_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'lint-offload.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.mark.asyncio
async def test_stale_claim_detector_does_not_block_event_loop(
    tmp_path: Path, db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _slow_detector(rows: list, repo_root_resolved: str) -> list:
        time.sleep(1.0)
        return []

    async def _no_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: True)
    monkeypatch.setattr(wiki_lint, "_detect_stale_claims", _slow_detector)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.audit_log.write_audit",
        _no_audit,
    )

    async def _marker() -> float:
        await asyncio.sleep(0.1)
        return time.monotonic()

    start = time.monotonic()
    lint_task = asyncio.create_task(
        wiki_lint.run_lint(
            "test-project",
            repo_root=str(tmp_path),
            base_dir=tmp_path / "memory",
            db_engine=db_engine,
        )
    )
    marker_task = asyncio.create_task(_marker())

    marker_done_at = await asyncio.wait_for(marker_task, timeout=0.5)
    assert marker_done_at - start < 0.5

    await lint_task
