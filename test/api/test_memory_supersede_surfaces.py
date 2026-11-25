"""MCP and remote restore carry the exact version from the store hint."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services import audit_log, memory_gateway
from cli_agent_orchestrator.services import memory_service as memory_service_module
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService


def run(coroutine):
    return asyncio.run(coroutine)


def tool(name):
    candidate = getattr(server, name)
    return getattr(candidate, "fn", candidate)


@pytest.mark.parametrize("enabled", [False, True])
def test_restore_registration_uses_startup_setting(enabled):
    environment = os.environ.copy()
    environment["CAO_MEMORY_SUPERSEDE_AUTO"] = "1" if enabled else "0"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from cli_agent_orchestrator.mcp_server import server; print('memory_restore' in {tool.name for tool in asyncio.run(server.mcp._list_tools())})",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == str(enabled)


@pytest.mark.parametrize("enabled", [False, True])
def test_restore_registration_uses_startup_setting(enabled):
    environment = os.environ.copy()
    environment["CAO_MEMORY_SUPERSEDE_AUTO"] = "1" if enabled else "0"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from cli_agent_orchestrator.mcp_server import server; print('memory_restore' in {tool.name for tool in asyncio.run(server.mcp._list_tools())})",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == str(enabled)


@pytest.fixture
def service(tmp_path: Path, isolated_memory_db, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: True)
    monkeypatch.setattr(settings_service, "get_compile_mode", lambda: "append")
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", tmp_path / "audit")
    monkeypatch.setattr(memory_service_module, "MEMORY_BASE_DIR", tmp_path / "memory")
    monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: None)
    return MemoryService(db_engine=isolated_memory_db)


def test_mcp_restore_idempotent(service):
    store = tool("memory_store")
    run(store(content="Original.", key="relay", scope="global", memory_type="project", tags=None))
    revised = run(
        store(content="Revised.", key="relay", scope="global", memory_type="project", tags=None)
    )
    version = re.search(r"version='([^']+)'", revised["superseded"][0]["undo"]).group(1)
    assert (
        run(tool("memory_restore")(key="relay", scope="global", version=version))["restored"]
        == "previous_version"
    )
    assert (
        run(tool("memory_restore")(key="relay", scope="global", version=version))["restored"]
        == "already_restored"
    )


def test_partial_store_exposes_restore_hint_on_mcp_and_http(service, client, monkeypatch):
    run(service.store("A.", key="relay", scope="global"))

    def broken_upsert(**kwargs):
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(MemoryService, "_upsert_metadata", broken_upsert)
    mcp_result = run(
        tool("memory_store")(
            content="B.", key="relay", scope="global", memory_type="project", tags=None
        )
    )
    assert "version=" in mcp_result["partial_write"]["superseded"][0]["undo"]
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        response = client.post(
            "/internal/memory/store", json={"content": "C.", "key": "relay", "scope": "global"}
        )
    assert "version=" in response.json()["partial_write"]["superseded"][0]["undo"]


def test_gateway_forwards_version(service, client, monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    def post(path, body):
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway, "_post", post)
    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", inline)
    run(service.store("Original.", key="relay", scope="global"))
    revised = run(service.store("Revised.", key="relay", scope="global"))
    version = re.search(r"version='([^']+)'", revised.superseded[0].undo).group(1)
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        result = run(
            memory_gateway.restore_memory(
                key="relay", scope="global", terminal_context=None, version=version
            )
        )
    assert result["restored_from"] == version


def test_http_clear_removes_all_visible_topics(service, client):
    run(service.store("Original.", key="old", scope="global"))
    run(service.store("New.", key="new", scope="global"))
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        listed = client.get("/memory", params={"scope": "global"})
        assert listed.status_code == 200, listed.text
        assert {memory["key"] for memory in listed.json()} == {"new", "old"}
        result = client.delete("/memory", params={"scope": "global"})
        assert result.status_code == 200, result.text
        assert result.json()["deleted_count"] == 2
    assert run(service.recall(scope="global")) == []


def test_default_off_operator_list_has_main_wire_shape(service, client, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    monkeypatch.setattr(settings_service, "is_memory_lifecycle_enabled", lambda: True)
    run(service.store("Original.", key="relay", scope="global"))
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        response = client.get("/memory", params={"scope": "global"})
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    memory = run(service.recall(scope="global"))[0]
    assert response.json() == [
        {
            "key": "relay",
            "scope": "global",
            "scope_id": None,
            "memory_type": "project",
            "tags": "",
            "created_at": memory.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": memory.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_kind": "native",
            "source_path": None,
            "indexed_at": None,
            "index_freshness": None,
            "content_truncated": False,
        }
    ]


def test_off_surface_hides_restore_and_matches_recall_schema(service, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    listed = {item.name: item for item in run(server.mcp._list_tools())}
    schema = listed["memory_recall"].to_mcp_tool(name="memory_recall").input_schema
    optional_text = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    assert schema == {
        "additionalProperties": False,
        "properties": {
            "query": {
                **optional_text,
                "description": "Search query matched against memory content (case-insensitive)",
            },
            "scope": {
                **optional_text,
                "description": 'Filter by scope: "global", "project", "session", "agent", "federated". Omit to search all.',
            },
            "memory_type": {
                **optional_text,
                "description": 'Filter by type: "user", "feedback", "project", "reference". Omit for all types.',
            },
            "limit": {
                "default": 10,
                "description": "Maximum number of results to return",
                "maximum": 100,
                "minimum": 1,
                "type": "integer",
            },
            "search_mode": optional_text,
            "sort_by": {
                **optional_text,
                "description": 'Ranking: "recency" (default), "score" (BM25+recency+usage), "usage", or "relevance" (lexical relevance over a widened pool; needs query, else behaves as recency). When both ranking arguments are omitted, memory.ranking.default_sort may select BM25 for query recall.',
            },
            "include_related": {
                "default": False,
                "description": "When True, expand each result's cross-references and append related articles after the primary results. Default False preserves the non-expanded recall behaviour.",
                "type": "boolean",
            },
        },
        "type": "object",
    }
    assert run(tool("memory_restore")(key="relay", scope="global", version="unused")) == {
        "success": False,
        "error_kind": "disabled",
        "error": "automatic supersede is off",
    }


def test_off_operator_list_with_lifecycle_edge_has_no_added_fields(service, client, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    monkeypatch.setattr(settings_service, "is_memory_lifecycle_enabled", lambda: True)
    run(service.store("Old.", key="old", scope="global"))
    run(service.store("New.", key="new", scope="global"))
    from cli_agent_orchestrator.clients.database import SessionLocal
    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    MemoryRelationshipService(service._db_session_factory or SessionLocal).create(
        "global", None, "new", "old", "supersedes", "human"
    )
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        response = client.get("/memory", params={"scope": "global"})
    assert response.status_code == 200
    assert {row["key"] for row in response.json()} == {"old", "new"}
    assert all("lifecycle_status" not in row for row in response.json())
