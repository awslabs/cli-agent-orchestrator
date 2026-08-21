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

``CodexProvider._handle_trust_prompt`` and ``CodexProvider.initialize`` were
added later (caom-7it), for the same reason and against the same property. Codex
is not the last remaining gap — kiro_cli, opencode_cli and cursor_cli still make
loop-side backend calls in ``initialize()`` — but it has the slowest init of the
group, so its stall was the one that showed up under a concurrent fan-out.
``initialize`` is covered as well as the handler because its OWN send_keys /
get_pane_current_command calls are blocking subprocess execs too, not just the
prompt poll; it gets its own layer (2b) because a tick count cannot measure a
coroutine that awaits a fixed ``asyncio.sleep`` of its own.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

# Simulated blocking backend latency per poll -- long enough that several
# ticker increments fit inside it if (and only if) the call is truly
# offloaded to a worker thread via asyncio.to_thread, rather than run
# directly on the event-loop thread.
_BLOCKING_CALL_SECONDS = 0.05
_TICKER_INTERVAL_SECONDS = 0.01

# Blocking latency and threshold for the LONGEST-GAP probe (layer 2b). Larger
# than _BLOCKING_CALL_SECONDS so the two outcomes sit far apart: offloaded, the
# worst gap stays near the ticker interval (0.01s) and must clear a 10x jitter
# margin; left on the loop, the gap is at least the full 0.2s blocking call.
_MAX_GAP_BLOCKING_SECONDS = 0.2
_MAX_ACCEPTABLE_GAP_SECONDS = 0.1

# A codex frame showing the idle composer and no blocking dialog, so
# _handle_trust_prompt returns after a single poll.
_CODEX_READY_FRAME = "OpenAI Codex (v0.145.0)\n› Explain this codebase\n  gpt-5.6-sol high · /tmp\n"


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

    The zero reading is load-bearing and depends on the ticker NOT having run
    yet: ``create_task`` only schedules it, so a handler that never yields
    starves it before its very first tick. Do not "warm up" the ticker before
    ``await handler_coro`` -- that makes the count non-zero unconditionally and
    silently voids every case below.
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


async def _run_with_max_gap_probe(handler_coro) -> tuple[int, float]:
    """Like ``_run_with_heartbeat_probe``, but returns ``(ticks, worst gap)``.

    For coroutines that ``await`` on their own for reasons unrelated to the
    offload -- ``CodexProvider.initialize()``'s fixed ``asyncio.sleep(2.0)``
    shell warm-up -- the tick COUNT proves nothing: the loop ticks during those
    sleeps whether or not the backend calls are offloaded. The LONGEST interval
    between two ticks does prove it, because it measures the stall itself and
    is therefore bounded by the size of the largest un-offloaded call rather
    than by the coroutine's total runtime.

    Unlike the tick-count probe, this one deliberately brackets the handler
    with ticks:

    * At least one tick BEFORE it runs, so ``last`` is anchored -- otherwise a
      stall that happens before the ticker's first tick goes unmeasured.
    * At least one tick AFTER it returns, because a gap is only recorded when
      the ticker resumes -- a handler that blocks and then returns without ever
      suspending would otherwise look gap-free because the cancel lands first.
    """
    ticks = 0
    max_gap = 0.0
    stop = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks, max_gap
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(_TICKER_INTERVAL_SECONDS)
            now = time.monotonic()
            max_gap = max(max_gap, now - last)
            last = now
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    try:
        while ticks < 1:
            await asyncio.sleep(_TICKER_INTERVAL_SECONDS)
        await handler_coro
        settled = ticks
        while ticks == settled:
            await asyncio.sleep(_TICKER_INTERVAL_SECONDS)
    finally:
        stop.set()
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass
    return ticks, max_gap


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


async def _codex_trust_prompt_run() -> int:
    """CodexProvider._handle_trust_prompt: one poll, idle composer, no dialog."""
    provider = CodexProvider("t1", "sess", "win")
    mock_backend = MagicMock()
    mock_backend.get_history.side_effect = _blocking_history(_CODEX_READY_FRAME)
    with patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=mock_backend):
        return await _run_with_heartbeat_probe(provider._handle_trust_prompt(timeout=5.0))


async def _codex_initialize_run() -> tuple[int, float]:
    """CodexProvider.initialize: its OWN backend calls, not just the prompt poll.

    ``get_pane_current_command`` and both ``send_keys`` calls are blocking
    subprocess execs as well. Stubbed at the longer
    ``_MAX_GAP_BLOCKING_SECONDS`` because this case is measured by worst gap
    (layer 3), not tick count -- ``initialize()`` awaits a fixed
    ``asyncio.sleep`` warm-up that ticks the ticker either way.
    """

    def _blocking(*_args, **_kwargs):
        time.sleep(_MAX_GAP_BLOCKING_SECONDS)

    def _blocking_pane_command(*_args, **_kwargs) -> str:
        time.sleep(_MAX_GAP_BLOCKING_SECONDS)
        return "zsh"

    mock_backend = MagicMock()
    mock_backend.send_keys.side_effect = _blocking
    mock_backend.get_pane_current_command.side_effect = _blocking_pane_command
    mock_backend.get_history.return_value = _CODEX_READY_FRAME

    provider = CodexProvider("t1", "sess", "win")
    with (
        patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=mock_backend),
        patch(
            "cli_agent_orchestrator.providers.codex.get_server_settings",
            return_value={"provider_init_timeout": 60, "startup_prompt_handler_timeout": 20},
        ),
        patch("cli_agent_orchestrator.providers.codex.wait_for_shell", return_value=True),
        patch("cli_agent_orchestrator.providers.codex.wait_until_status", return_value=True),
    ):
        return await _run_with_max_gap_probe(provider.initialize())


# ---------------------------------------------------------------------------
# Layer 1: structural pin -- must be real coroutine functions.
# ---------------------------------------------------------------------------

_COROUTINE_TARGETS = [
    ("kimi:_handle_startup_dialog", KimiCliProvider._handle_startup_dialog),
    ("antigravity:_handle_startup_dialog", AntigravityCliProvider._handle_startup_dialog),
    ("copilot:_accept_trust_prompts", CopilotCliProvider._accept_trust_prompts),
    ("copilot:_wait_for_shell_ready", CopilotCliProvider._wait_for_shell_ready),
    ("codex:_handle_trust_prompt", CodexProvider._handle_trust_prompt),
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
    ("codex:_handle_trust_prompt", _codex_trust_prompt_run),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,run_case", _HEARTBEAT_CASES, ids=[c[0] for c in _HEARTBEAT_CASES])
async def test_handler_does_not_starve_event_loop(name, run_case):
    ticks = await run_case()
    assert ticks > 0, f"{name}: event loop starved while its backend call was in flight"


# ---------------------------------------------------------------------------
# Layer 2b: longest-gap probe -- for coroutines whose own awaits tick the
# ticker regardless of whether their backend calls are offloaded.
#
# ``CodexProvider.initialize()`` awaits a fixed ``asyncio.sleep(2.0)`` shell
# warm-up, so layer 2's tick COUNT would pass even with every backend call left
# on the event loop. The metric that survives is the LONGEST interval between
# two ticks: it measures the stall itself, so it is bounded by the size of the
# largest un-offloaded call rather than by the coroutine's total runtime.
# ---------------------------------------------------------------------------

_MAX_GAP_CASES = [
    ("codex:initialize", _codex_initialize_run),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,run_case", _MAX_GAP_CASES, ids=[c[0] for c in _MAX_GAP_CASES])
async def test_handler_never_stalls_the_loop_for_a_full_backend_call(name, run_case):
    ticks, max_gap = await run_case()
    assert ticks > 0, f"{name}: ticker never sampled"
    assert max_gap < _MAX_ACCEPTABLE_GAP_SECONDS, (
        f"{name}: event loop stalled for {max_gap:.3f}s "
        f"(a {_MAX_GAP_BLOCKING_SECONDS}s backend call ran on the loop thread)"
    )


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
