"""Elastic worker provisioning and acknowledged completion tests."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.services import terminal_service


def test_assign_elastic_provisions_then_assigns(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "worker_id": "deadbeef",
        "target_host": "cao-worker-deadbeef.ns.svc.cluster.local",
        "working_directory": "/home/cao/workspace/jobs/deadbeef",
        "session_name": "cao-worker-deadbeef",
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
    assert assign.call_args.kwargs["remote_session_name"] == "cao-worker-deadbeef"
    assert "complete_assignment" in assign.call_args.args[1]


def test_assign_elastic_deferred_failure_reports_terminal_ended(monkeypatch):
    """Exercise the real elastic placement and deferred-session failure path."""
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    monkeypatch.setenv(server.ADVERTISED_URL_ENV, "http://cao-supervisor:9889")
    monkeypatch.setenv("CAO_ELASTIC_CALLBACK_URL", "http://broker:9890")

    lease = Mock(status_code=200)
    lease.raise_for_status.return_value = None
    lease.json.return_value = {
        "worker_id": "deadbeef",
        "target_host": "cao-worker-deadbeef.ns.svc.cluster.local",
        "working_directory": "/home/cao/workspace/jobs/deadbeef",
        "session_name": "cao-worker-deadbeef",
        "release_token": "release-token",
    }
    session = Mock(status_code=200)
    session.json.return_value = {
        "id": "def67890",
        "session_name": "cao-worker-deadbeef",
    }
    ok = Mock(status_code=200)
    ok.raise_for_status.return_value = None
    posts = []

    def post(url, *args, **kwargs):
        posts.append((url, kwargs))
        if url == "http://broker:9890/workers":
            return lease
        if url.endswith("/sessions"):
            return session
        return ok

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def exercise():
        result = await server.assign_elastic("developer", "Implement it")
        assert result["success"] is True

        # These are pod-level variables in production, injected by the broker
        # before the remote POST /sessions creates the terminal.
        monkeypatch.setenv("CAO_ELASTIC_WORKER_ID", "deadbeef")
        monkeypatch.setenv("CAO_ELASTIC_RELEASE_TOKEN", "release-token")
        provider = AsyncMock()
        provider.initialize.side_effect = RuntimeError("provider startup failed")
        before_tasks = set(terminal_service._deferred_init_tasks)
        terminal_service._schedule_deferred_init(
            provider,
            "def67890",
            "Implement it",
            OrchestrationType.ASSIGN,
            None,
        )
        (task,) = set(terminal_service._deferred_init_tasks) - before_tasks
        await task

    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "get", return_value=Mock(status_code=200)),
        patch.object(server.requests, "post", side_effect=post),
        patch.object(terminal_service.asyncio, "to_thread", inline_to_thread),
        patch.object(
            terminal_service,
            "get_terminal_metadata",
            return_value={"caller_id": None, "tmux_session": "cao-worker-deadbeef"},
        ),
        patch.object(
            terminal_service,
            "get_session_env",
            return_value={
                "CAO_CALLBACK_URL": "http://broker:9890",
                "CAO_CALLBACK_TERMINAL_ID": "abc12345",
            },
        ),
        patch.object(terminal_service, "delete_terminal") as delete,
    ):
        asyncio.run(exercise())

    urls = [url for url, _ in posts]
    assert any(url.endswith("/sessions") for url in urls)
    session_request = next(kwargs for url, kwargs in posts if url.endswith("/sessions"))
    assert session_request["params"]["session_name"] == "cao-worker-deadbeef"
    assert session_request["json"]["env_vars"]["CAO_CALLBACK_URL"] == "http://broker:9890"
    callback = next(
        kwargs for url, kwargs in posts if url.endswith("/terminals/abc12345/inbox/messages")
    )
    assert callback["headers"] == {
        "X-CAO-Worker-ID": "deadbeef",
        "X-CAO-Release-Token": "release-token",
    }
    assert "http://broker:9890/workers/deadbeef/terminal-ended" in urls
    terminal_ended = next(
        kwargs for url, kwargs in posts if url.endswith("/workers/deadbeef/terminal-ended")
    )
    assert terminal_ended["headers"] == {"X-CAO-Release-Token": "release-token"}
    delete.assert_called_once_with("def67890", registry=None)


def _lease_response():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "worker_id": "deadbeef",
        "target_host": "cao-worker-deadbeef.ns.svc.cluster.local",
        "working_directory": "/home/cao/workspace/jobs/deadbeef",
        "session_name": "cao-worker-deadbeef",
        "release_token": "release-token",
    }
    return response


def test_assign_elastic_omits_provider_so_the_broker_default_wins(monkeypatch):
    """A provider default in the tool signature would override the broker's.

    The provider a worker can actually run is a property of the deployment's
    image, so a caller that says nothing must leave the choice to the broker
    rather than silently requesting whatever this signature happens to name.
    """
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=_lease_response()) as post,
        patch.object(server, "_assign_impl", return_value={"success": True}),
    ):
        asyncio.run(server.assign_elastic("developer", "Implement it"))

    assert post.call_args.kwargs["json"] == {
        "agent_profile": "developer",
        "callback_terminal_id": "abc12345",
    }


def test_assign_elastic_forwards_an_explicit_provider(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=_lease_response()) as post,
        patch.object(server, "_assign_impl", return_value={"success": True}),
    ):
        asyncio.run(server.assign_elastic("developer", "Implement it", provider="claude_code"))

    assert post.call_args.kwargs["json"] == {
        "agent_profile": "developer",
        "callback_terminal_id": "abc12345",
        "provider": "claude_code",
    }


def test_assign_elastic_warns_the_worker_not_to_speak_first(monkeypatch):
    """The turn detector reads settled prose as end-of-turn and kills the window.

    A worker that opens with "working on it" is therefore terminated mid-task
    while the assignment still reports success, so the instruction that prevents
    it has to travel with every task.
    """
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=_lease_response()),
        patch.object(server, "_assign_impl", return_value={"success": True}) as assign,
    ):
        asyncio.run(server.assign_elastic("developer", "Implement it"))

    sent = assign.call_args.args[1]
    assert "BEFORE you write any prose" in sent
    assert "killed" in sent


def test_assign_elastic_releases_when_assignment_fails(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    create_response = Mock()
    create_response.raise_for_status.return_value = None
    create_response.json.return_value = {
        "worker_id": "deadbeef",
        "target_host": "worker",
        "working_directory": "/workspace/deadbeef",
        "session_name": "cao-worker-deadbeef",
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


# --- readiness: the caller waits on the Service, not on pod readiness --------
#
# The broker used to hold POST /workers open until the worker pod reported Ready,
# which cost the caller the worker's whole boot AND still handed back an address
# that was not yet routable (a Ready pod is not yet a Service with a published
# endpoint). The wait moved here, onto the address about to be used.


def test_wait_remote_ready_returns_on_the_first_healthy_answer():
    with patch.object(server.requests, "get", return_value=Mock(status_code=200)) as get:
        server._wait_remote_ready("http://worker:9889", 5.0)

    assert get.call_count == 1
    assert get.call_args.args[0] == "http://worker:9889/health"


def test_wait_remote_ready_polls_through_a_converging_service():
    """A refused connection while endpoints propagate is expected, not fatal."""
    responses = [
        server.requests.RequestException("Connection refused"),
        Mock(status_code=503),
        Mock(status_code=200),
    ]
    with (
        patch.object(server.requests, "get", side_effect=responses) as get,
        patch.object(server.time, "sleep") as sleep,
    ):
        server._wait_remote_ready("http://worker:9889", 5.0)

    assert get.call_count == 3
    # Sub-second polling: the gap being waited out is a second or two, so a 5s
    # backoff would spend the whole wait not looking.
    assert all(call.args[0] <= 0.5 for call in sleep.call_args_list)


def test_wait_remote_ready_raises_something_diagnosable_on_timeout():
    with (
        patch.object(
            server.requests, "get", side_effect=server.requests.RequestException("no route")
        ),
        patch.object(server.time, "sleep"),
    ):
        try:
            server._wait_remote_ready("http://worker:9889", 0.0)
            raise AssertionError("expected a ValueError")
        except ValueError as exc:
            message = str(exc)

    assert "http://worker:9889" in message
    assert "no route" in message
    assert "NetworkPolicy" in message


def _remote_session_response():
    response = Mock(status_code=200)
    response.json.return_value = {"id": "def67890", "session_name": "sess-1"}
    return response


def test_assign_remote_does_not_wait_by_default(monkeypatch):
    """Plain `assign` to a long-running node must stay byte-identical.

    A target_host naming a static pod is either up or genuinely broken, so a
    caller that did not just create it should still fail in seconds.
    """
    monkeypatch.setenv(server.ADVERTISED_URL_ENV, "http://cao-supervisor:9889")
    with (
        patch.object(server.requests, "post", return_value=_remote_session_response()),
        patch.object(server, "_wait_remote_ready") as wait,
    ):
        result = server._assign_remote(
            agent_profile="developer",
            worker_message="Implement it",
            current_terminal_id="abc12345",
            target_host="cao-worker-0",
            working_directory=None,
            engine=None,
            model=None,
            use_worktree=False,
        )

    assert result["success"] is True
    wait.assert_not_called()


def test_assign_remote_waits_before_it_posts_the_task(monkeypatch):
    """Order matters: POST /sessions is not idempotent, so it gets one shot."""
    monkeypatch.setenv(server.ADVERTISED_URL_ENV, "http://cao-supervisor:9889")
    calls = []
    with (
        patch.object(
            server.requests,
            "post",
            side_effect=lambda *a, **k: (calls.append("post"), _remote_session_response())[1],
        ),
        patch.object(
            server, "_wait_remote_ready", side_effect=lambda *a: calls.append("wait")
        ) as wait,
    ):
        server._assign_remote(
            agent_profile="developer",
            worker_message="Implement it",
            current_terminal_id="abc12345",
            target_host="cao-worker-deadbeef.ns.svc.cluster.local",
            working_directory=None,
            engine=None,
            model=None,
            use_worktree=False,
            ready_wait_seconds=30.0,
        )

    assert calls == ["wait", "post"]
    assert wait.call_args.args == ("http://cao-worker-deadbeef.ns.svc.cluster.local:9889", 30.0)


def test_assign_elastic_asks_the_assignment_to_wait_for_its_new_worker(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")
    monkeypatch.delenv("CAO_ELASTIC_WORKER_READY_WAIT", raising=False)
    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", return_value=_lease_response()),
        patch.object(server, "_assign_impl", return_value={"success": True}) as assign,
    ):
        asyncio.run(server.assign_elastic("developer", "Implement it"))

    assert assign.call_args.kwargs["ready_wait_seconds"] == 120.0


def test_elastic_ready_wait_is_tunable_and_survives_a_bad_value(monkeypatch):
    monkeypatch.setenv("CAO_ELASTIC_WORKER_READY_WAIT", "7.5")
    assert server._elastic_ready_wait() == 7.5
    monkeypatch.setenv("CAO_ELASTIC_WORKER_READY_WAIT", "-3")
    assert server._elastic_ready_wait() == 0.0
    monkeypatch.setenv("CAO_ELASTIC_WORKER_READY_WAIT", "soon")
    assert server._elastic_ready_wait() == 120.0


def test_assign_elastic_calls_overlap_instead_of_serialising(monkeypatch):
    """Fan-out is the whole point of an elastic fleet.

    Both blocking legs (the broker POST and the assignment itself) run off the
    event loop, so N delegations cost roughly one placement rather than N. With
    either leg back on the loop this takes ~3x as long, which is what a
    supervisor delegating five tasks used to pay.
    """
    monkeypatch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker:9890")
    monkeypatch.setenv("CAO_ELASTIC_BROKER_TOKEN", "broker-token")

    def slow_assign(*args, **kwargs):
        time.sleep(0.3)
        return {"success": True}

    def slow_post(*args, **kwargs):
        time.sleep(0.1)
        return _lease_response()

    async def three():
        return await asyncio.gather(
            *(server.assign_elastic("developer", f"Task {i}") for i in range(3))
        )

    with (
        patch.object(server, "_current_terminal_id", return_value="abc12345"),
        patch.object(server.requests, "post", side_effect=slow_post),
        patch.object(server, "_assign_impl", side_effect=slow_assign),
    ):
        started = time.monotonic()
        results = asyncio.run(three())
        elapsed = time.monotonic() - started

    assert all(r["success"] for r in results)
    assert elapsed < 0.8, f"three delegations took {elapsed:.2f}s; expected ~0.4s"
