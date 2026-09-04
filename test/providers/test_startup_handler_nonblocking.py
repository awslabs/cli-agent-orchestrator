"""Shared regression test: startup/trust-prompt handlers must not block the
shared asyncio event loop.

issue #494: ``KimiCliProvider._handle_startup_dialog``,
``AntigravityCliProvider._handle_startup_dialog``, and
``CopilotCliProvider._accept_trust_prompts`` / ``_wait_for_shell_ready`` were
converted from sync ``time.sleep()``-based methods (called un-awaited from an
async ``initialize()``) into real coroutines that offload every blocking
backend call via ``asyncio.to_thread``, mirroring PR #451's fix for
``ClaudeCodeProvider._handle_startup_prompts``.

Two layers, parameterized across the four handlers:
1. Structural pin -- each target must be a real coroutine function. Catches a
   regression back to a plain ``def`` (silently breaking every
   ``await self._handle_...()`` call site).
2. Heartbeat-starvation probe -- proves the coroutine actually YIELDS the
   event loop while its backend call is in flight, not just that it is
   syntactically ``async def`` while still blocking synchronously inside (the
   bug #494 reports: "mirrors ClaudeCodeProvider" docstrings that were never
   true because the body stayed fully sync).

ClaudeCodeProvider._handle_startup_prompts is deliberately excluded from both
layers: PR #451 (which converts it) is open/changes-requested, not merged, as
of this test.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

# Simulated blocking backend latency per poll -- long enough that several
# ticker increments fit inside it if (and only if) the call is truly
# offloaded to a worker thread via asyncio.to_thread, rather than run
# directly on the event-loop thread.
_BLOCKING_CALL_SECONDS = 0.05
_TICKER_INTERVAL_SECONDS = 0.01


def _blocking_history(ready_output: str):
    """Build a side_effect that blocks the calling thread, then returns.

    Stands in for a real tmux/backend subprocess exec (get_history /
    _history). Used as a Mock's side_effect, so it runs synchronously on
    whatever thread invokes the mock -- the event-loop thread if the code
    under test forgot to offload it via asyncio.to_thread, a worker thread if
    it didn't.
    """

    def _side_effect(*_args, **_kwargs) -> str:
        time.sleep(_BLOCKING_CALL_SECONDS)
        return ready_output

    return _side_effect


async def _run_with_heartbeat_probe(handler_coro) -> int:
    """Await ``handler_coro`` concurrently with a ticker; return the tick count.

    The ticker increments a counter every ``_TICKER_INTERVAL_SECONDS`` until
    the handler completes. A non-zero count proves the event loop kept
    running other coroutines while the handler's backend call was blocking on
    a worker thread. Zero would mean the handler's "blocking" call actually
    ran on the event-loop thread and starved everything else -- the exact
    pathology issue #494 (and PR #451 before it) fixes.
    """
    ticks = 0
    stop = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(_TICKER_INTERVAL_SECONDS)
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    try:
        await handler_coro
    finally:
        stop.set()
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass
    return ticks


async def _kimi_handler_run() -> int:
    """KimiCliProvider._handle_startup_dialog: one poll, already-ready output."""
    provider = KimiCliProvider("t1", "sess", "win")
    mock_backend = MagicMock()
    mock_backend.get_history.side_effect = _blocking_history("Welcome to Kimi!\n💫")
    with (
        patch("cli_agent_orchestrator.providers.kimi_cli.get_backend", return_value=mock_backend),
        patch.object(provider, "get_status", return_value=TerminalStatus.IDLE),
    ):
        return await _run_with_heartbeat_probe(
            provider._handle_startup_dialog(idle_gap=5.0, outer_timeout=5.0)
        )


async def _antigravity_handler_run() -> int:
    """AntigravityCliProvider._handle_startup_dialog: one poll, ready footer."""
    provider = AntigravityCliProvider("t1", "sess", "win")
    mock_backend = MagicMock()
    mock_backend.get_history.side_effect = _blocking_history("? for shortcuts\n> ")
    with patch(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend",
        return_value=mock_backend,
    ):
        return await _run_with_heartbeat_probe(
            provider._handle_startup_dialog(idle_gap=5.0, outer_timeout=5.0)
        )


async def _copilot_trust_run() -> int:
    """CopilotCliProvider._accept_trust_prompts: one poll, idle prompt near end."""
    provider = CopilotCliProvider("t1", "sess", "win")
    with patch.object(
        provider,
        "_history",
        side_effect=_blocking_history("GitHub Copilot v0.0.415\n❯ Type @ to mention files"),
    ):
        return await _run_with_heartbeat_probe(provider._accept_trust_prompts(timeout=5.0))


async def _copilot_shell_ready_run() -> int:
    """CopilotCliProvider._wait_for_shell_ready: needs 2 stable identical reads."""
    provider = CopilotCliProvider("t1", "sess", "win")
    with patch.object(
        provider,
        "_history",
        side_effect=_blocking_history("$ "),
    ):
        return await _run_with_heartbeat_probe(
            provider._wait_for_shell_ready(timeout=5.0, polling_interval=0.01)
        )


# ---------------------------------------------------------------------------
# Layer 1: structural pin -- must be real coroutine functions.
# ---------------------------------------------------------------------------

_COROUTINE_TARGETS = [
    ("kimi:_handle_startup_dialog", KimiCliProvider._handle_startup_dialog),
    ("antigravity:_handle_startup_dialog", AntigravityCliProvider._handle_startup_dialog),
    ("copilot:_accept_trust_prompts", CopilotCliProvider._accept_trust_prompts),
    ("copilot:_wait_for_shell_ready", CopilotCliProvider._wait_for_shell_ready),
]


@pytest.mark.parametrize("name,handler", _COROUTINE_TARGETS, ids=[t[0] for t in _COROUTINE_TARGETS])
def test_handler_is_a_real_coroutine_function(name, handler):
    assert asyncio.iscoroutinefunction(handler), f"{name} regressed to a plain (blocking) def"


# ---------------------------------------------------------------------------
# Layer 2: heartbeat-starvation probe -- must actually yield the event loop.
# ---------------------------------------------------------------------------

_HEARTBEAT_CASES = [
    ("kimi:_handle_startup_dialog", _kimi_handler_run),
    ("antigravity:_handle_startup_dialog", _antigravity_handler_run),
    ("copilot:_accept_trust_prompts", _copilot_trust_run),
    ("copilot:_wait_for_shell_ready", _copilot_shell_ready_run),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,run_case", _HEARTBEAT_CASES, ids=[c[0] for c in _HEARTBEAT_CASES])
async def test_handler_does_not_starve_event_loop(name, run_case):
    ticks = await run_case()
    assert ticks > 0, f"{name}: event loop starved while its backend call was in flight"


# ---------------------------------------------------------------------------
# Layer 3: cleanup() lock offload -- _unregister_mcp_servers must not block
# the event loop when cleanup() is called from an async context (e.g.
# flow_service.execute_flow → cleanup_provider on the loop thread).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_antigravity_cleanup_does_not_block_event_loop(tmp_path):
    """cleanup() offloads _unregister_mcp_servers via run_in_executor so the
    _MCP_CONFIG_WRITE_LOCK + file I/O never runs on the event-loop thread."""
    import json

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"cao-mcp-server": {"command": "x"}}}))
    provider = AntigravityCliProvider("t1", "sess", "win")
    provider._mcp_server_names = ["cao-mcp-server"]

    # Make _unregister_mcp_servers block long enough for heartbeat detection.
    original_unregister = provider._unregister_mcp_servers

    def _slow_unregister():
        time.sleep(_BLOCKING_CALL_SECONDS)
        original_unregister()

    ticks = 0
    stop = asyncio.Event()

    async def _ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(_TICKER_INTERVAL_SECONDS)
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        with patch.object(provider, "_unregister_mcp_servers", _slow_unregister):
            # cleanup() detects the running loop and offloads to executor.
            provider.cleanup()
            # Give the executor time to complete.
            await asyncio.sleep(_BLOCKING_CALL_SECONDS * 3)
    stop.set()
    ticker_task.cancel()
    try:
        await ticker_task
    except asyncio.CancelledError:
        pass
    assert ticks > 0, "event loop starved during cleanup's _unregister_mcp_servers"
