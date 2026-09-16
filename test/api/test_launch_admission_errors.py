"""Safe HTTP transport for typed launch-admission refusals."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.scope_admission import (
    AgentAdmissionError,
    LaunchAdmissionError,
)


@pytest.fixture(autouse=True)
def _isolate_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "launch-admission-errors.db"
    assert database_path.parent == tmp_path
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE",
        database_path,
        raising=True,
    )


def _post_create(client, route: str):
    if route == "session":
        return client.post(
            "/sessions",
            params={"provider": "mock_cli", "agent_profile": "developer"},
        )
    return client.post(
        "/sessions/test-session/terminals",
        params={"provider": "mock_cli", "agent_profile": "developer"},
    )


@pytest.mark.parametrize(
    ("route", "error_type", "status_code", "code"),
    [
        ("session", AgentAdmissionError, 403, "agent_not_declared"),
        ("session", LaunchAdmissionError, 409, "agent_policy_unavailable"),
        ("session", LaunchAdmissionError, 422, "agent_policy_invalid"),
        ("terminal", LaunchAdmissionError, 403, "agent_not_declared"),
        ("terminal", AgentAdmissionError, 409, "agent_policy_unavailable"),
        ("terminal", LaunchAdmissionError, 422, "agent_policy_invalid"),
    ],
)
def test_create_routes_transport_only_safe_admission_fields(
    client,
    route: str,
    error_type: type[AgentAdmissionError],
    status_code: int,
    code: str,
) -> None:
    error = error_type(
        "SECRET_MESSAGE /private/path unsafe-value",
        code=code,
        status_code=status_code,
    )
    seam = (
        "cli_agent_orchestrator.api.main.session_service.create_session"
        if route == "session"
        else "cli_agent_orchestrator.api.main.terminal_service.create_terminal"
    )

    with patch(seam, new=AsyncMock(side_effect=error)):
        response = _post_create(client, route)

    assert response.status_code == status_code
    assert response.json() == {
        "code": code,
        "status_code": status_code,
        "retryable": False,
    }
    assert "SECRET_MESSAGE" not in response.text
    assert "/private/path" not in response.text
    assert "unsafe-value" not in response.text


def test_registered_handler_is_a_safe_backstop_without_local_route_catch(client) -> None:
    async def uncaught_admission_error() -> None:
        raise LaunchAdmissionError(
            "SECRET_BACKSTOP_MESSAGE",
            code="agent_policy_unavailable",
            status_code=409,
        )

    app.add_api_route(
        "/_test/uncaught-launch-admission",
        uncaught_admission_error,
        methods=["GET"],
    )
    added_route = app.router.routes[-1]
    try:
        response = client.get("/_test/uncaught-launch-admission")
    finally:
        app.router.routes.remove(added_route)

    assert response.status_code == 409
    assert response.json() == {
        "code": "agent_policy_unavailable",
        "status_code": 409,
        "retryable": False,
    }
    assert "SECRET_BACKSTOP_MESSAGE" not in response.text


@pytest.mark.parametrize(
    ("route", "expected_status"),
    [("session", 400), ("terminal", 404)],
)
def test_create_routes_preserve_plain_value_error_mapping(
    client,
    route: str,
    expected_status: int,
) -> None:
    seam = (
        "cli_agent_orchestrator.api.main.session_service.create_session"
        if route == "session"
        else "cli_agent_orchestrator.api.main.terminal_service.create_terminal"
    )

    with patch(seam, new=AsyncMock(side_effect=ValueError("legacy-value-error"))):
        response = _post_create(client, route)

    assert response.status_code == expected_status
    assert response.json() == {"detail": "legacy-value-error"}


def test_request_validation_still_uses_fastapi_422(client) -> None:
    response = client.post("/sessions")

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert response.json()["detail"][0]["type"] == "missing"


def test_missing_bearer_still_fails_auth_before_create(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH0_DOMAIN", "test.local")
    monkeypatch.setenv("AUTH0_AUDIENCE", "cao://test")
    with patch(
        "cli_agent_orchestrator.api.main.session_service.create_session",
        new=AsyncMock(side_effect=AssertionError("create must not run")),
    ):
        response = client.post(
            "/sessions",
            params={"provider": "mock_cli", "agent_profile": "developer"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}
