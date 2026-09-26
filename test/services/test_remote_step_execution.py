"""A step drives an agent whose pane is in another pod (#745).

``run_agent_step`` is the one create → input → wait → extract → teardown
sequence, and every stage of it but the input send was addressed at THIS host:

- the waits polled ``status_monitor``, a detector reading a rolling buffer that
  is empty for a pane in another pod. It answered UNKNOWN forever, so a step
  timed out at its full budget while the worker had reported COMPLETED minutes
  earlier — and the run was reported as a failure with no output.
- extraction ran tmux capture-pane here, over a scrollback that does not exist.
- teardown sent a graceful exit into nothing, then deleted the central row while
  the worker kept running: a leaked pod still holding a model session.
- prompt redelivery (#562) probed a local pane to decide whether the paste
  landed, and "not visible" is what triggers a full re-send — so a remote
  worker's task could be typed a second time.

Review finding 4 on #802. The routing is the one the HTTP endpoints already do
(``GET /terminals/{id}``, ``GET /terminals/{id}/output``, ``DELETE``); these
tests hold the in-process step path to the same rule.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import CommandType
from cli_agent_orchestrator.services.agent_step import (
    _best_effort_teardown,
    _extract_last_message,
    _wait_for_completion,
    run_agent_step,
)
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils.terminal import effective_status, wait_until_status

_MODULE = "cli_agent_orchestrator.services.agent_step"
REMOTE = "ef38cd1c"
LOCAL = "abc12345"


@pytest.fixture
def placement():
    """Route ``REMOTE`` to a runtime and leave ``LOCAL`` on this host."""
    with (
        patch(
            "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.is_remote",
            side_effect=lambda tid: tid == REMOTE,
        ) as is_remote,
        patch(
            "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.get_status",
            return_value=TerminalStatus.COMPLETED,
        ) as remote_status,
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            return_value=TerminalStatus.IDLE,
        ) as local_status,
    ):
        yield SimpleNamespace(
            is_remote=is_remote, remote_status=remote_status, local_status=local_status
        )


class TestStatusComesFromWhereverThePaneIs:
    def test_a_remote_terminals_status_is_the_one_its_runtime_reported(self, placement):
        assert effective_status(REMOTE) == TerminalStatus.COMPLETED
        placement.local_status.assert_not_called()

    def test_the_local_detector_is_not_consulted_about_another_machine(self, placement):
        """It would answer about the wrong pane, or answer UNKNOWN forever."""
        effective_status(REMOTE)

        placement.remote_status.assert_called_once_with(REMOTE)

    def test_a_local_terminal_still_reads_the_local_detector(self, placement):
        assert effective_status(LOCAL) == TerminalStatus.IDLE
        placement.remote_status.assert_not_called()

    def test_the_readiness_wait_settles_on_a_runtime_reported_status(self, placement):
        reached = asyncio.run(wait_until_status(REMOTE, {TerminalStatus.COMPLETED}, timeout=1.0))

        assert reached is True
        placement.local_status.assert_not_called()


class TestTheCompletionWaitSeesARemoteWorkerFinish:
    def test_a_runtime_reported_completed_ends_the_wait(self, placement):
        """The finding's symptom, directly: before this, the poll read UNKNOWN
        from the local detector and the step burned its whole budget."""
        asyncio.run(_wait_for_completion(REMOTE, timeout=5.0))  # returns, no raise

    def test_an_unknown_remote_status_still_times_out_as_before(self, placement):
        """Routing the read must not turn a genuinely stuck worker into a pass."""
        placement.remote_status.return_value = TerminalStatus.UNKNOWN

        with pytest.raises(Exception, match="did not complete"):
            asyncio.run(_wait_for_completion(REMOTE, timeout=0.2))

    def test_a_remote_worker_is_never_re_prompted_by_a_local_probe(self, placement):
        """Redelivery decides from a local capture-pane. For a remote pane the
        text is never "visible", which is exactly the full-resend trigger — the
        worker would be handed its task twice."""
        placement.remote_status.return_value = TerminalStatus.IDLE

        with (
            patch(
                f"{_MODULE}.terminal_service.redeliver_dropped_message", return_value=False
            ) as redeliver,
            patch(f"{_MODULE}._PROMPT_PICKUP_GRACE", 0.0),
            patch(f"{_MODULE}._COMPLETION_POLL_INTERVAL", 0.01),
        ):
            with pytest.raises(Exception, match="did not complete"):
                asyncio.run(_wait_for_completion(REMOTE, timeout=0.2, prompt="do the task"))

        redeliver.assert_not_called()

    def test_a_local_worker_still_gets_its_prompt_re_delivered(self, placement):
        """#562's recovery is unchanged where the pane really is local."""
        placement.local_status.return_value = TerminalStatus.IDLE

        with (
            patch(
                f"{_MODULE}.terminal_service.redeliver_dropped_message", return_value=False
            ) as redeliver,
            patch(f"{_MODULE}._PROMPT_PICKUP_GRACE", 0.0),
            patch(f"{_MODULE}._COMPLETION_POLL_INTERVAL", 0.01),
        ):
            with pytest.raises(Exception, match="did not complete"):
                asyncio.run(_wait_for_completion(LOCAL, timeout=0.2, prompt="do the task"))

        assert redeliver.called


class TestTheAnswerIsExtractedWhereTheTranscriptIs:
    def test_a_remote_step_asks_the_runtime_to_extract(self, placement):
        sent = {}

        async def fake_command(terminal_id, command_type, payload, timeout=None):
            sent["args"] = (terminal_id, command_type, payload)
            return SimpleNamespace(payload={"output": "757"})

        with (
            patch(
                "cli_agent_orchestrator.runtime_channel.api.remote_terminal_command",
                side_effect=fake_command,
            ),
            patch(f"{_MODULE}.terminal_service.get_output") as local_extract,
        ):
            answer = asyncio.run(_extract_last_message(REMOTE))

        assert answer == "757"
        assert sent["args"] == (REMOTE, CommandType.EXTRACT, {"mode": OutputMode.LAST.value})
        local_extract.assert_not_called()

    def test_a_local_step_still_captures_its_own_pane(self, placement):
        with patch(
            f"{_MODULE}.terminal_service.get_output", return_value="the answer"
        ) as local_extract:
            answer = asyncio.run(_extract_last_message(LOCAL))

        assert answer == "the answer"
        local_extract.assert_called_once_with(LOCAL, OutputMode.LAST)


class TestTeardownReachesThePodThatHoldsTheSession:
    def test_a_remote_terminal_is_torn_down_over_the_channel(self, placement):
        """The local sequence would exit nothing and then drop the central row,
        leaving the worker pod running with the row that named it gone."""
        with (
            patch(
                "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal",
                new=AsyncMock(return_value=True),
            ) as remote_delete,
            patch(f"{_MODULE}.terminal_service.exit_terminal_cli") as exit_cli,
            patch(f"{_MODULE}.terminal_service.delete_terminal") as delete,
        ):
            asyncio.run(_best_effort_teardown(REMOTE, None))

        remote_delete.assert_awaited_once_with(REMOTE)
        exit_cli.assert_not_called()
        delete.assert_not_called()

    def test_a_failed_remote_teardown_is_still_best_effort(self, placement):
        """Teardown must never turn a settled step into a failure."""
        with patch(
            "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal",
            new=AsyncMock(side_effect=RuntimeError("channel closed")),
        ):
            asyncio.run(_best_effort_teardown(REMOTE, None))  # no raise

    def test_a_local_terminal_keeps_the_exit_then_delete_sequence(self, placement):
        with (
            patch(f"{_MODULE}.terminal_service.exit_terminal_cli") as exit_cli,
            patch(f"{_MODULE}.terminal_service.delete_terminal") as delete,
            patch(
                "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal",
                new=AsyncMock(),
            ) as remote_delete,
        ):
            asyncio.run(_best_effort_teardown(LOCAL, None))

        exit_cli.assert_called_once_with(LOCAL)
        delete.assert_called_once_with(LOCAL, registry=None)
        remote_delete.assert_not_awaited()


class TestTheWholeStepOnARemoteTerminal:
    def test_a_reused_remote_terminal_runs_to_a_result(self, placement):
        """End to end on the path that was broken: the caller hands in a terminal
        that lives in a runtime, and the step sends, waits, extracts and leaves
        the caller's terminal alone."""

        async def fake_command(terminal_id, command_type, payload, timeout=None):
            return SimpleNamespace(payload={"output": "757"})

        with (
            patch(
                f"{_MODULE}.terminal_service.get_terminal_metadata",
                return_value={"provider": "kiro_cli"},
            ),
            patch(f"{_MODULE}.terminal_service.send_input", return_value=True) as send,
            patch(
                "cli_agent_orchestrator.runtime_channel.api.remote_terminal_command",
                side_effect=fake_command,
            ),
            patch(f"{_MODULE}.terminal_service.get_output") as local_extract,
            patch(f"{_MODULE}.terminal_service.delete_terminal") as delete,
            patch(f"{_MODULE}.frozen_run_memory.frozen_memory_for", return_value=None),
        ):
            result = asyncio.run(
                run_agent_step(
                    "kiro_cli", "dev", "do the task", reuse_terminal_id=REMOTE, timeout=5.0
                )
            )

        assert result.terminal_id == REMOTE
        assert result.last_message == "757"
        assert result.status == TerminalStatus.COMPLETED
        send.assert_called_once_with(REMOTE, "do the task")
        local_extract.assert_not_called()
        # Reused terminals stay the caller's.
        delete.assert_not_called()

    def test_a_remote_terminal_the_step_created_is_reclaimed_remotely(self, placement):
        """``teardown`` on a step that made its own terminal, when the launch was
        placed in a runtime: the reclaim has to reach that pod."""

        async def fake_command(terminal_id, command_type, payload, timeout=None):
            return SimpleNamespace(payload={"output": "757"})

        created = MagicMock()
        created.id = REMOTE

        with (
            patch(
                f"{_MODULE}.terminal_service.create_terminal", new=AsyncMock(return_value=created)
            ),
            patch(f"{_MODULE}.wait_until_status", new=AsyncMock(return_value=True)),
            patch(f"{_MODULE}.terminal_service.send_input", return_value=True),
            patch(f"{_MODULE}.terminal_service.get_working_directory", return_value=None),
            patch(
                "cli_agent_orchestrator.runtime_channel.api.remote_terminal_command",
                side_effect=fake_command,
            ),
            patch(
                "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal",
                new=AsyncMock(return_value=True),
            ) as remote_delete,
            patch(f"{_MODULE}.terminal_service.delete_terminal") as local_delete,
            patch(f"{_MODULE}.frozen_run_memory.frozen_memory_for", return_value=None),
        ):
            result = asyncio.run(run_agent_step("kiro_cli", "dev", "do the task", timeout=5.0))

        assert result.last_message == "757"
        remote_delete.assert_awaited_once_with(REMOTE)
        local_delete.assert_not_called()
