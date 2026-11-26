"""Backend registry — module-level backend singleton management.

This module provides get_backend() and set_backend() as the central access point
for the configured TerminalBackend. It has no dependencies on providers or services,
breaking the circular import chain.
"""

from typing import Optional

from cli_agent_orchestrator.backends.base import TerminalBackend

# Module-level backend instance. Initialized lazily via get_backend().
_backend: Optional[TerminalBackend] = None


def get_backend() -> TerminalBackend:
    """Return the configured terminal backend (lazy-initialized via BackendFactory)."""
    global _backend
    if _backend is None:
        from cli_agent_orchestrator.backends.factory import BackendFactory

        _backend = BackendFactory.create()
    return _backend


def peek_backend() -> Optional[TerminalBackend]:
    """Return the backend if one was created or set, else None. Never creates one.

    For best-effort work (publishing labels) that must not be the thing that
    builds a backend, as ``get_backend()`` would on its first call.
    """
    return _backend


def set_backend(backend: TerminalBackend) -> None:
    """Set the terminal backend (called at application startup)."""
    global _backend
    _backend = backend
