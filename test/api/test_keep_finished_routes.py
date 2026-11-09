"""The two server entry points of keeping finished children (#1225 phase 1).

* ``POST /terminals/run-step`` keeps a handoff's worker only for the shape the
  handoff MCP tool sends; every other caller keeps its teardown.
* ``DELETE /terminals/{id}`` keeps only when the agent-side flag
  ``keep_if_enabled`` is sent; every operator delete deletes at once.

The keep decision itself is ``keep_service.keep_finished``, tested on a real store in
``test/services/test_keep_finished_service.py``; here it is a double so each route's
own wiring is what is under test.
"""

from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, patch

import pytest

from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.services import keep_service

UNTIL = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
KEPT = keep_service.KeepOutcome(kept=True, kept_until=UNTIL, note="It is kept until later.")
NOT_KEPT = keep_service.KeepOutcome(kept=False, note="Not kept: cap.")
_MAIN = "cli_agent_orchestrator.api.main"

HANDOFF = {
    "provider": "claude_code",
    "agent": "developer",
    "prompt": "do it",
    "session_name": "cao-session",
    "caller_id": "aaaaaaaa",
    "orchestration_type": "handoff",
    "delegation_message": "do it",
    "job_id": "0" * 32,
}


class FakeStep:
    """Stands in for run_agent_step: records the hook and runs it like the substrate."""

    def __init__(self):
        self.hook = "unset"
        self.torn_down = None

    async def __call__(self, **kwargs):
        self.hook = kwargs.get("keep_on_success")
        kept = self.hook("bbbbbbbb") if self.hook is not None else False
        self.torn_down = kwargs["teardown"] and kwargs["reuse_terminal_id"] is None and not kept
        return AgentStepResult(
            terminal_id="bbbbbbbb",
            last_message="done",
            status=TerminalStatus.COMPLETED,
            torn_down=self.torn_down,
        )


@pytest.fixture
def step():
    fake = FakeStep()
    with (
        patch(f"{_MAIN}.run_agent_step", new=fake),
        patch(f"{_MAIN}._prepare_delegation", new=AsyncMock(return_value=None)),
        patch(f"{_MAIN}._record_job_state", new=AsyncMock()),
    ):
        yield fake


def _post(client, **changes):
    body = {**HANDOFF, **changes}
    for key in [k for k, v in body.items() if v is None]:
        body.pop(key)
    return client.post(TERMINALS_RUN_STEP_ROUTE, json=body)


class TestRunStep:
    def test_a_handoff_worker_is_kept_and_the_response_says_until_when(self, client, step):
        with (
            patch.object(keep_service, "keep_finished", return_value=KEPT) as keep,
            patch(f"{_MAIN}._notify_elastic_terminal_ended") as ended,
        ):
            response = _post(client)
        assert response.status_code == 200
        body = response.json()
        assert body["terminal_id"] == "bbbbbbbb"
        assert body["kept_until"].startswith("2026-11-06T04:00:00")
        assert body["keep_note"] == "It is kept until later."
        keep.assert_called_once_with("bbbbbbbb", reason="handoff", registry=ANY)
        assert step.torn_down is False
        # A kept worker was not torn down, and #1203's field says so.
        assert body["torn_down"] is False
        ended.assert_not_called()

    def test_with_the_setting_off_the_response_is_unchanged(self, client, step):
        with patch.object(keep_service, "keep_finished", return_value=keep_service.NOT_APPLICABLE):
            response = _post(client)
        assert response.status_code == 200
        # Exactly #1203's body: torn_down reported, and no keep field at all.
        assert response.json() == {
            "terminal_id": "bbbbbbbb",
            "last_message": "done",
            "status": "completed",
            "torn_down": True,
            "replayed": False,
        }
        assert step.torn_down is True

    def test_a_worker_not_kept_for_the_cap_is_torn_down_and_the_reason_said(self, client, step):
        with patch.object(keep_service, "keep_finished", return_value=NOT_KEPT):
            body = _post(client).json()
        assert "kept_until" not in body and body["keep_note"] == "Not kept: cap."
        assert step.torn_down is True and body["torn_down"] is True

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"caller_id": None, "caller_ceiling": []}, id="remote-handoff"),
            pytest.param({"caller_id": None}, id="no-caller"),
            pytest.param(
                {"orchestration_type": None, "delegation_message": None}, id="not-a-handoff"
            ),
            pytest.param({"teardown": False}, id="teardown-false"),
            pytest.param(
                {
                    "orchestration_type": None,
                    "delegation_message": None,
                    "session_name": None,
                    "env_vars": {
                        "CAO_WORKFLOW_RUN_ID": "run-1",
                        "CAO_WORKFLOW_GENERATION": "g1",
                        "CAO_WORKFLOW_STEP_ID": "s1",
                    },
                },
                id="workflow-step",
            ),
            pytest.param(
                {
                    "session_name": None,
                    "env_vars": {
                        "CAO_WORKFLOW_RUN_ID": "run-1",
                        "CAO_WORKFLOW_GENERATION": "g1",
                        "CAO_WORKFLOW_STEP_ID": "s1",
                    },
                },
                # A script step that also sends the handoff fields through its opts
                # is still a workflow step, and keeps its teardown.
                id="workflow-step-in-a-handoff-shape",
            ),
        ],
    )
    def test_other_callers_get_no_keep_hook(self, client, step, changes):
        with (
            patch.object(keep_service, "keep_finished", return_value=KEPT) as keep,
            # The workflow-step body passes the generation fence and live-run check.
            patch("cli_agent_orchestrator.services.workflow_service.check_generation"),
            patch("cli_agent_orchestrator.services.script_runner.require_live_script_run"),
        ):
            response = _post(client, **changes)
        assert response.status_code == 200, response.text
        assert step.hook is None
        keep.assert_not_called()
        assert "kept_until" not in response.json()

    def test_a_reuse_call_gets_no_keep_hook(self, client, step):
        with patch.object(keep_service, "keep_finished", return_value=KEPT) as keep:
            response = _post(
                client,
                orchestration_type=None,
                delegation_message=None,
                session_name=None,
                caller_id=None,
                reuse_terminal_id="cccccccc",
                teardown=False,
            )
        assert response.status_code == 200
        assert step.hook is None
        keep.assert_not_called()


class TestDelete:
    def test_an_agent_delete_of_a_child_is_kept_when_the_setting_says_so(self, client):
        with (
            patch.object(keep_service, "keep_finished", return_value=KEPT) as keep,
            patch(f"{_MAIN}.terminal_service") as svc,
        ):
            response = client.delete("/terminals/abcd1234", params={"keep_if_enabled": "true"})
        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "kept": True,
            "kept_until": "2026-11-06T04:00:00+00:00",
            "keep_note": "It is kept until later.",
        }
        keep.assert_called_once_with("abcd1234", reason="delete_terminal", registry=ANY)
        svc.delete_terminal.assert_not_called()

    def test_an_operator_delete_never_asks_and_deletes_at_once(self, client):
        with (
            patch.object(keep_service, "keep_finished", return_value=KEPT) as keep,
            patch(f"{_MAIN}.terminal_service") as svc,
        ):
            svc.delete_terminal.return_value = True
            response = client.delete("/terminals/abcd1234")
        assert response.json() == {"success": True}
        keep.assert_not_called()
        svc.delete_terminal.assert_called_once_with("abcd1234", registry=ANY, end_reason="deleted")

    def test_with_the_setting_off_an_agent_delete_is_the_same_delete(self, client):
        with (
            patch.object(keep_service, "keep_finished", return_value=keep_service.NOT_APPLICABLE),
            patch(f"{_MAIN}.terminal_service") as svc,
        ):
            svc.delete_terminal.return_value = True
            response = client.delete("/terminals/abcd1234", params={"keep_if_enabled": "true"})
        assert response.json() == {"success": True}
        svc.delete_terminal.assert_called_once_with("abcd1234", registry=ANY, end_reason="deleted")

    def test_a_child_not_kept_is_deleted_and_the_reason_said(self, client):
        with (
            patch.object(keep_service, "keep_finished", return_value=NOT_KEPT),
            patch(f"{_MAIN}.terminal_service") as svc,
        ):
            svc.delete_terminal.return_value = True
            response = client.delete("/terminals/abcd1234", params={"keep_if_enabled": "true"})
        assert response.json() == {"success": True, "keep_note": "Not kept: cap."}

    def test_a_failing_keep_decision_falls_back_to_the_delete(self, client):
        with (
            patch.object(keep_service, "keep_finished", side_effect=RuntimeError("db locked")),
            patch(f"{_MAIN}.terminal_service") as svc,
        ):
            svc.delete_terminal.return_value = True
            response = client.delete("/terminals/abcd1234", params={"keep_if_enabled": "true"})
        assert response.json() == {"success": True}
        svc.delete_terminal.assert_called_once()

    def test_an_unknown_terminal_is_still_404(self, client):
        with (
            patch.object(keep_service, "keep_finished", return_value=keep_service.NOT_APPLICABLE),
            patch(f"{_MAIN}.terminal_service") as svc,
        ):
            svc.delete_terminal.side_effect = ValueError("Terminal 'deadbeef' not found")
            response = client.delete("/terminals/deadbeef", params={"keep_if_enabled": "true"})
        assert response.status_code == 404


def test_the_terminal_detail_shows_kept_and_its_expiry(client):
    detail = {
        "id": "abcd1234",
        "name": "w",
        "provider": "claude_code",
        "session_name": "cao-session",
        "agent_profile": "developer",
        "status": "idle",
        "kept": True,
        "kept_until": UNTIL,
        "last_active": None,
    }
    with patch(f"{_MAIN}.terminal_service.get_terminal", return_value=detail):
        body = client.get("/terminals/abcd1234").json()
    assert body["kept"] is True and body["kept_until"].startswith("2026-11-06T04:00:00")


def test_a_terminal_that_is_not_kept_reads_kept_false(client):
    detail = {
        "id": "abcd1234",
        "name": "w",
        "provider": "claude_code",
        "session_name": "cao-session",
        "status": "idle",
        "last_active": None,
    }
    with patch(f"{_MAIN}.terminal_service.get_terminal", return_value=detail):
        body = client.get("/terminals/abcd1234").json()
    assert body["kept"] is False and body["kept_until"] is None
