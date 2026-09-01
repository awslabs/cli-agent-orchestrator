"""Local-default and remote-client tests for distributed CAO memory."""

from unittest.mock import Mock, patch

import pytest
import requests

from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services import memory_gateway
from cli_agent_orchestrator.services.memory_service import MemoryPartialWriteError


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
