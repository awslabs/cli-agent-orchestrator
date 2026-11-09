"""``run_agent_step``'s keep hook (#1225 phase 1): the SUCCESS teardown only.

The hook decides whether a handoff's worker is kept. Every other teardown in the
substrate (cancellation, extraction failure, provider error) and every path that
leaves the terminal alive on purpose (timeout, worker error: #1203) must not see it.
"""

import asyncio
from test.services.test_agent_step import _MODULE, _patch_terminal_layer
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.agent_step import StepExecutionError, run_agent_step


def _run(hook, **kwargs):
    create, send, delete, get_output, exit_cli, _get_wd, wait, status = _patch_terminal_layer(
        **kwargs.pop("layer", {})
    )
    with (
        create,
        send,
        delete as m_delete,
        get_output,
        exit_cli as m_exit,
        wait,
        status,
        patch(f"{_MODULE}.upsert_handoff_result") as m_persist,
    ):
        result = asyncio.run(
            run_agent_step("claude_code", "dev", "x", keep_on_success=hook, **kwargs)
        )
    return result, m_exit, m_delete, m_persist


def test_a_kept_worker_gets_no_exit_command_and_no_delete():
    hook = Mock(return_value=True)
    result, m_exit, m_delete, _ = _run(hook)
    hook.assert_called_once_with("abc12345")
    m_exit.assert_not_called()
    m_delete.assert_not_called()
    assert result.terminal_id == "abc12345" and result.last_message == "the answer"


def test_a_kept_worker_is_reported_not_torn_down():
    """#1203's torn_down reports only a delete that happened; a keep is not one."""
    result, _, _, _ = _run(Mock(return_value=True))
    assert result.torn_down is False


def test_a_worker_not_kept_reports_its_real_teardown():
    result, _, _, _ = _run(Mock(return_value=False))
    assert result.torn_down is True


def test_the_result_is_persisted_before_the_keep_decision():
    order = []
    hook = Mock(side_effect=lambda tid: order.append("hook") or True)
    create, send, delete, get_output, exit_cli, _get_wd, wait, status = _patch_terminal_layer()
    with (
        create,
        send,
        delete,
        get_output,
        exit_cli,
        wait,
        status,
        patch(
            f"{_MODULE}.upsert_handoff_result", side_effect=lambda *a, **k: order.append("persist")
        ),
    ):
        asyncio.run(
            run_agent_step("claude_code", "dev", "x", job_id="0" * 32, keep_on_success=hook)
        )
    assert order == ["persist", "hook"]


def test_a_worker_not_kept_is_torn_down_exactly_as_before():
    _, m_exit, m_delete, _ = _run(Mock(return_value=False))
    m_exit.assert_called_once_with("abc12345")
    m_delete.assert_called_once_with("abc12345", registry=None, end_reason="deleted")


def test_a_failing_keep_decision_falls_back_to_the_teardown():
    _, m_exit, m_delete, _ = _run(Mock(side_effect=RuntimeError("db locked")))
    m_exit.assert_called_once_with("abc12345")
    m_delete.assert_called_once()


def test_no_hook_is_the_unchanged_teardown():
    _, m_exit, m_delete, _ = _run(None)
    m_exit.assert_called_once_with("abc12345")
    m_delete.assert_called_once_with("abc12345", registry=None, end_reason="deleted")


def test_teardown_false_never_asks():
    hook = Mock(return_value=True)
    _, m_exit, m_delete, _ = _run(hook, teardown=False)
    hook.assert_not_called()
    m_exit.assert_not_called()
    m_delete.assert_not_called()


def test_a_reused_terminal_never_asks():
    hook = Mock(return_value=True)
    with patch(
        f"{_MODULE}.terminal_service.get_terminal_metadata",
        return_value={"id": "reuse99", "provider": "claude_code"},
    ):
        _run(hook, reuse_terminal_id="reuse99")
    hook.assert_not_called()


def test_cancellation_tears_down_without_asking():
    from cli_agent_orchestrator.services.agent_step import StepCancelledError

    hook = Mock(return_value=True)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(StepCancelledError):
        _run(hook, cancel_event=cancel, layer={"final_status": TerminalStatus.PROCESSING})
    hook.assert_not_called()


def test_an_extraction_failure_tears_down_without_asking():
    from cli_agent_orchestrator.providers.base import OutputExtractionError

    hook = Mock(return_value=True)
    create, send, delete, _get_output, exit_cli, _get_wd, wait, status = _patch_terminal_layer()
    with (
        create,
        send,
        delete as m_delete,
        exit_cli as m_exit,
        wait,
        status,
        patch(f"{_MODULE}.terminal_service.get_output", side_effect=OutputExtractionError("no")),
    ):
        with pytest.raises(OutputExtractionError):
            asyncio.run(run_agent_step("claude_code", "dev", "x", keep_on_success=hook))
    hook.assert_not_called()
    m_exit.assert_called_once()
    m_delete.assert_called_once()


@pytest.mark.parametrize("kind", ["timeout", "error"])
def test_timeout_and_worker_error_still_leave_the_worker_alive_without_asking(kind):
    hook = Mock(return_value=True)
    create, send, delete, get_output, exit_cli, _get_wd, wait, status = _patch_terminal_layer()
    failure = StepExecutionError("boom", kind=kind, terminal_id="abc12345")
    with (
        create,
        send,
        delete as m_delete,
        get_output,
        exit_cli as m_exit,
        wait,
        status,
        patch(f"{_MODULE}._wait_for_completion", new=AsyncMock(side_effect=failure)),
    ):
        with pytest.raises(StepExecutionError):
            asyncio.run(run_agent_step("claude_code", "dev", "x", keep_on_success=hook))
    hook.assert_not_called()
    m_exit.assert_not_called()
    m_delete.assert_not_called()
