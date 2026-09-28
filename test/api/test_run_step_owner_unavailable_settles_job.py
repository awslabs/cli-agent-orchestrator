"""A handoff job is settled when the owner read fails, not stranded (#745).

``run_step`` writes job state "running" before execution and every failure arm
settles it. The ``OwnerUnavailableError`` arm raised 503 without recording a
terminal job state, so a handoff job stayed "running" forever (haofeif #13 on
#802). The 503 (retryable) is kept; the job state is now settled to "error".
``_settle_step`` is deliberately NOT called — the workflow step stays retryable.
"""

import asyncio

import pytest

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.services.agent_step import OwnerUnavailableError


class _FakeRequest:
    pass


class _FakeBackgroundTasks:
    def add_task(self, *a, **k):
        pass


def test_owner_unavailable_settles_job_to_error_and_answers_503(monkeypatch):
    states = []

    async def fake_record_job_state(job_id, state, **fields):
        states.append(state)

    async def fake_run_agent_step(**kwargs):
        raise OwnerUnavailableError("owner row read failed")

    monkeypatch.setattr(main, "_record_job_state", fake_record_job_state)
    monkeypatch.setattr(main, "run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    body = main.RunStepRequest(
        provider="mock_cli",
        agent="supervisor",
        prompt="hi",
        job_id="0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(main.HTTPException) as ei:
        asyncio.run(main.run_step(_FakeRequest(), _FakeBackgroundTasks(), body))

    # The 503 (retryable) is preserved — this is the OwnerUnavailableError arm,
    # ahead of the generic 500 arm.
    assert ei.value.status_code == 503
    # Job was recorded running and then SETTLED to error — not stranded forever.
    assert states[-1] == "error"
    assert "error" in states
