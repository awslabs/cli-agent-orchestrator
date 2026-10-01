"""Tests for durable handoff result persistence (issue #447).

Verifies the run-step handler writes to handoff_results before responding,
and the GET /handoff-results/{job_id} retrieval endpoint works correctly.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.constants import HANDOFF_RESULTS_ROUTE, TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import (
    AgentStepResult,
    TerminalLimitError,
    TerminalStatus,
)
from cli_agent_orchestrator.providers.base import OutputExtractionError
from cli_agent_orchestrator.providers.kiro_capabilities import KiroPhase0KASError
from cli_agent_orchestrator.services.agent_step import StepExecutionError
from cli_agent_orchestrator.services.worktree_service import WorktreeError

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"
_UPSERT = "cli_agent_orchestrator.api.main.upsert_handoff_result"
_GET = "cli_agent_orchestrator.api.main.get_handoff_result"


def _body(**overrides):
    base = {"provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
    base.update(overrides)
    return base


class TestRunStepDurability:
    def test_success_upserts_running_and_passes_job_id_through(self, client):
        """Happy path: handler writes 'running' at request start and forwards
        job_id into run_agent_step, which persists 'completed' itself BEFORE
        teardown (issue #447 / PR #453 review finding 2) — not here after
        run_agent_step has already returned (and torn the terminal down)."""
        result = AgentStepResult(
            terminal_id="abc12345",
            last_message="all done",
            status=TerminalStatus.COMPLETED,
        )
        calls = []
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run_step,
            patch(_UPSERT, side_effect=lambda *a, **kw: calls.append((a, kw))),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="cafe1234" * 4))

        assert resp.status_code == 200
        # Only "running" is written here; "completed" is run_agent_step's job.
        assert len(calls) == 1
        assert calls[0][0][1] == "running"
        assert m_run_step.await_args.kwargs["job_id"] == "cafe1234" * 4

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

    # PR #453 review (blocking): these four arms settled the SCRIPT step but never
    # transitioned the JOB, so a caller polling GET /handoff-results after a
    # transport timeout read "running" until the retention sweep deleted the row.
    # OutputExtractionError is the worst case -- the worker DID produce output.
    # Parametrised rather than four near-identical bodies so a fifth arm is one
    # table row, and asserted per-arm on the status code as well as the state so
    # exception ORDERING (all but TerminalLimitError/WorktreeError are caught
    # before the ValueError arm they subclass) stays pinned too.
    @pytest.mark.parametrize(
        ("exc", "expected_status", "fragment"),
        [
            (OutputExtractionError("no response marker in scrollback"), 500, "response marker"),
            (KiroPhase0KASError(False), 400, "not available in Phase 0"),
            (TerminalLimitError("node is at CAO_MAX_TERMINALS"), 429, "CAO_MAX_TERMINALS"),
            (WorktreeError("not a git repository"), 400, "not a git repository"),
        ],
        ids=["output_extraction", "kiro_phase0_kas", "terminal_limit", "worktree"],
    )
    def test_settled_failure_arms_persist_error_state(self, client, exc, expected_status, fragment):
        calls = []
        with (
            patch(_RUN_STEP, new=AsyncMock(side_effect=exc)),
            patch(_UPSERT, side_effect=lambda *a, **kw: calls.append((a, kw))),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id="99aabbcc" * 4))

        assert resp.status_code == expected_status
        assert [c[0][1] for c in calls] == ["running", "error"]
        assert fragment in calls[1][1]["error_message"]


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
            resp = client.get(
                HANDOFF_RESULTS_ROUTE.format(job_id="cafe1234cafe1234cafe1234cafe1234")
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "completed"
        assert data["last_message"] == "done"

    def test_returns_404_when_not_found(self, client):
        with patch(_GET, return_value=None):
            resp = client.get(HANDOFF_RESULTS_ROUTE.format(job_id="unknown-job-id"))

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
            resp = client.get(
                HANDOFF_RESULTS_ROUTE.format(job_id="aabbccddaabbccddaabbccddaabbccdd")
            )

        assert resp.status_code == 200
        assert resp.json()["state"] == "running"


class TestPersistFailureLogDoesNotLeakTheCapability:
    """PR #453 review (haofeif): truncating the formatted id is not enough while
    the same call also emits the exception.

    A SQLAlchemy DBAPI error stringifies its bound parameters --
    ``[parameters: ('<job_id>', 'running', ...)]`` -- so ``exc_info=True`` (or
    ``str(exc)``) reprints in full the id the format string deliberately
    truncates. Exercised with a REAL parameter-bearing SQLAlchemy exception, not
    a plain ``Exception``, because a bare mock cannot reproduce the leak.
    """

    JOB = "cafe1234" * 4

    def _sqlalchemy_error_carrying_the_job_id(self):
        from sqlalchemy.exc import OperationalError

        # Same shape SQLAlchemy raises on a locked SQLite file: the statement and
        # the bound params (which include job_id) are part of the exception.
        return OperationalError(
            "UPDATE handoff_results SET state=? WHERE job_id=?",
            (self.JOB, "running"),
            Exception("database is locked"),
        )

    def test_full_job_id_never_reaches_the_log_record(self, client, caplog):
        exc = self._sqlalchemy_error_carrying_the_job_id()
        # Sanity: the exception really does carry the id, so this test is not
        # passing merely because nothing had it to leak.
        assert self.JOB in str(exc)

        result = AgentStepResult(
            terminal_id="abc12345",
            last_message="ok",
            status=TerminalStatus.COMPLETED,
        )
        with (
            patch(_RUN_STEP, new=AsyncMock(return_value=result)),
            patch(_UPSERT, side_effect=exc),
            caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.api.main"),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(job_id=self.JOB))

        assert resp.status_code == 200  # persistence stays best-effort
        # Covers the formatted message, the args, AND any attached traceback text.
        rendered = "\n".join(r.getMessage() for r in caplog.records)
        formatted = "\n".join(logging.Formatter("%(message)s").format(r) for r in caplog.records)
        assert self.JOB not in rendered
        assert self.JOB not in formatted
        assert not any(r.exc_info for r in caplog.records)

        # Redaction must not cost the operator the two things they act on:
        # WHICH job (prefix) and WHICH failure class. Asserted on the same
        # records, so a fix that silenced the log entirely would fail here.
        assert self.JOB[:8] in rendered
        assert "OperationalError" in rendered


class TestLookupFailureDoesNotLeakTheCapability:
    """PR #453 review (haofeif, read-error path): fixing the WRITE warning left
    the READ symmetrically exposed.

    The retrieval handler had no guard and there is no generic exception handler
    above it, so a SQLAlchemy lookup failure escaped to uvicorn's
    ``ServerErrorMiddleware``, which logs the full traceback -- including
    ``[parameters: ('<job_id>',)]`` -- to ``uvicorn.error``. The access-log
    filter only scrubs the request path, so it never sees that surface.
    """

    JOB = "cafe1234" * 4

    def test_lookup_failure_is_content_safe(self, client, caplog):
        from sqlalchemy.exc import OperationalError

        # Same shape SQLAlchemy raises against a locked SQLite file: the bound
        # params (which are just the job_id here) are part of the exception.
        exc = OperationalError(
            "SELECT * FROM handoff_results WHERE job_id = ?",
            (self.JOB,),
            Exception("database is locked"),
        )
        # Sanity: the exception really does carry the id, so this test is not
        # passing merely because nothing had it to leak.
        assert self.JOB in str(exc)

        with (
            patch(_GET, side_effect=exc),
            caplog.at_level(logging.ERROR, logger="cli_agent_orchestrator.api.main"),
        ):
            resp = client.get(HANDOFF_RESULTS_ROUTE.format(job_id=self.JOB))

        # Reaching a response at all is half the fix: an escaping exception is
        # what handed the traceback to uvicorn (the client re-raises by default,
        # so this line fails outright if the guard is removed).
        assert resp.status_code == 500
        assert self.JOB not in resp.text

        rendered = "\n".join(r.getMessage() for r in caplog.records)
        formatted = "\n".join(logging.Formatter("%(message)s").format(r) for r in caplog.records)
        assert self.JOB not in rendered
        assert self.JOB not in formatted
        assert not any(r.exc_info for r in caplog.records)

        # Still actionable for an operator: which job, which failure class.
        assert self.JOB[:8] in rendered
        assert "OperationalError" in rendered

    def test_successful_lookup_is_unaffected(self, client):
        """The guard must not swallow a normal 200 (nor turn 404 into 500)."""
        record = {
            "job_id": self.JOB,
            "state": "completed",
            "terminal_id": "abc12345",
            "last_message": "done",
            "error_message": None,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:01:00+00:00",
        }
        with patch(_GET, return_value=record):
            assert client.get(HANDOFF_RESULTS_ROUTE.format(job_id=self.JOB)).status_code == 200
        with patch(_GET, return_value=None):
            assert client.get(HANDOFF_RESULTS_ROUTE.format(job_id=self.JOB)).status_code == 404
