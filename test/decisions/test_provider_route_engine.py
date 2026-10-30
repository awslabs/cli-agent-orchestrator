"""#1099: provider.route through the decision engine, asked first and within CAO's options."""

import asyncio
import logging
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.decisions.types import DecisionAnswer

TIERS = {
    provider: {t: f"{provider}-{t}" for t in ("small", "medium", "large")}
    for provider in ("claude_code", "codex")
}
LABELS = {"claude_code": "hard", "codex": "sandboxed"}
BRIEF = "inspect the synthetic logs"


class Decider:
    name = "route_test"
    version = "1"
    points = frozenset({"model.route", "effort.route", "provider.route"})

    def __init__(self):
        self.answers = {"provider.route": "codex", "model.route": "large", "effort.route": "high"}
        self.confidence = 0.9
        self.requests = []

    async def decide(self, request, config):
        self.requests.append(request)
        return {
            point: DecisionAnswer(self.answers[point], {self.answers[point]: self.confidence})
            for point in request.points
        }


@pytest.fixture
def route(tmp_path, monkeypatch):
    """An engine whose only target is a claimed ephemeral launch declaring every point auto."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState, ProviderOptions

    db = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    DecisionRecordModel.__table__.create(db)
    events = []
    store = DecisionStore(sessionmaker(bind=db), tmp_path / "decision-hash.key", emit=events.append)
    decider = Decider()
    honored = []

    def honors(provider, name, profile):
        honored.append(provider)
        return True

    def settings(state="on", **states):
        return DecisionSettings(
            points={
                point: PointSettings(PointState(states.get(point, state)), decider.name)
                for point in ("model.route", "effort.route", "provider.route")
            },
            model_tiers=TIERS,
        )

    engine = DecisionEngine(
        DeciderRegistry({decider.name: lambda: decider}),
        store,
        ShadowRunner(store),
        settings_loader=lambda: settings(),
        honors_model=honors,
        honors_effort=honors,
    )
    req = DelegationRequest(
        "assign",
        "claude_code",
        "Ramones-log_triage-3f9a",
        None,
        BRIEF,
        target_kind=TargetKind.EPHEMERAL,
        field_states={"provider.route": "auto", "model.route": "auto", "effort.route": "auto"},
        policy=PolicyBounds(
            min_tier="small",
            default_tier="medium",
            max_tier="large",
            min_effort="low",
            default_effort="medium",
            max_effort="high",
            allowed_providers=frozenset(TIERS),
        ),
        purpose="log_triage",
        provider_options=ProviderOptions(("claude_code", "codex"), LABELS),
    )
    yield type(
        "Route",
        (),
        dict(
            engine=engine,
            req=req,
            decider=decider,
            store=store,
            events=events,
            settings=settings,
            honored=honored,
            options=ProviderOptions,
        ),
    )


def rows(route, point=None):
    return [
        row
        for row in sorted(route.store.list(), key=lambda row: row["id"])
        if point is None or row["point"] == point
    ]


def only(route, point):
    (row,) = rows(route, point)
    return row


# The contract: one new point, an optional field, a plan field and a fallback source.


def test_the_point_is_declared_with_every_provider_id_as_its_static_options():
    from cli_agent_orchestrator.decisions.settings import STATE_ENV
    from cli_agent_orchestrator.decisions.types import (
        POINTS,
        DecisionFacts,
        DecisionRequest,
        Fallback,
        LaunchPlan,
    )
    from cli_agent_orchestrator.models.provider import ProviderType

    point = POINTS["provider.route"]
    assert point.question == "Which CLI should run this task?"
    assert set(point.options) == {p.value for p in ProviderType}
    assert STATE_ENV["provider.route"] == "CAO_DECISION_PROVIDER_ROUTE"
    facts = DecisionFacts("codex", "assign", None, None, 1, False)
    request = DecisionRequest(1, "id", ("model.route",), "m", None, facts=facts)
    assert request.options is None
    with pytest.raises(TypeError):
        DecisionRequest(1, "id", ("model.route",), "m", None, facts, ("codex",))
    assert LaunchPlan().provider is None
    assert Fallback("claude_code", "creator").source == "creator"


def test_policy_has_no_bound_or_default_for_the_provider_point():
    from cli_agent_orchestrator.decisions.policy import PolicyBounds, resolve_fallback
    from cli_agent_orchestrator.decisions.types import Fallback

    bounds = PolicyBounds(
        min_tier="small",
        default_tier="medium",
        max_tier="large",
        min_effort="low",
        default_effort="medium",
        max_effort="high",
    )
    assert bounds.ceiling_for("provider.route") is None
    assert bounds.floor_for("provider.route") is None
    assert bounds.default_for("provider.route") is None
    bounds.validate(TIERS)
    assert bounds.cap("provider.route", "codex") == ("codex", False)
    assert resolve_fallback("provider.route", None, bounds, TIERS["codex"], "claude_code") == (
        Fallback("claude_code", "creator")
    )
    # The other points keep their bounds.
    assert bounds.ceiling_for("model.route") == "large"
    assert bounds.floor_for("effort.route") == "low"


# AC3: on, a confident in-set answer routes the launch, asked first with one request_id.


@pytest.mark.asyncio
async def test_AC3_a_confident_answer_routes_and_later_points_see_the_decided_provider(route):
    plan = await route.engine.prepare_launch(route.req)
    assert plan.provider == "codex"
    asked = [request.points for request in route.decider.requests]
    assert asked == [("provider.route",), ("model.route",), ("effort.route",)]
    provider_ask, model_ask, effort_ask = route.decider.requests
    assert len({request.request_id for request in route.decider.requests}) == 1
    assert provider_ask.facts.provider == "claude_code"
    assert provider_ask.options == ("claude_code", "codex")
    assert model_ask.facts.provider == effort_ask.facts.provider == "codex"
    assert model_ask.options is None and effort_ask.options is None
    # P's table and P's honours checks.
    assert plan.model == TIERS["codex"]["large"] and plan.effort == "high"
    assert set(route.honored) == {"codex"}
    record = only(route, "provider.route")
    assert record["outcome"] == "applied" and record["reason"] is None
    assert record["applied_value"] == record["candidate_value"] == record["answer"] == "codex"
    assert record["fallback_value"] == "claude_code" and record["fallback_source"] == "creator"
    assert record["provider"] == "codex"
    assert record["options"] == ["claude_code", "codex"]
    assert record["fallback_enforcement"] == "hard" and record["applied_enforcement"] == "sandboxed"
    for point in ("model.route", "effort.route"):
        other = only(route, point)
        assert other["provider"] == "codex"
        assert other["options"] is None and other["fallback_enforcement"] is None
    assert only(route, "model.route")["candidate_model"] == TIERS["codex"]["large"]
    assert set(plan.record_ids) == {row["id"] for row in rows(route)}


@pytest.mark.asyncio
async def test_AC3_a_provider_only_auto_launch_routes_and_leaves_the_model_to_the_site(route):
    # With no model.route field the engine names no model; the ephemeral site maps the stored
    # tier with the decided provider's table (the route tests check that).
    req = replace(route.req, field_states={"provider.route": "auto"})
    plan = await route.engine.prepare_launch(req)
    assert plan.provider == "codex" and plan.model is None and plan.effort is None
    assert [request.points for request in route.decider.requests] == [("provider.route",)]


@pytest.mark.asyncio
async def test_an_answer_naming_the_fallback_applies_without_routing(route):
    route.decider.answers["provider.route"] = "claude_code"
    plan = await route.engine.prepare_launch(route.req)
    assert plan.provider is None and plan.model == TIERS["claude_code"]["large"]
    record = only(route, "provider.route")
    assert record["outcome"] == "applied" and record["provider"] == "claude_code"
    assert record["applied_enforcement"] == "hard"
    assert {request.facts.provider for request in route.decider.requests} == {"claude_code"}


# AC4: an answer outside this launch's options falls back, whatever the static options say.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,confidence,reason",
    [
        ("kiro_cli", 0.9, "invalid_answer"),
        ("not-a-provider", 0.9, "invalid_answer"),
        ("unsure", 0.9, "unsure"),
        ("codex", 0.2, "low_confidence"),
    ],
)
async def test_AC4_anything_but_a_confident_in_set_answer_runs_the_fallback(
    route, answer, confidence, reason
):
    route.decider.answers["provider.route"] = answer
    route.decider.confidence = confidence
    plan = await route.engine.prepare_launch(route.req)
    assert plan.provider is None
    record = only(route, "provider.route")
    assert (record["outcome"], record["reason"]) == ("fallback", reason)
    assert record["provider"] == "claude_code" and record["applied_value"] is None
    assert record["applied_enforcement"] is None
    assert "kiro_cli" not in record["options"]
    assert all(request.facts.provider == "claude_code" for request in route.decider.requests)
    if confidence >= 0.7:
        assert plan.model == TIERS["claude_code"]["large"]


def test_a_provider_answer_is_valid_only_within_the_request_options():
    from cli_agent_orchestrator.decisions.engine import DecisionEngine
    from cli_agent_orchestrator.decisions.types import DecisionFacts, DecisionRequest

    facts = DecisionFacts("claude_code", "assign", None, None, 1, False)
    request = DecisionRequest(1, "id", ("provider.route",), "m", None, facts=facts)
    answer = DecisionAnswer("codex", {"codex": 0.9})
    assert not DecisionEngine._valid("provider.route", answer, request)
    assert DecisionEngine._valid(
        "provider.route", answer, replace(request, options=("claude_code", "codex"))
    )
    assert not DecisionEngine._valid(
        "provider.route", answer, replace(request, options=("claude_code",))
    )
    # A probability for a provider outside the options is invalid too.
    spread = DecisionAnswer("codex", {"codex": 0.6, "kiro_cli": 0.4})
    assert not DecisionEngine._valid(
        "provider.route", spread, replace(request, options=("claude_code", "codex"))
    )


# AC5: with one option nothing is asked; the record says single_option.


@pytest.mark.asyncio
@pytest.mark.parametrize("state,outcome", [("on", "fallback"), ("shadow", "shadow")])
async def test_AC5_one_option_asks_nothing_and_records_single_option(route, state, outcome):
    route.engine.settings_loader = lambda: route.settings(**{"provider.route": state})
    req = replace(
        route.req,
        field_states={"provider.route": "auto"},
        provider_options=route.options(("claude_code",), {"claude_code": "hard"}),
    )
    plan = await route.engine.prepare_launch(req)
    assert route.decider.requests == []
    assert plan.provider is None
    record = only(route, "provider.route")
    assert (record["outcome"], record["reason"]) == (outcome, "single_option")
    assert record["state"] == state and record["decider"] is None
    assert record["options"] == ["claude_code"] and record["fallback_enforcement"] == "hard"
    assert record["decision_status"] == "done"


@pytest.mark.asyncio
async def test_single_option_neither_warns_nor_resets_a_failure_streak(route, caplog):
    from cli_agent_orchestrator.decisions.engine import _FailureStreak

    route.engine._streaks[(route.decider.name, "provider.route")] = _FailureStreak("timeout")
    req = replace(
        route.req,
        field_states={"provider.route": "auto"},
        provider_options=route.options(("claude_code",), {"claude_code": "hard"}),
    )
    with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.decisions.engine"):
        await route.engine.prepare_launch(req)
    assert (route.decider.name, "provider.route") in route.engine._streaks
    assert not [r for r in caplog.records if "decider" in r.getMessage()]


# AC2: shadow records the answer and never applies it.


@pytest.mark.asyncio
async def test_AC2_shadow_records_pending_and_launches_the_fallback(route):
    route.engine.settings_loader = lambda: route.settings(**{"provider.route": "shadow"})
    plan = await route.engine.prepare_launch(route.req)
    assert plan.provider is None
    assert plan.model == TIERS["claude_code"]["large"]
    await route.engine.runner.drain()
    record = only(route, "provider.route")
    assert record["outcome"] == "shadow" and record["answer"] == "codex"
    assert record["applied_value"] is None and record["applied_enforcement"] is None
    assert record["provider"] == "claude_code" and record["options"] == ["claude_code", "codex"]
    assert all(request.facts.provider == "claude_code" for request in route.decider.requests)
    assert len({request.request_id for request in route.decider.requests}) == 1


@pytest.mark.asyncio
async def test_off_writes_no_provider_record(route):
    route.engine.settings_loader = lambda: route.settings(**{"provider.route": "off"})
    plan = await route.engine.prepare_launch(route.req)
    assert plan.provider is None and rows(route, "provider.route") == []
    assert [request.points for request in route.decider.requests] == [
        ("model.route",),
        ("effort.route",),
    ]


@pytest.mark.asyncio
async def test_a_workflow_step_owner_records_out_of_scope(route):
    from cli_agent_orchestrator.decisions.owner import WORKFLOW_STEP

    plan = await route.engine.prepare_launch(replace(route.req, owner=WORKFLOW_STEP))
    assert route.decider.requests == [] and plan.provider is None
    record = only(route, "provider.route")
    assert record["reason"] == "out_of_scope" and record["options"] == ["claude_code", "codex"]


@pytest.mark.asyncio
async def test_a_decider_that_does_not_declare_the_point_is_unavailable(route):
    route.decider.points = frozenset({"model.route", "effort.route"})
    plan = await route.engine.prepare_launch(route.req)
    assert plan.provider is None
    assert only(route, "provider.route")["reason"] == "decider_unavailable"


@pytest.mark.asyncio
async def test_the_fixed_table_never_answers_an_ephemeral_provider_ask(route, caplog):
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    route.engine.registry = DeciderRegistry({"fixed_table": FixedTableDecider})
    route.engine.settings_loader = lambda: DecisionSettings(
        points={"provider.route": PointSettings(PointState.ON, "fixed_table")},
        model_tiers=TIERS,
        deciders={"fixed_table": {"provider.route": {"roles": {"reviewer": "codex"}}}},
    )
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.decisions.engine"):
        plan = await route.engine.prepare_launch(route.req)
    assert plan.provider is None
    assert only(route, "provider.route")["reason"] == "no_answer"
    assert not [r for r in caplog.records if "failing" in r.getMessage()]


# The invariant: only decision_inputs asks provider.route, with CAO's own options.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"field_states": {"provider.route": "codex"}},
        {"provider_options": None},
        {"target_kind": "installed"},
        {"options": (("codex",), {"codex": "sandboxed"})},
        {"options": (("claude_code", "claude_code"), {"claude_code": "hard"})},
        {"options": (("claude_code", "codex"), {"claude_code": "hard"})},
        {"options": (("claude_code",), {"claude_code": "hard", "codex": "sandboxed"})},
    ],
)
async def test_provider_route_without_cao_options_is_an_input_error(route, change):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError
    from cli_agent_orchestrator.decisions.targets import TargetKind

    req = route.req
    if "options" in change:
        req = replace(req, provider_options=route.options(*change["options"]))
    elif "target_kind" in change:
        req = replace(req, target_kind=TargetKind.INSTALLED)
    else:
        req = replace(req, **change)
    with pytest.raises(DecisionInputError):
        await route.engine.prepare_launch(req)
    assert route.decider.requests == [] and route.store.list() == []


@pytest.mark.asyncio
async def test_a_decided_provider_outside_allowed_providers_rejects_and_binds_its_record(route):
    from cli_agent_orchestrator.decisions.policy import ProviderNotAllowedError

    req = replace(
        route.req,
        policy=replace(route.req.policy, allowed_providers=frozenset({"claude_code"})),
    )
    with pytest.raises(ProviderNotAllowedError):
        await route.engine.prepare_launch(req)
    record = only(route, "provider.route")
    assert record["outcome"] == "applied" and record["launch_status"] == "not_launched"
    assert [request.points for request in route.decider.requests] == [("provider.route",)]


@pytest.mark.asyncio
async def test_a_decided_provider_without_the_default_tier_rejects_and_binds_its_record(route):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError
    from cli_agent_orchestrator.decisions.settings import DecisionSettings

    # Codex has no table at all, so the snapshot validates and only the decided provider's
    # default tier is unmapped.
    settings = route.settings()
    route.engine.settings_loader = lambda: DecisionSettings(
        points=settings.points, model_tiers={"claude_code": TIERS["claude_code"]}
    )
    req = replace(
        route.req,
        policy=replace(route.req.policy, max_tier=None, min_tier=None, allowed_providers=None),
    )
    with pytest.raises(DecisionInputError) as error:
        await route.engine.prepare_launch(req)
    assert error.value.reason == "default_unmapped"
    assert only(route, "provider.route")["launch_status"] == "not_launched"


# #1102's containment, for the provider ask (twins of test_engine.py's tests).


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancelled", "cancelled_future", "error"])
async def test_a_provider_decider_failure_returns_the_recorded_fallback(
    route, monkeypatch, failure
):
    async def fail(request, config):
        if "provider.route" not in request.points:
            return {"model.route": DecisionAnswer("large", {"large": 0.9})}
        if failure == "cancelled_future":
            future = asyncio.get_running_loop().create_future()
            future.cancel()
            await future
        if failure == "error":
            raise RuntimeError("synthetic decider failure")
        raise asyncio.CancelledError()

    monkeypatch.setattr(route.decider, "decide", fail)
    req = replace(route.req, field_states={"provider.route": "auto", "model.route": "auto"})
    plan = await route.engine.prepare_launch(req)
    assert plan.provider is None and plan.model == TIERS["claude_code"]["large"]
    record = only(route, "provider.route")
    assert (record["outcome"], record["reason"], record["decision_status"]) == (
        "fallback",
        "error",
        "done",
    )
    route.engine.bind_launch(plan, terminal_id="worker", model=plan.model, honored=True)
    assert only(route, "provider.route")["launch_status"] == "launched"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_from_decider", [False, True])
async def test_a_cancel_during_the_model_ask_binds_the_applied_provider_record(
    route, monkeypatch, cancel_from_decider
):
    started, cleaned = asyncio.Event(), asyncio.Event()
    decide = route.decider.decide

    async def block(request, config):
        if request.points == ("provider.route",):
            return await decide(request, config)
        started.set()
        try:
            if cancel_from_decider:
                launch.cancel()
                raise asyncio.CancelledError()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(route.decider, "decide", block)
    launch = asyncio.create_task(route.engine.prepare_launch(route.req))
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
    (record,) = rows(route)
    assert (record["point"], record["outcome"], record["launch_status"]) == (
        "provider.route",
        "applied",
        "not_launched",
    )
    assert len(route.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True])
async def test_a_provider_decider_timeout_cleans_up_before_recording(route, monkeypatch, shadow):
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    cleaned = asyncio.Event()

    async def block(request, config):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(route.decider, "decide", block)
    route.engine.settings_loader = lambda: DecisionSettings(
        points={
            "provider.route": PointSettings(
                PointState.SHADOW if shadow else PointState.ON, route.decider.name
            )
        },
        model_tiers=TIERS,
        on_timeout_ms=10,
        shadow_timeout_ms=10,
    )
    plan = await route.engine.prepare_launch(
        replace(route.req, field_states={"provider.route": "auto"})
    )
    await route.engine.runner.drain()
    assert cleaned.is_set() and plan.provider is None
    record = only(route, "provider.route")
    assert record["reason"] == "timeout" and record["decision_status"] == "done"


@pytest.mark.parametrize("fatal", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("when", ["decide", "cleanup", "owner_cancel"])
def test_a_fatal_provider_decider_exception_reaches_the_owner(route, monkeypatch, fatal, when):
    observed = []

    async def exercise():
        started = asyncio.Event()

        async def fail(request, config):
            started.set()
            if when == "cleanup":
                try:
                    await asyncio.Event().wait()
                finally:
                    raise fatal("synthetic decider exit")
            if when == "owner_cancel":
                launch.cancel()
            raise fatal("synthetic decider exit")

        async def request_task():
            try:
                await route.engine.prepare_launch(route.req)
            except BaseException as exc:
                observed.append(exc)

        monkeypatch.setattr(route.decider, "decide", fail)
        launch = asyncio.create_task(request_task())
        if when == "cleanup":
            await asyncio.wait_for(started.wait(), 1)
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
    # The provider ask raised before its record existed; nothing was written for it.
    assert rows(route) == []


@pytest.mark.asyncio
async def test_launches_without_provider_route_share_the_same_request_object(route):
    """The per-point request is today's object when no provider was decided."""
    req = replace(
        route.req,
        field_states={"model.route": "auto", "effort.route": "auto"},
        provider_options=None,
    )
    await route.engine.prepare_launch(req)
    first, second = route.decider.requests
    assert first.request_id == second.request_id and first.options is None
    assert first.facts is second.facts


# Records: three nullable columns, written only on provider.route rows.


def test_the_options_column_round_trips_as_a_list(tmp_path):
    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.store import DecisionStore

    db = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    DecisionRecordModel.__table__.create(db)
    store = DecisionStore(sessionmaker(bind=db), tmp_path / "key", emit=lambda _: None)
    base = dict(
        point="provider.route",
        state="on",
        kind="assign",
        provider="codex",
        fallback_source="creator",
        fallback_value="claude_code",
        outcome="applied",
    )
    record_id = store.insert(
        {
            **base,
            "options": ("claude_code", "codex"),
            "fallback_enforcement": "hard",
            "applied_enforcement": "sandboxed",
        },
        BRIEF,
    )
    row = store.get(record_id)
    assert row["options"] == ["claude_code", "codex"]
    assert (row["fallback_enforcement"], row["applied_enforcement"]) == ("hard", "sandboxed")
    other = store.get(store.insert({**base, "point": "model.route"}, BRIEF))
    assert other["options"] is None and other["fallback_enforcement"] is None


def _legacy_decision_table(path):
    import sqlite3

    rows = [(1, "model.route", "fixture", "error"), (2, "effort.route", None, "unsure")]
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE decision_records (id INTEGER PRIMARY KEY, point TEXT, decider TEXT, "
            "reason TEXT)"
        )
        db.executemany("INSERT INTO decision_records VALUES (?,?,?,?)", rows)
    return rows


COLUMNS = ("options", "fallback_enforcement", "applied_enforcement")


def test_the_migration_is_additive_nullable_idempotent_and_leaves_rows(tmp_path, monkeypatch):
    import sqlite3

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "legacy.db"
    rows = _legacy_decision_table(path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_provider_route()
    database._migrate_decision_records_provider_route()
    with sqlite3.connect(path) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(decision_records)")}
        migrated = db.execute(
            "SELECT id, point, decider, reason, " + ", ".join(COLUMNS) + " FROM decision_records"
        ).fetchall()
    for column in COLUMNS:
        assert columns[column][3] == 0 and columns[column][4] is None  # nullable, no default
    assert len(columns) == 4 + len(COLUMNS)
    assert migrated == [row + (None, None, None) for row in rows]


def test_the_migration_adds_only_the_missing_columns(tmp_path, monkeypatch):
    import sqlite3

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "partial.db"
    _legacy_decision_table(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE decision_records ADD COLUMN options TEXT")
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_provider_route()
    with sqlite3.connect(path) as db:
        names = [row[1] for row in db.execute("PRAGMA table_info(decision_records)")]
    assert all(names.count(column) == 1 for column in COLUMNS)


def test_the_migration_creates_nothing_without_the_table(tmp_path, monkeypatch):
    import sqlite3

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "other.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_provider_route()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_init_db_adds_the_columns_to_an_existing_database(tmp_path, monkeypatch):
    import sqlite3
    from unittest.mock import patch

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "legacy.db"
    _legacy_decision_table(path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    with patch.object(database, "Base"):
        database.init_db()
    with sqlite3.connect(path) as db:
        names = {row[1] for row in db.execute("PRAGMA table_info(decision_records)")}
    assert set(COLUMNS) <= names


def test_a_fresh_database_has_the_columns():
    from cli_agent_orchestrator.clients.database import DecisionRecordModel

    columns = DecisionRecordModel.__table__.columns
    assert all(columns[column].nullable for column in COLUMNS)
