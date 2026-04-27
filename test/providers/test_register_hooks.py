"""Default ``BaseProvider.register_hooks`` is a no-op for all providers.

Historically (Phase 2.5 U7) each provider delegated to thin installers in
``hooks/registration.py``. The plugin migration moved memory-context
injection to CAO plugins that listen on ``post_create_terminal``; no
provider owns hook registration any more. The default stays on
``BaseProvider`` so ``terminal_service.create_terminal`` can still call
``provider_instance.register_hooks(...)`` uniformly without a ladder.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.gemini_cli import GeminiCliProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.q_cli import QCliProvider


class _Concrete(BaseProvider):
    """Concrete BaseProvider that does not override register_hooks."""

    def initialize(self) -> bool:
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        return self._status

    def get_idle_pattern_for_log(self) -> str:
        return r">"

    def extract_last_message_from_script(self, script_output: str) -> str:
        return ""

    async def extract_session_context(self) -> Dict[str, Any]:
        return {}

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        pass


class TestBaseProviderDefault:
    def test_default_register_hooks_is_noop(self) -> None:
        provider = _Concrete("term-1", "sess", "win")
        assert provider.register_hooks("/tmp/does-not-matter", "profile") is None
        assert provider.register_hooks(None, None) is None


class TestAllProvidersInheritNoop:
    """No provider overrides ``register_hooks`` after the plugin migration."""

    _ALL_PROVIDERS = [
        ClaudeCodeProvider,
        KiroCliProvider,
        CodexProvider,
        QCliProvider,
        CopilotCliProvider,
        GeminiCliProvider,
        KimiCliProvider,
    ]

    @pytest.mark.parametrize("provider_cls", _ALL_PROVIDERS)
    def test_default_noop(self, provider_cls) -> None:
        provider = provider_cls("term-x", "sess", "win", agent_profile="dev")
        assert provider.register_hooks("/workspace/project", "dev") is None

    @pytest.mark.parametrize("provider_cls", _ALL_PROVIDERS)
    def test_does_not_override_register_hooks(self, provider_cls) -> None:
        """Guard against a provider re-acquiring bespoke hook registration."""
        assert "register_hooks" not in provider_cls.__dict__


class TestTerminalServiceHookDispatch:
    """``create_terminal`` must call the provider API uniformly, with no ladder."""

    def test_terminal_service_has_no_hook_ladder(self) -> None:
        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service.create_terminal)
        assert "register_hooks_claude_code" not in source
        assert "register_hooks_codex" not in source
        assert "register_hooks_kiro" not in source

    def test_terminal_service_calls_provider_register_hooks(self) -> None:
        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service.create_terminal)
        assert "provider_instance.register_hooks(" in source
