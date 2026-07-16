"""Tests for durable handoff result persistence (issue #447).

Verifies the run-step handler writes to handoff_results before responding,
and the GET /handoff-results/{job_id} retrieval endpoint works correctly.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.services.agent_step import StepExecutionError

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"
_UPSERT = "cli_agent_orchestrator.api.main.upsert_handoff_result"
_GET = "cli_agent_orchestrator.api.main.get_handoff_result"


def _body(**overrides):
    base = {"provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
    base.update(overrides)
    return base


class TestRunStepDurability:
    def test_success_upserts_running_then_completed(self, client):
        """Happy path: handler writes running then completed, in that order."""
        result = AgentStepResult(
            terminal_id="abc12345",
            last_message="all done",
            status=TerminalStatus.COMPLETED,
        )
        calls = []
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)),
            patch(_UPSERT, side_effect=lambda *a, **kw: calls.append((a, kw))),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="cafe1234" * 4))

        assert resp.status_code == 200
        # Two upserts: running (start) and completed (before response).
        assert len(calls) == 2
        assert calls[0][0][1] == "running"
        assert calls[1][0][1] == "completed"
        assert calls[1][1]["last_message"] == "all done"
        assert calls[1][1]["terminal_id"] == "abc12345"

    def test_no_job_id_skips_upsert(self, client):
        """Without job_id, nothing is persisted (backward-compat)."""
        result = AgentStepResult(
            terminal_id="abc12345",
            last_message="all done",
            status=TerminalStatus.COMPLETED,
        )
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)),
            patch(_UPSERT) as m_upsert,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())

        assert resp.status_code == 200
        m_upsert.assert_not_called()

    def test_step_execution_error_persists_error_state(self, client):
        """A StepExecutionError is persisted as state=error before the 504."""
        calls = []
        with (
            patch(
                _RUN_STEP,
                new=AsyncMock(
                    side_effect=StepExecutionError(
                        "timed out", kind="timeout", terminal_id="abc12345"
                    )
                ),
            ),
            patch(_UPSERT, side_effect=lambda *a, **kw: calls.append((a, kw))),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="aabbccdd" * 4))

        assert resp.status_code == 504
        # running + error
        assert len(calls) == 2
        assert calls[1][0][1] == "error"
        assert "timed out" in calls[1][1]["error_message"]

    def test_upsert_failure_does_not_break_successful_step(self, client):
        """A DB write failure is logged but must not turn a successful step into
        a failure — the work is done and the response must still be 200."""
        result = AgentStepResult(
            terminal_id="abc12345",
            last_message="ok",
            status=TerminalStatus.COMPLETED,
        )
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)),
            patch(_UPSERT, side_effect=Exception("db boom")),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="deadbeef" * 4))

        assert resp.status_code == 200
        assert resp.json()["last_message"] == "ok"


class TestRunStepDurabilityErrorBranches:
    """S-001: ValueError and generic Exception branches must also persist error state."""

    def test_value_error_persists_error_state(self, client):
        """ValueError (e.g. unknown terminal) must transition job to 'error', not leave it
        stuck at 'running'."""
        calls = []
        with (
            patch(_RUN_STEP, new=AsyncMock(side_effect=ValueError("Terminal 'x' not found"))),
            patch(_UPSERT, side_effect=lambda *a, **kw: calls.append((a, kw))),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="11223344" * 4))

        assert resp.status_code == 404
        # running then error — not stuck at running.
        assert len(calls) == 2
        assert calls[0][0][1] == "running"
        assert calls[1][0][1] == "error"
        assert "Terminal 'x'" in calls[1][1]["error_message"]

    def test_generic_exception_persists_error_state(self, client):
        """An unanticipated Exception must also transition job to 'error'."""
        calls = []
        with (
            patch(_RUN_STEP, new=AsyncMock(side_effect=RuntimeError("unexpected boom"))),
            patch(_UPSERT, side_effect=lambda *a, **kw: calls.append((a, kw))),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="55667788" * 4))

        assert resp.status_code == 500
        assert len(calls) == 2
        assert calls[1][0][1] == "error"
        assert "unexpected boom" in calls[1][1]["error_message"]


class TestJobIdValidation:
    """S-002: job_id field must reject non-hex and wrong-length values."""

    def test_valid_32_char_hex_accepted(self, client):
        result = AgentStepResult(
            terminal_id="abc12345", last_message="ok", status=TerminalStatus.COMPLETED
        )
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)),
            patch(_UPSERT),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="a" * 32))
        assert resp.status_code == 200

    def test_empty_string_rejected(self, client):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id=""))
        assert resp.status_code == 422

    def test_31_char_hex_rejected(self, client):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="a" * 31))
        assert resp.status_code == 422

    def test_33_char_hex_rejected(self, client):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="a" * 33))
        assert resp.status_code == 422

    def test_uppercase_hex_rejected(self, client):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="A" * 32))
        assert resp.status_code == 422

    def test_non_hex_char_rejected(self, client):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="g" * 32))
        assert resp.status_code == 422

    def test_none_omitted_is_fine(self, client):
        """Omitting job_id entirely (None) should still be accepted."""
        result = AgentStepResult(
            terminal_id="abc12345", last_message="ok", status=TerminalStatus.COMPLETED
        )
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)),
            patch(_UPSERT),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 200


class TestGetHandoffResult:
    def test_returns_record_when_found(self, client):
        record = {
            "job_id": "cafe1234" * 4,
            "state": "completed",
            "terminal_id": "abc12345",
            "last_message": "done",
            "error_message": None,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:01:00+00:00",
        }
        with patch(_GET, return_value=record):
            resp = client.get("/handoff-results/cafe1234cafe1234cafe1234cafe1234")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "completed"
        assert data["last_message"] == "done"

    def test_returns_404_when_not_found(self, client):
        with patch(_GET, return_value=None):
            resp = client.get("/handoff-results/unknown-job-id")

        assert resp.status_code == 404

    def test_returns_running_state(self, client):
        record = {
            "job_id": "aabbccdd" * 4,
            "state": "running",
            "terminal_id": None,
            "last_message": None,
            "error_message": None,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:30+00:00",
        }
        with patch(_GET, return_value=record):
            resp = client.get("/handoff-results/aabbccddaabbccddaabbccddaabbccdd")

        assert resp.status_code == 200
        assert resp.json()["state"] == "running"
