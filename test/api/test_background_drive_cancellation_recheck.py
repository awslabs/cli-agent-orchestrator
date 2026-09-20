"""#745: a cancellation accepted while a run is queued prevents dispatch.

The background dispatcher used to enter the prepared engine entry immediately
after acquiring drive capacity; a run cancelled while waiting on the semaphore
would still start its subprocess, and the script tier only checked the
cancelled flag after the process exited. These tests pin the recheck at the
execution boundary: launch count 0 for the queued-cancelled run, launch count
1 for the ordinary control.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.models.workflow_runtime import RunState


def _record(cancelled: bool) -> SimpleNamespace:
    return SimpleNamespace(cancelled=cancelled, state=RunState.RUNNING)


def _journal_row(state: str) -> SimpleNamespace:
    return SimpleNamespace(state=state)


class TestQueuedCancellationRecheck:
    @pytest.mark.asyncio
    async def test_cancelled_record_is_not_dispatched(self):
        launches = []

        async def fake_prepared(record, path, env):
            launches.append(record)

        with (
            patch(
                "cli_agent_orchestrator.services.script_runner.run_script_workflow_prepared",
                side_effect=fake_prepared,
            ),
            patch(
                "cli_agent_orchestrator.services.script_runner.build_env",
                return_value={},
            ),
            patch(
                "cli_agent_orchestrator.services.workflow_journal.get_run",
                return_value=_journal_row(RunState.RUNNING.value),
            ),
        ):
            await api_main._run_in_background(
                _record(cancelled=True), SimpleNamespace(path="x.py"), "run-1", "script", {}
            )
        assert launches == []

    @pytest.mark.asyncio
    async def test_journaled_cancelled_is_not_dispatched(self):
        """The in-memory flag can be absent (e.g. restart); the durable
        journal CANCELLED alone must also block dispatch."""
        launches = []

        async def fake_prepared(record, path, env):
            launches.append(record)

        with (
            patch(
                "cli_agent_orchestrator.services.script_runner.run_script_workflow_prepared",
                side_effect=fake_prepared,
            ),
            patch(
                "cli_agent_orchestrator.services.script_runner.build_env",
                return_value={},
            ),
            patch(
                "cli_agent_orchestrator.services.workflow_journal.get_run",
                return_value=_journal_row(RunState.CANCELLED.value),
            ),
        ):
            await api_main._run_in_background(
                _record(cancelled=False), SimpleNamespace(path="x.py"), "run-2", "script", {}
            )
        assert launches == []

    @pytest.mark.asyncio
    async def test_ordinary_queued_run_still_dispatches(self):
        launches = []

        async def fake_prepared(record, path, env):
            launches.append(record)

        with (
            patch(
                "cli_agent_orchestrator.services.script_runner.run_script_workflow_prepared",
                side_effect=fake_prepared,
            ),
            patch(
                "cli_agent_orchestrator.services.script_runner.build_env",
                return_value={},
            ),
            patch(
                "cli_agent_orchestrator.services.workflow_journal.get_run",
                return_value=_journal_row(RunState.RUNNING.value),
            ),
        ):
            await api_main._run_in_background(
                _record(cancelled=False), SimpleNamespace(path="x.py"), "run-3", "script", {}
            )
        assert len(launches) == 1

    @pytest.mark.asyncio
    async def test_cancel_landing_during_capacity_wait_blocks_dispatch(self):
        """The race the recheck exists for: cancellation arrives while the run
        is parked on the drive semaphore."""
        launches = []
        record = _record(cancelled=False)

        async def fake_prepared(rec, path, env):
            launches.append(rec)

        # Substitute a single-slot semaphore and hold its only slot so the
        # run genuinely queues (the production default is 12 slots).
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        try:
            api_main._drive_semaphore = sem
            with (
                patch(
                    "cli_agent_orchestrator.services.script_runner.run_script_workflow_prepared",
                    side_effect=fake_prepared,
                ),
                patch(
                    "cli_agent_orchestrator.services.script_runner.build_env",
                    return_value={},
                ),
                patch(
                    "cli_agent_orchestrator.services.workflow_journal.get_run",
                    return_value=_journal_row(RunState.RUNNING.value),
                ),
            ):
                task = asyncio.ensure_future(
                    api_main._run_in_background(
                        record, SimpleNamespace(path="x.py"), "run-4", "script", {}
                    )
                )
                await asyncio.sleep(0.02)  # parked on the semaphore
                record.cancelled = True  # cancel accepted while queued
                sem.release()  # capacity frees; stale queued work processes
                await task
        finally:
            api_main._drive_semaphore = None  # restore lazy default
        assert launches == []
