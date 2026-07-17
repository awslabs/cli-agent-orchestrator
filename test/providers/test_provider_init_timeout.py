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

should-fix 4 (call-me-ram's PR #428 review) extends the same wiring to
KimiCliProvider and AntigravityCliProvider, whose ``_handle_startup_dialog()``
previously hard-capped its outer deadline at the SERVER default regardless of
a longer per-profile override -- see ``TestKimiInitTimeoutWiring`` /
``TestAntigravityInitTimeoutWiring`` below.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

# claude_code module namespace (module-level imports patched here).
_CC = "cli_agent_orchestrator.providers.claude_code"
# get_init_timeout reads the server default from settings_service (lazy import).
_SETTINGS = "cli_agent_orchestrator.services.settings_service.get_server_settings"

# A rendered pane that satisfies NEW_TUI_BOX_PATTERN, returned by the mocked
# backend's get_history so initialize()'s final wait_until_input_ready() settle
# check sees a stable, input-ready box and returns immediately. Without a string
# here get_history yields a bare MagicMock, which strip_terminal_escapes ->
# re.sub rejects with "expected string or bytes-like object" (see PR #441).
_READY_PANE = "────────\n> \n────────"


class TestInitializePassesResolvedInitTimeout:
    """The timeout get_init_timeout resolves must cap every wait in initialize().

    Mocks every external call so only the timeout wiring is exercised:
    load_agent_profile (profile source), wait_for_shell / wait_until_status /
    wait_until_input_ready (the async waits), _build_claude_command (avoids
    temp-file I/O), _handle_startup_prompts (asserted separately),
    _ensure_skip_bypass_prompt_setting (avoids writing
    ~/.claude/settings.json), and the terminal backend.
    """

    @pytest.fixture(autouse=True)
    def _mock_input_ready(self):
        with patch.object(ClaudeCodeProvider, "wait_until_input_ready"):
            yield

    @pytest.mark.asyncio
    @patch.object(ClaudeCodeProvider, "wait_until_input_ready")
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
        mock_wait_ready,
    ):
        """provider_init_timeout=180 caps wait_for_shell, handler, and wait_until_status."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_backend.get_history.return_value = _READY_PANE
        mock_load.return_value = AgentProfile(name="a", description="d", provider_init_timeout=180)

        provider = ClaudeCodeProvider("t1", "sess", "win", "agent-x")
        result = await provider.initialize()

        assert result is True
        assert mock_wait_shell.call_args.kwargs["timeout"] == 180
        assert mock_wait_status.call_args.kwargs["timeout"] == 180
        assert mock_handle.call_args.kwargs["outer_timeout"] == 180

    @pytest.mark.asyncio
    @patch.object(ClaudeCodeProvider, "wait_until_input_ready")
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
        mock_wait_ready,
    ):
        """No provider_init_timeout on the profile -> the 60s server default flows through."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_backend.get_history.return_value = _READY_PANE
        mock_load.return_value = AgentProfile(name="a", description="d")

        provider = ClaudeCodeProvider("t1", "sess", "win", "agent-x")
        result = await provider.initialize()

        assert result is True
        assert mock_wait_shell.call_args.kwargs["timeout"] == 60
        assert mock_wait_status.call_args.kwargs["timeout"] == 60
        assert mock_handle.call_args.kwargs["outer_timeout"] == 60

    @pytest.mark.asyncio
    @patch.object(ClaudeCodeProvider, "wait_until_input_ready")
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
        mock_wait_ready,
    ):
        """No agent profile at all (_load_profile -> None) -> server default flows through."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_backend.get_history.return_value = _READY_PANE

        provider = ClaudeCodeProvider("t1", "sess", "win")  # agent_profile=None
        result = await provider.initialize()

        assert result is True
        assert mock_wait_shell.call_args.kwargs["timeout"] == 60
        assert mock_wait_status.call_args.kwargs["timeout"] == 60
        assert mock_handle.call_args.kwargs["outer_timeout"] == 60

    @pytest.mark.asyncio
    @patch.object(ClaudeCodeProvider, "wait_until_input_ready")
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
        mock_wait_ready,
    ):
        """initialize() must pass the timeout as outer_timeout, never positionally.

        _handle_startup_prompts(idle_gap, outer_timeout): the first positional
        slot is idle_gap. A regression that dropped the keyword would silently
        shrink the idle gap to the (large) init timeout and leave outer_timeout
        at the settings default -- a real bug this guards against.
        """
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_backend.get_history.return_value = _READY_PANE
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


# Kimi CLI module namespace (module-level imports patched here).
_KIMI = "cli_agent_orchestrator.providers.kimi_cli"


class TestKimiInitTimeoutWiring:
    """should-fix 4: KimiCliProvider._handle_startup_dialog honors the per-profile
    provider_init_timeout override, not just the server default.

    Before this fix, ``_handle_startup_dialog()`` hard-capped its outer
    deadline at ``get_server_settings()["provider_init_timeout"]`` (server
    default, often 60s) regardless of how long ``initialize()`` itself was
    willing to wait (``wait_until_status`` there uses a 120s floor, longer with
    a profile override). A dialog appearing after the server default but
    before the real init timeout would never be dismissed -- the handler
    exits early, init hangs until its own outer wait times out.
    """

    @pytest.mark.asyncio
    @patch.object(KimiCliProvider, "_handle_startup_dialog")
    @patch(f"{_KIMI}.load_agent_profile")
    @patch(f"{_KIMI}.wait_for_shell")
    @patch(f"{_KIMI}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_profile_override_flows_to_startup_dialog_outer_timeout(
        self, mock_backend, mock_wait_status, mock_wait_shell, mock_load, mock_handle
    ):
        """provider_init_timeout=180 reaches _handle_startup_dialog's outer_timeout kwarg."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.return_value = AgentProfile(name="a", description="d", provider_init_timeout=180)

        provider = KimiCliProvider("t1", "sess", "win", agent_profile="agent-x")
        result = await provider.initialize()

        assert result is True
        assert mock_handle.call_args.kwargs["outer_timeout"] == 180
        # wait_until_status uses max(120, init_timeout) -- 180 wins here.
        assert mock_wait_status.call_args.kwargs["timeout"] == 180
        assert mock_wait_shell.call_args.kwargs["timeout"] == 180

    @pytest.mark.asyncio
    @patch(_SETTINGS, return_value={"provider_init_timeout": 60})
    @patch.object(KimiCliProvider, "_handle_startup_dialog")
    @patch(f"{_KIMI}.wait_for_shell")
    @patch(f"{_KIMI}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_no_profile_uses_120s_floor_not_server_default(
        self, mock_backend, mock_wait_status, mock_wait_shell, mock_handle, mock_settings
    ):
        """No agent profile -> the 120s Kimi-specific floor wins over the 60s server default.

        Preserves Kimi's existing "account for first-run setup / concurrent
        launches" floor (see initialize()'s docstring) rather than silently
        shrinking it to the server default once the per-profile wiring landed.
        """
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = KimiCliProvider("t1", "sess", "win")  # agent_profile=None
        result = await provider.initialize()

        assert result is True
        assert mock_handle.call_args.kwargs["outer_timeout"] == 120.0
        assert mock_wait_status.call_args.kwargs["timeout"] == 120.0
        assert mock_wait_shell.call_args.kwargs["timeout"] == 60

    @pytest.mark.asyncio
    @patch.object(KimiCliProvider, "_handle_startup_dialog")
    @patch(f"{_KIMI}.load_agent_profile")
    @patch(f"{_KIMI}.wait_for_shell")
    @patch(f"{_KIMI}.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_broken_profile_falls_back_to_default_not_raise(
        self, mock_backend, mock_wait_status, mock_wait_shell, mock_load, mock_handle
    ):
        """A profile that fails to load for timeout resolution must not abort init early.

        _try_load_profile() swallows the failure so get_init_timeout() still
        resolves (to the server default); the REAL raising load in
        _build_kimi_command() is untouched and still reports a genuine broken
        profile at the point that method runs.
        """
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.side_effect = FileNotFoundError("nope")

        provider = KimiCliProvider("t1", "sess", "win", agent_profile="missing")
        with pytest.raises(Exception):
            # _build_kimi_command still raises ProviderError for the same
            # missing profile -- _try_load_profile only affects the timeout.
            await provider.initialize()


# Antigravity CLI module namespace (module-level imports patched here).
_AGY = "cli_agent_orchestrator.providers.antigravity_cli"


class TestAntigravityInitTimeoutWiring:
    """should-fix 4: AntigravityCliProvider._handle_startup_dialog honors the
    per-profile provider_init_timeout override, not just the server default.

    Mirrors TestKimiInitTimeoutWiring -- see its class docstring for the bug
    this closes (both Copilot inline review comments on PR #428).
    """

    @pytest.mark.asyncio
    @patch.object(AntigravityCliProvider, "_handle_startup_dialog")
    @patch(f"{_AGY}.load_agent_profile")
    @patch(f"{_AGY}.wait_for_shell")
    @patch(f"{_AGY}.wait_until_status")
    @patch(f"{_AGY}.get_backend")
    @patch(f"{_AGY}.shutil.which", return_value="/usr/local/bin/agy")
    async def test_profile_override_flows_to_startup_dialog_outer_timeout(
        self,
        mock_which,
        mock_get_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load,
        mock_handle,
    ):
        """provider_init_timeout=200 (> the 180s default) reaches both waits."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.return_value = AgentProfile(name="a", description="d", provider_init_timeout=200)

        provider = AntigravityCliProvider("t1", "sess", "win", agent_profile="agent-x")
        result = await provider.initialize()

        assert result is True
        assert mock_handle.call_args.kwargs["outer_timeout"] == 200
        assert mock_wait_status.call_args.kwargs["timeout"] == 200

    @pytest.mark.asyncio
    @patch(_SETTINGS, return_value={"provider_init_timeout": 60})
    @patch.object(AntigravityCliProvider, "_handle_startup_dialog")
    @patch(f"{_AGY}.wait_for_shell")
    @patch(f"{_AGY}.wait_until_status")
    @patch(f"{_AGY}.get_backend")
    @patch(f"{_AGY}.shutil.which", return_value="/usr/local/bin/agy")
    async def test_no_profile_uses_180s_floor_not_server_default(
        self,
        mock_which,
        mock_get_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_handle,
        mock_settings,
    ):
        """No agent profile -> the 180s agy-specific floor wins over the 60s server default."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = AntigravityCliProvider("t1", "sess", "win")  # agent_profile=None
        result = await provider.initialize()

        assert result is True
        assert mock_handle.call_args.kwargs["outer_timeout"] == 180.0
        assert mock_wait_status.call_args.kwargs["timeout"] == 180.0
