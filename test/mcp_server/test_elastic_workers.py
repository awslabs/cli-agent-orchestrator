"""Elastic worker provisioning and acknowledged completion tests."""

import asyncio
from unittest.mock import Mock, patch

from cli_agent_orchestrator.mcp_server import server


def test_assign_elastic_provisions_then_assigns(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "worker_id": "deadbeef",
        "target_host": "cao-worker-deadbeef.ns.svc.cluster.local",
        "working_directory": "/home/cao/workspace/jobs/deadbeef",
        "release_token": "release-token",
    }
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=response),
        patch.object(
            server,
            "_assign_impl",
            return_value={"success": True, "terminal_id": "def67890"},
        ) as assign,
    ):
        result = asyncio.run(server.assign_elastic("developer", "Implement it"))

    assert result["success"] is True
    assert result["worker_id"] == "deadbeef"
    assert result["elastic"] is True
    assert assign.call_args.args[2].endswith("/deadbeef")
    assert assign.call_args.kwargs["target_host"].startswith("cao-worker-deadbeef")
    assert "complete_assignment" in assign.call_args.args[1]


def test_assign_elastic_releases_when_assignment_fails(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    create_response = Mock()
    create_response.raise_for_status.return_value = None
    create_response.json.return_value = {
        "worker_id": "deadbeef",
        "target_host": "worker",
        "working_directory": "/workspace/deadbeef",
        "release_token": "release-token",
    }
    delete_response = Mock(status_code=200)
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=create_response),
        patch.object(server.requests, "delete", return_value=delete_response) as delete,
        patch.object(server, "_assign_impl", return_value={"success": False}),
    ):
        result = asyncio.run(server.assign_elastic("developer", "Implement it"))

    assert result["worker_released"] is True
    assert delete.call_args.args[0].endswith("/workers/deadbeef")


def test_complete_assignment_releases_only_after_delivery(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_WORKER_ID", "deadbeef")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_RELEASE_TOKEN", "release-token")
    response = Mock()
    response.raise_for_status.return_value = None
    with (
        patch.object(server, "_send_message_impl", return_value={"success": True}),
        patch.object(server.requests, "post", return_value=response) as post,
    ):
        result = asyncio.run(server.complete_assignment("Done"))

    assert result["success"] is True
    assert result["release_scheduled"] is True
    assert post.call_args.args[0].endswith("/workers/deadbeef/complete")
    assert post.call_args.kwargs["headers"]["X-CAO-Release-Token"] == "release-token"


def test_complete_assignment_keeps_worker_when_delivery_fails(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_WORKER_ID", "deadbeef")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_RELEASE_TOKEN", "release-token")
    with (
        patch.object(server, "_send_message_impl", return_value={"success": False}),
        patch.object(server.requests, "post") as post,
    ):
        result = asyncio.run(server.complete_assignment("Done"))

    assert result["success"] is False
    post.assert_not_called()
