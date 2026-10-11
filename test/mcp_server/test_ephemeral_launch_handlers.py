"""Real orchestration handlers cross the claim, bind and release HTTP boundaries."""

import json
import sys
import threading
from test.services.test_ephemeral_bind import (  # noqa: F401
    CHILD,
    assert_released,
    audit,
    release_runtime,
    runtime_store,
)
from test.services.test_ephemeral_claim import claimed_store, row_update  # noqa: F401
from test.services.test_ephemeral_service import CALLER
from test.services.test_ephemeral_service import create_store as _create_store  # noqa: F401
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services import agent_step, terminal_service
from cli_agent_orchestrator.utils import orchestration


class CallCounter:
    """Observe calls without replacing either implementation."""

    def __init__(self):
        self.count = 0

    def assert_not_called(self):
        assert self.count == 0


@pytest.fixture
def create_store(_create_store, request):
    """Set the creator ceiling before claimed_store creates and records the spec."""
    tools = getattr(request.node, "callspec", None)
    if tools is not None and "creator_tools" in tools.params:
        with _create_store[1]() as db:
            db.get(database.TerminalModel, CALLER).allowed_tools = json.dumps(
                tools.params["creator_tools"]
            )
            db.commit()
    return _create_store


@pytest.fixture
def launch_http(release_runtime, monkeypatch):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_step, "_wait_for_completion", AsyncMock())
    monkeypatch.setattr(agent_step.frozen_run_memory, "frozen_memory_for", lambda *a: None)
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    monkeypatch.setattr(
        terminal_service.status_monitor, "get_status", lambda _: TerminalStatus.IDLE
    )
    monkeypatch.setattr(terminal_service, "send_input", lambda *a, **kw: None)
    monkeypatch.setattr(terminal_service, "get_output", lambda *a: "done")
    client = TestClient(main.app, base_url="http://localhost")
    calls = []

    def call(method, url, **kwargs):
        path = url.removeprefix(orchestration.API_BASE_URL)
        calls.append((method, path, kwargs))
        kwargs.pop("timeout", None)
        # Match requests' wire encoding (httpx stringifies str Enums differently).
        if kwargs.get("params") is not None:
            kwargs["params"] = {k: getattr(v, "value", v) for k, v in kwargs["params"].items()}
        return client.request(method, path, **kwargs)

    for method in ["get", "post", "delete"]:
        monkeypatch.setattr(
            orchestration.requests, method, lambda url, _m=method, **kw: call(_m, url, **kw)
        )
    # Keep both functions unmodified. Observe their client-side call frames
    # rather than replacing tool inheritance with a mock or a wrapped mock.
    inherit, resolve = CallCounter(), CallCounter()
    watched = {
        orchestration._resolve_child_allowed_tools.__code__: inherit,
        orchestration.resolve_provider.__code__: resolve,
    }
    previous, previous_thread = sys.getprofile(), threading.getprofile()

    def observe(frame, event, arg):
        if (
            event == "call"
            and frame.f_code in watched
            and frame.f_back is not None
            and frame.f_back.f_globals is orchestration.__dict__
        ):
            watched[frame.f_code].count += 1

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        yield release_runtime, client, calls, inherit, resolve
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_thread)
        client.close()


async def invoke(path, name, **kwargs):
    if path == "assign":
        return orchestration._assign_impl(name, "inspect", working_directory="/workspace", **kwargs)
    return (
        await orchestration._handoff_impl(
            name,
            "inspect",
            working_directory="/workspace",
            wait=path != "nowait",
            on_terminal_id=(lambda _: None) if path == "early" else None,
            **kwargs,
        )
    ).model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("creator_tools", [["@cao-mcp-server", "fs_read", "fs_list"], ["*"]])
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
async def test_recorded_tools_launch_and_teardown(launch_http, creator_tools, path):
    runtime, client, calls, inherit, resolve = launch_http
    env, name, backend, factory = runtime
    assert audit(name)["creator"]["tools"] == creator_tools
    result = await invoke(path, name)
    assert result["success"], result
    inherit.assert_not_called()
    resolve.assert_not_called()
    claims = [c for c in calls if c[1].endswith("/claim")]
    assert len(claims) == 1 and claims[0][2]["json"] == {}
    launches = [c for c in calls if c[0] == "post" and not c[1].endswith("/claim")]
    fresh = launches[0]
    payload = fresh[2]["json"] if path == "default" else fresh[2]["params"]
    tools = payload["allowed_tools"]
    assert (tools.split(",") if isinstance(tools, str) else tools) == ["fs_read", "@cao-mcp-server"]
    assert payload["claim_id"]
    assert factory.call_args_list[0].args[5] == ["fs_read", "@cao-mcp-server"]
    assert backend.create_window.call_count == 1
    if path in ["assign", "nowait"]:
        assert database.get_ephemeral_agent(name)["state"] == "launched"
        assert orchestration._delete_terminal_impl(CHILD)["success"]
    if path == "early":
        assert "claim_id" not in launches[-1][2]["json"]
    assert_released(env, name, "terminal_deleted")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
async def test_model_override_refuses_before_claim(launch_http, path):
    runtime, _, calls, inherit, resolve = launch_http
    result = await invoke(path, runtime[1], model="private-model")
    assert result["success"] is False
    assert result["rule"] == "model_override_not_allowed"
    assert "private-model" not in result["message"]
    assert not calls
    inherit.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["assign", "handoff"])
async def test_r2_precedes_claim(launch_http, monkeypatch, tool):
    runtime, _, calls, _, _ = launch_http
    monkeypatch.setattr(
        server,
        "_get_terminal_context_from_env",
        lambda: {"terminal_id": CALLER, "ephemeral": True, "allowed_tools": ["@cao-mcp-server"]},
    )
    result = await getattr(server, tool)(
        runtime[1], "inspect", engine=None, model=None, use_worktree=False, target_host=None
    )
    data = result if isinstance(result, dict) else result.model_dump()
    assert not data["success"]
    assert "ephemeral" in str(data)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait"])
async def test_disabled_policy_reaches_normal_result(launch_http, path):
    runtime, _, calls, _, _ = launch_http
    runtime[0][4]["ephemeral"]["enabled"] = False
    result = await invoke(path, runtime[1])
    assert not result["success"]
    assert result["rule"] == "ephemeral_disabled"
    assert result["message"] == "ephemeral policy: ephemeral_disabled"
    assert len(calls) == 1 and calls[0][1].endswith("/claim")


@pytest.mark.asyncio
async def test_keyed_replay_reattaches_without_new_terminal(launch_http):
    runtime, _, calls, inherit, resolve = launch_http
    first = await invoke("nowait", runtime[1], idempotency_key="retry")
    assert first["success"], first
    second = await invoke("nowait", runtime[1], idempotency_key="retry")
    assert second["success"] and second["terminal_id"] == first["terminal_id"]
    assert runtime[2].create_window.call_count == 1
    assert [c[2]["json"] for c in calls if c[1].endswith("/claim")] == [
        {"idempotency_key": "retry"},
        {"idempotency_key": "retry"},
    ]
    inherit.assert_not_called()
    resolve.assert_not_called()
    assert orchestration._delete_terminal_impl(CHILD)["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
async def test_missing_caller_is_not_owner(launch_http, monkeypatch, path):
    monkeypatch.delenv("CAO_TERMINAL_ID")
    result = await invoke(path, launch_http[0][1])
    assert not result["success"] and result["rule"] == "not_owner"
    assert not launch_http[2]


@pytest.mark.asyncio
async def test_claimed_key_retry_waits_for_lease(launch_http):
    from datetime import timedelta
    from test.services.test_ephemeral_claim import NOW

    runtime, _, _, _, _ = launch_http
    env, name, _, _ = runtime
    first = orchestration._claim_ephemeral(name, None, "retry")
    result = await invoke("nowait", name, idempotency_key="retry")
    assert not result["success"] and result["rule"] == "already_claimed"
    assert runtime[2].create_window.call_count == 0
    row_update(env, name, claim_expires_at=NOW - timedelta(seconds=1))
    retry = orchestration._claim_ephemeral(name, None, "retry")
    assert retry.claim_id != first.claim_id


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait"])
async def test_remote_reserved_target_refuses_before_calls(launch_http, path):
    result = await invoke(path, launch_http[0][1], target_host="remote")
    assert not result["success"]
    assert "remote_placement_not_allowed" in result["message"]
    assert not launch_http[2]


def test_installed_target_needs_no_claim(monkeypatch):
    post = Mock(side_effect=AssertionError("installed target must not claim"))
    monkeypatch.setattr(orchestration.requests, "post", post)
    assert orchestration._claim_ephemeral("developer", "override", "key") is None
    orchestration._require_claim("developer", None)
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("failure", ["network", "json", "jsondecode", "auth", "malformed"])
async def test_claim_relay_never_echoes_transport_content(launch_http, monkeypatch, path, failure):
    import requests

    secret = "creator text must never escape"
    response = Mock(status_code=401)
    response.json.return_value = {"detail": secret}
    if failure == "json":
        response.json.side_effect = ValueError(secret)
    elif failure == "jsondecode":
        response.json.side_effect = requests.exceptions.JSONDecodeError(secret, "", 0)
    elif failure == "malformed":
        response.status_code = 200
        response.json.return_value = {}
    post = Mock(return_value=response)
    if failure == "network":
        post.side_effect = requests.ConnectionError(secret)
    monkeypatch.setattr(orchestration.requests, "post", post)
    result = await invoke(path, launch_http[0][1])
    assert not result["success"]
    assert result["rule"] == (
        "creator_unresolved" if failure == "network" else "unexpected_failure"
    )
    assert secret not in str(result)


@pytest.mark.asyncio
async def test_default_handoff_does_not_forward_early_key(launch_http):
    result = await invoke("default", launch_http[0][1], idempotency_key="early-only")
    assert result["success"], result
    assert launch_http[2][0][2]["json"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["assign", "handoff"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_mcp_caller_sees_normal_claim_result(launch_http, tool, enabled):
    runtime, _, calls, inherit, resolve = launch_http
    runtime[0][4]["ephemeral"]["enabled"] = enabled
    kwargs = {"engine": None, "model": None, "use_worktree": False, "target_host": None}
    if tool == "handoff":
        kwargs["timeout"] = 600
    if server.ENABLE_WORKING_DIRECTORY:
        kwargs["working_directory"] = "/workspace"
    result = await getattr(server, tool)(runtime[1], "inspect", **kwargs)
    data = result if isinstance(result, dict) else result.model_dump()
    assert data["success"] is enabled, data
    if not enabled:
        assert data["rule"] == "ephemeral_disabled"
        assert data["message"] == "ephemeral policy: ephemeral_disabled"
    claim_call = next(i for i, call in enumerate(calls) if call[1].endswith("/claim"))
    assert claim_call > 0  # MCP authorization looked up the caller before claiming.
    inherit.assert_not_called()
    resolve.assert_not_called()
    if enabled and tool == "assign":
        assert orchestration._delete_terminal_impl(CHILD)["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
async def test_bind_refusal_relays_rule_without_launch_parameters(launch_http, monkeypatch, path):
    from test.services.test_ephemeral_claim import NOW

    runtime, _, _, _, _ = launch_http
    env, name, backend, factory = runtime
    original = orchestration._claim_ephemeral

    def lapse_after_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        row_update(env, name, claim_expires_at=NOW)
        return result

    monkeypatch.setattr(orchestration, "_claim_ephemeral", lapse_after_claim)
    result = await invoke(path, name)
    assert not result["success"]
    assert result["rule"] == "claim_expired"
    assert result["message"].startswith("ephemeral policy: claim_expired")
    for forbidden in [
        "http://",
        "https://",
        "claim_id",
        "caller_id",
        "working_directory",
        "allowed_tools",
        "/workspace",
    ]:
        assert forbidden not in str(result)
    assert database.get_ephemeral_agent(name)["state"] == "pending"
    assert database.get_terminal_metadata(CHILD) is None
    backend.create_window.assert_not_called()
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
@pytest.mark.parametrize("body", [{"detail": "private launch URL and parameters"}, [], None])
async def test_nonpolicy_bind_error_after_claim_is_generic(launch_http, monkeypatch, path, body):
    runtime, _, _, _, _ = launch_http
    post = orchestration.requests.post

    def reject_launch(url, **kwargs):
        if url.endswith("/claim"):
            return post(url, **kwargs)
        response = Mock(status_code=500)
        if body is None:
            response.json.side_effect = ValueError("private launch URL and parameters")
        else:
            response.json.return_value = body
        response.raise_for_status.side_effect = AssertionError("must sanitize before raising")
        return response

    monkeypatch.setattr(orchestration.requests, "post", reject_launch)
    result = await invoke(path, runtime[1])
    assert not result["success"]
    assert result["rule"] == "unexpected_failure"
    assert result["message"] == "ephemeral launch failed (500)"
    for forbidden in [
        "http://",
        "https://",
        "claim_id",
        "caller_id",
        "working_directory",
        "allowed_tools",
        "/workspace",
        "private",
    ]:
        assert forbidden not in str(result)
    assert database.get_terminal_metadata(CHILD) is None
    runtime[2].create_window.assert_not_called()
    runtime[3].assert_not_called()
