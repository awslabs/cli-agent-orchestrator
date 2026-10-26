"""The ephemeral sweep's server wiring: one startup pass, then a periodic daemon (#1079).

The lifespan helpers from ``test_lifespan_inbox`` replace task creation, so the daemon
appears as a recorded fake task; its own loop is driven for real in the last test.
"""

import asyncio
import logging
import threading
from test.api.test_lifespan_inbox import _find_task, _patched_lifespan
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.services import ephemeral_service


@pytest.mark.asyncio
async def test_startup_pass_runs_before_serving_and_the_daemon_stops_on_shutdown() -> None:
    tasks: list = []
    daemon = object()
    with (
        _patched_lifespan(MagicMock(), tasks),
        patch.object(ephemeral_service, "sweep", return_value={}) as sweep,
        patch.object(main, "ephemeral_sweep_daemon", MagicMock(return_value=daemon)) as factory,
    ):
        async with lifespan(app):
            # Inside the context the app is serving: the startup pass has finished.
            sweep.assert_called_once_with(True)
            factory.assert_called_once_with()
            task = _find_task(tasks, daemon)
            assert task is not None
            task.cancel.assert_not_called()
        task.cancel.assert_called_once_with()
    sweep.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_a_failing_startup_pass_does_not_block_startup(caplog) -> None:
    tasks: list = []
    served = False
    with (
        _patched_lifespan(MagicMock(), tasks),
        patch.object(ephemeral_service, "sweep", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING, logger=main.__name__),
    ):
        async with lifespan(app):
            served = True
    assert served
    assert "ephemeral startup sweep failed" in caplog.text


@pytest.mark.asyncio
async def test_daemon_runs_periodic_passes_survives_errors_and_cancels_cleanly(
    monkeypatch,
) -> None:
    calls: list = []
    second = threading.Event()

    def fake_sweep(startup):
        calls.append(startup)
        if len(calls) == 1:
            raise RuntimeError("one failing pass")
        second.set()
        return {}

    monkeypatch.setattr(ephemeral_service, "sweep", fake_sweep)
    monkeypatch.setattr(ephemeral_service, "sweep_interval_seconds", lambda: 0.01)
    task = asyncio.create_task(main.ephemeral_sweep_daemon())
    for _ in range(500):
        if second.is_set():
            break
        await asyncio.sleep(0.01)
    assert second.is_set(), "the daemon stopped after a failing pass"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert calls and set(calls) == {False}  # Periodic passes keep the launch grace.
