"""Tests for the combined POST /terminals/run-step endpoint (issue #312, N0).

Asserts the handler delegates to run_agent_step and maps domain failures to
HTTPException at the API boundary (SD-2.2 / project boundary-map rule).
"""

from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.services.agent_step import StepExecutionError

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"


@pytest.fixture
def isolated_journal(tmp_path, monkeypatch):
    """Point the workflow journal at a temp DB for one test (issue #583).

    Any test here that drives a SCRIPT-TIER call both WRITES durable journal rows
    (``settlement-rewire``) and READS them back before dispatch
    (``run-step-replay-branch``), so against the developer's real database the
    first run leaves rows that change the verdict of the next one — the test would
    pass once and then fail on every subsequent local run. Isolation is what makes
    the assertion about the response, not about the machine's history.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()


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
        assert kwargs["model"] is None

    def test_model_forwarded_to_substrate(self, client):
        result = AgentStepResult(
            terminal_id="abc12345", last_message="all done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(model="fable-5"))

        assert resp.status_code == 200
        assert m_run.await_args.kwargs["model"] == "fable-5"

    @pytest.mark.parametrize(
        "bad_model",
        [
            "fable-5\nrm -rf /",  # newline -- delivery hazard, not word-splitting
            "fable\x00-5",  # NUL
            "fable 5",  # whitespace
            "fable;5",  # shell metacharacter
            "x" * 129,  # exceeds MODEL_ID_MAX_LEN (128)
        ],
    )
    def test_invalid_model_returns_422_and_never_reaches_the_substrate(self, client, bad_model):
        """PR #501 review: a control character/newline/metacharacter in
        model must be rejected at the request boundary, not merely arrive
        shlex-quoted at a provider's launch command."""
        with patch(_RUN_STEP, new=AsyncMock()) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(model=bad_model))

        assert resp.status_code == 422
        m_run.assert_not_awaited()

    def test_valid_model_with_slash_and_dots_is_accepted(self, client):
        """OpenCode's "vendor/model" form, and dotted version suffixes, must
        not be rejected by the boundary check."""
        result = AgentStepResult(
            terminal_id="abc12345", last_message="all done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE, json=_body(model="anthropic/claude-3.5-sonnet")
            )

        assert resp.status_code == 200
        assert m_run.await_args.kwargs["model"] == "anthropic/claude-3.5-sonnet"

    def test_explicit_model_null_is_accepted_same_as_omitted(self, client):
        """Regression: Pydantic v2 does not run field_validators on a field
        that falls back to its default (validate_default=False, the
        default) -- so `model` omitted entirely never exercises
        validate_model's `if v is None` branch. An explicit `"model": null`
        in the request body does exercise it, and must be accepted exactly
        like omitting the field."""
        result = AgentStepResult(
            terminal_id="abc12345", last_message="all done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(model=None))

        assert resp.status_code == 200
        assert m_run.await_args.kwargs["model"] is None

    def test_matching_v2_reuse_constraints_are_forwarded(self, client):
        result = AgentStepResult(
            terminal_id="existing-v2",
            last_message="all done",
            status=TerminalStatus.COMPLETED,
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json=_body(reuse_terminal_id="existing-v2", engine="v2"),
            )

        assert resp.status_code == 200
        kwargs = m_run.await_args.kwargs
        assert kwargs["reuse_terminal_id"] == "existing-v2"
        assert kwargs["engine"] == "v2"

    def test_invalid_engine_is_422(self, client):
        with patch(_RUN_STEP, new=AsyncMock()) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(engine="v3"))

        assert resp.status_code == 422
        m_run.assert_not_awaited()

    def test_timeout_maps_to_504_with_structured_terminal_id(self, client):
        with patch(
            _RUN_STEP,
            new=AsyncMock(
                side_effect=StepExecutionError(
                    "terminal abc12345 did not complete",
                    kind="timeout",
                    terminal_id="abc12345",
                )
            ),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 504
        # Structured detail carries terminal_id + kind as fields (no scraping).
        detail = resp.json()["detail"]
        assert detail["kind"] == "timeout"
        assert detail["terminal_id"] == "abc12345"

    def test_worker_error_maps_to_502_with_structured_terminal_id(self, client):
        """A crashed worker (kind='error') is a distinct status (502) from a
        timeout (504), so the caller can tell 'crashed' from 'ran long'."""
        with patch(
            _RUN_STEP,
            new=AsyncMock(
                side_effect=StepExecutionError(
                    "terminal abc12345 reached ERROR status",
                    kind="error",
                    terminal_id="abc12345",
                )
            ),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["kind"] == "error"
        assert detail["terminal_id"] == "abc12345"

    def test_value_error_maps_to_404(self, client):
        with patch(_RUN_STEP, new=AsyncMock(side_effect=ValueError("Terminal 'x' not found"))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 404

    def test_unexpected_error_maps_to_500(self, client):
        with patch(_RUN_STEP, new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 500
        assert "boom" in resp.json()["detail"]

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [(TimeoutError("raw timeout"), 504), (RuntimeError("unexpected"), 500)],
    )
    def test_untyped_failure_settles_script_step_failed(
        self, client, monkeypatch, isolated_journal, exc, expected_status
    ):
        """Failures outside StepExecutionError must not leave a script step RUNNING."""
        from cli_agent_orchestrator.models.workflow import StepState
        from cli_agent_orchestrator.models.workflow_runtime import RunState
        from cli_agent_orchestrator.services import workflow_journal, workflow_service
        from cli_agent_orchestrator.services.script_runner import ScriptRunRecord

        # ONE RUN ID PER PARAMETRISATION (issue #583, unit ``run-step-replay-branch``).
        # Both cases settle a DURABLE journal row for their (run_id, step_id) key, and
        # a script-tier run-step call now consults the replay gate before dispatch —
        # so a shared key made the second case arrive at a row the first case had
        # already settled, and the gate correctly halted it (409
        # ``provenance_unverifiable``: settled, but with no current-scheme
        # fingerprint) instead of reaching the failure arm under test. Each case is
        # independent by intent; the shared key was invisible until the route became
        # journal-aware.
        run_id = f"run-bookkeeping-{type(exc).__name__.lower()}"
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

        with (
            patch(
                "cli_agent_orchestrator.services.workflow_service.check_generation",
                return_value=None,
            ),
            patch(_RUN_STEP, new=AsyncMock(side_effect=exc)),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env_vars))

        assert resp.status_code == expected_status
        step = record.step_states["step-1"]
        assert step.state == StepState.FAILED
        assert step.attempts == 1
        assert step.error == str(exc)

    def test_missing_required_field_is_422(self, client):
        # Pydantic request-model validation rejects a missing prompt.
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json={"provider": "p", "agent": "a"})
        assert resp.status_code == 422

    def test_use_worktree_defaults_to_false_and_is_forwarded(self, client):
        """issue #100 Phase 1: the field is unconditionally forwarded (not
        omitted-when-falsy like the Optional fields above), so a caller that
        never mentions it still gets an explicit False into run_agent_step --
        no ambiguity between 'not set' and 'set to false'."""
        result = AgentStepResult(
            terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())

        assert resp.status_code == 200
        assert m_run.await_args.kwargs["use_worktree"] is False

    def test_use_worktree_true_is_forwarded(self, client):
        result = AgentStepResult(
            terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(use_worktree=True))

        assert resp.status_code == 200
        assert m_run.await_args.kwargs["use_worktree"] is True

    def test_worktree_error_maps_to_400_not_500(self, client):
        """A working_directory that isn't a git repo (or a failed
        'git worktree add') is a client-input problem, not a server crash --
        distinct from the generic 500 fallback."""
        from cli_agent_orchestrator.services.worktree_service import WorktreeError

        with patch(
            _RUN_STEP,
            new=AsyncMock(side_effect=WorktreeError("'/tmp/x' is not inside a git repository")),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(use_worktree=True))

        assert resp.status_code == 400
        assert "not inside a git repository" in resp.json()["detail"]

    def test_terminal_input_blocked_error_maps_to_504_not_500(self, client):
        """PR #539 review (call-me-ram, round 3, "ask 1"): by this head,
        TerminalInputBlockedError is no longer raised by initialize()'s
        init-failure fallback -- that outer raise was reverted back to a bare
        TimeoutError in round 3, once send_input's own WAITING_USER_ANSWER
        guard took over as the keep-alive signal for the deferred-init path.
        The one producer that can still reach run_agent_step's synchronous
        `terminal_service.send_input` call (which never states an
        orchestration_type, so the WAITING guard itself can't fire here) is
        send_input's ERROR-state guard: a terminal whose provider process has
        already exited (or flips to ERROR between the readiness wait and the
        send) raises TerminalInputBlockedError to refuse pasting into a dead
        terminal. Without a dedicated except-arm for that, it would fall
        through to the generic `except Exception -> 500` below -- silently
        shifting this synchronous caller's (handoff MCP client, future run
        engine) response from 504 Gateway Timeout to 500 Internal Server
        Error, and from the structured {message, kind, terminal_id} shape to
        a plain string. This confirms the restored 504 + structured
        kind="timeout" body for that ERROR-state case."""
        from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError

        with patch(
            _RUN_STEP,
            new=AsyncMock(
                side_effect=TerminalInputBlockedError(
                    "Terminal abc12345 provider is in ERROR state "
                    "(provider process may have exited). Refusing to deliver input."
                )
            ),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 504
        detail = resp.json()["detail"]
        assert detail["kind"] == "timeout"
