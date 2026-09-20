"""Bridge-mode elastic assignment (#745 step 4).

A bridge lease names a runtime the CENTRAL server routes to over the channel:
assignment goes through our own POST /runtimes/{id}/terminals, never a
per-worker Service. Server-mode leases keep the target_host path untouched.
"""

import asyncio
from unittest.mock import Mock, patch

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.utils import orchestration


def _bridge_lease():
    return {
        "worker_id": "deadbeef",
        "target_host": "cao-worker-deadbeef.ns.svc.cluster.local",
        "working_directory": "/home/cao/workspace/workers/deadbeef",
        "session_name": "cao-worker-deadbeef",
        "release_token": "release-token",
        "mode": "bridge",
        "runtime_id": "cao-worker-deadbeef",
        "provider": "claude_code",
    }


def test_assign_elastic_routes_a_bridge_lease_through_the_runtime_path(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    lease = Mock()
    lease.raise_for_status.return_value = None
    lease.json.return_value = _bridge_lease()
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(orchestration, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=lease),
        patch.object(
            server,
            "_assign_impl",
            return_value={"success": True, "terminal_id": "def67890"},
        ) as assign,
    ):
        result = asyncio.run(server.assign_elastic("developer", "Implement it"))

    assert result["success"] is True
    assert result["worker_id"] == "deadbeef"
    kwargs = assign.call_args.kwargs
    assert kwargs["runtime_id"] == "cao-worker-deadbeef"
    assert kwargs["provider"] == "claude_code"
    assert "target_host" not in kwargs
    assert kwargs["remote_session_name"] == "cao-worker-deadbeef"


def test_assign_bridge_posts_the_central_launch_with_callback_env(monkeypatch):
    monkeypatch.setenv(orchestration.ADVERTISED_URL_ENV, "http://cao-supervisor:9889")
    created = Mock(status_code=200)
    created.json.return_value = {"id": "def67890", "session_name": "cao-worker-deadbeef"}
    with patch.object(orchestration.requests, "post", return_value=created) as post:
        result = orchestration._assign_bridge(
            agent_profile="developer",
            worker_message="Implement it",
            current_terminal_id="abc12345",
            runtime_id="cao-worker-deadbeef",
            provider="claude_code",
            working_directory="/home/cao/workspace/workers/deadbeef",
            engine=None,
            model=None,
            use_worktree=False,
            remote_session_name="cao-worker-deadbeef",
        )

    assert result["success"] is True
    assert result["terminal_id"] == "def67890"
    assert result["runtime_id"] == "cao-worker-deadbeef"
    url = post.call_args.args[0]
    assert url == f"{API_BASE_URL}/runtimes/cao-worker-deadbeef/terminals"
    body = post.call_args.kwargs["json"]
    assert body["provider"] == "claude_code"
    assert body["session_name"] == "cao-worker-deadbeef"
    assert body["initial_message"] == "Implement it"
    env = body["env_vars"]
    assert env[orchestration.CALLBACK_URL_ENV] == "http://cao-supervisor:9889"
    assert env[orchestration.CALLBACK_TERMINAL_ID_ENV] == "abc12345"


def test_assign_bridge_waits_for_the_runtime_before_posting(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        resp = Mock(status_code=200)
        resp.raise_for_status.return_value = None
        # Connected on the second poll.
        connected = (
            {"runtimes": {"cao-worker-deadbeef": {}}} if len(calls) > 1 else {"runtimes": {}}
        )
        resp.json.return_value = connected
        return resp

    created = Mock(status_code=200)
    created.json.return_value = {"id": "def67890", "session_name": None}
    with (
        patch.object(orchestration.requests, "get", side_effect=fake_get),
        patch.object(orchestration.requests, "post", return_value=created),
        patch.object(orchestration.time, "sleep"),
    ):
        result = orchestration._assign_bridge(
            agent_profile="developer",
            worker_message="go",
            current_terminal_id="abc12345",
            runtime_id="cao-worker-deadbeef",
            provider="claude_code",
            working_directory=None,
            engine=None,
            model=None,
            use_worktree=False,
            ready_wait_seconds=30.0,
        )

    assert result["success"] is True
    assert len(calls) >= 2
    assert all(url.endswith("/runtimes") for url in calls)


def test_assign_bridge_surfaces_a_never_connecting_runtime(monkeypatch):
    resp = Mock(status_code=200)
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"runtimes": {}}
    fake_time = iter(range(0, 10000, 5))
    with (
        patch.object(orchestration.requests, "get", return_value=resp),
        patch.object(orchestration.time, "sleep"),
        patch.object(orchestration.time, "monotonic", side_effect=lambda: next(fake_time)),
        patch.object(orchestration.requests, "post") as post,
    ):
        import pytest

        # TimeoutError reaches _assign_impl's catch-all in real use (mapped to
        # a failed assignment); the launch POST must never fire.
        with pytest.raises(TimeoutError, match="did not connect"):
            orchestration._assign_bridge(
                agent_profile="developer",
                worker_message="go",
                current_terminal_id="abc12345",
                runtime_id="cao-worker-deadbeef",
                provider="claude_code",
                working_directory=None,
                engine=None,
                model=None,
                use_worktree=False,
                ready_wait_seconds=10.0,
            )

    post.assert_not_called()


def test_assign_bridge_maps_a_launch_error_to_a_diagnosable_failure(monkeypatch):
    failed = Mock(status_code=503)
    failed.json.return_value = {"detail": "runtime 'cao-worker-deadbeef' is not connected"}
    with patch.object(orchestration.requests, "post", return_value=failed):
        result = orchestration._assign_bridge(
            agent_profile="developer",
            worker_message="go",
            current_terminal_id="abc12345",
            runtime_id="cao-worker-deadbeef",
            provider="claude_code",
            working_directory=None,
            engine=None,
            model=None,
            use_worktree=False,
        )
    assert result["success"] is False
    assert "not connected" in result["message"]


def test_assign_impl_dispatches_runtime_id_before_target_host(monkeypatch):
    with (
        patch.object(orchestration, "_current_terminal_id", return_value="abc12345"),
        patch.object(
            orchestration, "_assign_bridge", return_value={"success": True, "terminal_id": "x"}
        ) as bridge,
        patch.object(orchestration, "_assign_remote") as remote,
    ):
        result = orchestration._assign_impl(
            "developer",
            "go",
            runtime_id="cao-worker-deadbeef",
            provider="claude_code",
            target_host="should-not-be-used",
        )
    assert result["success"] is True
    bridge.assert_called_once()
    remote.assert_not_called()
