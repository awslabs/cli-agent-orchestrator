"""Local-default and remote-client tests for distributed CAO memory."""

from unittest.mock import Mock, patch

import pytest

from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services import memory_gateway


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
    response = Mock()
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
