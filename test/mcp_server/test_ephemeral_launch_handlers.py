"""Real orchestration handlers cross the claim, bind and release HTTP boundaries."""

import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
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
from cli_agent_orchestrator.services.terminal_service import get_output as _real_get_output
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


@contextmanager
def record_decision_calls():
    """Observe every decision-package call, including calls in the HTTP portal thread."""
    calls = []
    previous, previous_thread = sys.getprofile(), threading.getprofile()

    def observe(frame, event, arg):
        if event == "call":
            module = frame.f_globals.get("__name__", "")
            function = frame.f_code.co_name
            if module.startswith("cli_agent_orchestrator.decisions.") and (
                function in ("prepare_launch", "_ask") or function.startswith("decide")
            ):
                calls.append(f"{module}.{function}")

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_thread)


@pytest.fixture
def askable_decisions(launch_http, tmp_path, monkeypatch):
    targets = Path(__file__).parents[2] / "src/cli_agent_orchestrator/decisions/targets.py"
    if not targets.exists():
        pytest.skip("decision module absent: delegation decision sites have not landed")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import (
        load_settings,
        set_point,
        set_table,
        set_tier,
    )
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.decisions.types import PointState
    from cli_agent_orchestrator.models.agent_profile import AgentProfile
    from cli_agent_orchestrator.services import settings_service

    db = create_engine("sqlite:///" + str(tmp_path / "decisions.db"))
    DecisionRecordModel.__table__.create(db)
    store = DecisionStore(
        sessionmaker(bind=db), tmp_path / "decision-hash.key", emit=lambda _e: None
    )
    engine = DecisionEngine(
        DeciderRegistry({"fixed_table": FixedTableDecider}),
        store,
        ShadowRunner(store),
        settings_loader=load_settings,
        profile_loader=lambda _: AgentProfile(name="worker", description="worker", model="model-y"),
        honors_model=lambda *args: True,
    )
    req = DelegationRequest("assign", "codex", "worker", None, "sentinel-body")
    for key in ("CAO_DECISION_MODEL_ROUTE", "CAO_DECISION_EFFORT_ROUTE"):
        monkeypatch.delenv(key, raising=False)
    set_point("model.route", "on", "fixed_table")
    set_tier("codex", "small", "model-x")
    set_table("model.route", "small", profile=req.agent_profile)
    settings = load_settings()
    assert settings.points["model.route"].state == PointState.ON
    assert settings.points["model.route"].decider == "fixed_table"
    assert (
        json.loads(settings_service.SETTINGS_FILE.read_text())["decisions"]["points"][
            "model.route"
        ]["state"]
        == "on"
    )
    monkeypatch.setattr(app.state, "decision_engine", engine, raising=False)
    yield req, store
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
async def test_ephemeral_launch_does_not_ask_decider(
    launch_http, askable_decisions, path, monkeypatch
):
    from cli_agent_orchestrator.api import main

    guard_calls, launches = [], []
    original_guard = main._claimed_ephemeral
    original_launch = terminal_service._create_terminal_unguarded

    def observe_guard(agent_profile, claim_id):
        result = original_guard(agent_profile, claim_id)
        guard_calls.append((agent_profile, claim_id, result))
        return result

    async def observe_launch(**kwargs):
        assert "launch_plan" in kwargs
        launches.append((kwargs["claim_id"], kwargs["launch_plan"]))
        return await original_launch(**kwargs)

    monkeypatch.setattr(main, "_claimed_ephemeral", observe_guard)
    monkeypatch.setattr(terminal_service, "_create_terminal_unguarded", observe_launch)
    name = launch_http[0][1]
    # The real HTTP decision guard must run with a claim, not be bypassed by
    # accidentally dropping delegation metadata. Observe the actual decider too.
    with record_decision_calls() as calls:
        result = await invoke(path, name)
    assert result["success"], result
    assert calls == [], calls
    assert len(guard_calls) == len(launches) == 1
    assert guard_calls[0][0] == name and guard_calls[0][2] is True
    assert guard_calls[0][1] and launches[0] == (guard_calls[0][1], None)
    fresh = next(
        kwargs
        for method, url, kwargs in launch_http[2]
        if method == "post" and not url.endswith("/claim")
    )
    assert fresh["json"]["delegation_message"] == "inspect"


@pytest.mark.asyncio
async def test_no_decider_recorder_captures_real_fixed_table_launch(askable_decisions):
    from cli_agent_orchestrator.decisions.engine import prepare_launch

    req, store = askable_decisions
    with record_decision_calls() as calls:
        plan = await prepare_launch(req)
    assert {
        "cli_agent_orchestrator.decisions.engine.prepare_launch",
        "cli_agent_orchestrator.decisions.engine._ask",
        "cli_agent_orchestrator.decisions.fixed_table.decide",
    } <= set(calls), calls
    assert plan.model == "model-x"
    assert store.list()[0]["outcome"] == "applied"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
@pytest.mark.parametrize("failure", ["post", "metadata"])
async def test_postclaim_exception_is_generic_before_terminal_exists(
    launch_http, monkeypatch, caplog, path, failure
):
    from datetime import timedelta
    from test.services.test_ephemeral_claim import NOW

    import requests

    runtime, _, _, _, _ = launch_http
    env, name, backend, factory = runtime
    monkeypatch.setattr(env[0].secrets, "token_hex", lambda n: "c" * (2 * n))
    original_post = orchestration.requests.post
    original_get = orchestration.requests.get
    leak = (
        "http://127.0.0.1:54321 url: /sessions/s/terminals?caller_id="
        + CALLER
        + "&claim_id="
        + "c" * 32
        + "&working_directory=%2Fworkspace&allowed_tools=fs_read"
    )

    def reject_post(url, **kwargs):
        if url.endswith("/claim"):
            return original_post(url, **kwargs)
        raise requests.ConnectionError(leak)

    def reject_metadata(url, **kwargs):
        response = requests.Response()
        response.status_code = 500
        response.url = url
        return response

    if failure == "post":
        monkeypatch.setattr(orchestration.requests, "post", reject_post)
    else:
        monkeypatch.setattr(orchestration.requests, "get", reject_metadata)
    result = await invoke(path, name)
    assert not result["success"]
    assert result["rule"] == "unexpected_failure"
    assert result["message"] == "ephemeral launch failed"
    for forbidden in [
        "http://",
        "claim_id",
        "caller_id",
        "working_directory",
        "allowed_tools",
        "/workspace",
        CALLER,
    ]:
        assert forbidden not in str(result)
    expected_class = "ConnectionError" if failure == "post" else "HTTPError"
    warnings = [r.getMessage() for r in caplog.records if r.name == orchestration.logger.name]
    assert warnings == ["ephemeral launch failed after claim: " + expected_class]
    backend.create_window.assert_not_called()
    factory.assert_not_called()
    assert database.get_terminal_metadata(CHILD) is None
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "claimed" and row["claim_id"] == "c" * 32
    assert row["claim_expires_at"] == NOW + timedelta(seconds=60)
    # Production makes no compensation call: a later claim lapses the lease.
    monkeypatch.setattr(orchestration.requests, "post", original_post)
    monkeypatch.setattr(orchestration.requests, "get", original_get)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
    monkeypatch.setattr(env[0].secrets, "token_hex", lambda n: "d" * (2 * n))
    retry = orchestration._claim_ephemeral(name, None)
    assert retry.claim_id == "d" * 32


@pytest.mark.asyncio
async def test_postclaim_failure_after_terminal_exists_keeps_existing_text(
    launch_http, monkeypatch
):
    import requests

    post = orchestration.requests.post

    def fail_submission(url, **kwargs):
        if url.endswith("/input"):
            raise requests.ConnectionError("submission failed")
        return post(url, **kwargs)

    monkeypatch.setattr(orchestration.requests, "post", fail_submission)
    result = await invoke("nowait", launch_http[0][1])
    assert not result["success"] and result["terminal_id"] == CHILD
    assert result["rule"] is None
    assert result["message"] == "Handoff failed: submission failed"
    assert database.get_ephemeral_agent(launch_http[0][1])["state"] == "launched"
    assert orchestration._delete_terminal_impl(CHILD)["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
async def test_failure_without_claim_keeps_existing_text(launch_http, monkeypatch, path):
    import requests

    def fail_metadata(url, **kwargs):
        response = requests.Response()
        response.status_code = 500
        response.url = url
        return response

    monkeypatch.setattr(orchestration.requests, "get", fail_metadata)
    result = await invoke(path, "developer")
    assert not result["success"]
    assert result.get("rule") is None
    assert "500 Server Error" in result["message"] and CALLER in result["message"]
    launch_http[0][2].create_window.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "error"])
async def test_claimed_default_handoff_preserves_real_worker_outcome(
    launch_http, monkeypatch, outcome
):
    runtime, _, _, _, _ = launch_http
    name = runtime[1]
    message = f"terminal {CHILD} reached ERROR status" if outcome == "error" else "step timeout"
    monkeypatch.setattr(
        agent_step,
        "_wait_for_completion",
        AsyncMock(
            side_effect=agent_step.StepExecutionError(message, kind=outcome, terminal_id=CHILD)
        ),
    )
    original_post = orchestration.requests.post
    responses = []

    def record_run_step(url, **kwargs):
        response = original_post(url, **kwargs)
        if url.endswith("/run-step"):
            responses.append(response)
        return response

    monkeypatch.setattr(orchestration.requests, "post", record_run_step)
    result = await invoke("default", name)
    assert responses[-1].status_code == {"timeout": 504, "error": 502}[outcome]
    monkeypatch.setattr(orchestration.requests, "post", lambda *a, **k: responses[-1])
    legacy = await orchestration._run_step_and_build_result({}, name, "claude_code", 600, 0)
    assert not result["success"] and result["rule"] is None
    assert result["message"] == legacy.message
    assert result["terminal_id"] == legacy.terminal_id == CHILD
    expected = {
        "timeout": "Handoff timed out after 600 seconds",
        "error": f"Handoff failed: worker errored (terminal {CHILD} reached ERROR status)",
    }
    assert result["message"] == expected[outcome]
    for forbidden in [
        "http://",
        "https://",
        "claim_id",
        "caller_id",
        "working_directory",
        "allowed_tools",
        "/workspace",
        CALLER,
    ]:
        assert forbidden not in str(result)
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "launched" and database.get_terminal_metadata(CHILD) is not None
    monkeypatch.setattr(orchestration.requests, "post", original_post)
    assert orchestration._delete_terminal_impl(CHILD)["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("timeout", 504), ("error", 502)])
async def test_claimed_handoff_known_worker_kind_without_id_keeps_existing_text(
    launch_http, monkeypatch, kind, status
):
    import requests

    post = orchestration.requests.post

    def worker_response(url, **kwargs):
        if url.endswith("/claim"):
            return post(url, **kwargs)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(
            {"detail": {"kind": kind, "message": "worker outcome", "terminal_id": None}}
        ).encode()
        return response

    monkeypatch.setattr(orchestration.requests, "post", worker_response)
    result = await invoke("default", launch_http[0][1])
    assert not result["success"] and result["rule"] is None and result["terminal_id"] is None
    assert result["message"] == (
        "Handoff timed out after 600 seconds"
        if kind == "timeout"
        else "Handoff failed: worker errored (worker outcome)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,rule",
    [(404, "unexpected_failure"), (403, "unexpected_failure"), ("transport", "creator_unresolved")],
)
async def test_replay_lookup_distinguishes_http_response_from_transport(
    launch_http, monkeypatch, failure, rule
):
    import requests

    runtime = launch_http[0]
    first = await invoke("nowait", runtime[1], idempotency_key="replay-error")
    assert first["success"]
    original_get = orchestration.requests.get

    def replay_get(url, **kwargs):
        if url.endswith("/" + CHILD):
            if failure == "transport":
                raise requests.ConnectionError("private replay URL " + url)
            response = requests.Response()
            response.status_code = failure
            response.url = url
            return response
        return original_get(url, **kwargs)

    monkeypatch.setattr(orchestration.requests, "get", replay_get)
    try:
        result = await invoke("nowait", runtime[1], idempotency_key="replay-error")
        assert not result["success"] and result["rule"] == rule
        assert result["message"] == (
            "ephemeral policy: creator_unresolved cao-server could not be reached"
            if failure == "transport"
            else "ephemeral policy: unexpected_failure"
        )
        assert "http://" not in str(result) and "https://" not in str(result)
        assert runtime[2].create_window.call_count == 1
    finally:
        monkeypatch.setattr(orchestration.requests, "get", original_get)
        assert orchestration._delete_terminal_impl(CHILD)["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed_tail", [False, True])
@pytest.mark.parametrize("delete_fails", [False, True])
async def test_real_extraction_failure_identifies_worker_and_cleanup_state(
    launch_http, monkeypatch, fixed_tail, delete_fails
):
    from cli_agent_orchestrator.providers.base import OutputExtractionError

    runtime, _, calls, _, _ = launch_http
    env, name, backend, factory = runtime
    provider = factory.return_value
    provider.extraction_tail_lines = 200 if fixed_tail else None
    provider.extraction_retries = 0
    provider.allow_raw_transcript_fallback = False
    provider.extract_last_message_from_script.side_effect = ValueError("no completion marker")
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda _: provider)
    monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda _: "screen contents")
    backend.get_history.return_value = "screen contents"
    originals = []

    def real_failure(*args, **kwargs):
        try:
            return _real_get_output(*args, **kwargs)
        except OutputExtractionError as exc:
            originals.append(exc)
            raise

    monkeypatch.setattr(terminal_service, "get_output", real_failure)
    delete = terminal_service.delete_terminal
    if delete_fails:
        monkeypatch.setattr(
            terminal_service, "delete_terminal", Mock(side_effect=RuntimeError("cleanup failed"))
        )
    original_post = orchestration.requests.post
    responses = []

    def record_response(url, **kwargs):
        response = original_post(url, **kwargs)
        if url.endswith("/run-step"):
            responses.append(response)
        return response

    monkeypatch.setattr(orchestration.requests, "post", record_response)
    result = await invoke("default", name)
    assert len(originals) == 1
    assert orchestration._terminal_id_from_detail(str(originals[0])) is None
    response = responses[-1]
    assert response.status_code == 500 and isinstance(response.json()["detail"], str)
    expected = f"terminal {CHILD}: {originals[0]}"
    assert response.json()["detail"] == expected
    assert result["rule"] is None and result["terminal_id"] == CHILD
    assert result["message"] == "Handoff failed: " + expected
    monkeypatch.setattr(orchestration.requests, "post", lambda *a, **k: response)
    legacy = await orchestration._run_step_and_build_result({}, name, "claude_code", 600, 0)
    assert (legacy.message, legacy.terminal_id, legacy.rule) == (result["message"], CHILD, None)
    job_id = next(c[2]["json"]["job_id"] for c in calls if c[1].endswith("/run-step"))
    job = database.get_handoff_result(job_id)
    assert job["state"] == "error" and job["error_message"] == expected
    for forbidden in [
        "http://",
        "https://",
        "claim_id",
        "caller_id",
        "working_directory",
        "allowed_tools",
        "/workspace",
        CALLER,
    ]:
        assert forbidden not in str(result)
    if delete_fails:
        assert database.get_ephemeral_agent(name)["state"] == "launched"
        assert database.get_terminal_metadata(CHILD) is not None
        monkeypatch.setattr(terminal_service, "delete_terminal", delete)
        monkeypatch.setattr(orchestration.requests, "post", original_post)
        assert orchestration._delete_terminal_impl(CHILD)["success"]
    else:
        assert database.get_ephemeral_agent(name)["state"] == "gc"
        assert database.get_terminal_metadata(CHILD) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_claimed_handoff_legacy_worker_status_is_not_launch_failure(
    launch_http, monkeypatch, status
):
    import requests

    original_post = orchestration.requests.post

    def legacy_response(url, **kwargs):
        if url.endswith("/claim"):
            return original_post(url, **kwargs)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({"detail": "legacy worker failure"}).encode()
        return response

    monkeypatch.setattr(orchestration.requests, "post", legacy_response)
    result = await invoke("default", launch_http[0][1])
    assert not result["success"] and result["rule"] is None
    assert result["message"] == (
        "Handoff timed out after 600 seconds"
        if status == 504
        else "Handoff failed: worker errored (legacy worker failure)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default", "nowait", "early"])
async def test_claimed_launch_persists_description(launch_http, monkeypatch, path):
    runtime, _, calls, _, _ = launch_http
    env, name, _, _ = runtime
    description = "Inspect the merge boundary"
    snapshots = []
    delete = terminal_service.delete_terminal

    def observe_delete(terminal_id, **kwargs):
        snapshots.append(database.get_terminal_metadata(terminal_id))
        return delete(terminal_id, **kwargs)

    monkeypatch.setattr(terminal_service, "delete_terminal", observe_delete)
    result = await invoke(path, name, description=description)
    assert result["success"], result
    launches = [c for c in calls if c[0] == "post" and not c[1].endswith("/claim")]
    fresh = launches[0][2]
    claim_payload = fresh["json"] if path == "default" else fresh["params"]
    assert claim_payload["claim_id"]
    assert fresh["json"]["description"] == description
    if path in ["assign", "nowait"]:
        assert database.get_terminal_metadata(CHILD)["description"] == description
        assert database.get_ephemeral_agent(name)["state"] == "launched"
        assert orchestration._delete_terminal_impl(CHILD)["success"]
    assert snapshots and all(row["description"] == description for row in snapshots)
    assert_released(env, name, "terminal_deleted")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["assign", "handoff"])
async def test_mcp_ephemeral_description_reaches_child(launch_http, monkeypatch, tool):
    runtime, _, _, _, _ = launch_http
    snapshots = []
    delete = terminal_service.delete_terminal

    def observe_delete(terminal_id, **kwargs):
        snapshots.append(database.get_terminal_metadata(terminal_id))
        return delete(terminal_id, **kwargs)

    monkeypatch.setattr(terminal_service, "delete_terminal", observe_delete)
    kwargs = dict(
        engine=None, model=None, use_worktree=False, target_host=None, description="Review worker"
    )
    if tool == "handoff":
        kwargs["timeout"] = 600
    if server.ENABLE_WORKING_DIRECTORY:
        kwargs["working_directory"] = "/workspace"
    result = await getattr(server, tool)(runtime[1], "inspect", **kwargs)
    data = result if isinstance(result, dict) else result.model_dump()
    assert data["success"], data
    if tool == "assign":
        assert database.get_terminal_metadata(CHILD)["description"] == "Review worker"
        assert orchestration._delete_terminal_impl(CHILD)["success"]
    assert snapshots[0]["description"] == "Review worker"


@pytest.mark.asyncio
async def test_claimed_provider_error_preserves_handoff_and_releases(launch_http, monkeypatch):
    runtime, _, calls, _, _ = launch_http
    env, name, _, _ = runtime
    error = "API Error: 400 model is unavailable"
    monkeypatch.setattr(terminal_service, "get_output", lambda *a: error)
    monkeypatch.setattr(terminal_service, "get_output_context", lambda *a: "⏺ " + error)
    post = orchestration.requests.post
    responses = []

    def observe_post(url, **kwargs):
        response = post(url, **kwargs)
        if url.endswith("/run-step"):
            responses.append(response)
        return response

    monkeypatch.setattr(orchestration.requests, "post", observe_post)
    result = await invoke("default", name)
    response = responses[-1]
    assert response.status_code == 502
    assert response.json()["detail"]["kind"] == "provider_error"
    assert response.json()["detail"]["terminal_id"] == CHILD
    assert not result["success"] and result["rule"] is None
    assert result["terminal_id"] == CHILD
    assert result["message"].startswith("Handoff failed: provider error (api_error)")
    assert "ephemeral launch failed" not in result["message"]
    # The no-claim response mapper must retain exactly the same established result.
    monkeypatch.setattr(orchestration.requests, "post", lambda *a, **kw: response)
    unclaimed = await orchestration._run_step_and_build_result({}, name, "claude_code", 600, 0)
    assert (unclaimed.message, unclaimed.terminal_id, unclaimed.rule) == (
        result["message"],
        CHILD,
        None,
    )
    assert database.get_terminal_metadata(CHILD) is None
    assert_released(env, name, "terminal_deleted")
    assert database.get_ephemeral_agent(name)["state"] == "gc"
