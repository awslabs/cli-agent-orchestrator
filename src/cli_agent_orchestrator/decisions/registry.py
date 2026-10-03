"""Lazy discovery by installed entry-point name, with cached failures."""

import inspect
import logging
from importlib.metadata import entry_points
from typing import Callable, Mapping, cast

from cli_agent_orchestrator.decisions.types import POINTS, Decider

logger = logging.getLogger(__name__)


class DeciderRegistry:
    def __init__(self, sources: Mapping[str, Callable[[], Decider]] | None = None) -> None:
        self._sources: dict[str, Callable[[], Decider]] = (
            dict(sources) if sources is not None else {}
        )
        if sources is None:
            for entry in entry_points(group="cao.deciders"):
                if entry.name in self._sources:
                    logger.warning(
                        "Duplicate decider entry-point name %s; last registration wins", entry.name
                    )
                self._sources[entry.name] = self._loader(entry.load)
        self._cache: dict[str, Decider | None] = {}

    @staticmethod
    def _loader(load: Callable[[], object]) -> Callable[[], Decider]:
        def construct() -> Decider:
            cls = cast(Callable[[], Decider], load())
            return cls()

        return construct

    def get(self, name: str, point: str) -> Decider | None:
        if name not in self._cache:
            try:
                value = self._sources[name]()
                if (
                    value.name != name
                    or not isinstance(value.version, str)
                    or not isinstance(value.points, frozenset)
                    or not value.points <= POINTS.keys()
                    or not callable(value.decide)
                ):
                    raise ValueError("invalid decider declaration")
                self._cache[name] = value
            except Exception:
                logger.warning("Decider unavailable; failure cached until restart")
                self._cache[name] = None
        decider = self._cache[name]
        return decider if decider is not None and point in decider.points else None

    async def close(self) -> None:
        for decider in self._cache.values():
            close = getattr(decider, "close", None)
            if close is not None:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.warning("Decider shutdown failed")
