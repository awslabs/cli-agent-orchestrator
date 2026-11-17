"""Visible key truncation and long-link resolution regressions (#1329 P1c)."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.wiki_lint import _detect_graph_density


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def stack(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    return MemoryService(base_dir=tmp_path / "memory", db_engine=engine), engine


def _install_service_factory(monkeypatch, service):
    class ServiceFactory:
        _key_was_truncated = staticmethod(MemoryService._key_was_truncated)

        def __new__(cls):
            return service

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_service.MemoryService", ServiceFactory
    )


def test_mcp_store_reports_when_requested_key_was_truncated(stack, monkeypatch):
    from cli_agent_orchestrator.mcp_server import server

    service, _ = stack
    requested = "a" * 70
    _install_service_factory(monkeypatch, service)
    tool = getattr(server.memory_store, "fn", server.memory_store)

    result = _run(
        tool(
            content="Synthetic long-key regression.",
            scope="global",
            memory_type="project",
            key=requested,
            tags="",
        )
    )

    assert result["success"] is True, result
    assert tuple(result) == (
        "success",
        "key",
        "scope",
        "scope_id",
        "file_path",
        "action",
        "key_truncated",
        "requested_key",
    )
    assert result["key"] == requested[:60]
    assert result["key_truncated"] is True
    assert result["requested_key"] == requested


def test_mcp_store_does_not_widen_ordinary_result(stack, monkeypatch):
    from cli_agent_orchestrator.mcp_server import server

    service, _ = stack
    _install_service_factory(monkeypatch, service)
    tool = getattr(server.memory_store, "fn", server.memory_store)

    result = _run(
        tool(
            content="Synthetic ordinary-key regression.",
            scope="global",
            memory_type="project",
            key="ordinary-key",
            tags="",
        )
    )

    assert result["success"] is True, result
    assert tuple(result) == (
        "success",
        "key",
        "scope",
        "scope_id",
        "file_path",
        "action",
    )


def test_full_long_related_key_resolves_to_truncated_topic(stack):
    service, engine = stack
    requested = "linked-" + "b" * 64
    stored = _run(
        service.store(
            content="Synthetic target.",
            scope="global",
            key=requested,
        )
    )
    _run(
        service.store(
            content="Synthetic source.",
            scope="global",
            key="source",
        )
    )
    with sessionmaker(bind=engine)() as db:
        source = db.query(MemoryMetadataModel).filter_by(key="source").one()
        source.related_keys = requested
        db.commit()

    recalled = _run(
        service.recall(
            query="source",
            scope="global",
            include_related=True,
            record_access=False,
        )
    )

    assert [memory.key for memory in recalled] == ["source", stored.key]
    assert recalled[1].is_related is True


def test_lint_reports_link_that_only_resolves_after_truncation():
    requested = "linked-" + "b" * 64
    truncated = requested[:60]

    issues = _detect_graph_density(
        {
            ("global", None): [
                {"key": "source", "related_keys": requested},
                {"key": truncated, "related_keys": None},
            ]
        }
    )

    assert any(
        issue.issue_type == "truncated_link"
        and issue.key == "source"
        and issue.related_key == truncated
        for issue in issues
    )
