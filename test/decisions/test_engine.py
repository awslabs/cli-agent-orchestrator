import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.models.agent_profile import AgentProfile


@pytest.fixture
def setup_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    from test.fixtures.decision_conformance import ConfidentDecider

    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.decisions.types import PointState

    db = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    DecisionRecordModel.__table__.create(db)
    events = []
    store = DecisionStore(sessionmaker(bind=db), tmp_path / "decision-hash.key", emit=events.append)
    decider = ConfidentDecider("small", 0.91)
    settings = DecisionSettings(
        points={
            p: PointSettings(PointState.ON, decider.name) for p in ("model.route", "effort.route")
        },
        model_tiers={"codex": {"small": "model-x", "medium": "model-m", "large": "model-l"}},
    )
    runner = ShadowRunner(store)
    engine = DecisionEngine(
        DeciderRegistry({decider.name: lambda: decider}),
        store,
        runner,
        settings_loader=lambda: settings,
        profile_loader=lambda _: AgentProfile(name="worker", description="worker", model="model-y"),
        honors_model=lambda *args: True,
    )
    return (
        engine,
        DelegationRequest("assign", "codex", "worker", None, "sentinel-body"),
        decider,
        store,
        events,
        settings,
    )


@pytest.mark.asyncio
async def test_off_returns_before_any_work(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    from cli_agent_orchestrator.decisions.settings import PointSettings

    engine.settings_loader = lambda: replace(
        settings, points={p: PointSettings() for p in settings.points}
    )
    engine.registry.get = Mock(side_effect=AssertionError("loaded"))
    assert await engine.prepare_launch(req) is None
    assert store.list() == [] and events == [] and decider.calls == 0


@pytest.mark.asyncio
async def test_on_profile_overridden_and_bound_once(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    plan = await engine.prepare_launch(req)
    assert plan.model == "model-x"
    assert decider.calls == 1
    row = store.list()[0]
    assert row["fallback_value"] == "model-y" and row["candidate_model"] == "model-x"
    assert events == []
    engine.bind_launch(plan, terminal_id="worker", model="model-x", honored=True)
    engine.bind_launch(plan, launch_status="not_launched")
    engine.bind_launch(plan, launch_status="launch_failed")
    assert len(events) == 1
    assert store.list()[0]["launch_status"] == "launch_failed"
    assert engine.launch_note(plan) is None


@pytest.mark.asyncio
async def test_explicit_and_exclusions(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    assert await engine.prepare_launch(replace(req, model="explicit")) is None
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.ON, decider.name, ("worker",))}
    )
    assert await engine.prepare_launch(req) is None
    assert store.list() == [] and decider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["off", "shadow", "on"])
@pytest.mark.parametrize("owner", ["delegation", "workflow_step"])
async def test_policy_before_scope_and_state(setup_engine, state, owner):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState(state), decider.name)}
    )
    req = replace(
        req,
        owner=owner,
        target_kind=TargetKind.EPHEMERAL,
        field_states={"model.route": "large"},
        policy=PolicyBounds(default_tier="small", max_tier="medium"),
    )
    with pytest.raises(DecisionInputError, match="medium"):
        await engine.prepare_launch(req)
    assert decider.calls == 0
    assert len(store.list()) == (0 if state == "off" else 1)
    if state != "off":
        assert store.list()[0]["reason"] == "above_ceiling"
        assert len(events) == 1


@pytest.mark.asyncio
async def test_insert_failure_binds_prior_row(setup_engine):
    engine, req, decider, store, events, settings = setup_engine
    insert = store.insert
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("insert failure")
        return insert(*args, **kwargs)

    store.insert = fail_second
    with pytest.raises(RuntimeError, match="insert failure"):
        await engine.prepare_launch(
            replace(req, field_states={"effort.route": "auto", "model.route": "auto"})
        )
    assert store.list()[0]["launch_status"] == "not_launched"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_policy_fallback_records_tier_not_model(setup_engine):
    from test.fixtures.decision_conformance import UnsureDecider

    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine
    engine.profile_loader = lambda _: None
    engine.registry._cache[decider.name] = UnsureDecider()
    plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind.EPHEMERAL,
            field_states={"model.route": "auto"},
            policy=PolicyBounds(default_tier="small"),
        )
    )
    assert plan.model == "model-x"
    row = store.list()[0]
    assert row["fallback_value"] == "small" and row["candidate_model"] is None
    engine.bind_launch(plan, terminal_id="worker", model=plan.model, honored=True)
    assert store.list()[0]["launched_model"] == "model-x"


@pytest.mark.asyncio
async def test_shadow_nonblocking_overflow_shutdown_and_bind(setup_engine):
    from test.fixtures.decision_conformance import BlockingDecider

    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    event = asyncio.Event()
    blocked = BlockingDecider(event)
    engine.registry._cache[decider.name] = blocked
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.SHADOW, decider.name)}
    )
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, terminal_id="worker", model="model-y", honored=True)
    assert not event.is_set() and events == []
    await blocked.started.wait()
    event.set()
    await engine.runner.drain()
    assert len(events) == 1 and store.list()[0]["candidate_model"] == "model-x"
    engine.bind_launch(plan, launch_status="not_launched")
    assert len(events) == 1
    event.clear()
    engine.runner.max_pending = 1
    plan = await engine.prepare_launch(req)
    await blocked.started.wait()
    overflow = await engine.prepare_launch(req)
    assert store.get(overflow.record_ids[0])["reason"] == "shadow_dropped"
    engine.bind_launch(overflow, launch_status="not_launched")
    await engine.runner.close()
    assert store.get(plan.record_ids[0])["decision_status"] == "interrupted"


@pytest.mark.parametrize(
    "outcome,reason,answer,candidate,expected",
    [
        ("applied", None, "small", "small", "ran on model-x (auto: small 0.91)"),
        ("capped", None, "large", "medium", "ran on model-x (auto: large 0.91, capped to medium)"),
    ]
    + [
        ("fallback", r, None, None, f"ran on model-x (auto: fallback, {r})")
        for r in (
            "unsure",
            "low_confidence",
            "timeout",
            "error",
            "invalid_answer",
            "no_answer",
            "tier_unmapped",
            "not_honored",
            "decider_unavailable",
        )
    ],
)
def test_note_renderer(outcome, reason, answer, candidate, expected):
    from cli_agent_orchestrator.decisions.engine import render_note

    row = dict(
        point="model.route",
        state="on",
        launch_status="launched",
        outcome=outcome,
        reason=reason,
        answer=answer,
        confidence=0.91,
        candidate_value=candidate,
        launched_model="model-x",
        model_honored=True,
    )
    assert render_note(row) == expected
    row["model_honored"] = False
    assert render_note(row).startswith("ran on provider default ")
    row["model_honored"] = True
    row["launched_model"] = None
    assert render_note(row).startswith("ran on provider default ")


@pytest.mark.asyncio
async def test_module_seam_uses_lifespan_engine(setup_engine, monkeypatch):
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.decisions.engine import bind_launch, launch_note, prepare_launch

    engine, req, decider, store, events, settings = setup_engine
    monkeypatch.setattr(app.state, "decision_engine", engine, raising=False)
    plan = await prepare_launch(req)
    bind_launch(plan, terminal_id="worker", model="model-x", honored=True)
    assert launch_note(plan) == "ran on model-x (auto: small 0.91)"
    assert decider.calls == 1


@pytest.mark.asyncio
async def test_entrypoint_load_stays_lazy_when_off(setup_engine, monkeypatch):
    from importlib.metadata import EntryPoint
    from unittest.mock import Mock

    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    loader = Mock(return_value=FixedTableDecider)
    monkeypatch.setattr(EntryPoint, "load", loader)
    engine.registry = DeciderRegistry()
    engine.settings_loader = lambda: replace(
        settings, points={p: PointSettings() for p in settings.points}
    )
    assert await engine.prepare_launch(req) is None
    assert not loader.called and store.list() == [] and events == []
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.ON, "fixed_table")}
    )
    await engine.prepare_launch(req)
    await engine.prepare_launch(req)
    assert loader.call_count == 1
    await engine.registry.close()


@pytest.mark.asyncio
async def test_missing_profile_metadata_uses_existing_default(setup_engine, monkeypatch):
    from test.fixtures.decision_conformance import UnsureDecider

    from cli_agent_orchestrator.decisions import engine as engine_module

    engine, req, decider, store, events, settings = setup_engine

    def missing(name):
        raise FileNotFoundError(name)

    monkeypatch.setattr(engine_module, "load_agent_profile", missing)
    engine.profile_loader = missing
    engine.registry._cache[decider.name] = UnsureDecider()
    plan = await engine.prepare_launch(req)
    row = store.list()[0]
    assert plan.model is None and row["fallback_source"] == "none" and row["fallback_value"] is None
    assert row["reason"] == "unsure"
