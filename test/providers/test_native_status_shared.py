"""Shared native-status (herdr backend) tests across all herdr-capable providers.

Every provider's ``get_status()`` must consult the backend's native agent state
before parsing its output buffer, because on the herdr backend ``pipe_pane`` is a
no-op and the StatusMonitor buffer is always empty. Before the fix for issue
#359, only ``claude_code`` did this; every other provider returned UNKNOWN on the
empty buffer and timed out at init.

These tests exercise the shared ``BaseProvider._resolve_native_status()`` path by
patching the backend singleton so ``get_native_status()`` returns a known status,
then calling ``get_status("")`` (empty buffer, as on herdr) and asserting the
mapping — including the ``_task_dispatched`` IDLE-vs-COMPLETED disambiguation and
the None fall-through to buffer parsing on the tmux backend.

``claude_code`` keeps its own detailed suite (``TestClaudeCodeProviderNativeStatus``
in test_claude_code_unit.py); it is included here for breadth alongside the
providers that previously had no native-status coverage. ``q_cli`` and
``gemini_cli`` are out of scope for the herdr backend and excluded.
"""

import time
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider
from cli_agent_orchestrator.providers.hermes import HermesProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider


def _make(provider_cls):
    """Construct a provider with the minimal args each constructor requires."""
    return provider_cls("test1234", "test-session", "window-0", "developer")


# (provider class, name of the provider's own buffer-detection dispatch flag or None).
# The own flag must ALSO flip on mark_input_received() alongside _task_dispatched.
PROVIDERS = [
    pytest.param(KiroCliProvider, "_input_received", id="kiro_cli"),
    pytest.param(CodexProvider, None, id="codex"),
    pytest.param(CopilotCliProvider, None, id="copilot_cli"),
    pytest.param(KimiCliProvider, "_has_received_input", id="kimi_cli"),
    pytest.param(OpenCodeCliProvider, None, id="opencode_cli"),
    pytest.param(CursorCliProvider, "_turns", id="cursor_cli"),
    pytest.param(AntigravityCliProvider, "_turns", id="antigravity_cli"),
    pytest.param(HermesProvider, None, id="hermes"),
    pytest.param(ClaudeCodeProvider, None, id="claude_code"),
]


@pytest.mark.parametrize("provider_cls, _flag", PROVIDERS)
class TestSharedNativeStatus:
    """Native-status mapping shared by every herdr-capable provider."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_processing(self, mock_backend, provider_cls, _flag):
        mock_backend.get_native_status.return_value = TerminalStatus.PROCESSING
        provider = _make(provider_cls)
        assert provider.get_status("") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_processing_resets_flush_timers(self, mock_backend, provider_cls, _flag):
        mock_backend.get_native_status.return_value = TerminalStatus.PROCESSING
        provider = _make(provider_cls)
        provider._task_dispatched = True
        provider._done_first_detected = time.time() - 5.0
        provider._idle_first_detected = time.time() - 5.0
        provider.get_status("")
        assert provider._done_first_detected == 0.0
        assert provider._idle_first_detected == 0.0

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_waiting_user_answer(self, mock_backend, provider_cls, _flag):
        mock_backend.get_native_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider = _make(provider_cls)
        assert provider.get_status("") == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_error(self, mock_backend, provider_cls, _flag):
        mock_backend.get_native_status.return_value = TerminalStatus.ERROR
        provider = _make(provider_cls)
        assert provider.get_status("") == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_idle_not_dispatched_is_idle(self, mock_backend, provider_cls, _flag):
        """herdr 'idle' before any task dispatched -> IDLE (unblocks init)."""
        mock_backend.get_native_status.return_value = TerminalStatus.IDLE
        provider = _make(provider_cls)
        # _task_dispatched defaults to False
        assert provider.get_status("") == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_completed_not_dispatched_is_completed(self, mock_backend, provider_cls, _flag):
        mock_backend.get_native_status.return_value = TerminalStatus.COMPLETED
        provider = _make(provider_cls)
        assert provider.get_status("") == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_idle_dispatched_flushes_then_completed(self, mock_backend, provider_cls, _flag):
        """herdr 'idle' after dispatch: PROCESSING within 10s flush, COMPLETED after."""
        mock_backend.get_native_status.return_value = TerminalStatus.IDLE
        provider = _make(provider_cls)
        provider.mark_input_received()

        # First detection stamps the timer -> still flushing -> PROCESSING.
        assert provider.get_status("") == TerminalStatus.PROCESSING
        # Rewind first-detection past the 10s flush window -> COMPLETED.
        provider._idle_first_detected = time.time() - 11.0
        assert provider.get_status("") == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_done_dispatched_flushes_then_completed(self, mock_backend, provider_cls, _flag):
        """herdr 'done' after dispatch: PROCESSING within 10s flush, COMPLETED after."""
        mock_backend.get_native_status.return_value = TerminalStatus.COMPLETED
        provider = _make(provider_cls)
        provider.mark_input_received()

        assert provider.get_status("") == TerminalStatus.PROCESSING
        provider._done_first_detected = time.time() - 11.0
        assert provider.get_status("") == TerminalStatus.COMPLETED

    @staticmethod
    def _empty_output_default(provider_cls) -> TerminalStatus:
        """Each provider's own no-output default (unaffected by this fix)."""
        return TerminalStatus.ERROR if provider_cls is HermesProvider else TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_falls_through_to_live_buffer_default(
        self, mock_backend, provider_cls, _flag
    ):
        """native None -> always falls through; a genuinely empty live-read buffer
        then hits each provider's own no-output default (UNKNOWN, ERROR for hermes).

        On the herdr backend an 'unknown' agent_status surfaces as native None.
        Before this fix, base resolution guessed IDLE from dispatch state
        instead of falling through -- silently reporting init success even for
        a dead/wedged launch (issue #400's own regression). Now native=None
        always falls through to BaseProvider._resolve_buffer(), which does a
        live get_history() read on herdr (mocked here to genuinely empty "");
        every provider's own empty-output branch then applies, exactly as it
        would on the tmux backend. No provider overrides _resolve_native_status,
        so this holds for all of them.
        """
        mock_backend.get_native_status.return_value = None
        mock_backend.get_history.return_value = ""
        mock_backend.supports_event_inbox.return_value = True
        provider = _make(provider_cls)
        # _task_dispatched defaults to False
        assert provider.get_status("") == self._empty_output_default(provider_cls)

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_dispatch_state_no_longer_affects_outcome(
        self, mock_backend, provider_cls, _flag
    ):
        """A dispatched-but-quiet pane must NOT guess PROCESSING/ERROR from a clock.

        Regresses the must-fix-2 bug: the removed staleness matrix reported
        PROCESSING for ~120s then ERROR purely from elapsed wall-clock time
        since dispatch, even though the pane never produced any content. With
        native=None always falling through, dispatch state must not change the
        result at all -- only real buffer content can.
        """
        mock_backend.get_native_status.return_value = None
        mock_backend.get_history.return_value = ""
        mock_backend.supports_event_inbox.return_value = True
        provider = _make(provider_cls)
        provider.mark_input_received()
        provider._last_dispatch_time = time.time() - 300.0  # would have been "stale" pre-fix
        assert provider.get_status("") == self._empty_output_default(provider_cls)

    def test_mark_input_received_sets_dispatch_flags(self, provider_cls, _flag):
        """mark_input_received() sets shared _task_dispatched AND the provider's own flag."""
        provider = _make(provider_cls)
        assert provider._task_dispatched is False
        provider.mark_input_received()
        assert provider._task_dispatched is True
        assert provider._last_dispatch_time > 0.0
        if _flag == "_turns":
            assert provider._turns == 1
        elif _flag is not None:
            assert getattr(provider, _flag) is True
