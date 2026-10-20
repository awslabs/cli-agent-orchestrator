import dataclasses
from dataclasses import replace
from test.decisions.test_engine import setup_engine
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    "env",
    [
        None,
        {},
        {"CAO_WORKFLOW_STEP_ID": "step"},
        {"CAO_WORKFLOW_RUN_ID": "live"},
        {"CAO_WORKFLOW_RUN_ID": "unknown", "CAO_WORKFLOW_STEP_ID": "step"},
        {"CAO_WORKFLOW_RUN_ID": "other", "CAO_WORKFLOW_STEP_ID": "step"},
        {"CAO_WORKFLOW_RUN_ID": "live", "CAO_WORKFLOW_STEP_ID": "step"},
    ],
)
def test_script_step_detector_and_recorders_agree(env, monkeypatch, tmp_path):
    from cli_agent_orchestrator.decisions.owner import launch_owner
    from cli_agent_orchestrator.services import script_runner

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    monkeypatch.setattr(
        script_runner,
        "run_registry",
        {"live": object.__new__(script_runner.ScriptRunRecord), "other": object()},
    )
    expected = (
        env is not None
        and env.get("CAO_WORKFLOW_RUN_ID") == "live"
        and env.get("CAO_WORKFLOW_STEP_ID") == "step"
    )
    assert script_runner.script_step_of(env) == (("live", "step") if expected else None)
    owner = launch_owner("run_step", {"env_vars": env}) == "workflow_step"
    for recorder in (
        script_runner.make_step_terminal_recorder,
        script_runner.record_step_replay,
        script_runner.record_step_completion,
    ):
        assert (recorder(env) is not None) == owner == bool(expected)
    assert launch_owner("create_session", {"env_vars": env}) == "delegation"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["ephemeral", "installed"])
async def test_purpose_is_redacted_and_never_retained(setup_engine, target):
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import DecisionRequest

    engine, req, decider, store, events, settings = setup_engine
    secret = "ghp_" + "z" * 36
    purpose = "purpose-sentinel " + secret
    plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind(target),
            field_states={"model.route": "auto"},
            purpose=purpose,
        )
    )
    seen = decider.requests[0]
    if target == "ephemeral":
        assert "purpose-sentinel" in seen.purpose and secret not in seen.purpose
        assert "[REDACTED:" in seen.purpose
    else:
        assert seen.purpose is None
    assert not hasattr(seen.facts, "purpose")
    fields = [field.name for field in dataclasses.fields(DecisionRequest)]
    assert fields.index("purpose") == fields.index("profile_description") + 1
    engine.bind_launch(plan, terminal_id="worker", model=plan.model, honored=True)
    assert "purpose-sentinel" not in str(store.list()) + str(events) + str(engine.launch_note(plan))
    assert all("purpose" not in record for record in store.list())
    first = store.get(plan.record_ids[0])
    second_plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind(target),
            field_states={"model.route": "auto"},
            purpose="another purpose",
        )
    )
    second = store.get(second_plan.record_ids[0])
    assert (
        first["message_hash"] == second["message_hash"]
        and first["message_bytes"] == second["message_bytes"]
    )


@pytest.mark.asyncio
async def test_fixed_table_receives_redacted_purpose_and_ignores_it(setup_engine):
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine
    table = FixedTableDecider()
    observed = []
    original = table.decide

    async def capture(request, config):
        observed.append(request)
        return await original(request, config)

    table.decide = capture
    engine.registry._cache[decider.name] = table
    secret = "ghp_" + "a" * 36
    plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind.EPHEMERAL,
            field_states={"model.route": "auto"},
            purpose="purpose-sentinel " + secret,
        )
    )
    assert secret not in observed[0].purpose and "purpose-sentinel" in observed[0].purpose
    assert store.get(plan.record_ids[0])["reason"] == "no_answer"


@pytest.mark.asyncio
async def test_conformance_adapter_carries_purpose(tmp_path, monkeypatch):
    from test.fixtures.decision_conformance import ConfidentDecider, EngineAdapter

    from cli_agent_orchestrator.decisions.targets import TargetKind

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    adapter = EngineAdapter(tmp_path)
    adapter.target = TargetKind.EPHEMERAL
    decider = ConfidentDecider()
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": decider},
        model_tiers={"codex": {"small": "model-x"}},
    )
    await adapter.delegate(
        message="message", field_states={"model.route": "auto"}, purpose="purpose-sentinel"
    )
    assert decider.requests[0].purpose == "purpose-sentinel"
    assert "purpose-sentinel" not in str(adapter.store.list())
    await adapter.engine.runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,expected_type,detail",
    [
        ("above_ceiling", "violation", "ceiling 'medium'"),
        ("explicit_unmapped", "violation", "model_tiers.codex.small"),
        ("provider_not_allowed", "violation", "provider 'codex'"),
        ("model_override_not_allowed", "violation", "set the tier in the spec"),
        ("default_unmapped", "config", "model_tiers.codex.small"),
        ("policy_invalid", "config", "ceiling requires a default"),
    ],
)
async def test_typed_policy_reason_matches_rejected_record(
    setup_engine, reason, expected_type, detail
):
    from cli_agent_orchestrator.decisions.policy import (
        DecisionInputError,
        PolicyBounds,
        PolicyConfigError,
        PolicyViolation,
    )
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine
    request = replace(req, field_states={"model.route": "auto"})
    if reason == "above_ceiling":
        request = replace(
            request,
            field_states={"model.route": "large"},
            policy=PolicyBounds(default_tier="small", max_tier="medium"),
        )
    elif reason == "explicit_unmapped":
        engine.settings_loader = lambda: replace(settings, model_tiers={})
        request = replace(request, field_states={"model.route": "small"})
    elif reason == "provider_not_allowed":
        request = replace(
            request, policy=PolicyBounds(allowed_providers=frozenset({"claude_code"}))
        )
    elif reason == "model_override_not_allowed":
        request = replace(request, target_kind=TargetKind.EPHEMERAL, model="model-x")
    elif reason == "default_unmapped":
        engine.settings_loader = lambda: replace(settings, model_tiers={})
        request = replace(
            request, target_kind=TargetKind.EPHEMERAL, policy=PolicyBounds(default_tier="small")
        )
    else:
        request = replace(request, policy=PolicyBounds(max_tier="medium"))
    with pytest.raises(DecisionInputError) as raised:
        await engine.prepare_launch(request)
    error = raised.value
    expected = PolicyViolation if expected_type == "violation" else PolicyConfigError
    opposite = PolicyConfigError if expected_type == "violation" else PolicyViolation
    assert isinstance(error, expected) and not isinstance(error, opposite)
    assert error.reason == reason and detail in str(error)
    assert store.list()[0]["reason"] == error.reason and error.reason is not None


@pytest.mark.asyncio
async def test_malformed_input_has_no_policy_reason(setup_engine):
    from cli_agent_orchestrator.decisions.policy import (
        DecisionInputError,
        PolicyConfigError,
        PolicyViolation,
    )
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, _, store, _, _ = setup_engine
    with pytest.raises(DecisionInputError) as raised:
        await engine.prepare_launch(
            replace(req, target_kind=TargetKind.EPHEMERAL, field_states={"effort.route": "max"})
        )
    assert raised.value.reason is None
    assert not isinstance(raised.value, (PolicyViolation, PolicyConfigError)) and store.list() == []
