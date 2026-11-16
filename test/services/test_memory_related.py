"""The content-free memory_related read surface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services import memory_eligibility
from cli_agent_orchestrator.services.memory_service import MemoryService


def _run(coro):
    return asyncio.run(coro)


class _ExcludeTarget:
    name = "fixture.related-policy"

    def is_enabled(self):
        return True

    def excluded(self, request, candidates):
        return {identity: "fixture" for identity in candidates if identity.key == "target"}


@pytest.fixture
def service(tmp_path: Path, isolated_memory_db, monkeypatch) -> MemoryService:
    monkeypatch.setattr(memory_eligibility, "_SOURCES", {})
    return MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)


def test_related_shape_is_content_free_and_policy_filters_neighbours(service) -> None:
    _run(service.store("secret target body", scope="global", key="target"))
    _run(service.store("Source hook. [[target]]", scope="global", key="source"))

    result = _run(
        service.related(
            "source",
            scope="global",
            terminal_context={"agent_profile": "memory_manager"},
        )
    )
    assert result["success"] is True
    assert result["edges"] == [
        {
            "source_key": "source",
            "target_key": "target",
            "type": "relates_to",
            "origin": "content_link",
            "status": "active",
            "confidence": None,
            "direction": "out",
            "depth": 1,
        }
    ]
    assert result["neighbours"] == [
        {
            "key": "target",
            "kind": "project",
            "hook": "secret target body",
            "status": "active",
        }
    ]
    assert "content" not in result
    assert "proposals" not in result

    memory_eligibility.register_source(_ExcludeTarget())
    filtered = _run(
        service.related(
            "source",
            scope="global",
            terminal_context={"agent_profile": "memory_manager"},
        )
    )
    assert filtered["edges"] == []
    assert filtered["neighbours"] == []


def test_related_not_found_and_ambiguous_scope(service) -> None:
    assert _run(service.related("missing")) == {
        "success": False,
        "error_kind": "not_found",
        "error": "memory not found: missing",
    }
    _run(service.store("global", scope="global", key="same-key"))
    _run(service.store("federated", scope="federated", key="same-key"))

    ambiguous = _run(service.related("same-key"))
    assert ambiguous == {
        "success": False,
        "error_kind": "ambiguous_scope",
        "error": "memory key is present in multiple scopes: same-key",
        "scopes": ["global", "federated"],
    }


def test_memory_related_tool_calls_local_service(monkeypatch) -> None:
    expected = {"success": True, "key": "source", "edges": [], "neighbours": []}
    related = monkeypatch.setattr
    related(MemoryService, "related", lambda self, *args, **kwargs: _async_result(expected))
    related(server, "_get_terminal_context_from_env", lambda: None)

    tool = getattr(server.memory_related, "fn", server.memory_related)
    assert _run(tool("source")) == expected


async def _async_result(value):
    return value
