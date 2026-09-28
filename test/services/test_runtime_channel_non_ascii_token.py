"""The runtime-channel handshake rejects a non-ASCII token cleanly.

``hmac.compare_digest`` on ``str`` raises ``TypeError`` for a non-ASCII operand,
so a hostile header would crash the handshake instead of closing 1008. The
endpoint is driven directly with a stub WebSocket: before the fix the coroutine
raises TypeError; after it, it closes with the policy-violation code.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fastapi import status


def _reset_token_cache():
    import cli_agent_orchestrator.utils.runtime_token as rt

    rt._reset_cache_for_tests()


def test_non_ascii_token_closes_1008_without_a_typeerror(monkeypatch):
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
    _reset_token_cache()

    from cli_agent_orchestrator.runtime_channel.api import runtime_channel

    ws = MagicMock()
    ws.headers = {"x-cao-runtime-token": "tökén-nön-ascii"}
    ws.close = AsyncMock()
    ws.accept = AsyncMock()

    # Must not raise TypeError; must close before accepting.
    asyncio.run(runtime_channel(ws))

    ws.accept.assert_not_called()
    ws.close.assert_awaited_once()
    _, kwargs = ws.close.call_args
    assert kwargs.get("code") == status.WS_1008_POLICY_VIOLATION
    _reset_token_cache()
