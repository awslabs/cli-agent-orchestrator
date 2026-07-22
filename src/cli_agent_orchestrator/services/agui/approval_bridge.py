"""ApprovalBridge: event-bus consumer that triggers approval interrupts.

Subscribes to ``terminal.*.status`` and creates/expires interrupts on the
``AgentHandoffWithApproval`` construct when terminals enter or leave the
``WAITING_USER_ANSWER`` state.

Only active when ``agui_surface_enabled()`` returns True.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    AnswerDelivery,
    RecordingUiEmitter,
)
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


class ApprovalBridge:
    """Event-bus consumer bridging terminal status events to approval interrupts.

    Lifecycle:
    - On terminal entering WAITING_USER_ANSWER: captures prompt text, resolves
      provider, and calls ``on_provider_waiting``.
    - On terminal leaving WAITING_USER_ANSWER with an open interrupt: calls
      ``expire`` (zero keystrokes).
    """

    def __init__(
        self,
        construct: AgentHandoffWithApproval,
        get_output_fn: Optional[object] = None,
        get_provider_fn: Optional[object] = None,
        get_session_fn: Optional[object] = None,
    ) -> None:
        """Initialize the bridge.

        Args:
            construct: The AgentHandoffWithApproval instance to delegate to.
            get_output_fn: Callable(terminal_id) -> str to get terminal output.
                Defaults to terminal_service.get_output.
            get_provider_fn: Callable(terminal_id) -> str|None to get provider name.
                Defaults to provider_manager.get_provider_type.
            get_session_fn: Callable(terminal_id) -> str|None to get session name.
        """
        self._construct = construct
        self._get_output_fn = get_output_fn
        self._get_provider_fn = get_provider_fn
        self._get_session_fn = get_session_fn
        # Track which terminals are in WAITING_USER_ANSWER
        self._waiting_terminals: set = set()
        self._task: Optional[asyncio.Task] = None

    @property
    def construct(self) -> AgentHandoffWithApproval:
        """Access the underlying approval construct."""
        return self._construct

    async def run(self) -> None:
        """Main consumer loop, subscribes to terminal.*.status."""
        from cli_agent_orchestrator.services.agui_enablement import agui_surface_enabled

        if not agui_surface_enabled():
            logger.debug("ApprovalBridge: AG-UI surface disabled, not starting")
            return

        queue = bus.subscribe("terminal.*.status")
        logger.info("ApprovalBridge started")

        while True:
            try:
                event = await queue.get()
                status_value = event["data"]["status"]
                terminal_id = terminal_id_from_topic(event["topic"])

                if status_value == TerminalStatus.WAITING_USER_ANSWER.value:
                    # Terminal entered waiting state
                    if terminal_id not in self._waiting_terminals:
                        self._waiting_terminals.add(terminal_id)
                        await self._on_waiting(terminal_id)
                else:
                    # Terminal left waiting state
                    if terminal_id in self._waiting_terminals:
                        self._waiting_terminals.discard(terminal_id)
                        self._on_leave_waiting(terminal_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in ApprovalBridge: {e}")

    async def _on_waiting(self, terminal_id: str) -> None:
        """Handle terminal entering WAITING_USER_ANSWER."""
        # Get prompt text
        raw_prompt = ""
        if self._get_output_fn:
            try:
                raw_prompt = self._get_output_fn(terminal_id)
            except Exception:
                logger.debug("Failed to get output for terminal %s", terminal_id)
        else:
            try:
                from cli_agent_orchestrator.services import terminal_service

                raw_prompt = terminal_service.get_output(terminal_id)
            except Exception:
                logger.debug("Failed to get output for terminal %s", terminal_id)

        # Get the last portion of prompt (tail)
        if raw_prompt and len(raw_prompt) > 1024:
            raw_prompt = raw_prompt[-1024:]

        # Get provider name
        provider = ""
        if self._get_provider_fn:
            try:
                provider = self._get_provider_fn(terminal_id) or ""
            except Exception:
                pass
        else:
            try:
                from cli_agent_orchestrator.providers.manager import provider_manager

                p = provider_manager.get_provider(terminal_id)
                if p:
                    # Get the provider type name
                    provider = type(p).__name__.replace("Provider", "").lower()
                    # Map back to the expected names
                    provider_map = {
                        "claudecode": "claude_code",
                        "kirocli": "kiro_cli",
                        "codex": "codex",
                        "mockcli": "mock_cli",
                    }
                    provider = provider_map.get(provider, provider)
            except Exception:
                pass

        # Get session name
        session_name = None
        if self._get_session_fn:
            try:
                session_name = self._get_session_fn(terminal_id)
            except Exception:
                pass

        self._construct.on_provider_waiting(
            terminal_id=terminal_id,
            provider=provider,
            raw_prompt=raw_prompt,
            session_name=session_name,
        )

    def _on_leave_waiting(self, terminal_id: str) -> None:
        """Handle terminal leaving WAITING_USER_ANSWER with open interrupt."""
        self._construct.expire(terminal_id)

    async def start(self) -> None:
        """Start the bridge as a background task."""
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Stop the bridge."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


__all__ = ["ApprovalBridge"]
