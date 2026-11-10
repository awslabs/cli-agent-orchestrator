"""#1068 on the #810 side: PolicyBounds.min_tier (MF5) and the ephemeral description fact."""

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
from cli_agent_orchestrator.decisions.targets import TargetKind
from cli_agent_orchestrator.decisions.types import POINTS

TIERS = POINTS["model.route"].options
FULL = {"codex": {tier: "model-" + tier for tier in TIERS}}
BOUNDS = (None,) + TIERS


def index(tier, missing):
    return TIERS.index(tier) if tier is not None else missing


@pytest.mark.parametrize("floor,default,ceiling", itertools.product(BOUNDS, repeat=3))
def test_MF5_floor_default_ceiling_order(floor, default, ceiling):
    bounds = PolicyBounds(default_tier=default, max_tier=ceiling, min_tier=floor)
    valid = (
        (ceiling is None or default is not None)
        and (floor is None or default is not None)
        and index(floor, 0) <= index(default, 0) <= index(ceiling, 2)
        and index(floor, 0) <= index(ceiling, 2)
    )
    if valid:
        bounds.validate(FULL)
    else:
        with pytest.raises(PolicyConfigError):
            bounds.validate(FULL)


def test_MF5_floor_value_is_in_the_closed_set():
    with pytest.raises(PolicyConfigError):
        PolicyBounds(default_tier="medium", min_tier="huge").validate(FULL)


@pytest.mark.parametrize(
    "floor,default,ceiling,required",
    [
        ("medium", "medium", "large", {"medium", "large"}),
        ("large", "large", "large", {"large"}),
        (None, "small", "medium", {"small", "medium"}),
        ("medium", "large", None, {"large"}),
    ],
)
def test_MF5_mappings_run_from_floor_to_ceiling(floor, default, ceiling, required):
    for missing in TIERS:
        tiers = {"codex": {t: "model-" + t for t in TIERS if t != missing}}
        bounds = PolicyBounds(
            default_tier=default,
            max_tier=ceiling,
            min_tier=floor,
            allowed_providers=frozenset({"codex"}),
        )
        if missing in required:
            with pytest.raises(PolicyConfigError, match="model_tiers.codex." + missing):
                bounds.validate(tiers)
        else:
            bounds.validate(tiers)


def test_MF5_explicit_below_floor_refuses_before_ceiling_and_mapping():
    bounds = PolicyBounds(default_tier="medium", max_tier="large", min_tier="medium")
    with pytest.raises(BelowFloorError) as err:
        bounds.check_explicit("model.route", "small", "codex", {})
    assert isinstance(err.value, PolicyViolation)
    assert err.value.reason == "below_floor" and "below_floor" in POLICY_REJECTION_REASONS
    bounds.check_explicit("model.route", "medium", "codex", FULL)
    # The floor is a tier floor; effort has none.
    bounds.check_explicit("effort.route", "low", "codex", {})


@pytest.mark.parametrize(
    "floor,ceiling",
    [
        pair
        for pair in itertools.product(BOUNDS, repeat=2)
        if index(pair[0], 0) <= index(pair[1], 2)
    ],
)
def test_MF5_cap_clamps_into_floor_and_ceiling(floor, ceiling):
    bounds = PolicyBounds(min_tier=floor, max_tier=ceiling)
    for option in TIERS:
        expected = TIERS[min(max(TIERS.index(option), index(floor, 0)), index(ceiling, 2))]
        assert bounds.cap("model.route", option) == (expected, expected != option)
    assert bounds.cap("effort.route", "low") == ("low", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,expected", [("small", "medium"), ("large", "medium")])
async def test_MF5_engine_records_an_answer_capped_into_range(setup_engine, answer, expected):
    engine, req, decider, store, _, _ = setup_engine
    decider.option = answer
    plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind.EPHEMERAL,
            field_states={"model.route": "auto"},
            policy=PolicyBounds(
                default_tier="medium",
                max_tier="medium",
                min_tier="medium",
                allowed_providers=frozenset({"codex"}),
            ),
        )
    )
    assert plan.model == "model-m"
    (record,) = store.list()
    assert record["outcome"] == "capped" and record["answer"] == answer
    assert record["applied_value"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["ephemeral", "installed"])
async def test_description_reaches_a_decider_redacted_and_only_for_ephemeral(setup_engine, target):
    engine, req, decider, store, events, _ = setup_engine
    secret = "ghp_" + "z" * 36
    plan = await engine.prepare_launch(
        replace(
            req,
            target_kind=TargetKind(target),
            field_states={"model.route": "auto"},
            description="description-sentinel " + secret,
        )
    )
    seen = decider.requests[0].profile_description
    if target == "ephemeral":
        assert "description-sentinel" in seen and secret not in seen and "[REDACTED:" in seen
    else:
        # An installed target keeps its own profile's description.
        assert seen == "worker"
    engine.bind_launch(plan, terminal_id="worker", model=plan.model, honored=True)
    assert "description-sentinel" not in str(store.list()) + str(events)


@pytest.mark.asyncio
async def test_ephemeral_without_description_sends_none(setup_engine):
    engine, req, decider, _, _, _ = setup_engine
    await engine.prepare_launch(
        replace(req, target_kind=TargetKind.EPHEMERAL, field_states={"model.route": "auto"})
    )
    assert decider.requests[0].profile_description is None


@pytest.mark.asyncio
async def test_an_installed_target_without_a_profile_sends_no_description(setup_engine):
    engine, req, decider, _, _, _ = setup_engine
    engine.profile_loader = lambda _: None
    await engine.prepare_launch(
        replace(req, field_states={"model.route": "auto"}, description="description-sentinel")
    )
    assert decider.requests[0].profile_description is None
