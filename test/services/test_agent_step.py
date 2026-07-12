"""Tests for the shared agent-step substrate (issue #312, unit N0).

Mocks the terminal layer (create/send/wait/extract/delete) and asserts the
canonical sequence + the reliability contract: run_agent_step returns ONLY on
success and RAISES on every failure mode (RD-2.1) — it never returns a falsy
success.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
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
    exit_cli = patch(f"{_MODULE}.terminal_service.exit_terminal_cli", return_value=None)
    wait = patch(
        f"{_MODULE}.wait_until_status",
        new=AsyncMock(side_effect=list(wait_results)),
    )
    status = patch(f"{_MODULE}.status_monitor.get_status", return_value=final_status)
    return create, send, delete, get_output, exit_cli, wait, status


class TestHappyPath:
    def test_create_per_call_runs_full_sequence_and_tears_down(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with (
            create as m_create,
            send as m_send,
            delete as m_delete,
            get_output as m_out,
            exit_cli as m_exit,
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
        # Created-here + teardown default -> graceful exit THEN delete.
        m_exit.assert_called_once_with("abc12345")
        m_delete.assert_called_once_with("abc12345", registry=None)

    def test_teardown_false_skips_delete(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create, send, delete as m_delete, get_output, exit_cli as m_exit, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", teardown=False))
        m_delete.assert_not_called()
        m_exit.assert_not_called()

    def test_reuse_terminal_skips_create_and_delete(self):
        # Reuse: only ONE wait (completion); no readiness wait, no create/delete.
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,)
        )
        with (
            create as m_create,
            send as m_send,
            delete as m_delete,
            get_output,
            exit_cli as m_exit,
            wait,
            status,
        ):
            result = asyncio.run(
                run_agent_step("kiro_cli", "dev", "x", reuse_terminal_id="reuse99")
            )
        assert result.terminal_id == "reuse99"
        m_create.assert_not_awaited()
        m_delete.assert_not_called()
        # A reused terminal is owned by the caller — no graceful exit either.
        m_exit.assert_not_called()
        m_send.assert_called_once_with("reuse99", "x")

    def test_working_directory_forwarded_to_create(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", working_directory="/tmp/wd"))
        assert m_create.await_args.kwargs["working_directory"] == "/tmp/wd"

    def test_no_session_name_creates_new_session(self):
        """Regression: session_name=None must create a NEW tmux session
        (new_session=True). Otherwise create_terminal auto-generates a name and
        then fails with 'Session not found' because it tries to add a window to
        a session that does not exist yet."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert m_create.await_args.kwargs["new_session"] is True
        assert m_create.await_args.kwargs["session_name"] is None

    def test_session_name_adds_to_existing_session(self):
        """A supplied session_name adds a window to that EXISTING session
        (new_session=False) — the handoff same-session path."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", session_name="cao-sup"))
        assert m_create.await_args.kwargs["new_session"] is False
        assert m_create.await_args.kwargs["session_name"] == "cao-sup"

    def test_caller_id_and_allowed_tools_forwarded_to_create(self):
        """caller_id (#284 callback routing) and inherited allowed_tools must
        reach create_terminal for handoff workers."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(
                run_agent_step(
                    "kiro_cli",
                    "dev",
                    "x",
                    session_name="cao-sup",
                    caller_id="sup-123",
                    allowed_tools=["fs_read", "fs_write"],
                )
            )
        assert m_create.await_args.kwargs["caller_id"] == "sup-123"
        assert m_create.await_args.kwargs["allowed_tools"] == ["fs_read", "fs_write"]

    def test_registry_threaded_to_delete_on_teardown(self):
        """The plugin registry passed to run_agent_step must reach delete_terminal
        so post_kill_terminal hooks dispatch (parity with the DELETE endpoint)."""
        from cli_agent_orchestrator.plugins import PluginRegistry

        sentinel = PluginRegistry()
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create, send, delete as m_delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", registry=sentinel))
        m_delete.assert_called_once_with("abc12345", registry=sentinel)


class TestFailureRaises:
    def test_completion_timeout_raises(self):
        """wait_until_status -> False on completion: must RAISE, never return a
        falsy success (the key reliability contract, RD-2.1)."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True, False),  # ready, then completion times out
            final_status=TerminalStatus.PROCESSING,
        )
        with create, send, delete, get_output, exit_cli, wait, status:
            with pytest.raises(StepExecutionError, match="did not complete") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # Timeout (ran long), with the live terminal carried structurally.
        assert exc_info.value.kind == "timeout"
        assert exc_info.value.terminal_id == "abc12345"

    def test_readiness_timeout_raises(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(False,),  # readiness times out before any input
        )
        with create, send as m_send, delete, get_output, exit_cli, wait, status:
            with pytest.raises(StepExecutionError, match="ready status") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # Fail-fast: no prompt sent if the terminal never became ready.
        m_send.assert_not_called()
        assert exc_info.value.kind == "timeout"
        assert exc_info.value.terminal_id == "abc12345"

    def test_error_end_state_raises_with_error_kind(self):
        """Completion wait returns False AND status is ERROR -> kind='error'
        (worker CRASHED), distinct from a plain timeout, with terminal_id."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True, False),
            final_status=TerminalStatus.ERROR,
        )
        with create, send, delete, get_output, exit_cli, wait, status:
            with pytest.raises(StepExecutionError, match="ERROR status") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert exc_info.value.kind == "error"
        assert exc_info.value.terminal_id == "abc12345"

    def test_error_after_completed_wait_still_raises(self):
        """Defensive re-check: even if completion wait returned True, an ERROR
        final status must not be reported as success."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True, True),
            final_status=TerminalStatus.ERROR,
        )
        with create, send, delete, get_output as m_out, exit_cli, wait, status:
            with pytest.raises(StepExecutionError, match="ERROR status") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # No output extraction once ERROR is detected.
        m_out.assert_not_called()
        assert exc_info.value.kind == "error"

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
        create, send, _delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        delete = patch(
            f"{_MODULE}.terminal_service.delete_terminal",
            side_effect=Exception("kill failed"),
        )
        with create, send, delete, get_output, exit_cli, wait, status:
            result = asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert result.status == TerminalStatus.COMPLETED
        assert result.last_message == "the answer"

    def test_graceful_exit_failure_does_not_fail_step_and_still_deletes(self):
        """A failure sending the graceful exit must be logged, not raised, and
        must NOT prevent the subsequent delete (best-effort exit-then-delete)."""
        create, send, delete, get_output, _exit, wait, status = _patch_terminal_layer()
        exit_cli = patch(
            f"{_MODULE}.terminal_service.exit_terminal_cli",
            side_effect=Exception("exit boom"),
        )
        with create, send, delete as m_delete, get_output, exit_cli, wait, status:
            result = asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert result.status == TerminalStatus.COMPLETED
        # Exit failed but delete still ran.
        m_delete.assert_called_once_with("abc12345", registry=None)

    def test_on_terminal_created_callback_failure_does_not_fail_step(self):
        """F9(b): a raising ``on_terminal_created`` callback (BR-31 sweep
        bookkeeping) must never propagate into ``run_agent_step`` — it is
        best-effort, logged and swallowed, and the step still completes."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()

        def _boom_callback(terminal_id):
            raise RuntimeError("sweep bookkeeping boom")

        with create, send, delete, get_output, exit_cli, wait, status:
            result = asyncio.run(
                run_agent_step("kiro_cli", "dev", "x", on_terminal_created=_boom_callback)
            )
        assert result.status == TerminalStatus.COMPLETED
        assert result.last_message == "the answer"
