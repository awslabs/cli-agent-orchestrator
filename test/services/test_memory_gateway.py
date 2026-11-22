"""Local-default and remote-client tests for distributed CAO memory."""

from unittest.mock import Mock, patch

import pytest
import requests
from sqlalchemy.exc import OperationalError

from cli_agent_orchestrator.models.memory import Memory, mcp_recall_wire_dict
from cli_agent_orchestrator.services import memory_gateway
from cli_agent_orchestrator.services.memory_service import (
    ForgetResult,
    MemoryAmbiguousScopeError,
    MemoryPartialWriteError,
)


def _memory() -> Memory:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Memory(
        id="m1",
        key="shared-fact",
        memory_type="project",
        scope="project",
        scope_id="project-1",
        file_path="/memory/shared-fact.md",
        created_at=now,
        updated_at=now,
        content="Shared across workers.",
    )


def test_remote_memory_is_opt_in(monkeypatch):
    monkeypatch.delenv("CAO_MEMORY_API_URL", raising=False)
    assert memory_gateway.remote_memory_url() is None


def test_remote_memory_url_normalized(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://cao-supervisor:9889/")
    assert memory_gateway.remote_memory_url() == "http://cao-supervisor:9889"


def test_remote_context_propagates_local_terminal_lookup_failure(monkeypatch):
    from cli_agent_orchestrator.clients import database

    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    def locked_session():
        raise OperationalError("SELECT terminals", {}, Exception("database is locked"))

    monkeypatch.setattr(database, "SessionLocal", locked_session)
    post = Mock()
    monkeypatch.setattr(memory_gateway, "_post", post)

    with pytest.raises(RuntimeError, match="Could not get terminal context"):
        memory_gateway.memory_context_for_terminal("worker")

    post.assert_not_called()


@pytest.mark.asyncio
async def test_remote_store_serializes_context(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    stored = _memory()
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "memory": stored.model_dump(mode="json"),
        "action": "created",
    }
    with patch.object(memory_gateway.requests, "post", return_value=response) as post:
        result = await memory_gateway.store_memory(
            content=stored.content,
            scope="project",
            memory_type="project",
            key=stored.key,
            tags="shared",
            terminal_context={"terminal_id": "abc12345", "cwd": "/workspace/repo"},
        )
    assert result.key == "shared-fact"
    assert result.action == "created"
    assert post.call_args.args[0] == "http://memory-owner:9889/internal/memory/store"
    assert post.call_args.kwargs["json"]["terminal_context"]["terminal_id"] == "abc12345"


@pytest.mark.asyncio
async def test_remote_forget_reconstructs_typed_result(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "deleted": ForgetResult("deindexed", "vault", "CAO/topic.md").__dict__
    }
    with patch.object(memory_gateway.requests, "post", return_value=response):
        result = await memory_gateway.forget_memory(
            key="topic",
            scope="project",
            terminal_context=None,
        )

    assert result.action == "deindexed"
    assert result.path == "CAO/topic.md"
    assert bool(result) is True


@pytest.mark.asyncio
async def test_remote_recall_omits_none_fields(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    post = Mock(return_value={"memories": []})
    monkeypatch.setattr(memory_gateway, "_post", post)

    result = await memory_gateway.recall_memory(
        query=None,
        scope=None,
        memory_type=None,
        limit=10,
        terminal_context=None,
        search_mode=None,
        sort_by=None,
        include_related=False,
        detail="snippet",
    )

    assert result == []
    assert post.call_args.args == (
        "/internal/memory/recall",
        {"limit": 10, "include_related": False, "detail": "snippet"},
    )


@pytest.mark.asyncio
async def test_remote_snippet_falls_back_to_content_from_older_owner(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    memory = _memory()
    monkeypatch.setattr(
        memory_gateway,
        "_post",
        Mock(return_value={"memories": [memory.model_dump(mode="json")]}),
    )

    recalled = await memory_gateway.recall_memory(
        query="shared",
        scope="project",
        memory_type=None,
        limit=10,
        terminal_context=None,
        search_mode="metadata",
        sort_by=None,
        include_related=False,
        detail="snippet",
    )
    wire = mcp_recall_wire_dict(recalled[0], detail="snippet")

    assert wire["snippet"] == "Shared across workers."


@pytest.mark.asyncio
async def test_remote_memory_get_parity(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    post = Mock(
        return_value={
            "memory": {
                "key": "shared-fact",
                "scope": "global",
                "content": "Complete body.",
                "status": "active",
                "earlier_entries": 0,
                "links_in": [],
                "links_out": [],
            }
        }
    )
    monkeypatch.setattr(memory_gateway, "_post", post)

    result = await memory_gateway.get_memory(
        key="shared-fact",
        scope="global",
        include_history=True,
        max_chars=1234,
        terminal_context={"terminal_id": "fixture-terminal"},
    )

    assert result["content"] == "Complete body."
    assert post.call_args.args == (
        "/internal/memory/get",
        {
            "key": "shared-fact",
            "scope": "global",
            "include_history": True,
            "max_chars": 1234,
            "terminal_context": {"terminal_id": "fixture-terminal"},
        },
    )


@pytest.mark.asyncio
async def test_remote_memory_get_preserves_key_in_ambiguity_error(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    response = Mock(status_code=409)
    response.json.return_value = {
        "error_kind": "ambiguous_scope",
        "error": "memory key 'shared-fact' exists in scopes",
        "scopes": ["global", "federated"],
    }

    with (
        patch.object(memory_gateway.requests, "post", return_value=response),
        pytest.raises(MemoryAmbiguousScopeError, match="shared-fact"),
    ):
        await memory_gateway.get_memory(
            key="shared-fact",
            scope=None,
            include_history=False,
            max_chars=8000,
            terminal_context=None,
        )


@pytest.mark.asyncio
async def test_remote_store_reconstructs_partial_write_error(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(memory_gateway.asyncio, "to_thread", run_inline)
    response = Mock(status_code=500)
    response.json.return_value = {
        "error_kind": "memory_metadata_partial_write",
        "error": "metadata failed after durable writes",
        "partial_write": {
            "key": "shared-fact",
            "scope": "project",
            "scope_id": "project-1",
            "file_path": "/memory/project-1/wiki/project/shared-fact.md",
            "completed_phases": ["wiki", "index"],
            "repair_command": "cao memory repair --apply",
        },
    }

    with (
        patch.object(memory_gateway.requests, "post", return_value=response),
        pytest.raises(MemoryPartialWriteError) as caught,
    ):
        await memory_gateway.store_memory(
            content="already durable",
            scope="project",
            memory_type="project",
            key="shared-fact",
            tags="",
            terminal_context={"cwd": "/workspace/repo"},
        )

    assert caught.value.key == "shared-fact"
    assert caught.value.scope_id == "project-1"
    assert caught.value.completed_phases == ["wiki", "index"]
    response.raise_for_status.assert_not_called()


def test_remote_non_typed_error_still_raises_http_error(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner:9889")
    response = Mock(status_code=503)
    response.json.return_value = {"detail": "unavailable"}
    response.raise_for_status.side_effect = requests.HTTPError("503")

    with (
        patch.object(memory_gateway.requests, "post", return_value=response),
        pytest.raises(requests.HTTPError, match="503"),
    ):
        memory_gateway._post("/internal/memory/store", {})


def test_remote_memory_uses_elastic_gateway_credentials(monkeypatch):
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://cao-worker-broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_WORKER_ID", "deadbeef")
    monkeypatch.setenv("CAO_ELASTIC_RELEASE_TOKEN", "release-token")
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"context": "shared"}

    with patch.object(memory_gateway.requests, "post", return_value=response) as post:
        result = memory_gateway._post("/internal/memory/context", {})

    assert result == {"context": "shared"}
    assert post.call_args.kwargs["headers"] == {
        "X-CAO-Worker-ID": "deadbeef",
        "X-CAO-Release-Token": "release-token",
    }
