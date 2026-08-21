"""Server startup policy tests for memory metadata reconciliation."""

import asyncio
import logging
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import _reconcile_memory_at_startup, app, lifespan
from cli_agent_orchestrator.clients import database as db_module
from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import memory_reconciliation
from cli_agent_orchestrator.services.memory_reconciliation import (
    MemoryIdentity,
    MemoryReconciliationError,
    RepairAction,
    RepairFinding,
    RepairRecord,
    RepairReport,
)


def _write_topic(
    base: Path, scope: str, scope_id: str | None, key: str, tags: str = "startup"
) -> Path:
    if scope == "project":
        path = base / str(scope_id) / "wiki" / scope / f"{key}.md"
    elif scope in {"session", "agent"}:
        path = base / "global" / "wiki" / scope / str(scope_id) / f"{key}.md"
    else:
        path = base / "global" / "wiki" / scope / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {key}\n"
        f"<!-- id: {uuid.uuid4()} | scope: {scope} | "
        f"type: reference | tags: {tags} -->\n\n"
        "## 2026-07-16T02:00:00Z\n"
        "surviving content\n",
        encoding="utf-8",
    )
    return path


async def _quick_task(*args, **kwargs) -> None:
    del args, kwargs
    await asyncio.sleep(0)


def test_unexpected_reconciliation_failure_logs_and_returns(caplog) -> None:
    report = RepairReport(
        records=(
            RepairRecord(
                identity=MemoryIdentity("repaired-first", "global"),
                file_path="/memory/repaired-first.md",
                actions=(RepairAction.CREATE_METADATA,),
                status="repaired",
            ),
            RepairRecord(
                identity=MemoryIdentity("failed-later", "global"),
                file_path="/memory/failed-later.md",
                actions=(RepairAction.FAILED,),
                status="failed",
                finding=RepairFinding("unexpected_error", "injected"),
            ),
        ),
        applied=True,
    )
    with (
        patch(
            "cli_agent_orchestrator.services.memory_reconciliation.reconcile_memory_startup",
            side_effect=MemoryReconciliationError(report),
        ),
        caplog.at_level(logging.ERROR, logger="cli_agent_orchestrator.api.main"),
    ):
        _reconcile_memory_at_startup()

    assert "repaired=1" in caplog.text
    assert "failed=1" in caplog.text
    assert "cao memory repair --apply" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_repairs_replacement_database_and_second_startup_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = tmp_path / "memory"
    expected = {
        ("global-topic", "global", None),
        ("project-topic", "project", "project-one"),
        ("session-topic", "session", "session-one"),
        ("agent-topic", "agent", "code_supervisor"),
    }
    for key, scope, scope_id in expected:
        _write_topic(base, scope, scope_id, key)

    replacement_engine = create_engine(
        f"sqlite:///{tmp_path / 'replacement.db'}",
        connect_args={"check_same_thread": False},
    )
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=replacement_engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessions)
    monkeypatch.setattr(memory_reconciliation, "MEMORY_BASE_DIR", base)

    def initialize_replacement() -> None:
        Base.metadata.create_all(bind=replacement_engine)

    forbidden = {
        "lint": AsyncMock(side_effect=AssertionError("full lint called")),
        "compile": AsyncMock(side_effect=AssertionError("compiler called")),
        "related": AsyncMock(side_effect=AssertionError("model linking called")),
        "network": MagicMock(side_effect=AssertionError("network called")),
    }
    with ExitStack() as stack:
        stack.enter_context(patch("cli_agent_orchestrator.api.main.setup_logging"))
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.api.main.init_db",
                side_effect=initialize_replacement,
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
                return_value=True,
            )
        )
        stack.enter_context(patch("cli_agent_orchestrator.api.main.cleanup_old_data"))
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.api.main.cleanup_expired_memories",
                new=AsyncMock(side_effect=_quick_task),
            )
        )
        for name in (
            "flow_daemon",
            "opencode_inbox_delivery_daemon",
            "inbox_reconciliation_daemon",
        ):
            stack.enter_context(patch(f"cli_agent_orchestrator.api.main.{name}", _quick_task))
        for name in ("status_monitor.run", "log_writer.run", "inbox_service.run"):
            stack.enter_context(patch(f"cli_agent_orchestrator.api.main.{name}", new=AsyncMock()))
        stack.enter_context(patch("cli_agent_orchestrator.api.main.bus.set_loop"))
        stack.enter_context(
            patch("cli_agent_orchestrator.api.main.get_backend", return_value=MagicMock())
        )
        stack.enter_context(patch.object(PluginRegistry, "load", new=AsyncMock()))
        stack.enter_context(patch.object(PluginRegistry, "teardown", new=AsyncMock()))
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.wiki_lint.run_lint",
                new=forbidden["lint"],
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.wiki_compiler.compile",
                new=forbidden["compile"],
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.wiki_compiler.find_related",
                new=forbidden["related"],
            )
        )
        stack.enter_context(patch("requests.get", new=forbidden["network"]))
        with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.api.main"):
            async with lifespan(app):
                pass
            async with lifespan(app):
                pass

    with sessions() as db:
        rows = db.query(MemoryMetadataModel).all()
        assert {(row.key, row.scope, row.scope_id) for row in rows} == expected
    global_index = (base / "global" / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "[global-topic](global/global-topic.md)" in global_index
    assert "[session-topic](session/session-one/session-topic.md)" in global_index
    assert "[agent-topic](agent/code_supervisor/agent-topic.md)" in global_index
    project_index = (base / "project-one" / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "[project-topic](project/project-topic.md)" in project_index
    assert "repaired=4" in caplog.text
    assert "unchanged=4" in caplog.text
    for blocked in forbidden.values():
        blocked.assert_not_called()
