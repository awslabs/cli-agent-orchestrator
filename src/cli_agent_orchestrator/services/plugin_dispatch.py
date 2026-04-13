"""Helpers for emitting plugin events from synchronous service functions."""

import asyncio

from cli_agent_orchestrator.plugins import CaoEvent, PluginRegistry


def dispatch_plugin_event(
    registry: PluginRegistry | None, event_type: str, event: CaoEvent
) -> None:
    """Dispatch a plugin event without forcing a broad async refactor.

    If called inside a running event loop (the common FastAPI path), the
    dispatch coroutine is scheduled as a background task. If no loop is
    running (for synchronous code paths and unit tests), the dispatch runs to
    completion via ``asyncio.run``.
    """

    if registry is None:
        return

    coroutine = registry.dispatch(event_type, event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coroutine)
    else:
        loop.create_task(coroutine)
