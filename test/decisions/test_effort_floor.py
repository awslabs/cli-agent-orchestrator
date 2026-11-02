"""#1083 on the #810 side: an effort floor, LaunchPlan.effort and honors_effort.

All three are provisional: Q1 (the plan carries the decided effort), Q2 (an optional
effort floor, following #1068's tier floor) and Q3 (each point is its own request).
"""

import asyncio
import itertools
from dataclasses import replace
from test.decisions.test_engine import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    setup_engine,
)

import pytest

from cli_agent_orchestrator.decisions.policy import (
    POLICY_REJECTION_REASONS,
    BelowFloorError,
    PolicyBounds,
    PolicyConfigError,
    PolicyViolation,
)
from cli_agent_orchestrator.decisions.registry import DeciderRegistry
from cli_agent_orchestrator.decisions.settings import PointSettings
from cli_agent_orchestrator.decisions.targets import TargetKind
from cli_agent_orchestrator.decisions.types import POINTS, DecisionAnswer, LaunchPlan, PointState

EFFORTS = POINTS["effort.route"].options
TIERS = POINTS["model.route"].options
BOUNDS = (None,) + EFFORTS


def index(effort, missing):
    return EFFORTS.index(effort) if effort is not None else missing


@pytest.mark.parametrize("floor,default,ceiling", list(itertools.product(BOUNDS, repeat=3)))
def test_Q2_effort_floor_default_ceiling_order(floor, default, ceiling):
    bounds = PolicyBounds(default_effort=default, max_effort=ceiling, min_effort=floor)
    valid = (
        (ceiling is None or default is not None)
        and (floor is None or default is not None)
        and index(floor, 0) <= index(default, 0) <= index(ceiling, 2)
        and index(floor, 0) <= index(ceiling, 2)
    )
    if valid:
        bounds.validate({})
    else:
        with pytest.raises(PolicyConfigError, match="effort.route"):
            bounds.validate({})


def test_Q2_effort_floor_value_is_in_the_closed_set():
    with pytest.raises(PolicyConfigError, match="effort.route"):
        PolicyBounds(default_effort="medium", min_effort="huge").validate({})


def test_Q2_an_effort_floor_leaves_the_tier_rules_alone():
    bounds = PolicyBounds(default_effort="medium", max_effort="high", min_effort="medium")
    bounds.validate({})
    bounds.check_explicit("model.route", "small", "codex", {"codex": {"small": "model-s"}})
    assert bounds.cap("model.route", "small") == ("small", False)
    assert bounds.floor_for("model.route") is None
    assert bounds.floor_for("effort.route") == "medium"


def test_Q2_explicit_effort_below_floor_refuses_before_the_ceiling():
    bounds = PolicyBounds(default_effort="medium", max_effort="medium", min_effort="medium")
    with pytest.raises(BelowFloorError) as err:
        bounds.check_explicit("effort.route", "low", "codex", {})
    assert isinstance(err.value, PolicyViolation)
    assert err.value.reason == "below_floor" and "below_floor" in POLICY_REJECTION_REASONS
    assert err.value.point == "effort.route"
    assert "effort" in str(err.value) and "tier" not in str(err.value)
    bounds.check_explicit("effort.route", "medium", "codex", {})
    with pytest.raises(PolicyViolation) as above:
        bounds.check_explicit("effort.route", "high", "codex", {})
    assert above.value.reason == "above_ceiling" and above.value.point == "effort.route"


def test_violations_name_the_point_they_bound():
    bounds = PolicyBounds(default_tier="medium", max_tier="medium", min_tier="medium")
    for value, reason in (("small", "below_floor"), ("large", "above_ceiling")):
        with pytest.raises(PolicyViolation) as err:
            bounds.check_explicit("model.route", value, "codex", {})
        assert err.value.reason == reason and err.value.point == "model.route"
        assert "tier" in str(err.value)


@pytest.mark.parametrize(
    "floor,ceiling",
    [
        pair
        for pair in itertools.product(BOUNDS, repeat=2)
        if index(pair[0], 0) <= index(pair[1], 2)
    ],
)
def test_Q2_cap_clamps_an_effort_into_floor_and_ceiling(floor, ceiling):
    bounds = PolicyBounds(min_effort=floor, max_effort=ceiling)
    for option in EFFORTS:
        expected = EFFORTS[min(max(EFFORTS.index(option), index(floor, 0)), index(ceiling, 2))]
        assert bounds.cap("effort.route", option) == (expected, expected != option)


def test_Q1_positional_launch_plans_carry_no_effort():
    plan = LaunchPlan("model-x", (1,))
    assert (plan.model, plan.record_ids, plan.effort) == ("model-x", (1,), None)


class Scripted:
    """Answers each point it is asked for, from a per-point script."""

    name = "fixture"
    version = "1"
    points = frozenset(POINTS)

    def __init__(self):
        self.answers = {"model.route": ("large", 0.91), "effort.route": ("high", 0.91)}
        self.failures = {}
        self.requests = []

    async def decide(self, request, config):
        self.requests.append(request)
        failures = {self.failures.get(point) for point in request.points}
        if "timeout" in failures:
            await asyncio.sleep(1)
        if "error" in failures:
            raise ValueError("synthetic decider failure")
        return {
            point: DecisionAnswer(
                self.answers[point][0], {self.answers[point][0]: self.answers[point][1]}
            )
            for point in request.points
        }


@pytest.fixture
def scripted(setup_engine):
    engine, req, _, store, events, settings = setup_engine
    decider = Scripted()
    engine.registry = DeciderRegistry({decider.name: lambda: decider})

    def configure(states=None, **changes):
        points = {
            point: PointSettings(PointState(state), decider.name)
            for point, state in (states or {"model.route": "on", "effort.route": "on"}).items()
        }
        engine.settings_loader = lambda: replace(settings, points=points, **changes)

    configure()
    req = replace(req, agent_profile="ephemeral-sentinel", target_kind=TargetKind.EPHEMERAL)
    return engine, req, decider, store, configure


EFFORT_ONLY = {"effort.route": "auto"}
POLICY = dict(default_effort="medium", min_effort="low", max_effort="high")


def launch(engine, req, fields=EFFORT_ONLY, **policy):
    return engine.prepare_launch(
        replace(
            req,
            field_states=fields,
            policy=PolicyBounds(
                **{**POLICY, **policy}, allowed_providers=frozenset({req.provider})
            ),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,policy,expected",
    [
        ("high", {}, "high"),
        ("low", {}, "low"),
        ("high", {"max_effort": "medium"}, "medium"),
        ("low", {"min_effort": "medium"}, "medium"),
    ],
)
async def test_Q1_an_on_mode_answer_is_carried_capped_into_bounds(
    scripted, answer, policy, expected
):
    engine, req, decider, store, _ = scripted
    decider.answers["effort.route"] = (answer, 0.91)
    plan = await launch(engine, req, **policy)
    (record,) = store.list()
    assert plan.effort == record["applied_value"] == expected
    assert record["outcome"] == ("applied" if answer == expected else "capped")
    assert record["answer"] == answer and record["point"] == "effort.route"
    assert record["fallback_source"] == "policy" and record["fallback_value"] == "medium"
    assert record["target_kind"] == "ephemeral" and record["agent_profile"] == req.agent_profile
    # An effort-only launch carries no model of its own.
    assert plan.model is None and plan.record_ids == (record["id"],)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,failure,reason",
    [
        (("unsure", 0.91), None, "unsure"),
        (("high", 0.2), None, "low_confidence"),
        (("huge", 0.91), None, "invalid_answer"),
        (("high", 0.91), "timeout", "timeout"),
        (("high", 0.91), "error", "error"),
    ],
)
async def test_Q1_a_fallback_carries_no_effort(scripted, answer, failure, reason):
    engine, req, decider, store, configure = scripted
    configure(on_timeout_ms=50)
    decider.answers["effort.route"] = answer
    if failure:
        decider.failures["effort.route"] = failure
    plan = await launch(engine, req)
    (record,) = store.list()
    assert record["outcome"] == "fallback" and record["reason"] == reason
    assert record["applied_value"] is None and record["fallback_value"] == "medium"
    assert plan.effort is None and plan.record_ids == (record["id"],)


@pytest.mark.asyncio
async def test_Q1_shadow_records_and_carries_no_effort(scripted):
    engine, req, decider, store, configure = scripted
    configure({"effort.route": "shadow"})
    plan = await launch(engine, req)
    await engine.runner.drain()
    (record,) = store.list()
    assert record["outcome"] == "shadow" and record["answer"] == "high"
    assert record["applied_value"] is None
    assert plan.effort is None and plan.record_ids == (record["id"],)


@pytest.mark.asyncio
async def test_Q1_off_asks_nothing_and_returns_no_plan(scripted):
    engine, req, decider, store, configure = scripted
    configure({"effort.route": "off"})
    assert await launch(engine, req) is None
    assert decider.requests == [] and store.list() == []


@pytest.mark.asyncio
async def test_Q1_the_effort_is_carried_when_its_record_cannot_be_written(scripted, monkeypatch):
    engine, req, decider, store, _ = scripted
    monkeypatch.setattr(store, "insert", lambda *a, **kw: None)
    plan = await launch(engine, req)
    assert plan == LaunchPlan(None, (), "high")


@pytest.mark.asyncio
async def test_Q3_each_point_is_its_own_request_model_first(scripted):
    engine, req, decider, store, _ = scripted
    plan = await launch(
        engine,
        req,
        {"model.route": "auto", "effort.route": "auto"},
        default_tier="medium",
        min_tier="small",
        max_tier="large",
    )
    assert [r.points for r in decider.requests] == [("model.route",), ("effort.route",)]
    first, second = decider.requests
    assert first.request_id == second.request_id
    assert replace(first, points=()) == replace(second, points=())
    assert (plan.model, plan.effort) == ("model-l", "high")
    records = sorted(store.list(), key=lambda r: r["id"])
    assert [r["point"] for r in records] == ["model.route", "effort.route"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["model.route", "effort.route"])
async def test_Q3_one_points_failure_does_not_stop_the_other(scripted, failed):
    engine, req, decider, store, configure = scripted
    configure(on_timeout_ms=50)
    decider.failures[failed] = "timeout"
    plan = await launch(
        engine,
        req,
        {"model.route": "auto", "effort.route": "auto"},
        default_tier="medium",
        min_tier="small",
        max_tier="large",
    )
    records = {r["point"]: r for r in store.list()}
    assert records[failed]["reason"] == "timeout"
    other = ({"model.route", "effort.route"} - {failed}).pop()
    assert records[other]["outcome"] == "applied"
    assert plan.model == ("model-m" if failed == "model.route" else "model-l")
    assert plan.effort == (None if failed == "effort.route" else "high")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["on", "shadow"])
async def test_a_provider_that_cannot_apply_effort_is_never_asked(scripted, state):
    engine, req, decider, store, configure = scripted
    configure({"effort.route": state})
    seen = []

    def honors_effort(provider, name, profile):
        seen.append((provider, name, profile))
        return False

    engine.honors_effort = honors_effort
    plan = await launch(engine, req)
    await engine.runner.drain()
    assert decider.requests == []
    (record,) = store.list()
    assert record["reason"] == "not_honored"
    assert record["outcome"] == ("fallback" if state == "on" else "shadow")
    assert plan.effort is None
    # An ephemeral target loads no installed profile.
    assert seen == [("codex", req.agent_profile, None)]


@pytest.mark.asyncio
async def test_the_default_declaration_comes_from_the_provider_class(scripted):
    engine, req, decider, store, _ = scripted
    # Kiro declares no effort mapping, so it keeps the base class's False.
    plan = await launch(engine, replace(req, provider="kiro_cli"))
    assert decider.requests == []
    (record,) = store.list()
    assert record["reason"] == "not_honored" and plan.effort is None


@pytest.mark.asyncio
async def test_not_honored_effort_still_asks_the_model(scripted):
    engine, req, decider, store, _ = scripted
    engine.honors_effort = lambda *a: False
    plan = await launch(
        engine,
        req,
        {"model.route": "auto", "effort.route": "auto"},
        default_tier="medium",
        min_tier="small",
        max_tier="large",
    )
    assert [r.points for r in decider.requests] == [("model.route",)]
    assert (plan.model, plan.effort) == ("model-l", None)


@pytest.mark.asyncio
async def test_the_launch_note_stays_model_only(scripted):
    engine, req, _, store, _ = scripted
    plan = await launch(engine, req)
    engine.bind_launch(plan, terminal_id="worker", model=None, honored=True)
    assert engine.launch_note(plan) is None
