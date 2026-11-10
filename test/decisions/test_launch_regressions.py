"""Launch regressions and observable isolation around HTTP delegation."""

import asyncio
from dataclasses import replace
from test.decisions.test_engine import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    setup_engine,
)
from test.decisions.test_launch_binding import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    terminal_path,
)
from test.decisions.test_wiring import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    handler_path,
)
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("state", ["off", "on"])
def test_delegation_keeps_sidecar_provider(
    handler_path, state, monkeypatch, hermetic_kiro_agent_store
):
    from cli_agent_orchestrator.constants import DEFAULT_PROVIDER
    from cli_agent_orchestrator.decisions.settings import PointSettings

    api, client, engine, decider, store, events, calls = handler_path
    # The sidecar launches on the default provider, kiro_cli, and is refused up
    # front unless memory_manager is installed for Kiro (#1074).
    (hermetic_kiro_agent_store / "memory_manager.json").write_text("{}")
    settings = engine.settings_loader()
    if state == "off":
        engine.settings_loader = lambda: replace(
            settings, points={p: PointSettings() for p in settings.points}
        )
    monkeypatch.setattr(api, "resolve_provider", Mock(return_value="codex"))
    response = client.post(
        "/sessions",
        params={"agent_profile": "developer", "memory_manager": "true"},
        json={
            "initial_message": "delivery",
            "initial_message_orchestration_type": "assign",
            "delegation_message": "task",
        },
    )
    assert response.status_code == 201, response.text
    assert calls[0]["provider"] == "codex"
    assert calls[1]["provider"] == DEFAULT_PROVIDER
    if state == "on":
        assert store.list()[0]["provider"] == "codex" and calls[0]["launch_plan"].model == "model-x"
    else:
        assert store.list() == [] and decider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["initialize", "not_started", "cancelled", "input_blocked"])
async def test_deferred_start_through_creation_binds_once(terminal_path, failure, monkeypatch):
    from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    monkeypatch.setattr(service, "_notify_caller_of_deferred_failure", Mock())
    monkeypatch.setattr(service, "send_input", Mock(return_value=True))
    monkeypatch.setattr(service, "get_terminal_metadata", Mock(return_value={}))
    # Deferred delivery dispatches directly; it returns the dispatch generation.
    monkeypatch.setattr(service, "dispatch_input", Mock(return_value=1))
    if failure == "initialize":
        provider.initialize.side_effect = RuntimeError("startup failed")
    elif failure == "not_started":
        monkeypatch.setattr(
            service, "_confirm_worker_started_or_resubmit", AsyncMock(return_value=False)
        )
    elif failure == "input_blocked":
        monkeypatch.setattr(
            service, "send_input", Mock(side_effect=TerminalInputBlockedError("blocked"))
        )
    else:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def initialize():
            entered.set()
            await release.wait()

        provider.initialize.side_effect = initialize
    before = set(service._deferred_init_tasks)
    terminal = await service.create_terminal(
        "codex",
        "developer",
        new_session=True,
        defer_init=True,
        initial_message="task",
        launch_plan=plan,
    )
    (task,) = set(service._deferred_init_tasks) - before
    assert store.list()[0]["launch_status"] == "launched" and len(events) == 1
    if failure == "cancelled":
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    row = store.list()[0]
    assert row["launch_status"] == ("launched" if failure == "input_blocked" else "launch_failed")
    assert row["terminal_id"] == terminal.id and row["launched_model"] == terminal.model
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["explicit", "empty_plan", "no_plan"])
async def test_probe_refusal_never_replaces_explicit_or_fallback(terminal_path, variant):
    from cli_agent_orchestrator.models.kiro_engine import KiroEngine
    from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilityError

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req) if variant == "empty_plan" else None
    if plan is not None:
        plan = replace(plan, model=None)
    error = KiroCapabilityError("missing_flag", KiroEngine.V2, "unsupported current model")
    probe = Mock(side_effect=error)
    with pytest.raises(KiroCapabilityError) as raised:
        await service.create_terminal(
            "kiro_cli",
            "developer",
            new_session=True,
            model="explicit" if variant == "explicit" else None,
            launch_plan=plan,
            kiro_capability_probe=probe,
        )
    assert raised.value is error and probe.call_count == 1
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_both_probe_failures_leave_decision_unchanged(terminal_path):
    from cli_agent_orchestrator.models.kiro_engine import KiroEngine
    from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilityError

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    original = store.list()[0]
    error = KiroCapabilityError("missing_flag", KiroEngine.V2, "unsupported fallback model")
    probe = Mock(side_effect=error)
    with pytest.raises(KiroCapabilityError) as raised:
        await service.create_terminal(
            "kiro_cli", "developer", new_session=True, launch_plan=plan, kiro_capability_probe=probe
        )
    assert raised.value is error and probe.call_count == 2
    row = store.list()[0]
    assert row["launch_status"] == "launch_failed"
    assert (row["outcome"], row["reason"]) == (original["outcome"], original["reason"])
    assert row["applied_value"] == original["applied_value"] and len(events) == 1
    providers.create_provider.assert_not_called()


def test_default_runtime_guard_detects_swallowed_store_access(tmp_path):
    from test.fixtures.decision_runtime import isolated_runtime

    runtime, guard = isolated_runtime(tmp_path)
    guard.check()
    assert runtime.store.insert({"point": "model.route", "state": "on"}) is None
    assert guard.accesses == 1
    with pytest.raises(AssertionError, match="default-off test runtime accessed decision records"):
        guard.check()


@pytest.mark.parametrize(
    "model_name,existing",
    [
        (
            "CreateTerminalBody",
            ["initial_message", "initial_message_orchestration_type", "description"],
        ),
        (
            "CreateSessionBody",
            [
                "initial_message",
                "initial_message_orchestration_type",
                "description",
                "env_vars",
                "group",
                "metadata",
                "caller_ceiling",
            ],
        ),
        (
            "RunStepRequest",
            [
                "description",
                "provider",
                "agent",
                "prompt",
                "session_name",
                "reuse_terminal_id",
                "teardown",
                "timeout",
                "working_directory",
                "caller_id",
                "allowed_tools",
                "caller_ceiling",
                "use_worktree",
                "env_vars",
                "model",
                "recovery",
                "engine",
                "job_id",
                "claim_id",
            ],
        ),
    ],
)
def test_existing_request_fields_stay_a_prefix_and_new_fields_are_optional(model_name, existing):
    """Any field added after this snapshot must be backward compatible: it
    comes after every pre-existing field (so positional/ordered callers are
    unaffected) and is optional (so no existing caller is newly required to
    supply it). This is what the old pin -- an exact, closed list of the two
    decision fields appended for the decision platform -- was really
    protecting; checking the property directly means an unrelated new
    optional field doesn't have to be added to this test's literal list to
    keep it green.
    """
    from cli_agent_orchestrator.api import main as api

    model = getattr(api, model_name)
    fields = list(model.model_fields)
    assert fields[: len(existing)] == existing
    for name in fields[len(existing) :]:
        field = model.model_fields[name]
        assert not field.is_required(), f"{model_name}.{name} must be optional"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["assign", "remote_assign", "handoff"])
async def test_double_probe_refusal_is_handler_400(entrypoint, tmp_path, monkeypatch):
    from test.fixtures.decision_conformance import ConfidentDecider
    from test.fixtures.delegation_conformance import DelegationAdapter

    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.provider = "kiro_cli"
    adapter.reprobe = True
    adapter.probe_refuses_fallback = True
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": ConfidentDecider("small", 0.91)},
        model_tiers={"kiro_cli": {"small": "model-x"}},
    )
    observation = await adapter.delegate(message="task")
    assert not observation.launched and adapter.probe.call_count == 2
    response = adapter.responses[-1]
    assert response.status_code == 400 and response.json()["detail"] == "unsupported fallback model"
    row = adapter.store.list()[0]
    assert row["launch_status"] == "launch_failed"
    assert row["outcome"] == "applied" and row["reason"] is None
    assert row["applied_value"] == "small" and len(adapter.spans) == 1
    await adapter.engine.runner.close()
