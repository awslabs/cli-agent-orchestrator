"""Smoke tests for the public CAO plugin package API."""

from cli_agent_orchestrator.plugins import (
    CaoEvent,
    CaoPlugin,
    MessageSentEvent,
    PluginRegistry,
    SessionCreatedEvent,
    SessionKilledEvent,
    TerminalCreatedEvent,
    TerminalKilledEvent,
    __all__,
    hook,
)
from cli_agent_orchestrator.plugins.base import CaoPlugin as BaseCaoPlugin
from cli_agent_orchestrator.plugins.base import hook as base_hook
from cli_agent_orchestrator.plugins.events import CaoEvent as BaseCaoEvent
from cli_agent_orchestrator.plugins.events import MessageSentEvent as BaseMessageSentEvent
from cli_agent_orchestrator.plugins.events import SessionCreatedEvent as BaseSessionCreatedEvent
from cli_agent_orchestrator.plugins.events import SessionKilledEvent as BaseSessionKilledEvent
from cli_agent_orchestrator.plugins.events import TerminalCreatedEvent as BaseTerminalCreatedEvent
from cli_agent_orchestrator.plugins.events import TerminalKilledEvent as BaseTerminalKilledEvent
from cli_agent_orchestrator.plugins.registry import PluginRegistry as BasePluginRegistry


class TestPluginPackageAPI:
    """Tests for the plugin package's public exports."""

    def test_public_imports_resolve_to_expected_symbols(self) -> None:
        """Importing from the package should resolve to the concrete implementation objects."""

        assert CaoPlugin is BaseCaoPlugin
        assert hook is base_hook
        assert CaoEvent is BaseCaoEvent
        assert MessageSentEvent is BaseMessageSentEvent
        assert SessionCreatedEvent is BaseSessionCreatedEvent
        assert SessionKilledEvent is BaseSessionKilledEvent
        assert TerminalCreatedEvent is BaseTerminalCreatedEvent
        assert TerminalKilledEvent is BaseTerminalKilledEvent
        assert PluginRegistry is BasePluginRegistry

    def test___all___contains_exactly_the_phase_two_public_api(self) -> None:
        """The package __all__ should expose exactly the documented public symbols."""

        assert __all__ == [
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
