"""HerdrInboxService registry — module-level service singleton management.

This module provides get_herdr_inbox_service() and set_herdr_inbox_service() as the
central access point for the configured event-inbox service (HerdrInboxService or
HerdrAppInboxService). It types the singleton with the structural EventInboxService
protocol, so it has no dependency on either service module, breaking the circular
import chain.

Unlike backends/registry.py, there is no lazy initialization: get_herdr_inbox_service()
returns None until set_herdr_inbox_service() is called at application startup.
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class EventInboxService(Protocol):
    """What terminal_service needs from an event-inbox service.

    Implemented by HerdrInboxService (herdr TUI backend) and
    HerdrAppInboxService (herdr-app backend). A structural type, so this module
    still imports neither service.
    """

    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None: ...

    def unregister_terminal(self, terminal_id: str) -> None: ...


# Module-level service instance. Set at application startup via set_herdr_inbox_service().
_herdr_inbox_service: Optional[EventInboxService] = None


def get_herdr_inbox_service() -> Optional[EventInboxService]:
    """Return the configured event-inbox service, or None if not yet set."""
    return _herdr_inbox_service


def set_herdr_inbox_service(service: Optional[EventInboxService]) -> None:
    """Set the event-inbox service, or clear it by passing None.

    Called with a service instance at application startup and with None during
    shutdown so the module-level singleton does not outlive the running service.
    """
    global _herdr_inbox_service
    _herdr_inbox_service = service
