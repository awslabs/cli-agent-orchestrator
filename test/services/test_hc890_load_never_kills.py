"""harness-control#890 (operator invariant, 2026-08-15): "no amount of CPU load may tear down a
live process/session." A provider init/settle TIMEOUT is a performance signal, not liveness loss --
the tmux pane and CLI process are alive, just slow to settle on a contended box. These tests pin the
behavior that a TimeoutError during create_terminal's synchronous initialize() KEEPS the live pane
(never kill_session / kill_window / db_delete_terminal) and reports UNKNOWN, while a genuine
(non-timeout) failure still tears down. RED before the fix (TimeoutError fell through to the
except-block teardown and re-raised); GREEN after.

Also covers the load-aware timeout scaling (load is answered by WAITING LONGER, not killing) and the
StatusMonitor TOCTOU crash-harden.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider, load_scaled_timeout
from cli_agent_orchestrator.services.terminal_service import create_terminal

_TS = "cli_agent_orchestrator.services.terminal_service"


class TestInitTimeoutKeepsLivePane:
    """The core invariant: a settle/init timeout must never kill a live pane."""

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_new_session_init_timeout_keeps_pane_reports_unknown(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        # The provider's cold start blows its (load-scaled) budget under contention.
        mock_provider.initialize.side_effect = TimeoutError(
            "Claude Code initialization timed out after 60s"
        )
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        # The pane's foreground command moved off the pre-init shell: the CLI launched and is
        # still running, which is what makes this a PERFORMANCE timeout rather than a dead
        # launch. See TestInitTimeoutRequiresPaneLiveness for that distinction itself.
        mock_tmux.get_pane_current_command.side_effect = ["zsh"] + ["kiro-cli"] * 8

        result = await create_terminal("kiro_cli", "developer", new_session=True)

        # INVARIANT: the live pane/session survives; nothing is torn down.
        mock_tmux.kill_session.assert_not_called()
        mock_tmux.kill_window.assert_not_called()
        mock_db_delete.assert_not_called()
        # And it is honestly reported as not-yet-ready (UNKNOWN), not a fake IDLE.
        assert result.id == "test1234"
        assert result.status == TerminalStatus.UNKNOWN

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_existing_session_init_timeout_keeps_window(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        """new_session=False (window added to a live session): the harness-control#186 kill_window
        path must NOT fire on a mere timeout -- that window is a live pane, its neighbours in the
        session are live too."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = True
        mock_tmux.create_window.return_value = "developer-abcd"
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.side_effect = TimeoutError("timed out after 60s")
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        mock_tmux.get_pane_current_command.side_effect = ["zsh"] + ["kiro-cli"] * 8

        result = await create_terminal("kiro_cli", "developer", session_name="cao-existing")

        mock_tmux.kill_window.assert_not_called()
        mock_tmux.kill_session.assert_not_called()
        mock_db_delete.assert_not_called()
        assert result.status == TerminalStatus.UNKNOWN

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_non_timeout_failure_still_tears_down(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        """Regression guard: a GENUINE failure (crash / bad profile / backend error, not a perf
        timeout) still cleans up, so we don't leak orphan windows for real faults."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.side_effect = RuntimeError("provider crashed on launch")
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        with pytest.raises(RuntimeError, match="crashed"):
            await create_terminal("kiro_cli", "developer", new_session=True)

        mock_tmux.kill_session.assert_called_once()
        mock_db_delete.assert_called_once()


class TestLoadScaledTimeout:
    """Load is answered by WAITING LONGER (degrade throughput), never by shortening/killing."""

    def test_noop_at_or_below_full_utilization(self):
        with (
            patch(
                "cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(2.0, 2.0, 2.0)
            ),
            patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4),
        ):
            # load 2 on 4 cores -> factor clamps to 1.0 -> unchanged.
            assert load_scaled_timeout(60.0) == 60.0

    def test_scales_up_under_contention(self):
        with (
            patch(
                "cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(8.0, 8.0, 8.0)
            ),
            patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4),
        ):
            # load 8 on 4 cores -> factor 2.0 -> 120s. It only ever EXTENDS.
            assert load_scaled_timeout(60.0) == 120.0

    def test_capped_by_max_factor(self):
        with (
            patch.dict("os.environ", {"CAO_INIT_TIMEOUT_LOAD_MAX_FACTOR": "3"}),
            patch(
                "cli_agent_orchestrator.providers.base.os.getloadavg",
                return_value=(40.0, 40.0, 40.0),
            ),
            patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4),
        ):
            # load/cores = 10, capped at 3 -> 180s (not 600s).
            assert load_scaled_timeout(60.0) == 180.0

    def test_disabled_when_max_factor_le_one(self):
        with (
            patch.dict("os.environ", {"CAO_INIT_TIMEOUT_LOAD_MAX_FACTOR": "1"}),
            patch(
                "cli_agent_orchestrator.providers.base.os.getloadavg",
                return_value=(99.0, 99.0, 99.0),
            ),
            patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4),
        ):
            assert load_scaled_timeout(60.0) == 60.0

    def test_getloadavg_unavailable_falls_back_to_base(self):
        with patch("cli_agent_orchestrator.providers.base.os.getloadavg", side_effect=OSError):
            assert load_scaled_timeout(60.0) == 60.0

    def test_get_init_timeout_extends_under_load(self):
        prov = MagicMock(spec=BaseProvider)
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.get_server_settings",
                return_value={"provider_init_timeout": 60},
            ),
            patch(
                "cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(8.0, 8.0, 8.0)
            ),
            patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4),
        ):
            # Call the real method against a bare mock instance.
            assert BaseProvider.get_init_timeout(prov, None) == 120


class TestStatusMonitorTOCTOU:
    """harness-control#890 TOCTOU: a terminal deleted between event-enqueue and processing must not
    surface as a crash on the StatusMonitor loop."""

    def test_process_chunk_swallows_terminal_gone(self):
        from cli_agent_orchestrator.providers.base import UnknownTerminalError
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        mon = StatusMonitor()
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm:
            # UnknownTerminalError, not a bare ValueError: see
            # TestStatusMonitorDistinguishesProviderFaults for why the handler had to narrow.
            pm.get_provider.side_effect = UnknownTerminalError(
                "Terminal gone123 not found in database"
            )
            # Must NOT raise -- the chunk is dropped quietly.
            mon._process_chunk("gone123", "some output chunk")

    def test_process_chunk_still_propagates_other_errors(self):
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        mon = StatusMonitor()
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm:
            pm.get_provider.side_effect = RuntimeError("real bug")
            with pytest.raises(RuntimeError, match="real bug"):
                mon._process_chunk("t1", "chunk")


class TestInitTimeoutRequiresPaneLiveness:
    """PR #623 review (Copilot, ``terminal_service.py:522``): a provider ``TimeoutError`` does not
    by itself prove the pane is alive.

    ``ClaudeCodeProvider.initialize()`` raises the very same exception type for a
    "genuinely broken/unrecognized launch", and its in-place comment says it picked ``TimeoutError``
    precisely so that case keeps the clean teardown "rather than leaving an unreapable worker alive
    in UNKNOWN status". Keeping every timing-out terminal overrode that for every provider.

    The keep-alive is now gated on POSITIVE evidence: the pane's foreground command must have moved
    off the pre-init shell, i.e. the CLI really did launch and is still running. No evidence ->
    the pre-#890 rollback path, unchanged.
    """

    @staticmethod
    def _pane_commands(mock_tmux, *sequence):
        """Feed ``get_pane_current_command`` a sequence, repeating the last value forever.

        A plain ``side_effect`` list raises StopIteration if anything calls the probe one more
        time than the test predicted, which would make these tests fail for a reason unrelated to
        what they assert.
        """
        values = list(sequence)

        def _next(*_args, **_kwargs):
            return values.pop(0) if len(values) > 1 else values[0]

        mock_tmux.get_pane_current_command.side_effect = _next

    def _wire(
        self,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_load_profile,
        mock_provider_manager,
        mock_fifo_dir,
    ):
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.side_effect = TimeoutError(
            "Claude Code initialization timed out after 60s"
        )
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        return mock_provider

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_timeout_with_the_cli_still_running_keeps_the_pane(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        """The #890 case: ``claude`` is in the pane's foreground, it is just slow to settle."""
        self._wire(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_load_profile,
            mock_provider_manager,
            mock_fifo_dir,
        )
        # zsh before initialize(), claude after -- the CLI launched and is still running.
        self._pane_commands(mock_tmux, "zsh", "claude")

        result = await create_terminal("kiro_cli", "developer", new_session=True)

        mock_tmux.kill_session.assert_not_called()
        mock_tmux.kill_window.assert_not_called()
        mock_db_delete.assert_not_called()
        assert result.status == TerminalStatus.UNKNOWN

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_timeout_with_the_pane_back_at_the_shell_still_tears_down(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        """The broken-launch case Copilot named: the CLI never started, or started and exited, so
        the pane's foreground command is still the pre-init shell. Nothing live to protect ->
        the exception propagates and the pre-#890 rollback runs, exactly as on main."""
        self._wire(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_load_profile,
            mock_provider_manager,
            mock_fifo_dir,
        )
        self._pane_commands(mock_tmux, "zsh")

        with pytest.raises(TimeoutError):
            await create_terminal("kiro_cli", "developer", new_session=True)

        mock_tmux.kill_session.assert_called_once()
        mock_db_delete.assert_called_once()

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_timeout_with_no_liveness_evidence_tears_down(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        """A backend that cannot report a foreground command (``None``) is ABSENCE of evidence,
        not evidence of life: keep-alive is opt-in, so this falls through to rollback rather than
        protecting an unverified pane."""
        self._wire(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_load_profile,
            mock_provider_manager,
            mock_fifo_dir,
        )
        self._pane_commands(mock_tmux, None)

        with pytest.raises(TimeoutError):
            await create_terminal("kiro_cli", "developer", new_session=True)

        mock_tmux.kill_session.assert_called_once()
        mock_db_delete.assert_called_once()

    @pytest.mark.asyncio
    @patch(f"{_TS}.db_delete_terminal")
    @patch(f"{_TS}.status_monitor")
    @patch(f"{_TS}.fifo_manager")
    @patch(f"{_TS}.FIFO_DIR")
    @patch(f"{_TS}.provider_manager")
    @patch(f"{_TS}.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch(f"{_TS}.generate_window_name")
    @patch(f"{_TS}.generate_session_name")
    @patch(f"{_TS}.generate_terminal_id")
    @patch(f"{_TS}.load_agent_profile")
    async def test_timeout_with_a_failing_liveness_probe_tears_down(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
    ):
        """The probe itself raising must not be swallowed into a silent keep-alive either."""
        self._wire(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_load_profile,
            mock_provider_manager,
            mock_fifo_dir,
        )
        # First call (pre-init baseline) and every later one raise; create_terminal's own capture
        # degrades the baseline to None and the post-timeout probe reports no evidence.
        mock_tmux.get_pane_current_command.side_effect = RuntimeError("tmux gone")

        with pytest.raises(TimeoutError):
            await create_terminal("kiro_cli", "developer", new_session=True)

        mock_tmux.kill_session.assert_called_once()
        mock_db_delete.assert_called_once()


class TestEnvFloatRejectsUnusableValues:
    """PR #623 review (Copilot, ``providers/base.py:60``): ``float()`` accepts ``inf``/``nan``,
    which bypass the fallback ``_env_float`` promises.

    ``CAO_INPUT_READY_TIMEOUT=inf`` makes ``time.monotonic() + timeout`` an infinite deadline, so a
    pane that never matches is waited on FOREVER; ``nan`` makes every ``<`` comparison against that
    deadline False, so the same loop expires immediately. A negative duration is a deadline already
    in the past -- the same class of bug, rejected here too.
    """

    @pytest.mark.parametrize("raw", ["inf", "Infinity", "-inf", "nan", "NaN", "-1", "-0.5"])
    def test_unusable_values_fall_back_to_the_default(self, raw):
        from cli_agent_orchestrator.providers.base import _env_float

        with patch.dict("os.environ", {"CAO_INPUT_READY_TIMEOUT": raw}):
            assert _env_float("CAO_INPUT_READY_TIMEOUT", 5.0) == 5.0

    @pytest.mark.parametrize(
        "raw,expected", [("12.5", 12.5), ("0", 0.0), ("  7  ", 7.0), ("1e2", 100.0)]
    )
    def test_usable_values_still_win(self, raw, expected):
        from cli_agent_orchestrator.providers.base import _env_float

        with patch.dict("os.environ", {"CAO_INPUT_READY_TIMEOUT": raw}):
            assert _env_float("CAO_INPUT_READY_TIMEOUT", 5.0) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "5s"])
    def test_unset_blank_and_garbage_fall_back(self, raw):
        from cli_agent_orchestrator.providers.base import _env_float

        with patch.dict("os.environ", {"CAO_INPUT_READY_TIMEOUT": raw}):
            assert _env_float("CAO_INPUT_READY_TIMEOUT", 5.0) == 5.0

    def test_missing_var_falls_back(self):
        import os as _os

        from cli_agent_orchestrator.providers.base import _env_float

        env = {k: v for k, v in _os.environ.items() if k != "CAO_INPUT_READY_TIMEOUT"}
        with patch.dict("os.environ", env, clear=True):
            assert _env_float("CAO_INPUT_READY_TIMEOUT", 5.0) == 5.0

    def test_infinite_max_factor_cannot_make_the_scaled_timeout_unbounded(self):
        """The same hole on the other knob: ``CAO_INIT_TIMEOUT_LOAD_MAX_FACTOR=inf`` removed the
        cap entirely, so a load spike could push one init to an arbitrarily long wait. The
        rejected value falls back to the 6.0 default cap."""
        with (
            patch.dict("os.environ", {"CAO_INIT_TIMEOUT_LOAD_MAX_FACTOR": "inf"}),
            patch(
                "cli_agent_orchestrator.providers.base.os.getloadavg",
                return_value=(400.0, 0.0, 0.0),
            ),
            patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4),
        ):
            assert load_scaled_timeout(60.0) == 360.0  # capped at the default 6.0x, not 6000s

    @pytest.mark.asyncio
    async def test_input_ready_settle_budget_stays_finite_with_inf_in_the_env(self):
        """End-to-end: the settle gate resolves a FINITE deadline even when the env says ``inf``,
        so ``wait_until_input_ready`` cannot hang a never-matching pane forever."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        provider = ClaudeCodeProvider("t1", "sess", "win")
        with (
            patch.dict("os.environ", {"CAO_INPUT_READY_TIMEOUT": "inf"}),
            patch("cli_agent_orchestrator.backends.registry._backend") as mock_backend,
        ):
            mock_backend.get_history.return_value = "nothing that matches an input box"
            # Would never return if the deadline were infinite.
            assert await provider.wait_until_input_ready() is False


class TestStatusMonitorDistinguishesProviderFaults:
    """PR #623 review (Copilot, ``status_monitor.py:146``): the TOCTOU handler was a bare
    ``except ValueError``, which also swallowed genuine provider-CREATION faults.

    ``ProviderManager.get_provider`` raises plain ``ValueError`` for an unknown persisted provider
    type, a Kiro row with no ``agent_profile``, and ``resume_session_id`` on a non-claude_code
    provider. Swallowing those drops the terminal's output and freezes its status with nothing
    logged above DEBUG. Only the narrower ``UnknownTerminalError`` means "the terminal is gone".
    """

    def _monitor(self):
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        return StatusMonitor()

    def test_terminal_gone_is_dropped_quietly(self):
        from cli_agent_orchestrator.providers.base import UnknownTerminalError

        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm:
            pm.get_provider.side_effect = UnknownTerminalError(
                "Terminal gone123 not found in database"
            )
            self._monitor()._process_chunk("gone123", "some output chunk")

    def test_provider_creation_fault_is_not_swallowed(self):
        """The regression this narrows: a real fault must stay visible instead of being
        indistinguishable from a deleted terminal."""
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm:
            pm.get_provider.side_effect = ValueError("Unknown provider type: bogus_cli")
            with pytest.raises(ValueError, match="Unknown provider type"):
                self._monitor()._process_chunk("t1", "chunk")

    def test_unknown_terminal_error_is_still_a_value_error(self):
        """Subclassing ``ValueError`` is load-bearing: every pre-existing ``except ValueError``
        caller of ``get_provider`` must keep working unchanged."""
        from cli_agent_orchestrator.providers.base import UnknownTerminalError

        assert issubclass(UnknownTerminalError, ValueError)

    def test_manager_raises_the_narrow_type_for_a_missing_row(self):
        from cli_agent_orchestrator.providers.base import UnknownTerminalError
        from cli_agent_orchestrator.providers.manager import ProviderManager

        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata", return_value=None
        ):
            with pytest.raises(UnknownTerminalError):
                ProviderManager().get_provider("gone123")

    def test_manager_raises_a_plain_value_error_for_an_unknown_provider_type(self):
        from cli_agent_orchestrator.providers.base import UnknownTerminalError
        from cli_agent_orchestrator.providers.manager import ProviderManager

        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": "bogus_cli",
                "tmux_session": "s",
                "tmux_window": "w",
                "agent_profile": None,
            },
        ):
            with pytest.raises(ValueError) as excinfo:
                ProviderManager().get_provider("t1")
        assert not isinstance(excinfo.value, UnknownTerminalError)


class TestDeferredInitTimeoutKeepsWorkerAndDeliversTheTask:
    """Two review findings meet in the deferred path.

    anilkmr-a2z, ``[FIX] Deferred-init TimeoutError handler lacks test coverage``: the synchronous
    handler was tested, its deferred twin was not.

    Copilot, ``terminal_service.py:1030``: the deferred handler told the caller the worker "will
    pick up the task once it settles" -- but ``initialize()`` raised BEFORE the ``send_input`` that
    delivers ``initial_message``, so nothing ever handed the task over and a supervisor waiting on
    the resulting callback waits forever. The worker is now kept alive AND the task is delivered
    once it settles; if it never settles the caller is told so plainly.
    """

    @staticmethod
    async def _run_deferred(provider_instance, initial_message="do the task"):
        from cli_agent_orchestrator.services import terminal_service

        before = set(terminal_service._deferred_init_tasks)
        terminal_service._schedule_deferred_init(
            provider_instance,
            "worker99",
            initial_message,
            None,
            None,
            pre_init_command="zsh",
        )
        (task,) = set(terminal_service._deferred_init_tasks) - before
        await task

    @staticmethod
    def _timed_out_provider():
        provider_instance = AsyncMock()
        provider_instance.initialize.side_effect = TimeoutError(
            "Claude Code initialization timed out after 60s"
        )
        provider_instance.session_name = "cao-session"
        provider_instance.window_name = "developer-abcd"
        return provider_instance

    @pytest.mark.asyncio
    @patch(f"{_TS}._notify_caller_of_deferred_failure")
    @patch(f"{_TS}._confirm_worker_started_or_resubmit")
    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.wait_until_status")
    @patch(f"{_TS}.get_terminal_metadata")
    @patch(f"{_TS}._init_timeout_left_a_live_cli", return_value=True)
    async def test_task_is_delivered_once_the_slow_worker_settles(
        self, _mock_live, mock_meta, mock_wait, mock_send, mock_confirm, mock_notify
    ):
        """The hole Copilot found: without the retry the worker is kept alive holding NO task.
        Assert the task actually reaches it, and that the caller is not sent a failure notice."""
        mock_meta.return_value = {"caller_id": "super123"}
        mock_wait.return_value = True  # the CLI finished settling on its own
        mock_confirm.return_value = True  # ...and started the task

        await self._run_deferred(self._timed_out_provider())

        mock_send.assert_called_once()
        assert mock_send.call_args.args[0] == "worker99"
        assert mock_send.call_args.args[1] == "do the task"
        assert mock_send.call_args.kwargs["sender_id"] == "super123"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    @patch(f"{_TS}._notify_caller_of_deferred_failure")
    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.wait_until_status")
    @patch(f"{_TS}.get_terminal_metadata")
    @patch(f"{_TS}._init_timeout_left_a_live_cli", return_value=True)
    async def test_worker_that_never_settles_is_kept_but_the_caller_is_told_the_truth(
        self, _mock_live, mock_meta, mock_wait, mock_send, mock_notify
    ):
        """No silent lie and no teardown: the worker stays alive (delete_worker=False) and the
        message says the task was NOT delivered, rather than promising it is queued."""
        mock_meta.return_value = {"caller_id": "super123"}
        mock_wait.return_value = False  # never reached IDLE/COMPLETED

        await self._run_deferred(self._timed_out_provider())

        mock_send.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False
        message = mock_notify.call_args.args[1]
        assert "NOT delivered" in message
        assert "re-send the task yourself" in message

    @pytest.mark.asyncio
    @patch(f"{_TS}._notify_caller_of_deferred_failure")
    @patch(f"{_TS}.get_terminal_metadata")
    @patch(f"{_TS}._init_timeout_left_a_live_cli", return_value=False)
    async def test_timeout_with_no_live_cli_still_tears_the_worker_down(
        self, _mock_live, mock_meta, mock_notify
    ):
        """The deferred twin of the synchronous narrowing: a timeout that left nothing running is
        a failed launch, not a slow one, and must not leave an unreapable UNKNOWN worker."""
        mock_meta.return_value = {"caller_id": "super123"}

        await self._run_deferred(self._timed_out_provider())

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is True
        assert "failed to initialize" in mock_notify.call_args.args[1]

    @pytest.mark.asyncio
    @patch(f"{_TS}._notify_caller_of_deferred_failure")
    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.wait_until_status")
    @patch(f"{_TS}.get_terminal_metadata")
    @patch(f"{_TS}._init_timeout_left_a_live_cli", return_value=True)
    async def test_no_initial_message_means_nothing_to_deliver(
        self, _mock_live, mock_meta, mock_wait, mock_send, mock_notify
    ):
        """A deferred create with no task attached (POST /sessions without initial_message): keep
        the worker, do not invent a delivery, and do not claim a task is pending."""
        mock_meta.return_value = {"caller_id": None}

        await self._run_deferred(self._timed_out_provider(), initial_message=None)

        mock_wait.assert_not_called()
        mock_send.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False
        assert "no task was assigned" in mock_notify.call_args.args[1]

    @pytest.mark.asyncio
    @patch(f"{_TS}._notify_caller_of_deferred_failure")
    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.wait_until_status")
    @patch(f"{_TS}.get_terminal_metadata")
    @patch(f"{_TS}._init_timeout_left_a_live_cli", return_value=True)
    async def test_a_failing_redelivery_does_not_escape_as_an_unhandled_task_error(
        self, _mock_live, mock_meta, mock_wait, mock_send, mock_notify
    ):
        """The retry runs inside an ``except`` block, so anything it raises would escape ``_run``
        entirely and surface as an unhandled asyncio task exception with the caller told nothing.
        It is contained, and the caller still gets the not-delivered notice."""
        mock_meta.return_value = {"caller_id": "super123"}
        mock_wait.return_value = True
        mock_send.side_effect = RuntimeError("tmux paste failed")

        await self._run_deferred(self._timed_out_provider())

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False
        assert "NOT delivered" in mock_notify.call_args.args[1]
