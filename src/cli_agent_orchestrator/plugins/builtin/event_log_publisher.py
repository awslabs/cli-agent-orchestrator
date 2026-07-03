"""Observer-only plugin that mirrors lifecycle hooks into the event ring buffer.

The ``EventLogPublisher`` is a pure side-observer of CAO's existing orchestration
lifecycle. It subscribes to the five ``Post*`` lifecycle events, normalizes each
to one of the six semantic primitives, appends a metadata-only row to the in-process
``EventLog``, and relays the same row to the ``SseBus`` for live fan-out.

Invariants:
- **Observer-only.** It never mutates Backplane state in response to an event —
  it only reads the event and writes to the in-process buffer / bus.
- **Privacy boundary.** It stores metadata only. Message *bodies* (e.g.
  ``PostSendMessageEvent.message``) are never appended or relayed.
- **Failure-isolated.** Hooks run *after* the orchestration action; the registry
  already swallows and logs hook exceptions, so a buffer/bus hiccup can never
  crash ``cao-server`` or alter fleet behavior.
"""

from __future__ import annotations

import logging

from cli_agent_orchestrator.plugins import (
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
    hook,
)
from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.event_log_service import get_event_log
from cli_agent_orchestrator.services.event_primitives import normalize_kind
from cli_agent_orchestrator.services.sse_bus import get_bus

logger = logging.getLogger(__name__)


def _apps_enabled() -> bool:
    """Return whether the MCP Apps surface is enabled via ``apps.enabled``
    (``CAO_MCP_APPS_ENABLED`` env var or ``settings.json``).

    Mirrors the gate used by ``mcp_apps`` / ``app_tools`` / ``sep2133`` so this
    observer is genuinely default-off. The plugin is still discovered and loaded
    by the entry-point registry, but when the flag is unset its lifecycle hooks
    no-op: no normalization, no ring-buffer retention, and no SSE fan-out. That
    preserves the "default-off, zero-cost-when-unused" contract — an operator who
    never enables the surface neither pays the per-event cost nor accumulates 24h
    of fleet metadata in process memory.
    """

    return bool(ConfigService.get("apps.enabled", default=False))


class EventLogPublisher(CaoPlugin):
    """Mirror ``Post*`` lifecycle hooks into the ring buffer as 6-primitive events."""

    async def setup(self) -> None:
        """Stateless plugin; nothing to initialize."""

    async def teardown(self) -> None:
        """Stateless plugin; nothing to release."""

    # ------------------------------------------------------------------
    # shared emission path

    def _emit(
        self,
        event_type: str,
        terminal_id: str | None,
        session_name: str | None,
        detail: dict,
    ) -> None:
        """Normalize, append (metadata only), and relay to the SSE bus.

        ``detail`` MUST already be metadata-only — callers below construct it
        explicitly from event fields and deliberately exclude message bodies.
        """

        # Default-off gate: when the MCP Apps surface is disabled the observer
        # does nothing, so the ring buffer / SSE bus are never touched (see
        # ``_apps_enabled``). This keeps the lifecycle hooks zero-cost and
        # retains no fleet metadata unless an operator opts in.
        if not _apps_enabled():
            return

        kind = normalize_kind(event_type, detail)
        event = get_event_log().append(kind, terminal_id, session_name, detail)
        # Best-effort live fan-out; drop-on-slow is handled inside the bus and
        # the durable record is always the ring buffer.
        get_bus().publish(event)

    # ------------------------------------------------------------------
    # lifecycle hooks (observer-only)

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Record a terminal launch."""

        self._emit(
            event.event_type,
            event.terminal_id,
            event.session_id,
            {
                "event_type": event.event_type,
                "provider": event.provider,
                "agent_name": event.agent_name,
            },
        )

    @hook("post_create_session")
    async def on_post_create_session(self, event: PostCreateSessionEvent) -> None:
        """Record a session launch."""

        self._emit(
            event.event_type,
            None,
            event.session_name,
            {"event_type": event.event_type, "session_name": event.session_name},
        )

    @hook("post_send_message")
    async def on_post_send_message(self, event: PostSendMessageEvent) -> None:
        """Record a message dispatch as handoff / a2a_delegation.

        Privacy boundary: the message *body* (``event.message``) is deliberately
        excluded — only routing/orchestration metadata is stored.
        """

        self._emit(
            event.event_type,
            event.receiver or None,
            event.session_id,
            {
                "event_type": event.event_type,
                "sender": event.sender,
                "receiver": event.receiver,
                "orchestration_type": event.orchestration_type,
            },
        )

    @hook("post_kill_terminal")
    async def on_post_kill_terminal(self, event: PostKillTerminalEvent) -> None:
        """Record a terminal completion."""

        self._emit(
            event.event_type,
            event.terminal_id,
            event.session_id,
            {"event_type": event.event_type, "agent_name": event.agent_name},
        )

    @hook("post_kill_session")
    async def on_post_kill_session(self, event: PostKillSessionEvent) -> None:
        """Record a session completion."""

        self._emit(
            event.event_type,
            None,
            event.session_name,
            {"event_type": event.event_type, "session_name": event.session_name},
        )
