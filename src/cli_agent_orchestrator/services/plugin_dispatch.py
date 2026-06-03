"""Helpers for emitting plugin events from synchronous service functions."""

import asyncio
import logging

from cli_agent_orchestrator.plugins import CaoEvent, PluginRegistry

logger = logging.getLogger(__name__)


async def _dispatch_with_logging(
    registry: PluginRegistry, event_type: str, event: CaoEvent
) -> None:
    """Run registry dispatch with local error isolation at the adapter boundary."""

    try:
        await registry.dispatch(event_type, event)
    except Exception:
        logger.warning("Plugin event dispatch failed for %s", event_type, exc_info=True)


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

    coroutine = _dispatch_with_logging(registry, event_type, event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coroutine)
    else:
        loop.create_task(coroutine)
