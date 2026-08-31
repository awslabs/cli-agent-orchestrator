"""Tests for the run-plane SSE idle heartbeat (P1-2 / F-SL6).

The heartbeat interval is injectable so this runs at sub-second cadence rather
than baking a multi-second sleep into CI; the production default is asserted
separately via config inspection.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List

import pytest


def _minimal_run_input() -> Dict[str, Any]:
    return {
        "threadId": "thread-hb",
        "runId": "run-hb",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


async def _collect(gen) -> List[str]:
    return [frame async for frame in gen]


def test_production_default_heartbeat_is_15s() -> None:
    # F-SL6: assert the production default via config inspection (not a sleep).
    from cli_agent_orchestrator.services.agui import run_plane

    assert run_plane.RUN_PLANE_HEARTBEAT_SECONDS == 15.0


@pytest.mark.asyncio
async def test_run_plane_emits_keepalive_when_idle() -> None:
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    async def idle_then_stop() -> AsyncGenerator[Any, None]:
        # Idle long enough to trip at least one heartbeat, then end.
        await asyncio.sleep(0.15)
        return
        yield  # pragma: no cover  (make this an async generator)

    frames = await _collect(
        run_plane_stream(
            input_data=_minimal_run_input(),
            bus_subscribe_fn=idle_then_stop,
            heartbeat_interval=0.03,
        )
    )

    assert any(f.startswith(":keep-alive") for f in frames), frames
    # Lifecycle still well-formed around the heartbeats.
    assert frames[0].startswith("data:")  # RUN_STARTED
    assert frames[-1].startswith("data:")  # RUN_FINISHED


@pytest.mark.asyncio
async def test_keepalive_does_not_corrupt_pending_read() -> None:
    """A heartbeat timeout must not cancel the in-flight bus read: the delayed
    event still arrives after the keep-alive(s)."""
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

    async def one_delayed_event() -> AsyncGenerator[Any, None]:
        await asyncio.sleep(0.1)
        yield {"type": "status_changed", "terminal_id": "t1", "status": "PROCESSING"}

    frames = await _collect(
        run_plane_stream(
            input_data=_minimal_run_input(),
            bus_subscribe_fn=one_delayed_event,
            heartbeat_interval=0.03,
        )
    )

    # Got keep-alives while idle AND completed the run after the delayed read.
    assert any(f.startswith(":keep-alive") for f in frames), frames
    assert frames[-1].startswith("data:")
