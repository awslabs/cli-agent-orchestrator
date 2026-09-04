"""Turning on CAO_MEMORY_API_URL must not reject keys that work without it.

The MCP memory tools have always let ``MemoryService._sanitize_key`` normalize the
key: ``memory_store(key="Prefer Pytest")`` stores ``preferpytest`` when running
in-process. The ``/internal/memory/*`` routes that back the gateway validated the
wire key as the strict ``MemoryKey`` (``^[a-z0-9-]{1,60}$``), so the identical
call 422'd as soon as the gateway was configured — a silent break for any caller
whose keys were not already slugs.

These pin the normalize-don't-reject contract on the wire, and that the operator
routes keep rejecting (they mirror the CLI rule, where a bad key should be a
visible error rather than a quiet rename).
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.memory import Memory

FACTORY_TARGET = "cli_agent_orchestrator.api.main._get_memory_service"
MEMORY_ENABLED = "cli_agent_orchestrator.services.settings_service.is_memory_enabled"

CONTEXT = {
    "terminal_id": "abc12345",
    "session_name": "sess-1",
    "provider": "kiro_cli",
    "agent_profile": "developer",
    "cwd": "/repo",
}


def _memory(key: str) -> Memory:
    return Memory(
        id=f"agent:{key}",
        key=key,
        memory_type="reference",
        scope="agent",
        scope_id="developer",
        file_path=f"/mem/{key}.md",
        tags="",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        content="body",
        action="created",
    )


@pytest.fixture
def svc():
    service = MagicMock()
    service.base_dir = Path("/mem")
    # Mirrors _sanitize_key: lowercase, strip non-slug characters.
    service.store = AsyncMock(return_value=_memory("preferpytest"))
    service.forget = AsyncMock(return_value=True)
    with patch(FACTORY_TARGET, return_value=service), patch(MEMORY_ENABLED, return_value=True):
        yield service


class TestInternalStoreKeyCharset:
    def test_unsanitized_key_is_accepted_and_normalized(self, client, svc):
        response = client.post(
            "/internal/memory/store",
            json={
                "content": "x",
                "scope": "agent",
                "memory_type": "reference",
                "key": "Prefer Pytest",
                "terminal_context": CONTEXT,
            },
        )
        assert response.status_code == 200, response.text
        # The service receives the raw key and normalizes it, as in-process.
        assert svc.store.await_args.kwargs["key"] == "Prefer Pytest"
        assert response.json()["memory"]["key"] == "preferpytest"

    def test_slug_key_still_works(self, client, svc):
        svc.store.return_value = _memory("prefer-pytest")
        response = client.post(
            "/internal/memory/store",
            json={"content": "x", "scope": "agent", "key": "prefer-pytest"},
        )
        assert response.status_code == 200, response.text

    def test_control_characters_are_still_rejected(self, client, svc):
        response = client.post(
            "/internal/memory/store",
            json={"content": "x", "scope": "agent", "key": "bad\nkey"},
        )
        assert response.status_code == 422

    def test_empty_key_is_rejected(self, client, svc):
        response = client.post(
            "/internal/memory/store", json={"content": "x", "scope": "agent", "key": ""}
        )
        assert response.status_code == 422


class TestInternalForgetKeyCharset:
    def test_unsanitized_key_is_accepted(self, client, svc):
        response = client.post(
            "/internal/memory/forget",
            json={"key": "Prefer Pytest", "scope": "agent", "terminal_context": CONTEXT},
        )
        assert response.status_code == 200, response.text
        assert svc.forget.await_args.kwargs["key"] == "Prefer Pytest"


class TestOperatorRoutesStayStrict:
    """Contrast pin: the operator surface still rejects, mirroring the CLI."""

    def test_operator_delete_rejects_an_unsanitized_key(self, client, svc):
        response = client.delete("/memory/Prefer%20Pytest", params={"scope": "global"})
        assert response.status_code == 422

    def test_operator_get_rejects_an_unsanitized_key(self, client, svc):
        response = client.get("/memory/Prefer%20Pytest", params={"scope": "global"})
        assert response.status_code == 422
