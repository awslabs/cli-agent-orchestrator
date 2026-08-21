"""HerdrInboxService registry — module-level service singleton management.

This module provides get_herdr_inbox_service() and set_herdr_inbox_service() as the
central access point for the configured HerdrInboxService. It uses a TYPE_CHECKING
guard for the HerdrInboxService import so it has no runtime dependency on the service
module, breaking the circular import chain.

Unlike backends/registry.py, there is no lazy initialization: get_herdr_inbox_service()
returns None until set_herdr_inbox_service() is called at application startup.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

# Module-level service instance. Set at application startup via set_herdr_inbox_service().
_herdr_inbox_service: Optional["HerdrInboxService"] = None


def get_herdr_inbox_service() -> Optional["HerdrInboxService"]:
    """Return the configured HerdrInboxService, or None if not yet set."""
    return _herdr_inbox_service


def set_herdr_inbox_service(service: Optional["HerdrInboxService"]) -> None:
    """Set the HerdrInboxService, or clear it by passing None.

    Called with a service instance at application startup and with None during
    shutdown so the module-level singleton does not outlive the running service.
    """
    global _herdr_inbox_service
    _herdr_inbox_service = service
