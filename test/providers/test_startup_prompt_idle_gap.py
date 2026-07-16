"""Tests for idle-gap startup prompt handling across providers.

Validates the refactored startup prompt handlers that use idle-gap semantics
(issue #400): each handled prompt resets ``last_prompt_time``; the loop exits
after ``startup_prompt_handler_timeout`` seconds of no-new-prompt (idle gap);
total runtime is hard-capped by ``provider_init_timeout``.

Covers: ClaudeCodeProvider, KimiCliProvider, AntigravityCliProvider.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

# ---------------------------------------------------------------------------
# Shared mock settings
# ---------------------------------------------------------------------------

_SETTINGS = {
    "startup_prompt_handler_timeout": 20,
    "provider_init_timeout": 60,
}


def _settings():
    return dict(_SETTINGS)


# Outer-cap tests set idle_gap > provider_init_timeout so the idle-gap check can
# never fire — the only way out of the loop is time passing provider_init_timeout.
_OUTER_CAP_SETTINGS = {
    "startup_prompt_handler_timeout": 100,
    "provider_init_timeout": 60,
}


def _outer_cap_settings():
    return dict(_OUTER_CAP_SETTINGS)


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------


class TestClaudeCodeIdleGap:
    """Idle-gap semantics in ClaudeCodeProvider._handle_startup_prompts."""

    def _make(self):
        return ClaudeCodeProvider("t1", "sess", "win")

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_late_prompt_handled(self, mock_backend, mock_time):
        """A prompt at t=35s (past the old 20s window) is still handled.

        Two prompts: bypass at t=18 resets the idle timer; the trust prompt at
        t=35 is within idle_gap of that reset (35-18=17 < 20) so it is still
        answered. Under the old fixed-window logic the handler would have exited
        at t=20 and never seen the trust prompt.
        """
        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            18.0,  # iter1 now: gap=18<20, bypass prompt → handled
            18.0,  # last_prompt_time reset to 18
            # continues past the old 20s total window...
            35.0,  # iter2 now: gap=35-18=17<20, trust prompt → handled → return
        ]
        mock_backend.get_history.side_effect = [
            "WARNING: Bypass\n1. No\n2. Yes, I accept\n",
            "Yes, I trust this folder",
        ]

        p = self._make()
        p._handle_startup_prompts()

        # Bypass at t=18 (Down + Enter) and the late trust prompt at t=35 (Enter)
        # are both handled — proving the idle-gap reset kept the loop polling past
        # the old 20s window. Under old logic send_special_key would fire once.
        assert mock_backend.send_keys.call_count == 1  # bypass Down arrow
        assert mock_backend.send_special_key.call_count == 2  # bypass Enter + trust Enter

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_no_prompt_exits_at_outer_cap_not_idle_gap(self, mock_backend, mock_time):
        """No prompt ever appears — the idle gap does NOT apply until a first prompt lands.

        Before any prompt is observed, ``last_prompt_time`` has nothing real to
        measure a gap from, so only the outer cap can end the loop. This is the
        should-fix-3 rework: the exit at t=25 (old idle-gap boundary) must NOT
        fire here — only t=61 (past the 60s outer cap) does.
        """
        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            5.0,  # iter1 now: no prompt handled yet, idle-gap check skipped -> sleep
            25.0,  # iter2 now: still no prompt handled -> idle-gap check skipped -> sleep
            61.0,  # iter3 now: 61>=60 -> outer cap -> return
        ]
        mock_backend.get_history.return_value = "Loading..."

        p = self._make()
        p._handle_startup_prompts()

        # No prompts handled
        mock_backend.send_special_key.assert_not_called()
        mock_backend.send_keys.assert_not_called()

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_first_prompt_later_than_idle_gap_still_handled(self, mock_backend, mock_time):
        """A FIRST dialog later than idle_gap (the issue #400 scenario) is now caught.

        Before this fix, a first prompt at t=35 (past the 20s idle-gap default)
        would never be seen: the loop measured the gap from handler-start, not
        from a real prompt, and exited at t=20. Now the idle-gap clock only
        starts once a prompt has actually been handled, so a first prompt at
        t=35 is well within the still-open outer cap and is handled.
        """
        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            35.0,  # iter1 now: no prompt handled yet -> idle-gap check skipped ->
            # trust prompt found in output -> handled -> return
        ]
        mock_backend.get_history.return_value = "Yes, I trust this folder"

        p = self._make()
        p._handle_startup_prompts()

        mock_backend.send_special_key.assert_called_once_with("sess", "win", "Enter")

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", _outer_cap_settings)
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_outer_cap_respected(self, mock_backend, mock_time):
        """Loop exits at provider_init_timeout, NOT via the idle gap.

        idle_gap=100 > provider_init_timeout=60, so the idle-gap check can never
        fire — the only way out is time advancing past the outer deadline. The
        bypass prompt is handled once (resetting the timer), then the loop idles
        until t=61 trips the outer cap.
        """
        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            10.0,  # iter1: bypass prompt found, handled
            10.0,  # last_prompt_time reset after bypass
            # continues loop (gap=20<100, so idle gap never fires)...
            30.0,  # iter2: bypass_accepted=True, no trust, no welcome; sleep
            61.0,  # iter3 now >= 60 outer_deadline → return (outer cap)
        ]
        # Output always has bypass prompt (handled once, then ignored)
        mock_backend.get_history.return_value = (
            "WARNING: Bypass Permissions\n1. No\n2. Yes, I accept\n"
        )

        p = self._make()
        p._handle_startup_prompts()

        # Bypass accepted once
        mock_backend.send_keys.assert_called_once()
        mock_backend.send_special_key.assert_called_once()

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_cascading_prompts_all_handled(self, mock_backend, mock_time):
        """Multiple prompts in sequence — bypass then trust, both handled."""
        mock_time.sleep = MagicMock()
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            3.0,  # iter1: gap=3<20, bypass prompt → handled
            3.0,  # last_prompt_time reset
            # loop continues
            8.0,  # iter2: gap=8-3=5<20, trust prompt → handled → return
        ]
        mock_backend.get_history.side_effect = [
            "WARNING: Bypass\n1. No\n2. Yes, I accept\n",
            "Yes, I trust this folder",
        ]

        p = self._make()
        p._handle_startup_prompts()

        # Bypass: send_keys (Down arrow) + send_special_key (Enter)
        # Trust: send_special_key (Enter)
        assert mock_backend.send_keys.call_count == 1
        assert mock_backend.send_special_key.call_count == 2

    @patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_idle_gap_resets_on_each_prompt(self, mock_backend, mock_time):
        """First prompt at t=5s resets timer; second at t=22s still within gap of first."""
        mock_time.sleep = MagicMock()
        # idle_gap=20. First prompt at t=5, resets last_prompt_time to 5.
        # Second prompt at t=22: gap=22-5=17<20, so still polled and handled.
        # Without reset, gap would be 22-0=22>=20 → would have exited.
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            5.0,  # iter1: gap=5<20, bypass prompt → handled
            5.0,  # last_prompt_time reset to 5
            # continues
            22.0,  # iter2: gap=22-5=17<20, trust prompt → handled → return
        ]
        mock_backend.get_history.side_effect = [
            "WARNING: Bypass\n1. No\n2. Yes, I accept\n",
            "Yes, I trust this folder",
        ]

        p = self._make()
        p._handle_startup_prompts()

        # Both prompts handled
        assert mock_backend.send_keys.call_count == 1  # bypass Down arrow
        assert mock_backend.send_special_key.call_count == 2  # bypass Enter + trust Enter


# ---------------------------------------------------------------------------
# KimiCliProvider
# ---------------------------------------------------------------------------


class TestKimiCliIdleGap:
    """Idle-gap semantics in KimiCliProvider._handle_startup_dialog."""

    def _make(self):
        return KimiCliProvider("t1", "sess", "win")

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.kimi_cli.time")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_late_prompt_handled(self, mock_get_backend, mock_time):
        """Upgrade at t=18 resets the timer; loop survives to detect ready at t=35.

        Kimi has a single actionable dialog (upgrade), so the two-event structure
        is: upgrade at t=18 resets the idle timer, and the loop keeps polling past
        the old 20s total window to detect readiness at t=35 (35-18=17 < 20). Under
        the old fixed-window logic the loop would have given up at t=20.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            18.0,  # iter1: gap=18<20, upgrade prompt → handled
            18.0,  # last_prompt_time reset to 18
            # continues past the old 20s total window...
            35.0,  # iter2: gap=35-18=17<20, output → ready → return
        ]
        mock_backend.get_history.side_effect = [
            "Skip reminders for version 1.2.3\n[Enter] Upgrade now  [q] Not now",
            "Welcome to Kimi!\n💫",
        ]

        p = self._make()
        # get_status is checked only in iter2 (iter1 continues after handling upgrade)
        with patch.object(p, "get_status", return_value=TerminalStatus.IDLE):
            p._handle_startup_dialog()

        # 's' sent to skip reminders at t=18 — the reset kept the loop alive to
        # cleanly detect readiness at t=35, past the old 20s window.
        mock_backend.send_keys.assert_called_once_with("sess", "win", "s", enter_count=0)

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.kimi_cli.time")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_no_prompt_exits_at_outer_cap_not_idle_gap(self, mock_get_backend, mock_time):
        """No upgrade dialog ever appears — the idle gap does NOT apply pre-first-prompt.

        should-fix-3 rework: with no dialog ever handled, only the outer cap
        (not the idle-gap boundary at t=20) can end the loop.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            5.0,  # iter1: no dialog handled yet -> idle-gap check skipped, not ready -> sleep
            25.0,  # iter2: still no dialog -> idle-gap check skipped, not ready -> sleep
            61.0,  # iter3: 61>=60 -> outer cap -> return
        ]
        mock_backend.get_history.return_value = "Starting kimi..."

        p = self._make()
        # Mock get_status to return PROCESSING (not ready yet)
        with patch.object(p, "get_status", return_value=TerminalStatus.PROCESSING):
            p._handle_startup_dialog()

        mock_backend.send_keys.assert_not_called()

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.kimi_cli.time")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_first_dialog_later_than_idle_gap_still_handled(self, mock_get_backend, mock_time):
        """A FIRST upgrade dialog later than idle_gap (issue #400 scenario) is caught.

        Before this fix, a first dialog at t=35 (past the 20s default) would
        never be seen -- the loop would have exited at t=20. Now the idle-gap
        clock only starts once a dialog has actually been handled, so a first
        dialog at t=35 is well within the still-open outer cap and is handled;
        the loop then detects readiness on the next poll (t=36) and returns.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            35.0,  # iter1: no dialog handled yet -> idle-gap check skipped ->
            # upgrade dialog found in output -> handled -> continue
            35.0,  # last_prompt_time reset to 35
            36.0,  # iter2: gap=1<20, output -> kimi ready -> return
        ]
        mock_backend.get_history.side_effect = [
            "Skip reminders for version 1.2.3\n[Enter] Upgrade now",
            "Welcome to Kimi!\n💫",
        ]

        p = self._make()
        with patch.object(p, "get_status", return_value=TerminalStatus.IDLE):
            p._handle_startup_dialog()

        mock_backend.send_keys.assert_called_once_with("sess", "win", "s", enter_count=0)

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_server_settings", _outer_cap_settings)
    @patch("cli_agent_orchestrator.providers.kimi_cli.time")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_outer_cap_respected(self, mock_get_backend, mock_time):
        """Loop exits at provider_init_timeout, NOT via the idle gap.

        idle_gap=100 > provider_init_timeout=60, so the idle-gap check can never
        fire. The upgrade prompt is handled once (resetting the timer), then the
        loop idles until t=61 trips the outer cap.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            10.0,  # iter1: upgrade prompt → handled
            10.0,  # last_prompt_time reset
            # continues (gap=15<100, so idle gap never fires)...
            25.0,  # iter2: no new prompt, not ready → sleep
            61.0,  # iter3: now>=60 → return (outer cap)
        ]
        mock_backend.get_history.return_value = (
            "Skip reminders for version 2.0\n[Enter] Upgrade now"
        )

        p = self._make()
        with patch.object(p, "get_status", return_value=TerminalStatus.PROCESSING):
            p._handle_startup_dialog()

        # Upgrade handled once
        mock_backend.send_keys.assert_called_once()

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.kimi_cli.time")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_cascading_prompts_all_handled(self, mock_get_backend, mock_time):
        """Upgrade dialog handled, then ready state detected → exits cleanly.

        Kimi only has one startup dialog type (upgrade), so "cascading" means
        the dialog is handled and then IDLE is detected on the next poll.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            3.0,  # iter1: upgrade prompt → handled
            3.0,  # last_prompt_time reset
            # continues
            8.0,  # iter2: gap=5<20, output → kimi is ready → early return
        ]
        mock_backend.get_history.side_effect = [
            "Skip reminders for version 1.5\n[Enter] Upgrade now",
            "Welcome to Kimi!\n💫",  # ready output
        ]

        p = self._make()
        # get_status called only in iter2 (iter1 continues after handling upgrade)
        with patch.object(p, "get_status", return_value=TerminalStatus.IDLE):
            p._handle_startup_dialog()

        mock_backend.send_keys.assert_called_once()

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.kimi_cli.time")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_idle_gap_resets_on_prompt(self, mock_get_backend, mock_time):
        """Upgrade at t=5 resets timer; at t=22 (gap=17<20) still within window."""
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        # Without reset: t=22-0=22>=20 would exit. With reset: 22-5=17<20.
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            5.0,  # iter1: upgrade prompt → handled
            5.0,  # last_prompt_time reset to 5
            # continues
            22.0,  # iter2: gap=22-5=17<20, check output → ready → return
        ]
        mock_backend.get_history.side_effect = [
            "Skip reminders for version 1.0\n[Enter] Upgrade now",
            "Welcome!\n💫",
        ]

        p = self._make()
        # get_status called only in iter2 (iter1 continues after handling upgrade)
        with patch.object(p, "get_status", return_value=TerminalStatus.IDLE):
            p._handle_startup_dialog()

        # Prompt at t=5 was handled (timer reset enabled continued polling at t=22)
        mock_backend.send_keys.assert_called_once()


# ---------------------------------------------------------------------------
# AntigravityCliProvider
# ---------------------------------------------------------------------------


class TestAntigravityCliIdleGap:
    """Idle-gap semantics in AntigravityCliProvider._handle_startup_dialog."""

    def _make(self):
        return AntigravityCliProvider("t1", "sess", "win")

    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.antigravity_cli.time")
    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_backend")
    def test_late_prompt_handled(self, mock_get_backend, mock_time):
        """A survey at t=35s (past the old 20s window) is still handled.

        Two prompts: trust at t=18 resets the idle timer; the survey at t=35 is
        within idle_gap of that reset (35-18=17 < 20) so it is still answered.
        Under the old fixed-window logic the handler would have exited at t=20 and
        never seen the late survey.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            18.0,  # iter1: gap=18<20, trust prompt → handled
            18.0,  # last_prompt_time reset to 18
            # continues past the old 20s total window...
            35.0,  # iter2: gap=35-18=17<20, survey → handled
            35.0,  # last_prompt_time reset to 35
            # continues
            40.0,  # iter3: gap=40-35=5<20, ready footer → return
        ]
        mock_backend.get_history.side_effect = [
            "Yes, I trust this folder\nrequires permission to read",
            "How's the CLI experience so far?\n[0] Skip",
            "? for shortcuts\n> ",
        ]

        p = self._make()
        p._handle_startup_dialog()

        # Trust at t=18 (Enter) and the late survey at t=35 ("0" + Enter) are both
        # handled — the idle-gap reset kept the loop polling past the old 20s
        # window. Under old logic send_special_key would fire once.
        assert mock_backend.send_special_key.call_count == 2  # trust Enter + survey Enter
        assert mock_backend.send_keys.call_count == 1  # survey "0"

    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.antigravity_cli.time")
    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_backend")
    def test_no_prompt_exits_at_outer_cap_not_idle_gap(self, mock_get_backend, mock_time):
        """No dialog ever appears — the idle gap does NOT apply pre-first-dialog.

        should-fix-3 rework: with no dialog ever handled, only the outer cap
        (not the idle-gap boundary at t=20) can end the loop.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            5.0,  # iter1: no dialog handled yet -> idle-gap check skipped, no ready footer -> sleep
            25.0,  # iter2: still no dialog -> idle-gap check skipped, no ready footer -> sleep
            61.0,  # iter3: 61>=60 -> outer cap -> return
        ]
        mock_backend.get_history.return_value = "Starting agy..."

        p = self._make()
        p._handle_startup_dialog()

        mock_backend.send_special_key.assert_not_called()
        mock_backend.send_keys.assert_not_called()

    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.antigravity_cli.time")
    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_backend")
    def test_first_dialog_later_than_idle_gap_still_handled(self, mock_get_backend, mock_time):
        """A FIRST trust dialog later than idle_gap (issue #400 scenario) is caught.

        Before this fix, a first dialog at t=35 (past the 20s default) would
        never be seen. Now the idle-gap clock only starts once a dialog has
        actually been handled, so a first dialog at t=35 is well within the
        still-open outer cap.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            35.0,  # iter1: no dialog handled yet -> idle-gap check skipped ->
            # trust prompt found -> handled -> continue
            35.0,  # last_prompt_time reset to 35
            36.0,  # iter2: gap=1<20, ready footer -> return
        ]
        mock_backend.get_history.side_effect = [
            "Yes, I trust this folder\nrequires permission to read",
            "? for shortcuts\n> ",
        ]

        p = self._make()
        p._handle_startup_dialog()

        mock_backend.send_special_key.assert_called_once_with("sess", "win", "Enter")

    @patch(
        "cli_agent_orchestrator.providers.antigravity_cli.get_server_settings", _outer_cap_settings
    )
    @patch("cli_agent_orchestrator.providers.antigravity_cli.time")
    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_backend")
    def test_outer_cap_respected(self, mock_get_backend, mock_time):
        """Loop exits at provider_init_timeout, NOT via the idle gap.

        idle_gap=100 > provider_init_timeout=60, so the idle-gap check can never
        fire. Trust is handled once (resetting the timer), then the loop idles
        with no ready footer until t=61 trips the outer cap.
        """
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            10.0,  # iter1: trust prompt → handled
            10.0,  # last_prompt_time reset
            # continues (gap=15<100, so idle gap never fires)...
            25.0,  # iter2: no ready footer, no new dialog → sleep
            61.0,  # iter3: now>=60 → return (outer cap)
        ]
        # Trust prompt text persists in buffer after dismissal
        mock_backend.get_history.return_value = (
            "Yes, I trust this folder\nrequires permission to read"
        )

        p = self._make()
        p._handle_startup_dialog()

        # Trust handled once (trust_done flag prevents re-handling)
        assert mock_backend.send_special_key.call_count == 1

    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.antigravity_cli.time")
    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_backend")
    def test_cascading_prompts_all_handled(self, mock_get_backend, mock_time):
        """Trust dialog then survey — both handled in sequence."""
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            3.0,  # iter1: trust prompt → handled
            3.0,  # last_prompt_time reset
            # continues
            8.0,  # iter2: survey prompt → handled
            8.0,  # last_prompt_time reset
            # continues
            14.0,  # iter3: ready footer → return
        ]
        mock_backend.get_history.side_effect = [
            "Yes, I trust this folder\nrequires permission",
            "How's the CLI experience so far?\n[1] Good [2] Fine [3] Bad [0] Skip",
            "? for shortcuts\n> ",
        ]

        p = self._make()
        p._handle_startup_dialog()

        # Trust: send_special_key (Enter)
        # Survey: send_keys ("0") + send_special_key (Enter)
        assert mock_backend.send_special_key.call_count == 2
        assert mock_backend.send_keys.call_count == 1

    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_server_settings", _settings)
    @patch("cli_agent_orchestrator.providers.antigravity_cli.time")
    @patch("cli_agent_orchestrator.providers.antigravity_cli.get_backend")
    def test_idle_gap_resets_on_each_prompt(self, mock_get_backend, mock_time):
        """Trust at t=5 resets timer; survey at t=22 (gap=17<20) still within window."""
        mock_time.sleep = MagicMock()
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        # Without reset: 22-0=22>=20 would exit before seeing survey.
        # With reset: 22-5=17<20, so survey is still polled.
        mock_time.monotonic.side_effect = [
            0.0,  # outer_deadline = 60
            0.0,  # last_prompt_time = 0
            5.0,  # iter1: trust prompt → handled
            5.0,  # last_prompt_time reset to 5
            # continues
            22.0,  # iter2: gap=22-5=17<20, survey → handled
            22.0,  # last_prompt_time reset to 22
            # continues
            30.0,  # iter3: gap=30-22=8<20, ready footer → return
        ]
        mock_backend.get_history.side_effect = [
            "Yes, I trust this folder\nrequires permission",
            "How's the CLI experience so far?\n[0] Skip",
            "? for shortcuts\n> ",
        ]

        p = self._make()
        p._handle_startup_dialog()

        # Both dialogs handled
        assert mock_backend.send_special_key.call_count == 2  # trust Enter + survey Enter
        assert mock_backend.send_keys.call_count == 1  # survey "0"
