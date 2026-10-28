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


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["installed", "ephemeral"])
@pytest.mark.parametrize("failure", ["cancelled", "cancelled_future", "error"])
async def test_decider_failure_returns_recorded_fallback(
    setup_engine, monkeypatch, target, failure
):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine

    async def fail(request, config):
        if failure == "cancelled_future":
            future = asyncio.get_running_loop().create_future()
            future.cancel()
            await future
        if failure == "error":
            raise RuntimeError("synthetic decider failure")
        raise asyncio.CancelledError()

    monkeypatch.setattr(decider, "decide", fail)
    plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind(target),
            field_states={"model.route": "auto"},
            policy=PolicyBounds(default_tier="small") if target == "ephemeral" else req.policy,
        )
    )
    assert plan is not None and len(plan.record_ids) == 1
    assert plan.model == ("model-x" if target == "ephemeral" else None)
    row = store.get(plan.record_ids[0])
    assert (row["outcome"], row["reason"], row["decision_status"]) == ("fallback", "error", "done")
    engine.bind_launch(plan, terminal_id="worker", model=plan.model or "model-y", honored=True)
    assert store.get(plan.record_ids[0])["launch_status"] == "launched"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_shadow_decider_cancellation_is_done_with_error(setup_engine, monkeypatch):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine

    async def fail(request, config):
        raise asyncio.CancelledError()

    monkeypatch.setattr(decider, "decide", fail)
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.SHADOW, decider.name)}
    )
    plan = await engine.prepare_launch(req)
    await engine.runner.drain()
    row = store.get(plan.record_ids[0])
    assert (row["outcome"], row["reason"], row["decision_status"]) == ("shadow", "error", "done")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_from_decider", [False, True])
async def test_launch_cancellation_cleans_decider_and_binds_prior_record(
    setup_engine, monkeypatch, cancel_from_decider
):
    engine, req, decider, store, events, settings = setup_engine
    started, cleaned = asyncio.Event(), asyncio.Event()
    decide = decider.decide

    async def block(request, config):
        if request.points == ("effort.route",):
            return await decide(request, config)
        started.set()
        try:
            if cancel_from_decider:
                launch.cancel()
                raise asyncio.CancelledError()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(decider, "decide", block)
    launch = asyncio.create_task(
        engine.prepare_launch(
            replace(req, field_states={"effort.route": "auto", "model.route": "auto"})
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if not cancel_from_decider:
            launch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await launch
    finally:
        if not launch.done():
            launch.cancel()
        await asyncio.gather(launch, return_exceptions=True)
    assert cleaned.is_set()
    rows = store.list()
    assert len(rows) == 1
    assert (rows[0]["point"], rows[0]["launch_status"]) == ("effort.route", "not_launched")
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True])
async def test_decider_timeout_cleans_up_before_recording(setup_engine, monkeypatch, shadow):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    cleaned = asyncio.Event()

    async def block(request, config):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(decider, "decide", block)
    engine.settings_loader = lambda: replace(
        settings,
        points={
            "model.route": PointSettings(
                PointState.SHADOW if shadow else PointState.ON, decider.name
            )
        },
        on_timeout_ms=10,
        shadow_timeout_ms=10,
    )
    plan = await engine.prepare_launch(req)
    await engine.runner.drain()
    assert cleaned.is_set()
    row = store.get(plan.record_ids[0])
    assert row["reason"] == "timeout" and row["decision_status"] == "done"


@pytest.mark.asyncio
async def test_shadow_close_cleans_decider_and_stays_interrupted(setup_engine, monkeypatch):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def block(request, config):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(decider, "decide", block)
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.SHADOW, decider.name)}
    )
    plan = await engine.prepare_launch(req)
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        await engine.runner.close()
    row = store.get(plan.record_ids[0])
    assert cleaned.is_set()
    assert row["decision_status"] == "interrupted" and row["reason"] is None


@pytest.mark.asyncio
async def test_owner_cancellation_consumes_completed_decider_error(setup_engine, monkeypatch):
    import gc

    engine, req, decider, store, events, settings = setup_engine
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))

    async def fail(request, config):
        launch.cancel()
        raise RuntimeError("synthetic-private-decider-error")

    monkeypatch.setattr(decider, "decide", fail)
    try:
        launch = asyncio.create_task(engine.prepare_launch(req))
        with pytest.raises(asyncio.CancelledError):
            await launch
        launch = None
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
        assert store.list() == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["answer", "error"])
async def test_timeout_keeps_decider_cancellation_cleanup_result(
    setup_engine, monkeypatch, cleanup
):
    engine, req, decider, store, events, settings = setup_engine
    decide = decider.decide

    async def block(request, config):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if cleanup == "error":
                raise RuntimeError("synthetic cleanup failure")
            return await decide(request, config)

    monkeypatch.setattr(decider, "decide", block)
    engine.settings_loader = lambda: replace(settings, on_timeout_ms=50)
    plan = await engine.prepare_launch(req)
    row = store.get(plan.record_ids[0])
    if cleanup == "answer":
        assert plan.model == "model-x"
        assert (row["outcome"], row["reason"]) == ("applied", None)
    else:
        assert plan.model is None
        assert (row["outcome"], row["reason"]) == ("fallback", "error")


@pytest.mark.parametrize("fatal", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("when", ["decide", "cleanup", "cleanup_twice", "owner_cancel"])
def test_fatal_decider_exception_reaches_owner_without_stopping_loop(
    setup_engine, monkeypatch, fatal, when
):
    engine, req, decider, store, events, settings = setup_engine
    observed = []
    decide = decider.decide

    async def exercise():
        started = asyncio.Event()
        cleanup_started = asyncio.Event()

        async def fail(request, config):
            if request.points == ("effort.route",):
                return await decide(request, config)
            started.set()
            if when in {"cleanup", "cleanup_twice"}:
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    try:
                        if when == "cleanup_twice":
                            await asyncio.Event().wait()
                        else:
                            await asyncio.sleep(0)
                    finally:
                        raise fatal("synthetic decider exit")
            if when == "owner_cancel":
                launch.cancel()
            raise fatal("synthetic decider exit")

        async def request_task():
            try:
                await engine.prepare_launch(
                    replace(req, field_states={"effort.route": "auto", "model.route": "auto"})
                )
            except BaseException as exc:
                observed.append(exc)

        monkeypatch.setattr(decider, "decide", fail)
        launch = asyncio.create_task(request_task())
        if when in {"cleanup", "cleanup_twice"}:
            await asyncio.wait_for(started.wait(), 1)
            launch.cancel()
            if when == "cleanup_twice":
                await asyncio.wait_for(cleanup_started.wait(), 1)
                launch.cancel()
        await launch
        await asyncio.sleep(0)

    escaped = None
    try:
        asyncio.run(exercise())
    except BaseException as exc:
        escaped = exc
    assert escaped is None, f"fatal decider exception escaped the request: {type(escaped)}"
    assert len(observed) == 1 and type(observed[0]) is fatal
    rows = store.list()
    assert len(rows) == 1
    assert (rows[0]["point"], rows[0]["launch_status"]) == ("effort.route", "not_launched")
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_owner_cancel_waits_for_multiturn_cleanup_without_error_leak(
    setup_engine, monkeypatch, cleanup_error
):
    import gc

    engine, req, decider, store, events, settings = setup_engine
    started, cleaned = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))

    async def block(request, config):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            for _ in range(3):
                await asyncio.sleep(0)
            cleaned.set()
            if cleanup_error:
                raise RuntimeError("synthetic-private-cleanup-error")

    monkeypatch.setattr(decider, "decide", block)
    launch = asyncio.create_task(engine.prepare_launch(req))
    try:
        await asyncio.wait_for(started.wait(), 1)
        launch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await launch
        assert cleaned.is_set()
        launch = None
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
        assert store.list() == []
    finally:
        if launch is not None and not launch.done():
            launch.cancel()
        if launch is not None:
            await asyncio.gather(launch, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
