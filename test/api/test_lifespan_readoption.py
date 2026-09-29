"""Tests for the terminal re-adoption step of the FastAPI lifespan (#667).

Re-adoption is blocking work (tmux subprocesses plus SQLite, per persisted
terminal), so the lifespan must run it OFF the event loop -- the way it already
runs ``cleanup_old_data`` and ``_sweep_workflow_runs_at_startup`` -- and keep the
task handle so shutdown can cancel it like its sibling tasks. The registry rows
are snapshotted BEFORE the server serves anything, so a terminal created by this
server while re-adoption is still running is never mistaken for a leftover.

The task-recording doubles and the patched lifespan come from
``test_lifespan_inbox``; see its module docstring for the mocking notes.
"""

import asyncio
from test.api.test_lifespan_inbox import _find_task, _patched_lifespan
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.api import main as main_module
from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.services import terminal_service

_ROWS = [{"id": "t1", "tmux_session": "cao-s", "tmux_window": "dev-1"}]


class _ToThreadRecorder:
    """Stand-in for ``asyncio.to_thread`` that records calls instead of running them."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, func, *args, **kwargs) -> object:
        token = object()
        self.calls.append((func, args, kwargs, token))
        return token

    def token_for(self, func) -> object:
        matches = [call[3] for call in self.calls if call[0] is func]
        assert len(matches) == 1, f"expected one to_thread({func.__name__}, ...), got {matches}"
        return matches[0]


class TestLifespanTerminalReadoption:
    """Startup + shutdown wiring for startup terminal re-adoption."""

    @pytest.mark.asyncio
    async def test_readoption_runs_in_a_worker_thread_on_a_pre_serving_snapshot(self) -> None:
        tasks: list = []
        to_thread = _ToThreadRecorder()

        with (
            _patched_lifespan(MagicMock(), tasks),
            patch.object(main_module, "list_all_terminals", return_value=_ROWS) as list_rows,
            patch("asyncio.to_thread", to_thread),
        ):
            async with lifespan(app):
                # The snapshot is taken synchronously during startup, before the
                # lifespan yields and the server starts serving requests.
                list_rows.assert_called_once_with()
                token = to_thread.token_for(main_module._readopt_terminals_at_startup)
                call = next(c for c in to_thread.calls if c[3] is token)
                assert call[1] == (_ROWS,)
                assert _find_task(tasks, token) is not None

    @pytest.mark.asyncio
    async def test_readoption_task_is_cancelled_on_shutdown(self) -> None:
        tasks: list = []
        to_thread = _ToThreadRecorder()

        with (
            _patched_lifespan(MagicMock(), tasks),
            patch.object(main_module, "list_all_terminals", return_value=_ROWS),
            patch("asyncio.to_thread", to_thread),
        ):
            async with lifespan(app):
                pass

        token = to_thread.token_for(main_module._readopt_terminals_at_startup)
        readopt_task = _find_task(tasks, token)
        assert readopt_task is not None
        readopt_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_unreadable_registry_does_not_block_startup(self) -> None:
        tasks: list = []
        to_thread = _ToThreadRecorder()

        with (
            _patched_lifespan(MagicMock(), tasks),
            patch.object(main_module, "list_all_terminals", side_effect=RuntimeError("locked")),
            patch("asyncio.to_thread", to_thread),
        ):
            async with lifespan(app):
                token = to_thread.token_for(main_module._readopt_terminals_at_startup)
                call = next(c for c in to_thread.calls if c[3] is token)
                assert call[1] == ([],)

    def test_readoption_service_is_synchronous(self) -> None:
        """A coroutine function with no ``await`` would run to completion on the
        loop the moment it was scheduled, starving it for the whole pass."""
        assert not asyncio.iscoroutinefunction(terminal_service.readopt_terminals_at_startup)


class TestReadoptTerminalsAtStartupWrapper:
    """The thread-side wrapper: best-effort, and it reports what it did."""

    def test_logs_counts(self, caplog) -> None:
        counts = {"readopted": 2, "finalized": 1, "skipped": 1}
        with patch.object(
            terminal_service, "readopt_terminals_at_startup", return_value=counts
        ) as readopt:
            with caplog.at_level("INFO", logger=main_module.logger.name):
                main_module._readopt_terminals_at_startup(_ROWS)

        readopt.assert_called_once_with(_ROWS)
        assert "2 re-adopted, 1 finalized, 1 left untouched" in caplog.text

    def test_swallows_errors(self, caplog) -> None:
        with patch.object(
            terminal_service, "readopt_terminals_at_startup", side_effect=RuntimeError("boom")
        ):
            with caplog.at_level("WARNING", logger=main_module.logger.name):
                main_module._readopt_terminals_at_startup(_ROWS)  # must not raise

        assert "boom" in caplog.text
