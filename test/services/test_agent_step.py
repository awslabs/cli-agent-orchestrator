"""Tests for the shared agent-step substrate (issue #312, unit N0).

Mocks the terminal layer (create/send/wait/extract/delete) and asserts the
canonical sequence + the reliability contract: run_agent_step returns ONLY on
success and RAISES on every failure mode (RD-2.1) — it never returns a falsy
success.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.workflow import AgentStepResult
from cli_agent_orchestrator.services.agent_step import StepExecutionError, run_agent_step
from cli_agent_orchestrator.services.terminal_service import OutputMode

_MODULE = "cli_agent_orchestrator.services.agent_step"


def _fake_terminal(terminal_id="abc12345"):
    t = MagicMock()
    t.id = terminal_id
    return t


def _patch_terminal_layer(
    *,
    created_id="abc12345",
    wait_results=(True, True),
    final_status=TerminalStatus.COMPLETED,
    output="the answer",
):
    """Context-manager bundle patching the terminal layer for run_agent_step.

    wait_results: side_effect list for wait_until_status calls (ready, complete).
    """
    create = patch(
        f"{_MODULE}.terminal_service.create_terminal",
        new=AsyncMock(return_value=_fake_terminal(created_id)),
    )
    send = patch(f"{_MODULE}.terminal_service.send_input", return_value=True)
    delete = patch(f"{_MODULE}.terminal_service.delete_terminal", return_value=True)
    get_output = patch(f"{_MODULE}.terminal_service.get_output", return_value=output)
    wait = patch(
        f"{_MODULE}.wait_until_status",
        new=AsyncMock(side_effect=list(wait_results)),
    )
    status = patch(f"{_MODULE}.status_monitor.get_status", return_value=final_status)
    return create, send, delete, get_output, wait, status


class TestHappyPath:
    def test_create_per_call_runs_full_sequence_and_tears_down(self):
        create, send, delete, get_output, wait, status = _patch_terminal_layer()
        with (
            create as m_create,
            send as m_send,
            delete as m_delete,
            get_output as m_out,
            wait,
            status,
        ):
            result = asyncio.run(run_agent_step("kiro_cli", "developer", "do the task"))

        assert isinstance(result, AgentStepResult)
        assert result.terminal_id == "abc12345"
        assert result.last_message == "the answer"
        assert result.status == TerminalStatus.COMPLETED
        # Canonical sequence: created, prompt sent, output extracted in LAST mode.
        m_create.assert_awaited_once()
        m_send.assert_called_once_with("abc12345", "do the task")
        m_out.assert_called_once_with("abc12345", OutputMode.LAST)
        # Created-here + teardown default -> deleted.
        m_delete.assert_called_once_with("abc12345")

    def test_teardown_false_skips_delete(self):
        create, send, delete, get_output, wait, status = _patch_terminal_layer()
        with create, send, delete as m_delete, get_output, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", teardown=False))
        m_delete.assert_not_called()

    def test_reuse_terminal_skips_create_and_delete(self):
        # Reuse: only ONE wait (completion); no readiness wait, no create/delete.
        create, send, delete, get_output, wait, status = _patch_terminal_layer(wait_results=(True,))
        with create as m_create, send as m_send, delete as m_delete, get_output, wait, status:
            result = asyncio.run(
                run_agent_step("kiro_cli", "dev", "x", reuse_terminal_id="reuse99")
            )
        assert result.terminal_id == "reuse99"
        m_create.assert_not_awaited()
        m_delete.assert_not_called()
        m_send.assert_called_once_with("reuse99", "x")

    def test_working_directory_forwarded_to_create(self):
        create, send, delete, get_output, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", working_directory="/tmp/wd"))
        assert m_create.await_args.kwargs["working_directory"] == "/tmp/wd"


class TestFailureRaises:
    def test_completion_timeout_raises(self):
        """wait_until_status -> False on completion: must RAISE, never return a
        falsy success (the key reliability contract, RD-2.1)."""
        create, send, delete, get_output, wait, status = _patch_terminal_layer(
            wait_results=(True, False),  # ready, then completion times out
            final_status=TerminalStatus.PROCESSING,
        )
        with create, send, delete, get_output, wait, status:
            with pytest.raises(StepExecutionError, match="did not complete"):
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))

    def test_readiness_timeout_raises(self):
        create, send, delete, get_output, wait, status = _patch_terminal_layer(
            wait_results=(False,),  # readiness times out before any input
        )
        with create, send as m_send, delete, get_output, wait, status:
            with pytest.raises(StepExecutionError, match="ready status"):
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # Fail-fast: no prompt sent if the terminal never became ready.
        m_send.assert_not_called()

    def test_error_end_state_raises(self):
        """Completion wait returns False AND status is ERROR -> ERROR message."""
        create, send, delete, get_output, wait, status = _patch_terminal_layer(
            wait_results=(True, False),
            final_status=TerminalStatus.ERROR,
        )
        with create, send, delete, get_output, wait, status:
            with pytest.raises(StepExecutionError, match="ERROR status"):
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))

    def test_error_after_completed_wait_still_raises(self):
        """Defensive re-check: even if completion wait returned True, an ERROR
        final status must not be reported as success."""
        create, send, delete, get_output, wait, status = _patch_terminal_layer(
            wait_results=(True, True),
            final_status=TerminalStatus.ERROR,
        )
        with create, send, delete, get_output as m_out, wait, status:
            with pytest.raises(StepExecutionError, match="ERROR status"):
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # No output extraction once ERROR is detected.
        m_out.assert_not_called()

    def test_create_failure_propagates(self):
        """A terminal-create failure is surfaced (ValueError), never swallowed."""
        create = patch(
            f"{_MODULE}.terminal_service.create_terminal",
            new=AsyncMock(side_effect=ValueError("session not found")),
        )
        with create:
            with pytest.raises(ValueError, match="session not found"):
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))


class TestTeardownIsBestEffort:
    def test_teardown_failure_does_not_fail_successful_step(self):
        """A delete failure after a successful step is logged, not raised — the
        work is done and captured."""
        create, send, _delete, get_output, wait, status = _patch_terminal_layer()
        delete = patch(
            f"{_MODULE}.terminal_service.delete_terminal",
            side_effect=Exception("kill failed"),
        )
        with create, send, delete, get_output, wait, status:
            result = asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert result.status == TerminalStatus.COMPLETED
        assert result.last_message == "the answer"
