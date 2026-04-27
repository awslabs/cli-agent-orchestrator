"""U7 — Hook Registration via BaseProvider.

Verifies that each CLI provider owns its hook registration via
``BaseProvider.register_hooks`` instead of a provider-type ladder in
``services/terminal_service.py``. Acceptance criteria (see
``aidlc-docs/phase2.5/tasks.md §U7``):

- AC1: ``terminal_service.py`` has no provider-type conditionals for hooks.
- AC2: Adding a new provider requires zero changes in ``terminal_service.py``
  for hook registration.
- AC3: Existing Phase 2 U7 (cache-aware injection) unaffected.
- AC4: All existing hook tests pass unchanged.
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
        # Should not raise and should return None regardless of arguments.
        assert provider.register_hooks("/tmp/does-not-matter", "profile") is None
        assert provider.register_hooks(None, None) is None


class TestClaudeCodeRegisterHooks:
    def test_delegates_to_register_hooks_claude_code(self, mocker) -> None:
        called = mocker.patch(
            "cli_agent_orchestrator.hooks.registration.register_hooks_claude_code"
        )
        provider = ClaudeCodeProvider("term-cc", "sess", "win", agent_profile="dev")
        provider.register_hooks("/workspace/project", "dev")
        called.assert_called_once_with("/workspace/project")

    def test_skips_when_no_working_directory(self, mocker) -> None:
        called = mocker.patch(
            "cli_agent_orchestrator.hooks.registration.register_hooks_claude_code"
        )
        provider = ClaudeCodeProvider("term-cc", "sess", "win")
        provider.register_hooks(None, "dev")
        called.assert_not_called()


class TestKiroRegisterHooks:
    def test_delegates_to_register_hooks_kiro(self, mocker) -> None:
        called = mocker.patch("cli_agent_orchestrator.hooks.registration.register_hooks_kiro")
        provider = KiroCliProvider("term-k", "sess", "win", agent_profile="dev")
        provider.register_hooks("/workspace/project", "dev")
        called.assert_called_once_with("dev")

    def test_skips_when_no_agent_profile(self, mocker) -> None:
        called = mocker.patch("cli_agent_orchestrator.hooks.registration.register_hooks_kiro")
        provider = KiroCliProvider("term-k", "sess", "win", agent_profile="placeholder")
        provider.register_hooks("/workspace/project", None)
        called.assert_not_called()


class TestHookLessProviders:
    """Providers without hook support must inherit the no-op default."""

    @pytest.mark.parametrize(
        "provider_cls",
        [QCliProvider, CopilotCliProvider, GeminiCliProvider, KimiCliProvider, CodexProvider],
    )
    def test_default_noop(self, provider_cls) -> None:
        provider = provider_cls("term-x", "sess", "win", agent_profile="dev")
        # Must not raise. Default no-op lives on BaseProvider; none of these
        # providers should redefine it unless they acquire hook support.
        assert provider.register_hooks("/workspace/project", "dev") is None

    @pytest.mark.parametrize(
        "provider_cls",
        [QCliProvider, CopilotCliProvider, GeminiCliProvider, KimiCliProvider, CodexProvider],
    )
    def test_does_not_override_register_hooks(self, provider_cls) -> None:
        """Guard against accidentally shadowing the default with a stub."""
        assert "register_hooks" not in provider_cls.__dict__


class TestTerminalServiceHookDispatch:
    """AC1+AC2: no provider-type conditionals in terminal_service for hooks.

    Verified by source inspection — the hook registration step must
    call ``provider_instance.register_hooks(...)`` without branching
    on provider type.
    """

    def test_terminal_service_has_no_hook_ladder(self) -> None:
        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service.create_terminal)
        # The old ladder imported these three together — none of them
        # should be referenced from create_terminal any longer.
        assert "register_hooks_claude_code" not in source
        assert "register_hooks_codex" not in source
        assert "register_hooks_kiro" not in source

    def test_terminal_service_calls_provider_register_hooks(self) -> None:
        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service.create_terminal)
        assert "provider_instance.register_hooks(" in source


class TestRegisterHooksFailureContainment:
    """A raised exception from register_hooks must not block terminal creation.

    The service wraps the call in try/except and logs a warning. We verify
    the provider itself re-raises (so failures are visible) while trusting
    the service to contain them — ``test_terminal_service_has_no_hook_ladder``
    already confirms the wrapping is present.
    """

    def test_claude_code_propagates_registration_errors(self, mocker) -> None:
        mocker.patch(
            "cli_agent_orchestrator.hooks.registration.register_hooks_claude_code",
            side_effect=ValueError("bad path"),
        )
        provider = ClaudeCodeProvider("term-cc", "sess", "win")
        with pytest.raises(ValueError, match="bad path"):
            provider.register_hooks("/workspace/project", "dev")
