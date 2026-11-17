"""Auto-supersede and memory_restore across MCP, the internal API and the gateway (#1335).

The services are real: a MemoryService on per-test files and SQLite. Gateway
parity routes the remote client's HTTP call into the real FastAPI app.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services import (
    audit_log,
    memory_gateway,
    memory_relationship_service,
)
from cli_agent_orchestrator.services import memory_service as memory_service_module
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService

FACTORY_TARGET = "cli_agent_orchestrator.api.main._get_memory_service"
STALE = "Retired revision one says the cobalt bridge opens at dawn."
CURRENT = "Current revision one says the cobalt bridge opens at noon. Replaces [[bridge-stale]]."
SUPERSEDED = [
    {
        "key": "bridge-stale",
        "scope": "global",
        "reason": "similar",
        "undo": "memory_restore(key='bridge-stale', scope='global')",
    }
]


def _run(coro):
    return asyncio.run(coro)


def _tool(name: str):
    tool = getattr(server, name)
    return getattr(tool, "fn", tool)


@pytest.fixture
def service(tmp_path: Path, isolated_memory_db, monkeypatch) -> MemoryService:
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: True)
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", tmp_path / "audit")
    # The MCP tools construct MemoryService() themselves: point its default
    # base at this test's directory; isolated_memory_db routes its sessions.
    monkeypatch.setattr(memory_service_module, "MEMORY_BASE_DIR", tmp_path / "memory")
    monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: None)
    monkeypatch.setattr(
        memory_relationship_service, "SessionLocal", sessionmaker(bind=isolated_memory_db)
    )
    return MemoryService()


@pytest.fixture
def remote(client, service, monkeypatch):
    """Route the gateway's HTTP POST into the real app backed by ``service``."""
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway, "_post", post)
    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    with patch(FACTORY_TARGET, return_value=service):
        yield


def test_mcp_store_reports_superseded_and_restore_undoes_it(service) -> None:
    store = _tool("memory_store")
    first = _run(
        store(content=STALE, scope="global", memory_type="project", key="bridge-stale", tags=None)
    )
    assert "superseded" not in first
    second = _run(
        store(
            content=CURRENT, scope="global", memory_type="project", key="bridge-current", tags=None
        )
    )
    assert second["superseded"] == SUPERSEDED

    recall = _tool("memory_recall")
    common = dict(scope="global", memory_type=None, limit=10, search_mode=None, sort_by=None)
    hidden = _run(recall(query="cobalt bridge", include_related=False, **common))
    assert [m["key"] for m in hidden["memories"]] == ["bridge-current"]
    listed = _run(
        recall(query="cobalt bridge", include_related=False, include_superseded=True, **common)
    )
    assert {m["key"]: m.get("lifecycle_status") for m in listed["memories"]} == {
        "bridge-current": None,
        "bridge-stale": "superseded",
    }

    restored = _run(_tool("memory_restore")(key="bridge-stale", scope="global"))
    assert restored == {
        "success": True,
        "key": "bridge-stale",
        "scope": "global",
        "restored": "superseded_note",
        "superseded_by": ["bridge-current"],
    }
    again = _run(recall(query="cobalt bridge", include_related=False, **common))
    assert sorted(m["key"] for m in again["memories"]) == ["bridge-current", "bridge-stale"]


def test_mcp_restore_reports_errors(service) -> None:
    result = _run(_tool("memory_restore")(key="missing-note", scope="global"))
    assert result == {
        "success": False,
        "error_kind": "not_found",
        "error": "memory not found: missing-note",
    }


def test_gateway_parity_for_store_recall_and_restore(service, remote) -> None:
    local_first = _run(
        memory_gateway.store_memory(
            content=STALE,
            scope="global",
            memory_type="project",
            key="bridge-stale",
            tags="",
            terminal_context=None,
        )
    )
    assert local_first.superseded == []
    second = _run(
        memory_gateway.store_memory(
            content=CURRENT,
            scope="global",
            memory_type="project",
            key="bridge-current",
            tags="",
            terminal_context=None,
        )
    )
    assert [item.model_dump() for item in second.superseded] == SUPERSEDED

    recall_args: dict[str, Any] = dict(
        query="cobalt bridge",
        scope="global",
        memory_type=None,
        limit=10,
        terminal_context=None,
        search_mode=None,
        sort_by=None,
        include_related=False,
    )
    hidden = _run(memory_gateway.recall_memory(**recall_args))
    assert [m.key for m in hidden] == ["bridge-current"]
    listed = _run(memory_gateway.recall_memory(**recall_args, include_superseded=True))
    assert sorted(m.key for m in listed) == ["bridge-current", "bridge-stale"]

    restored = _run(
        memory_gateway.restore_memory(key="bridge-stale", scope="global", terminal_context=None)
    )
    assert restored == {
        "success": True,
        "key": "bridge-stale",
        "scope": "global",
        "restored": "superseded_note",
        "superseded_by": ["bridge-current"],
    }
    assert sorted(m.key for m in _run(memory_gateway.recall_memory(**recall_args))) == [
        "bridge-current",
        "bridge-stale",
    ]


def test_gateway_recall_body_omits_include_superseded_by_default(monkeypatch) -> None:
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")
    seen: list[tuple[str, dict[str, Any]]] = []

    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append((path, body))
        return {"memories": []}

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway, "_post", post)
    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    args: dict[str, Any] = dict(
        query=None,
        scope=None,
        memory_type=None,
        limit=10,
        terminal_context=None,
        search_mode=None,
        sort_by=None,
        include_related=False,
    )
    _run(memory_gateway.recall_memory(**args))
    _run(memory_gateway.recall_memory(**args, include_superseded=True))
    assert seen == [
        ("/internal/memory/recall", {"limit": 10, "include_related": False}),
        (
            "/internal/memory/recall",
            {"limit": 10, "include_related": False, "include_superseded": True},
        ),
    ]
