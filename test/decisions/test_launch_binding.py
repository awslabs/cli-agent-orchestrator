"""Terminal-layer plan precedence, capability fallback and deferred binding."""

import asyncio
from test.decisions.test_engine import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    setup_engine,
)
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def terminal_path(setup_engine, monkeypatch, tmp_path):
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.services import terminal_service as service

    engine, req, decider, store, events, settings = setup_engine
    monkeypatch.setattr(app.state, "decision_engine", engine, raising=False)
    from test.fixtures.terminal_launch import install_launch_stubs

    provider, backend, providers = install_launch_stubs(monkeypatch, tmp_path)
    return service, engine, req, provider, backend, providers, store, events


@pytest.mark.asyncio
async def test_terminal_plan_model_is_separate_and_bound(terminal_path):
    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    terminal = await service.create_terminal(
        "codex", "developer", new_session=True, launch_plan=plan
    )
    assert terminal.model == "model-x" and terminal.model_honored is True
    assert providers.create_provider.call_args.kwargs["model"] == "model-x"
    assert store.list()[0]["launched_model"] == terminal.model
    assert store.list()[0]["launch_status"] == "launched" and len(events) == 1
    assert service.resolve_launch_model("explicit", None, plan) == "explicit"


@pytest.mark.asyncio
async def test_provider_start_failure_binds_launch_failed(terminal_path):
    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    provider.initialize.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        await service.create_terminal("codex", "developer", new_session=True, launch_plan=plan)
    assert store.list()[0]["launch_status"] == "launch_failed"
    engine.bind_launch(plan, launch_status="not_launched")
    assert store.list()[0]["launch_status"] == "launch_failed" and len(events) == 1


@pytest.mark.asyncio
async def test_ceiling_refusal_binds_not_launched(terminal_path):
    """A #948 refusal is decided but never launched, so it is not a launch failure."""
    from cli_agent_orchestrator.constants import CALLBACK_TERMINAL_ID_ENV
    from cli_agent_orchestrator.utils.delegation import DelegationCeilingError

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    # A remote-placement marker without caller_ceiling is refused in Step 0c, before any lookup.
    with pytest.raises(DelegationCeilingError):
        await service.create_terminal(
            "codex",
            "developer",
            new_session=True,
            launch_plan=plan,
            env_vars={CALLBACK_TERMINAL_ID_ENV: "0000feed"},
        )
    row = store.list()[0]
    assert row["launch_status"] == "not_launched" and row["terminal_id"] is None
    providers.create_provider.assert_not_called()
    engine.bind_launch(plan, launch_status="launch_failed")  # a later bind cannot rewrite it
    assert store.list()[0]["launch_status"] == "not_launched" and len(events) == 1


@pytest.mark.asyncio
async def test_deferred_failure_updates_bound_record_once(terminal_path, monkeypatch):
    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, terminal_id="worker-fixed", model="model-x", honored=True)
    provider.initialize.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr(service, "_notify_caller_of_deferred_failure", Mock())
    before = set(service._deferred_init_tasks)
    service._schedule_deferred_init(provider, "worker-fixed", None, None, None, launch_plan=plan)
    await asyncio.gather(*(set(service._deferred_init_tasks) - before))
    assert store.list()[0]["launch_status"] == "launch_failed" and len(events) == 1
    assert store.list()[0]["terminal_id"] == "worker-fixed"


@pytest.mark.asyncio
async def test_decided_capability_failure_reprobes_today_model(terminal_path):
    service, engine, req, provider, backend, providers, store, events = terminal_path
    from cli_agent_orchestrator.models.kiro_engine import KiroEngine
    from cli_agent_orchestrator.providers.kiro_capabilities import (
        KiroCapabilities,
        KiroCapabilityError,
    )

    plan = await engine.prepare_launch(req)
    probe = Mock(
        side_effect=[
            KiroCapabilityError("missing_flag", KiroEngine.V2, "not supported"),
            KiroCapabilities(version="2.0", flags=frozenset()),
        ]
    )
    terminal = await service.create_terminal(
        "kiro_cli", "developer", new_session=True, launch_plan=plan, kiro_capability_probe=probe
    )
    assert probe.call_count == 2 and terminal.model == "model-y"
    row = store.list()[0]
    assert row["outcome"] == "fallback" and row["reason"] == "not_honored"
    assert row["launched_model"] == "model-y" and row["applied_value"] is None
    assert engine.launch_note(plan) == "ran on model-y (auto: fallback, not_honored)"


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_fingerprint_or_note_plan(terminal_path, monkeypatch):
    from test.services.test_terminal_service_full import _record, _valid_row

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    monkeypatch.setattr(
        service, "get_idempotency_record", Mock(return_value=_record(provider="codex"))
    )
    monkeypatch.setattr(service, "get_terminal", Mock(return_value=_valid_row()))
    result = await service.create_terminal(
        "codex", "developer", idempotency_key="fixed-request", launch_plan=plan
    )
    assert result.id == "prior-terminal"
    assert store.list()[0]["launch_status"] == "not_launched"
    assert store.list()[0]["terminal_id"] is None and engine.launch_note(plan) is None
    providers.create_provider.assert_not_called()
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strict, status, delete_on_failure",
    [
        (True, "error", True),
        (True, "error", False),
        (True, "completed", False),
        (False, "error", True),
    ],
)
async def test_deferred_provider_error_preserves_surface_and_binds_once(
    terminal_path, monkeypatch, strict, status, delete_on_failure
):
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, terminal_id="worker-fixed", model="model-x", honored=True)
    provider.requires_execution_evidence = strict
    monkeypatch.setattr(service, "get_terminal_metadata", Mock(return_value={}))
    monkeypatch.setattr(service, "send_input", Mock(return_value=True))
    # Deferred delivery dispatches directly; it returns the dispatch generation.
    monkeypatch.setattr(service, "dispatch_input", Mock(return_value=1))
    service.status_monitor.get_status.return_value = TerminalStatus(status)
    surface = AsyncMock()
    clear_owner = AsyncMock()
    monkeypatch.setattr(service, "_surface_deferred_init_failure", surface)
    monkeypatch.setattr(service, "_clear_deferred_init_external_owner", clear_owner)
    assert store.list()[0]["launch_status"] == "launched" and len(events) == 1

    task = service._schedule_deferred_init(
        provider,
        "worker-fixed",
        "task",
        None,
        None,
        delete_on_failure=delete_on_failure,
        launch_plan=plan,
    )
    assert task is not None
    await task
    failed = strict and status == "error"
    row = store.list()[0]
    assert row["launch_status"] == ("launch_failed" if failed else "launched")
    assert row["terminal_id"] == "worker-fixed" and len(events) == 1
    if failed:
        surface.assert_awaited_once_with(
            "worker-fixed",
            kind="provider_error_after_delivery",
            message=(
                "Worker worker-fixed accepted the assigned task but the "
                "provider entered ERROR before producing a result. Correct "
                "the provider/model configuration and re-assign the task."
            ),
            registry=None,
            delete_on_failure=delete_on_failure,
        )
        clear_owner.assert_not_awaited()
    else:
        surface.assert_not_awaited()
        clear_owner.assert_awaited_once_with("worker-fixed")
