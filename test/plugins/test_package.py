"""Smoke tests for the public CAO plugin package API."""

from cli_agent_orchestrator.plugins import (
    CaoEvent,
    CaoPlugin,
    PluginRegistry,
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
    __all__,
    hook,
)
from cli_agent_orchestrator.plugins.base import CaoPlugin as BaseCaoPlugin
from cli_agent_orchestrator.plugins.base import hook as base_hook
from cli_agent_orchestrator.plugins.events import CaoEvent as BaseCaoEvent
from cli_agent_orchestrator.plugins.events import (
    PostCreateSessionEvent as BasePostCreateSessionEvent,
)
from cli_agent_orchestrator.plugins.events import (
    PostCreateTerminalEvent as BasePostCreateTerminalEvent,
)
from cli_agent_orchestrator.plugins.events import PostKillSessionEvent as BasePostKillSessionEvent
from cli_agent_orchestrator.plugins.events import PostKillTerminalEvent as BasePostKillTerminalEvent
from cli_agent_orchestrator.plugins.events import PostSendMessageEvent as BasePostSendMessageEvent
from cli_agent_orchestrator.plugins.registry import PluginRegistry as BasePluginRegistry


class TestPluginPackageAPI:
    """Tests for the plugin package's public exports."""

    def test_public_imports_resolve_to_expected_symbols(self) -> None:
        """Importing from the package should resolve to the concrete implementation objects."""

        assert CaoPlugin is BaseCaoPlugin
        assert hook is base_hook
        assert CaoEvent is BaseCaoEvent
        assert PostSendMessageEvent is BasePostSendMessageEvent
        assert PostCreateSessionEvent is BasePostCreateSessionEvent
        assert PostKillSessionEvent is BasePostKillSessionEvent
        assert PostCreateTerminalEvent is BasePostCreateTerminalEvent
        assert PostKillTerminalEvent is BasePostKillTerminalEvent
        assert PluginRegistry is BasePluginRegistry

    def test___all___contains_exactly_the_phase_two_public_api(self) -> None:
        """The package __all__ should expose exactly the documented public symbols."""

        assert __all__ == [
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
