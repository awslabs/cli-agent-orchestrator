"""Partial-write contract tests for the memory service and MCP boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.memory_service import (
    MemoryPartialWriteError,
    MemoryService,
)


def test_store_raises_typed_error_after_durable_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    service = MemoryService(tmp_path / "memory", engine)

    def fail_upsert(**kwargs) -> None:
        del kwargs
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(service, "_upsert_metadata", fail_upsert)

    with pytest.raises(MemoryPartialWriteError) as caught:
        asyncio.run(
            service.store(
                content="durable content must survive",
                scope="agent",
                memory_type="reference",
                key="partial-topic",
                terminal_context={"agent_profile": "developer"},
            )
        )

    error = caught.value
    assert error.error_kind == "memory_metadata_partial_write"
    assert error.key == "partial-topic"
    assert error.scope == "agent"
    assert error.scope_id == "developer"
    assert error.completed_phases == ["wiki", "index"]
    assert error.repair_command == "cao memory repair --apply"
    assert "durable content must survive" not in str(error)
    topic = Path(error.file_path)
    assert "durable content must survive" in topic.read_text(encoding="utf-8")
    assert "[partial-topic]" in service.get_index_path("agent", "developer").read_text(
        encoding="utf-8"
    )
    with sessionmaker(bind=engine)() as db:
        assert db.query(MemoryMetadataModel).count() == 0


def test_mcp_memory_store_serializes_partial_write_envelope() -> None:
    from cli_agent_orchestrator.mcp_server.server import memory_store

    error = MemoryPartialWriteError(
        key="partial-topic",
        scope="global",
        scope_id=None,
        file_path="/safe/memory/global/wiki/global/partial-topic.md",
    )
    fake_service = AsyncMock()
    fake_service.store.side_effect = error

    with (
        patch(
            "cli_agent_orchestrator.services.memory_service.MemoryService",
            return_value=fake_service,
        ),
        patch(
            "cli_agent_orchestrator.mcp_server.server._get_terminal_context_from_env",
            return_value=None,
        ),
    ):
        result = asyncio.run(
            memory_store(
                content="not serialized",
                scope="global",
                memory_type="reference",
                key="partial-topic",
                tags=None,
            )
        )

    assert result == {
        "success": False,
        "error_kind": "memory_metadata_partial_write",
        "error": str(error),
        "partial_write": {
            "key": "partial-topic",
            "scope": "global",
            "scope_id": None,
            "file_path": "/safe/memory/global/wiki/global/partial-topic.md",
            "completed_phases": ["wiki", "index"],
            "repair_command": "cao memory repair --apply",
        },
    }
