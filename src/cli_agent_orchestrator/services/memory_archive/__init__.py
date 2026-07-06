"""Memory archive backend registry (#345 D1).

Same shape as ``backends/registry.py`` and the provider manager: a
module-level dict populated at import time via ``register_backend``.
``get_backend`` raises ``ValueError`` on unknown names, which the CLI
maps to a ``click.ClickException`` and the API maps to HTTP 400.

``okf`` is the first registered backend; the parked CAO-native tar.gz
format registers later as ``cao``.
"""

from typing import Dict, Type

from cli_agent_orchestrator.services.memory_archive.base import (
    ExportReport,
    ImportReport,
    MemoryArchiveBackend,
)

__all__ = [
    "ExportReport",
    "ImportReport",
    "MemoryArchiveBackend",
    "OkfArchiveBackend",
    "get_backend",
    "register_backend",
]

# Module-level registry: format name → backend class.
_backends: Dict[str, Type[MemoryArchiveBackend]] = {}


def register_backend(name: str, cls: Type[MemoryArchiveBackend]) -> None:
    """Register an archive backend class under ``name`` (e.g. "okf")."""
    _backends[name] = cls


def get_backend(name: str) -> Type[MemoryArchiveBackend]:
    """Return the backend class registered under ``name``.

    Raises ``ValueError`` on unknown names — the CLI/API boundary maps it
    to a user-facing error per project rules.
    """
    if name not in _backends:
        known = ", ".join(sorted(_backends)) or "<none>"
        raise ValueError(f"Unknown memory archive format {name!r} (known: {known})")
    return _backends[name]


# Import placed after the registry helpers: okf.py imports the base module,
# and registering at import time is the D1 pattern (provider manager shape).
from cli_agent_orchestrator.services.memory_archive.okf import (  # noqa: E402
    OkfArchiveBackend,
)

register_backend(OkfArchiveBackend.format_name, OkfArchiveBackend)
