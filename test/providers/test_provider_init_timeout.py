"""Integration tests for the per-profile ``provider_init_timeout`` feature.

Task 4 / issue #400. ``TestGetInitTimeout`` in ``test_base_provider.py`` covers
``BaseProvider.get_init_timeout`` in isolation (no-profile/override/no-override).
These tests verify the resolved timeout actually FLOWS through
``ClaudeCodeProvider.initialize()`` into every wait it caps, plus edge cases of
the resolver and the outer-cap relationship of the startup-prompt handler.

``initialize()`` loads the profile once, resolves ``get_init_timeout(profile)``,
and passes that single value as:
  - ``wait_for_shell(..., timeout=init_timeout)``
  - ``_handle_startup_prompts(outer_timeout=init_timeout)``
  - ``wait_until_status(..., timeout=init_timeout, ...)``
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

# claude_code module namespace (module-level imports patched here).
_CC = "cli_agent_orchestrator.providers.claude_code"
# get_init_timeout reads the server default from settings_service (lazy import).
_SETTINGS = "cli_agent_orchestrator.services.settings_service.get_server_settings"


class TestInitializePassesResolvedInitTimeout:
    """The timeout get_init_timeout resolves must cap every wait in initialize().

    Mocks every external call so only the timeout wiring is exercised:
    load_agent_profile (profile source), wait_for_shell / wait_until_status
    (the async waits), _build_claude_command (avoids temp-file I/O),
    _handle_startup_prompts (asserted separately), _ensure_skip_bypass_prompt_setting
    (avoids writing ~/.claude/settings.json), and the terminal backend.
    """

    @pytest.mark.asyncio
    @patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")
    @patch.object(ClaudeCodeProvider, "_build_claude_command", return_value="claude")
    @patch.object(ClaudeCodeProvider, "_handle_startup_prompts")
    @patch(f"{_CC}.load_agent_profile")
    @patch(f"{_CC}.wait_for_shell")
    @patch(f"{_CC}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_profile_override_flows_to_every_wait(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load,
        mock_handle,
        mock_build,
        mock_ensure,
    ):
        """provider_init_timeout=180 caps wait_for_shell, handler, and wait_until_status."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.return_value = AgentProfile(name="a", description="d", provider_init_timeout=180)

        provider = ClaudeCodeProvider("t1", "sess", "win", "agent-x")
        result = await provider.initialize()

        assert result is True
        assert mock_wait_shell.call_args.kwargs["timeout"] == 180
        assert mock_wait_status.call_args.kwargs["timeout"] == 180
        assert mock_handle.call_args.kwargs["outer_timeout"] == 180

    @pytest.mark.asyncio
    @patch(_SETTINGS, return_value={"provider_init_timeout": 60})
    @patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")
    @patch.object(ClaudeCodeProvider, "_build_claude_command", return_value="claude")
    @patch.object(ClaudeCodeProvider, "_handle_startup_prompts")
    @patch(f"{_CC}.load_agent_profile")
    @patch(f"{_CC}.wait_for_shell")
    @patch(f"{_CC}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_profile_without_override_uses_server_default(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load,
        mock_handle,
        mock_build,
        mock_ensure,
        mock_settings,
    ):
        """No provider_init_timeout on the profile -> the 60s server default flows through."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.return_value = AgentProfile(name="a", description="d")

        provider = ClaudeCodeProvider("t1", "sess", "win", "agent-x")
        result = await provider.initialize()

        assert result is True
        assert mock_wait_shell.call_args.kwargs["timeout"] == 60
        assert mock_wait_status.call_args.kwargs["timeout"] == 60
        assert mock_handle.call_args.kwargs["outer_timeout"] == 60

    @pytest.mark.asyncio
    @patch(_SETTINGS, return_value={"provider_init_timeout": 60})
    @patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")
    @patch.object(ClaudeCodeProvider, "_build_claude_command", return_value="claude")
    @patch.object(ClaudeCodeProvider, "_handle_startup_prompts")
    @patch(f"{_CC}.wait_for_shell")
    @patch(f"{_CC}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_no_profile_uses_server_default(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_handle,
        mock_build,
        mock_ensure,
        mock_settings,
    ):
        """No agent profile at all (_load_profile -> None) -> server default flows through."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = ClaudeCodeProvider("t1", "sess", "win")  # agent_profile=None
        result = await provider.initialize()

        assert result is True
        assert mock_wait_shell.call_args.kwargs["timeout"] == 60
        assert mock_wait_status.call_args.kwargs["timeout"] == 60
        assert mock_handle.call_args.kwargs["outer_timeout"] == 60

    @pytest.mark.asyncio
    @patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")
    @patch.object(ClaudeCodeProvider, "_build_claude_command", return_value="claude")
    @patch.object(ClaudeCodeProvider, "_handle_startup_prompts")
    @patch(f"{_CC}.load_agent_profile")
    @patch(f"{_CC}.wait_for_shell")
    @patch(f"{_CC}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_outer_timeout_passed_as_keyword_not_positional(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load,
        mock_handle,
        mock_build,
        mock_ensure,
    ):
        """initialize() must pass the timeout as outer_timeout, never positionally.

        _handle_startup_prompts(idle_gap, outer_timeout): the first positional
        slot is idle_gap. A regression that dropped the keyword would silently
        shrink the idle gap to the (large) init timeout and leave outer_timeout
        at the settings default -- a real bug this guards against.
        """
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.return_value = AgentProfile(name="a", description="d", provider_init_timeout=180)

        provider = ClaudeCodeProvider("t1", "sess", "win", "agent-x")
        await provider.initialize()

        assert mock_handle.call_args.args == ()  # nothing positional
        assert mock_handle.call_args.kwargs == {"outer_timeout": 180}


class TestGetInitTimeoutEdgeCases:
    """Boundary and coercion cases for get_init_timeout not covered elsewhere."""

    def _provider(self) -> ClaudeCodeProvider:
        return ClaudeCodeProvider("t1", "sess", "win")

    @patch(_SETTINGS, return_value={"provider_init_timeout": 999})
    def test_zero_override_is_not_treated_as_unset(self, _):
        """provider_init_timeout=0 returns 0 (current behavior).

        The resolver guards on ``is not None``, so 0 short-circuits and is
        returned verbatim -- it does NOT fall through to the server default.
        Documents the edge: 0 is a caller-supplied value, not "unset". The
        patched 999 default proves the profile value wins.
        """
        profile = AgentProfile(name="a", description="d", provider_init_timeout=0)
        assert self._provider().get_init_timeout(profile) == 0

    @patch(_SETTINGS, return_value={"provider_init_timeout": 999})
    def test_minimum_positive_override(self, _):
        """provider_init_timeout=1 (smallest positive) is returned, not the default."""
        profile = AgentProfile(name="a", description="d", provider_init_timeout=1)
        assert self._provider().get_init_timeout(profile) == 1

    @pytest.mark.parametrize("server_value", [30, 60, 90, 120])
    def test_none_profile_reads_server_default(self, server_value):
        """With no profile, the resolver returns whatever the server default is."""
        with patch(_SETTINGS, return_value={"provider_init_timeout": server_value}):
            assert self._provider().get_init_timeout(None) == server_value

    @patch(_SETTINGS, return_value={"provider_init_timeout": 30.9})
    def test_float_server_default_coerced_to_int(self, _):
        """A float server default is truncated via int() (not rounded)."""
        result = self._provider().get_init_timeout(None)
        assert result == 30
        assert isinstance(result, int)


class TestStartupPromptHandlerHonorsOuterTimeout:
    """_handle_startup_prompts must use the outer_timeout it is passed as its deadline.

    Complements TestClaudeCodeIdleGap.test_outer_cap_respected in
    test_startup_prompt_idle_gap.py: that test uses the settings default; this
    one proves an EXPLICITLY passed outer_timeout (what initialize() forwards
    from the per-profile value) governs the outer deadline instead.
    """

    @patch(f"{_CC}.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_passed_outer_timeout_extends_deadline_past_settings_default(
        self, mock_backend, mock_time
    ):
        """A prompt at t=100 is still handled when outer_timeout=180.

        Deadline = monotonic()[0] + outer_timeout = 0 + 180 = 180. At t=100 the
        loop is still alive (100 < 180) and answers the trust prompt. Had the
        handler ignored the passed value and used the 60s default, the top-of-loop
        ``now >= outer_deadline`` (100 >= 60) would have returned before reaching
        get_history -- so the trust Enter firing is the discriminating signal.
        idle_gap is pinned huge so only the outer cap can end the loop.
        """
        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 0 + 180 = 180
            0.0,  # last_prompt_time = 0
            100.0,  # iter1 now: 100<180 (alive), gap 100<1000 -> trust prompt -> handled
        ]
        mock_backend.get_history.return_value = "Yes, I trust this folder"

        provider = ClaudeCodeProvider("t1", "sess", "win")
        provider._handle_startup_prompts(idle_gap=1000, outer_timeout=180)

        mock_backend.send_special_key.assert_called_once()

    @patch(f"{_CC}.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_passed_outer_timeout_caps_a_wedged_start(self, mock_backend, mock_time):
        """With no prompt ever appearing, the loop exits at the passed outer_timeout.

        idle_gap is pinned above outer_timeout so the idle-gap exit can never
        fire; the only way out is time crossing the 180s deadline.
        """
        import logging

        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 180
            0.0,  # last_prompt_time = 0
            100.0,  # iter1 now: 100<180, gap 100<1000, no prompt -> sleep
            181.0,  # iter2 now: 181>=180 -> outer cap -> return
        ]
        mock_backend.get_history.return_value = "still starting..."

        provider = ClaudeCodeProvider("t1", "sess", "win")
        with patch.object(logging.getLogger(_CC), "warning") as mock_warn:
            provider._handle_startup_prompts(idle_gap=1000, outer_timeout=180)

        mock_backend.send_special_key.assert_not_called()
        mock_backend.send_keys.assert_not_called()
        assert mock_warn.called  # "hit provider_init_timeout outer cap"
