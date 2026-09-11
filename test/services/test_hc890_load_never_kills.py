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
        with patch("cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(2.0, 2.0, 2.0)), \
             patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4):
            # load 2 on 4 cores -> factor clamps to 1.0 -> unchanged.
            assert load_scaled_timeout(60.0) == 60.0

    def test_scales_up_under_contention(self):
        with patch("cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(8.0, 8.0, 8.0)), \
             patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4):
            # load 8 on 4 cores -> factor 2.0 -> 120s. It only ever EXTENDS.
            assert load_scaled_timeout(60.0) == 120.0

    def test_capped_by_max_factor(self):
        with patch.dict("os.environ", {"CAO_INIT_TIMEOUT_LOAD_MAX_FACTOR": "3"}), \
             patch("cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(40.0, 40.0, 40.0)), \
             patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4):
            # load/cores = 10, capped at 3 -> 180s (not 600s).
            assert load_scaled_timeout(60.0) == 180.0

    def test_disabled_when_max_factor_le_one(self):
        with patch.dict("os.environ", {"CAO_INIT_TIMEOUT_LOAD_MAX_FACTOR": "1"}), \
             patch("cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(99.0, 99.0, 99.0)), \
             patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4):
            assert load_scaled_timeout(60.0) == 60.0

    def test_getloadavg_unavailable_falls_back_to_base(self):
        with patch("cli_agent_orchestrator.providers.base.os.getloadavg", side_effect=OSError):
            assert load_scaled_timeout(60.0) == 60.0

    def test_get_init_timeout_extends_under_load(self):
        prov = MagicMock(spec=BaseProvider)
        with patch("cli_agent_orchestrator.services.settings_service.get_server_settings",
                   return_value={"provider_init_timeout": 60}), \
             patch("cli_agent_orchestrator.providers.base.os.getloadavg", return_value=(8.0, 8.0, 8.0)), \
             patch("cli_agent_orchestrator.providers.base.os.cpu_count", return_value=4):
            # Call the real method against a bare mock instance.
            assert BaseProvider.get_init_timeout(prov, None) == 120


class TestStatusMonitorTOCTOU:
    """harness-control#890 TOCTOU: a terminal deleted between event-enqueue and processing must not
    surface as a crash on the StatusMonitor loop."""

    def test_process_chunk_swallows_terminal_gone(self):
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        mon = StatusMonitor()
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm:
            pm.get_provider.side_effect = ValueError("Terminal gone123 not found in database")
            # Must NOT raise -- the chunk is dropped quietly.
            mon._process_chunk("gone123", "some output chunk")

    def test_process_chunk_still_propagates_other_errors(self):
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        mon = StatusMonitor()
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as pm:
            pm.get_provider.side_effect = RuntimeError("real bug")
            with pytest.raises(RuntimeError, match="real bug"):
                mon._process_chunk("t1", "chunk")
