"""GraphSink ABC and its name-keyed registry (FR-5).

Design: Issue #348 (graph-layer epic); design record:
aidlc/spaces/default/intents/260709-graph-layer/ (AIDLC intent, not shipped
with the package).
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, ClassVar

from cli_agent_orchestrator.constants import graph_export_root
from cli_agent_orchestrator.graph.models import GraphView
from cli_agent_orchestrator.utils.path_validation import safe_join_under_base


def confine_under_export_root(dest: str, description: str = "export destination") -> Path:
    """Confine ``dest`` under the configured graph-export root (B3 security).

    This is the sink-side containment primitive owned by BOTH the route and
    the sink (the U4 route does NOT pre-validate ``dest``, so every sink must
    confine it before its first write). ``dest`` is treated as a path
    *relative* to ``CAO_GRAPH_EXPORT_ROOT`` (see
    ``constants.graph_export_root``); an ABSOLUTE ``dest`` is accepted only
    when it already resolves under that root, otherwise it is rejected. The
    relative segments are joined via
    :func:`utils.path_validation.safe_join_under_base`, which validates every
    segment and enforces realpath containment — so no ``..`` traversal,
    absolute-path escape, or symlink escape can leave the root.

    Unlike the plain ``resolve_and_validate_path`` blocklist (which blocks
    only ~18 exact system dirs, NOT their subdirectories), this guarantees
    the result is the root itself or a descendant of it.

    Returns:
        The canonicalized absolute path under the export root (may not exist
        yet — the caller creates it under this confined path).

    Raises:
        ValueError: If ``dest`` escapes the root or names no subpath.
    """
    root_real = os.path.realpath(os.path.abspath(str(graph_export_root())))

    if os.path.isabs(dest):
        dest_real = os.path.realpath(os.path.abspath(os.path.expanduser(dest)))
        if dest_real != root_real and not dest_real.startswith(root_real + os.sep):
            raise ValueError(
                f"{description} {dest!r} is outside the graph export root "
                f"{root_real!r}; supply a path relative to the root or one under it"
            )
        rel = os.path.relpath(dest_real, root_real)
    else:
        rel = dest

    segments = [seg for seg in rel.split(os.sep) if seg not in ("", ".")]
    if not segments:
        raise ValueError(f"{description} must name a subpath under the graph export root: {dest!r}")
    return Path(safe_join_under_base(root_real, *segments, description=description))


class GraphSink(ABC):
    """Exports a GraphView to some external format/destination.

    Security contract for ``dest``: implementations MUST resolve and
    confine ``dest`` under an allowed base directory before writing —
    reject ``..`` traversal, absolute-path escapes, and symlink escapes.
    The allowed base is the configured graph-export root
    (``CAO_GRAPH_EXPORT_ROOT`` / ``constants.graph_export_root``); use the
    shared :func:`confine_under_export_root` helper, which joins ``dest``
    under that root via ``safe_join_under_base`` (per-segment validation +
    realpath containment). ``dest`` validation is owned by the sink
    implementation itself — the U4 route forwards ``dest`` unvalidated, so a
    sink must never assume its caller already confined the path. Every sink
    confines before its first write.

    NOTE: the plain ``resolve_and_validate_path`` blocklist is NOT sufficient
    confinement here — it blocks only a fixed set of exact system directories,
    not their subdirectories, so ``~/.ssh`` / ``/etc/cron.d`` / ``/var/www``
    would pass. Confinement under the export root is mandatory.
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
