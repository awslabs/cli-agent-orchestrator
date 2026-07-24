from unittest.mock import patch

IDENTITY = {
    "reservation_id": "reservation-1",
    "terminal_id": "deadbeef",
    "generation": "generation-1",
    "provider": "kimi_cli",
    "vintage": "v1",
}


def test_managed_control_identity_surface(client):
    with patch(
        "cli_agent_orchestrator.api.main.managed_launch.managed_control_identity",
        return_value=IDENTITY,
    ):
        response = client.get("/terminals/deadbeef/managed-control")

    assert response.status_code == 200
    assert response.json()["managed"] is True
    assert response.json()["generation"] == "generation-1"


def test_managed_input_uses_provider_control_not_tmux(client):
    receipt = {"state": "accepted", "provider_turn_id": "turn-1"}
    with (
        patch(
            "cli_agent_orchestrator.api.main.managed_launch.managed_control_identity",
            return_value=IDENTITY,
        ),
        patch(
            "cli_agent_orchestrator.api.main.managed_launch.begin_managed_session_operation",
            return_value=receipt,
        ) as begin,
        patch("cli_agent_orchestrator.api.main.terminal_service.send_input") as tmux_send,
    ):
        response = client.post(
            "/terminals/deadbeef/input",
            params={"message": "continue", "operation_id": "op-follow-up-1"},
        )

    assert response.status_code == 200
    assert response.json()["managed"] is True
    tmux_send.assert_not_called()
    assert begin.call_args.kwargs["action"] == "follow-up"
    assert begin.call_args.kwargs["message"] == "continue"
    assert begin.call_args.kwargs["operation_id"] == "op-follow-up-1"


def test_stale_generation_is_reported_as_conflict(client):
    from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

    with patch(
        "cli_agent_orchestrator.api.main.managed_launch.begin_managed_session_operation",
        side_effect=ManagedLaunchConflict("stale managed terminal generation"),
    ):
        response = client.post(
            "/terminals/deadbeef/managed-operations",
            json={
                "action": "route-query",
                "generation": "old-generation",
                "operation_id": "op-1",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "stale managed terminal generation"


def test_managed_exit_never_sends_raw_cli_input(client):
    with (
        patch(
            "cli_agent_orchestrator.api.main.managed_launch.managed_control_identity",
            return_value=IDENTITY,
        ),
        patch("cli_agent_orchestrator.api.main.terminal_service.exit_terminal_cli") as raw_exit,
    ):
        response = client.post("/terminals/deadbeef/exit")

    assert response.status_code == 409
    assert "raw CLI exit is disabled" in response.json()["detail"]
    raw_exit.assert_not_called()
