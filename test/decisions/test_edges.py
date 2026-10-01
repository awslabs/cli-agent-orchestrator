import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from test.decisions.test_engine import setup_engine
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["off", "shadow", "on"])
@pytest.mark.parametrize("scope", ["delegation", "workflow_step", "excluded"])
@pytest.mark.parametrize(
    "violation", ["policy_invalid", "provider_not_allowed", "above_ceiling", "default_unmapped"]
)
async def test_policy_checks_precede_scope(setup_engine, state, scope, violation):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    exclusions = ("worker",) if scope == "excluded" else ()
    settings = replace(
        settings, points={"model.route": PointSettings(PointState(state), decider.name, exclusions)}
    )
    target = TargetKind.INSTALLED if scope == "excluded" else TargetKind.EPHEMERAL
    # Installed off is the deliberate early-return exception.
    if state == "off" and scope == "excluded":
        target = TargetKind.EPHEMERAL
    fields = {"model.route": "large" if violation == "above_ceiling" else "auto"}
    policy = PolicyBounds(default_tier="small", max_tier="medium")
    if violation == "policy_invalid":
        settings = replace(settings, model_tiers={"codex": {"medium": "m"}})
    elif violation == "provider_not_allowed":
        policy = PolicyBounds(allowed_providers=frozenset({"claude_code"}))
    elif violation == "default_unmapped":
        settings = replace(settings, model_tiers={})
        engine.profile_loader = lambda _: None
    engine.settings_loader = lambda: settings
    req = replace(
        req,
        owner="workflow_step" if scope == "workflow_step" else "delegation",
        target_kind=target,
        field_states=fields,
        policy=policy,
    )
    with pytest.raises(DecisionInputError):
        await engine.prepare_launch(req)
    assert decider.calls == 0
    rows = store.list()
    assert len(rows) == (state != "off")
    if rows:
        assert rows[0]["reason"] == violation
        assert rows[0]["answer"] is None and rows[0]["probabilities"] is None
        assert rows[0]["terminal_id"] is None and rows[0]["decision_status"] == "done"
        store.sweep()
        assert store.list()[0]["launch_status"] == "not_launched"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["off", "shadow", "on"])
@pytest.mark.parametrize("two_points", [False, True])
async def test_unhonored_unmapped_default_refused_without_ask(setup_engine, state, two_points):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds, UnmappedTierError
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.profile_loader = lambda _: None
    engine.honors_model = lambda *args: False
    fields = (
        {"effort.route": "auto", "model.route": "auto"} if two_points else {"model.route": "auto"}
    )
    engine.settings_loader = lambda: replace(
        settings,
        model_tiers={},
        points={p: PointSettings(PointState(state), decider.name) for p in fields},
    )
    with pytest.raises(UnmappedTierError, match="model_tiers.codex.small"):
        await engine.prepare_launch(
            replace(
                req,
                target_kind=TargetKind.EPHEMERAL,
                field_states=fields,
                policy=PolicyBounds(default_tier="small"),
            )
        )
    assert decider.calls == 0
    assert len(store.list()) == (state != "off")
    assert all(row["point"] == "model.route" for row in store.list())


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["off", "shadow", "on"])
async def test_whole_envelope_names_other_provider(setup_engine, state):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds, PolicyConfigError
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    settings = replace(
        settings,
        model_tiers={"codex": {"small": "x", "medium": "m"}, "claude_code": {"medium": "c"}},
        points={"model.route": PointSettings(PointState(state), decider.name)},
    )
    engine.settings_loader = lambda: settings
    with pytest.raises(PolicyConfigError, match="model_tiers.claude_code.small"):
        await engine.prepare_launch(
            replace(
                req,
                target_kind=TargetKind.EPHEMERAL,
                field_states={"model.route": "auto"},
                policy=PolicyBounds(default_tier="small", max_tier="medium"),
            )
        )
    assert decider.calls == 0
    assert len(store.list()) == (state != "off")


@pytest.mark.asyncio
async def test_check_order_and_step_three_reused(setup_engine):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds

    engine, req, decider, store, events, settings = setup_engine
    # An invalid envelope wins over an outside provider and an explicit ceiling violation.
    with pytest.raises(DecisionInputError):
        await engine.prepare_launch(
            replace(
                req,
                field_states={"model.route": "large"},
                policy=PolicyBounds(max_tier="small", allowed_providers=frozenset({"claude_code"})),
            )
        )
    assert store.list()[0]["reason"] == "policy_invalid"
    store.purge(all_records=True)
    # The provider check wins over explicit checks when the envelope is valid.
    with pytest.raises(DecisionInputError):
        await engine.prepare_launch(
            replace(
                req,
                field_states={"model.route": "large"},
                policy=PolicyBounds(allowed_providers=frozenset({"claude_code"})),
            )
        )
    assert store.list()[0]["reason"] == "provider_not_allowed"
    store.purge(all_records=True)
    lookup = engine.registry.get
    engine.registry.get = Mock(side_effect=lookup)
    await engine.prepare_launch(req)
    assert engine.registry.get.call_count == 1


@pytest.mark.asyncio
async def test_workflow_scope_and_effort_cap(setup_engine):
    from test.fixtures.decision_conformance import AboveCeilingDecider

    from cli_agent_orchestrator.decisions.policy import PolicyBounds

    engine, req, decider, store, events, settings = setup_engine
    plan = await engine.prepare_launch(replace(req, owner="workflow_step"))
    assert decider.calls == 0
    row = store.list()[0]
    assert (
        row["reason"] == "out_of_scope" and row["decider"] is None and row["message_hash"] is None
    )
    store.purge(all_records=True)
    engine.registry._cache[decider.name] = AboveCeilingDecider()
    plan = await engine.prepare_launch(
        replace(
            req,
            field_states={"effort.route": "auto"},
            policy=PolicyBounds(default_effort="low", max_effort="medium"),
        )
    )
    assert plan.model is None
    row = store.list()[0]
    assert row["outcome"] == "capped" and row["candidate_value"] == "medium"
    assert row["fallback_value"] == "low"


@pytest.mark.asyncio
async def test_redaction_before_builtin_and_facts(setup_engine, caplog):
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    engine, req, decider, store, events, settings = setup_engine
    token = "ghp_" + "a" * 36
    description_token = "ghp_" + "b" * 36
    engine.profile_loader = lambda _: AgentProfile(
        name="worker",
        description="description " + description_token,
        role="not a bounded role",
        model="model-y",
    )
    plan = await engine.prepare_launch(replace(req, message="sentinel-body " + token))
    seen = decider.requests[0]
    assert token not in seen.message and description_token not in seen.profile_description
    assert seen.facts.profile_role is None and seen.facts.profile == "worker"
    assert seen.facts.message_bytes == len(seen.message.encode())
    assert not hasattr(seen, "message_hash")
    assert "sentinel-body" not in str(store.list()) + str(events) + caplog.text
    # The local built-in receives the same redacted input, not a privileged bypass.
    table = FixedTableDecider()
    observed = []
    original = table.decide

    async def capture(request, config):
        observed.append(request)
        return await original(request, config)

    table.decide = capture
    engine.registry._cache[decider.name] = table
    await engine.prepare_launch(replace(req, message=token))
    assert token not in observed[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        None,
        "simple",
        "complex",
        "xlarge",
        "bad_probabilities",
        "nan",
        "outside_key",
        "missing_option",
    ],
)
async def test_invalid_answer_never_capped(setup_engine, answer):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.types import DecisionAnswer

    engine, req, decider, store, events, settings = setup_engine
    if answer is None:
        value = None
    else:
        value = DecisionAnswer(
            answer if answer in ("simple", "complex", "xlarge") else "small",
            (
                []
                if answer == "bad_probabilities"
                else (
                    {"small": float("nan")}
                    if answer == "nan"
                    else (
                        {"small": 0.9, "outside": 0.1}
                        if answer == "outside_key"
                        else {} if answer == "missing_option" else {answer: 1}
                    )
                )
            ),
        )

    async def decide(request, config):
        return None if answer is None else {"model.route": value}

    decider.decide = decide
    cap = Mock(side_effect=AssertionError("cap called"))

    class SpyPolicy(PolicyBounds):
        def cap(self, point, option):
            return cap(point, option)

    await engine.prepare_launch(replace(req, policy=SpyPolicy()))
    row = store.list()[0]
    assert row["reason"] == ("no_answer" if answer is None else "invalid_answer")
    assert row["answer"] is None and row["probabilities"] is None
    assert row["candidate_value"] is None and row["applied_value"] is None
    assert not cap.called


@pytest.mark.asyncio
async def test_no_call_and_submit_error_bind_once(setup_engine):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.honors_model = lambda *args: False
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, launch_status="not_launched")
    assert len(events) == 1 and store.list()[0]["reason"] == "not_honored"
    engine.honors_model = lambda *args: True
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.SHADOW, decider.name)}
    )
    engine.runner.submit = Mock(side_effect=RuntimeError("submit"))
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, launch_status="not_launched")
    engine.bind_launch(plan, launch_status="not_launched")
    assert len(events) == 2 and store.list()[0]["reason"] == "error"


def test_live_store_rotation_and_retention(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    from cli_agent_orchestrator.clients.database import DecisionRecordModel

    row = dict(
        point="model.route",
        state="on",
        kind="assign",
        provider="codex",
        fallback_source="none",
        outcome="fallback",
    )
    first = store.insert(row, "message")
    key_id = store.get(first)["hash_key_id"]
    store.purge(before=datetime.now(timezone.utc) - timedelta(days=1), rotate=True)
    second = store.insert(row, "message")
    assert store.get(second)["hash_key_id"] != key_id
    with store.sessions() as db:
        db.query(DecisionRecordModel).filter_by(id=first).update(
            {"created_at": datetime.now(timezone.utc) - timedelta(days=100)}
        )
        db.commit()
    assert store.purge(before=datetime.now(timezone.utc) - timedelta(days=90)) == 1
    assert store.get(second) is not None


def test_owner_uses_live_record_only(monkeypatch):
    from cli_agent_orchestrator.decisions.owner import launch_owner
    from cli_agent_orchestrator.services import script_runner

    monkeypatch.setenv("CAO_WORKFLOW_RUN_ID", "run")
    monkeypatch.setattr(
        script_runner, "run_registry", {"run": object.__new__(script_runner.ScriptRunRecord)}
    )
    body = {"env_vars": {"CAO_WORKFLOW_RUN_ID": "run", "CAO_WORKFLOW_STEP_ID": "step"}}
    assert launch_owner("run_step", body) == "workflow_step"
    assert launch_owner("create_session", body) == "delegation"
    assert launch_owner("run_step", {}) == "delegation"
    body["env_vars"]["CAO_WORKFLOW_RUN_ID"] = "unknown"
    assert launch_owner("run_step", body) == "delegation"


def test_purge_never_reuses_record_identity(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    row = dict(
        point="model.route",
        state="shadow",
        kind="assign",
        provider="codex",
        fallback_source="none",
        outcome="shadow",
        decision_status="pending",
    )
    first = store.insert(row, "old message")
    store.shadow_started(first)
    store.purge(all_records=True)
    second = store.insert(
        {**row, "state": "on", "outcome": "fallback", "decision_status": "done"}, "new message"
    )
    assert second > first
    store.update_decision(first, {"decision_status": "done", "reason": "error"})
    store.bind((second,), launch_status="launched")
    assert store.get(second)["reason"] is None
    assert len(events) == 1


def test_deferred_failure_preserves_launch_metadata(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    record = store.insert(
        dict(
            point="model.route",
            state="on",
            kind="assign",
            provider="codex",
            fallback_source="none",
            outcome="fallback",
        ),
        "message",
    )
    store.bind(
        (record,),
        launch_status="launched",
        terminal_id="worker",
        launched_model="model-x",
        model_honored=True,
    )
    store.bind((record,), launch_status="launch_failed")
    row = store.get(record)
    assert row["terminal_id"] == "worker" and row["launched_model"] == "model-x"
    assert row["model_honored"] is True and row["launch_status"] == "launch_failed"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_each_ask_contains_only_its_enabled_point(setup_engine):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = lambda: replace(
        settings,
        points={
            "model.route": PointSettings(PointState.ON, decider.name),
            "effort.route": PointSettings(),
        },
    )
    await engine.prepare_launch(
        replace(req, field_states={"model.route": "auto", "effort.route": "auto"})
    )
    assert decider.requests[0].points == ("model.route",)
    assert len(store.list()) == 1


@pytest.mark.asyncio
async def test_unavailable_decider_is_a_recorded_fallback(setup_engine):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.ON, "unknown")}
    )
    plan = await engine.prepare_launch(req)
    row = store.list()[0]
    assert row["reason"] == "decider_unavailable" and row["decider"] is None
    assert row["candidate_model"] is None and plan.model is None and decider.calls == 0


@pytest.mark.asyncio
async def test_shadow_invalid_answer_preserves_launch_default(setup_engine):
    from test.fixtures.decision_conformance import OutOfSetDecider

    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.registry._cache[decider.name] = OutOfSetDecider()
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.SHADOW, decider.name)}
    )
    plan = await engine.prepare_launch(req)
    assert plan.model is None
    engine.bind_launch(plan, terminal_id="worker", model="model-y", honored=True)
    await engine.runner.drain()
    row = store.list()[0]
    assert row["outcome"] == "shadow" and row["reason"] == "invalid_answer"
    assert row["answer"] is None and row["probabilities"] is None
    assert row["launched_model"] == "model-y"
