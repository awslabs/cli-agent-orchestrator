"""Codex exits back to the shell while CAO is still initializing (#1018).

The reporter's pane, verbatim apart from the user and host names::

      >_ OpenAI Codex (v0.162.1)
    › Error: account/read failed during TUI bootstrap: account/read failed: workspace routing discovery timed out (code -32603)
    xiaowo@host:~/reviews$

Codex had exited with status 1. Two readings of that pane were wrong on main:

1. ``_status_of_frame`` saw the ``›`` glyph on the error line and answered IDLE,
   and ``_get_status`` only consulted the pane's foreground process once
   ``_initialized`` was already true -- so during startup nothing could veto
   the IDLE verdict, ``initialize()``'s readiness wait accepted it, and the
   assigned task was pasted into bash, which executed every line of it.
2. ``initialize()`` itself never looked at the pane's foreground process after
   capturing ``shell_baseline``, so a Codex that died between the startup
   handler settling and readiness being declared was indistinguishable from
   one that was running.

These tests drive ``initialize()`` and ``get_status()`` with that pane. None of
them launch codex.
"""

from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderExitedError
from cli_agent_orchestrator.providers.codex import CodexProvider

BANNER = "  >_ OpenAI Codex (v0.162.1)\n"
# The composer Codex drew before the account read failed. The startup handler
# settles on this frame, exactly as it did for the reporter.
COMPOSER_FRAME = BANNER + "› \n"
# The pane after Codex exited: its own error line, then the bash prompt.
EXITED_FRAME = (
    BANNER + "› Error: account/read failed during TUI bootstrap: account/read failed: "
    "workspace routing discovery timed out (code -32603)\n"
    "user@host:~/reviews$ \n"
)


def pane_commands(*commands: str):
    """A ``get_pane_current_command`` side effect that yields ``commands`` then repeats the last.

    The first read is ``initialize()``'s baseline capture (the shell); later
    reads are exit checks. Repeating the final value keeps an unexpected extra
    read from raising StopIteration inside ``asyncio.to_thread``, where it
    would hang the test instead of failing it.
    """
    remaining = list(commands)

    def _next(*_args, **_kwargs):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _next


def frames(*sequence: str):
    """A ``get_history`` side effect that yields ``sequence`` then repeats its last frame."""
    remaining = list(sequence)

    def _next(*_args, **_kwargs):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _next


class TestCodexExitDuringInitialize:
    """``initialize()`` must fail, and type nothing, when codex is gone at readiness."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_exit_after_readiness_wait_raises_and_does_not_mark_initialized(
        self, mock_backend, mock_wait_shell, mock_wait_status, mock_sleep
    ):
        """The reporter's timeline: the monitor said idle, but the pane is bash again."""
        mock_wait_shell.return_value = True
        # The status monitor read the `› Error:` line as an idle composer.
        mock_wait_status.return_value = True
        backend = mock_backend.return_value
        backend.get_history.side_effect = frames(COMPOSER_FRAME, EXITED_FRAME)
        # Baseline capture sees bash; every later read sees bash again: codex exited.
        backend.get_pane_current_command.side_effect = pane_commands("bash", "bash")

        provider = CodexProvider("1e293d67", "test-session", "window-0", None)

        with pytest.raises(ProviderExitedError, match="exited"):
            await provider.initialize()

        assert provider._initialized is False
        # Only the warm-up echo and the codex launch were typed; no task text.
        assert backend.send_keys.call_count == 2
        typed = [call.args[2] for call in backend.send_keys.call_args_list]
        assert typed[0] == "echo ready"
        assert typed[1].startswith("codex ")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "monitor_verdict",
        [TerminalStatus.IDLE, TerminalStatus.PROCESSING],
        ids=["monitor-misread-idle", "monitor-never-ready"],
    )
    @patch("cli_agent_orchestrator.utils.terminal.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_exit_while_waiting_fails_on_the_first_poll(
        self,
        mock_backend,
        mock_wait_shell,
        mock_sleep,
        mock_monitor,
        mock_wait_sleep,
        monitor_verdict,
    ):
        """The real readiness wait, with the pane back at the shell, ends on its first poll.

        Whatever the monitor says about the frame -- the reporter's misread IDLE,
        or a PROCESSING that would otherwise have burned the whole
        ``provider_init_timeout`` (60 s by default) before a TimeoutError blamed
        slowness -- the exit check runs after every poll and ends startup with
        the real reason.
        """
        mock_wait_shell.return_value = True
        mock_monitor.get_status.return_value = monitor_verdict
        backend = mock_backend.return_value
        backend.get_history.side_effect = frames(COMPOSER_FRAME, EXITED_FRAME)
        backend.get_pane_current_command.side_effect = pane_commands("bash", "bash")

        provider = CodexProvider("1e293d67", "test-session", "window-0", None)

        with pytest.raises(ProviderExitedError):
            await provider.initialize()

        assert mock_monitor.get_status.call_count == 1
        assert provider._initialized is False
        assert backend.send_keys.call_count == 2

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_slow_but_alive_codex_still_times_out_with_the_old_message(
        self, mock_backend, mock_wait_shell, mock_wait_status, mock_sleep
    ):
        """The exit hook must not change the verdict for a codex that is merely slow."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        backend = mock_backend.return_value
        backend.get_history.return_value = COMPOSER_FRAME
        backend.get_pane_current_command.side_effect = pane_commands("bash", "codex")

        provider = CodexProvider("1e293d67", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Codex initialization timed out"):
            await provider.initialize()

        # One wait, the configured bound, and the exit check wired in as its poll hook.
        mock_wait_status.assert_called_once()
        assert mock_wait_status.call_args.kwargs["timeout"] > 0
        hook = mock_wait_status.call_args.kwargs["on_poll"]
        assert hook.__self__ is provider
        assert hook.__name__ == "_raise_if_exited_during_startup"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_alive_codex_initializes(
        self, mock_backend, mock_wait_shell, mock_wait_status, mock_sleep
    ):
        """The happy path: the pane's foreground process is codex from launch to readiness."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        backend = mock_backend.return_value
        backend.get_history.return_value = COMPOSER_FRAME
        backend.get_pane_current_command.side_effect = pane_commands("bash", "codex")

        provider = CodexProvider("1e293d67", "test-session", "window-0", None)

        assert await provider.initialize() is True
        assert provider._initialized is True
        assert provider.shell_baseline == "bash"


class TestCodexReadinessVerdictOnBareShell:
    """A ready verdict is vetoed by the pane's foreground process, initialized or not."""

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_exited_pane_is_error_before_initialization_completes(self, mock_backend):
        """The reporter's pane with the pane command back at bash: ERROR, not IDLE.

        This is the verdict ``wait_until_status`` polls during ``initialize()``;
        on main it was IDLE because the exit check was gated on ``_initialized``.
        """
        mock_backend.return_value.get_pane_current_command.return_value = "bash"
        provider = CodexProvider("1e293d67", "test-session", "window-0")
        provider.shell_baseline = "bash"
        provider._launched = True
        assert provider._initialized is False

        assert provider.get_status(EXITED_FRAME) == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_live_composer_before_initialization_is_still_idle(self, mock_backend):
        """The veto needs the pane command to match the baseline; a running codex is not vetoed."""
        mock_backend.return_value.get_pane_current_command.return_value = "codex"
        provider = CodexProvider("1e293d67", "test-session", "window-0")
        provider.shell_baseline = "bash"
        provider._launched = True

        assert provider.get_status(COMPOSER_FRAME) == TerminalStatus.IDLE

    # A `❯` prompt theme (pure, starship, powerlevel10k) matches the idle
    # composer glyph, so the shell's own prompt reads as a ready frame.
    FANCY_PROMPT_FRAME = "$ echo ready\nready\n~/proj on main\n❯ \n"

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_fancy_shell_prompt_before_launch_is_not_a_false_error(self, mock_backend):
        """Before the codex command is sent the pane IS the shell; the veto must stay off.

        Review of #1018 (P3-2): with the veto applied from the baseline read
        onward, a `❯` prompt during the 2 s warm-up flipped IDLE to ERROR and a
        status reader saw a short false ERROR before codex drew its composer.
        """
        mock_backend.return_value.get_pane_current_command.return_value = "zsh"
        provider = CodexProvider("1e293d67", "test-session", "window-0")
        provider.shell_baseline = "zsh"
        assert provider._launched is False

        assert provider.get_status(self.FANCY_PROMPT_FRAME) == TerminalStatus.IDLE
        mock_backend.return_value.get_pane_current_command.assert_not_called()

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_fancy_shell_prompt_after_launch_is_an_exit(self, mock_backend):
        """The same frame after the codex command was sent is a codex that exited."""
        mock_backend.return_value.get_pane_current_command.return_value = "zsh"
        provider = CodexProvider("1e293d67", "test-session", "window-0")
        provider.shell_baseline = "zsh"
        provider._launched = True

        assert provider.get_status(self.FANCY_PROMPT_FRAME) == TerminalStatus.ERROR

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_marks_launched_when_the_codex_command_is_sent(
        self, mock_backend, mock_wait_shell, mock_wait_status, mock_sleep
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        backend = mock_backend.return_value
        backend.get_history.return_value = COMPOSER_FRAME
        backend.get_pane_current_command.side_effect = pane_commands("bash", "codex")
        provider = CodexProvider("1e293d67", "test-session", "window-0", None)
        seen: list[bool] = []

        def _send_keys(_session, _window, keys, **_kwargs):
            seen.append(provider._launched)

        backend.send_keys.side_effect = _send_keys

        await provider.initialize()

        # Not launched at the warm-up echo nor at the codex command itself; launched after.
        assert seen == [False, False]
        assert provider._launched is True

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_pre_launch_non_ready_verdict_does_not_read_the_pane(self, mock_backend):
        """Before codex is exec'd the pane IS the shell by construction.

        A non-ready verdict is not one anyone acts on, so it keeps the old gate
        and costs no tmux round trip; only a ready verdict pays for the check.
        """
        provider = CodexProvider("1e293d67", "test-session", "window-0")
        provider.shell_baseline = "bash"

        status = provider.get_status("echo ready\nready\nuser@host:~$ codex --yolo\n")

        assert status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
        mock_backend.return_value.get_pane_current_command.assert_not_called()

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_exited_pane_is_error_after_initialization_too(self, mock_backend):
        """The post-init behaviour main already had is unchanged."""
        mock_backend.return_value.get_pane_current_command.return_value = "bash"
        provider = CodexProvider("1e293d67", "test-session", "window-0")
        provider.shell_baseline = "bash"
        provider._initialized = True

        assert provider.get_status(EXITED_FRAME) == TerminalStatus.ERROR
