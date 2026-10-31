"""Remote-runner failures keep their meaning over HTTP (#745).

A call that never left the server (503), one whose outcome is unknown (504)
and one the runner executed and reported as failed (502) are different facts
for a client: only the first is safe to retry blindly, and none of them is a
generic server crash. Each terminal and session route that reaches the backend
must surface them with their own status code instead of folding them into 500.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.runner.protocol import (
    RunnerError,
    RunnerOutcomeUnknown,
    RunnerReportedError,
    RunnerUnavailable,
)

ERRORS = [
    (RunnerUnavailable("runner a is not connected; nothing was sent"), 503),
    (RunnerOutcomeUnknown("runner a: no reply within 30s; outcome unknown"), 504),
    (RunnerReportedError("runner a: tmux failed"), 502),
]

# (method, path, patched service, attribute, query)
ROUTES = [
    ("get", "/terminals/abcd1234", "terminal_service", "get_terminal", None),
    (
        "get",
        "/terminals/abcd1234/working-directory",
        "terminal_service",
        "get_working_directory",
        None,
    ),
    ("post", "/terminals/abcd1234/input", "terminal_service", "dispatch_input", {"message": "hi"}),
    ("post", "/terminals/abcd1234/key", "terminal_service", "send_special_key", {"key": "Enter"}),
    ("get", "/terminals/abcd1234/output", "terminal_service", "get_output", None),
    ("post", "/terminals/abcd1234/exit", "terminal_service", "exit_terminal_cli", None),
    ("delete", "/terminals/abcd1234", "terminal_service", "delete_terminal", None),
    ("get", "/sessions/cao-remote", "session_service", "get_session", None),
    ("delete", "/sessions/cao-remote", "session_service", "delete_session", None),
]


def test_the_status_codes_are_fixed_per_class():
    assert RunnerUnavailable.status_code == 503
    assert RunnerOutcomeUnknown.status_code == 504
    assert RunnerReportedError.status_code == 502
    assert all(issubclass(cls, RunnerError) for cls in (type(e) for e, _ in ERRORS))


@pytest.mark.parametrize("error,expected", ERRORS, ids=["503", "504", "502"])
@pytest.mark.parametrize(
    "method,path,service,attribute,query", ROUTES, ids=[f"{r[0]} {r[1]}" for r in ROUTES]
)
def test_route_maps_runner_errors(client, method, path, service, attribute, query, error, expected):
    with patch(f"cli_agent_orchestrator.api.main.{service}") as svc:
        getattr(svc, attribute).side_effect = error
        response = getattr(client, method)(path, params=query)
    assert response.status_code == expected, response.text
    assert str(error) in response.json()["detail"]


@pytest.mark.parametrize("error,expected", ERRORS, ids=["503", "504", "502"])
def test_a_wrapped_runner_error_keeps_its_status(client, error, expected):
    # Session teardown re-raises backend failures as RuntimeError(...) from e.
    try:
        raise RuntimeError("could not verify tmux session liveness during teardown") from error
    except RuntimeError as wrapped:
        failure = wrapped
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.delete_session.side_effect = failure
        response = client.delete("/sessions/cao-remote")
    assert response.status_code == expected, response.text
    assert "teardown" in response.json()["detail"]


@pytest.mark.parametrize("error,expected", ERRORS, ids=["503", "504", "502"])
def test_run_step_maps_runner_errors(client, error, expected):
    from unittest.mock import AsyncMock

    from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE

    with patch("cli_agent_orchestrator.api.main.run_agent_step", new=AsyncMock(side_effect=error)):
        response = client.post(
            TERMINALS_RUN_STEP_ROUTE,
            json={"provider": "claude_code", "agent": "developer", "prompt": "do it"},
        )
    assert response.status_code == expected, response.text


def test_a_non_runner_backend_failure_is_still_a_500(client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as svc:
        svc.get_working_directory.side_effect = RuntimeError("tmux exploded")
        response = client.get("/terminals/abcd1234/working-directory")
    assert response.status_code == 500


def test_a_remote_error_reply_decodes_to_the_502_class():
    from cli_agent_orchestrator.runner.protocol import raise_remote_error

    with pytest.raises(RunnerReportedError):
        raise_remote_error({"kind": "backend_error", "message": "boom"}, "a")
    with pytest.raises(RunnerReportedError):
        raise_remote_error({"kind": "something new"}, "a")
