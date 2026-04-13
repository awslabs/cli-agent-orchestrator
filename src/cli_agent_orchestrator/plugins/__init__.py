"""Public API for the CAO plugin system."""

from cli_agent_orchestrator.plugins.base import CaoPlugin, hook
from cli_agent_orchestrator.plugins.events import (
    CaoEvent,
    MessageSentEvent,
    SessionCreatedEvent,
    SessionKilledEvent,
    TerminalCreatedEvent,
    TerminalKilledEvent,
)
from cli_agent_orchestrator.plugins.registry import PluginRegistry

__all__ = [
    "CaoPlugin",
    "hook",
    "CaoEvent",
    "MessageSentEvent",
    "SessionCreatedEvent",
    "SessionKilledEvent",
    "TerminalCreatedEvent",
    "TerminalKilledEvent",
    "PluginRegistry",
]
