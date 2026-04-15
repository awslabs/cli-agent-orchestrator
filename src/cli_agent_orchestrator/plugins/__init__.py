"""Public API for the CAO plugin system."""

from cli_agent_orchestrator.plugins.base import CaoPlugin, hook
from cli_agent_orchestrator.plugins.events import (
    CaoEvent,
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.plugins.registry import PluginRegistry

__all__ = [
    "CaoPlugin",
    "hook",
    "CaoEvent",
    "PostSendMessageEvent",
    "PostCreateSessionEvent",
    "PostKillSessionEvent",
    "PostCreateTerminalEvent",
    "PostKillTerminalEvent",
    "PluginRegistry",
]
