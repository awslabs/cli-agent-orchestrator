"""GraphSink ABC and its name-keyed registry (FR-5)."""

from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar

from cli_agent_orchestrator.graph.models import GraphView


class GraphSink(ABC):
    """Exports a GraphView to some external format/destination."""

    capabilities: ClassVar[set[str]] = set()

    @abstractmethod
    def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
        """Write view to dest per sink format; return the written file paths."""
        raise NotImplementedError


_SINK_REGISTRY: dict[str, type[GraphSink]] = {}


def register_sink(name: str) -> Callable[[type[GraphSink]], type[GraphSink]]:
    """Class decorator; registers a GraphSink subclass under `name`.

    Raises ValueError on duplicate name registration.
    """

    def decorator(cls: type[GraphSink]) -> type[GraphSink]:
        if name in _SINK_REGISTRY:
            raise ValueError(f"sink {name!r} is already registered")
        _SINK_REGISTRY[name] = cls
        return cls

    return decorator


def get_sink(name: str) -> GraphSink:
    """Resolve and instantiate a registered sink by name.

    Raises KeyError for an unregistered name.
    """
    if name not in _SINK_REGISTRY:
        raise KeyError(f"no sink registered under {name!r}")
    return _SINK_REGISTRY[name]()


def list_sinks() -> list[str]:
    """List all registered sink names."""
    return list(_SINK_REGISTRY.keys())
