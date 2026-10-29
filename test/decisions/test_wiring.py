"""Delegation request boundaries, handler placement and launch evidence."""

import asyncio
from dataclasses import replace
from datetime import datetime
from test.decisions.test_engine import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    setup_engine,
)
from unittest.mock import AsyncMock

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError


@pytest.fixture
def handler_path(setup_engine, monkeypatch):
    from cli_agent_orchestrator.api import main as api
    from cli_agent_orchestrator.models.terminal import AgentStepResult, Terminal, TerminalStatus
    from cli_agent_orchestrator.plugins import PluginRegistry

    engine, req, decider, store, events, settings = setup_engine
    monkeypatch.setattr(api.app.state, "decision_engine", engine, raising=False)
    monkeypatch.setattr(api.app.state, "plugin_registry", PluginRegistry(), raising=False)
    monkeypatch.setattr(api, "_record_job_state", AsyncMock())
    from cli_agent_orchestrator.utils import agent_profiles

    monkeypatch.setattr(agent_profiles, "resolve_provider", lambda *a, **k: "codex")
    calls = []

    async def launch(**kwargs):
        calls.append(kwargs)
        plan = kwargs.get("launch_plan")
        model = kwargs.get("model") or (plan.model if plan else None) or "model-y"
        engine.bind_launch(plan, terminal_id="worker-fixed", model=model, honored=True)
        return Terminal(
            id="worker-fixed",
            name="worker",
            provider="codex",
            session_name="cao-fixture",
            agent_profile="developer",
            model=model,
            model_honored=True,
            status=TerminalStatus.IDLE,
            last_active=datetime(2026, 1, 1),
        )

    async def step(**kwargs):
        terminal = await launch(**kwargs)
        return AgentStepResult(
            terminal_id=terminal.id, last_message="worker result", status=TerminalStatus.COMPLETED
        )

    monkeypatch.setattr(api.session_service, "create_session", AsyncMock(side_effect=launch))
    monkeypatch.setattr(api.terminal_service, "create_terminal", AsyncMock(side_effect=launch))
    monkeypatch.setattr(api, "run_agent_step", AsyncMock(side_effect=step))
    client = TestClient(api.app, headers={"Host": "localhost"})
    return api, client, engine, decider, store, events, calls


def post_delegation(client, path, *, model=None, fields=None):
    if path == "step":
        return client.post(
            "/terminals/run-step",
            json={
                "provider": "codex",
                "agent": "developer",
                "prompt": "delivered task",
                "orchestration_type": "handoff",
                "delegation_message": "unshaped task",
                **({"model": model} if model else {}),
                **(fields or {}),
            },
        )
    return client.post(
        "/sessions" if path == "session" else "/sessions/cao-fixture/terminals",
        params={
            "agent_profile": "developer",
            "provider": "codex",
            "defer_init": "true",
            **({"caller_id": "a1b2c3d4"} if path == "terminal" else {}),
            **({"model": model} if model else {}),
        },
        json={
            "initial_message": "delivered task",
            "initial_message_orchestration_type": "assign",
            "delegation_message": "unshaped task",
            **(fields or {}),
        },
    )


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
def test_handlers_route_and_bind_before_note(handler_path, path, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    from cli_agent_orchestrator.decisions.policy import IDENTITY_POLICY
    from cli_agent_orchestrator.decisions.targets import TargetKind

    seen = []

    async def prepare(req):
        assert req.policy is IDENTITY_POLICY and req.target_kind == TargetKind.INSTALLED
        assert calls == []
        seen.append(req)
        return await engine.prepare_launch(req)

    monkeypatch.setattr(api, "prepare_launch", prepare, raising=False)
    response = post_delegation(client, path)
    assert response.status_code in (200, 201), response.text
    assert len(seen) == 1 and seen[0].message == "unshaped task"
    assert len(calls) == 1 and calls[0]["launch_plan"].model == "model-x"
    assert store.list()[0]["launch_status"] == "launched" and len(events) == 1
    assert response.headers["X-CAO-Decision"] == "ran on model-x (auto: small 0.91)"
    assert "reason" not in response.json() and "record_ids" not in response.json()
    if path != "step":
        from cli_agent_orchestrator.models.terminal import Terminal

        assert set(response.json()) == set(Terminal.model_fields)


@pytest.mark.parametrize(
    "body",
    [
        {"delegation_kind": "handoff"},
        {"delegation_kind": "handoff", "delegation_message": "task", "initial_message": "task"},
        {"delegation_message": "task"},
        {"delegation_message": ""},
        {"delegation_kind": "assign", "delegation_message": "task"},
    ],
)
def test_create_delegation_fields_reject_invalid_pairs(body):
    from cli_agent_orchestrator.api.main import CreateSessionBody, CreateTerminalBody

    for cls in (CreateSessionBody, CreateTerminalBody):
        with pytest.raises(ValidationError):
            cls(**body)


@pytest.mark.parametrize(
    "fields",
    [
        {"orchestration_type": "handoff"},
        {"delegation_message": "task"},
        {"orchestration_type": "assign", "delegation_message": "task"},
        {"orchestration_type": "handoff", "delegation_message": ""},
        {
            "orchestration_type": "handoff",
            "delegation_message": "task",
            "reuse_terminal_id": "worker-fixed",
        },
    ],
)
def test_step_delegation_fields_reject_invalid_pairs(fields):
    from cli_agent_orchestrator.api.main import RunStepRequest

    with pytest.raises(ValidationError):
        RunStepRequest(provider="codex", agent="developer", prompt="task", **fields)


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
def test_handler_failure_binds_not_launched(handler_path, path):
    api, client, engine, decider, store, events, calls = handler_path
    target = (
        api.run_agent_step
        if path == "step"
        else (
            api.session_service.create_session
            if path == "session"
            else api.terminal_service.create_terminal
        )
    )
    target.side_effect = RuntimeError("service failed")
    response = post_delegation(client, path)
    assert response.status_code == 500
    assert len(store.list()) == 1 and store.list()[0]["launch_status"] == "not_launched"
    assert store.list()[0]["terminal_id"] is None and len(events) == 1


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
def test_header_failure_cannot_fail_launch(handler_path, path, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    monkeypatch.setattr(
        engine, "launch_note", lambda plan: (_ for _ in ()).throw(ValueError("header"))
    )
    response = post_delegation(client, path)
    assert response.status_code in (200, 201)
    assert "X-CAO-Decision" not in response.headers
    assert len(store.list()) == 1 and store.list()[0]["launch_status"] == "launched"


@pytest.mark.parametrize(
    "model,encoded",
    [
        ("with space", "with%20space"),
        ("modèle", "mod%C3%A8le"),
        ("model\r\nvalue", "model%0D%0Avalue"),
    ],
)
def test_note_encodes_model_characters(model, encoded):
    from cli_agent_orchestrator.decisions.engine import render_note

    assert (
        render_note(
            {
                "point": "model.route",
                "state": "on",
                "launch_status": "launched",
                "model_honored": True,
                "launched_model": model,
                "outcome": "fallback",
                "reason": "unsure",
            }
        )
        == f"ran on {encoded} (auto: fallback, unsure)"
    )


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
@pytest.mark.parametrize("state", ["shadow", "on"])
def test_policy_default_error_is_in_process_http_400(handler_path, path, state, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.types import PointState
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    engine.settings_loader = lambda: DecisionSettings(
        points={"model.route": PointSettings(PointState(state), decider.name)}, model_tiers={}
    )
    engine.profile_loader = lambda name: AgentProfile(name=name, description="worker")

    async def prepare(req):
        return await engine.prepare_launch(
            replace(req, policy=PolicyBounds(default_tier="small", max_tier="medium"))
        )

    monkeypatch.setattr(api, "prepare_launch", prepare)
    response = post_delegation(client, path)
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "model_tiers.codex.small is not mapped (policy default tier for model.route)"
    )
    assert decider.calls == 0 and calls == [] and "X-CAO-Decision" not in response.headers
    assert store.list()[0]["reason"] == "default_unmapped"
    assert store.list()[0]["launch_status"] == "not_launched"


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
def test_ephemeral_override_is_refused_before_creation(handler_path, path, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    from cli_agent_orchestrator.decisions.targets import TargetKind

    monkeypatch.setattr(api, "profile_source", lambda name: TargetKind.EPHEMERAL)
    response = post_delegation(client, path, model="explicit")
    assert response.status_code == 400 and "set the tier in the spec" in response.json()["detail"]
    assert decider.calls == 0 and calls == [] and store.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["session", "terminal", "step"])
async def test_cancellation_binds_pending_plan(handler_path, path):
    from fastapi import BackgroundTasks

    api, client, engine, decider, store, events, calls = handler_path
    request = Request({"type": "http", "app": api.app})
    target = (
        api.run_agent_step
        if path == "step"
        else (
            api.session_service.create_session
            if path == "session"
            else api.terminal_service.create_terminal
        )
    )
    target.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        if path == "step":
            await api.run_step(
                request=request,
                response=Response(),
                background_tasks=BackgroundTasks(),
                body=api.RunStepRequest(
                    provider="codex",
                    agent="developer",
                    prompt="task",
                    orchestration_type="handoff",
                    delegation_message="task",
                ),
            )
        else:
            body = (api.CreateSessionBody if path == "session" else api.CreateTerminalBody)(
                initial_message="task",
                initial_message_orchestration_type="assign",
                delegation_message="task",
            )
            if path == "session":
                await api.create_session(
                    request=request,
                    response=Response(),
                    background_tasks=BackgroundTasks(),
                    agent_profile="developer",
                    provider="codex",
                    body=body,
                )
            else:
                await api.create_terminal_in_session(
                    request=request,
                    response=Response(),
                    session_name="cao-fixture",
                    agent_profile="developer",
                    provider="codex",
                    caller_id="a1b2c3d4",
                    defer_init=True,
                    claim_id=None,
                    body=body,
                )
    assert len(store.list()) == 1 and store.list()[0]["launch_status"] == "not_launched"
    assert store.list()[0]["terminal_id"] is None and len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["terminal", "step"])
@pytest.mark.parametrize(
    "agent,model",
    [
        ("Worker-fixture_task-0a1b", None),
        ("Worker-fixture_task-0a1b", "model-x"),
        ("developer", None),
    ],
)
async def test_claimed_launch_skips_the_decision_site(
    handler_path, path, agent, model, monkeypatch
):
    from fastapi import BackgroundTasks

    from cli_agent_orchestrator.clients import database

    api, client, engine, decider, store, events, calls = handler_path
    # No stored spec declares auto for this name (#1068), so the opt-in finds nothing to ask.
    monkeypatch.setattr(database, "get_ephemeral_agent", lambda name: None)
    request = Request({"type": "http", "app": api.app})
    claim = "0" * 32
    target = api.run_agent_step if path == "step" else api.terminal_service.create_terminal
    target.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        if path == "step":
            await api.run_step(
                request=request,
                response=Response(),
                background_tasks=BackgroundTasks(),
                body=api.RunStepRequest(
                    provider="codex",
                    agent=agent,
                    model=model,
                    prompt="task",
                    orchestration_type="handoff",
                    delegation_message="task",
                    claim_id=claim,
                ),
            )
        else:
            await api.create_terminal_in_session(
                request=request,
                response=Response(),
                session_name="cao-fixture",
                agent_profile=agent,
                provider="codex",
                caller_id="a1b2c3d4",
                model=model,
                defer_init=True,
                claim_id=claim,
                body=api.CreateTerminalBody(
                    initial_message="task",
                    initial_message_orchestration_type="assign",
                    delegation_message="task",
                ),
            )
    # The launch is reached; a per-call model is left for the ephemeral policy to refuse.
    assert target.call_args.kwargs["claim_id"] == claim
    assert target.call_args.kwargs["model"] == model
    if agent == "developer":
        # A claim on an installed profile claims nothing, so the launch is routed as usual.
        assert decider.calls == 1 and "launch_plan" in target.call_args.kwargs
        return
    # A claimed ephemeral launch without a declared auto: no decider call, no record, no plan.
    assert decider.calls == 0 and store.list() == [] and events == []
    assert "launch_plan" not in target.call_args.kwargs


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
def test_unclaimed_ephemeral_target_asks_nothing_and_writes_nothing(
    handler_path, path, monkeypatch
):
    from cli_agent_orchestrator.decisions.targets import TargetKind

    api, client, engine, decider, store, events, calls = handler_path
    monkeypatch.setattr(api, "profile_source", lambda name: TargetKind.EPHEMERAL)
    response = post_delegation(client, path)
    assert response.status_code in (200, 201), response.text
    # The engine asks nothing and records nothing for an ephemeral target, with or without a claim.
    assert decider.calls == 0 and store.list() == [] and events == []
    assert len(calls) == 1
    plan = calls[0].get("launch_plan")
    assert plan is None or (plan.record_ids == () and plan.model is None)
    assert "X-CAO-Decision" not in response.headers


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
@pytest.mark.parametrize("mode", ["old_client", "off", "explicit", "excluded"])
def test_unrouted_calls_keep_no_rows_or_header(handler_path, path, mode, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    from cli_agent_orchestrator.decisions.settings import PointSettings

    settings = engine.settings_loader()
    if mode == "off":
        engine.settings_loader = lambda: replace(
            settings, points={p: PointSettings() for p in settings.points}
        )
    elif mode == "excluded":
        engine.settings_loader = lambda: replace(
            settings,
            points={
                p: replace(v, exclude_profiles=("developer",)) for p, v in settings.points.items()
            },
        )
    fields = {"delegation_message": None}
    if path == "step":
        fields["orchestration_type"] = None
    response = post_delegation(
        client,
        path,
        model="explicit" if mode == "explicit" else None,
        fields=fields if mode == "old_client" else None,
    )
    assert response.status_code in (200, 201), response.text
    assert calls and "launch_plan" not in calls[0]
    assert decider.calls == 0 and store.list() == [] and events == []
    assert "X-CAO-Decision" not in response.headers


@pytest.mark.parametrize("orchestration", [None, "handoff"])
def test_live_script_launch_is_out_of_scope(handler_path, orchestration, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    from cli_agent_orchestrator.services import script_runner, workflow_service

    monkeypatch.setattr(workflow_service, "check_generation", lambda *args: None)
    monkeypatch.setattr(
        script_runner, "run_registry", {"live": object.__new__(script_runner.ScriptRunRecord)}
    )
    for name in ("make_step_terminal_recorder", "record_step_completion", "record_step_replay"):
        monkeypatch.setattr(script_runner, name, lambda env: None)
    response = client.post(
        "/terminals/run-step",
        json={
            "provider": "codex",
            "agent": "developer",
            "prompt": "task",
            "env_vars": {
                "CAO_WORKFLOW_RUN_ID": "live",
                "CAO_WORKFLOW_STEP_ID": "step",
                "CAO_WORKFLOW_GENERATION": "1",
            },
            **(
                {"orchestration_type": orchestration, "delegation_message": "task"}
                if orchestration
                else {}
            ),
        },
    )
    assert response.status_code == 200, response.text
    assert decider.calls == 0 and store.list()[0]["reason"] == "out_of_scope"
    assert store.list()[0]["decider"] is None and "X-CAO-Decision" not in response.headers


@pytest.mark.parametrize("path", ["session", "terminal", "step"])
def test_server_workflow_environment_does_not_own_child_delegation(handler_path, path, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    monkeypatch.setenv("CAO_WORKFLOW_RUN_ID", "live")
    monkeypatch.setenv("CAO_WORKFLOW_STEP_ID", "step")
    response = post_delegation(client, path)
    assert response.status_code in (200, 201) and decider.calls == 1
    assert store.list()[0]["kind"] == ("handoff" if path == "step" else "assign")


def test_session_resolves_provider_once_for_decision_and_service(handler_path, monkeypatch):
    api, client, engine, decider, store, events, calls = handler_path
    from unittest.mock import Mock

    resolve = Mock(return_value="codex")
    monkeypatch.setattr(api, "resolve_provider", resolve)
    response = client.post(
        "/sessions",
        params={"agent_profile": "developer"},
        json={
            "initial_message": "delivery",
            "initial_message_orchestration_type": "assign",
            "delegation_message": "task",
        },
    )
    assert response.status_code == 201, response.text
    resolve.assert_called_once_with("developer", fallback_provider="kiro_cli")
    assert calls[0]["provider"] == store.list()[0]["provider"] == "codex"
    assert calls[0]["launch_plan"].model == "model-x"


@pytest.mark.parametrize("gate", ["fenced", "replayed", "reuse"])
def test_nonlaunch_step_paths_never_call_seam(handler_path, gate, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cli_agent_orchestrator.services import (
        agent_step,
        script_runner,
        step_replay,
        workflow_service,
    )

    api, client, engine, decider, store, events, calls = handler_path
    prepare = AsyncMock(side_effect=AssertionError("decision seam on nonlaunch path"))
    monkeypatch.setattr(api, "prepare_launch", prepare)
    payload = {"provider": "codex", "agent": "developer", "prompt": "task"}
    if gate == "reuse":
        payload["reuse_terminal_id"] = "worker-fixed"
    else:
        payload["env_vars"] = {
            "CAO_WORKFLOW_RUN_ID": "live",
            "CAO_WORKFLOW_STEP_ID": "step",
            "CAO_WORKFLOW_GENERATION": "1",
        }
        monkeypatch.setattr(script_runner, "make_step_terminal_recorder", lambda env: Mock())
        monkeypatch.setattr(script_runner, "record_step_completion", lambda env: None)
        monkeypatch.setattr(script_runner, "record_step_replay", lambda env: None)
        if gate == "fenced":
            monkeypatch.setattr(
                workflow_service,
                "check_generation",
                Mock(side_effect=workflow_service.StaleGenerationError("stale")),
            )
        else:
            monkeypatch.setattr(workflow_service, "check_generation", lambda *args: None)
            monkeypatch.setattr(
                agent_step,
                "resolve_effective_working_directory",
                AsyncMock(return_value="/workspace"),
            )
            envelope = SimpleNamespace(
                terminal_id="prior-worker", last_message="cached output", status="completed"
            )
            monkeypatch.setattr(
                step_replay,
                "decide",
                lambda *args: SimpleNamespace(
                    verdict=step_replay.ReplayVerdict.REPLAY, envelope=envelope
                ),
            )
    response = client.post("/terminals/run-step", json=payload)
    assert response.status_code == (409 if gate == "fenced" else 200), response.text
    prepare.assert_not_awaited()
    assert decider.calls == 0 and store.list() == [] and events == []
    if gate != "reuse":
        assert calls == []
