"""Tests for the combined POST /terminals/run-step endpoint (issue #312, N0).

Asserts the handler delegates to run_agent_step and maps domain failures to
HTTPException at the API boundary (SD-2.2 / project boundary-map rule).
"""

from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.workflow import AgentStepResult
from cli_agent_orchestrator.services.agent_step import StepExecutionError

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"


def _body(**overrides):
    base = {"provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
    base.update(overrides)
    return base


class TestRunStepEndpoint:
    def test_happy_path_returns_result(self, client):
        result = AgentStepResult(
            terminal_id="abc12345",
            last_message="all done",
            status=TerminalStatus.COMPLETED,
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())

        assert resp.status_code == 200
        data = resp.json()
        assert data["terminal_id"] == "abc12345"
        assert data["last_message"] == "all done"
        assert data["status"] == "completed"
        # The handler forwarded the request fields to the substrate.
        kwargs = m_run.await_args.kwargs
        assert kwargs["provider"] == "kiro_cli"
        assert kwargs["agent"] == "developer"
        assert kwargs["prompt"] == "do it"

    def test_step_execution_error_maps_to_504(self, client):
        with patch(
            _RUN_STEP,
            new=AsyncMock(side_effect=StepExecutionError("terminal abc12345 timed out")),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 504
        assert "abc12345" in resp.json()["detail"]

    def test_value_error_maps_to_404(self, client):
        with patch(_RUN_STEP, new=AsyncMock(side_effect=ValueError("Terminal 'x' not found"))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 404

    def test_unexpected_error_maps_to_500(self, client):
        with patch(_RUN_STEP, new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 500
        assert "boom" in resp.json()["detail"]

    def test_missing_required_field_is_422(self, client):
        # Pydantic request-model validation rejects a missing prompt.
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json={"provider": "p", "agent": "a"})
        assert resp.status_code == 422
