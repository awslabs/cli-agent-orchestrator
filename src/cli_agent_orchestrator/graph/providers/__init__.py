"""Graph provider seam: ABC, name-keyed registry, and built-in providers."""

from cli_agent_orchestrator.graph.providers.base import (
    GraphProvider,
    get_provider,
    list_providers,
    register_provider,
)

# Import-time registration: importing this package registers the built-in
# providers ("memory", "stub") via their @register_provider decorators.
from cli_agent_orchestrator.graph.providers.memory import MemoryGraphProvider
from cli_agent_orchestrator.graph.providers.stub import StubGraphProvider

__all__ = [
    "GraphProvider",
    "register_provider",
    "get_provider",
    "list_providers",
    "MemoryGraphProvider",
    "StubGraphProvider",
]
