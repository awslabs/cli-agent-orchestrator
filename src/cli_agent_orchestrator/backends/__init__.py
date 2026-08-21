"""Terminal backend abstraction layer.

This package provides the TerminalBackend ABC and concrete implementations
(TmuxBackend, HerdrBackend) that decouple CAO core services from any
specific terminal multiplexer.
"""

from cli_agent_orchestrator.backends.base import (
    TerminalBackend,
    TerminalBackendError,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.backends.factory import BackendFactory, ConfigurationError

__all__ = [
    "BackendFactory",
    "ConfigurationError",
    "TerminalBackend",
    "TerminalBackendError",
    "TerminalNotFoundError",
]
