"""Operator inspection leaves reinforcement/promotion counters alone when opted in."""

from datetime import datetime, timedelta, timezone
from test.services.test_scoring import _ctx, _row, _run, db_engine, svc

import pytest
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import MemoryMetadataModel
from cli_agent_orchestrator.services import memory_relationship_service, settings_service


@pytest.fixture
def inspection(svc, db_engine, monkeypatch):
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    ctx = _ctx(caller_scope="global")
    _run(
        svc.store(
            key="inspect-note",
            content="Prefer pytest for tests.",
            scope="global",
            terminal_context=ctx,
        )
    )
    return svc, factory, ctx


def test_recall_record_access_false_leaves_counts(inspection, db_engine):
    service, factory, ctx = inspection
    rows = _run(service.recall(query="inspect-note", scope="global", record_access=False))
    assert [m.key for m in rows] == ["inspect-note"]
    row = _row(db_engine, "inspect-note")
    assert row.access_count == 0
    assert row.last_accessed_at is None


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("surface", ["list", "show"])
def test_cli_inspection_access_counts(inspection, monkeypatch, db_engine, enabled, surface):
    from cli_agent_orchestrator.cli.commands import memory

    service, _, ctx = inspection
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", str(enabled).lower())
    monkeypatch.setattr(memory, "_get_memory_service", lambda: service)
    command, args = (
        (memory.list_memories, ["--scope", "global"])
        if surface == "list"
        else (memory.show, ["inspect-note", "--scope", "global"])
    )
    result = CliRunner().invoke(command, args)
    assert result.exit_code == 0, result.output
    row = _row(db_engine, "inspect-note")
    assert row.access_count == int(not enabled)
    assert (row.last_accessed_at is None) == enabled


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("surface", ["list", "show"])
def test_get_memory_routes_non_accessing_when_on(
    inspection, monkeypatch, db_engine, enabled, surface
):
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    service, _, _ = inspection
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", str(enabled).lower())
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)
    response = TestClient(main.app, base_url="http://localhost").get(
        "/memory" if surface == "list" else "/memory/inspect-note", params={"scope": "global"}
    )
    assert response.status_code == 200, response.text
    row = _row(db_engine, "inspect-note")
    assert row.access_count == int(not enabled)
    assert (row.last_accessed_at is None) == enabled


def test_mcp_recall_still_counts_when_on(inspection, monkeypatch, db_engine):
    from cli_agent_orchestrator.mcp_server import server

    service, _, _ = inspection
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", "true")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_service.MemoryService", lambda *a, **kw: service
    )
    tool = getattr(server.memory_recall, "fn", server.memory_recall)
    result = _run(
        tool(
            query="inspect-note",
            scope="global",
            memory_type=None,
            limit=10,
            sort_by="recency",
            include_related=False,
        )
    )
    assert result["success"], result
    assert _row(db_engine, "inspect-note").access_count == 1


def test_internal_recall_still_counts_when_on(inspection, monkeypatch, db_engine):
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    service, _, _ = inspection
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", "true")
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)
    response = TestClient(main.app, base_url="http://localhost").post(
        "/internal/memory/recall", json={"query": "inspect-note", "scope": "global"}
    )
    assert response.status_code == 200, response.text
    assert _row(db_engine, "inspect-note").access_count == 1


def test_promotion_plan_unchanged_by_inspection_when_on(
    inspection, monkeypatch, tmp_path, db_engine
):
    from cli_agent_orchestrator.cli.commands import memory
    from cli_agent_orchestrator.services.promotion_service import PromotionService

    service, factory, ctx = inspection
    _run(
        service.store(
            key="lesson",
            content="Prefer pytest for tests.",
            scope="agent",
            memory_type="feedback",
            terminal_context=ctx,
        )
    )
    profile = tmp_path / "developer.md"
    profile.write_text("---\nname: developer\n---\n# Developer\n")
    promotion = PromotionService(memory_base_dir=service.base_dir, db_engine=db_engine)
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", "true")
    monkeypatch.setattr(memory, "_get_memory_service", lambda: service)
    before = promotion.plan(agent_profile="developer", profile_path=profile)
    assert before.candidates == []
    for _ in range(3):
        with factory() as db:
            db.query(MemoryMetadataModel).filter_by(key="lesson").update(
                {"last_accessed_at": datetime.now(timezone.utc) - timedelta(seconds=61)}
            )
            db.commit()
        result = CliRunner().invoke(memory.list_memories, ["--scope", "agent", "--all"])
        assert result.exit_code == 0, result.output
        assert "lesson" in result.output
    after = promotion.plan(agent_profile="developer", profile_path=profile)
    assert after == before
    with factory() as db:
        assert db.query(MemoryMetadataModel).filter_by(key="lesson").one().access_count == 0


def test_non_accessing_inspection_setting_precedence(monkeypatch):
    from cli_agent_orchestrator.services.config_service import ConfigService, MemoryConfig

    monkeypatch.delenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", raising=False)
    assert settings_service.is_non_accessing_inspection_enabled() is False
    assert MemoryConfig().non_accessing_inspection is False
    ConfigService.set("memory.non_accessing_inspection", True)
    assert ConfigService.get("memory.non_accessing_inspection") is True
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", "false")
    assert settings_service.is_non_accessing_inspection_enabled() is False
    monkeypatch.setenv("CAO_MEMORY_NON_ACCESSING_INSPECTION", "true")
    ConfigService.set("memory.non_accessing_inspection", False)
    assert ConfigService.get("memory.non_accessing_inspection") is True


def test_non_accessing_read_error_is_off(monkeypatch):
    def broken():
        raise OSError("unavailable")

    monkeypatch.setattr(settings_service, "get_memory_settings", broken)
    assert settings_service.is_non_accessing_inspection_enabled() is False
