"""GraphSink ABC and its name-keyed registry (FR-5).

Design: Issue #348 (graph-layer epic); design record:
aidlc/spaces/default/intents/260709-graph-layer/ (AIDLC intent, not shipped
with the package).
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar

from cli_agent_orchestrator.graph.models import GraphView


class GraphSink(ABC):
    """Exports a GraphView to some external format/destination.

    Security contract for ``dest``: implementations MUST resolve and
    confine ``dest`` under an allowed base directory before writing —
    reject ``..`` traversal, absolute-path escapes, and symlink escapes.
    Follow this repo's path-validation convention
    (``utils/path_validation.resolve_and_validate_path``). Validation is
    owned by BOTH the U4 route (first line of defense, validates before
    calling a sink) AND each sink implementation (defense in depth) — a
    sink must not assume its caller already validated ``dest``.
    """

    capabilities: ClassVar[set[str]] = set()

    @abstractmethod
    def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
        """Write view to dest per sink format; return the written file paths.

        Returns the written file paths as list[str]. The API route (U4)
        owns wrapping this into the response envelope
        ({written_files, sink, dest}) — GraphSink.export() itself never
        returns that envelope shape.
        """
        raise NotImplementedError

    def query(self, *args: Any, **kwargs: Any) -> Any:
        """Optional read-query entry point, gated on capabilities (FR-5).

        Only sinks that declare "query" in `capabilities` support this;
        every other sink raises NotImplementedError. A subclass declaring
        "query" must override this method with its own implementation.
        """
        if "query" not in self.capabilities:
            raise NotImplementedError(
                f"{type(self).__name__} does not support query() "
                f"('query' not in capabilities={sorted(self.capabilities)!r})"
            )
        raise NotImplementedError(
            f"{type(self).__name__} declares 'query' in capabilities but did not override query()"
        )


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
