"""Production kept-child reaper scheduling and lifespan wiring, without real sleeps."""

import asyncio
from test.api.test_lifespan_inbox import _find_task, _patched_lifespan
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.services import keep_service


@pytest.mark.asyncio
async def test_kept_reaper_runs_before_sleep_and_survives_a_failed_pass(monkeypatch):
    events = []
    registry = object()
    elapsed = [0.0]

    def clock():
        # Reads cost time: a loop that keeps resetting a deadline cannot hide here.
        elapsed[0] += 0.25
        return elapsed[0]

    async def thread(fn, passed_registry):
        assert fn is keep_service.reap and passed_registry is registry
        events.append(("pass", clock()))
        if len(events) == 1:
            raise RuntimeError("failed pass")

    async def sleep(delay):
        assert delay == 60
        events.append(("sleep", clock()))
        elapsed[0] += delay
        if len(events) > 6:
            pytest.fail("daemon swallowed cancellation")
        if len(events) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "to_thread", thread)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with patch.object(main.logger, "exception") as error:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(main.kept_terminal_reaper_daemon(registry), timeout=1)
    assert [kind for kind, _ in events] == ["pass", "sleep", "pass", "sleep"]
    assert events[2][1] - events[0][1] == 60.5
    error.assert_called_once_with("Kept-terminal reaper error")


@pytest.mark.asyncio
async def test_lifespan_starts_kept_reaper_reports_posture_and_cancels_it():
    tasks = []
    daemon = object()
    with (
        _patched_lifespan(MagicMock(), tasks),
        patch.object(keep_service, "log_startup_posture") as posture,
        patch.object(
            main, "kept_terminal_reaper_daemon", MagicMock(return_value=daemon)
        ) as factory,
    ):
        async with main.lifespan(main.app):
            posture.assert_called_once_with()
            factory.assert_called_once_with(main.app.state.plugin_registry)
            task = _find_task(tasks, daemon)
            assert task is not None
            task.cancel.assert_not_called()
        task.cancel.assert_called_once_with()
