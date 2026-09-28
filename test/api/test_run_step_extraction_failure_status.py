"""run-step must not report an output-extraction failure as 404 (issue #570).

A provider's ``extract_last_message_from_script`` raises when it cannot find a
completion marker in the scrollback. That failure used to share ``run_step``'s
blanket ``except ValueError`` arm with genuine bad-terminal references, so it
surfaced as 404 Not Found -- indistinguishable from a missing route, and the
reason the reporter in #562 went and pulled the live OpenAPI schema to check
whether the endpoint existed at all.

The terminal exists, the route exists, the step ran. Per this endpoint's own
documented failure contract ("A bad terminal reference -> 404; any other
failure -> 500"), it is a 500.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.providers.base import OutputExtractionError

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"

# The real message from providers/opencode_cli.py.
_MARKER_MISSING = "No completion marker found after last user message"


def _body(**overrides):
    base = {"provider": "opencode_cli", "agent": "developer", "prompt": "do it"}
    base.update(overrides)
    return base


class TestExtractionFailureStatus:
    def test_extraction_failure_is_not_404(self, client):
        # The regression itself: 404 is the one status this must never be.
        with patch(_RUN_STEP, new=AsyncMock(side_effect=OutputExtractionError(_MARKER_MISSING))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code != 404

    def test_extraction_failure_maps_to_500(self, client):
        with patch(_RUN_STEP, new=AsyncMock(side_effect=OutputExtractionError(_MARKER_MISSING))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 500

    def test_detail_is_a_plain_string_without_kind(self, client):
        # Contract: only step-execution outcomes carry the structured
        # {"message", "kind", "terminal_id"} detail. This is not one of them.
        with patch(_RUN_STEP, new=AsyncMock(side_effect=OutputExtractionError(_MARKER_MISSING))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert _MARKER_MISSING in detail

    def test_bad_terminal_reference_still_maps_to_404(self, client):
        # The other half of the contract, pinned so narrowing the arm above did
        # not take the genuine lookup failure with it.
        with patch(_RUN_STEP, new=AsyncMock(side_effect=ValueError("Terminal 'abc123' not found"))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 404


class TestExceptionType:
    def test_subclasses_value_error(self):
        # Existing `except ValueError` callers (including the retry loops inside
        # terminal_service.get_output) must keep catching it.
        assert issubclass(OutputExtractionError, ValueError)

    def test_carries_the_provider_message(self):
        with pytest.raises(ValueError, match=_MARKER_MISSING):
            raise OutputExtractionError(_MARKER_MISSING)


class TestExtractionFailureSettlement:
    """An extraction failure must settle a script step as FAILED.

    The ``run_step`` boundary settles the step in its OutputExtractionError arm
    (``_settle_step(None, str(e))``), just like the other arms that run after
    dispatch. These assertions pin that bookkeeping so the call cannot be
    dropped or altered without detection -- same shape as the parametrized
    untyped-failure settlement guard in test_run_step.py.
    """

    def test_extraction_failure_settles_script_step_failed(self, client, monkeypatch):
        from cli_agent_orchestrator.models.workflow import StepState
        from cli_agent_orchestrator.models.workflow_runtime import RunState
        from cli_agent_orchestrator.services import workflow_journal, workflow_service
        from cli_agent_orchestrator.services.script_runner import ScriptRunRecord

        run_id = f"run-bookkeeping-{uuid4().hex}"
        env_vars = {
            "CAO_WORKFLOW_RUN_ID": run_id,
            "CAO_WORKFLOW_GENERATION": "1",
            "CAO_WORKFLOW_STEP_ID": "step-1",
        }
        record = ScriptRunRecord(
            run_id=run_id,
            workflow_name="wf",
            state=RunState.RUNNING,
            cancelled=False,
            current_step_id=None,
            step_states={},
            process=None,
            generation="1",
            started_at="2026-07-15T00:00:00Z",
            finished_at=None,
        )
        monkeypatch.setitem(workflow_service.run_registry, run_id, record)
        monkeypatch.setattr(workflow_journal, "append_step", lambda *args, **kwargs: None)
        monkeypatch.setattr(workflow_journal, "update_step", lambda *args, **kwargs: None)

        exc = OutputExtractionError(_MARKER_MISSING)
        with (
            patch(
                "cli_agent_orchestrator.services.workflow_service.check_generation",
                return_value=None,
            ),
            patch(_RUN_STEP, new=AsyncMock(side_effect=exc)),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env_vars))

        assert resp.status_code == 500
        step = record.step_states["step-1"]
        assert step.state == StepState.FAILED
        assert step.attempts == 1
        assert step.error == _MARKER_MISSING
