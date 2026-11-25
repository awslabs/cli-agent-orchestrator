"""MCP and remote restore carry the exact version from the store hint."""

import asyncio
import re
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


def test_http_clear_removes_hidden_legacy_target(service, client):
    run(service.store("Original.", key="old", scope="global"))
    run(service.store("New.", key="new", scope="global"))
    service._relationship_service().create(
        "global", None, "new", "old", "supersedes", "auto_supersede"
    )
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        listed = client.get("/memory", params={"scope": "global", "include_superseded": True})
        assert listed.status_code == 200, listed.text
        assert {memory["key"]: memory.get("lifecycle_status") for memory in listed.json()} == {
            "new": None,
            "old": "superseded",
        }
        result = client.delete("/memory", params={"scope": "global"})
        assert result.status_code == 200, result.text
        assert result.json()["deleted_count"] == 2
    assert run(service.recall(scope="global", include_superseded=True)) == []


def test_default_off_operator_list_has_main_wire_shape(service, client, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    run(service.store("Original.", key="relay", scope="global"))
    with patch("cli_agent_orchestrator.api.main._get_memory_service", return_value=service):
        response = client.get("/memory", params={"scope": "global"})
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    assert set(response.json()[0]) == {
        "key",
        "scope",
        "scope_id",
        "memory_type",
        "tags",
        "created_at",
        "updated_at",
        "source_kind",
        "source_path",
        "indexed_at",
        "index_freshness",
        "content_truncated",
    }
