"""Plugin discovery, registration, dispatch, and lifecycle management."""

import importlib.metadata
import inspect
import logging
from typing import Any

from cli_agent_orchestrator.plugins.base import _HOOK_EVENT_ATTR, CaoPlugin
from cli_agent_orchestrator.plugins.events import CaoEvent

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "cao.plugins"


class PluginRegistry:
    """Registry for discovered CAO plugins and their hook handlers."""

    def __init__(self) -> None:
        """Initialize an empty plugin registry."""

        self._plugins: list[CaoPlugin] = []
        self._dispatch: dict[str, list[Any]] = {}

    async def load(self) -> None:
        """Discover, instantiate, and set up all registered CAO plugins."""

        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        for entry_point in entry_points:
            try:
                plugin_class = entry_point.load()
                if not (isinstance(plugin_class, type) and issubclass(plugin_class, CaoPlugin)):
                    logger.warning(
                        "Plugin entry point '%s' is not a CaoPlugin subclass, skipping",
                        entry_point.name,
                    )
                    continue

                plugin = plugin_class()
                await plugin.setup()
                self._register(plugin)
                logger.info("Loaded CAO plugin: %s", entry_point.name)
            except Exception:
                logger.warning(
                    "Failed to load plugin '%s'",
                    entry_point.name,
                    exc_info=True,
                )

        if not self._plugins:
            logger.info("No CAO plugins registered (cao.plugins entry point group is empty)")

    def _register(self, plugin: CaoPlugin) -> None:
        """Register a plugin instance and index any decorated hook methods."""

        self._plugins.append(plugin)
        for _, method in inspect.getmembers(plugin, predicate=inspect.ismethod):
            event_type = getattr(method, _HOOK_EVENT_ATTR, None)
            if event_type is not None:
                self._dispatch.setdefault(event_type, []).append(method)

    async def dispatch(self, event_type: str, event: CaoEvent) -> None:
        """Dispatch an event to all matching plugin hook handlers."""

        for handler in self._dispatch.get(event_type, []):
            try:
                await handler(event)
            except Exception:
                logger.warning(
                    "Hook '%s' raised an error for event '%s'",
                    handler.__qualname__,
                    event_type,
                    exc_info=True,
                )

    async def teardown(self) -> None:
        """Call teardown() on every loaded plugin, continuing after failures."""

        for plugin in self._plugins:
            try:
                await plugin.teardown()
            except Exception:
                logger.warning(
                    "Plugin teardown failed for %s",
                    type(plugin).__name__,
                    exc_info=True,
                )
