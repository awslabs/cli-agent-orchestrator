"""GraphProvider ABC and its name-keyed registry (FR-4).

Design: Issue #348 (graph-layer epic); design record:
aidlc/spaces/default/intents/260709-graph-layer/ (AIDLC intent, not shipped
with the package).
"""

from abc import ABC, abstractmethod
from typing import Any, Callable

from cli_agent_orchestrator.graph.models import GraphView


class GraphProvider(ABC):
    """Projects some subsystem's state into a GraphView.

    Uniformly async (ADR-7) so U4's route handler never needs to branch
    between a sync and an async provider. Never raises for an empty
    result — returns an empty GraphView(nodes=[], edges=[]).

    Providers may expose two optional, non-abstract hooks:

    * ``project_inflight(**filters)`` returns a cache-owned projection Future.
      Without it, request timeout cancellation reaches ``project()`` directly,
      and the provider forfeits the graph route's single-flight deduplication
      and cache admission control.
    * ``projection_status(**filters)`` returns additive build status for a
      projection. Without it, timeout responses omit ``build_state``,
      ``build_elapsed_s``, and ``build_started_at``.

    These hooks are intentionally documentation-only seams. Adding concrete
    defaults would make the route's ``callable()`` capability checks always
    true, silently killing the heterogeneous fallback path; any future default
    must remove both checks and that fallback in the same change.

    Trivial providers need only implement ``project``.
    """

    @abstractmethod
    async def project(self, **filters: Any) -> GraphView:
        """Build a GraphView for the given filters (e.g. scope, scope_id)."""
        raise NotImplementedError


_REGISTRY: dict[str, type[GraphProvider]] = {}


def register_provider(name: str) -> Callable[[type[GraphProvider]], type[GraphProvider]]:
    """Class decorator; registers a GraphProvider subclass under `name`.

    Raises ValueError on duplicate name registration.
    """

    def decorator(cls: type[GraphProvider]) -> type[GraphProvider]:
        if name in _REGISTRY:
            raise ValueError(f"provider {name!r} is already registered")
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_provider(name: str) -> GraphProvider:
    """Resolve and instantiate a registered provider by name.

    Raises KeyError for an unregistered name.
    """
    if name not in _REGISTRY:
        raise KeyError(f"no provider registered under {name!r}")
    return _REGISTRY[name]()


def list_providers() -> list[str]:
    """List all registered provider names."""
    return list(_REGISTRY.keys())
