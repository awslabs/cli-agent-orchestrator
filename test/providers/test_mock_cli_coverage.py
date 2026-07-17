"""Coverage for MockCliProvider.initialize() + cleanup().

The existing unit suite covers get_status / extraction / contract surfaces;
this fills the async ``initialize`` branches (success, shell timeout, status
timeout) and the no-op ``cleanup`` by mocking the tmux backend + waiters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import cli_agent_orchestrator.providers.mock_cli as mock_cli_mod
from cli_agent_orchestrator.providers.mock_cli import MockCliProvider


@pytest.mark.asyncio
async def test_initialize_success(monkeypatch) -> None:
    monkeypatch.setattr(mock_cli_mod, "wait_for_shell", AsyncMock(return_value=True))
    monkeypatch.setattr(mock_cli_mod, "wait_until_status", AsyncMock(return_value=True))
    backend = MagicMock()
    monkeypatch.setattr(mock_cli_mod, "get_backend", lambda: backend)

    provider = MockCliProvider("t1", "sess", "win", delay_ms=1)
    assert await provider.initialize() is True
    assert provider._initialized is True
    backend.send_keys.assert_called_once()
    # The launch command carries the configured delay.
    sent_cmd = backend.send_keys.call_args[0][2]
    assert "mock_cli" in sent_cmd and "--delay-ms" in sent_cmd


@pytest.mark.asyncio
async def test_initialize_raises_on_shell_timeout(monkeypatch) -> None:
    monkeypatch.setattr(mock_cli_mod, "wait_for_shell", AsyncMock(return_value=False))
    monkeypatch.setattr(mock_cli_mod, "get_backend", lambda: MagicMock())
    provider = MockCliProvider("t1", "sess", "win")
    with pytest.raises(TimeoutError, match="Shell initialization"):
        await provider.initialize()


@pytest.mark.asyncio
async def test_initialize_raises_on_status_timeout(monkeypatch) -> None:
    monkeypatch.setattr(mock_cli_mod, "wait_for_shell", AsyncMock(return_value=True))
    monkeypatch.setattr(mock_cli_mod, "wait_until_status", AsyncMock(return_value=False))
    monkeypatch.setattr(mock_cli_mod, "get_backend", lambda: MagicMock())
    provider = MockCliProvider("t1", "sess", "win")
    with pytest.raises(TimeoutError, match="mock_cli initialization"):
        await provider.initialize()


def test_cleanup_is_noop() -> None:
    provider = MockCliProvider("t1", "sess", "win")
    assert provider.cleanup() is None
